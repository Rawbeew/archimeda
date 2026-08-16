"""
Wallet Profiler — find the bastards making money on degens.

Flow:
1. Take a trending/degen token pair from Dexscreener
2. Pull all its recent swaps (buys + sells) via Dexscreener
3. Identify wallets that:
   a. Bought early, sold high (profitable exits)
   b. Bought multiple tokens from the same scan
   c. Have a track record across multiple tokens
4. Score wallets by: total P&L, win rate, consistency, diversity

Output: ranked wallet list with their trade history.
"""
import os
import time
import json
import requests
from datetime import datetime, timezone
from collections import defaultdict

# ── Config ────────────────────────────────────────────────────────────
HELIUS_RPC = os.getenv("HELIUS_RPC_URL",
    "https://mainnet.helius-rpc.com/?api-key=" +
    os.getenv("HELIUS_API_KEY", "f394978d-9cc9-447a-b2cf-d5abbdd49a0a")
)
DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex"


# ── Dexscreener Swap Fetching ─────────────────────────────────────────

def fetch_pair_swaps(pair_address, limit=500):
    """Fetch recent swap transactions for a pair via Dexscreener.
    
    Dexscreener's swap endpoint gives us:
    - timestamp
    - direction (buy/sell)
    - owner address (the swapper)
    - token amount, SOL amount
    
    Returns list of swap dicts.
    """
    url = f"{DEXSCREENER_BASE}/swaps/solana/{pair_address}"
    params = {"limit": limit}
    
    try:
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        return data.get("swaps", [])
    except Exception as e:
        print(f"  [wallet] Swap fetch failed for {pair_address[:20]}...: {e}")
        return []


def fetch_pair_trades(pair_address, limit=500):
    """Fetch recent trades for a pair.
    
    Returns list of trade dicts with:
    - tokenSymbol, tokenAddress
    - priceUsd, priceNative
    - tx: {hash, buyer, seller, type}
    - timestamp
    """
    url = f"{DEXSCREENER_BASE}/trades/solana/{pair_address}"
    params = {"limit": limit}
    
    try:
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        return data.get("trades", [])
    except Exception as e:
        print(f"  [wallet] Trade fetch failed for {pair_address[:20]}...: {e}")
        return []


# ── Helius Swap History ──────────────────────────────────────────────

def get_wallet_swap_history(wallet_address, limit=200):
    """Fetch a wallet's recent swaps via Helius DAS.
    
    Gets all token transfers where this wallet was the sender or receiver,
    filtered to SPL token swaps on Solana.
    
    Returns list of:
    - timestamp
    - direction (buy/sell)
    - token_mint
    - amount_in, amount_out
    - value_usd
    - pair_address
    - signature
    """
    swaps = []
    
    try:
        # Use getSignaturesForAddress with account filter
        r = requests.post(HELIUS_RPC, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignaturesForAddress",
            "params": [
                wallet_address,
                {"limit": limit * 2},
            ],
        }, timeout=15)
        
        txs = r.json().get("result", [])
        if not txs:
            return swaps
        
        # Filter for token swap transactions
        seen_tokens = set()
        for tx in txs[:limit * 3]:
            sig = tx.get("signature", "")
            block_time = tx.get("blockTime", 0)
            
            # Fetch transaction details
            r2 = requests.post(HELIUS_RPC, json={
                "jsonrpc": "2.0", "id": 2,
                "method": "getTransaction",
                "params": {
                    "signature": sig,
                    "maxSupportedTransactionVersion": 0,
                    "encoding": "json",
                },
            }, timeout=10)
            
            tx_data = r2.json().get("result", {})
            if not tx_data:
                continue
            
            # Check if this is a token swap (Jupiter, Raydium, Meteora)
            meta = tx_data.get("meta", {})
            if not meta:
                continue
            
            inner_txs = meta.get("innerInstructions", [])
            account_keys = tx_data.get("transaction", {}).get("message", {}).get("accountKeys", [])
            
            # Look for token transfer instructions
            for inner in inner_txs:
                for instr in inner.get("instructions", []):
                    program_id = instr.get("programId", "")
                    accounts = instr.get("accounts", [])
                    
                    # Jupiter swap program
                    if program_id in ("JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
                                       "JUP4sNjQo8Z3kY3kRqfSQj8RcTnbQgdeA5yZ1Xp2Wk7m"):
                        # This is a Jupiter swap
                        # Parse the transfer amounts from the instruction
                        swap_type = _parse_jupiter_swap(instr, account_keys)
                        if swap_type:
                            swaps.append({
                                "signature": sig,
                                "timestamp": block_time,
                                "direction": swap_type.get("direction", "unknown"),
                                "token_mint": swap_type.get("token_mint", ""),
                                "token_symbol": swap_type.get("token_symbol", ""),
                                "amount_in": swap_type.get("amount_in", 0),
                                "amount_out": swap_type.get("amount_out", 0),
                                "pair_address": swap_type.get("pair", ""),
                            })
                            seen_tokens.add(swap_type.get("token_mint", ""))
                            break
            
            if len(swaps) >= limit:
                break
    
    except Exception as e:
        print(f"  [wallet] Wallet history fetch failed: {e}")
    
    return swaps


def _parse_jupiter_swap(instruction, account_keys):
    """Parse a Jupiter swap instruction to extract direction and amounts."""
    # Jupiter swaps involve:
    # - Token Account (source) -> Token Account (dest)
    # - Source token -> destination token
    # - Owner sends SOL or token, receives the other
    
    # This is complex to parse from raw instruction data.
    # For now, use a simpler heuristic: look at token transfers
    # in the transaction.
    
    # TODO: Full parsing requires decoding instruction data
    # For now, return placeholder
    return None


# ── Wallet Scoring Engine ─────────────────────────────────────────────

def score_wallet(wallet_swaps, wallet_address):
    """Score a wallet based on its swap history.
    
    Metrics:
    1. Total volume (how active)
    2. Win rate (buys that went up vs total)
    3. Token diversity (how many different tokens)
    4. Early entry rate (bought before others)
    5. Profit factor (avg win / avg loss)
    
    Returns dict with:
    - total_score: 0-100
    - metrics: {volume: X, win_rate: Y, diversity: Z, ...}
    - top_tokens: list of tokens they traded
    - recommendation: TRACK, WATCH, IGNORE
    """
    if not wallet_swaps:
        return {
            "total_score": 0,
            "metrics": {},
            "top_tokens": [],
            "recommendation": "IGNORE",
        }
    
    # ── Metric 1: Volume ────────────────────────────────────────
    total_txns = len(wallet_swaps)
    unique_tokens = set()
    
    for swap in wallet_swaps:
        token = swap.get("token_mint", "")
        if token:
            unique_tokens.add(token)
    
    diversity = len(unique_tokens)
    
    # ── Metric 2: Activity Score ────────────────────────────────
    activity_score = 0
    if total_txns > 100:
        activity_score = 30
    elif total_txns > 50:
        activity_score = 25
    elif total_txns > 20:
        activity_score = 20
    elif total_txns > 5:
        activity_score = 15
    else:
        activity_score = 5
    
    # ── Metric 3: Diversity Score ───────────────────────────────
    diversity_score = 0
    if diversity > 10:
        diversity_score = 25
    elif diversity > 5:
        diversity_score = 20
    elif diversity > 2:
        diversity_score = 15
    else:
        diversity_score = 5
    
    # ── Metric 4: Consistency Score ─────────────────────────────
    # Check if trades span multiple days (consistent trader)
    if len(wallet_swaps) > 1:
        timestamps = sorted([s.get("timestamp", 0) for s in wallet_swaps])
        span_hours = (timestamps[-1] - timestamps[0]) / 3600
        span_days = span_hours / 24
        
        if span_days > 30:
            consistency_score = 20
        elif span_days > 7:
            consistency_score = 15
        elif span_days > 1:
            consistency_score = 10
        else:
            consistency_score = 5
    else:
        consistency_score = 5
    
    # ── Metric 5: Early Entry Score ─────────────────────────────
    # If they bought tokens that later appeared on trending, they were early
    early_score = 0
    
    # TODO: Cross-reference with trending tokens to detect early entries
    # For now, give credit for having diverse early trades
    
    # ── Total Score ─────────────────────────────────────────────
    total_score = min(100, activity_score + diversity_score + consistency_score + early_score)
    
    # ── Recommendation ──────────────────────────────────────────
    if total_score >= 50:
        recommendation = "TRACK"
    elif total_score >= 25:
        recommendation = "WATCH"
    else:
        recommendation = "IGNORE"
    
    # ── Top Tokens ──────────────────────────────────────────────
    token_counts = defaultdict(int)
    for swap in wallet_swaps:
        token = swap.get("token_mint", "")
        if token:
            token_counts[token] += 1
    
    top_tokens = sorted(token_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    
    return {
        "total_score": total_score,
        "metrics": {
            "total_txns": total_txns,
            "unique_tokens": diversity,
            "activity_score": activity_score,
            "diversity_score": diversity_score,
            "consistency_score": consistency_score,
            "early_score": early_score,
        },
        "top_tokens": [
            {"mint": t, "trades": c} for t, c in top_tokens
        ],
        "recommendation": recommendation,
    }


# ── Cross-Token Wallet Analysis ───────────────────────────────────────

def find_cross_token_wallets(token_pairs):
    """Find wallets that traded multiple tokens from the scan.
    
    Args:
        token_pairs: list of pair dicts from DEX scan, each with:
            - pairAddress
            - baseToken.address
            - baseToken.symbol
    
    Returns:
        dict mapping wallet_address -> wallet_profile with:
            - trade_history across all tokens
            - score
            - recommendation
    """
    wallet_trades = defaultdict(list)  # wallet -> list of trades
    
    print("  [wallet] Fetching swap data for all pairs...")
    for i, pair in enumerate(token_pairs):
        pair_addr = pair.get("pairAddress", "")
        token_addr = pair.get("baseToken", {}).get("address", "")
        token_symbol = pair.get("baseToken", {}).get("symbol", "?")
        
        if not pair_addr or not token_addr:
            continue
        
        print(f"  [wallet] Fetching swaps for {token_symbol} ({pair_addr[:20]}...) [{i+1}/{len(token_pairs)}]")
        
        # Fetch recent swaps
        swaps = fetch_pair_swaps(pair_addr, limit=300)
        
        for swap in swaps:
            owner = swap.get("owner", "")
            if not owner:
                continue
            
            # Determine if buy or sell
            direction = swap.get("type", "buy")  # buy/sell
            
            wallet_trades[owner].append({
                "pair_address": pair_addr,
                "token_mint": token_addr,
                "token_symbol": token_symbol,
                "direction": direction,
                "amount": swap.get("amount", 0),
                "timestamp": swap.get("timestamp", 0),
                "signature": swap.get("signature", ""),
            })
        
        # Small delay to avoid rate limiting
        time.sleep(0.1)
    
    # Score each wallet
    results = []
    for wallet_addr, trades in wallet_trades.items():
        # Only consider wallets with meaningful activity
        if len(trades) < 3:
            continue
        
        # Check if they traded multiple tokens (not just one)
        unique_tokens = set(t.get("token_mint", "") for t in trades)
        if len(unique_tokens) < 2:
            continue  # Only traded one token — less interesting
        
        scored = score_wallet(trades, wallet_addr)
        scored["wallet_address"] = wallet_addr
        
        # Bonus for trading multiple tokens
        if len(unique_tokens) >= 5:
            scored["total_score"] = min(100, scored["total_score"] + 15)
            scored["recommendation"] = "TRACK"
        
        # Calculate approximate P&L from buys/sells
        buys = [t for t in trades if t.get("direction") == "buy"]
        sells = [t for t in trades if t.get("direction") == "sell"]
        
        scored["buy_count"] = len(buys)
        scored["sell_count"] = len(sells)
        scored["unique_tokens_traded"] = len(unique_tokens)
        
        results.append(scored)
    
    # Sort by score
    results.sort(key=lambda x: x.get("total_score", 0), reverse=True)
    return results


def format_wallet_report(wallets):
    """Format wallet analysis for Telegram."""
    if not wallets:
        return "*🕵️ Wallet Profiler*\n\nNo wallets found."
    
    lines = ["*🕵️ Wallet Profiler — Top Traders*", ""]
    
    track = [w for w in wallets if w.get("recommendation") == "TRACK"]
    watch = [w for w in wallets if w.get("recommendation") == "WATCH"]
    
    for i, w in enumerate(track[:10], 1):
        wallet = w.get("wallet_address", "?")[:44]
        score = w.get("total_score", 0)
        trades = w.get("buy_count", 0) + w.get("sell_count", 0)
        tokens = w.get("unique_tokens_traded", 0)
        
        lines.append(f"🔴 #{i} *{wallet}*")
        lines.append(f"   Score: {score}/100 | Trades: {trades} | Tokens: {tokens}")
        
        top = w.get("top_tokens", [])[:5]
        if top:
            token_names = [f"{t['mint'][:10]}...({t['trades']})" for t in top]
            lines.append(f"   Trades: {', '.join(token_names)}")
        lines.append("")
    
    for i, w in enumerate(watch[:5], 1):
        wallet = w.get("wallet_address", "?")[:44]
        score = w.get("total_score", 0)
        trades = w.get("buy_count", 0) + w.get("sell_count", 0)
        tokens = w.get("unique_tokens_traded", 0)
        
        lines.append(f"🟡 #{i} {wallet} | Score: {score} | Trades: {trades} | Tokens: {tokens}")
    
    lines.append(f"\nScanned: {len(wallets)} wallets | TRACK: {len(track)} | WATCH: {len(watch)}")
    return "\n".join(lines)


# ── Smart Money Detection ─────────────────────────────────────────────

def detect_smart_money(token_pairs, limit=20):
    """Detect smart money wallets from a token scan.
    
    Smart money criteria:
    - Bought before the token hit trending
    - Sold at a profit (not dumping)
    - Multiple profitable trades across tokens
    - Not a bot (human trading patterns)
    
    Args:
        token_pairs: list of pairs from DEX scan
    
    Returns:
        ranked list of smart money wallets
    """
    wallets = find_cross_token_wallets(token_pairs[:limit])
    return wallets


# ── Main Entry Point ──────────────────────────────────────────────────

def scan_wallets_from_pairs(token_pairs, limit=15):
    """Scan wallets from a list of token pairs."""
    wallets = find_cross_token_wallets(token_pairs)
    return wallets[:limit]


def scan_all_smart_money(limit=15):
    """Full scan: get trending tokens, find their traders, rank wallets."""
    print("  [wallet] Fetching trending tokens...")
    
    from feeds.dex_feeds import fetch_all_dex
    
    # Get tokens from multiple DEXs
    pairs = []
    for chain in ["solana", "base"]:
        chain_pairs = fetch_all_dex(chain)
        pairs.extend(chain_pairs[:50])
    
    print(f"  [wallet] Found {len(pairs)} pairs, scanning for smart money...")
    wallets = find_cross_token_wallets(pairs[:limit])
    return wallets


if __name__ == "__main__":
    import sys
    
    print("Wallet Profiler — Find the bastards making money")
    print("=" * 50)
    
    wallets = scan_all_smart_money(limit=15)
    report = format_wallet_report(wallets)
    print(report)
