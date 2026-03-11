from transaction import TransactionList
from transaction import Transaction
from wallet import Wallet
import json
import base64
from cryptography.hazmat.primitives.asymmetric import ed25519

def verify_transaction_signature(public_key_b64: str, signature_b64: str, transaction) -> bool:
        try:
            pub_bytes = base64.urlsafe_b64decode(public_key_b64.encode("ascii"))
            sig_bytes = base64.urlsafe_b64decode(signature_b64.encode("ascii"))
            pub = ed25519.Ed25519PublicKey.from_public_bytes(pub_bytes)
        except Exception as e:
            print(f"[verify] failed to decode keys: {e}")
            return False


        try:
            if isinstance(transaction, dict):
                tmp_tx = Transaction(
                    transaction["txin"],
                    transaction["txout"],
                    float(transaction["amount"]),
                    float(transaction["fee"]),
                    nonce=int(transaction.get("nonce", 0))
                )
                tmp_tx.timestamp = transaction["timestamp"]
            else:
                tmp_tx = transaction

            msg = tmp_tx.serialize_for_signing()
        except Exception as e:
            print(f"[verify] failed to serialize tx: {e}")
            return False
        try:
            pub.verify(sig_bytes, msg)
            return True
        except Exception:
            return False