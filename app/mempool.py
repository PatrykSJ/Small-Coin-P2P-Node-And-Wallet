import time
import threading
import queue
from dataclasses import dataclass
from typing import List, Optional, Callable, Dict, Any, Tuple
from datetime import datetime as _dt
import base64, json, time
from cryptography.hazmat.primitives.asymmetric import ed25519
from . import config
from collections import deque
from . import miner
import hashlib
import re

@dataclass
class TxItem:
    when: float
    source: str
    tx: dict

class Mempool:
    def __init__(self, ledger):
        self._pending: List[TxItem] = []
        self._ok: List[TxItem] = []
        self._invalid: List[TxItem] = []
        self._lock = threading.Lock()
        self._max = 2000

        self._ledger = ledger

        self._q: "queue.Queue[Tuple[int, dict]]" = queue.Queue(maxsize=10000)
        self._stop = threading.Event()

        self._seen: Dict[str, str] = {}
        self._on_valid: Optional[Callable[[str, dict], None]] = None

        self._new_ok = deque(maxlen=100000)
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._mis_cb = None
        self._worker.start()

    def admission_validator(self, tx: dict):
        ok, why = self.tx_precheck(tx)
        if not ok:
            return (False, {**tx, "_why": why})
        ok_txs = self.snapshot_ok(clear=False)
        bal, nonce = self._ledger.project_with_txs(ok_txs)
        ok2 = self._ledger.validate_tx(tx, bal, nonce)
        return ((True, tx) if ok2 else (False, {**tx, "_why": "statecheck-failed"}))

    def set_on_valid(self, fn: Callable[[str, dict], None]):
        self._on_valid = fn
        
    def set_misbehavior_cb(self, fn):
        self._mis_cb = fn

    def add(self, source: str, tx: dict):
        txid = miner.txid(tx)
        with self._lock:
            state = self._seen.get(txid, None) if txid else None
            if state in ("ok", "pending", "invalid"):
                return
            item = TxItem(time.time(), source, tx)
            self._pending.append(item)
            self._trim_list(self._pending)
            if txid:
                self._seen[txid] = "pending"
            idx = len(self._pending) - 1

        try:
            self._q.put_nowait((idx, tx))
        except queue.Full:
            with self._lock:
                if self._pending:
                    it = self._pending.pop()
                    self._invalid.append(it)
                    self._trim_list(self._invalid)
                    if txid:
                        self._seen[txid] = "invalid"

    def list_pending(self) -> List[dict]:
        with self._lock:
            return [self._to_dict(it) for it in reversed(self._pending)]

    def list_ok(self) -> List[dict]:
        with self._lock:
            return [self._to_dict(it) for it in reversed(self._ok)]

    def list_invalid(self) -> List[dict]:
        with self._lock:
            return [self._to_dict(it) for it in reversed(self._invalid)]

    def contains(self, txid: str) -> bool:
        with self._lock:
            return txid in self._seen

    def snapshot_ok(self, clear: bool = True) -> List[dict]:
        with self._lock:
            txs = [it.tx for it in self._ok]
            if clear:
                self._ok.clear()
            return txs

    def stop(self, timeout: Optional[float] = None):
        self._stop.set()
        self._worker.join(timeout=timeout)

    def drain_new_ok(self) -> list[tuple[str, dict]]:
        out = []
        with self._lock:
            while self._new_ok:
                out.append(self._new_ok.popleft())
        return out

    def _worker_loop(self):
        while not self._stop.is_set():
            try:
                _, tx = self._q.get(timeout=0.5)
            except queue.Empty:
                continue

            ok, norm = False, tx
            try:
                ok, norm = self.admission_validator(tx)
            except Exception:
                ok, norm = False, tx

            txid = miner.txid(norm)

            callback_payload = None
            callback_source = None

            with self._lock:
                src = "user"
                idx = -1
                for i in range(len(self._pending)-1, -1, -1):
                    if self._pending[i].tx is tx:
                        idx = i
                        break
                if idx >= 0:
                    item = self._pending.pop(idx)
                    src = item.source
                    item.tx = norm

                    if ok:
                        self._ok.append(item)
                        self._trim_list(self._ok)
                        if txid:
                            prev = self._seen.get(txid)
                            self._seen[txid] = "ok"
                            if prev != "ok":
                                callback_payload = norm # PROBABLY TO BE REMOVED
                                callback_source = src
                                self._new_ok.append((src, norm))
                    else:
                        self._invalid.append(item)
                        self._trim_list(self._invalid)
                        if txid:
                            self._seen[txid] = "invalid"
                        if isinstance(norm, dict) and self._mis_cb and src not in ("self", "user"):
                            why = str(norm.get("_why", ""))
                            if why in (
                                "txin/pubkey mismatch",
                                "incorrect txid value",
                                "bad txid",
                                "bad signature",
                                "bad public_key bytes",
                                "bad public_key b64",
                                "bad signature b64",
                                "bad public_key length",
                            ):
                                self._mis_cb(src, 1, f"tx-{why}")
                else:
                    if txid and txid not in self._seen:
                        self._seen[txid] = "ok" if ok else "invalid"

            self._q.task_done()

            if callback_payload and self._on_valid:
                try:
                    self._on_valid(callback_source, callback_payload)
                except Exception:
                    pass

    def _txids_from_block(self, block: dict) -> set[str]:
        txs = block.get("transactions") or []
        ids = set()
        for tx in txs:
            tid = miner.txid(tx)
            if tid:
                ids.add(tid)
        return ids

    def purge_included_block(self, block: dict) -> dict:
        txids = self._txids_from_block(block)
        if not txids:
            return {"removed_pending": 0, "removed_ok": 0}

        removed_pending = 0
        removed_ok = 0
        with self._lock:
            kept = []
            for it in self._pending:
                tid = miner.txid(it.tx)
                if tid and tid in txids:
                    removed_pending += 1
                    self._seen.pop(tid, None)
                else:
                    kept.append(it)
            self._pending = kept

            # ok
            kept = []
            for it in self._ok:
                tid = miner.txid(it.tx)
                if tid and tid in txids:
                    removed_ok += 1
                    self._seen.pop(tid, None)
                else:
                    kept.append(it)
            self._ok = kept

        return {"removed_pending": removed_pending, "removed_ok": removed_ok}

    def state_of(self, txid: str) -> Optional[str]:
        with self._lock:
            return self._seen.get(txid)

    def _trim_list(self, lst: List[TxItem]):
        if len(lst) > self._max:
            del lst[0 : len(lst) - self._max]

    def _to_dict(self, it: TxItem) -> Dict[str, Any]:
        ts = _dt.fromtimestamp(float(it.when)).isoformat(timespec="seconds")
        return {"time": ts, "source": it.source, "tx": it.tx}

    def _parse_ts(self, ts):
        if isinstance(ts, (int, float)): return int(ts)
        if isinstance(ts, str):
            try: return int(_dt.fromisoformat(ts.replace("Z","+00:00")).timestamp())
            except: pass
        raise ValueError("bad timestamp")


    def lp_bytes(self, s) -> bytes:
        b = str(s or "").encode("utf-8")
        return len(b).to_bytes(4, "big") + b

    def _canon_msg(self, tx: dict) -> bytes:
        parts = []
        parts.append(self.lp_bytes(tx.get("timestamp")))
        parts.append(self.lp_bytes(tx.get("txin")))
        parts.append(self.lp_bytes(tx.get("txout")))

        amount_int = int(round(float(tx.get("amount")) * 10**8))
        fee_int = int(round(float(tx.get("fee")) * 10**8))
        parts.append(amount_int.to_bytes(8, "big", signed=False))
        parts.append(fee_int.to_bytes(8, "big", signed=False))

        nonce_int = int(tx.get("nonce") or 0)
        parts.append(nonce_int.to_bytes(8, "big", signed=False))

        return b"".join(parts)


    def check_tx_id(self, bytes_tx, current_txid) -> bool:
        h = hashlib.sha3_256(bytes_tx).hexdigest()
        calculated_txid = "Tx" + h[:40]
        return calculated_txid == current_txid

    def is_valid_address(self, addr: str) -> bool:
        pattern = r"^Hx[a-f0-9]{40}$"
        return re.match(pattern, addr) is not None

    def _derive_address_from_pub(self, pub_bytes: bytes) -> str:
        h = hashlib.sha3_256(pub_bytes).digest()
        return "Hx" + h[-20:].hex()

    def tx_precheck(self, tx: dict) -> tuple[bool, str]:
        req = ["timestamp","txin","txout","amount","fee","nonce","public_key","signature"]
        for k in req:
            if k not in tx:
                return False, f"missing {k}"
        try:
            ts = self._parse_ts(tx["timestamp"])
            if ts > int(time.time()) + config.MAX_FUTURE_SKEW_SEC:
                return False, "timestamp in future"
            float(tx["amount"])
            float(tx["fee"])
            int(tx["nonce"])
        except Exception:
            return False, "bad types"
        
        if not self.is_valid_address(tx["txin"]):
            return False, "wrong txin address format"
        if not self.is_valid_address(tx["txout"]):
            return False, "wrong txout address format"

        try:
            pub_bytes = base64.urlsafe_b64decode(tx["public_key"].encode("ascii"))
        except Exception:
            return False, "bad public_key b64"

        if len(pub_bytes) != 32:
            return False, "bad public_key length"

        derived_addr = self._derive_address_from_pub(pub_bytes)
        if derived_addr != tx["txin"]:
            return False, "txin/pubkey mismatch"

        try:
            sig_bytes = base64.urlsafe_b64decode(tx["signature"].encode("ascii"))
        except Exception:
            return False, "bad signature b64"

        try:
            pub = ed25519.Ed25519PublicKey.from_public_bytes(pub_bytes)
        except Exception:
            return False, "bad public_key bytes"

        msg = self._canon_msg(tx)
        if not self.check_tx_id(msg, tx["txid"]):
            return False, "bad txid"

        try:
            pub.verify(sig_bytes, msg)
        except Exception:
            return False, "bad signature"

        if float(tx["amount"]) <= 0:
            return False, "non-positive amount"
        if float(tx["fee"]) < 0:
            return False, "negative fee"

        return True, ""

        
    def block_txs_validator(self, txs: List[dict]) -> tuple[bool, str, int]:
        bal, nonce = self._ledger.snapshot_state()

        total_fees = 0.0
        for tx in txs:
            if tx.get("type") == "coinbase":
                continue
            ok, why = self.tx_precheck(tx)
            if not ok:
                return False, f"precheck: {why}", 0
            if not self._ledger.validate_tx(tx, bal, nonce):
                return False, "statecheck", 0
            frm, to, amt, _ = self._ledger._extract_io(tx)
            fee = miner._fee_of_tx(tx)
            bal[frm]   = bal.get(frm, 0.0) - (amt + fee)
            bal[to]    = bal.get(to, 0.0)  + amt
            nonce[frm] = int(nonce.get(frm, 0)) + 1
            total_fees += float(fee or 0.0)

        return True, "ok", total_fees
    
    def requeue_from_stale_block(self, block: dict):
        txs = block.get("transactions") or []
        for tx in txs:
            if tx.get("type") == "coinbase":
                continue
            txid = miner.txid(tx)
            with self._lock:
                if txid:
                    self._seen.pop(txid, None)
            self.add("reorg", tx)

    def revalidate_all_after_reorg(self):
        with self._lock:
            items = list(self._pending) + list(self._ok)
            self._pending.clear()
            self._ok.clear()
            self._invalid.clear()
            self._seen.clear()
        for it in items:
            self.add(it.source, it.tx)