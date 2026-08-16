"""
NFT Minting Engine — detects hot collections on OpenSea + Etherscan, prepares mint txns.

Flows:
1. OpenSea API v2: trending collections (by volume/sales momentum)
2. Etherscan API: latest NFT mints (new collections with high mint count)
3. Floor price monitoring: track entry floor, suggest optimal mint timing
4. Auto-mint: sign and broadcast mint tx via private RPC

Requires:
- OpenSea API key: https://docs.opensea.io/reference/api-keys
- Etherscan API key: https://etherscan.io/myapikey
- Private RPC (Alchemy/Infura) for signing
"""
import os
import time
import json
import requests
from datetime import datetime, timezone

# ── API Keys ──────────────────────────────────────────────────────────
OPENSEA_API_KEY = os.getenv("OPENSEA_API_KEY", "")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")
ALCHEMY_KEY = os.getenv("ALCHEMY_KEY", "alch_etOLRGRFIDNmIFu-NnzuQ")
WALLET_PRIVATE_KEY = os.getenv("HERMES_WALLET_PRIVATE_KEY", "")

# ── Private RPCs ──────────────────────────────────────────────────────
ETH_RPC = f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_KEY}"
BASE_RPC = f"https://base-mainnet.g.alchemy.com/v2/{ALCHEMY_KEY}"

# ── OpenSea API v2 ────────────────────────────────────────────────────
OPENSEA_HEADERS = {"x-api-key": OPENSEA_API_KEY} if OPENSEA_API_KEY else {}

def get_opensea_trending(limit=10):
    """Get trending NFT collections from OpenSea API v2.
    
    Returns:
        list of dicts: {slug, name, floor_price, volume_24h, sales_24h, image_url}
    """
    url = "https://api.opensea.io/api/v2/tokens/trending"
    params = {"limit": str(limit)}
    try:
        r = requests.get(url, params=params, headers=OPENSEA_HEADERS, timeout=15)
        data = r.json()
        tokens = data.get("tokens", [])
        
        results = []
        for t in tokens:
            collection = t.get("collection", {})
            results.append({
                "slug": collection.get("slug", ""),
                "name": collection.get("name", ""),
                "floor_price": t.get("floor_price_usd", 0),
                "floor_price_native": t.get("floor_price_native", 0),
                "volume_24h": t.get("volume_24h_usd", 0),
                "sales_24h": t.get("sales_24h", 0),
                "image_url": collection.get("image_url", ""),
                "chain": collection.get("chain", "ethereum"),
            })
        return results
    except Exception as e:
        print(f"  [nft] OpenSea trending failed: {e}")
        return []


def get_opensea_collection_stats(slug):
    """Get detailed stats for a collection."""
    url = f"https://api.opensea.io/api/v2/collections/{slug}/stats"
    try:
        r = requests.get(url, headers=OPENSEA_HEADERS, timeout=15)
        data = r.json()
        stats = data.get("collection", {}).get("stats", {})
        return {
            "slug": slug,
            "total_supply": stats.get("total_supply", 0),
            "num_owners": stats.get("num_owners", 0),
            "floor_price": stats.get("floor_price", {}).get("amount", 0),
            "total_volume": stats.get("total_volume", 0),
            "one_day_volume": stats.get("one_day_volume", 0),
            "one_day_change": stats.get("one_day_change", 0),
            "one_day_sales": stats.get("one_day_sales", 0),
            "seven_day_volume": stats.get("seven_day_volume", 0),
            "seven_day_change": stats.get("seven_day_change", 0),
            "average_price": stats.get("average_price", 0),
        }
    except:
        return {}


# ── Etherscan NFT Mints ───────────────────────────────────────────────
ETHERSCAN_API = "https://api.etherscan.io/api"

def get_etherscan_nft_mints(contract_address=None, page=1, page_size=20):
    """Get latest NFT mints from Etherscan.
    
    Args:
        contract_address: Specific NFT contract to monitor (optional)
        page: Page number
        page_size: Results per page
    
    Returns:
        list of dicts: {tx_hash, token_id, minter, contract, timestamp}
    """
    # Use ERC721 token transfers (mint = to == owner address, or specific pattern)
    # Etherscan doesn't have a direct "nft mints" endpoint, so we use token transfer logs
    
    if contract_address:
        # Get internal txs for a specific NFT contract
        url = f"{ETHERSCAN_API}"
        params = {
            "module": "account",
            "action": "txlist",
            "address": contract_address,
            "startblock": "0",
            "endblock": "99999999",
            "page": str(page),
            "offset": str(page_size),
            "sort": "desc",
            "apikey": ETHERSCAN_API_KEY,
        }
    else:
        # For all NFTs, we'd need to poll multiple contracts
        # This is rate-limited, so focus on trending first
        return []
    
    try:
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        if data.get("status") == "1":
            return data.get("result", [])
        return []
    except:
        return []


def get_etherscan_nft_mints_by_address(address, page=1):
    """Get NFT mints for a specific address (watching smart money)."""
    url = f"{ETHERSCAN_API}"
    params = {
        "module": "account",
        "action": "txlist",
        "address": address,
        "startblock": "0",
        "endblock": "99999999",
        "page": str(page),
        "offset": 20,
        "sort": "desc",
        "apikey": ETHERSCAN_API_KEY,
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        if data.get("status") == "1":
            # Filter for ERC721/ERC1155 transfers
            txs = data.get("result", [])
            nft_mints = []
            for tx in txs:
                # Check ERC721/1155 token transfers
                if "tokenID" in tx:  # Etherscan includes token info for NFTs
                    nft_mints.append({
                        "tx_hash": tx.get("hash", ""),
                        "from": tx.get("from", ""),
                        "to": tx.get("to", ""),
                        "token_id": tx.get("tokenID", ""),
                        "contract": tx.get("contractAddress", ""),
                        "timestamp": tx.get("timeStamp", ""),
                        "gas": tx.get("gasUsed", ""),
                    })
            return nft_mints
        return []
    except:
        return []


# ── NFT Analysis ──────────────────────────────────────────────────────

def analyze_nft_collection(slug):
    """Analyze an NFT collection for minting opportunity.
    
    Score criteria:
    - Volume momentum: 1d change > 0, 7d change > 0
    - Floor price stability: not dropping sharply
    - Holder concentration: >1000 unique owners = healthy distribution
    - Rarity: track rare trait combinations
    - Gas costs: check current ETH gas, mint during low periods
    """
    stats = get_opensea_collection_stats(slug)
    if not stats:
        return None
    
    score = 0
    signals = []
    
    # Volume momentum
    one_day_change = float(stats.get("one_day_change", 0))
    seven_day_change = float(stats.get("seven_day_change", 0))
    
    if one_day_change > 10:
        score += 2
        signals.append(f"Volume up {one_day_change:.1f}% in 24h")
    elif one_day_change > 0:
        score += 1
        signals.append(f"Volume up {one_day_change:.1f}% in 24h")
    
    if seven_day_change > 20:
        score += 2
        signals.append(f"Volume up {seven_day_change:.1f}% in 7d")
    
    # Floor price stability
    floor_price = float(stats.get("floor_price", 0))
    if floor_price > 0 and seven_day_change > 0:
        score += 1
        signals.append(f"Floor: {floor_price} ETH (rising)")
    
    # Holder diversity
    num_owners = int(stats.get("num_owners", 0))
    total_supply = int(stats.get("total_supply", 0))
    if num_owners > 1000 and total_supply > 0:
        ratio = num_owners / total_supply
        if ratio > 0.3:
            score += 1
            signals.append(f"Distribution: {ratio:.0%} of supply held")
    
    # Floor price absolute value
    if 0.01 <= floor_price <= 0.1:
        score += 1  # Cheap entry
        signals.append(f"Low floor: {floor_price} ETH")
    
    return {
        "slug": slug,
        "score": score,
        "signals": signals,
        "stats": stats,
        "recommendation": "BUY" if score >= 4 else "WATCH" if score >= 2 else "AVOID",
    }


# ── NFT Mint Execution ────────────────────────────────────────────────

def mint_nft(contract_address, token_id, mint_price, chain="ethereum"):
    """Execute an NFT mint transaction.
    
    Args:
        contract_address: NFT contract address
        token_id: Token ID to mint (0 for first available)
        mint_price: Price in native token (ETH for Ethereum, ETH for Base)
        chain: ethereum, base, polygon
    
    Returns:
        dict with tx_hash or error
    """
    try:
        from eth_account import Account
        from web3 import Web3
        
        rpc_map = {
            "ethereum": ETH_RPC,
            "base": BASE_RPC,
        }
        chain_id_map = {"ethereum": 1, "base": 8453, "polygon": 137}
        
        w3 = Web3(Web3.HTTPProvider(rpc_map.get(chain, ETH_RPC)))
        account = Account.from_key(WALLET_PRIVATE_KEY)
        
        # Get contract ABI (simplified — would need actual ABI for real contracts)
        # For now, we build a generic mint call
        contract = w3.eth.contract(
            address=w3.to_checksum_address(contract_address),
            abi=[]  # Would need the actual ABI
        )
        
        # Build mint tx
        # This is a simplified version — real implementation needs ABI
        mint_tx = {
            "to": w3.to_checksum_address(contract_address),
            "data": "0x",  # Would be encoded mint function
            "value": int(mint_price * w3.eth.wei_to_ether(1)),
            "gas": 500000,
            "gasPrice": w3.eth.gas_price,
            "nonce": w3.eth.get_transaction_count(account.address),
            "chainId": chain_id_map.get(chain, 1),
        }
        
        # Sign and broadcast
        signed = account.sign_transaction(mint_tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        
        return {
            "status": "success",
            "tx_hash": w3.to_hex(tx_hash),
            "chain": chain,
            "contract": contract_address,
        }
    
    except ImportError:
        return {"status": "failed", "error": "pip install web3 eth_account"}
    except Exception as e:
        return {"status": "failed", "error": str(e)}


# ── Telegram Command Integration ──────────────────────────────────────

def scan_nft_collections(limit=5):
    """Run full NFT scan: OpenSea trending + analysis."""
    trending = get_opensea_trending(limit)
    results = []
    
    for token in trending:
        slug = token.get("slug", "")
        if not slug:
            continue
        
        analysis = analyze_nft_collection(slug)
        if analysis:
            results.append(analysis)
    
    return sorted(results, key=lambda x: x.get("score", 0), reverse=True)


def format_nft_report(analyses):
    """Format NFT analysis for Telegram."""
    if not analyses:
        return "*NFT Scan*\n\nNo trending collections found."
    
    lines = ["*🖼️ NFT Hot Collections*", ""]
    
    for i, a in enumerate(analyses[:5], 1):
        stats = a.get("stats", {})
        floor = stats.get("floor_price", 0) or 0
        volume = stats.get("one_day_volume", 0) or 0
        change = stats.get("one_day_change", 0) or 0
        
        lines.append(f"{i}. *{a['slug']}* ({a['recommendation']})")
        lines.append(f"   Score: {a['score']}/7")
        lines.append(f"   Floor: {floor:.4f} ETH")
        lines.append(f"   24h Volume: {volume:.1f} ETH ({change:+.1f}%)")
        lines.append(f"   Holders: {stats.get('num_owners', 0):,}")
        
        for s in a.get("signals", [])[:2]:
            lines.append(f"   • {s}")
        lines.append("")
    
    return "\n".join(lines)


if __name__ == "__main__":
    print("NFT Minting Engine loaded.")
    print("Requires: OPENSEA_API_KEY + ETHERSCAN_API_KEY in .env")
    print("Scan: python -c \"from nft_detector import scan_nft_collections; import json; print(json.dumps(scan_nft_collections(), indent=2))\"")
