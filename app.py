from fastapi.responses import Response
from twilio.twiml.messaging_response import MessagingResponse
from fastapi import FastAPI, Request
from db import SessionLocal, Task
from ai import parse_message, generate_response
from datetime import datetime
import uuid

app = FastAPI()


# -------------------------
# CONTROL ENGINE (unchanged)
# -------------------------
def decide(db, room, ai):

    tasks = db.query(Task).filter(
        Task.room == room,
        Task.status != "closed",
        Task.status != "cancelled"
    ).all()

    # -------------------------
    # GREETING
    # -------------------------
    if ai["intent"] == "greeting":
        return "greeting", None

    # -------------------------
    # INFO
    # -------------------------
    if ai["intent"] == "info_request":
        return "info", None

    # -------------------------
    # TASK
    # -------------------------
    if ai["intent"] == "task":
        for t in tasks:
            if t.category == ai["category"]:
                t.quantity += 1
                db.commit()
                return "duplicate", t

        return "create", None

    # -------------------------
    # COMPLETION (FIXED)
    # -------------------------
    if ai["intent"] == "completion":

        if not tasks:
            return "default", None

        # if category mentioned → use it
        for t in tasks:
            if t.category == ai.get("category"):
                t.status = "completed_unverified"
                db.commit()
                return "completed", t

        # else → pick MOST RECENT task
        task = sorted(tasks, key=lambda x: x.created_at, reverse=True)[0]

        task.status = "completed_unverified"
        db.commit()
        return "closed", task

    # -------------------------
    # NOT RECEIVED
    # -------------------------
    if ai["intent"] == "not_received":
        for t in tasks:
            if t.status == "completed_unverified":
                t.status = "in_progress"
                t.escalation_level += 1
                db.commit()
                return "escalation", t

    # -------------------------
    # CANCEL (FIXED)
    # -------------------------
    if ai["intent"] == "cancel":

        if not tasks:
            return "default", None

        # if category mentioned → use it
        for t in tasks:
            if t.category == ai.get("category"):
                t.status = "cancelled"
                db.commit()
                return "cancelled", t

        # else → pick MOST RECENT
        task = sorted(tasks, key=lambda x: x.created_at, reverse=True)[0]

        task.status = "cancelled"
        db.commit()
        return "cancelled", task

    return "default", None


# -------------------------
# WEBHOOK
# -------------------------
@app.post("/webhook")
async def whatsapp_webhook(req: Request):

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

        # -------------------------
        # AI RESPONSE (NEW)
        # -------------------------
        action_summary = []

        if task:
            action_summary.append({
                "action": action,
                "category": task.category
            })
        else:
            action_summary.append({
                "action": action
            })

        final_reply = generate_response(action_summary)

        resp.message(final_reply)

    except Exception as e:
        print("❌ ERROR:", str(e))
        resp.message("Got it 👍 working on your request")

    return Response(content=str(resp), media_type="application/xml")
