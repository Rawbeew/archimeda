"""
Wallet Profiler — early entrant detection via getTransactionsForAddress.

For each degen token:
1. Use Helius getTransactionsForAddress(mint) to get all recent txs involving that mint
2. Parse the account keys to find which wallets bought/sold
3. Track who's active across multiple tokens
"""
import os
import json
import requests
from collections import defaultdict

HELIUS_RPC = os.getenv("HELIUS_RPC_URL",
    "https://mainnet.helius-rpc.com/?api-key=" +
    os.getenv("HELIUS_API_KEY", "f394978d-9cc9-447a-b2cf-d5abbdd49a0a")
)

JUPITER_V6 = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
RAYDIUM_AMM = "675kPX9M4SG3G7eaCztUo626fA96R5LaQqyRr682sbBt"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SOL_MINT = "So11111111111111111111111111111111111111112"
JITO = "jitonobundLe11111111111111111111111111111111"


def get_mint_transactions(mint_address, limit=200):
    """Get all recent transactions involving a token mint via Helius.
    
    Uses getTransactionsForAddress which returns signature-level results.
    Then fetches full tx details for parsing.
    """
    txns = []
    try:
        r = requests.post(HELIUS_RPC, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getTransactionsForAddress",
            "params": [mint_address, {"limit": limit}],
        }, timeout=15)
        
        data = r.json().get("result", {}).get("data", [])
        
        for sig_data in data:
            sig = sig_data["signature"]
            block_time = sig_data.get("blockTime", 0)
            
            r2 = requests.post(HELIUS_RPC, json={
                "jsonrpc": "2.0", "id": 2,
                "method": "getTransaction",
                "params": [sig, {"maxSupportedTransactionVersion": 0, "encoding": "json"}],
            }, timeout=8)
            
            tx = r2.json().get("result", {})
            if not tx:
                continue
            
            meta = tx.get("meta", {})
            msg = tx.get("transaction", {}).get("message", {})
            account_keys = msg.get("accountKeys", [])
            
            # Resolve account keys to addresses
            addresses = []
            for ak in account_keys:
                addr = ak if isinstance(ak, str) else ak.get("pubkey", "")
                addresses.append(addr)
            
            # Parse instructions to find direction
            direction = None
            for instr in msg.get("instructions", []):
                pid_idx = instr.get("programIdIndex", -1)
                if pid_idx < len(addresses):
                    pid = addresses[pid_idx]
                    if pid == JUPITER_V6 or pid.startswith("JUP"):
                        direction = "swap"
                    elif pid == RAYDIUM_AMM:
                        direction = "swap"
            
            txns.append({
                "signature": sig,
                "block_time": block_time,
                "accounts": addresses,
                "direction": direction,
            })
    
    except Exception as e:
        print(f"  [wallet] Fetch failed: {e}")
    
    return txns


def parse_swap_accounts(tx, mint, addresses):
    """Parse a swap transaction to find buyer/seller wallets.
    
    Looks for Jupiter/Raydium instructions and finds:
    - Token input account (who sold)
    - Token output account (who bought)
    - The signer (who initiated the swap)
    """
    results = []
    msg_accounts = addresses
    
    # Find Jupiter/Raydium instruction
    jupiter_instr = None
    for instr in msg_accounts:
        pass  # We'll parse from top-level instructions
    
    # Actually, we need the top-level instructions, not account keys
    # The tx was already parsed - let me check the raw tx structure
    return results


def find_early_entrants(token_pairs, limit=20):
    """Find wallets trading across multiple degen tokens.
    
    For each token:
    1. Get all recent transactions involving the mint
    2. Parse account keys to find wallets involved in swaps
    3. Track wallet -> token mapping
    
    Returns wallets active across multiple tokens = real money movers.
    """
    tokens = []
    seen = set()
    for pair in token_pairs:
        addr = pair.get("address", "")
        if addr and addr not in seen and addr != "So11111111111111111111111111111111111111112":
            tokens.append(addr)
            seen.add(addr)
    
    print(f"  [wallet] Analyzing {len(tokens)} tokens...")
    
    # Map: wallet -> set of tokens they traded
    wallet_tokens = defaultdict(set)
    wallet_txns = defaultdict(int)
    wallet_details = {}  # wallet -> list of trade details
    
    for i, token_addr in enumerate(tokens[:10]):
        print(f"  [wallet] Token {i+1}/{len(tokens)}: {token_addr[:12]}...")
        
        txns = get_mint_transactions(token_addr, limit=100)
        print(f"  [wallet] Found {len(txns)} transactions")
        
        for j, tx in enumerate(txns[:50]):
            accounts = tx.get("accounts", [])
            
            # Find the signers from inner instruction account lists
            # or from the accounts that are NOT program IDs
            wallets = set()
            for acct in accounts:
                # Skip program IDs and known constants
                if acct in (SOL_MINT, TOKEN_PROGRAM, JITO):
                    continue
                if len(acct) != 44:
                    continue
                if acct.startswith("1111") or acct.startswith("ComputeBudget"):
                    continue
                if acct.startswith("Tokenkeg"):
                    continue
                if acct.startswith("ATokenGP"):
                    continue
                if acct.startswith("JUP6"):
                    continue
                if acct.startswith("675k"):
                    continue
                if acct == "Sysvar":
                    continue
                
                # This looks like a wallet address
                wallets.add(acct)
            
            # Filter: keep wallets that traded this specific token
            if wallets:
                for w in wallets:
                    wallet_tokens[w].add(token_addr)
                    wallet_txns[w] += 1
    
    print(f"  [wallet] Unique wallets: {len(wallet_tokens)}")
    
    # Score and rank
    results = []
    for wallet, token_set in wallet_tokens.items():
        diversity = len(token_set)
        txn_count = wallet_txns[wallet]
        
        if diversity < 1:
            continue
        
        # Base score for trading activity
        score = 0
        if diversity >= 3:
            score += 40
        elif diversity >= 2:
            score += 25
        else:
            score += 10
        
        if txn_count > 50:
            score += 30
        elif txn_count > 20:
            score += 20
        elif txn_count > 5:
            score += 10
        else:
            score += 5
        
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
        return "*🕵️ Wallet Profiler*\n\nNo cross-token wallets found.\nTip: try when more tokens have volume."
    
    lines = ["*🕵️ Cross-Token Wallet Tracker*", ""]
    
    track = [w for w in wallets if w.get("recommendation") == "TRACK"]
    watch = [w for w in wallets if w.get("recommendation") == "WATCH"]
    
    for i, w in enumerate(track[:10], 1):
        wallet = w.get("wallet_address", "?")[:44]
        score = w.get("total_score", 0)
        tokens = w.get("unique_tokens", 0)
        txns = w.get("total_txns", 0)
        
        lines.append(f"🔴 #{i} *{wallet}*")
        lines.append(f"   Score: {score}/100 | Tokens: {tokens} | Trades: {txns}")
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
    from feeds.dex_feeds import fetch_all_dex
    
    print("Wallet Profiler — Cross-Token Analysis")
    print("=" * 50)
    
    pairs = fetch_all_dex()[:5]
    solana = [p for p in pairs if p.get("chain", "") == "solana"
              and p.get("address", "") != "So11111111111111111111111111111111111111112"]
    print(f"Solana pairs: {len(solana)}")
    
    wallets = find_early_entrants(solana, limit=20)
    print(format_wallet_report(wallets))
