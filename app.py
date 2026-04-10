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
                "status": t.status
            }
            for t in db_tasks if t.status == "active"
        ]
    }

    prompt = f"""
You are a hotel concierge decision engine.

You are NOT writing a reply.
You are deciding system behavior.

CONTEXT:
{json.dumps(structured_state)}

USER MESSAGE:
"{message}"

POSSIBLE ACTIONS:
- create_task(category)
- mark_complete(task_id)
- cancel_task(task_id)
- ask_clarification
- followup_status
- ignore

RULES:
- Use context heavily
- If duplicate → do not create new
- If strong completion + 1 task → mark_complete
- If multiple tasks + completion → ask_clarification
- If cancel → cancel correct task

OUTPUT JSON ONLY:
{{
  "action": "...",
  "task_id": "...",
  "category": "...",
  "reason": "..."
}}
"""

    res = client.chat.completions.create(
        model="gpt-5-mini",
        messages=[{"role": "user", "content": prompt}]
    )

    try:
        return json.loads(res.choices[0].message.content)
    except:
        return {"action": "ask_clarification"}


# -----------------------
# VALIDATION
# -----------------------

def validate(decision):
    valid = [
        "create_task",
        "mark_complete",
        "cancel_task",
        "ask_clarification",
        "followup_status",
        "ignore"
    ]

    if decision.get("action") not in valid:
        return {"action": "ask_clarification"}

    return decision


# -----------------------
# EXECUTION ENGINE
# -----------------------

def execute(decision, db: Session, room):

    action = decision.get("action")

    if action == "create_task":
        task = Task(
            id=str(uuid.uuid4()),
            room=room,
            category=decision.get("category"),
            status="active",
            created_at=datetime.utcnow()
        )
        db.add(task)
        db.commit()
        return task

    if action == "mark_complete":
        task = db.query(Task).filter(Task.id == decision.get("task_id")).first()
        if task:
            task.status = "completed"
            db.commit()
            return task

    if action == "cancel_task":
        task = db.query(Task).filter(Task.id == decision.get("task_id")).first()
        if task:
            task.status = "cancelled"
            db.commit()
            return task

    return None


# -----------------------
# RESPONSE ENGINE
# -----------------------

def generate_response(decision):

    prompt = f"""
You are a hotel WhatsApp concierge.

Decision:
{json.dumps(decision)}

Rules:
- Max 12 words
- Natural tone
- No generic replies
- Be specific

Generate reply:
"""

    res = client.chat.completions.create(
        model="gpt-5-mini",
        messages=[{"role": "user", "content": prompt}]
    )

    return res.choices[0].message.content.strip()


# -----------------------
# TWILIO WEBHOOK
# -----------------------

@app.post("/webhook")
async def whatsapp_webhook(req: Request):

    form = await req.form()
    message = form.get("Body")
    phone = form.get("From")

    room = phone[-4:]  # simple mapping

    db = SessionLocal()

    tasks = db.query(Task).filter(
        Task.room == room,
        Task.status == "active"
    ).all()

    decision = llm_decide(message, tasks)
    decision = validate(decision)

    execute(decision, db, room)

    reply = generate_response(decision)

    resp = MessagingResponse()
    resp.message(reply)

    return Response(content=str(resp), media_type="application/xml")
