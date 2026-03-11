from hashlib import sha256
from time import time
from typing import List, Dict, Any, Optional, Callable
import json, struct
from . import config

def _sha256(b: bytes) -> bytes:
    return sha256(b).digest()

def _dsha256(b: bytes) -> bytes:
    return _sha256(_sha256(b))

def _hex_le_from_bytes(b: bytes) -> str:
    return b[::-1].hex()

def _compact_to_target(nbits: int) -> int:
    exp = (nbits >> 24) & 0xff
    mant = nbits & 0x007fffff
    if exp <= 3:
        return mant >> (8 * (3 - exp))
    return mant << (8 * (exp - 3))

def _pack_header(version: int, prev_hex: str, mr_hex: str, ts: int, nbits: int, nonce: int) -> bytes:
    prev_le = bytes.fromhex(prev_hex)[::-1]
    mr_le   = bytes.fromhex(mr_hex)[::-1]
    return struct.pack("<L", version) + prev_le + mr_le + struct.pack("<LLL", ts, nbits, nonce)

def _txid_bytes(tx: Dict[str, Any]) -> bytes:
    blob = json.dumps(tx, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _dsha256(blob)

def _merkle_root_hex(txs: List[Dict[str, Any]]) -> str:
    if not txs:
        return "0" * 64
    level = [_txid_bytes(tx) for tx in txs]
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level), 2):
            a = level[i]
            b = level[i + 1] if i + 1 < len(level) else level[i]
            nxt.append(_dsha256(a + b))
        level = nxt
    return _hex_le_from_bytes(level[0])

def _fee_of_tx(tx: Dict[str, Any]) -> float:
    try:
        if "fee" in tx and tx["fee"] is not None:
            return float(tx["fee"])
    except Exception:
        pass
    try:
        d = tx.get("data", {})
        if isinstance(d, dict) and d.get("fee") is not None:
            return float(d["fee"])
    except Exception:
        pass
    return 0.0

def _reward_for_height(height: int) -> float:
    return float(config.BASE_REWARD)

def _block_base(index: int, prev_hash: str, txs: List[Dict[str, Any]], nbits: int) -> Dict[str, Any]:
    ts = int(time())
    return {
        "numer_bloku": index,
        "version": config.VERSION,
        "timestamp": ts,
        "merkle_root": _merkle_root_hex(txs),  
        "prev_hash": prev_hash,                 
        "nBits": nbits,
        "transactions": txs,
    }

def _pow_try_hash(base_wo_hash_nonce: Dict[str, Any], nonce: int) -> tuple[bytes, str]:
    hdr = _pack_header(base_wo_hash_nonce["version"],
                       base_wo_hash_nonce["prev_hash"],
                       base_wo_hash_nonce["merkle_root"],
                       base_wo_hash_nonce["timestamp"],
                       base_wo_hash_nonce["nBits"],
                       nonce)
    h = _dsha256(hdr)
    return h, _hex_le_from_bytes(h)  

def mine_block(index: int,
               prev_hash: str,
               txs_snapshot: List[Dict[str, Any]],
               miner_address: str,
               nbits: int = config.NBITS,
               cancel_cb: Optional[Callable[[], bool]] = None) -> Optional[Dict[str, Any]]:
    total_fees = sum(_fee_of_tx(tx) for tx in txs_snapshot)
    reward = _reward_for_height(index)

    coinbase = {
        "type": "coinbase",
        "txout": miner_address,
        "amount": float(reward + total_fees),
        "reward": float(reward),
        "fees": float(total_fees),
        "height": index
    }

    txs_full = [coinbase] + list(txs_snapshot)
    base = _block_base(index=index, prev_hash=prev_hash, txs=txs_full, nbits=nbits)
    nonce = 0
    target = _compact_to_target(nbits)

    while True:
        if cancel_cb and cancel_cb():
            return None
        h_bytes, h_hex = _pow_try_hash(base, nonce)
        h_int = int.from_bytes(h_bytes, "big")
        if h_int <= target:
            out = dict(base)
            out["nonce"] = nonce
            out["hash"]  = h_hex  
            return out
        nonce = (nonce + 1) & 0xffffffff

def genesis_block() -> Dict[str, Any]:
    NBITS_GENESIS = 0x1e0ffff0
    
    coinbases = []
    for addr, amount in (config.GENESIS_ALLOCATION or {}).items():
        coinbases.append({
            "type": "coinbase",
            "txout": addr,
            "amount": float(amount),
            "reward": float(amount),
            "fees": 0.0,
            "height": 0,
        })

    base = {
        "numer_bloku": 0,
        "version": config.VERSION,
        "timestamp": 0,
        "merkle_root": _merkle_root_hex(coinbases),
        "prev_hash": "0" * 64,
        "nBits": NBITS_GENESIS,
        "transactions": coinbases,
    }

    nonce = 5353390
    target = _compact_to_target(NBITS_GENESIS)
    while True:
        h_bytes, h_hex = _pow_try_hash(base, nonce)
        if int.from_bytes(h_bytes, "big") <= target:
            out = dict(base)
            out["nonce"] = nonce
            out["hash"]  = h_hex
            return out
        nonce = (nonce + 1) & 0xffffffff

def calc_header_hash_hex(block: Dict[str, Any], merkle_root_hex: str) -> tuple[bytes, str]:
    hdr = _pack_header(
        int(block.get("version", 1)),
        str(block.get("prev_hash", "")),
        merkle_root_hex,
        int(block.get("timestamp", 0)),
        int(block.get("nBits", 0)),
        int(block.get("nonce", 0)),
    )
    h = _dsha256(hdr)
    return h, _hex_le_from_bytes(h)


def meets_pow(h_bytes: bytes, nbits: int) -> bool:
    try:
        target = _compact_to_target(int(nbits))
        return int.from_bytes(h_bytes, "big") <= int(target)
    except Exception:
        return False

def chainwork_of_block(block: Dict[str, Any]) -> int:
    try:
        t = _compact_to_target(int(block.get("nBits", 0)))
        if t <= 0 or t >= (1 << 256):
            return 0
        return (1 << 256) // (t + 1)
    except Exception:
        return 0
    
def txid(tx: dict) -> Optional[str]:
    if not isinstance(tx, dict): return None
    v = tx.get("txid")
    return str(v) if v is not None else None
    