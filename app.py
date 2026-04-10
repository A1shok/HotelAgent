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
# DECISION → ACTIONS (ADD HERE)
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
    task_id = decision.get("task_id")

    # get active tasks
    active_tasks = db.query(Task).filter(
        Task.room == room,
        Task.status == "active"
    ).all()

    # -------------------
    # CREATE TASK
    # -------------------
    if action == "create_task":

        # prevent duplicates
        for t in active_tasks:
            if t.category == category:
                return t  # already exists

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

    # -------------------
    # COMPLETE TASK
    # -------------------
    if action == "mark_complete":

        # 🔥 CASE 1: only one task → safe
        if len(active_tasks) == 1:
            task = active_tasks[0]
            task.status = "completed"
            db.commit()
            return task

        # 🔥 CASE 2: match by category
        if category:
            for t in active_tasks:
                if t.category == category:
                    t.status = "completed"
                    db.commit()
                    return t

        # 🔥 CASE 3: fallback → DO NOTHING
        return None

    # -------------------
    # CANCEL TASK
    # -------------------
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
