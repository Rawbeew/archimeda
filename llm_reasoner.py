"""
LLM reasoning layer — before every buy, the bot sends the full analysis
to an LLM and asks "should I buy this?" If the LLM says yes, proceed.

This is NOT the trading decision — the LLM is slow. It's a
human-like second opinion on top of the automated safety checks.

Usage:
  from llm_reasoner import should_buy
  result = should_buy(signal_data, safety_data)
  # Returns {"action": "buy"/"skip", "reason": "..."}

Requires:
  OPENROUTER_API_KEY in .env

Fallback: if LLM is unavailable, proceed with buy (don't miss opportunities).
"""
import os
import json
import requests
from datetime import datetime, timezone

LLM_API = "https://openrouter.ai/api/v1/chat/completions"
LLM_MODEL = os.getenv("LLM_REASONING_MODEL", "anthropic/claude-sonnet-4")
API_KEY = os.getenv("OPENROUTER_API_KEY", "")

# If no API key, LLM is disabled — buy on safety checks only
LLM_ENABLED = bool(API_KEY)

SYSTEM_PROMPT = """You are a crypto trade analyst. You review potential shitcoin buys
and give a quick recommendation: BUY or SKIP.

You consider:
- Token liquidity: $25k-$200k is ideal (too low = rug risk, too high = no upside)
- Buy/sell ratio: >70% buys = accumulation signal
- Volume surge: volume increasing before price = smart money loading
- Mint authority: MUST be revoked (otherwise dev can print tokens)
- Freeze authority: MUST be disabled (otherwise dev can freeze your tokens)
- Supply: reasonable supply (not trillions)
- Chain: Solana, Base, BSC are active shitcoin chains
- Price action: flat price + high volume = accumulation, about to pump

Rules:
- Keep your reasoning under 2 sentences
- Be decisive: BUY or SKIP
- If mint authority is active → SKIP (rug risk)
- If freeze authority is enabled → SKIP (rug risk)
- If liq is $0 → SKIP
- If buy ratio is <50% and price dropping → SKIP
- If buy ratio >60% and volume surging → BUY
- If unsure → SKIP (capital preservation first)"""


def should_buy(signal_data, safety_data):
    """Ask LLM if this trade is worth executing.
    
    Returns dict with:
      - action: "buy" or "skip"
      - reason: why
      - llm_error: if LLM failed
    """
    if not LLM_ENABLED:
        return {"action": "buy", "reason": "LLM not configured, proceeding on safety checks"}

    mint = signal_data.get("mint", signal_data.get("address", "unknown"))
    symbol = signal_data.get("symbol", "unknown")
    chain = signal_data.get("chain", "unknown")
    liq = signal_data.get("liq", signal_data.get("liquidity_usd", 0))
    price = signal_data.get("price", 0)
    signal = signal_data.get("signal", "unknown")
    
    buy_ratio = signal_data.get("buy_ratio", 0)
    ch_1h = signal_data.get("price_change_1h", 0)
    vol_24h = signal_data.get("vol_24h", 0)
    vol_1h = signal_data.get("vol_1h", 0)
    
    mint_auth = safety_data.get("mint_authority", "unknown")
    freeze_auth = safety_data.get("freeze_authority", "unknown")
    supply = safety_data.get("supply", 0)
    
    # Build concise trade summary
    trade_summary = (
        f"TOKEN: {symbol} on {chain}\n"
        f"Mint: {mint[:20]}...\n"
        f"Signal: {signal}\n"
        f"Price: ${float(price):.10f}\n"
        f"Liquidity: ${float(liq):,.0f}\n"
        f"24h Volume: ${float(vol_24h):,.0f}\n"
        f"1h Volume: ${float(vol_1h):,.0f}\n"
        f"Buy Ratio: {buy_ratio:.0%}\n"
        f"1h Change: {ch_1h:+.1f}%\n"
        f"Mint Authority: {mint_auth}\n"
        f"Freeze Authority: {freeze_auth}\n"
        f"Supply: {int(supply):,}\n"
    )
    
    try:
        r = requests.post(LLM_API, json={
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": trade_summary},
            ],
            "max_tokens": 100,
            "temperature": 0.1,
        }, timeout=15)
        data = r.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        
        # Parse response
        upper = content.upper()
        if "SKIP" in upper:
            return {"action": "skip", "reason": content.strip()[:200]}
        elif "BUY" in upper:
            return {"action": "buy", "reason": content.strip()[:200]}
        else:
            return {"action": "buy", "reason": f"LLM unclear: {content.strip()[:100]}"}
    
    except Exception as e:
        return {"action": "buy", "reason": f"LLM failed ({str(e)[:80]}), proceeding on safety checks"}


if __name__ == "__main__":
    # Test
    test_signal = {
        "mint": "test123", "symbol": "TEST", "chain": "solana",
        "liq": 50000, "price": 0.001, "signal": "BUY_PRESSURE_EARLY",
        "buy_ratio": 0.85, "price_change_1h": 2.0,
        "vol_24h": 500000, "vol_1h": 200000,
    }
    test_safety = {
        "mint_authority": "REVOLED",
        "freeze_authority": "DISABLED",
        "supply": 1000000000,
    }
    result = should_buy(test_signal, test_safety)
    print(json.dumps(result, indent=2))
