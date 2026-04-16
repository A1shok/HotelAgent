from fastapi import FastAPI, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import json
import uuid
from datetime import datetime

from db import SessionLocal, Task

client = OpenAI()
app = FastAPI()

# -----------------------
# LLM DECISION ENGINE
# -----------------------

def llm_decide(message, db_tasks, pending_action=None):
    last_task = None

    if db_tasks:
        last_task = sorted(
            db_tasks,
            key=lambda x: x.created_at,
            reverse=True
        )[0] if db_tasks else None
        
    structured_state = {
        "active_tasks": [
            {
                "id": t.id,
                "category": t.category,
                "item": getattr(t, "item", None),
                "status": t.status,
                "created_at": str(t.created_at)
            }
            for t in db_tasks if t.status == "active"
        ],
        "recent_tasks": [
            {
                "category": t.category,
                "item": getattr(t, "item", None),
                "status": t.status
            }
            for t in db_tasks
        ],
        "last_task": [
            {
                "category": last_task.category,
                "item": getattr(last_task, "item", None)
            } if last_task else None
        ]
    }
    print("🧾 STATE:", json.dumps(structured_state, indent=2))

    prompt = f"""
You are a hotel concierge decision engine.

You DO NOT reply to the user.
You ONLY decide system actions.

--------------------------------
CONTEXT (STATE)
--------------------------------
{structured_state}

--------------------------------
USER MESSAGE
--------------------------------
"{message}"

--------------------------------
GLOBAL PRINCIPLES
--------------------------------

- Understand intent based on MEANING, not exact words
- Handle typos, Hinglish, informal language
- Always use CONTEXT (active tasks)
- Avoid duplicates
- Prefer existing tasks over new ones
- Treat examples as guidance, not exhaustive list
- Generalize similar meaning phrases automatically

STATE SIGNALS:
- "still", "again", "not yet" -> follow-up
- "fixed", "done", "received" -> completion

- NEVER assume completion if multiple tasks exist
- If unsure -> ask_clarification (never guess)

PRIORITY:
category match > semantic match > recent task > clarification

--------------------------------
OPERATIONAL CATEGORIES (IMPORTANT)
--------------------------------

Map ALL user requests into these hotel departments:

- engineering (AC, TV, geyser, lights, fan, switches, remote, electrical issues)
- housekeeping (towels, bedsheets, cleaning, room hygiene, toiletries)
- fnb (food, water, menu, breakfast, beverages)
- it (wifi, internet, connectivity, TV signal)
- guest_service (fallback if unclear)

IMPORTANT RULES:

For every task, identify BOTH:

- category (department)
- item (specific object or issue)

Examples:

"ac not working"
-> {{"action":"create_task","category":"engineering","item":"ac"}}

"tv remote"
-> {{"action":"create_task","category":"engineering","item":"tv_remote"}}

"need towels"
-> {{"action":"create_task","category":"housekeeping","item":"towels"}}

"wifi not working"
-> {{"action":"create_task","category":"it","item":"wifi"}}

- Users may mention specific items (like "geyser", "remote", "blanket")
  -> Map them to the closest department

- DO NOT create new categories
- DO NOT use item names as categories

- If mapping is unclear:
  -> ask_clarification

--------------------------------
EXAMPLES OF MAPPING
--------------------------------

- "ac not working", "room hot", "no cooling" -> engineering
- "tv remote", "geyser issue", "lights not working" -> engineering
- "towels", "bedsheet", "dirty room" -> housekeeping
- "water", "food", "menu", "breakfast" -> fnb
- "wifi not working", "internet slow" -> it

--------------------------------
AVAILABLE ACTIONS
--------------------------------

- create_task(category)
- mark_complete(category)
- cancel_task(category)
- followup_status(category)
- info_request(query)
- ask_clarification
- ignore

--------------------------------
1. MULTI-INTENT (CORE)
--------------------------------

- A message may contain multiple intents
- Split into independent intents
- Each intent -> ONE action
- DO NOT ignore any part of message

Examples:

"need water and towels"
-> [
{{"action":"create_task","category":"fnb","item":"water"}},
{{"action":"create_task","category":"housekeeping","item":"towels"}}
]

"ac not working and wifi password"
-> [
{{"action":"create_task","category":"engineering","item":"ac"}},
{{"action":"info_request","query":"wifi password"}}
]

"cancel water and send towels"
-> [
{{"action":"cancel_task","category":"fnb","item":"water"}},
{{"action":"create_task","category":"housekeeping","item":"towels"}}
]

--------------------------------
2. DIRECT REQUEST
--------------------------------

If user asks for service:

-> create_task(mapped department)

If informational:

-> info_request(query)

IMPORTANT:
- Problems -> ALWAYS task
- Info -> NEVER task

--------------------------------
3. FOLLOW-UP / STATUS
--------------------------------

Detect:

"where is it", "still waiting", "not received", "any update"

RESOLUTION:

-If category AND item match -> followup_status
-If category matches but item is DIFFERENT -> create_task
- If one active task -> follow it
- If multiple:
  -> prefer most recent
  -> else ask_clarification

URGENCY:

"hello???", "??", repeated messages

-> If tasks exist -> follow most relevant
-> If none -> ask_clarification
-> NEVER ignore

FAILURE SIGNAL:

"still not working", "not fixed", "again", "again problem", "still not received"

RESOLUTION LOGIC (VERY IMPORTANT):

1. Check active_tasks:
   - If matching task (same category + item) exists:
     -> followup_status

2. Else check recent_tasks:
   - If a matching task was previously completed:
     -> create_task (REOPEN)

3. Else:
   - If user clearly mentions item (e.g. "AC again"):
       -> create_task
   - If vague (e.g. "again", "still not working"):
       -> ask_clarification
--------------------------------
4. COMPLETION
--------------------------------

"done", "fixed", "received"

- If one task -> mark_complete
- If category mentioned -> mark_complete(category)

PARTIAL:

"AC fixed but still waiting for towels"

-> [
{{"action":"mark_complete","category":"engineering","item":"ac"}},
{{"action":"followup_status","category":"housekeeping","item":"towels"}}
]

--------------------------------
5. VAGUE COMPLETION
--------------------------------

- one task -> mark_complete
- multiple -> ask_clarification

--------------------------------
6. CANCELLATION
--------------------------------

Detect cancellation intent based on MEANING, not exact words.

Examples of cancellation intent:

- "no need"
- "don't send"
- "leave it"
- "cancel it"
- "not required"
- "mat bhejo"
- "rehne do"
- "chahiye nahi"

→ treat ALL as cancel_task

"cancel water", "no need towels"

-> cancel_task(mapped category)

If vague:

- one task -> cancel
- multiple -> ask_clarification

If vague:

- one task -> cancel
- multiple -> ask_clarification

--------------------------------
7. ADD-ON / CONTINUATION
--------------------------------

"and towels", "also water"

-> create_task(mapped category)

If already exists:
-> followup_status (NOT duplicate)

--------------------------------
8. SHORT INPUT
--------------------------------

"ac", "water", "towels"

If active task exists with SAME category AND SAME item:
-> followup_status (NOT create_task)
- If no active task -> create_task
- If unclear -> ask_clarification

--------------------------------
9. INFO REQUEST
--------------------------------

"wifi password", "menu", "timing"

-> info_request

BUT:

"wifi not working"
-> create_task(category = it)

--------------------------------
10. LOW-INTENT / EMOTIONAL
--------------------------------

"hi", "ok", "thanks", "👍"
"this is bad", "not happy"

-> ignore

EXCEPTION:
If tied to task -> follow-up

--------------------------------
11. DUPLICATE PREVENTION
--------------------------------

Before create_task:

If active task exists in same department:
-> followup_status (NOT create_task)

--------------------------------
12. CONFLICT RESOLUTION
--------------------------------

If multiple rules apply:

1. department match
2. semantic meaning
3. clear intent
4. single task
5. recent task
6. ask_clarification

--------------------------------
OUTPUT FORMAT
--------------------------------

Return ONLY JSON ARRAY.

Examples:

[
{{"action":"create_task","category":"engineering","item":"ac"}}
]

[
{{"action":"followup_status","category":"housekeeping","item":"towels"}}
]

[
{{"action":"ask_clarification"}}
]

NO TEXT. NO EXPLANATION. ONLY JSON.
"""

    res = client.chat.completions.create(
        model="gpt-5-mini",
        messages=[{"role": "user", "content": prompt}]
    )

    try:
        decision = json.loads(res.choices[0].message.content)

        if isinstance(decision, dict):
            decision = [decision]

        return decision

    except Exception as e:
        print("Decision parse error:", str(e))
        return [{"action": "ask_clarification"}]


# -----------------------
# VALIDATION
# -----------------------

def validate(decisions):

    valid = {
        "create_task",
        "mark_complete",
        "cancel_task",
        "ask_clarification",
        "followup_status",
        "ignore",
        "info_request",
        "reset_session"
    }

    cleaned = []

    for d in decisions:
        if d.get("action") in valid:
            cleaned.append(d)

    return cleaned if cleaned else [{"action": "ask_clarification"}]


# -----------------------
# DECISION → ACTIONS
# -----------------------

def decision_to_actions(decisions):

    action_map = {
        "create_task": "created",
        "mark_complete": "completed",
        "cancel_task": "cancelled",
        "ask_clarification": "ambiguous",
        "followup_status": "escalation",
        "ignore": "ignore",
        "info_request": "info",
        "reset_session": "ignore"
    }

    actions = []

    for decision in decisions:
        action = decision.get("action")
        mapped = action_map.get(action, "unknown")

        obj = {"action": mapped}

        if decision.get("category"):
            obj["category"] = decision.get("category").lower()

        if decision.get("query"):
            obj["query"] = decision.get("query")

        actions.append(obj)

    return actions


# -----------------------
# EXECUTION ENGINE
# -----------------------

def execute(decision, db, room):

    action = decision.get("action")
    category = decision.get("category")
    item = decision.get("item")  # 🔥 ADDED

    if category:
        category = category.lower()

    active_tasks = db.query(Task).filter(
        Task.room == room,
        Task.status == "active"
    ).all()

    # RESET SESSION
    if action == "reset_session":
        db.query(Task).filter(
            Task.room == room,
            Task.status == "active"
        ).update({"status": "completed"})
        db.commit()
        return None

    # CREATE
    if action == "create_task":
        existing = db.query(Task).filter(
            Task.room == room,
            Task.category == category,
            Task.item == item,
            Task.status == "active"
        ).first()

        if existing:
            return existing

        task = Task(
            id=str(uuid.uuid4()),
            room=room,
            category=category,
            item=item,  # 🔥 ADDED
            status="active",
            created_at=datetime.utcnow()
        )
        db.add(task)
        db.flush() 
        db.commit()
        return task

    # COMPLETE
    if action == "mark_complete":
        if len(active_tasks) == 1:
            task = active_tasks[0]
            task.status = "completed"
            db.commit()
            return task

        if category:
            for t in active_tasks:
                if t.category.lower() == category and (item is None or getattr(t, "item", None) == item):  # 🔥 UPDATED
                    t.status = "completed"
                    return t
            db.commit()

        return None

    # CANCEL
    if action == "cancel_task":
        if len(active_tasks) == 1:
            task = active_tasks[0]
            task.status = "cancelled"
            db.commit()
            return task

        if category:
            for t in active_tasks:
                if t.category.lower() == category and (item is None or getattr(t, "item", None) == item):  # 🔥 UPDATED
                    t.status = "cancelled"
                    db.commit()
                    return t

        return None

    return None


# -----------------------
# RESPONSE ENGINE
# -----------------------

def generate_response(actions):

    prompt = prompt = f"""
You are a hotel WhatsApp concierge.

Your job is to generate a short, natural reply to the guest based ONLY on the given structured actions.

--------------------------------
INPUT ACTIONS (STRICT JSON)
--------------------------------
{actions}

--------------------------------
CRITICAL RULES
--------------------------------

1. ONLY use information from actions
   - DO NOT invent anything
   - DO NOT assume missing details

2. Convert internal categories into guest-friendly language:

   engineering -> "the issue"
   housekeeping -> "housekeeping"
   fnb -> "your request"
   it -> "WiFi"
   guest_service -> "this"

3. Response style:
   - 1 short sentence (max 12–15 words)
   - Friendly, human, WhatsApp tone
   - Maximum 1 emoji
   - No repetition

4. Multi-action handling (VERY IMPORTANT):

   - Combine naturally into ONE sentence
   - Do NOT repeat "Sending..." twice
   - Do NOT list categories

   Example:
   Actions:
   [
     {{"action":"created","category":"engineering"}},
     {{"action":"created","category":"housekeeping"}}
   ]

   Good:
   "Sending someone to check and housekeeping right away 👍"

   Bad:
   "Sending engineering 👍 Sending housekeeping 👍"

5. Action mapping:

   created ->
     "Sending <mapped phrase>"

   escalation ->
     "Checking <mapped phrase>"

   completed ->
     "Glad it's sorted"

   cancelled ->
     "Done, cancelled"

   ambiguous ->
     "Which request do you mean?"

   info ->
     - Answer directly using query
     - Example:
       wifi -> "WiFi password is Hotel_Guest"
       menu -> "Sharing the menu"
       breakfast -> "Breakfast is from 7–10 AM"

   ignore ->
     "Hi 👋 how can I help you?"

6. NEVER:
   - Mention "engineering", "fnb", etc.
   - Mention system terms like "task"
   - Invent items like shampoo
   - Repeat words unnecessarily

--------------------------------
OUTPUT
--------------------------------

Return ONLY the final reply text.
No JSON. No explanation.
"""

    res = client.chat.completions.create(
        model="gpt-5-mini",
        messages=[{"role": "user", "content": prompt}]
    )

    return res.choices[0].message.content.strip()


# -----------------------
# WEBHOOK
# -----------------------

@app.post("/webhook")
async def whatsapp_webhook(req: Request):

    resp = MessagingResponse()
    db: Session = SessionLocal()

    try:
        print("STEP 1: message received")

        form = await req.form()
        msg = form.get("Body")
        phone = form.get("From")

        room = phone[-3:]

        # TEMP CLEAN DB (remove after testing)
        #db.query(Task).delete()
        #db.commit()

        print("📩", msg)
        
        tasks = db.query(Task).filter(Task.room == room).all()
        if any(d.get("action") == "ask_clarification" for d in decisions):
            pending_action = "cancel"

        decisions = llm_decide(msg, tasks, pending_action)
        print("🧠 decision:", json.dumps(decisions, indent=2))

        decisions = validate(decisions)

        for decision in decisions:
            execute(decision, db, room)
        db.query(Task).filter(
            Task.room == room,
            Task.status != "active"
        ).delete()
        db.commit()
        tasks_after = db.query(Task).filter(Task.room == room).all()
        print("📦 DB AFTER WRITE:", [
            {"category": t.category, "item": getattr(t, "item", None)}
            for t in tasks_after
        ])

        all_actions = decision_to_actions(decisions)

        reply = generate_response(all_actions)

        print("💬 reply:", reply)

        resp.message(reply if reply else "👍")

    except Exception as e:
        print("❌ ERROR:", str(e))
        resp.message("Working on it 👍")

    finally:
        db.close()

    return Response(content=str(resp), media_type="application/xml")
