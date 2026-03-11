from typing import Dict, Any, Tuple, Optional, List
from collections import defaultdict
import threading
from . import config
from . import miner

class Ledger:
    def __init__(self, chain):
        self._bal = defaultdict(float)
        self._nonce = defaultdict(int)     
        self._lock = threading.Lock()
        self._chain = chain 
        self._immature_cache_tip = None
        self._immature_cache_bal = {}

    def _extract_io(self, tx: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[float], Optional[int]]:
        d = tx.get("data", tx)
        if not isinstance(d, dict):
            return None, None, None, None
        frm = d.get("txin")
        to  = d.get("txout")
        amt = d.get("amount")
        n   = d.get("nonce")
        try: amt = float(amt) if amt is not None else None
        except Exception: amt = None
        try: n = int(n) if n is not None else None
        except Exception: n = None
        return frm, to, amt, n

    def get_balance(self, addr: str) -> float:
        if not addr: return 0.0
        with self._lock:
            return float(self._bal.get(addr, 0.0))
        
    def get_nonce(self, addr: str) -> int:    
        if not addr: return 0
        with self._lock:
            return int(self._nonce.get(addr, 0))

    def balances(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._bal)


    def rollback_block(self, block: dict) -> bool:
        txs = block.get("transactions") or []
        with self._lock:
            bal = dict(self._bal)
            nonce = dict(self._nonce)

        coinbases = [tx for tx in txs if tx.get("type") == "coinbase"]
        normal = [tx for tx in txs if tx.get("type") != "coinbase"]

        for cb in reversed(coinbases):
            to_addr = cb.get("txout") or cb.get("miner")
            amt = cb.get("amount")
            try:
                amt = float(amt)
            except Exception:
                return False
            if not to_addr or amt < 0:
                return False
            bal[to_addr] = bal.get(to_addr, 0.0) - amt

        for tx in reversed(normal):
            frm, to, amt, _ = self._extract_io(tx)
            if not frm or not to or amt is None:
                return False
            fee = miner._fee_of_tx(tx)
            bal[frm] = bal.get(frm, 0.0) + (amt + fee)
            bal[to] = bal.get(to, 0.0) - amt
            nonce[frm] = int(nonce.get(frm, 0)) - 1
            if nonce[frm] < 0:
                nonce[frm] = 0

        with self._lock:
            self._bal = defaultdict(float, bal)
            self._nonce = defaultdict(int, nonce)
        return True

    def rebuild_from_chain(self, chain: List[Dict[str, Any]]):
        with self._lock:
            self._bal.clear()
            self._nonce.clear()
        if not chain:
            return
        tip_idx = len(chain) - 1
        for i, b in enumerate(chain):
            depth = tip_idx - i + 1
            if i == 0 or depth >= int(config.COINBASE_MATURITY):
                ok = self.apply_block(b, commit=True)
                if not ok:
                    break

    def validate_tx(self, tx: Dict[str, Any], bal: Dict[str, float], nonce: Dict[str, int]) -> bool:
        frm, to, amt, n = self._extract_io(tx)
        if not frm or not to or amt is None or amt < 0:
            return False
        fee = miner._fee_of_tx(tx)
        if fee < 0:
            return False

        expected = int(nonce.get(frm, 0))
        if n is None or int(n) != expected:
            return False

        available = bal.get(frm, 0.0) - self._immature_coinbase_for(frm)
        if available < (amt + fee):
            return False

        return True

    def apply_block(self, block: Dict[str, Any], commit: bool = True) -> bool:
        txs = block.get("transactions") or []
        with self._lock:
            bal = dict(self._bal)
            nonce = dict(self._nonce)

        coinbases = [tx for tx in txs if tx.get("type") == "coinbase"]
        normal = [tx for tx in txs if tx.get("type") != "coinbase"]

        prev_hash = block.get("prev_hash")
        is_genesis = (prev_hash == "0" * 64)
        if not is_genesis and len(coinbases) != 1:
            return False

        for tx in normal:
            if not self.validate_tx(tx, bal, nonce):
                return False
            frm, to, amt, _ = self._extract_io(tx)
            fee = miner._fee_of_tx(tx)
            bal[frm]   = bal.get(frm, 0.0) - (amt + fee)
            bal[to]    = bal.get(to, 0.0)  + amt
            nonce[frm] = int(nonce.get(frm, 0)) + 1

        for cb in coinbases:              
            to_addr = cb.get("txout") or cb.get("miner")
            amt = cb.get("amount")
            try: amt = float(amt)
            except Exception: return False
            if not to_addr or amt < 0:
                return False
            bal[to_addr] = bal.get(to_addr, 0.0) + amt

        if commit:
            with self._lock:
                self._bal   = defaultdict(float, bal)
                self._nonce = defaultdict(int, nonce)
        return True

    def snapshot_state(self) -> Tuple[Dict[str, float], Dict[str, int]]:
        with self._lock:
            return dict(self._bal), dict(self._nonce)

    def project_with_txs(self, txs: List[Dict[str, Any]]) -> Tuple[Dict[str, float], Dict[str, int]]:
        bal, nonce = self.snapshot_state()
        for t in (txs or []):
            if t.get("type") == "coinbase":
                continue
            frm, to, amt, n = self._extract_io(t)
            if not frm or not to or amt is None:
                continue
            fee = miner._fee_of_tx(t)
            expected = int(nonce.get(frm, 0))
            try:
                if n is not None and int(n) == expected and bal.get(frm, 0.0) >= (amt + fee):
                    bal[frm]  = bal.get(frm, 0.0) - (amt + fee)
                    bal[to]   = bal.get(to, 0.0) + amt
                    nonce[frm] = expected + 1
            except Exception:
                pass
        return bal, nonce

    def get_confirmed_balance(self, addr: str) -> float:
        if not addr:
            return 0.0
        full = self.get_balance(addr)
        if full <= 0:
            return 0.0

        imm = self._immature_coinbase_for(addr)
        visible = full - imm
        return visible if visible > 0 else 0.0

    def _immature_coinbase_for(self, addr: str) -> float:
        if not addr or self._chain is None:
            return 0.0
        self._ensure_immature_cache()
        v = self._immature_cache_bal.get(addr, 0.0)
        try:
            return float(v)
        except Exception:
            return 0.0
        
    def _ensure_immature_cache(self) -> None:
        if self._chain is None:
            self._immature_cache_tip = None
            self._immature_cache_bal = {}
            return
        tip_h, tip_block = self._chain.chain_head()
        if tip_block is None:
            self._immature_cache_tip = None
            self._immature_cache_bal = {}
            return
        if self._immature_cache_tip == tip_h:
            return
        blocks = self._chain.get_blockchain()
        if not blocks:
            self._immature_cache_tip = tip_h
            self._immature_cache_bal = {}
            return
        total = defaultdict(float)
        for blk in reversed(blocks):
            h = int(blk.get("numer_bloku", 0))
            if h == 0:
                continue
            depth = tip_h - h + 1
            if depth >= config.COINBASE_MATURITY:
                break
            txs = blk.get("transactions") or []
            if not txs:
                continue
            for tx in txs:
                if tx.get("type") == "coinbase":
                    to_addr = tx.get("txout")
                    if not to_addr:
                        continue
                    try:
                        amt = float(tx.get("amount", 0.0))
                    except Exception:
                        amt = 0.0
                    total[to_addr] += amt
                    break
        self._immature_cache_tip = tip_h
        self._immature_cache_bal = dict(total)