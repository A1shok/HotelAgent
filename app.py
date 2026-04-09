from fastapi import FastAPI, Request
from fastapi.responses import Response
from twilio.twiml.messaging_response import MessagingResponse

app = FastAPI()

@app.post("/webhook")
async def whatsapp_webhook(request: Request):
    print("🔥 WEBHOOK HIT")

    form = await request.form()

    msg = form.get("Body")
    phone = form.get("From")

    print("📩 Message:", msg)
    print("📱 Phone:", phone)

    resp = MessagingResponse()
    resp.message("FastAPI working 👍")

    return Response(content=str(resp), media_type="application/xml")
