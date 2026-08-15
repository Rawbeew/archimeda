"""
Paper execution engine — simulated trades, P&L tracking.
No real money. No broker. No orders. Only state files on disk.
"""
import json
import os
import time
from datetime import datetime, timezone
from config import (
    PAPER_STARTING_BALANCE, PAPER_POSITION_SIZE_PCT,
    PAPER_MAX_POSITIONS, PAPER_STOP_LOSS_PCT,
    PAPER_TAKE_PROFIT_PCT, STATE_DIR, PAPER_POSITIONS_PATH, TRADE_LOG_PATH,
)

os.makedirs(STATE_DIR, exist_ok=True)


def load_positions():
    """Load open paper positions from state file."""
    if os.path.exists(PAPER_POSITIONS_PATH):
        try:
            with open(PAPER_POSITIONS_PATH, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_positions(positions):
    """Save open paper positions to state file."""
    with open(PAPER_POSITIONS_PATH, "w") as f:
        json.dump(positions, f, indent=2)


def load_trade_log():
    """Load closed trade log."""
    if os.path.exists(TRADE_LOG_PATH):
        try:
            with open(TRADE_LOG_PATH, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def append_trade_log(trade):
    """Append a closed trade to the log."""
    log = load_trade_log()
    log.append(trade)
    with open(TRADE_LOG_PATH, "w") as f:
        json.dump(log, f, indent=2)
    return log


def open_paper_position(signal, current_price=None):
    """Open a paper position based on a signal. Returns position dict or None.
    Direction is inferred from signal type:
    - BULL_SFP, BULL_ENGULFING, BULL_CVD_DIVERGENCE, VWAP_LOWER_TOUCH -> BUY
    - BEAR_SFP, BEAR_ENGULFING, BEAR_CVD_DIVERGENCE, VWAP_UPPER_TOUCH -> SELL
    - DEX signals -> BUY only (spot, can't short shitcoins)
    """
    positions = load_positions()
    if len(positions) >= PAPER_MAX_POSITIONS:
        return None

    sig_type = signal.get("signal", "")
    symbol = signal.get("symbol", "")
    source = signal.get("source", "")

    # Skip if already in position on this symbol
    if any(p["symbol"] == symbol and p.get("source") == source for p in positions):
        return None

    # Infer direction
    bull_signals = {"BULL_SFP", "BULL_ENGULFING", "BULL_CVD_DIVERGENCE",
                    "VWAP_LOWER_TOUCH", "DEX_VOLUME_SURGE", "DEX_BUY_PRESSURE",
                    "DEX_PUMP_1H", "VOLUME_SPIKE"}
    bear_signals = {"BEAR_SFP", "BEAR_ENGULFING", "BEAR_CVD_DIVERGENCE",
                    "VWAP_UPPER_TOUCH", "DEX_SELL_PRESSURE", "DEX_DUMP_1H"}

    if sig_type in bull_signals:
        direction = "BUY"
    elif sig_type in bear_signals:
        direction = "SELL"
    else:
        return None

    # Determine entry price
    if "close" in signal:
        entry_price = float(signal["close"])
    elif "price_usd" in signal:
        entry_price = float(signal["price_usd"])
    else:
        return None

    if entry_price <= 0:
        return None

    # Position size: fixed % of starting balance
    risk_amount = PAPER_STARTING_BALANCE * PAPER_POSITION_SIZE_PCT
    size = risk_amount / entry_price

    position = {
        "id": f"paper_{int(time.time())}_{symbol}",
        "symbol": symbol,
        "source": source,
        "chain": signal.get("chain", "CEX"),
        "direction": direction,
        "entry_price": entry_price,
        "size": size,
        "risk_amount": risk_amount,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "signal_type": sig_type,
        "stop_loss": entry_price * (1 - PAPER_STOP_LOSS_PCT) if direction == "BUY"
                       else entry_price * (1 + PAPER_STOP_LOSS_PCT),
        "take_profit": entry_price * (1 + PAPER_TAKE_PROFIT_PCT) if direction == "BUY"
                        else entry_price * (1 - PAPER_TAKE_PROFIT_PCT),
    }

    positions.append(position)
    save_positions(positions)
    return position


def check_exits(positions, price_map):
    """Check if any open positions hit stop loss or take profit.
    price_map: {symbol: current_price}
    Returns list of closed position dicts with P&L."""
    closed = []
    still_open = []

    for pos in positions:
        symbol = pos["symbol"]
        current_price = price_map.get(symbol)
        if current_price is None:
            still_open.append(pos)
            continue

        entry = pos["entry_price"]
        direction = pos["direction"]
        sl = pos["stop_loss"]
        tp = pos["take_profit"]

        hit_sl = False
        hit_tp = False

        if direction == "BUY":
            if current_price <= sl:
                hit_sl = True
            elif current_price >= tp:
                hit_tp = True
        else:  # SELL
            if current_price >= sl:
                hit_sl = True
            elif current_price <= tp:
                hit_tp = True

        if hit_sl or hit_tp:
            # Calculate P&L
            if direction == "BUY":
                pnl_pct = (current_price - entry) / entry
                exit_reason = "STOP_LOSS" if hit_sl else "TAKE_PROFIT"
            else:
                pnl_pct = (entry - current_price) / entry
                exit_reason = "STOP_LOSS" if hit_sl else "TAKE_PROFIT"

            pnl_usd = pos["size"] * (current_price - entry) if direction == "BUY" \
                      else pos["size"] * (entry - current_price)

            trade = {
                **pos,
                "exit_price": current_price,
                "exit_reason": exit_reason,
                "pnl_pct": round(pnl_pct, 4),
                "pnl_usd": round(pnl_usd, 2),
                "closed_at": datetime.now(timezone.utc).isoformat(),
            }
            closed.append(trade)
            append_trade_log(trade)
        else:
            still_open.append(pos)

    save_positions(still_open)
    return closed


def get_summary():
    """Get paper portfolio summary."""
    positions = load_positions()
    trade_log = load_trade_log()

    total_pnl = sum(t.get("pnl_usd", 0) for t in trade_log)
    wins = [t for t in trade_log if t.get("pnl_usd", 0) > 0]
    losses = [t for t in trade_log if t.get("pnl_usd", 0) <= 0]
    win_rate = len(wins) / len(trade_log) * 100 if trade_log else 0

    return {
        "open_positions": len(positions),
        "total_trades": len(trade_log),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "realized_pnl": round(total_pnl, 2),
        "balance": round(PAPER_STARTING_BALANCE + total_pnl, 2),
        "positions_detail": [
            {
                "symbol": p["symbol"],
                "direction": p["direction"],
                "entry": p["entry_price"],
                "size": round(p["size"], 4),
                "signal": p["signal_type"],
                "opened": p["opened_at"][:19],
            }
            for p in positions
        ],
    }
