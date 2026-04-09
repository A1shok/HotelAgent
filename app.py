from fastapi.responses import Response
from twilio.twiml.messaging_response import MessagingResponse
from fastapi import FastAPI, Request
from db import SessionLocal, Task
from ai import parse_message
from response_engine import reply
from datetime import datetime
import uuid

app = FastAPI()


def twilio_reply(message):
    resp = MessagingResponse()
    resp.message(message)
    return Response(content=str(resp), media_type="application/xml")


# -------------------------
# CONTROL ENGINE
# -------------------------
def decide(db, room, ai):

    tasks = db.query(Task).filter(
        Task.room == room,
        Task.status != "closed",
        Task.status != "cancelled"
    ).all()

    # TASK
    if ai["intent"] == "task":
        for t in tasks:
            if t.category == ai["category"]:
                t.quantity += 1
                db.commit()
                return "duplicate", t

        return "create", None

    # COMPLETION
    if ai["intent"] == "completion":
        if len(tasks) == 1:
            task = tasks[0]
            task.status = "completed_unverified"
            db.commit()
            return "completed", task

        if len(tasks) > 1:
            for t in tasks:
                if t.category == ai["category"]:
                    t.status = "completed_unverified"
                    db.commit()
                    return "completed", t

            return "ambiguous", None

    # NOT RECEIVED
    if ai["intent"] == "not_received":
        for t in tasks:
            if t.status == "completed_unverified":
                t.status = "in_progress"
                t.escalation_level += 1
                db.commit()
                return "escalation", t

    # CANCEL
    if ai["intent"] == "cancel":
        if tasks:
            task = tasks[0]
            task.status = "cancelled"
            db.commit()
            return "cancelled", task

    return "default", None


# -------------------------
# WEBHOOK
# -------------------------
@app.post("/webhook")
async def whatsapp_webhook(req: Request):

    form = await req.form()

    msg = form.get("Body")
    phone = form.get("From")

    print("📩", msg)

    room = phone[-3:]

    ai = parse_message(msg)

    db = SessionLocal()

    action, task = decide(db, room, ai)

    # -------------------------
    # CREATE TASK
    # -------------------------
    if action == "create":

        new_task = Task(
            id=str(uuid.uuid4()),
            room=room,
            category=ai["category"],
            status="created",
            priority="urgent" if ai["category"] == "ac" else "normal",
            escalation_level=0,
            quantity=1,
            created_at=datetime.now()
        )

        db.add(new_task)
        db.commit()

        reply_text = reply("task_created", {"task": ai["category"], "eta": "10 minutes"})

    elif action == "duplicate":
        reply_text = reply("duplicate", {"task": task.category})

    elif action == "completed":
        reply_text = reply("completed", {"task": task.category})

    elif action == "escalation":
        reply_text = reply("escalation", {"task": task.category})

    elif action == "cancelled":
        reply_text = reply("cancelled", {"task": task.category})

    elif action == "ambiguous":
        reply_text = "Multiple requests active. Which one is completed?"

    else:
        reply_text = reply("default", {})

    return twilio_reply(reply_text)
