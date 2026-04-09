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

You MUST convert system actions into a natural reply.

STRICT RULES:
- Be specific to the action (DO NOT use generic replies)
- 1 short sentence only
- No repetition
- No greetings unless action is greeting
- No "All set" or vague phrases
- Mention task clearly

Action meanings:
- created → confirm request is being handled
- duplicate → say already working on it
- cancelled → confirm cancellation
- closed → acknowledge completion
- escalation → apologize and say urgent handling
- ambiguous → ask which request
- info → answer directly
- greeting → greet

Actions:
{actions}
"""

    res = client.chat.completions.create(
        model="gpt-5-mini",
        messages=[{"role": "user", "content": prompt}]
    )

    return res.choices[0].message.content.strip()

