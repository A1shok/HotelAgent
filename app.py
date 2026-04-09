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

    # New Logic
    if ai["intent"] == "greeting":
        return "greeting", None

    if ai["intent"] == "info_request":
        return "info", None

    if ai["intent"] == "completion":
        if tasks:
            task = tasks[0]
            task.status = "closed"
            db.commit()
            return "closed", task

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

    from twilio.twiml.messaging_response import MessagingResponse

    resp = MessagingResponse()

    try:
        print("STEP 1: message received")

        form = await req.form()

        msg = form.get("Body")
        phone = form.get("From")

        print("📩 Message:", msg)

        print("STEP 2: AI parsing start")
        ai = parse_message(msg)
        print("STEP 2 DONE:", ai)

        print("STEP 3: DB connecting")
        db = SessionLocal()
        print("STEP 3 DONE")

        print("STEP 4: decision start")
        action, task = decide(db, phone[-3:], ai)
        print("STEP 4 DONE:", action)

        reply_text = "Working on it 👍"

        if action == "create":
            reply_text = reply("task_created", {"task": ai["category"]})

        elif action == "duplicate":
            reply_text = reply("duplicate", {"task": task.category})

        elif action == "completed":
            reply_text = reply("completed", {"task": task.category})

        elif action == "escalation":
            reply_text = reply("escalation", {"task": task.category})

        elif action == "cancelled":
            reply_text = reply("cancelled", {"task": task.category})

        elif action == "ambiguous":
            reply_text = "Multiple requests active. Which one?"

        resp.message(reply_text)

    except Exception as e:
        print("❌ ERROR:", str(e))
        resp.message("Got it 👍 working on your request")

    return Response(content=str(resp), media_type="application/xml")
