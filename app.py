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

STAFF_NUMBERS = ["+9198xxxx001", "+9198xxxx002"]

# 🔥 GLOBAL MEMORY (per room)
pending_actions = {}

room_to_phone = {}

# 🔥 STAFF MAPPING (ADD HERE)
DEPT_MAP = {
    "engineering": "+9198xxxx001",
    "housekeeping": "+9198xxxx002",
    "fnb": "+9198xxxx003",
    "it": "+9198xxxx004"
}
# 🔥 SLA RULES (ADD HERE)
SLA_RULES = {
    "engineering": 10,
    "housekeeping": 5,
    "fnb": 3,
    "it": 5
}

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
       "pending_confirmations": [   # 🔥 ADD THIS HERE
            {
                "category": t.category,
                "item": getattr(t, "item", None)
            }
            for t in db_tasks
            if t.status == "completed_unverified" and t.confirmation_required
         ],
        "recent_tasks": [
            {
                "category": t.category,
                "item": getattr(t, "item", None),
                "status": t.status
            }
            for t in db_tasks
        ],
        "last_task":
            {
                "category": last_task.category,
                "item": getattr(last_task, "item", None)
            } if last_task else None
    }
    print("🧾 STATE:", json.dumps(structured_state, indent=2))

    pending_context = ""

    if pending_action:
        pending_context = f"""
    --------------------------------
    PENDING ACTION CONTEXT
    --------------------------------
    Type: {pending_action.get("type")}
    Options: {pending_action.get("options")}
    """

    prompt = f"""
You are a hotel concierge decision engine.

You DO NOT reply to the user.
You ONLY decide system actions.

{pending_context}

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
CONFIRMATION HANDLING (CRITICAL)
--------------------------------

If there are pending_confirmations:

User message may be:

1. Confirmation YES:
   "yes", "done", "fixed", "ok", "ho gaya"

→ mark_complete (ONLY for that task)

2. Confirmation NO / ISSUE:
   "no", "still not working", "again problem", "not fixed"

→ create_task (REOPEN same category + item)

3. Mixed message:
   "AC fixed but towels not received"

→ [
  {"action":"mark_complete","category":"engineering","item":"ac"},
  {"action":"followup_status","category":"housekeeping","item":"towels"}
]

4. If unclear which task:
→ ask_clarification

CRITICAL:
- Confirmation applies ONLY to pending_confirmations
- NEVER assume all tasks are confirmed
- Match item if mentioned
- If vague:
  - one pending → use it
  - multiple → ask_clarification

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

4.FAILURE SIGNAL:

--------------------------------
FAILURE / REOPEN SIGNAL (CRITICAL)
--------------------------------

Detect failure or repeat issues:

"still not working", "not fixed", "again", "again problem", "still not received"

--------------------------------
RESOLUTION LOGIC (STRICT ORDER)
--------------------------------

1. If user clearly mentions item (e.g. "AC again", "wifi still not working"):

   - If SAME active task exists:
       -> followup_status

   - Else if SAME task exists in recent_tasks with status = completed:
       -> create_task (REOPEN)

   - Else:
       -> create_task

--------------------------------

2. If message is VAGUE (e.g. "again", "still not working"):

   - If ONLY ONE active task exists:
       -> followup_status

   - Else if last_task exists:
       -> use last_task category + item

       Then:

       - If SAME active task exists:
           -> followup_status

       - Else if last_task was completed:
           -> create_task (REOPEN)

   - Else:
       -> ask_clarification

--------------------------------

CRITICAL:

- NEVER ignore failure signals
- NEVER assume new task if followup is possible
- ALWAYS prefer context (active_tasks > last_task > recent_tasks)
--------------------------------
5. COMPLETION
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
6. VAGUE COMPLETION
--------------------------------

- one task -> mark_complete
- multiple -> ask_clarification

--------------------------------
7. CANCELLATION
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
                                    
IMPORTANT (AMBIGUITY HANDLING):

If user expresses cancellation intent but message is vague (e.g. "cancel it", "don't need"):

- If ONLY ONE active task exists:
  -> cancel_task

- If MULTIPLE active tasks exist:
  -> ask_clarification

--------------------------------
PENDING ACTION CONTEXT
--------------------------------

If system previously asked clarification for cancellation:

Example:
System: "Do you want to cancel AC, towels, or water?"
User: "water"

→ [
  {{"action":"cancel_task","category":"fnb","item":"water"}}
]

CRITICAL:
- DO NOT create_task
- DO NOT followup_status
- DO NOT ask again
- Treat user reply as FINAL selection

--------------------------------
8. ADD-ON / CONTINUATION
--------------------------------

"and towels", "also water"

-> create_task(mapped category)

If already exists:
-> followup_status (NOT duplicate)

--------------------------------
9. SHORT INPUT
--------------------------------

"ac", "water", "towels"

If active task exists with SAME category AND SAME item:
-> followup_status (NOT create_task)
- If no active task -> create_task
- If unclear -> ask_clarification

--------------------------------
10. INFO REQUEST
--------------------------------

"wifi password", "menu", "timing"

-> info_request

BUT:

"wifi not working"
-> create_task(category = it)

--------------------------------
11. LOW-INTENT / EMOTIONAL
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
13. CONFLICT RESOLUTION
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
            
        if decision.get("item"):
            obj["item"] = decision.get("item")

        if decision.get("query"):
            obj["query"] = decision.get("query")

        actions.append(obj)

    return actions

# -----------------------
# SLA ESCALATION ENGINE
# -----------------------

def apply_sla_escalation(db: Session):
    now = datetime.utcnow()

    tasks = db.query(Task).filter(
        Task.status.in_(["assigned", "active"])
    ).all()

    for t in tasks:
        if not t.created_at or not t.category:
            continue

        sla_minutes = SLA_RULES.get(t.category, 10)
        elapsed = (now - t.created_at).total_seconds() / 60

        if elapsed > sla_minutes:

            # 🔥 Avoid repeated escalation
            if getattr(t, "priority", None) != "escalated":

                t.priority = "escalated"
                t.updated_at = now

                print(f"""
🔥 SLA ESCALATION
Room: {t.room}
Item: {t.item}
Dept: {t.category}
Time: {round(elapsed,1)} min (SLA {sla_minutes})
""")

                # 🔥 Notify staff
                if t.assigned_to:
                    print(f"""
📩 ESCALATION MESSAGE TO STAFF ({t.assigned_to})
URGENT: Room {t.room} - {t.item} delayed. Please prioritize.
""")

    db.commit()


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
        Task.status.in_(["assigned", "active"])
    ).all()

    unverified_tasks = db.query(Task).filter(
        Task.room == room,
        Task.status == "completed_unverified"
    ).all()

    # RESET SESSION
    if action == "reset_session":
        db.query(Task).filter(
            Task.room == room,
            Task.status.in_(["assigned", "active"])
        ).update({"status": "completed"})
        db.commit()
        return None

    # -----------------------
    # CREATE
    # -----------------------
    if action == "create_task":
    
        # 🔥 REOPEN LOGIC (ONLY HERE)
        recent_tasks = db.query(Task).filter(
            Task.room == room,
            Task.category == category,
            Task.item == item
        ).all()
    
        for t in recent_tasks:
            if (
                t.status in ["completed_unverified", "completed"] and
                t.category == category and
                t.item == item
            ):
                t.status = "active"
                t.priority = "escalated"
                t.confirmation_required = False
                t.updated_at = datetime.utcnow()
                db.commit()
                return t
    
        # 🔥 DUPLICATE PREVENTION
        existing = db.query(Task).filter(
            Task.room == room,
            Task.category == category,
            Task.item == item,
            Task.status.in_(["assigned", "active"])
        ).first()
    
        if existing:
            return existing
    
        # 🔥 CREATE NEW
        task = Task(
            id=str(uuid.uuid4()),
            room=room,
            category=category,
            item=item,
            status="assigned",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
    
        task.assigned_to = DEPT_MAP.get(category)
        task.department = category
    
        db.add(task)
        db.commit()
    
        print(f"""
    📌 TASK ASSIGNED
    Room: {room}
    Item: {item}
    To: {task.assigned_to}
    """)

        return task

    # COMPLETE
    if action == "mark_complete":

        # 🔥 ONLY allow final completion from completed_unverified

        # 🎯 STRICT MATCH FIRST (category + item)
        for t in unverified_tasks:
            if (
                t.category == category and
                getattr(t, "item", None) == item
            ):
                t.status = "completed"
                t.confirmation_required = False
                t.updated_at = datetime.utcnow()
                db.commit()
                return t
    
        # 🎯 FALLBACK (ONLY if ONE unverified task exists)
        if len(unverified_tasks) == 1:
            t = unverified_tasks[0]
            t.status = "completed"
            t.confirmation_required = False
            t.updated_at = datetime.utcnow()
            db.commit()
            return t
    
        # 🎯 OTHERWISE → DO NOTHING (LLM should have asked clarification)
        return None
        
    # 🔥 FOLLOW-UP SAFETY NET (ADD HERE)
    if action == "followup_status":
        query = db.query(Task).filter(
            Task.room == room,
            Task.category == category,
            Task.status.in_(["assigned", "active"])
        )
    
        if item:
            query = query.filter(Task.item == item)
    
        existing = query.first()
    
        if not existing:
            return execute(
                {"action": "create_task", "category": category, "item": item},
                db,
                room
            )
    
        return existing

    # CANCEL
    if action == "cancel_task":
        if len(active_tasks) == 1:
            task = active_tasks[0]
            task.status = "cancelled"
            task.updated_at = datetime.utcnow()
            db.commit()
            return task

        if category:
            for t in active_tasks:
                if t.category.lower() == category and (item is None or getattr(t, "item", None) == item):  # 🔥 UPDATED
                    t.status = "cancelled"
                    t.updated_at = datetime.utcnow()
                    db.commit()
                    return t

        return "no_active_task"

    return None
# -----------------------
# 🧠 SIGNAL ENGINE (V3)
# -----------------------

def generate_signals(db, room):

    tasks = db.query(Task).filter(Task.room == room).all()
    signals = []

    # 🔁 REPEAT ISSUE
    item_count = {}
    for t in tasks:
        key = (t.room, getattr(t, "item", None))
        item_count[key] = item_count.get(key, 0) + 1

    for (room_id, item), count in item_count.items():
        if item and count >= 2:
            signals.append({
                "type": "repeat_issue",
                "room": room_id,
                "item": item,
                "count": count,
                "severity": min(count * 10, 50)
            })

    # ⏱ DELAY
    for t in tasks:
        if t.status in ["assigned", "active"]:
            minutes = (datetime.utcnow() - t.created_at).total_seconds() / 60
            if minutes > 10:
                signals.append({
                    "type": "delay",
                    "room": t.room,
                    "item": t.item,
                    "minutes": int(minutes),
                    "owner": t.assigned_to,
                    "severity": min(int(minutes), 30)
                })

    # 👤 STAFF LOAD
    staff_load = {}
    for t in tasks:
        if t.status in ["assigned", "active"]:
            staff_load[t.assigned_to] = staff_load.get(t.assigned_to, 0) + 1

    for staff, count in staff_load.items():
        if count >= 3:
            signals.append({
                "type": "staff_overload",
                "staff": staff,
                "count": count
            })

    return signals

# -----------------------
# ⚡ SCORING ENGINE
# -----------------------

ESCALATION_RULES = {
    "engineering": 40,
    "housekeeping": 30,
    "fnb": 25,
    "it": 30
}

def score_tasks(db, room):

    tasks = db.query(Task).filter(
        Task.room == room,
        Task.status.in_(["assigned", "active"])
    ).all()

    signals = generate_signals(db, room)

    scored = []

    for t in tasks:

        score = 0
        minutes = (datetime.utcnow() - t.created_at).total_seconds() / 60

        # ⏱ Base urgency
        score += minutes * 1.5

        # 🔥 SLA escalation boost
        if getattr(t, "priority", None) == "escalated":
            score += 25

        # 🔁 Repeat issue
        for s in signals:
            if s["type"] == "repeat_issue" and s["item"] == t.item:
                score += s["severity"]

        # 👤 Staff overload
        for s in signals:
            if s["type"] == "staff_overload" and s["staff"] == t.assigned_to:
                score += 10

        # 🧠 Category importance
        if t.category == "engineering":
            score += 10

        # 🔥 AUTO ESCALATION (correct placement)
        threshold = ESCALATION_RULES.get(t.category, 50)

        if score > threshold and getattr(t, "priority", None) != "escalated":
            t.priority = "escalated"
            t.updated_at = datetime.utcnow()

        scored.append({
            "task": t,
            "score": score,
            "minutes": int(minutes)
        })

    db.commit()

    scored.sort(key=lambda x: x["score"], reverse=True)

    return scored, signals

# -----------------------
# 🌍 GLOBAL SIGNALS
# -----------------------

def global_signals(db):

    tasks = db.query(Task).all()

    dept_count = {}

    for t in tasks:
        if t.status in ["assigned", "active"]:
            dept_count[t.category] = dept_count.get(t.category, 0) + 1

    signals = []

    for dept, count in dept_count.items():
        if count >= 5:
            signals.append({
                "type": "department_spike",
                "department": dept,
                "count": count
            })

    return signals

# -----------------------
# RESPONSE ENGINE
# -----------------------

def generate_response(actions, signals):

    prompt = f"""
You are a premium hotel WhatsApp concierge.

Your job is to generate a short, warm, and professional reply to the guest based ONLY on the given structured actions.

--------------------------------
INPUT ACTIONS (STRICT JSON)
--------------------------------
{actions}

--------------------------------
CONTEXT SIGNALS
--------------------------------
{signals}

Interpret signals:
- has_delay → use apology tone
- has_repeat → add ownership ("personally")
- has_overload → reassure guest

--------------------------------
TONE & STYLE (VERY IMPORTANT)
--------------------------------

- Warm, polite, and human
- Slightly premium hotel tone (not robotic)
- Show ownership ("I’ll check", "We’ll arrange")
- Reassuring and service-oriented
- WhatsApp-friendly (short and natural)

--------------------------------
RESPONSE RULES
--------------------------------

1. Keep it SHORT:
   - 1 sentence preferred (max 12–15 words)
   - Natural conversational flow

2. Use soft hospitality language:
   - "right away"
   - "just now"
   - "I’ll check"
   - "on it"
   - "happy to help"

3. NEVER:
   - Mention "task", "category"
   - Sound mechanical or robotic
   - Repeat phrases

--------------------------------
EMOJI USAGE (IMPORTANT)
--------------------------------

- Use at most ONE emoji (optional, not mandatory)
- Choose emoji based on context and tone

GUIDELINES:

- Service / action → 👍 🙂
- Follow-up / checking → 🙂
- Confirmation / completion → 😊 👍
- Apology / delay → 🙏
- Clarification → 🙂
- Info sharing → 😊
- Issue / complaint → avoid overly cheerful emojis

- DO NOT force emoji in every response
- If message is serious → no emoji or use 🙏

--------------------------------
CATEGORY MAPPING
--------------------------------

engineering → "the issue"
housekeeping → "housekeeping"
fnb → "your request"
it → "WiFi"

--------------------------------
ACTION MAPPING (UPGRADED)
--------------------------------

created →
- "I’ll have this arranged right away"
- "On it, we’ll take care of this right away"
- "Sending this across right away"

escalation →
- "Let me check this for you right away"
- "I’m checking this now, will update you shortly"

completed →
- "Glad that’s sorted"
- "Happy to hear it’s fixed"

cancelled →
- "Done, I’ve cancelled that for you"

ambiguous →
- "Could you let me know which request you mean?"

info →
- wifi → "WiFi password is Hotel_Guest"
- parking → "Yes, parking is available"
- menu → "Sharing the menu with you"
- breakfast → "Breakfast is served from 7–10 AM"
- no active request → "There’s no active request for that right now"

ignore →
- "Hi 👋 how can I assist you today?"
--------------------------------
MULTI-ACTION HANDLING (STRICT)
--------------------------------

If info_request exists:
→ ALWAYS give the information DIRECTLY

If combined with create_task:
→ Split naturally:
- give info instantly
- confirm service separately

Example:

Input:
[create_task(towels), info_request(wifi)]

Output:
"WiFi password is Hotel_Guest, and I’ll send towels right away 👍"

CRITICAL:
- NEVER say "I’ll share" for known info
- ALWAYS provide info immediately

--------------------------------
PARTIAL COMPLETION (CRITICAL)
--------------------------------

If actions include BOTH:
- completed
- escalation (follow-up)

→ ALWAYS:

1. Acknowledge completion FIRST
2. Then handle pending request

Example:

Input:
[completed(ac), escalation(towels)]

Output:
"Glad that’s fixed, I’ll check on the towels right away 👍"

CRITICAL:
- NEVER ignore completion
- NEVER respond with only follow-up

--------------------------------
URGENCY HANDLING
--------------------------------

If action = escalation AND issue is delayed or repeated:

→ Use stronger ownership tone:

- "I’m personally checking this right away"
- "Let me get this prioritized immediately"

If severe:
→ include light apology tone (🙏)

Example:
"I’m really sorry for the delay, I’m getting this prioritized right away 🙏"

--------------------------------
DUPLICATE ACTION HANDLING
--------------------------------

If multiple actions of same type:
→ Merge into ONE natural sentence

Example:

Bad:
"I’ll send towels and I’ll send water"

Good:
"I’ll send that right away 👍"

--------------------------------
ITEM AWARENESS (IMPORTANT)
--------------------------------

If item is available:
→ Use natural reference

Examples:

engineering + ac → "AC issue"
housekeeping + towels → "towels"
it + wifi → "WiFi"

Example:
"I’ll check the AC right away"
NOT:
"I’ll check the issue"
CRITICAL:
- ALWAYS prefer item over generic words
-If item exists in actions:
→ You MUST explicitly mention it in the response
- Only fallback to generic if item missing

--------------------------------
IGNORE HANDLING (STRICT)
--------------------------------

If action = ignore:

- If greeting → respond
- If acknowledgment (thanks, ok, 👍) → DO NOT reply

--------------------------------
OWNERSHIP BOOST (PREMIUM)
--------------------------------

Prefer:

- "I’ll personally check this"
- "I’ll take care of this for you"
- "I’ll get this sorted right away"

Over generic responses.

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

async def handle_staff(req: Request):
    db: Session = SessionLocal()
    resp = MessagingResponse()

    form = await req.form()
    msg = form.get("Body")
    phone = form.get("From")

    tasks = db.query(Task).filter(
        Task.assigned_to == phone,
        Task.status.in_(["assigned", "active", "completed_unverified"])
    ).all()
    
    working_tasks = db.query(Task).filter(
        Task.assigned_to == phone,
        Task.status.in_(["assigned", "active"])
    ).all()
    
    review_tasks = db.query(Task).filter(
        Task.assigned_to == phone,
        Task.status == "completed_unverified"
    ).all()

    # ACCEPT TASK
    if msg == "1" and len(tasks) == 1:
        task = tasks[0]
        task.status = "active"
        db.commit()

        resp.message("Task accepted 👍")
        return Response(str(resp), media_type="application/xml")

    # LIST TASKS
    if msg.lower() == "tasks":
        text = "📋 Tasks:\n"
        for i, t in enumerate(tasks):
            text += f"{i+1}. Room {t.room} - {t.item}\n"

        resp.message(text)
        return Response(str(resp), media_type="application/xml")

    # COMPLETE TASK
    if msg.isdigit():
        idx = int(msg) - 1

        if 0 <= idx < len(tasks):
            task = tasks[idx]

            task.status = "completed_unverified"
            task.confirmation_required = True
            task.updated_at = datetime.utcnow()
            db.commit()

            # notify guest
            # (you’ll map room → phone later)
            guest_phone = room_to_phone.get(task.room)

            if guest_phone:
                print(f"""
            📩 MESSAGE TO GUEST ({guest_phone})
            Has the {task.item} issue been resolved?
            """)

            resp.message("Marked done 👍")
            return Response(str(resp), media_type="application/xml")

    resp.message("Invalid input")
    return Response(str(resp), media_type="application/xml")


# -----------------------
# WEBHOOK
# -----------------------

@app.post("/webhook")
async def whatsapp_webhook(req: Request):

    resp = MessagingResponse()
    db: Session = SessionLocal()
    # 🔥 ADD THIS EXACTLY HERE
    apply_sla_escalation(db)
    decisions = [{"action": "ask_clarification"}]

    try:
        print("STEP 1: message received")

        form = await req.form()
        msg = (form.get("Body") or "").strip()
        phone = form.get("From")

        # 🔥 STAFF FLOW (ADD HERE)
        if phone in STAFF_NUMBERS:
            return await handle_staff(req)

        room = phone[-3:]
        room_to_phone[room] = phone

        # TEMP CLEAN DB (remove after testing)
        #db.query(Task).delete()
        #db.commit()

        print("📩", msg)
        
        tasks = db.query(Task).filter(Task.room == room).all()

        # 🔥 GET PENDING ACTION FROM MEMORY
        pending = pending_actions.get(room)
            
        decisions = llm_decide(msg, tasks, pending)
        print("🧠 decision:", json.dumps(decisions, indent=2))

        decisions = validate(decisions)
        # 🔥 HANDLE CLARIFICATION MEMORY
        if any(d.get("action") == "ask_clarification" for d in decisions):

            active_tasks = db.query(Task).filter(
                Task.room == room,
                Task.status.in_(["assigned", "active"])
            ).all()
        
            if len(active_tasks) > 1:
        
                # 🔥 DETECT TYPE FROM ORIGINAL INTENT (LLM OUTPUT)
                action_type = "followup"  # default
        
                for d in decisions:
                    if d.get("action") == "cancel_task":
                        action_type = "cancel"
                        break
        
                pending_actions[room] = {
                    "type": action_type,
                    "options": [
                        {
                            "category": t.category,
                            "item": getattr(t, "item", None)
                        }
                        for t in active_tasks
                    ]
                }

        results = []

        for decision in decisions:
            result = execute(decision, db, room)
        
            if result == "no_active_task":
                decisions = [{"action": "info_request", "query": "no active request"}]
                break

        # 🔥 CLEAR pending after successful resolution
        if room in pending_actions:
            if not any(d.get("action") == "ask_clarification" for d in decisions):
                pending_actions.pop(room, None)
        
        db.commit()
        # -----------------------
        # 🧠 V3 INTELLIGENCE OUTPUT
        # -----------------------
        
        scored_tasks, signals = score_tasks(db, room)
        global_sig = global_signals(db)

        print("🌍 GLOBAL SIGNALS:", global_sig)
        signal_summary = {
            "has_delay": any(s["type"] == "delay" for s in signals),
            "has_repeat": any(s["type"] == "repeat_issue" for s in signals),
            "has_overload": any(s["type"] == "staff_overload" for s in signals)
        }
        
        if scored_tasks:
            top = scored_tasks[0]
        
            print(f"""
        🔥 HERO DECISION
        Room: {top['task'].room}
        Item: {top['task'].item}
        Score: {top['score']}
        Minutes: {top['minutes']}
        """)
        
        print("🧠 SIGNALS:", signals)
        tasks_after = db.query(Task).filter(Task.room == room).all()
        print("📦 DB AFTER WRITE:", [
            {"category": t.category, "item": getattr(t, "item", None)}
            for t in tasks_after
        ])

        all_actions = decision_to_actions(decisions)

        # 🔥 IGNORE HANDLING (ADD HERE)
        if all_actions and all(a["action"] == "ignore" for a in all_actions):
            return Response("", media_type="application/xml")

        reply = generate_response(all_actions, signal_summary)

        print("💬 reply:", reply)

        resp.message(reply if reply else "👍")

    except Exception as e:
        print("❌ ERROR:", str(e))
        resp.message("Working on it 👍")

    finally:
        db.close()

    return Response(content=str(resp), media_type="application/xml")

# -----------------------
# 🚀 V3 DECISION API
# -----------------------

@app.get("/v3/decision/{room}")
def get_decision(room: str):

    db = SessionLocal()

    scored, signals = score_tasks(db, room)
    global_sig = global_signals(db)

    if not scored:
        return {"status": "ok"}

    top = scored[0]["task"]

    return {
        "hero": {
            "room": top.room,
            "item": top.item,
            "minutes": scored[0]["minutes"],
            "owner": top.assigned_to
        },
        "signals": signals,
        "global_signals": global_sig
    }
