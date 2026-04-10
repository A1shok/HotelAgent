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

    prompt = f"""
You are a hotel concierge decision engine.

You DO NOT reply to user.
You decide system actions.

------------------------
CONTEXT
------------------------
{json.dumps(structured_state)}

------------------------
USER MESSAGE
------------------------
"{message}"

------------------------
POSSIBLE ACTIONS
------------------------
- create_task(category)
- mark_complete(task_id)
- cancel_task(task_id)
- followup_status(task_id)
- ask_clarification
- ignore

------------------------
CRITICAL RULES
------------------------

1. MULTI-INTENT:
If user asks multiple things → return MULTIPLE actions

Example:
"I need towels and water"
→ [
  {{"action": "create_task", "category": "towels"}},
  {{"action": "create_task", "category": "water"}}
]

---

2. GREETING / NOISE:
If message is greeting, typo, or irrelevant:
→ return [{{"action": "ignore"}}]

Examples:
"hi", "ok", "hello", "hmm", "typo"
→ ignore

---

3. COMPLETION:
If user says:
"done", "fixed", "thanks fixed", "resolved"

AND:
- only 1 active task → complete it
- multiple tasks → use category if mentioned
- if unclear → ask_clarification

---

4. FOLLOW-UP:
If user says:
"where is it", "not received", "still not"

→ pick MOST RECENT active task
→ followup_status(task_id)

---

5. CANCEL:
If user says cancel:
- if 1 task → cancel it
- if multiple → match category
- if unclear → ask_clarification

---

6. DUPLICATE:
If same task already active:
→ DO NOT create again

---

7. INFO REQUEST:
If user asks info (wifi, menu, breakfast):
→ return ignore
(handled by response layer separately)

---

8. PRIORITY:
Always prefer:
- category match
- latest task

---

------------------------
OUTPUT FORMAT (VERY IMPORTANT)
------------------------

Return JSON ARRAY ONLY

Example:
[
  {{"action": "create_task", "category": "towels"}},
  {{"action": "create_task", "category": "water"}}
]

OR

[
  {{"action": "mark_complete", "task_id": "123"}}
]

OR

[
  {{"action": "ask_clarification"}}
]

NO TEXT. ONLY JSON ARRAY.
"""

    res = client.chat.completions.create(
        model="gpt-5-mini",
        messages=[{"role": "user", "content": prompt}]
    )

    try:
        return json.loads(res.choices[0].message.content)
    except:
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
        "ignore"
    }

    cleaned = []

    for d in decisions:
        if d.get("action") in valid:
            cleaned.append(d)

    return cleaned if cleaned else [{"action": "ask_clarification"}]


# -----------------------
# DECISION → ACTIONS
# -----------------------

def decision_to_actions(decision):

    action_map = {
        "create_task": "created",
        "mark_complete": "completed",
        "cancel_task": "cancelled",
        "ask_clarification": "ambiguous",
        "followup_status": "escalation",
        "ignore": "ignore"
    }

    action = decision.get("action")
    mapped = action_map.get(action, "unknown")

    if decision.get("category"):
        return [{"action": mapped, "category": decision.get("category")}]

    return [{"action": mapped}]


# -----------------------
# EXECUTION ENGINE
# -----------------------

def execute(decision, db, room):

    action = decision.get("action")
    category = decision.get("category")

    active_tasks = db.query(Task).filter(
        Task.room == room,
        Task.status == "active"
    ).all()

    # CREATE
    if action == "create_task":

        for t in active_tasks:
            if t.category == category:
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
                if t.category == category:
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
                if t.category == category:
                    t.status = "cancelled"
                    db.commit()
                    return t

        return None

    return None


# -----------------------
# RESPONSE ENGINE (LLM)
# -----------------------

def generate_response(actions):

    prompt = f"""
You are a hotel WhatsApp concierge.

You MUST generate a reply STRICTLY based on the given actions.

CRITICAL RULES:
- Do NOT invent anything
- Do NOT mention things not in actions
- Do NOT use generic phrases like "request received"
- Keep it short (1 sentence)
- Sound natural and human

Action meanings:
- created → say what is being sent
- duplicate → already working
- cancelled → confirm cancellation
- completed → acknowledge completion
- escalation → urgent handling
- ambiguous → ask which request
- ignore → empty reply

Actions:
{actions}

Generate ONLY the reply.
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

    try:
        print("STEP 1: message received")

        form = await req.form()
        msg = form.get("Body")
        phone = form.get("From")

        room = phone[-3:]

        print("📩", msg)

        db: Session = SessionLocal()

        tasks = db.query(Task).filter(Task.room == room).all()

        # DECISION
        decisions = llm_decide(msg, tasks)
        print("🧠 decision:", decisions)

        decisions = validate(decisions)

        all_actions = []

        for decision in decisions:
            execute(decision, db, room)
            actions = decision_to_actions(decision)
            all_actions.extend(actions)

        reply = generate_response(all_actions)

        print("💬 reply:", reply)

        resp.message(reply if reply else "👍")

    except Exception as e:
        print("❌ ERROR:", str(e))
        resp.message("Working on it 👍")

    return Response(content=str(resp), media_type="application/xml")
