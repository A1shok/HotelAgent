from openai import OpenAI
import os

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def reply(event, data):
    prompt = f"""
You are a hotel WhatsApp concierge.

Rules:
- 1–2 lines
- Friendly
- Human tone
- 1 emoji max

Event: {event}
Data: {data}
"""

    res = client.chat.completions.create(
        model="gpt-5-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.4
    )

    return res.choices[0].message.content.strip()