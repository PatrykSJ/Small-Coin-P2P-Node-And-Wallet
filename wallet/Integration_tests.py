import base64
from transaction import TransactionList
from transaction import Transaction
from wallet import Wallet
from getpass import getpass
import os
import signature_verification as signature_verification
import json
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
#####################
#### Test Wallet ####
#####################
def wallet():
    path = "wallet/wallet_files/test2510_3.json" # Ścieżka do pliku portfela do zapsisu1
    w = Wallet(path)
    entered_master_password = getpass("Enter master password to add key: ")
    entry = w.wallet_add_derived_key(entered_master_password,  label="test")
    print("Added entry:", entry["label"], "pub:", entry["public"]["public_key"])
    entry2 = w.wallet_add_derived_key(entered_master_password,  label="test2")
    print("Added entry:", entry2["label"], "pub:", entry2["public"]["public_key"])
    priv = w.wallet_unlock_private_key(entered_master_password, index=2)
    private_key_bytes = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption()
    )
    private_key_str = base64.b64encode(private_key_bytes).decode('utf-8')
    print("Private key: ", private_key_str)
    #entered_master_password = getpass("Enter master password to add key: ")
    #entry = w.wallet_add_derived_key(entered_master_password, index=1, label="test")
    #print("Added entry:", entry["label"], "pub:", entry["public"]["public_key"])

    # pw2 = getpass("Enter master password to unlock key: ")
    # # # obiekt klucza prywatnego - umożliwia operacje np. sign_transaction
    # entry = w.wallet_add_derived_key(pw2, label="test")
    # print("Added entry:", entry["label"], "pub:", entry["public"]["public_key"])
    # priv = w.wallet_unlock_private_key(pw2, index=1)
    # private_key_bytes = priv.private_bytes(
    #     encoding=serialization.Encoding.Raw,
    #     format=serialization.PrivateFormat.Raw,
    #     encryption_algorithm=serialization.NoEncryption()
    # )
    # private_key_str = base64.b64encode(private_key_bytes).decode('utf-8')

    #print("Private key: ", private_key_str) # Odszyfrowanie klucza prywatnego
    
    # pub = "WFJLmLHyP5Ce1cn4fkYsuKdIxd96jfp9oLga8fgPeOA="

    # pub_bytes = base64.urlsafe_b64decode(pub.encode("ascii"))
    # print("Public key: ", pub)
    
    # secret_message = "Tajna wiadomosc"
    
    # signature = priv.sign(secret_message.encode('utf-8')) # hash 
    # print("Signed message:", signature)
    
    # pub = ed25519.Ed25519PublicKey.from_public_bytes(pub_bytes)
    # print(pub.verify(signature, secret_message.encode('utf-8')))
    
    #print("Unlocked private object:", type(priv))
    #print('signed: ', w.sign_transaction(priv, {'key':'test'}))


############################################
####### TEST WALLET AND TRANSACTIONS #######
############################################

def init():
    wallet = Wallet("wallet_files/test_02_11.json")
    wallet.wallet_add_derived_key("test", "test1")
    wallet.wallet_add_derived_key("test", "test2")
    wallet.wallet_add_derived_key("test", "test3")

def wall_and_trans():

    ### MANY TRANSACTIONS ###
    wallet = Wallet("wallet_files/test_02_11.json")
    tx = Transaction("Hx28bc72d9fbcce93543f2c5a13721cd7a1be341cd", "Hx28bc72d9fbcce93543f2c5a13721cd7a1be341sd", 10.0, 0.001)
    tx2 = Transaction("Hx9479860c72111920999ff6645d82a45ed83ac05e", "Hx9479860c72111920999ff6645d82a45ed83ac0sd", 20.0, 0.001)
    tx_list = [tx, tx2]
    tx_collection = TransactionList(tx_list)
    formated_json = json.dumps(tx_collection.convert_to_dict(), indent=4)
    print(formated_json)
    signed_json = wallet.sign_transactions(tx_collection, password="test")
    formated_json2 = json.dumps(signed_json, indent=4)
    print(formated_json2)
    print("tx1", signature_verification.verify_transaction_signature(signed_json[0]["public_key"], signed_json[0]["signature"], tx))
    print("tx2", signature_verification.verify_transaction_signature(signed_json[1]["public_key"], signed_json[1]["signature"], tx2))
    #password_session = getpass('Password: ')
    ### SINGLE KEY TESTS - SINGLE TRANSACTION
    #priv_temp = wallet.wallet_unlock_private_key(password="test", index=1)
   # signed = wallet.sign_transaction(tx, "test", 1)
   # print('signed: ', signed)
    # “Node-side” verification (just local check for now)
    #valid = signature_verification.verify_transaction_signature(
    #    signed["public_key"], signed["signature"], tx # Tx passed as transaction
    #)
    #print("Signature valid:", valid)

def splitting_txs():
    wallet = Wallet("wallet_files/test_02_11.json")
    #tx = Transaction("Hx5d6f9590dbcb462aa6033336707ba36ad68f3dae", "Hx5d6f9590dbcb462aa6033336707ba36ad68f3123", 100.0, 0.5)
    txs_list_after_split = wallet.create_mixed_transactions('Hx5d6f9590dbcb462aa6033336707ba36ad68f3123', 100.0, 0.5)
    formated_json = json.dumps(txs_list_after_split.convert_to_dict(), indent=4)
    print(formated_json)
    signed_json = wallet.sign_transactions(txs_list_after_split, password="test")
    formated_json2 = json.dumps(signed_json, indent=4)
    print(formated_json2)
    for tx in signed_json:
        print(tx['txid'], signature_verification.verify_transaction_signature(tx['public_key'], tx['signature'], tx))

def nonce_Test():
    wallet = Wallet("wallet_files/test2510_2.json")
    wallet.update_wallet_nonce('Hx4f51ad7b16243130f59534015193980d4c8050a2', 20)

def test():
    if True:
        w = Wallet("wallet_files/wallet_k_test.json")
        entered_master_password = getpass("Enter master password to add key: ")
        
    if False:
        entry = w.wallet_add_derived_key(entered_master_password)
        print("Added entry:", entry["label"], "pub:", entry["public"]["public_key"])
        priv = w.wallet_unlock_private_key(entered_master_password, index=1)
        private_key_bytes = priv.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption()
        )
        private_key_str = base64.b64encode(private_key_bytes).decode('utf-8')
        print("Private key: ", private_key_str)
        
    if False:
        for i in range(0,10):
            entry = w.wallet_add_derived_key(entered_master_password)
          
    addres = "Hx58472357bc3a95a2513a278b0ebeac775bdc2d62"
    transaction_id = ""
    node_port =  7001
    API = f"http://localhost:{node_port}"
    LEDGER_API = f"http://localhost:{node_port}/api/balance/{addres}"
    TEST_TRANSACTION_API = f"http://localhost:{node_port}/api/tx/{transaction_id}"
    TRANSACTION_ENDPOINTS = f"http://localhost:{node_port}/api/tx"
    
    
    if False:
        print(w.get_account_balance(addres, API))
        w.execute_mixed_transaction_flow("0xGENESIS_USER1",30.0, 1.0, API, entered_master_password)
    if True:
        tx = Transaction("Hx5d40aea233b7764688d052abef3db3f4a387fa44", "Hx58472357bc3a95a2513a278b0ebeac775bdc2d62", 540, 5)
        w.send_transaction(tx, API, "test")

###################################################
##############CHECK CURRENT DIR ###################   
###################################################

if __name__ == "__main__":
    #init()
    #wallet()
    test()
    #splitting_txs()
    #nonce_Test()
    #test()


