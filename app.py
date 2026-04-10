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

You DO NOT reply to the user.
You ONLY decide system actions.

--------------------------------
CONTEXT (STATE)
--------------------------------
{json.dumps(structured_state)}

--------------------------------
USER MESSAGE
--------------------------------
"{message}"

--------------------------------
AVAILABLE ACTIONS
--------------------------------
- create_task(category)
- mark_complete(task_id)
- cancel_task(task_id)
- followup_status(task_id)
- info_request(query)
- ask_clarification
- ignore

--------------------------------
BEHAVIOR RULES (STRICT)
--------------------------------

1. MULTI-INTENT
If user asks multiple things → return multiple actions

Example:
"I need towels and water"
→ [
  {{"action":"create_task","category":"towels"}},
  {{"action":"create_task","category":"water"}}
]

---

2. DIRECT REQUEST (VERY IMPORTANT)
If user clearly asks for something (need, send, give, want)
→ ALWAYS create_task

---

3. ROOM ISSUES / COMPLAINTS
If user describes problem:
dirty, hot, smell, not clean, AC not working

→ map to task:

dirty → cleaning  
AC not working → ac  
hot room → ac  

→ create_task

---

4. COMPLETION
If user says:
"done", "fixed", "thanks fixed", "resolved"

→ if 1 active task → mark_complete  
→ if multiple:
   - use category if present
   - else ask_clarification

---

5. FOLLOW-UP / STATUS
If user says:
"where is it", "not received", "still not", "hello???", "any update"

→ pick MOST RECENT active task  
→ followup_status(task_id)

---

6. CANCEL
If user says cancel:

- if 1 task → cancel it  
- if multiple:
   - match category if given
   - else ask_clarification

---

7. PARTIAL FOLLOW-UP
If user says:
"what about towels", "and shampoo?"

→ create_task for that item

---

8. SHORT CATEGORY
If user sends:
"ac", "towels", "water"

→ match existing task OR create new if none

---

9. INFO REQUEST (IMPORTANT)
If user asks:
wifi, menu, breakfast, timing, checkout

→ return:
{{"action":"info_request","query":"<actual intent>"}}

---

10. MIXED INTENT (CRITICAL)
If message has BOTH task + info:

"I need water and wifi password"

→ [
  {{"action":"create_task","category":"water"}},
  {{"action":"info_request","query":"wifi password"}}
]

---

11. DUPLICATE PREVENTION
If same task already active:
→ DO NOT create again

---

12. EMOTIONAL / COMPLAINT (NO TASK)
If user says:
"worst service", "bad service"

→ followup MOST RECENT task

---

13. GREETING / NOISE
If message is:
"hi", "hello", "ok", "thanks", typo

→ ignore

---

14. PRIORITY LOGIC
Always prefer:
1. category match  
2. most recent task  

---

15. PARTIAL COMPLETION (CRITICAL)

If user mentions one task completed but others exist:

Example:
"AC fixed but waiting for towels"

→ [
  {{"action":"mark_complete","category":"ac"}},
  {{"action":"followup_status","category":"towels"}}
]

NEVER mark all tasks complete unless explicitly stated.

---

16. VAGUE COMPLETION

If user says:
"done", "fixed", "thanks fixed"

AND multiple tasks exist:

→ ask_clarification

---

17. FAILURE AFTER COMPLETION

If user says:
"still not fixed", "not done", "again problem"

→ treat as ACTIVE issue

→ [
  {{"action":"create_task","category":"<same as before>"}}
]

---

18. COMPLAINT WITH CONTEXT

If user says:
"worst service", "still waiting"

→ attach to MOST RECENT task

→ followup_status

---

19. MIXED STATE MESSAGE

If user gives mixed info:

"AC fixed but towels not received"

→ [
  {{"action":"mark_complete","category":"ac"}},
  {{"action":"followup_status","category":"towels"}}
]

---

20. REPEATED / URGENT MESSAGE

If user sends:
"hello???", "any update??", repeated messages

→ followup MOST RECENT task

--------------------------------
OUTPUT FORMAT (STRICT)
--------------------------------

Return ONLY JSON ARRAY.

Examples:

[
  {{"action":"create_task","category":"towels"}}
]

[
  {{"action":"cancel_task","category":"water"}},
  {{"action":"create_task","category":"towels"}}
]

[
  {{"action":"followup_status","task_id":"123"}}
]

[
  {{"action":"info_request","query":"wifi password"}}
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
    
        # 🔥 ALWAYS RETURN LIST
        if isinstance(decision, dict):
            decision = [decision]

        return decision

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
        "info_request"
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
        "info_request": "info"
    }

    actions = []

    for decision in decisions:
        action = decision.get("action")
        mapped = action_map.get(action, "unknown")

        obj = {"action": mapped}

        if decision.get("category"):
            obj["category"] = decision.get("category")

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

    active_tasks = db.query(Task).filter(
        Task.room == room,
        Task.status == "active"
    ).all()
    # -------------------
    # RESET SESSION
    # -------------------
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

        for decision in decisions:
            execute(decision, db, room)

        # 🔥 THEN convert ALL at once
        all_actions = decision_to_actions(decisions)

        reply = generate_response(all_actions)

        print("💬 reply:", reply)

        resp.message(reply if reply else "👍")

    except Exception as e:
        print("❌ ERROR:", str(e))
        resp.message("Working on it 👍")

    return Response(content=str(resp), media_type="application/xml")
