import time, queue, threading, datetime
from dataclasses import dataclass
from typing import List, Optional, Dict, Any, Tuple
from collections import deque
from . import config
from . import miner

@dataclass
class InboxItem:
    when: float
    peer: str
    payload: dict

class Inbox:
    def __init__(self, chain, mempool):
        
        self._items: List[InboxItem] = []
        self._pending: List[InboxItem] = []
        self._ok: List[InboxItem] = []
        self._invalid: List[InboxItem] = []
        self._lock = threading.Lock()
        self._queue: "queue.Queue[InboxItem]" = queue.Queue(maxsize=100000)
        self._stop = threading.Event()

        self._mempool = mempool
        self._chain = chain
        
        self._bad_block_hashes: set[str] = set()    
        self._mis_cb = None
        
        self._entered: "deque[tuple[str, dict]]" = deque(maxlen=100000)
        self._worker = threading.Thread(target=self._validator_loop, daemon=True)
        self._worker.start()

    def block_txs_validator(self, block: dict) -> tuple[bool, str]:
        txs = block.get("transactions") or []
        if not txs:
            return False, "empty block"
        ok, why, total_fees = self._mempool.block_txs_validator(txs)
        if not ok:
            return False, why
        reward = float(config.BASE_REWARD)
        coinbases = [t for t in txs if t.get("type") == "coinbase"]
        if len(coinbases) != (0 if block.get("numer_bloku", 0) == 0 else 1):
            return False, "bad coinbase count"
        if coinbases and txs[0].get("type") != "coinbase":
            return False, "coinbase not first"
        if coinbases:
            try:
                cb_amount = float(coinbases[0].get("amount", 0.0))
            except Exception:
                return False, "coinbase amount invalid"
            if cb_amount > reward + total_fees + 1e-12:
                return False, "coinbase too big"
        return True, "ok"

    def block_validator(self, payload: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        if not isinstance(payload, dict):
            return False, payload
        required_keys = ["hash", "prev_hash", "timestamp", "nonce", "version", "nBits", "numer_bloku", "transactions", "merkle_root"]
        for k in required_keys:
            if k not in payload:
                return False, payload
        try:
            nb = int(payload["numer_bloku"])
            ts = int(payload["timestamp"])
            int(payload["nonce"])
            ver = int(payload["version"])
            nbits = int(payload["nBits"])
        except Exception:
            return False, payload
        if nb < 0:
            return False, payload
        h_hex = payload.get("hash")
        p_hex = payload.get("prev_hash")
        if not isinstance(h_hex, str) or len(h_hex) != 64:
            return False, payload
        if not isinstance(p_hex, str) or len(p_hex) != 64:
            return False, payload
        txs = payload.get("transactions")
        if not isinstance(txs, list):
            return False, payload
        now = int(time.time())
        if ts > now + config.MAX_FUTURE_SKEW_SEC:
            return False, payload
        if p_hex == "0" * 64:
            if nb != 0:
                return False, payload
        else:
            if nb == 0:
                return False, payload
        calc_mr = miner._merkle_root_hex(txs)
        if payload.get("merkle_root") != calc_mr:
            return False, payload
        h_bytes, calc_hex = miner.calc_header_hash_hex(payload, calc_mr)
        if calc_hex != h_hex:
            return False, payload
        if not miner.meets_pow(h_bytes, nbits):
            return False, payload
        if ver != config.VERSION:
            return False, payload
        if nbits != config.NBITS:
            return False, payload
        return True, payload

    def add(self, peer: str, payload: dict):
        h = None
        
        try:
            h = str(payload.get("hash"))
        except Exception:
            h = None
            
        if h and h in self._bad_block_hashes:
            it = InboxItem(time.time(), peer, payload)
            with self._lock:
                self._items.append(it)
                self._invalid.append(it)
            return

        it = InboxItem(time.time(), peer, payload)
        with self._lock:
            self._items.append(it)
            self._pending.append(it)
        try:
            self._queue.put_nowait(it)
        except queue.Full:
            with self._lock:
                if it in self._pending:
                    self._pending.remove(it)
                self._invalid.append(it)

    def add_tx(self, source: str, tx: dict):
        self._mempool.add(source, tx)

    def list(self) -> List[dict]:
        with self._lock:
            out = []
            for it in reversed(self._items):
                out.append({
                    "time": datetime.datetime.fromtimestamp(it.when).isoformat(timespec="seconds"),
                    "peer": it.peer,
                    "payload": it.payload,
                })
            return out

    def list_pending(self) -> List[dict]:
        with self._lock:
            return [{"time": datetime.datetime.fromtimestamp(it.when).isoformat(timespec="seconds"), "peer": it.peer, "payload": it.payload} for it in reversed(self._pending)]

    def list_ok(self) -> List[dict]:
        with self._lock:
            return [{"time": datetime.datetime.fromtimestamp(it.when).isoformat(timespec="seconds"), "peer": it.peer, "payload": it.payload} for it in reversed(self._ok)]

    def list_invalid(self) -> List[dict]:
        with self._lock:
            return [{"time": datetime.datetime.fromtimestamp(it.when).isoformat(timespec="seconds"), "peer": it.peer, "payload": it.payload} for it in reversed(self._invalid)]

    def drain_entered_main(self) -> List[tuple[str, dict]]:
        out = []
        with self._lock:
            while self._entered:
                out.append(self._entered.popleft())
        return out

    def getBlockChain(self) -> List[dict]:
        return self._chain.get_blockchain()

    def tx_lookup(self, txid: str):
        return self._chain.tx_lookup(txid)

    def tx_depth(self, height: int) -> int:
        return self._chain.tx_depth(height)

    def chain_head(self) -> tuple[int, dict | None]:
        return self._chain.chain_head()

    def chain_recent(self, last_n: int = 64) -> List[dict]:
        return self._chain.chain_recent(last_n)

    def export_dag(self, last: int | None = None) -> dict:
        return self._chain.export_dag(last=last)

    def _validator_loop(self):
        while not self._stop.is_set():
            try:
                it = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            ok, normalized = False, it.payload
            try:
                ok, normalized = self.block_validator(it.payload)
                if ok:
                    ok2, why2 = self.block_txs_validator(normalized)
                    ok = ok and ok2
            except Exception:
                ok = False
            entered = []
            with self._lock:
                if it in self._pending:
                    self._pending.remove(it)
                it.payload = normalized
                if ok:
                    entered = self._chain.accept_block(normalized)
                    self._ok.append(it)
                else:
                    self._invalid.append(it)
                    try:
                        bh = str((normalized.get("hash") if isinstance(normalized, dict) else None) or it.payload.get("hash") or "")
                        if len(bh) == 64:
                            self._bad_block_hashes.add(bh)
                    except Exception:
                        pass
            self._queue.task_done()
            if not ok and self._mis_cb:
                try:
                    self._mis_cb(it.peer, 1, "block-invalid")
                except Exception:
                    pass
            if entered:
                for b in entered:
                    self._entered.append((it.peer, b))

    def stop(self, timeout: Optional[float] = None):
        self._stop.set()
        self._worker.join(timeout=timeout)

    def _local_head(self) -> tuple[int, Optional[str]]:
        h, last = self._chain.chain_head()
        return h, last.get("hash") if last else None

    def _recent(self, n: int = 64) -> List[dict]:
        return self._chain.chain_recent(n)

    def _find_fork_height(self, remote_recent: List[dict]) -> int:
        if not isinstance(remote_recent, list):
            return -1
        local = self._chain.chain_recent(256)
        local_by_hash = {str(x.get("hash")): int(x.get("height", -1)) for x in local if x.get("hash")}
        try:
            rr = sorted([r for r in remote_recent if isinstance(r, dict) and r.get("hash")],
                        key=lambda r: int(r.get("height", -1)), reverse=True)
        except Exception:
            rr = list(remote_recent)[::-1]
        for r in rr:
            rh = int(r.get("height", -1))
            hh = str(r.get("hash"))
            if hh in local_by_hash:
                return rh
        try:
            min_h = min(int(r.get("height", -1)) for r in remote_recent if isinstance(r, dict))
        except Exception:
            min_h = -1
        return min_h - 1
    
    def set_misbehavior_cb(self, fn):
        self._mis_cb = fn