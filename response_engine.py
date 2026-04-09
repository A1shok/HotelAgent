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

Examples:
task_created → "Got it 👍 sending towels in 10 minutes"
duplicate → "Already working on it 👍"
completed → "Just checking 👍 have you received it?"
escalation → "Sorry 🙏 fixing this right away"
cancelled → "Done 👍 request cancelled"
"""

    res = client.chat.completions.create(
        model="gpt-5-mini",
        messages=[{"role": "user", "content": prompt}],
    )

    return res.choices[0].message.content.strip()
