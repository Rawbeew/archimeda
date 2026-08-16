"""
Hermes Telegram Bot — interactive command interface.
 Run: python bot.py
 Listens for Telegram commands and responds.
 Paper mode only. No real trading.
"""
import os
import sys
import time
import asyncio
import threading
import requests

from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    PAPER_MODE, SCAN_INTERVAL_SEC,
)
from paper_engine import get_summary, load_positions, load_trade_log
from feeds.dex_feeds import search_dex, get_trending_boosted, fetch_token_pairs
from signals import scan_cex_symbol
from feeds.cex_feeds import fetch_ohlcv
from telegram_bot import send_alert, format_summary

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

HELP_TEXT = """*Hermes Commands*

*Portfolio:*
/status - Paper portfolio + P&L
/positions - Open positions detail
/trades - Closed trade history

*Scanning:*
/scan <query> - Search Dexscreener
/trending - Top boosted DEX tokens (with names)
/degen - Broad scan: pump.fun, raydium, meteora, orca, base
/dex - DEX backtest snapshot (scan all DEX, run signals)
/price <symbol> - Live CEX price + signals
/wallets - Tracked smart wallets

*Backtesting:*
/backtest - Run backtest (all CEX symbols, 90 days)
/backtest BTC - Backtest single symbol

*Pump.fun:*
/pump - Start Pump.fun live detector (WebSocket)
/stop - Stop Pump.fun detector

*System:*
/help - This message
/about - About Hermes"""

ABOUT_TEXT = """*Hermes Signal Engine*

Paper mode: ACTIVE
No real money. No broker. No live trades.

*Feeds:*
- CEX: OKX (BTC, ETH, SOL, DOGE, AVAX, LINK, XRP)
- DEX: Dexscreener (Solana, Base, 90+ chains)
- Pump.fun: Helius WebSocket detector
- Wallet profiler: Smart money detection
- Robinhood Chain: Arbitrum L2 support

*Signals:*
- VWAP bands, SFP, engulfing, CVD divergence
- DEX volume surge, buy/sell pressure, pump/dump
- Pump.fun new token detection
- Smart wallet profiling (3+ token, 30%+ hit rate)

*Paper engine:*
- $10,000 starting balance
- 1% risk per trade, 2% stop, 4% target
- Max 5 concurrent positions"""


def handle_command(text, chat_id):
    """Parse a command and return a response string."""
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower().lstrip("/") if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""

    # ── Portfolio ─────────────────────────────────────────
    if cmd in ("status", "s"):
        return format_summary(get_summary())

    elif cmd in ("positions", "p"):
        positions = load_positions()
        if not positions:
            return "No open positions."
        lines = ["*Open Positions:*"]
        for i, p in enumerate(positions):
            lines.append(
                f"\n{i+1}. `{p['symbol']}` {p['direction']}\n"
                f"   Entry: ${p['entry_price']:.6f}\n"
                f"   Signal: {p['signal_type']}\n"
                f"   SL: ${p['stop_loss']:.6f} | TP: ${p['take_profit']:.6f}\n"
                f"   Source: {p.get('source', '?')} | Chain: {p.get('chain', '?')}"
            )
        return "\n".join(lines)

    elif cmd in ("trades", "t"):
        trades = load_trade_log()
        if not trades:
            return "No closed trades yet."
        lines = [f"*Closed Trades ({len(trades)}):*"]
        for t in trades[-10:]:
            emoji = "+" if t.get("pnl_usd", 0) > 0 else ""
            lines.append(
                f"\n`{t['symbol']}` {t['direction']}\n"
                f"   {t['entry_price']:.6f} -> {t['exit_price']:.6f}\n"
                f"   P&L: {emoji}${t.get('pnl_usd', 0):+.2f} ({t.get('exit_reason', '?')})"
            )
        return "\n".join(lines)

    # ── Scanning ───────────────────────────────────────────
    elif cmd == "scan" and arg:
        pairs = search_dex(arg)
        if not pairs:
            return f"No Dexscreener results for '{arg}'."
        lines = [f"*Dexscreener: {arg}* ({len(pairs)} pairs)"]
        for p in pairs[:8]:
            base = p.get("baseToken", {})
            vol = p.get("volume", {}).get("h24", 0)
            price = p.get("priceUsd", 0) or 0
            change = p.get("priceChange", {}).get("h24", 0) or 0
            liq = p.get("liquidity", {}).get("usd", 0) or 0
            chain = p.get("chainId", "?")
            lines.append(
                f"\n`{base.get('symbol', '?')}` ({chain})\n"
                f"   Price: ${float(price):.10f}".rstrip("0").rstrip(".") + "\n"
                f"   24h: {change:+.1f}% | Vol: ${vol:,.0f} | Liq: ${liq:,.0f}\n"
                f"   [Chart]({p.get('url', '')})"
            )
        return "\n".join(lines)

    elif cmd == "scan" and not arg:
        return "Usage: /scan <query>\nExample: /scan SOL meme"

    elif cmd == "trending":
        boosted = get_trending_boosted()
        if not boosted:
            return "No trending data right now."
        lines = [f"*Top Trending ({len(boosted)} boosted)*"]
        for i, t in enumerate(boosted[:10]):
            chain = t.get("chainId", "?")
            url = t.get("url", "")
            desc = t.get("description", "").split("\n")[0][:30]
            addr = t.get("tokenAddress", "")[:12]
            lines.append(f"\n{i+1}. ({chain}) {desc}\n   `{addr}...`\n   [Chart]({url})")
        return "\n".join(lines)

    elif cmd == "degen":
        # Broad DEX scan: search across multiple degen platforms
        from feeds.dex_feeds import search_dex, DEX_SEARCH_QUERIES
        all_pairs = []
        seen = set()
        queries = [
            "pump.fun", "raydium", "meteora", "orca",
            "base degen", "solana meme", "bonding curve",
            "just launched", "new pair", "moonshot",
        ]
        for q in queries:
            pairs = search_dex(q)
            for p in pairs[:3]:
                sig = p.get("pairAddress", "")
                if sig in seen:
                    continue
                seen.add(sig)
                all_pairs.append(p)
        
        if not all_pairs:
            return "No degen pairs found."
        
        # Sort by volume
        all_pairs.sort(key=lambda p: p.get("volume", {}).get("h24", 0) or 0, reverse=True)
        
        lines = [f"*Top Degen Pairs ({len(all_pairs)} found)*"]
        for p in all_pairs[:12]:
            base = p.get("baseToken", {})
            vol = p.get("volume", {}).get("h24", 0) or 0
            price = p.get("priceUsd", 0) or 0
            change = p.get("priceChange", {}).get("h24", 0) or 0
            liq = p.get("liquidity", {}).get("usd", 0) or 0
            chain = p.get("chainId", "?")
            dex = p.get("dexId", "?")
            lines.append(
                f"\n`{base.get('symbol', '?')}` ({chain}/{dex})\n"
                f"   Price: ${float(price):.10f}".rstrip("0").rstrip(".") + "\n"
                f"   24h: {change:+.1f}% | Vol: ${vol:,.0f} | Liq: ${liq:,.0f}\n"
                f"   [Chart]({p.get('url', '')})"
            )
        return "\n".join(lines)

    elif cmd in ("dex", "dexback"):
        # DEX backtest + signal scan using the new shitcoin-optimized signals
        from dex_signals import scan_all_dex_signals, format_signals_report
        signals = scan_all_dex_signals()
        report = format_signals_report(signals)
        return report

    elif cmd == "buy":
        # Generate Jupiter swap link for a token
        if not arg:
            return "Usage: /buy <token_address>\nExample: /buy 8vNd1xWgVgyNfG5idYaTaZG3BViWYg2PGwtqcZr7pump"
        
        token_mint = arg.strip()
        from buy_engine import check_safety, send_buy_alert
        # Quick check: get token info from Dexscreener
        from feeds.dex_feeds import fetch_token_pairs
        pairs = fetch_token_pairs(token_mint, chain=None)
        if not pairs:
            return f"Token not found on Dexscreener: {token_mint[:20]}..."
        
        pair = pairs[0]
        token_info = {
            "mint": token_mint,
            "symbol": pair.get("baseToken", {}).get("symbol", "?"),
            "chain": pair.get("chainId", "?"),
            "price_usd": pair.get("priceUsd", 0),
            "vol_24h": pair.get("volume", {}).get("h24", 0),
            "liquidity_usd": pair.get("liquidity", {}).get("usd", 0),
            "price_change_1h": pair.get("priceChange", {}).get("h1", 0) or pair.get("priceChange", {}).get("h24", 0),
            "price_change_6h": pair.get("priceChange", {}).get("h6", 0) or 0,
            "buy_ratio": 0,  # Dexscreener doesn't always have this
            "buy_24h": pair.get("txns", {}).get("h24", {}).get("buys", 0) or 0,
            "sell_24h": pair.get("txns", {}).get("h24", {}).get("sells", 0) or 0,
            "url": pair.get("url", ""),
        }
        total = token_info["buy_24h"] + token_info["sell_24h"]
        token_info["buy_ratio"] = token_info["buy_24h"] / max(total, 1)
        
        safety = check_safety(token_mint)
        alert = send_buy_alert(token_info, safety)
        return alert

    elif cmd in ("price", "pr") and arg:
        symbol = arg.upper().strip()
        if "/" not in symbol:
            symbol = symbol + "/USDT"
        df = fetch_ohlcv(symbol)
        if df is None:
            return f"Could not fetch {symbol}. Try BTC, ETH, SOL, DOGE, AVAX, LINK, XRP."
        sigs = scan_cex_symbol(symbol, df)
        last = df.iloc[-2]
        price = last["Close"]
        lines = [f"*{symbol}*\nPrice: ${price:.4f}\n"]
        if sigs:
            lines.append("*Signals:*")
            for s in sigs:
                lines.append(f"  {s['signal']}")
        else:
            lines.append("No signals detected this bar.")
        return "\n".join(lines)

    elif cmd in ("price", "pr") and not arg:
        return "Usage: /price <symbol>\nExample: /price BTC or /price SOL/USDT"

    # ── Wallet tracking ────────────────────────────────────
    elif cmd == "wallets":
        from config import STATE_DIR
        import json
        wallet_path = os.path.join(STATE_DIR, "tracked_wallets.json")
        if os.path.exists(wallet_path):
            with open(wallet_path) as f:
                wallets = json.load(f)
            if wallets:
                lines = [f"*Tracked Smart Wallets ({len(wallets)}):*"]
                for w in wallets[:10]:
                    lines.append(
                        f"\n`{w['wallet'][:16]}...`\n"
                        f"   Score: {w.get('score', '?')} | "
                        f"Tokens: {w.get('num_unique_tokens', '?')} | "
                        f"Hit rate: {w.get('hit_rate', '?')}"
                    )
                return "\n".join(lines)
        return "No smart wallets tracked yet. Run a scan cycle to find them."

    # ── Pump.fun detector ──────────────────────────────────
    elif cmd == "pump":
        # Start the Pump.fun detector in a background thread
        # Can't run asyncio in a thread easily, so we run it as a subprocess
        import subprocess
        proc = subprocess.Popen(
            [sys.executable, "pumpfun_detector.py"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Save PID so /stop can kill it
        from config import STATE_DIR
        pid_path = os.path.join(STATE_DIR, "pumpfun.pid")
        with open(pid_path, "w") as f:
            f.write(str(proc.pid))
        return ("Pump.fun detector STARTED.\n"
                "Listening for new token launches via Helius WebSocket.\n"
                "You will receive alerts here when new tokens are detected.\n"
                "Paper mode: no buys. Send /stop to stop.")

    elif cmd == "stop":
        from config import STATE_DIR
        pid_path = os.path.join(STATE_DIR, "pumpfun.pid")
        if os.path.exists(pid_path):
            with open(pid_path) as f:
                pid = int(f.read())
            try:
                import signal as sig_module
                os.kill(pid, sig_module.SIGTERM)
                os.remove(pid_path)
                return "Pump.fun detector STOPPED."
            except Exception as e:
                os.remove(pid_path)
                return f"Could not stop process (may already be dead): {e}"
        return "Pump.fun detector is not running."

    # ── System ──────────────────────────────────────────────
    elif cmd == "backtest":
        import subprocess
        if arg:
            # Single symbol
            result = subprocess.run(
                [sys.executable, "backtest.py", "--symbol", arg, "--days", "90"],
                capture_output=True, text=True, timeout=120,
                cwd=os.path.dirname(os.path.abspath(__file__)),
            )
            return result.stdout[-4000:] if result.stdout else "Backtest failed."
        else:
            # All symbols
            return ("Running full backtest (90 days, all symbols).\n"
                    "This takes ~2 min. Will send results here.\n"
                    "Or use /backtest BTC for single symbol.")

    elif cmd == "help":
        return HELP_TEXT

    elif cmd == "about":
        return ABOUT_TEXT

    elif cmd == "start":
        return (f"Welcome to Hermes Signal Engine.\n\n"
                f"Paper mode: ACTIVE. No real money.\n\n"
                f"Send /help for commands.")

    else:
        return f"Unknown command: /{cmd}\nSend /help for available commands."


def run_bot():
    """Long-poll Telegram for commands."""
    if not TELEGRAM_BOT_TOKEN:
        print("No TELEGRAM_BOT_TOKEN set.")
        return

    print("Hermes Telegram bot running. Send /help on Telegram.")
    print("Press Ctrl+C to stop.\n")

    offset = 0
    while True:
        try:
            r = requests.get(
                f"{TG_API}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=35,
            )
            if r.status_code != 200:
                print(f"  getUpdates failed: {r.status_code}")
                time.sleep(5)
                continue

            updates = r.json().get("result", [])
            for update in updates:
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                text = msg.get("text", "")
                chat_id = msg.get("chat", {}).get("id", TELEGRAM_CHAT_ID)

                if not text:
                    continue

                # Handle commands
                if text.startswith("/"):
                    print(f"  <- {text}")
                    reply = handle_command(text, chat_id)
                    # Send reply (try Markdown, fall back to plain)
                    try:
                        requests.post(
                            f"{TG_API}/sendMessage",
                            json={
                                "chat_id": chat_id,
                                "text": reply,
                                "parse_mode": "Markdown",
                            },
                            timeout=15,
                        )
                    except:
                        requests.post(
                            f"{TG_API}/sendMessage",
                            json={"chat_id": chat_id, "text": reply},
                            timeout=15,
                        )
                    print(f"  -> replied ({len(reply)} chars)")

        except KeyboardInterrupt:
            print("\nBot stopped.")
            break
        except Exception as e:
            print(f"  Error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    run_bot()
