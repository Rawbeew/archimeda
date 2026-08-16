"""
Wallet Profiler — find bastards making money from degens.

Uses Jupiter program signatures to find all recent swaps,
then filters by the tokens we care about and cross-references wallets.

This works because:
1. Jupiter v6 is the main DEX aggregator — most Solana swaps go through it
2. getSignaturesForAddress on Jupiter's program ID returns ALL swaps
3. Each swap contains the token mints and wallet addresses
"""
import os
import time
import json
import requests
from collections import defaultdict

HELIUS_RPC = os.getenv("HELIUS_RPC_URL",
    "https://mainnet.helius-rpc.com/?api-key=" +
    os.getenv("HELIUS_API_KEY", "f394978d-9cc9-447a-b2cf-d5abbdd49a0a")
)

DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex"

JUPITER_V6 = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
RAYDIUM_AMM = "675kPX9M4SG3G7eaCztUo626fA96R5LaQqyRr682sbBt"


def find_cross_token_wallets(token_pairs, limit=20):
    """Find wallets that traded across multiple degen tokens.
    
    Uses Jupiter program signatures as the data source:
    1. Get all recent Jupiter swap signatures (last 2000 txs)
    2. For each signature, fetch the transaction
    3. Parse: find which tokens were swapped and by which wallet
    4. Cross-reference: wallets trading 2+ of our target tokens
    """
    # Get target token addresses
    tokens = []
    seen = set()
    for pair in token_pairs:
        addr = pair.get("address", "")
        if addr and addr not in seen and addr != "So11111111111111111111111111111111111111112":
            tokens.append(addr)
            seen.add(addr)
    
    print(f"  [wallet] Target tokens: {len(tokens)}")
    for t in tokens[:5]:
        print(f"    {t[:20]}...")
    
    wallet_tokens = defaultdict(set)
    wallet_txns = defaultdict(int)
    
    # Method: fetch recent Jupiter swap signatures
    print("  [wallet] Fetching Jupiter swap signatures...")
    try:
        r = requests.post(HELIUS_RPC, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignaturesForAddress",
            "params": [JUPITER_V6, {"limit": 500}],
        }, timeout=15)
        
        sigs = r.json().get("result", [])
        print(f"  [wallet] Found {len(sigs)} Jupiter swap signatures")
        
        # Parse each signature to find token pairs and wallets
        for i, sig_data in enumerate(sigs[:300]):
            sig = sig_data.get("signature", "")
            
            r2 = requests.post(HELIUS_RPC, json={
                "jsonrpc": "2.0", "id": 2,
                "method": "getTransaction",
                "params": {
                    "signature": sig,
                    "maxSupportedTransactionVersion": 0,
                    "encoding": "json",
                },
            }, timeout=8)
            
            tx = r2.json().get("result", {})
            if not tx:
                continue
            
            meta = tx.get("meta", {})
            if not meta:
                continue
            
            # Get signer (the wallet that initiated the swap)
            signers = meta.get("signers", [])
            wallet = None
            for s in signers:
                if isinstance(s, dict):
                    wallet = s.get("publicKey", "")
                else:
                    wallet = str(s)
                if wallet:
                    break
            
            if not wallet:
                continue
            
            # Get account keys to find which tokens were involved
            account_keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
            token_mints = []
            
            for acct in account_keys:
                addr = acct.get("pubkey", "") if isinstance(acct, dict) else str(acct)
                # Check if this mint is one of our target tokens
                if addr in tokens and addr != "So111...":
                    token_mints.append(addr)
            
            # If this swap involved any of our target tokens, record it
            if token_mints:
                wallet_tokens[wallet].update(token_mints)
                wallet_txns[wallet] += 1
            
            if i % 100 == 0 and i > 0:
                print(f"  [wallet] Processed {i} sigs, found {len(wallet_tokens)} wallets")
    
    except Exception as e:
        print(f"  [wallet] Jupiter fetch failed: {e}")
    
    print(f"  [wallet] Total wallets: {len(wallet_tokens)}")
    
    # Score and rank
    results = []
    for wallet, token_set in wallet_tokens.items():
        diversity = len(token_set)
        txn_count = wallet_txns[wallet]
        
        if diversity < 2:
            continue
        
        score = 0
        if diversity >= 4:
            score += 40
        elif diversity >= 3:
            score += 30
        elif diversity >= 2:
            score += 20
        
        if txn_count > 30:
            score += 20
        elif txn_count > 10:
            score += 15
        elif txn_count > 3:
            score += 10
        else:
            score += 5
        
        if diversity >= 3 and txn_count > 10:
            score += 20
        
        recommendation = "TRACK" if score >= 50 else ("WATCH" if score >= 25 else "AVOID")
        
        results.append({
            "wallet_address": wallet,
            "total_score": min(100, score),
            "unique_tokens": diversity,
            "total_txns": txn_count,
            "recommendation": recommendation,
            "tokens_traded": list(token_set)[:10],
        })
    
    results.sort(key=lambda x: x["total_score"], reverse=True)
    return results[:limit]


def format_wallet_report(wallets):
    """Format for Telegram."""
    if not wallets:
        return "*🕵️ Wallet Profiler*\n\nNo cross-token wallets found.\nTip: try scanning when more tokens are trending."
    
    lines = ["*🕵️ Cross-Token Wallet Tracker*", ""]
    
    track = [w for w in wallets if w.get("recommendation") == "TRACK"]
    watch = [w for w in wallets if w.get("recommendation") == "WATCH"]
    
    for i, w in enumerate(track[:10], 1):
        wallet = w.get("wallet_address", "?")[:44]
        score = w.get("total_score", 0)
        tokens = w.get("unique_tokens", 0)
        
        lines.append(f"🔴 #{i} *{wallet}*")
        lines.append(f"   Score: {score}/100 | Tokens: {tokens} | Trades: {w.get('total_txns', 0)}")
        traded = w.get("tokens_traded", [])[:5]
        if traded:
            names = [f"`{t[:12]}...`" for t in traded]
            lines.append(f"   Trading: {', '.join(names)}")
        lines.append("")
    
    for i, w in enumerate(watch[:5], 1):
        wallet = w.get("wallet_address", "?")[:44]
        score = w.get("total_score", 0)
        tokens = w.get("unique_tokens", 0)
        lines.append(f"🟡 #{i} {wallet} | Score: {score} | Tokens: {tokens}")
    
    lines.append(f"\nScanned: {len(wallets)} wallets | TRACK: {len(track)} | WATCH: {len(watch)}")
    return "\n".join(lines)


if __name__ == "__main__":
    print("Wallet Profiler — Jupiter Swap Analysis")
    print("=" * 50)
    
    from feeds.dex_feeds import fetch_all_dex
    pairs = fetch_all_dex()[:5]
    solana = [p for p in pairs if p.get("chain", "") == "solana"
              and p.get("address", "") != "So11111111111111111111111111111111111111112"]
    print(f"Solana pairs: {len(solana)}")
    
    wallets = find_cross_token_wallets(solana, limit=20)
    print(format_wallet_report(wallets))
