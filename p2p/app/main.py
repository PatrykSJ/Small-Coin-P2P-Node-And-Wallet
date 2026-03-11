from .inbox import Inbox
from .mempool import Mempool
from .peers import PeerManager, p2p_boot
from .webapp import run_http
from .ledger import Ledger
from .mining_ctrl import MinerController
from .chain import Chain

def main():
    chain = Chain()
    ledger = Ledger(chain)
    mempool = Mempool(ledger)
    inbox = Inbox(chain, mempool)
    peers = PeerManager(inbox)
    miner_ctrl = MinerController(inbox, mempool, ledger, peers, chain)
    p2p_boot(peers)
    run_http(peers, inbox, mempool, ledger, miner_ctrl)

if __name__ == "__main__":
    main()
