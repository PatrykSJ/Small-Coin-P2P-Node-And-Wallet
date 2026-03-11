import threading
import time
from typing import Optional, Dict, Any
from .miner import mine_block, genesis_block
from . import config

class MinerController:
    def __init__(self, inbox, mempool, ledger, peers, chain):
        self._inbox = inbox
        self._mempool = mempool
        self._ledger = ledger
        self._peers = peers
        self._chain = chain
        
        self._auto = False
        self._busy = False
        self._lock = threading.Lock()
        self._t: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._react_stop = threading.Event()
        self._react_t = threading.Thread(target=self._reactor_loop, daemon=True)
        self._react_t.start()
        self.bootstrap_genesis()


    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {"busy": self._busy, "auto": self._auto}

    def set_auto(self, enabled: bool):
        with self._lock:
            self._auto = bool(enabled)
            if not self._auto and self._busy:
                self._stop.set()
            elif self._auto and not self._busy:
                self._start_async()

    def mine_once(self) -> bool:
        with self._lock:
            if self._busy:
                return False
            return self._start_async()

    def on_external_block(self, block: dict):
        self._stop.set()

        def try_restart():
            time.sleep(0.1)
            with self._lock:
                if self._auto and not self._busy:
                    self._start_async()
        threading.Thread(target=try_restart, daemon=True).start()

    def _start_async(self) -> bool:
        self._stop = threading.Event()
        self._busy = True
        t = threading.Thread(target=self._worker, daemon=True)
        self._t = t
        t.start()
        return True

    def _worker(self):
        try:
            txs = self._mempool.snapshot_ok(clear=False)
            bc = self._inbox.getBlockChain()
            prev_hash = bc[-1]["hash"] if bc else ("0" * 64)
            next_index = len(bc) if bc else 0

            cancel_cb = self._stop.is_set
            block = mine_block(index=next_index,
                               prev_hash=prev_hash,
                               txs_snapshot=txs,
                               miner_address=config.MINER_ADDRESS,
                               cancel_cb=cancel_cb)
            if block and not self._stop.is_set():
                self._inbox.add("self", block)
        finally:
            with self._lock:
                self._busy = False
            with self._lock:
                if self._auto and not self._stop.is_set():
                    self._start_async()

    def bootstrap_genesis(self):
        g = genesis_block()
        self._chain.set_genesis(g)
        self._ledger.rebuild_from_chain(self._inbox.getBlockChain())

    def _reactor_loop(self):
        while not self._react_stop.is_set():
            for rb in self._chain.drain_removed_main():
                self._ledger.rollback_block(rb)
                self._mempool.requeue_from_stale_block(rb)

            for origin, blk in self._inbox.drain_entered_main():
                self._ledger.apply_block(blk, commit=True)
                self._mempool.purge_included_block(blk)
                self.on_external_block(blk)
                self._peers.broadcast_json_except(
                    {"type": "BLOCK", "payload": blk},
                    except_addr=origin
                )

            for src, tx in self._mempool.drain_new_ok():
                self._peers.broadcast_json_except({"type": "TX", "payload": tx}, except_addr=src or "self")

            time.sleep(0.05)
