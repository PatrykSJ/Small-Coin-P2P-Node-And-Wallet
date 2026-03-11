import threading
from typing import Dict, List, Optional
from collections import defaultdict
from . import miner

class Chain:
    def __init__(self):
        self._lock = threading.Lock()
        self._blockchain: List[dict] = []
        self._tx_index: Dict[str, dict] = {}
        self._index: Dict[str, dict] = {}
        self._parent: Dict[str, str] = {}
        self._children = defaultdict(set)
        self._height: Dict[str, int] = {}
        self._work: Dict[str, int] = {}
        self._total_work: Dict[str, int] = {}
        self._orphans_by_parent = defaultdict(list)
        self._best_tip: Optional[str] = None
        self._removed_main = []

    def set_genesis(self, block: dict) -> None:
        with self._lock:
            self._blockchain = [block]
            self._index.clear()
            self._parent.clear()
            self._children.clear()
            self._height.clear()
            self._work.clear()
            self._total_work.clear()
            self._orphans_by_parent.clear()
            self._tx_index.clear()
            
            h = block.get("hash")
            p = block.get("prev_hash")
            self._index[str(h)] = block
            self._parent[str(h)] = str(p)
            self._height[str(h)] = 0
            w = miner.chainwork_of_block(block)
            self._work[str(h)] = w
            self._total_work[str(h)] = w
            self._best_tip = str(h)
            self._index_block(block)

    def get_blockchain(self) -> List[dict]:
        with self._lock:
            out: List[dict] = []
            for i, b in enumerate(self._blockchain):
                c = dict(b)
                hh = c.get("hash")
                h = self._height.get(hh, i)
                c["numer_bloku"] = int(h)
                out.append(c)
            return out

    def tx_lookup(self, txid: str) -> Optional[dict]:
        if not txid:
            return None
        with self._lock:
            return self._tx_index.get(txid)

    def tx_depth(self, height: int) -> int:
        with self._lock:
            if self._best_tip is None:
                return 0
            tip_height = int(self._height.get(self._best_tip, -1))
        if tip_height < 0 or height < 0:
            return 0
        return max(0, tip_height - int(height) + 1)

    def chain_head(self) -> tuple[int, Optional[dict]]:
        with self._lock:
            if not self._blockchain or self._best_tip is None:
                return -1, None
            last = self._blockchain[-1]
            h = int(self._height.get(self._best_tip, len(self._blockchain) - 1))
            return h, last

    def chain_recent(self, last_n: int = 64) -> List[dict]:
        with self._lock:
            items = self._blockchain[-last_n:]
            base = len(self._blockchain) - len(items)
            out = []
            for i, b in enumerate(items):
                hh = b.get("hash") or b.get("Hash")
                height = self._height.get(hh, base + i)
                out.append({
                    "height": int(height),
                    "hash": hh,
                    "prev_hash": b.get("prev_hash"),
                })
            return out

    def accept_block(self, block: dict) -> List[dict]:
        entered_main: List[dict] = []
        to_process: List[dict] = [block]
        while to_process:
            b = to_process.pop(0)
            queue_children: List[dict] = []
            with self._lock:
                h = b.get("hash")
                p = b.get("prev_hash")
                if not isinstance(h, str) or not isinstance(p, str):
                    continue
                is_genesis = (b.get("numer_bloku", 0) == 0 and p == "0" * 64)
                if (p not in self._index) and not is_genesis:
                    self._orphans_by_parent[p].append(b)
                    continue
                if h in self._index:
                    continue
                self._index[h] = b
                self._work[h] = miner.chainwork_of_block(b)
                self._parent[h] = p
                self._children[p].add(h)
                if is_genesis:
                    self._height[h] = 0
                    self._total_work[h] = self._work[h]
                else:
                    self._height[h] = self._height.get(p, -1) + 1
                    self._total_work[h] = self._total_work.get(p, 0) + self._work[h]
                queue_children = list(self._orphans_by_parent.pop(h, []))
                reorg_needed = (self._best_tip is None) or (self._total_work[h] > self._total_work.get(self._best_tip, -1))
                if reorg_needed:
                    entered_main.extend(self._reorg_to_tip_locked(h))
            if queue_children:
                to_process.extend(queue_children)
        return entered_main

    def export_dag(self, last: int | None = None, include_orphans: bool = True) -> dict:
        with self._lock:
            main_set = set()
            for b in self._blockchain:
                hh = b.get("hash")
                if isinstance(hh, str):
                    main_set.add(hh)
            best_h = self._height.get(self._best_tip, -1) if self._best_tip else -1
            min_h = (best_h - int(last) + 1) if (last is not None and best_h >= 0) else None
            nodes, edges = [], []
            visible = set()
            for h, blk in self._index.items():
                p = self._parent.get(h, None)
                ht = int(self._height.get(h, -1))
                if (min_h is not None) and (ht >= 0) and (ht < min_h):
                    continue
                nodes.append({
                    "id": h,
                    "hash": h,
                    "prev_hash": p,
                    "height": ht,
                    "total_work": int(self._total_work.get(h, 0)),
                    "status": ("main" if h in main_set else "stale"),
                })
                visible.add(h)
            if include_orphans:
                for parent_hash, lst in self._orphans_by_parent.items():
                    for blk in lst:
                        h = blk.get("hash")
                        if not h:
                            continue
                        nodes.append({
                            "id": h,
                            "hash": h,
                            "prev_hash": parent_hash,
                            "height": -1,
                            "total_work": 0,
                            "status": "orphan"
                        })
            for h, blk in self._index.items():
                p = self._parent.get(h, None)
                if p and (h in visible) and (p in visible):
                    edges.append({"source": p, "target": h})
            meta = {
                "best_tip": self._best_tip,
                "best_tip_height": (self._height.get(self._best_tip, -1) if self._best_tip else -1),
                "counts": {
                    "main": sum(1 for n in nodes if n["status"] == "main"),
                    "stale": sum(1 for n in nodes if n["status"] == "stale"),
                    "orphan": sum(1 for n in nodes if n["status"] == "orphan"),
                }
            }
            return {"nodes": nodes, "edges": edges, "meta": meta}

    def truncate_to_height(self, height: int) -> None:
        with self._lock:
            if not self._blockchain:
                return
            cut_idx = None
            for i in range(len(self._blockchain) - 1, -1, -1):
                b = self._blockchain[i]
                h = int(b.get("numer_bloku", i))
                if h <= height:
                    cut_idx = i
                    break
            keep = 0 if cut_idx is None else (cut_idx + 1)
            removed = self._blockchain[keep:]
            for b in removed:
                for tx in (b.get("transactions") or []):
                    tid = miner.txid(tx)
                    if tid:
                        self._tx_index.pop(tid, None)
            del self._blockchain[keep:]

    def _index_block(self, block: dict):
        txs = block.get("transactions") or []
        block_hash = block.get("hash") or block.get("Hash")
        height = int(self._height.get(block_hash, len(self._blockchain) - 1))
        for idx, tx in enumerate(txs):
            tid = miner.txid(tx)
            if tid:
                self._tx_index[tid] = {
                    "height": height,
                    "block_hash": block_hash,
                    "tx_index": idx,
                    "tx": tx,
                }

    def drain_removed_main(self) -> List[dict]:
        with self._lock:
            out = list(self._removed_main)
            self._removed_main.clear()
            return out

    def _find_ancestor(self, a: Optional[str], b: Optional[str]) -> Optional[str]:
                va = a
                vb = b
                if va is None or vb is None:
                    return None
                ha = self._height.get(va, -1)
                hb = self._height.get(vb, -1)
                if ha < 0 or hb < 0:
                    return None
                while ha > hb and va:
                    va = self._parent.get(va)
                    ha -= 1
                while hb > ha and vb:
                    vb = self._parent.get(vb)
                    hb -= 1
                while va and vb and va in self._index and vb in self._index:
                    if va == vb:
                        return va
                    va = self._parent.get(va)
                    vb = self._parent.get(vb)
                return None

    def _reorg_to_tip_locked(self, new_tip: str) -> List[dict]:
        if new_tip == self._best_tip and self._blockchain:
            return []

        if self._best_tip is None or not self._blockchain:
            path = []
            cur = new_tip
            while cur and cur in self._index:
                path.append(cur)
                p = self._parent.get(cur)
                if not p or p == "0" * 64:
                    break
                cur = p
            chain_hashes = list(reversed(path))
            self._blockchain = [self._index[h] for h in chain_hashes]
            self._tx_index.clear()
            for b in self._blockchain:
                self._index_block(b)
            self._best_tip = new_tip
            return list(self._blockchain)

        old_tip = self._best_tip
        ancestor = self._find_ancestor(old_tip, new_tip)

        removed_blocks: List[dict] = []
        if ancestor is None:
            removed_blocks = list(self._blockchain)
            self._blockchain = []
            self._tx_index.clear()
        else:
            cur = old_tip
            while cur and cur != ancestor and cur in self._index:
                removed_blocks.append(self._index[cur])
                cur = self._parent.get(cur)
            ah = self._height.get(ancestor, -1)
            if ah < 0 or ah >= len(self._blockchain):
                removed_blocks = list(self._blockchain)
                self._blockchain = []
                self._tx_index.clear()
            else:
                tail = self._blockchain[ah + 1 :]
                for b in tail:
                    for tx in (b.get("transactions") or []):
                        tid = miner.txid(tx)
                        if tid:
                            self._tx_index.pop(tid, None)
                self._blockchain = self._blockchain[: ah + 1]

        to_add_hashes: List[str] = []
        if ancestor is None:
            cur = new_tip
            stack: List[str] = []
            while cur and cur in self._index:
                stack.append(cur)
                p = self._parent.get(cur)
                if not p or p == "0" * 64:
                    break
                cur = p
            to_add_hashes = list(reversed(stack))
        else:
            cur = new_tip
            stack: List[str] = []
            while cur and cur != ancestor and cur in self._index:
                stack.append(cur)
                cur = self._parent.get(cur)
            to_add_hashes = list(reversed(stack))

        entered: List[dict] = []
        for hh in to_add_hashes:
            b = self._index[hh]
            self._blockchain.append(b)
            self._index_block(b)
            entered.append(b)

        self._best_tip = new_tip
        if removed_blocks:
            self._removed_main.extend(removed_blocks)
        return entered

    