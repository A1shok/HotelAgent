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

You MUST generate a reply STRICTLY based on the given actions.

CRITICAL RULES:
- Do NOT invent anything
- Do NOT mention things not in actions
- Do NOT use generic phrases like "request received"
- Do NOT list categories like room service, transport, etc.
- Keep it short (1 sentence)
- Sound natural and human

Action meanings:
- created → say what is being sent (e.g. "Sending towels to your room")
- duplicate → say already working on it
- cancelled → confirm cancellation
- completed → acknowledge completion
- escalation → apologize and say urgent handling
- ambiguous → ask clearly which request
- greeting → greet naturally
- info → answer directly

Actions:
{actions}

Generate ONLY the final reply.
"""

    res = client.chat.completions.create(
        model="gpt-5-mini",
        messages=[{"role": "user", "content": prompt}]
    )

    return res.choices[0].message.content.strip()


