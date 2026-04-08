from openai import OpenAI
import json
import os

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def parse_message(msg):
    prompt = f"""
Classify hotel guest message.

Return JSON:
{{
 "intent": "task|cancel|followup|completion|not_received",
 "category": "ac|towels|water|other",
 "confidence": 0.0
}}

Message: "{msg}"
"""

    res = client.chat.completions.create(
        model="gpt-5-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0
    )

    try:
        return json.loads(res.choices[0].message.content)
    except:
        return {"intent": "unknown", "category": "other"}