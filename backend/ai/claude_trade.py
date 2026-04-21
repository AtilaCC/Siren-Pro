import os
import json
import aiohttp

CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY")

# ==============================
# SYSTEM 1 — BASE INTELIGÊNCIA
# ==============================
BASE_PROMPT = """
You are an institutional-grade autonomous trading intelligence system.

Your primary objective is to preserve capital and only trade when statistical edge exists.

Steps:
1. Analyze market (volatility, trend, structure)
2. Classify regime: TREND / RANGE / LOW_VOL / CHAOTIC
3. Only trade if confidence >= 0.7
4. Validate risk before trading
5. Avoid unclear conditions

If any rule fails → NO TRADE
"""

# ==============================
# SYSTEM 2 — PERFORMANCE LAYER
# ==============================
PERFORMANCE_PROMPT = """
You are under strict performance optimization.

Rules:

- Max 3 trades active
- Confidence must be >= 0.75
- Risk/Reward minimum = 1:2
- Avoid low volatility and sideways markets
- After 3 losses → STOP trading
- If daily loss > 2% → STOP

If any doubt → NO TRADE

Priority: quality over quantity
"""

# ==============================
# DECISÃO DO TRADE
# ==============================
async def claude_trade_decision(token: dict) -> dict:
    url = "https://api.anthropic.com/v1/messages"

    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }

    market_data = {
        "symbol": token.get("sym"),
        "price": token.get("price"),
        "volume": token.get("vol"),
        "rsi": token.get("rsi"),
        "score": token.get("score"),
        "change": token.get("chg"),
        "trend": token.get("trend"),
    }

    payload = {
        "model": "claude-3-sonnet-20240229",
        "max_tokens": 200,
        "system": BASE_PROMPT + "\n\n" + PERFORMANCE_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": f"""
Analyze and respond ONLY in JSON:

{json.dumps(market_data)}

Format:
{{
 "trade": true/false,
 "confidence": 0-1,
 "reason": "short explanation",
 "regime": "TREND/RANGE/etc"
}}
"""
            }
        ]
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                data = await resp.json()

                text = data["content"][0]["text"]

                return json.loads(text)

    except Exception as e:
        return {
            "trade": False,
            "confidence": 0,
            "reason": f"error: {str(e)}",
            "regime": "UNKNOWN"
        }
