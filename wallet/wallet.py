import os
import json
import base64
import hashlib
from datetime import datetime, timezone
from getpass import getpass
from argon2.low_level import hash_secret_raw, Type  
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.asymmetric import ed25519
from ecdsa import SigningKey, SECP256k1  
import requests


# default KDF parameters in line with OWASP guideline
DEFAULT_ARGON2_PARAMS = {
    "time_cost": 2,
    "memory_kib": 194566,  
    "parallelism": 1,
    "out_len": 32
}

WALLET_VERSION = 1

class Wallet:

    def __init__(self, path: str, kdf_params: dict = None):
        self.path = path
        self.wallet = None
        print(os.path.exists(path), path)
        if not os.path.exists(path):
            print("Wallet not found, new one is being created")
            self.wallet = self.init_wallet_file(path)
        else:
            with open(path, "r", encoding="utf-8") as f:
                self.wallet = json.load(f)


    def init_wallet_file(self, path: str, kdf_params: dict = None):
        if kdf_params is None:
            kdf_params = DEFAULT_ARGON2_PARAMS
        
        
        directory = os.path.dirname(path)
        if directory and not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)


        salt = os.urandom(16)
        self.wallet = {
            "version": WALLET_VERSION,
            "kdf": {"name": "argon2id", **kdf_params},
            "salt": self.b64e(salt),
            "keys": []
        }
        user_password = getpass("Enter master password to initialize wallet: ")

        # Creating verification key with index 0 - always with initializing new wallet (used for password verification while adding new keys)
        master_key = self.derive_master_key(user_password, salt, kdf_params)
        seed_0 = self.derive_seed_i(master_key, 0)
        priv_raw, pub_raw = self.generate_pair_of_keys(seed_0)
        pub_b64 = self.b64e(pub_raw)

        digest_sha3 = hashes.Hash(hashes.SHA3_256())
        digest_sha3.update(pub_raw)
        hash_bytes = digest_sha3.finalize()
        address = "Hx" + hash_bytes[-20:].hex()

        dek = os.urandom(32)
        aad_wrap = f"key_index=0".encode("utf-8") 
        wrapped_dek = self.aead_encrypt(master_key, dek, aad_wrap)
        encrypted_private_key = self.aead_encrypt(dek, priv_raw, aad_wrap)

        verification_key = {
            "index": 0,
            "label": "verification_key",
            "algorithm": "ed25519",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "public": {
                "format": "raw",
                "public_key": pub_b64,
            },
            "address": address,
            "encrypted_keys": {
                "dek_wrapped": wrapped_dek,
                "priv": encrypted_private_key
            }
        }

        self.wallet["keys"].append(verification_key)

        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.wallet, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass

        # Clear sensitive data
        try:
            for b in (dek, seed_0, priv_raw):
                if isinstance(b, bytearray):
                    for i in range(len(b)): b[i] = 0
        except Exception:
            pass
        return self.wallet


    # Master key generation
    def derive_master_key(self, password: str, salt: bytes, params: dict = None) -> bytes:
        if params is None:
            params = DEFAULT_ARGON2_PARAMS
        master_key = hash_secret_raw(
            secret=password.encode("utf-8"),
            salt=salt,
            time_cost=int(params["time_cost"]),
            memory_cost=int(params["memory_kib"]),
            parallelism=int(params["parallelism"]),
            hash_len=int(params["out_len"]),
            type=Type.ID
        )
        return master_key

    # Generation of the next seed (i_{th}) from Master Key
    def derive_seed_i(self, master_seed: bytes, index: int, length: int = 32) -> bytes:
        info = f"wallet:derive:ed25519:{index}".encode("utf-8")
        return HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info).derive(master_seed)

    def generate_pair_of_keys(self, seed32: bytes):
        priv = ed25519.Ed25519PrivateKey.from_private_bytes(seed32)
        pub = priv.public_key()
        pub_raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw) 
        priv_raw = priv.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption()
        )  
        return priv_raw, pub_raw


    # AEAD helpers (ChaCha20-Poly1305) cipher
    def aead_encrypt(self, key32: bytes, plaintext: bytes, aad: bytes) -> dict:
        nonce = os.urandom(12)
        aead = ChaCha20Poly1305(key32)
        ct = aead.encrypt(nonce, plaintext, aad)
        return {"nonce": self.b64e(nonce), "aad": self.b64e(aad), "ciphertext": self.b64e(ct)}

    def aead_decrypt(self, key32: bytes, enc: dict, expected_aad: bytes) -> bytes:
        nonce = self.b64d(enc["nonce"])
        aad = self.b64d(enc["aad"])
        ct = self.b64d(enc["ciphertext"])
        if aad != expected_aad:
            raise ValueError("AAD tampering detected!")
        aead = ChaCha20Poly1305(key32)
        return aead.decrypt(nonce, ct, aad)




    # Add deterministic keys to wallet
    def wallet_add_derived_key(self, password: str, label: str = None):
        with open(self.path, "r", encoding="utf-8") as f:
            self.wallet = json.load(f)

        try:
            verification_entry = next(k for k in self.wallet["keys"] if k["index"] == 0)
            salt = self.b64d(self.wallet["salt"])
            kdf_params = self.wallet["kdf"]
            test_master_key = self.derive_master_key(password, salt, kdf_params)
            
            aad_wrap = f"key_index=0".encode("utf-8")
            _ = self.aead_decrypt(test_master_key, verification_entry["encrypted_keys"]["dek_wrapped"], aad_wrap)
        except Exception:
            raise ValueError("Incorrect master password - cannot add new key")
        
        existing_indices = {key["index"] for key in self.wallet.get("keys", [])}
        index = max(existing_indices) + 1 if existing_indices else 1

        salt = self.b64d(self.wallet["salt"])
        kdf_params = self.wallet["kdf"]
        master_key = self.derive_master_key(password, salt, kdf_params)

        seed_i = self.derive_seed_i(master_key, index)
        priv_raw, pub_raw = self.generate_pair_of_keys(seed_i)
        pub_b64 = self.b64e(pub_raw)


        digest_sha3 = hashes.Hash(hashes.SHA3_256())
        digest_sha3.update(pub_raw)
        hash_bytes = digest_sha3.finalize()
        address = "Hx" + hash_bytes[-20:].hex()

        dek = os.urandom(32)
        aad_wrap = f"key_index={index}".encode("utf-8") 
        wrapped_dek = self.aead_encrypt(master_key, dek, aad_wrap)
        encrypted_private_key = self.aead_encrypt(dek, priv_raw, aad_wrap)

        current_entry = {
            "index": index,
            "label": label or f"ed25519-{index}",
            "algorithm": "ed25519",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "public": {
                "format": "raw",
                "public_key": pub_b64,
                
            },
            "address": address,
            "encrypted_keys": {
                "dek_wrapped": wrapped_dek,
                "priv": encrypted_private_key
            },
            "nonce": 0
        }

        self.wallet["keys"].append(current_entry)

        try:
            for b in (dek, seed_i, priv_raw):
                if isinstance(b, bytearray):
                    for i in range(len(b)): b[i] = 0 # overwriting RAM memory
        except Exception:
            pass

        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.wallet, f, ensure_ascii=False, indent=2)

        return current_entry


    def wallet_unlock_private_key(self, password: str, index: int):
        with open(self.path, "r", encoding="utf-8") as f:
            self.wallet = json.load(f)

        salt = self.b64d(self.wallet["salt"]) # sól do master seeda
        kdf_params = self.wallet["kdf"]
        master_key = self.derive_master_key(password, salt, kdf_params)

        entry = next((k for k in self.wallet["keys"] if k["index"] == index), None)
        if entry is None:
            raise ValueError("Key index not found")

        aad_wrap = f"key_index={index}".encode("utf-8")
        dek = self.aead_decrypt(master_key, entry["encrypted_keys"]["dek_wrapped"], aad_wrap)
        private_key_raw = self.aead_decrypt(dek, entry["encrypted_keys"]["priv"], aad_wrap)

        private_key_obj = ed25519.Ed25519PrivateKey.from_private_bytes(private_key_raw)

        try:
            for buf in (dek, private_key_raw):
                if isinstance(buf, bytearray):
                    for i in range(len(buf)): buf[i]=0 # clear secrets from RAM
        except Exception:
            pass

        return private_key_obj




    def wallet_unlock_private_key_related_to_address(self, password: str, related_address: str):
        with open(self.path, "r", encoding="utf-8") as f:
            self.wallet = json.load(f)

        salt = self.b64d(self.wallet["salt"])
        kdf_params = self.wallet["kdf"]
        master_key = self.derive_master_key(password, salt, kdf_params)

        entry = None
        for key in self.wallet.get("keys", []):
            if key.get("address") == related_address:
                entry = key
                break
        if entry is None:
            raise ValueError(f"Key not found for address {related_address}")

        aad_wrap = f"key_index={entry.get("index")}".encode("utf-8")
        dek = self.aead_decrypt(master_key, entry["encrypted_keys"]["dek_wrapped"], aad_wrap)
        private_key_raw = self.aead_decrypt(dek, entry["encrypted_keys"]["priv"], aad_wrap)

        private_key_obj = ed25519.Ed25519PrivateKey.from_private_bytes(private_key_raw)
        related_public_key = entry["public"]["public_key"]
        try:
            for buf in (dek, private_key_raw):
                if isinstance(buf, bytearray):
                    for i in range(len(buf)): buf[i]=0 # clear secrets from RAM
        except Exception:
            pass

        return private_key_obj, related_public_key


    # Easy change of the master password owing to DEK implementation
    def wallet_rotate_password(self, path: str, old_password: str, new_password: str):
        with open(path, "r", encoding="utf-8") as f:
            self.wallet = json.load(f)

        try:
            verification_entry = next(k for k in self.wallet["keys"] if k["index"] == 0)
            salt = self.b64d(self.wallet["salt"])
            kdf_params = self.wallet["kdf"]
            test_master_key = self.derive_master_key(old_password, salt, kdf_params)
            
            aad_wrap = f"key_index=0".encode("utf-8")
            _ = self.aead_decrypt(test_master_key, verification_entry["encrypted_keys"]["dek_wrapped"], aad_wrap)
        except Exception:
            raise ValueError("Incorrect old password - cannot rotate password")

        salt = self.b64d(self.wallet["salt"])
        kdf_params = self.wallet["kdf"]
        master_key_old = self.derive_master_key(old_password, salt, kdf_params)
        master_key_new = self.derive_master_key(new_password, salt, kdf_params)

        for entry in self.wallet["keys"]:
            aad_wrap = f"key_index={entry['index']}".encode("utf-8")
            dek = self.aead_decrypt(master_key_old, entry["encrypted_keys"]["dek_wrapped"], aad_wrap)
            new_wrapped = self.aead_encrypt(master_key_new, dek, aad_wrap)
            entry["encrypted_keys"]["dek_wrapped"] = new_wrapped
            try:
                if isinstance(dek, bytearray):
                    for i in range(len(dek)): dek[i]=0
            except Exception:
                pass

        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.wallet, f, ensure_ascii=False, indent=2)

        return True

    def b64e(self, b: bytes) -> str:
        return base64.urlsafe_b64encode(b).decode("ascii")

    def b64d(self, s: str) -> bytes:
        return base64.urlsafe_b64decode(s.encode("ascii"))

    def sign_transaction(self, transaction, password: str, key_index: int) -> dict:
        from transaction import TransactionList
        priv = self.wallet_unlock_private_key(password, key_index)


        address = self.wallet["keys"][key_index]["address"]
        current_nonce = self.get_nonce(address)
        transaction.nonce = current_nonce
        self.increment_nonce(address)

        transaction.txid = transaction.compute_txid()

        message_bytes = transaction.serialize_for_signing()
        signature = priv.sign(message_bytes)
        signature_b64 = base64.urlsafe_b64encode(signature).decode("ascii")
        pub_b64 = self.wallet["keys"][key_index]["public"]["public_key"]
        tx_dict = {
                "timestamp": transaction.timestamp,
                "txin": transaction.txin,
                "txout": transaction.txout,
                "amount": transaction.amount,
                "fee": transaction.fee,
                "txid": transaction.txid,
                "nonce": transaction.nonce,
                "public_key": pub_b64,
                "signature": signature_b64
        }
        return tx_dict
    
    def sign_transactions(self, transaction_list, password: str) -> dict:
        from transaction import TransactionList
        signed_transactions = [] 
        for tx in transaction_list.transactions:
            try:
                relation_address = tx.txin
                tx_private_key, tx_public_key = self.wallet_unlock_private_key_related_to_address(password, relation_address)

                # Set and increment nonce
                current_nonce = self.get_nonce(relation_address)
                tx.nonce = current_nonce
                self.increment_nonce(relation_address)
                
                # Recompute txid with nonce
                tx.txid = tx.compute_txid()

                message_bytes = tx.serialize_for_signing()
                signature = tx_private_key.sign(message_bytes)
                signature_b64 = base64.urlsafe_b64encode(signature).decode("ascii")

                tx_dict = {
                    "timestamp": tx.timestamp,
                    "txin": tx.txin,
                    "txout": tx.txout,
                    "amount": tx.amount,
                    "fee": tx.fee,
                    "txid": tx.txid,
                    "nonce": tx.nonce,
                    "public_key": tx_public_key,
                    "signature": signature_b64
                }
                #tx_dict["public_key"] = tx_public_key
                #tx_dict["signature"] = signature_b64
                #tx_dict["txid"] = tx.txid
                signed_transactions.append(tx_dict)
                print(f'Tx signed: txid={tx.txid}')
            except Exception as e:
                print(f"Failed to sign tx: {e}")
                raise Exception(f"Failed to sign transaction {getattr(tx, 'txid', 'unknown')}: {e}")

        return signed_transactions

    def get_nonce(self, address: str) -> int:
        """Get current nonce for a specific address"""
        with open(self.path, "r", encoding="utf-8") as f:
            self.wallet = json.load(f)
        
        for key in self.wallet.get("keys", []):
            if key.get("address") == address:
                return key.get("nonce", 0)
        raise ValueError(f"Address {address} not found in wallet")



    def increment_nonce(self, address: str) -> int:
        with open(self.path, "r", encoding="utf-8") as f:
            self.wallet = json.load(f)
        
        for key in self.wallet.get("keys", []):
            if key.get("address") == address:
                current_nonce = key.get("nonce", 0)
                key["nonce"] = current_nonce + 1
                
                # Save the updated wallet
                with open(self.path, "w", encoding="utf-8") as f:
                    json.dump(self.wallet, f, ensure_ascii=False, indent=2)
                
                return key["nonce"]
        raise ValueError(f"Address {address} not found in wallet")



    #Splits a single transaction into multiple transactions from different sender's keys from the same wallet
    def create_mixed_transactions(self, tx_out: str, amount: float, fee: float, node_url: str, num_inputs: int = None):
        from transaction import Transaction
        from transaction import TransactionList

        user_addresses = []
        for key in self.wallet.get("keys", []):
            if key["index"] != 0:  
                user_addresses.append(key["address"])
        
        
        if num_inputs is None:
            num_inputs = len(user_addresses)
        
        if len(user_addresses) < num_inputs:
            raise ValueError(f"Not enough addresses in wallet. Need {num_inputs}, have {len(user_addresses)}")
        
        
        import random
        selected_senders = random.sample(user_addresses, num_inputs) 
        
        # Calculating proportions instead of just deviding amount
        proportions = self.generate_random_proportions(num_inputs, amount)
        
        # Calculate fees (can be equal or proportional)
        # fee_parts = original_tx.fee / num_inputs
        fee_parts = [round(fee * (p / sum(proportions)), 8) for p in proportions] ## CUSTOM PROPORTION - NOT IN BTC OR ETH STYLE
        

        insufficient_addresses = []
        sufficient_addresses = []
        address_requirements = {}


        BUFFER_MULTIPLIER = 1.1
        
        for i, sender in enumerate(selected_senders):
            balance = self.get_account_balance(sender, node_url, True)
            required_amount = proportions[i] + fee_parts[i]
            required_with_buffer = required_amount * BUFFER_MULTIPLIER
            deficit = max(0, required_with_buffer - balance)
            
            address_requirements[sender] = {
                'balance': balance,
                'required': required_amount,
                'required_with_buffer': required_with_buffer,
                'proportion': proportions[i],
                'fee_part': fee_parts[i],
                'deficit': deficit
            }
            
            if balance >= required_with_buffer:
                sufficient_addresses.append(sender)
            else:
                insufficient_addresses.append(sender)
                print(f"Address {sender} has deficit of {deficit} (balance: {balance}, required: {required_amount})")
        
        # If any addresses are insufficient, perform internal mixing
        internal_transactions = []
        if insufficient_addresses:
            print(f"Performing internal mixing for {len(insufficient_addresses)} addresses with insufficient funds")
            internal_transactions = self.perform_internal_mixing(insufficient_addresses, sufficient_addresses, address_requirements, node_url)
        
        # Create the main transactions
            

        mixed_transactions = []
        for i, sender in enumerate(selected_senders):
            current_nonce = self.get_nonce(sender)
            mixed_tx = Transaction(
                txin=sender,
                txout=tx_out,
                amount=proportions[i],
                fee=fee_parts[i],
                nonce=current_nonce
            )
            mixed_transactions.append(mixed_tx)
        
        # Return both internal and main transactions
        return {
            'internal_transactions': TransactionList(internal_transactions),
            'main_transactions': TransactionList(mixed_transactions)
        }


    # Performs internal transfers between wallet addresses to ensure all addresses have sufficient funds
    def perform_internal_mixing(self, insufficient_addresses: list, sufficient_addresses: list, address_requirements: dict, node_url: str):
        from transaction import Transaction
        internal_transactions = []
        
        for deficient_addr in insufficient_addresses:
            deficit = address_requirements[deficient_addr]['deficit']
            print(f"Address {deficient_addr} needs {deficit} more coins")
            internal_fee = max(0.01, deficit * 0.01) 
            # Find a source address with sufficient surplus
            source_addr = self.find_funding_source(deficient_addr, deficit, sufficient_addresses, 
                                                address_requirements, node_url, internal_fee)
            
            if source_addr:
                # Create internal transfer transaction
                current_nonce = self.get_nonce(source_addr)
                
                internal_tx = Transaction(
                    txin=source_addr,
                    txout=deficient_addr,
                    amount=deficit,
                    fee=internal_fee,
                    nonce=current_nonce
                )
                
                internal_transactions.append(internal_tx)
                
                # Keep local balance tracking
                address_requirements[source_addr]['balance'] -= (deficit + internal_fee)
                address_requirements[deficient_addr]['balance'] += deficit
                
                print(f"Internal transfer: {source_addr} -> {deficient_addr} for {deficit}")
            else:
                raise Exception(f"Cannot find sufficient funding source for address {deficient_addr}")
        
        return internal_transactions

    # Finds a wallet address that can fund the deficient address
    def find_funding_source(self, deficient_addr: str, deficit: float, sufficient_addresses: list,
                        address_requirements: dict, node_url: str, internal_fee: float):
        # Get all wallet addresses (excluding the deficient one and index 0)
        all_wallet_addresses = []
        for key in self.wallet.get("keys", []):
            if key["index"] != 0 and key["address"] != deficient_addr:
                all_wallet_addresses.append(key["address"])
        
        # First, try addresses not in the current transaction set
        for addr in all_wallet_addresses:
            if addr not in sufficient_addresses and addr not in [deficient_addr]:
                balance = self.get_account_balance(addr, node_url)
                # Check if this address has enough surplus (while keeping buffer)
                required_for_internal = deficit + internal_fee  # amount + internal fee
                if balance > required_for_internal * 1.15:  
                    return addr
        
        # If no external addresses found, try addresses from sufficient_addresses
        for addr in sufficient_addresses:
            if addr != deficient_addr:
                balance_info = address_requirements.get(addr, {})
                current_balance = balance_info.get('balance', self.get_account_balance(addr, node_url))
                required_for_original = balance_info.get('required_with_buffer', 0)
                
                # Check if after helping, this address still has enough for its original transaction
                remaining_after_help = current_balance - (deficit + internal_fee)  # internal transfer fee
                if remaining_after_help >= required_for_original:  
                    return addr
        
        return None


    # Generate random proportions that sum to total_amount so as to split amounts
    def generate_random_proportions(self, num_splits: int, total_amount: float) -> list:
        import random
        
        if num_splits == 1:
            return [total_amount]
        
        
        splits = []
        remaining = total_amount
        
        for i in range(num_splits - 1):
            # More variation: between 10% and 90% of remaining amount
            min_proportion = 0.1  
            max_proportion = 0.9  
            
            if num_splits - i <= 2:
                min_proportion = 0.3  # Ensure reasonable amounts for last splits
            
            proportion = random.uniform(min_proportion, max_proportion)
            split_amount = remaining * proportion
            split_amount = round(split_amount, 8)
            splits.append(split_amount)
            remaining -= split_amount
        
        
        splits.append(round(remaining, 8))
        
        # Shuffling final list of the amounts
        random.shuffle(splits)
        

        total_check = sum(splits)
        if abs(total_check - total_amount) > 0.00000001:
            # Adjust the difference in the largest split to minimize relative error
            difference = total_amount - total_check
            max_index = splits.index(max(splits))
            splits[max_index] = round(splits[max_index] + difference, 8)

        return splits

    # Query the node/ledger for an address balance
    def get_account_balance(self, address: str, node_url: str, nonce: bool = False) -> float:
        try:
            response = requests.get(f"{node_url}/api/balance/{address}", timeout=10)
            if response.status_code == 200:
                data = response.json()
                if nonce:
                    ledger_nonce = int(data.get("nonce", 0))
                    current_nonce = self.get_nonce(address)
                    if ledger_nonce != current_nonce:
                        self.update_wallet_nonce(address, ledger_nonce)
                        print(f"Nonce for {address} updated: {current_nonce} -> {ledger_nonce}")
                    else:
                        print(f"Nonce for {address} is already up-to-date: {ledger_nonce}")
                return float(data.get("balance", 0.0))
            else:
                print(f"Warning: cannot fetch balance for {address}: {response.status_code}")
                return 0.0
        except Exception as e:
            print(f"Error checking balance for {address}: {e}")
            return 0.0


    # Query the ledger for balances of all wallet addresses (except index 0)
    def get_balances_for_all_addresses(self, node_url: str, sync_nonce: bool = False) -> dict:
        import requests

        addresses = [key["address"] for key in self.wallet_data.get("keys", [])
                     if key.get("index") != 0 and "address" in key]
        if not addresses:
            print("No addresses found (excluding index 0)")
            return {"per_address": {}, "total": 0.0}

        payload = {"addresses": addresses}
        per_addr, errors = {}, {}

        try:
            resp = requests.post(f"{node_url}/api/balances", json=payload, timeout=10)
            if resp.status_code != 200:
                raise RuntimeError(f"Unexpected status {resp.status_code}: {resp.text}")

            data = resp.json()
            if not isinstance(data, list):
                raise ValueError("Unexpected response format (expected list of {address,balance,nonce})")

            for entry in data:
                try:
                    addr = entry["address"]
                    bal = float(entry.get("balance", 0.0))
                    per_addr[addr] = bal
                    if sync_nonce and "nonce" in entry:
                        try:
                            ledger_nonce = int(entry["nonce"])
                            self.update_wallet_nonce(addr, ledger_nonce)
                        except Exception as e:
                            print(f"Nonce sync failed for {addr}: {e}")
                except Exception as e:
                    print(f"Failed to parse entry {entry}: {e}")
                    errors[entry.get("address", "?")] = str(e)

            total = sum(per_addr.values())
            result = {"per_address": per_addr, "total": float(total)}
            if errors:
                result["errors"] = errors
            return float(total)

        except Exception as e:
            print(f"Failed to query balances: {e}")
            return 0.0

    # Internal method to update the nonce for an address in the wallet file
    def update_wallet_nonce(self, address: str, new_nonce: int):
        with open(self.path, "r", encoding="utf-8") as f:
            self.wallet = json.load(f)
        
        # Find the key with matching address and update its nonce
        for key in self.wallet.get("keys", []):
            if key.get("address") == address:
                key["nonce"] = new_nonce
                break
        
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.wallet, f, ensure_ascii=False, indent=2)

    # Signs and sends a list of transactions to the ledger
    def send_transaction_list(self, transaction_list, node_url: str, password: str):
        from transaction import TransactionList
        
        if not transaction_list or len(transaction_list.transactions) == 0:
            print("No transactions to send")
            return {'successful': [], 'failed': [], 'results': {}}
        
        # Sign all transactions
        signed_txs = self.sign_transactions(transaction_list, password)
        
        results = {}
        for tx in signed_txs:
            txid = tx['txid']
            try:
                response = requests.post(f"{node_url}/api/tx", json=tx, timeout=30)
                if response.status_code == 200:
                    response_data = response.json()
                    print(f"Transaction {txid} sent to ledger")
                    results[txid] = {
                        'status': 'accepted',
                        'response': response_data
                    }
                else:
                    print(f"Failed to send transaction {txid}: {response.status_code}")
                    results[txid] = {
                        'status': 'http_error',
                        'error': f"HTTP {response.status_code}"
                    }
            except Exception as e:
                print(f"Error sending transaction {txid}: {e}")
                results[txid] = {
                    'status': 'error',
                    'error': str(e)
                }
        
        # Separate successful and failed transactions
        successful_txids = [txid for txid, result in results.items() if result['status'] == 'accepted']
        failed_txids = [txid for txid, result in results.items() if result['status'] != 'accepted']
        
        print(f"Send results: {len(successful_txids)} accepted, {len(failed_txids)} failed")
        
        return {
            'successful': successful_txids,
            'failed': failed_txids,
            'results': results
        }


    # Waits for transactions to be confirmed using info returned by the node
    def wait_for_transaction_confirmations(self, txids: list, node_url: str, delay: float = 5.0):
        import time
        if not txids:
            return {'all_confirmed': True, 'confirmed': [], 'pending': [], 'not_found': []}
        
        print(f"Waiting for {len(txids)} transactions...")
        
        tx_status = {txid: 'pending' for txid in txids}
        try:
            while True:
                for txid in txids:
                    if tx_status[txid] != 'pending':
                        continue
                        
                    try:
                        response = requests.get(f"{node_url}/api/tx/status/{txid}", timeout=10) 
                        if response.status_code == 200:
                            data = response.json()
                            if data.get('final'):
                                tx_status[txid] = 'confirmed'
                                print(f"{txid} confirmed (final)")
                            elif data.get('present') == 'none':
                                tx_status[txid] = 'not_found'
                                print(f"{txid} not found on ledger")
                            else:
                                mp_state = data.get('mempool_state')
                                if mp_state == 'invalid':
                                    tx_status[txid] = 'not_found' 
                                    print(f"{txid} is INVALID in mempool (statecheck failed)")
                                elif data.get('in_blockchain'):
                                    print(f". {txid} still in blockchain (but not final)")
                                elif mp_state in ('pending', 'ok'):
                                    print(f". {txid} still in mempool")
                        elif response.status_code == 404:
                            tx_status[txid] = 'not_found'
                            print(f"{txid} 404 not found")
                    except Exception as e:
                        print(f"Error checking {txid}: {e}")
                
                # Check if all are finalized
                pending_count = sum(1 for status in tx_status.values() if status == 'pending')
                confirmed_count = sum(1 for status in tx_status.values() if status == 'confirmed')
                not_found_count = sum(1 for status in tx_status.values() if status == 'not_found')
                
                print(f"Status: {confirmed_count} confirmed, {not_found_count} not found, {pending_count} pending")
                
                if pending_count == 0:
                    break
                    
                time.sleep(delay)
        except KeyboardInterrupt:
            print("User interrupted waiting process. Returning current transaction status...")

        
        # Final results
        confirmed_txids = [txid for txid, status in tx_status.items() if status == 'confirmed']
        not_found_txids = [txid for txid, status in tx_status.items() if status == 'not_found']
        pending_txids = [txid for txid, status in tx_status.items() if status == 'pending']
        
        return {
            'all_confirmed': len(confirmed_txids) == len(txids),
            'confirmed': confirmed_txids,
            'not_found': not_found_txids,
            'pending': pending_txids
        }

    # Main method for executing entire mixed transaction flow
    def execute_mixed_transaction_flow(self, tx_out: str, amount: float, fee: float, node_url: str, password: str, num_inputs: int = None):
        try:
            verification_entry = next(k for k in self.wallet["keys"] if k["index"] == 0)
            salt = self.b64d(self.wallet["salt"])
            kdf_params = self.wallet["kdf"]
            test_master_key = self.derive_master_key(password, salt, kdf_params)
            
            aad_wrap = f"key_index=0".encode("utf-8")
            _ = self.aead_decrypt(test_master_key, verification_entry["encrypted_keys"]["dek_wrapped"], aad_wrap)
        except Exception:
            raise ValueError("Incorrect master password - cannot add new key")


        import time
        print("=== Starting Mixed Transaction Flow ===")

        # Step 1: Create transaction plan
        print("Step 1: Creating transaction plan...")
        transaction_plan = self.create_mixed_transactions(tx_out, amount, fee, node_url, num_inputs)
        
        internal_txs = transaction_plan['internal_transactions']
        main_txs = transaction_plan['main_transactions']
        
        # Step 2: Send internal transactions (ABORT ON ANY FAILURE)
        if internal_txs and len(internal_txs.transactions) > 0:
            print(f"Step 2: Sending {len(internal_txs.transactions)} internal transactions...")
            send_result = self.send_transaction_list(internal_txs, node_url, password)
            
            # ABORT if any failed to send
            if send_result['failed']:
                print(f"{len(send_result['failed'])} internal transactions failed to send. Aborting.")
                return False
            
            # Wait for confirmations
            print("Waiting for internal transactions to confirm...")
            confirmation_result = self.wait_for_transaction_confirmations(send_result['successful'], node_url)
            
            # ABORT if any are not found or still pending
            if confirmation_result['not_found'] or confirmation_result['pending']:
                failed_count = len(confirmation_result['not_found']) + len(confirmation_result['pending'])
                print(f"{failed_count} internal transactions failed. Aborting.")
                return False
            
            print(f"All {len(confirmation_result['confirmed'])} internal transactions confirmed")
            time.sleep(2)  # Wait for ledger to update balances
        else:
            print("No internal mixing needed")
        
        # Step 3: Send main transactions
        print(f"Step 3: Sending {len(main_txs.transactions)} main transactions...")
        send_result = self.send_transaction_list(main_txs, node_url, password)

        # Show status but don't abort - just report
        if send_result['failed']:
            print(f"{len(send_result['failed'])} main transactions failed to send (continuing anyway)")
        else:
            print("All main transactions sent successfully")

        # If none were sent successfully, then abort
        if not send_result['successful']:
            print("No main transactions were sent successfully")
            return False

        print(f"Waiting for {len(send_result['successful'])} main transactions to confirm...")
        confirmation_result = self.wait_for_transaction_confirmations(send_result['successful'], node_url)


        confirmed_txids = confirmation_result.get('confirmed', [])
        not_found_txids = confirmation_result.get('not_found', [])
        pending_txids = confirmation_result.get('pending', [])


        if confirmed_txids:
            print(f"Mixed transaction flow completed! {len(confirmed_txids)} transactions confirmed")
            for txid in confirmed_txids:
                print(f" Confirmed tx: {txid}")

        if not_found_txids:
            print(f"{len(not_found_txids)} main transactions not found:")
            for txid in not_found_txids:
                print(f" Not found tx: {txid}")

        if pending_txids:
            print(f"{len(pending_txids)} main transactions still pending:")
            for txid in pending_txids:
                print(f" Pending tx: {txid}")


        return confirmation_result.get('all_confirmed', False)

    # sends single tx to ledger
    def send_transaction(self, tx, node_url: str, password: str, key_index: int):
        try:
            verification_entry = next(k for k in self.wallet["keys"] if k["index"] == 0)
            salt = self.b64d(self.wallet["salt"])
            kdf_params = self.wallet["kdf"]
            test_master_key = self.derive_master_key(password, salt, kdf_params)
            
            aad_wrap = f"key_index=0".encode("utf-8")
            _ = self.aead_decrypt(test_master_key, verification_entry["encrypted_keys"]["dek_wrapped"], aad_wrap)
        except Exception:
            raise ValueError("Incorrect master password - cannot add new key")
        from transaction import TransactionList
        with open(self.path, "r", encoding="utf-8") as f:
            self.wallet = json.load(f)
        address = self.wallet["keys"][key_index]["address"]

        try:
            _ = self.get_account_balance(address, node_url, nonce=True)
        except Exception as e:
            print(f"Warning: failed to sync nonce for {address}: {e}")

        # Sign tx
        signed_tx = self.sign_transaction(tx, password, key_index)

        try:
            response = requests.post(f"{node_url}/api/tx", json=signed_tx, timeout=30)
            if response.status_code == 200:
                print("Single transaction sent successfully")
            else:
                print(f"Failed to send transaction: HTTP {response.status_code}")
        except Exception as e:
            print(f"Error sending transaction: {e}")
