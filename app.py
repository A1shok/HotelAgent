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

def llm_decide(message, db_tasks):

    structured_state = {
        "active_tasks": [
            {
                "id": t.id,
                "category": t.category,
                "status": t.status,
                "created_at": str(t.created_at)
            }
            for t in db_tasks if t.status == "active"
        ]
    }

    prompt = prompt = f"""
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
- "still", "again", "not yet" → follow-up
- "fixed", "done", "received" → completion

- NEVER assume completion if multiple tasks exist
- If unsure → ask_clarification (never guess)

PRIORITY:
category match > semantic match > recent task > clarification

--------------------------------
AVAILABLE ACTIONS
--------------------------------
- create_task(category)
- mark_complete(category)
- cancel_task(category)
- followup_status(category)
- info_request(query)
- ask_clarification
-reset_session
- ignore

--------------------------------
1. MULTI-INTENT (CORE)
--------------------------------

- Split message into independent intents
- Each intent → ONE action
- DO NOT ignore any part

Examples:

"need water and towels"
→ [
{{"action":"create_task","category":"water"}},
{{"action":"create_task","category":"towels"}}
]

"need water and wifi password"
→ [
{{"action":"create_task","category":"water"}},
{{"action":"info_request","query":"wifi password"}}
]

"cancel water and send towels"
→ [
{{"action":"cancel_task","category":"water"}},
{{"action":"create_task","category":"towels"}}
]

"AC fixed but towels not received"
→ [
{{"action":"mark_complete","category":"ac"}},
{{"action":"followup_status","category":"towels"}}
]

--------------------------------
2. DIRECT REQUEST
--------------------------------

Service → create_task  
Info → info_request  

IMPORTANT:
- Problems → ALWAYS task
- Info → NEVER task

--------------------------------
3. ROOM ISSUES (MAPPING)
--------------------------------

AC → "ac"  
dirty / smell → "cleaning"  

If already active → followup_status

--------------------------------
4. FOLLOW-UP / STATUS
--------------------------------

Detect:
"where is it", "still waiting", "not received", "any update"

RESOLUTION:

- category → followup_status
- semantic → followup_status
- one task → follow
- multiple → recent OR ask_clarification

URGENCY:

"hello???", "??"
→ follow most relevant

FAILURE SIGNAL:

"still not working", "not fixed", "again problem", "still not received"

→ If task was previously COMPLETED:
   → create_task (REOPEN)
→ Else:
   → followup_status

--------------------------------
5. COMPLETION
--------------------------------

"done", "fixed", "received"

- one task → mark_complete
- category → mark_complete(category)

PARTIAL:

"AC fixed but towels not received"
→ complete + follow-up

--------------------------------
6. VAGUE COMPLETION
--------------------------------

- one task → mark_complete
- multiple → ask_clarification

--------------------------------
7. CANCELLATION
--------------------------------

"cancel water"
→ cancel_task

- one task → cancel
- multiple → ask_clarification

--------------------------------
8. ADD-ON
--------------------------------

"and towels"
→ create_task

(no duplicates)

--------------------------------
9. SHORT INPUT
--------------------------------

"ac", "towels"

- exists → followup_status
- unclear → ask_clarification

--------------------------------
10. INFO REQUEST
--------------------------------

"wifi password" → info_request  
"wifi not working" → create_task  

--------------------------------
11. LOW-INTENT
--------------------------------

"hi", "ok", "thanks"
→ ignore

(EXCEPTION → if tied to task → follow-up)

--------------------------------
12. DUPLICATE PREVENTION
--------------------------------

If active exists:
→ followup_status

--------------------------------
13. CONFLICT RESOLUTION
--------------------------------

1. category  
2. semantic  
3. intent  
4. single  
5. recent  
6. clarify  

--------------------------------
OUTPUT FORMAT
--------------------------------

Return ONLY JSON ARRAY.

NO TEXT. ONLY JSON.
"""
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
        for t in active_tasks:
            if t.category.lower() == category:
                return t

        task = Task(
            id=str(uuid.uuid4()),
            room=room,
            category=category,
            status="active",
            created_at=datetime.utcnow()
        )
        db.add(task)
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
                if t.category.lower() == category:
                    t.status = "completed"
                    db.commit()
                    return t

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
                if t.category.lower() == category:
                    t.status = "cancelled"
                    db.commit()
                    return t

        return None

    return None


# -----------------------
# RESPONSE ENGINE
# -----------------------

def generate_response(actions):

    prompt = f"""
You are a hotel WhatsApp concierge.

Generate reply STRICTLY based on actions.

Actions:
{actions}

Rules:
- 1 short sentence
- Friendly
- 1 emoji max
- Combine actions naturally
- No assumptions

Mappings:

created → "Sending {{category}} 👍"
cancelled → "Cancelled {{category}} 👍"
completed → "Glad it's sorted 👍"
escalation → "Checking this for you 👍"
ambiguous → "Which request do you mean?"
info → Answer directly
ignore → "Hi 👋 how can I help you?"

Return only reply.
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

        print("📩", msg)

        tasks = db.query(Task).filter(Task.room == room).all()

        decisions = llm_decide(msg, tasks)
        print("🧠 decision:", json.dumps(decisions, indent=2))

        decisions = validate(decisions)

        for decision in decisions:
            execute(decision, db, room)

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
