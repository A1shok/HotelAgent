@app.post("/webhook")
async def whatsapp_webhook(request: Request):
    from twilio.twiml.messaging_response import MessagingResponse
    from fastapi.responses import Response

    resp = MessagingResponse()

    try:
        print("🔥 WEBHOOK HIT")

        form = await request.form()

        msg = form.get("Body")
        phone = form.get("From")

        print("📩 Message:", msg)
        print("📱 Phone:", phone)

        resp.message("FastAPI working 👍")

        return Response(content=str(resp), media_type="application/xml")

    except Exception as e:
        print("❌ ERROR:", str(e))

        resp.message(f"Error: {str(e)}")

        return Response(content=str(resp), media_type="application/xml")
