from openai import OpenAI
import json
import os

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def parse_message(msg):
    prompt = f"""
Classify hotel guest/staff message.

Return JSON:
{{
 "intent": "task|cancel|completion|not_received|greeting|info_request",
 "category": "ac|towels|water|food|other",
 "urgency": "low|normal|high",
 "confidence": 0.0
}}

Message: "{msg}"
"""

    res = client.chat.completions.create(
        model="gpt-5-mini",
        messages=[{"role": "user", "content": prompt}],
    )

    try:
        return json.loads(res.choices[0].message.content)
    except:
        return {"intent": "unknown", "category": "other"}

def generate_response(actions):
    prompt = f"""
You are a hotel WhatsApp concierge.

You MUST generate a reply strictly based on system actions.

DO NOT:
- Use generic phrases like "Your request has been received"
- Add greetings unless explicitly required
- Ignore action meaning

BE SPECIFIC.

Action meanings:
- created → say what is being sent
- duplicate → say already working on it
- cancelled → confirm cancellation
- closed → acknowledge completion
- escalation → apologize + urgent fix
- ambiguous → ask clearly which request
- info → answer directly

Actions:
{actions}

Generate ONE short natural reply.
"""

    res = client.chat.completions.create(
        model="gpt-5-mini",
        messages=[{"role": "user", "content": prompt}]
    )

    return res.choices[0].message.content.strip()

