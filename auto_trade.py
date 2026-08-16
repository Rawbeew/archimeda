"""
Auto-trade engine — runs on detected signals, executes via trading.py.

Flow:
1. Signal detected (DEX, Pump.fun, Raydium)
2. Safety check: mint auth revoked, freeze disabled, liq > $10k
3. If safety passes -> buy with configured amount
4. Start auto-sell timer: sell at 4h or manual /sell
5. Notify user via Telegram

Configuration (from .env):
- HERMES_WALLET_PRIVATE_KEY: cold wallet key (B58 encoded)
- AUTO_TRADE_AMOUNT_SOL: SOL per trade (default 0.1)
- AUTO_TRADE_ENABLED: "true" or "false"
- MAX_TRADES_PER_HOUR: 5
"""
import os
import time
import json
import threading
from datetime import datetime, timezone

from config import STATE_DIR
from buy_engine import check_safety
from trading import buy_spl_token, sell_spl_token, check_token_price

AUTO_TRADE_ENABLED = os.getenv("HERMES_AUTO_TRADE", "false").lower() == "true"
TRADE_AMOUNT_SOL = float(os.getenv("HERMES_TRADE_AMOUNT_SOL", "0.1"))
MAX_TRADES_PER_HOUR = int(os.getenv("HERMES_MAX_TRADES_PER_HOUR", "5"))

ACTIVE_TRADES = os.path.join(STATE_DIR, "active_trades.json")
TRADE_RATE_LIMIT = os.path.join(STATE_DIR, "trade_rate.json")


def load_active_trades():
    if os.path.exists(ACTIVE_TRADES):
        with open(ACTIVE_TRADES) as f:
            return json.load(f)
    return {}


def save_active_trades(trades):
    with open(ACTIVE_TRADES, "w") as f:
        json.dump(trades, f, indent=2)


def get_trade_count_last_hour():
    now = time.time()
    if os.path.exists(TRADE_RATE_LIMIT):
        with open(TRADE_RATE_LIMIT) as f:
            timestamps = json.load(f)
        recent = [t for t in timestamps if now - t < 3600]
        return len(recent)
    return 0


def log_trade_count():
    now = time.time()
    timestamps = []
    if os.path.exists(TRADE_RATE_LIMIT):
        with open(TRADE_RATE_LIMIT) as f:
            timestamps = json.load(f)
    timestamps.append(now)
    timestamps = [t for t in timestamps if now - t < 7200]
    with open(TRADE_RATE_LIMIT, "w") as f:
        json.dump(timestamps, f)


def auto_buy(signal_data, telegram_chat_id="", bot_token=""):
    """Execute an auto-buy if conditions are met.
    
    signal_data: dict with token info from DEX signal
    telegram_chat_id: where to send alert
    bot_token: bot API key
    """
    if not AUTO_TRADE_ENABLED:
        print("  [autobuy] Disabled (HERMES_AUTO_TRADE=false)")
        return

    if get_trade_count_last_hour() >= MAX_TRADES_PER_HOUR:
        print(f"  [autobuy] Rate limit: {MAX_TRADES_PER_HOUR}/hr")
        return

    mint = signal_data.get("mint", signal_data.get("address", ""))
    symbol = signal_data.get("symbol", "unknown")
    chain = signal_data.get("chain", "solana")
    liq = float(signal_data.get("liq", signal_data.get("liquidity_usd", 0)))

    if not mint:
        return

    # Safety check
    print(f"  [autobuy] Safety check: {symbol} ({mint[:12]}...)")
    safety = check_safety(mint)

    if "ACTIVE" in safety.get("mint_authority", ""):
        print(f"  [autobuy] REJECTED: mint authority active")
        return

    if "ENABLED" in safety.get("freeze_authority", ""):
        print(f"  [autobuy] REJECTED: freeze authority enabled")
        return

    if liq < 10000:
        print(f"  [autobuy] REJECTED: liq ${liq:,.0f} < $10k")
        return

    # Execute buy
    print(f"  [autobuy] EXECUTING: {TRADE_AMOUNT_SOL} SOL -> {symbol}")
    try:
        result = buy_spl_token(mint, TRADE_AMOUNT_SOL)
        if result.get("status") != "success":
            print(f"  [autobuy] FAILED: {result.get('error', 'unknown')}")
            return

        log_trade_count()

        # Record position
        trades = load_active_trades()
        trades[mint] = {
            "symbol": symbol,
            "chain": chain,
            "mint": mint,
            "entry_sol": TRADE_AMOUNT_SOL,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "tx_hash": result.get("tx_hash", ""),
            "signal": signal_data.get("signal", "signal"),
        }
        save_active_trades(trades)

        # Start auto-sell monitor
        t = threading.Thread(
            target=auto_sell_monitor,
            args=(mint, symbol, TRADE_AMOUNT_SOL),
            daemon=True,
        )
        t.start()

        # Alert user
        if telegram_chat_id:
            from telegram_bot import send_alert
            alert = (
                "*AUTO-BOUGHT " + symbol + "*\n"
                + "`" + mint[:20] + "..." + "`\n"
                + "Amount: " + str(TRADE_AMOUNT_SOL) + " SOL\n"
                + "TX: " + str(result.get('tx_hash', 'N/A')[:30]) + "...\n"
                + "Auto-sell: time-stop in 4h\n"
                + "Liq: $" + f"{liq:,.0f}"
            )
            try:
                send_alert(alert)
            except Exception:
                pass

        print(f"  [autobuy] DONE: {result.get('tx_hash', '')}")
    except Exception as e:
        print(f"  [autobuy] ERROR: {e}")


def auto_sell_monitor(mint, symbol, entry_sol):
    """Monitor position and auto-sell at time stop (4h)."""
    time_stop_seconds = 4 * 3600
    last_alert = 0

    print(f"  [autosell] Monitoring {symbol} (mint: {mint[:12]}...)")

    while True:
        time.sleep(15)
        now = time.time()

        trades = load_active_trades()
        if mint not in trades:
            print(f"  [autosell] Position closed, stopping monitor")
            return

        entry_time = datetime.fromisoformat(trades[mint]["entry_time"])
        elapsed = now - entry_time.timestamp()

        if elapsed > time_stop_seconds:
            print(f"  [autosell] TIME STOP: {symbol} for {elapsed/3600:.1f}h")
            auto_sell_position(mint, symbol, trades[mint])
            return

        # Price alert every 2 minutes
        if now - last_alert > 120:
            last_alert = now
            price = check_token_price(mint)
            if price:
                print(f"  [autosell] {symbol}: {price:.10f}")


def auto_sell_position(mint, symbol, trade_info):
    """Auto-sell a position."""
    print(f"  [autosell] Selling {symbol}...")
    try:
        # For now, use time-stop sell via Jupiter (sells estimated amount)
        result = sell_spl_token(mint, 1000000)
        print(f"  [autosell] Result: {result}")

        trades = load_active_trades()
        trades.pop(mint, None)
        save_active_trades(trades)
    except Exception as e:
        print(f"  [autosell] Sell failed: {e}")
