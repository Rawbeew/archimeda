# Archimeda — Crypto Signal Engine (Paper Mode)

Paper-only multi-chain crypto signal scanner. Pulls OHLCV + on-chain DEX data,
runs VWAP/SFP/CVD/volume-spike detection, and alerts via Telegram.

**No real money. No broker. No live trades.** Signals are logged and sent as
Telegram alerts for human review.

## Feeds

| Source | What it covers | Chain |
|--------|---------------|-------|
| ccxt (Binance) | BTC, ETH, SOL, major pairs OHLCV | CEX |
| Dexscreener API | Shitcoins, meme tokens, trending pairs | Solana, Base, + 90 chains |
| Dexscreener search | Token lookup by name/symbol | All chains |

## Signals detected

1. **Daily SFP (Swing Failure Pattern)** — sweep + reclaim
2. **Engulfing bars** (bull/bear)
3. **Volume spike** — 2x+ above 20-bar average
4. **CVD divergence** — price up + CVD down (or inverse)
5. **Dexscreener volume/liquidity spike** — 24h volume surge on DEX pairs

## Architecture

```
archimeda/
├── config.py          # Watchlists, thresholds, env vars
├── feeds/
│   ├── cex_feeds.py   # ccxt OHLCV (Binance/Coinbase)
│   └── dex_feeds.py   # Dexscreener (Solana/Base/trending)
├── signals.py         # VWAP, SFP, CVD, engulfing, volume spike
├── paper_engine.py    # Simulated trades + P&L tracking
├── telegram_bot.py    # Alert sender + command handler
├── run_cycle.py       # Main entry: one scan cycle
├── state/             # Paper positions + trade log (JSON)
├── requirements.txt
└── .github/
    └── workflows/
        └── scan.yml   # 15-minute GitHub Actions cycle
```

## Run

```bash
python run_cycle.py          # one scan cycle
python run_cycle.py --watch  # continuous (every 15 min)
```

## Telegram

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as GitHub secrets (or local
`.env`). The bot sends alerts on each signal and responds to:

- `/status` — current paper positions + P&L
- `/scan <query>` — manual Dexscreener token search
- `/trending` — top trending DEX pairs
- `/help` — available commands

## Safety

```
PAPER_MODE = True   # hardcoded. No real-money path exists in this codebase.
```

There is no broker integration, no MT5, no MetaApi, no order execution. Only
data pull, signal detection, and alerts.
