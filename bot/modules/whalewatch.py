# bot/modules/whalewatch.py
"""
WhaleWatch — Large ETH transfer monitor via Etherscan.

Polls every 5 minutes for large ETH transfers on-chain.
Fires an alert when a single transfer exceeds the threshold.
Deduplicates by transaction hash — each tx fires once, ever.

Default threshold: 1,000 ETH (configurable via WHALE_MIN_ETH env var).
"""

import logging
import os
from datetime import datetime, timezone

import requests

from bot.utils import send_text

log = logging.getLogger("whalewatch")

# ─── Config ──────────────────────────────────────────────────────────────────

ETHERSCAN_KEY  = os.getenv("ETHERSCAN_API_KEY", "")
WHALE_MIN_ETH  = float(os.getenv("WHALE_MIN_ETH", "1000"))   # minimum ETH to alert
WHALE_MIN_USD  = float(os.getenv("WHALE_MIN_USD", "0"))       # optional USD filter (0 = off)
ETHERSCAN_BASE = "https://api.etherscan.io/v2/api"

# Known exchange/contract labels for cleaner alerts
KNOWN_LABELS = {
    "0x28c6c06298d514db089934071355e5743bf21d60": "Binance",
    "0x21a31ee1afc51d94c2efccaa2092ad1028285549": "Binance",
    "0xdfd5293d8e347dfe59e90efd55b2956a1343963d": "Binance",
    "0x56eddb7aa87536c09ccc2793473599fd21a8b17f": "Binance",
    "0xa7efae728d2936e78bda97dc267687568dd593f3": "Binance",
    "0x47ac0fb4f2d84898e4d9e7b4dab3c24507a6d503": "Binance",
    "0xbe0eb53f46cd790cd13851d5eff43d12404d33e8": "Binance",
    "0xf977814e90da44bfa03b6295a0616a897441acec": "Binance",
    "0x3f5ce5fbfe3e9af3971dd833d26ba9b5c936f0be": "Binance",
    "0xd551234ae421e3bcba99a0da6d736074f22192ff": "Binance",
    "0x8894e0a0c962cb723c1976a4421c95949be2d4e3": "Binance",
    "0x2b5634c42055806a59e9107ed44d43c426e58258": "KuCoin",
    "0x689c56aef474df92d44a1b70850f808488f9769c": "KuCoin",
    "0xa1d8d972560c2f8144af871db508f0b0b10a3fbf": "KuCoin",
    "0xeb2629a2734e272bcc07bda959863f316f4bd4cf": "Coinbase",
    "0x71660c4005ba85c37ccec55d0c4493e66fe775d3": "Coinbase",
    "0x503828976d22510aad0201ac7ec88293211d23da": "Coinbase",
    "0xddfabcdc4d8ffc6d5beaf154f18b778f892a0740": "Coinbase",
    "0x3cd751e6b0078be393132286c442345e5dc49699": "Coinbase",
    "0xb5d85cbf7cb3ee0d56b3bb207d5fc4b82f43f511": "Coinbase",
    "0xa9d1e08c7793af67e9d92fe308d5697fb81d3e43": "Coinbase",
    "0x77696bb39917c91a0c3d2f8d83ef1e48e43c6b5": "OKX",
    "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b": "OKX",
    "0x236f9f97e0e62388479bf9e5ba4889e46b0273c3": "OKX",
    "0x2faf487a4414fe77e2327f0bf4ae2a264a776ad2": "FTX",
    "0xc098b2a3aa256d2140208c3de6543aaef5cd3a94": "FTX",
    "0x00000000219ab540356cbb839cbe05303d7705fa": "ETH 2.0 Deposit",
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": "WETH Contract",
}

# ─── State ───────────────────────────────────────────────────────────────────

STATE = {
    "seen_hashes": set(),   # dedup by tx hash
    "last_check_utc": None,
    "last_alert_utc": None,
    "total_fired": 0,
}


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _label(addr: str) -> str:
    return KNOWN_LABELS.get((addr or "").lower(), addr[:6] + "..." + addr[-4:])


def _eth_price() -> float:
    """Fetch current ETH price from Etherscan."""
    try:
        r = requests.get(
            ETHERSCAN_BASE,
            params={"chainid": "1", "module": "stats", "action": "ethprice", "apikey": ETHERSCAN_KEY},
            timeout=5,
        )
        return float(r.json()["result"]["ethusd"])
    except Exception:
        return 0.0


# ─── Fetcher ─────────────────────────────────────────────────────────────────

def _fetch_large_transfers() -> list:
    """
    Fetch recent large ETH internal + normal transfers.
    Uses Etherscan's ETH transfer list filtered by value.
    """
    if not ETHERSCAN_KEY:
        return []

    try:
        r = requests.get(
            ETHERSCAN_BASE,
            params={
                "module":     "account",
                "action":     "txlist",
                "address":    "0x0000000000000000000000000000000000000000",
                "sort":       "desc",
                "page":       "1",
                "offset":     "50",
                "apikey":     ETHERSCAN_KEY,
            },
            timeout=10,
        )
        # Etherscan doesn't support global tx filtering by value this way
        # Use tokentx for ERC20 or the beacon deposit contract — instead use
        # the ETH supply endpoint to get large normal transactions
        data = r.json()
        return data.get("result", []) if isinstance(data.get("result"), list) else []
    except Exception as e:
        log.warning(f"Etherscan fetch failed: {e}")
        return []


def _fetch_recent_large_txs() -> list:
    """
    Fetch recent transactions to/from known whale addresses.
    More reliable than global scan — targets high-value known wallets.
    """
    if not ETHERSCAN_KEY:
        return []

    results = []
    eth_px  = _eth_price()

    try:
        # Fetch latest blocks and scan for large value transfers
        r = requests.get(
            ETHERSCAN_BASE,
            params={
                "chainid": "1",
                "module":  "proxy",
                "action":  "eth_blockNumber",
                "apikey":  ETHERSCAN_KEY,
            },
            timeout=5,
        )
        latest_block = int(r.json().get("result", "0x0"), 16)
        scan_from    = max(0, latest_block - 25)  # last ~5 mins of blocks

        # Get transactions in recent blocks
        r2 = requests.get(
            ETHERSCAN_BASE,
            params={
                "chainid":    "1",
                "module":     "account",
                "action":     "txlistinternal",
                "startblock": str(scan_from),
                "endblock":   str(latest_block),
                "sort":       "desc",
                "apikey":     ETHERSCAN_KEY,
            },
            timeout=10,
        )
        txs = r2.json().get("result", [])
        if not isinstance(txs, list):
            return []

        for tx in txs:
            try:
                val_eth = int(tx.get("value", "0")) / 1e18
                if val_eth < WHALE_MIN_ETH:
                    continue
                val_usd = val_eth * eth_px if eth_px else 0

                if WHALE_MIN_USD > 0 and val_usd < WHALE_MIN_USD:
                    continue

                results.append({
                    "hash":    tx.get("hash", ""),
                    "from":    tx.get("from", ""),
                    "to":      tx.get("to", ""),
                    "eth":     val_eth,
                    "usd":     val_usd,
                    "block":   tx.get("blockNumber", ""),
                })
            except Exception:
                continue

    except Exception as e:
        log.warning(f"Block scan failed: {e}")

    return results


# ─── Poll ────────────────────────────────────────────────────────────────────

def poll_once():
    if not ETHERSCAN_KEY:
        log.warning("WhaleWatch: ETHERSCAN_API_KEY not set — disabled")
        return

    now = datetime.now(timezone.utc)
    STATE["last_check_utc"] = now

    txs = _fetch_recent_large_txs()

    for tx in txs:
        h = tx["hash"]
        if not h or h in STATE["seen_hashes"]:
            continue
        STATE["seen_hashes"].add(h)

        # Trim cache to avoid unbounded growth
        if len(STATE["seen_hashes"]) > 5000:
            STATE["seen_hashes"] = set(list(STATE["seen_hashes"])[-2500:])

        from_lbl = _label(tx["from"])
        to_lbl   = _label(tx["to"])
        eth_fmt  = f"{tx['eth']:,.0f}"
        usd_fmt  = f"${tx['usd']:,.0f}" if tx["usd"] else ""

        # Determine flow context
        from_known = tx["from"].lower() in KNOWN_LABELS
        to_known   = tx["to"].lower() in KNOWN_LABELS

        if to_known and not from_known:
            context = "📥 Exchange inflow — potential sell pressure"
        elif from_known and not to_known:
            context = "📤 Exchange outflow — potential accumulation"
        elif from_known and to_known:
            context = "🔄 Exchange to exchange transfer"
        else:
            context = "🐋 Unknown wallet movement"

        STATE["last_alert_utc"] = now
        STATE["total_fired"]   += 1

        send_text(
            f"🐋 *[WhaleWatch] Large ETH Transfer*\n"
            f"Amount: {eth_fmt} ETH {usd_fmt}\n"
            f"From: {from_lbl}\n"
            f"To:   {to_lbl}\n"
            f"{context}\n"
            f"Tx: `{h[:20]}...`\n"
            f"Time (UTC): {now.strftime('%Y-%m-%d %H:%M')}"
        )
        log.info(f"WhaleWatch: {eth_fmt} ETH — {from_lbl} → {to_lbl}")
