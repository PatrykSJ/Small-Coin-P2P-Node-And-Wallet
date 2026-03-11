import json
import hashlib
import base64
from datetime import datetime, timezone
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.exceptions import InvalidSignature
from wallet import Wallet
from typing import List
 

    

class Transaction():
    def __init__(self, txin: str, txout: str, amount: float, fee: float, nonce: int = None):
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.txin = txin        # sender address 
        self.txout = txout      # receiver address 
        self.amount = amount   
        self.fee = fee          # fixed transaction fee
        self.nonce = nonce 
        self.txid = self.compute_txid()


    def convert_to_dict(self):
        return {
            "timestamp": self.timestamp,
            "txin": self.txin,
            "txout": self.txout,
            "amount": self.amount,
            "fee": self.fee,
            "nonce": self.nonce
        }



    def lp_bytes(self, s: str) -> bytes:
        """
        Turns a string into an explicitly length-prefixed byte sequence.
        This way the tx is hashed unambigously to the contrary to json.

        """
        b = (s or "").encode("utf-8")
        return len(b).to_bytes(4, "big") + b

    def serialize_for_signing(self) -> bytes:
        parts = []
        parts.append(self.lp_bytes(self.timestamp))
        parts.append(self.lp_bytes(self.txin))
        parts.append(self.lp_bytes(self.txout))

        # convert amounts to integer atomic units (8 decimals) - to avoid loosing precision with float representation
        amount_int = int(round(self.amount * 10**8))
        fee_int = int(round(self.fee * 10**8))
        parts.append(amount_int.to_bytes(8, "big", signed=False))
        parts.append(fee_int.to_bytes(8, "big", signed=False))

        nonce_int = 0 if self.nonce is None else int(self.nonce)
        parts.append(nonce_int.to_bytes(8, "big", signed=False))

        return b"".join(parts)


    def compute_txid(self) -> str:
        raw = self.serialize_for_signing()
        h = hashlib.sha3_256(raw).hexdigest()
        return "Tx" + h[:40]
    
class TransactionList:
    def __init__(self, transfer_list: List[Transaction]):
        self.version = 1
        self.transactions = transfer_list
        

    def convert_to_dict(self) -> dict:
        transactions_list = []
        for transaction in self.transactions:
            tx_temp_dict = {
            "timestamp": transaction.timestamp,
            "txin": transaction.txin,
            "txout": transaction.txout,
            "amount": transaction.amount,
            "fee": transaction.fee,
            "txid": transaction.txid,
            "nonce": transaction.nonce
        }
            transactions_list.append(tx_temp_dict)
        return transactions_list