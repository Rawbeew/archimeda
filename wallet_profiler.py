"""
Wallet profiler — the smart-money filter.
 Flow: Dexscreener detects a token spike → Solana RPC pulls the token's recent
 swap txs → extract buyer wallets → check each wallet's token history via
 Dexscreener → score them by how many of their past picks did a multiple-X.

 A wallet is SMART if:
 - it held >= 3 different tokens in its history
 - >= SMART_WALLET_HIT_RATIO of those tokens are up >= SMART_WALLET_MIN_GAIN
 - it has been active for >= SMART_WALLET_MIN_TXS transactions

 Returns a SmartWalletScore per wallet. If enough buyers of a token are smart,
 the signal passes the gate.
"""
import requests
import time
from config import (
    SMART_WALLET_MIN_TXS, SMART_WALLET_MIN_UNIQUE_TOKENS,
    SMART_WALLET_MIN_GAIN_PCT, SMART_WALLET_HIT_RATIO,
    SMART_WALLET_MIN_BUYERS, SMART_WALLET_MIN_SMART_RATIO,
    SOLANA_RPC_URL, BASE_RPC_URL, SCAN_LOOKBACK_TXS,
)

TIMEOUT = 20


def _rpc(url, method, params):
    """Call a JSON-RPC endpoint."""
    try:
        r = requests.post(url, json={
            "jsonrpc": "2.0", "id": 1,
            "method": method,
            "params": params,
        }, timeout=TIMEOUT)
        return r.json().get("result")
    except Exception as e:
        print(f"    [rpc] {method} failed: {e}")
        return None


def get_recent_signatures(address, limit=SCAN_LOOKBACK_TXS, rpc_url=None):
    """Get recent transaction signatures for an address."""
    url = rpc_url or SOLANA_RPC_URL
    result = _rpc(url, "getSignaturesForAddress", [address, {"limit": limit}])
    if result:
        return [(s["signature"], s.get("err")) for s in result]
    return []


def get_transaction(sig, rpc_url=None):
    """Fetch a parsed transaction."""
    url = rpc_url or SOLANA_RPC_URL
    return _rpc(url, "getTransaction",
                [sig, {"maxSupportedTransactionVersion": 0, "encoding": "jsonParsed"}])


def extract_buyers_from_pair(pair_address, rpc_url=None, limit=10, token_mint=None):
    """For a Solana token pair address, get recent swap transactions and
    extract the buyer wallet addresses. Returns list of {wallet, sig, token_mint}.

    If pair_address returns no sigs, fall back to token_mint (the token's
    mint address also receives swap activity on Solana)."""
    url = rpc_url or SOLANA_RPC_URL

    # Try pair address first
    sigs = get_recent_signatures(pair_address, limit=limit, rpc_url=url)

    # If no sigs and we have a token mint, try that
    if not sigs and token_mint:
        print(f"    [rpc] pair addr returned no sigs, trying token mint...")
        sigs = get_recent_signatures(token_mint, limit=limit, rpc_url=url)

    if not sigs:
        return []

    buyers = []

    for i, (sig, err) in enumerate(sigs):
        if err:
            continue
        tx = get_transaction(sig, rpc_url=url)
        if not tx:
            continue

        if i == 0:
            print(f"    [rpc] first tx has {len(tx.get('meta', {}).get('postTokenBalances', []))} token balances")

        meta = tx.get("meta", {})
        if not meta:
            continue

        # The fee payer is the first account key; that's who initiated the swap
        msg = tx.get("transaction", {}).get("message", {})
        account_keys = msg.get("accountKeys", [])
        if not account_keys:
            continue

        fee_payer = account_keys[0]
        if isinstance(fee_payer, dict):
            fee_payer = fee_payer.get("pubkey", "")

        # Check post-token balances: did this wallet receive the token?
        post_bal = meta.get("postTokenBalances", [])
        pre_bal = meta.get("preTokenBalances", [])

        for b in post_bal:
            owner = b.get("owner", "")
            mint = b.get("mint", "")
            post_amt = b.get("uiTokenAmount", {}).get("uiAmount")
            acct_idx = b.get("accountIndex", -1)

            if post_amt is None:
                continue

            # Find pre-balance for same account
            pre_amt = None
            for pb in pre_bal:
                if pb.get("accountIndex") == acct_idx:
                    pre_amt = pb.get("uiTokenAmount", {}).get("uiAmount")
                    break

            # If they received tokens (balance increased), they bought
            if pre_amt is None or post_amt > (pre_amt or 0):
                # Skip wrapped SOL (we want the actual token)
                if mint == "So11111111111111111111111111111111111111112":
                    continue

                # Only count the token we're profiling (if we know the mint)
                if token_mint and mint != token_mint:
                    continue

                if owner:
                    buyers.append({
                        "wallet": owner,
                        "token_mint": mint,
                        "sig": sig[:20],
                        "amount": post_amt,
                        "fee_payer": fee_payer == owner,
                    })

        time.sleep(0.15)  # free RPC rate limit

    # Deduplicate by wallet
    seen = set()
    unique_buyers = []
    for b in buyers:
        if b["wallet"] not in seen:
            seen.add(b["wallet"])
            unique_buyers.append(b)
    return unique_buyers


def profile_wallet(wallet_address, rpc_url=None):
    """Check a wallet's transaction history and score it.
    Returns dict with:
    - is_smart: bool
    - total_txs: int
    - unique_tokens: list of mints
    - smart_picks: list of mints that are up >= min gain
    - hit_rate: float
    - score: float (0-100)
    """
    url = rpc_url or SOLANA_RPC_URL

    # 1. Get wallet's transaction count (as proxy for activity)
    sigs = get_recent_signatures(wallet_address, limit=50, rpc_url=url)
    total_txs = len(sigs)

    if total_txs < SMART_WALLET_MIN_TXS:
        return {
            "wallet": wallet_address,
            "is_smart": False,
            "reason": f"not enough txs ({total_txs} < {SMART_WALLET_MIN_TXS})",
            "total_txs": total_txs,
            "unique_tokens": [],
            "smart_picks": [],
            "hit_rate": 0,
            "score": 0,
        }

    # 2. Scan their txs for unique token mints they held
    token_mints = {}  # mint -> count
    for sig, err in sigs:
        if err:
            continue
        tx = get_transaction(sig, rpc_url=url)
        if not tx:
            continue

        meta = tx.get("meta", {})
        post_bal = meta.get("postTokenBalances", [])
        for b in post_bal:
            owner = b.get("owner", "")
            mint = b.get("mint", "")
            if owner == wallet_address and mint:
                if mint not in token_mints:
                    token_mints[mint] = 0
                token_mints[mint] += 1

        time.sleep(0.1)

    # Filter out wrapped SOL
    token_mints.pop("So11111111111111111111111111111111111111112", None)

    if len(token_mints) < SMART_WALLET_MIN_UNIQUE_TOKENS:
        return {
            "wallet": wallet_address,
            "is_smart": False,
            "reason": f"not enough unique tokens ({len(token_mints)} < {SMART_WALLET_MIN_UNIQUE_TOKENS})",
            "total_txs": total_txs,
            "unique_tokens": list(token_mints.keys()),
            "smart_picks": [],
            "hit_rate": 0,
            "score": 0,
        }

    # 3. Check current price performance of each token they held
    smart_picks = []
    checked = 0
    for mint in list(token_mints.keys())[:15]:  # cap at 15 to save API calls
        checked += 1
        try:
            r = requests.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                timeout=10,
            )
            data = r.json()
            pairs = data.get("pairs") or []
            if pairs:
                pairs.sort(key=lambda p: p.get("volume", {}).get("h24", 0), reverse=True)
                p = pairs[0]
                change_24h = p.get("priceChange", {}).get("h24", 0) or 0
                change_6h = p.get("priceChange", {}).get("h6", 0) or 0
                # Use the better of 6h/24h change
                best_change = max(change_24h, change_6h)
                if best_change >= SMART_WALLET_MIN_GAIN_PCT:
                    smart_picks.append({
                        "mint": mint,
                        "symbol": p["baseToken"]["symbol"],
                        "gain": best_change,
                    })
        except Exception:
            pass
        time.sleep(0.2)

    hit_rate = len(smart_picks) / checked if checked > 0 else 0
    is_smart = hit_rate >= SMART_WALLET_HIT_RATIO and len(token_mints) >= SMART_WALLET_MIN_UNIQUE_TOKENS

    # Score: weighted by hit rate * unique token count
    score = min(100, hit_rate * 100 * (len(token_mints) / SMART_WALLET_MIN_UNIQUE_TOKENS))

    return {
        "wallet": wallet_address,
        "is_smart": is_smart,
        "total_txs": total_txs,
        "unique_tokens": list(token_mints.keys()),
        "num_unique_tokens": len(token_mints),
        "smart_picks": smart_picks,
        "num_smart_picks": len(smart_picks),
        "checked": checked,
        "hit_rate": round(hit_rate, 2),
        "score": round(score, 1),
    }


def score_token_buyers(pair_address, chain="solana", limit=10, token_mint=None):
    """Full wallet-profiling gate for a token.
    1. Get recent buyers of the token from on-chain swaps
    2. Profile each buyer
    3. Decide if enough smart wallets are buying

    Returns dict with buyer_profiles and verdict.
    """
    rpc_url = SOLANA_RPC_URL if chain == "solana" else BASE_RPC_URL

    # Step 1: extract buyers
    buyers = extract_buyers_from_pair(pair_address, rpc_url=rpc_url, limit=limit, token_mint=token_mint)

    if len(buyers) < SMART_WALLET_MIN_BUYERS:
        return {
            "pair_address": pair_address,
            "chain": chain,
            "verdict": "INSUFFICIENT_BUYERS",
            "reason": f"only {len(buyers)} buyers found (< {SMART_WALLET_MIN_BUYERS})",
            "num_buyers": len(buyers),
            "smart_wallets": [],
            "total_buyers": len(buyers),
        }

    print(f"    [wallet] profiling {len(buyers)} unique buyers...")

    # Step 2: profile each buyer (cap at 8 to save RPC calls on free tier)
    profiles = []
    for b in buyers[:8]:
        print(f"      profiling {b['wallet'][:12]}...")
        profile = profile_wallet(b["wallet"], rpc_url=rpc_url)
        profiles.append(profile)
        if profile["is_smart"]:
            print(f"        ⭐ SMART: {profile['num_smart_picks']}/{profile['checked']} picks, "
                  f"{profile['num_unique_tokens']} tokens, hit rate {profile['hit_rate']}")
        else:
            print(f"        pass: {profile.get('reason', 'not smart')}")

    # Step 3: verdict
    smart_wallets = [p for p in profiles if p["is_smart"]]
    smart_ratio = len(smart_wallets) / len(profiles) if profiles else 0

    if smart_ratio >= SMART_WALLET_MIN_SMART_RATIO:
        verdict = "APPROVED"
        reason = f"{len(smart_wallets)}/{len(profiles)} buyers are smart wallets ({smart_ratio:.0%})"
    else:
        verdict = "REJECTED"
        reason = f"only {len(smart_wallets)}/{len(profiles)} buyers are smart ({smart_ratio:.0%})"

    return {
        "pair_address": pair_address,
        "chain": chain,
        "verdict": verdict,
        "reason": reason,
        "num_buyers": len(buyers),
        "num_profiled": len(profiles),
        "num_smart": len(smart_wallets),
        "smart_ratio": round(smart_ratio, 2),
        "smart_wallets": [
            {
                "wallet": p["wallet"],
                "score": p["score"],
                "smart_picks": p["num_smart_picks"],
                "unique_tokens": p["num_unique_tokens"],
                "hit_rate": p["hit_rate"],
            }
            for p in smart_wallets
        ],
    }
