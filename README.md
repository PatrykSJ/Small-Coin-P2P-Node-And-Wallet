# Blockchain – custom cryptocurrency

A simple blockchain based on **proof-of-work**, with a **peer-to-peer** network, full block and transaction validation, support for **forks/orphan blocks**, and a **secure wallet** (Argon2id, HKDF, Ed25519, ChaCha20-Poly1305).

The project was developed in Python as part of the Applied Cryptography course at the Warsaw University of Technology.

## Main features

- **P2P Network**
  - Nodes connect over TCP using a simple JSON-line protocol (HELLO, HEALTHCHECK, CHAIN_HEAD, GET_BLOCKS_FROM, BLOCK, TX).
  - Healthcheck mechanisms, rate-limits, and a penalty system for misbehaving peers (banning malicious peers).

- **Blockchain and PoW consensus**
  - A block contains, among others: `block_number`, `version`, `timestamp`, `prev_hash`, `merkle_root`, `nBits`, a transaction list, `nonce`, and `hash`.
  - Proof-of-Work based on hashing the header and comparing it against the target derived from `nBits`.
  - Best chain selection based on **total work**, with full fork and orphan-block handling (reorgs).

- **Transactions and ledger (account model)**
  - Transactions follow an account model: `txin` (sender address), `txout` (recipient), `amount`, `fee`, `nonce`, `public_key`, `signature`, `txid`.
  - Validation:
    - field format and types, timestamp correctness, no future dates, correct `txid` hash;
    - checking address format (`Hx...` + checksum) and that `txin` matches the public key;
    - Ed25519 signature verification on the canonical transaction message;
    - double-spend protection (balance + nonce in the ledger).
  - The ledger maintains balances and nonces for each address, supports apply/rollback of blocks, and handles **coinbase reward maturity (COINBASE_MATURITY)**.

- **Blocks and miner reward**
  - The first transaction in a block is the **coinbase**, creating new coins.
  - Validation checks that there is exactly one coinbase (except in the genesis block), it is at position 0, and does not exceed `BASE_REWARD + fees`.

- **Mempool**
  - An asynchronous worker validates incoming transactions (pending → ok/invalid queue).
  - Storage of pending/ok/invalid transactions, clearing included transactions, requeuing transactions from abandoned blocks after reorgs.

- **Fork and invalid block handling**
  - A dedicated `Inbox` module validates blocks (structure, Merkle root, PoW, version, difficulty, timestamp) and their transactions before they reach the `Chain`.
  - Chain reorganizations trigger ledger rollback and mempool revalidation.

- **Secure wallet**
  - User password + salt → **master key** using **Argon2id** (per OWASP recommendations).
  - Deterministic seeds for subsequent keys are derived from the master key using **HKDF (SHA-256)**.
  - Signing keys use **Ed25519**; addresses use `Hx` + the last 20 bytes of SHA3-256 of the public key.
  - Private keys are encrypted using **ChaCha20-Poly1305** (AEAD, associated data bound to key index).
  - The wallet supports multiple addresses, internal transfers between owned addresses, and **transaction “mixing”** by splitting amounts across multiple inputs from different addresses.

- **Node HTTP API**
  - The wallet communicates with the node via a REST API:
    - `POST /api/tx` – submit a signed transaction,
    - `GET /api/tx/status/{txid}` – check status and finality,
    - `GET /api/balance/{address}` – balance of a single address,
    - `POST /api/balances` – balances of multiple addresses.

## Project structure

- `config.py` – network parameters (P2P/HTTP ports, block reward, difficulty, coinbase maturity, rate-limit/ban thresholds).
- `miner.py` – block and transaction structure definitions, Merkle root calculation, header hashing, PoW validation, block mining function.
- `chain.py` – chain DAG structure, best-tip selection by total work, management of main/stale/orphan blocks, DAG export.
- `ledger.py` – balance and nonce logic, state-based transaction validation, apply/rollback of blocks, confirmed balance computation (with COINBASE_MATURITY).
- `mempool.py` – receiving and asynchronously validating transactions, status tracking (`pending/ok/invalid`), requeueing after reorgs.
- `inbox.py` – block queue and validator for network blocks, integration with `Chain`, reporting invalid blocks to the ban system.
- `peers.py` – peer management (connections, healthcheck, chain synchronization, rate-limiting, banning).
- `mining_ctrl.py` – mining controller: automatic mining, reacting to new blocks, handling reorganizations (rollback + mempool revalidation).
- `wallet.py` – wallet implementation and tools for key creation, balance checks, transaction mixing, and node API communication.

(The repository also contains a `Dockerfile` for running the node in containers—e.g., multiple instances for testing forks and network behaviour.)

# Collaborators

Collaborators for this repository include:
* Patryk Jankowicz ([GitHub](https://github.com/PatrykSJ)), Warsaw University of Technology
* Jan Walczak ([GitHub](https://github.com/JanWalczak)), Warsaw University of Technology


