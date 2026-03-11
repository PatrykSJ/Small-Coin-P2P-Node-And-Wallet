# wallet-app.py
# Full wallet frontend with:
#  - "Get all balances" button (with nonce sync)
#  - Persisting last-entered form values in session
#  - Real-time streaming of mixed transaction flow console output via SSE
import os
import base64
import io
import json
import traceback
import threading
import queue
import uuid
import time
import html
from transaction import Transaction
from contextlib import redirect_stdout
from flask import (
    Flask, request, render_template_string, redirect, url_for,
    flash, session, Response
)

# Ensure this file sits in the same dir as wallet.py
import wallet as wallet_module

#DEFAULT_WALLET_PATH = os.path.join(os.path.dirname(__file__), "wallet_files", "wallet.json")
DEFAULT_WALLET_PATH = os.path.join("wallet_files", "wallet.json")

app = Flask(__name__)
app.secret_key = "dev-secret-for-local-change-me"  # change for real use
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2MB limit (just in case)


# ---------- Globals for SSE/background tasks ----------
# Map run_id -> queue.Queue of strings (log lines). Background thread pushes log lines,
# SSE endpoint consumes them and yields to client.
job_queues = {}
job_locks = {}  # optional per-job lock if needed
JOB_QUEUE_TIMEOUT = 300  # seconds to wait before assuming job died/expired


# ---------- Helpers ----------
def get_wallet_instance(path=None):
    path = path or session.get('wallet_path', DEFAULT_WALLET_PATH)
    if not os.path.exists(path):
        return None
    try:
        w = wallet_module.Wallet(path)
        return w
    except Exception as e:
        print("Error loading wallet:", e)
        return None


def ensure_wallet_file(path=DEFAULT_WALLET_PATH):
    if not os.path.exists(path):
        directory = os.path.dirname(path)
        if directory and not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({}, f)


def read_wallet_json(path=None):
    path = path or session.get('wallet_path', DEFAULT_WALLET_PATH)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# Queue writer that captures writes (from redirect_stdout) and pushes them into queue
class QueueWriter:
    def __init__(self, q: queue.Queue, run_id: str):
        self.q = q
        self.run_id = run_id
        self._buffer = ""

    def write(self, s):
        # redirect_stdout may call write with partial strings; collect until newline
        if not s:
            return
        self._buffer += s
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            # push line into queue
            try:
                self.q.put(line)
            except Exception:
                pass

    def flush(self):
        # flush remaining buffer as a line if present
        if self._buffer:
            try:
                self.q.put(self._buffer)
            except Exception:
                pass
            self._buffer = ""


def cleanup_job(run_id):
    # Remove queue and lock if exist
    job_queues.pop(run_id, None)
    job_locks.pop(run_id, None)


def start_mixed_tx_background(run_id: str, path: str, password: str, tx_out: str,
                              amount: float, fee: float, node_url: str, num_inputs: int):
    """
    Background thread target that runs the mixed tx flow and writes logs to the job queue.
    On completion or error, pushes a sentinel '<<__EOF__>>' and optionally a final JSON status line.
    """
    q = job_queues.get(run_id)
    if q is None:
        # nothing to write to
        return

    writer = QueueWriter(q, run_id)
    try:
        # instantiate wallet
        w = wallet_module.Wallet(path)
    except Exception as e:
        q.put(f"Failed to open wallet: {e}")
        q.put("<<__EOF__>>")
        return

    try:
        q.put("=== Starting Mixed Transaction Flow (background) ===")
        # capture print() calls into our queue writer
        with redirect_stdout(writer):
            ok = w.execute_mixed_transaction_flow(tx_out=tx_out, amount=amount, fee=fee,
                                                  node_url=node_url, password=password, num_inputs=num_inputs)
            # ensure writer flushes leftover
            writer.flush()
        q.put(f"=== Flow finished: success={bool(ok)} ===")
        # final json-ish line for structured result (could be parsed by client if desired)
        q.put(json.dumps({"success": bool(ok)}))
    except Exception as e:
        # push exception message
        q.put(f"Exception during mixed flow: {e}")
        q.put(traceback.format_exc())
    finally:
        # send EOF sentinel so SSE client knows to stop listening if desired
        q.put("<<__EOF__>>")
        # Optionally clean up after some delay to allow client to fetch final lines
        def delayed_cleanup(rid):
            time.sleep(5)
            cleanup_job(rid)
        threading.Thread(target=delayed_cleanup, args=(run_id,), daemon=True).start()


# ---------- Templates (no uploads, local files only) ----------
BASE_HTML = """
<!doctype html>
<title>Simple Wallet Frontend (local files only)</title>
<meta charset="utf-8">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/water.css@2/out/water.css">
<style>
  /* Improve raw JSON readability */
  .raw-json { background:#0b1220;color:#e6eef6;padding:12px;border-radius:6px;font-family:monospace;white-space:pre-wrap;overflow:auto }
  .wallet-path { background:#0b1220;color:#e6eef6;padding:12px;border-radius:6px;font-family:monospace;white-space:pre-wrap;overflow:auto }
  .console-output { background:#111;color:#0f0;padding:10px;border-radius:6px;font-family:monospace;white-space:pre-wrap;overflow:auto;max-height:420px }
  .side-container { display:flex;gap:20px;align-items:flex-start; }
  .side-form { flex:1; }
  .side-console { flex:1; }
  .small { font-size:0.9rem; color:#666 }
  label { font-weight:600 }
</style>
<div style="display:flex;align-items:center;gap:12px">
  <img src="/static/hugo.png" alt="logo" style="height:150px">
  <h1 style="margin:0">HugoCoin - Wallet</h1>
</div>
<div style="display:flex;gap:12px;align-items:center">
  <div class="wallet-path">Active wallet: <strong>{{ wallet_path }}</strong></div>
  <form method="post" action="{{ url_for('change_wallet_path') }}" style="display:inline-block">
    <input name="path" placeholder="Enter wallet path or leave blank" style="width:360px" value="{{ session.get('path', '') }}">
    <button type="submit">Change</button>
  </form>
  <a href="{{ url_for('load_wallet') }}">Activate wallet by path</a>
</div>

<nav style="margin-top:8px">
  <a href="{{ url_for('index') }}">Home</a> |
  <a href="{{ url_for('create_wallet') }}">Create wallet</a> |
  <a href="{{ url_for('add_key') }}">Add new key</a> |
  <a href="{{ url_for('unlock_key') }}">Unlock key</a> |
  <a href="{{ url_for('get_balance') }}">Get balance</a> |
  <a href="{{ url_for('send_single_tx') }}">Send single tx</a> |
  <a href="{{ url_for('send_tx') }}">Send mixed tx</a>
</nav>

<hr>
{% with messages = get_flashed_messages(with_categories=true) %}
  {% if messages %}
    <ul>
    {% for category, msg in messages %}
      <li><strong>{{ category }}:</strong> {{ msg }}</li>
    {% endfor %}
    </ul>
  {% endif %}
{% endwith %}
"""

INDEX_BODY = """
<h2>Wallet overview</h2>
{% if wallet is none %}
  <p><em>No wallet found at this path. Create one or activate a local wallet file.</em></p>
{% else %}
  <h3>Keys</h3>
  <table>
    <thead><tr><th>Index</th><th>Label</th><th>Address</th><th>Algorithm</th><th>Created</th></tr></thead>
    <tbody>
    {% for k in wallet.get('keys', []) %}
      <tr>
        <td>{{ k.get('index') }}</td>
        <td>{{ k.get('label') }}</td>
        <td>{{ k.get('address') }}</td>
        <td>{{ k.get('algorithm') }}</td>
        <td>{{ k.get('created_at') }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>

  <h3>Raw wallet JSON</h3>
  <div class="raw-json">{{ wallet|tojson(indent=2) }}</div>
{% endif %}
"""

LOAD_WALLET_BODY = """
<h2>Activate local wallet</h2>
<p>Specify a path to a wallet file that already exists on this machine. No uploads are used — the file must be local and readable by the server process.</p>

<form method="post">
  <label>Wallet file path (absolute or relative to app)</label><br>
  <input name="path" value="{{ wallet_path }}" style="width:60%"><br><br>

  <button type="submit">Activate</button>
</form>
"""

CREATE_WALLET_BODY = """
<h2>Create wallet</h2>
<form method="post">
  <label>Wallet file path (relative or absolute)</label><br>
  <input name="path" value="{{ wallet_path }}" style="width:60%"><br><br>

  <label>Master password</label><br>
  <input type="password" name="password" style="width:40%"><br><br>

  <button type="submit">Create wallet</button>
</form>

<p><strong>Note</strong>: This creates a wallet file using the same initialization logic as your `wallet.py`. The form injects the password into the init routine (monkeypatch) so there is no interactive terminal prompt.</p>
"""

ADD_KEY_BODY = """
<h2>Add derived key</h2>
<form method="post">
  <label>Wallet file path</label><br>
  <input name="path" value="{{ wallet_path }}" style="width:60%"><br><br>

  <label>Master password</label><br>
  <input type="password" name="password" style="width:40%"><br><br>

  <label>Label (optional)</label><br>
  <input name="label" style="width:40%"><br><br>

  <button type="submit">Add key</button>
</form>
"""

UNLOCK_KEY_BODY = """
<h2>Unlock private key</h2>
<form method="post">
  <label>Wallet file path</label><br>
  <input name="path" value="{{ wallet_path }}" style="width:60%"><br><br>

  <label>Master password</label><br>
  <input type="password" name="password" style="width:40%"><br><br>

  <label>Key index (integer)</label><br>
  <input name="index" value="1" style="width:120px"><br><br>

  <button type="submit">Unlock</button>
</form>

{% if private_b64 %}
  <h3>Unlocked key (base64)</h3>
  <p><strong>Address:</strong> {{ address }}</p>
  <p><strong>Public (from wallet):</strong> <code>{{ public }}</code></p>
  <p><strong>Private (raw base64):</strong></p>
  <textarea style="width:80%;height:80px">{{ private_b64 }}</textarea>
{% endif %}
"""

GET_BALANCE_BODY = """
<h2>Get account balance</h2>
<form method="post">
  <label>Node base URL (e.g. http://127.0.0.1:5001)</label><br>
  <input name="node_url" style="width:60%" value="{{ session.get('node_url', 'http://127.0.0.1:4444') }}"><br><br>

  <label>Address (leave empty and click 'Get all balances' to query all addresses)</label><br>
  <input name="address" style="width:60%" value="{{ session.get('address', '') }}"><br><br>

  <label>Sync nonce with ledger?</label>
  <input type="checkbox" name="sync_nonce" value="1" {% if session.get('sync_nonce') %}checked{% endif %}><br><br>

  <button type="submit" name="single_balance" value="1">Get balance</button>
  <button type="submit" name="all_balances" value="1">Get all balances</button>
</form>

{% if balance is defined %}
  <h3>Result</h3>
  <p><strong>Balance:</strong> {{ balance }}</p>
{% endif %}
"""

SEND_TX_BODY = """
<h2>Execute mixed transaction flow</h2>
<div class="side-container">
<div class="side-form">
<form method="post">
  <label>Wallet file path</label><br>
  <input name="path" value="{{ session.get('path', wallet_path) }}" style="width:60%"><br><br>

  <label>Master password</label><br>
  <input type="password" name="password" style="width:40%"><br><br>

  <label>Destination address (tx_out)</label><br>
  <input name="tx_out" style="width:60%" value="{{ session.get('tx_out', '') }}"><br><br>

  <label>Amount (float)</label><br>
  <input name="amount" value="{{ session.get('amount', '1.0') }}" style="width:160px"><br><br>

  <label>Fee (float)</label><br>
  <input name="fee" value="{{ session.get('fee', '0.01') }}" style="width:160px"><br><br>

  <label>Node URL</label><br>
  <input name="node_url" value="{{ session.get('node_url', 'http://127.0.0.1:4444') }}" style="width:60%"><br><br>

  <label>Number of inputs (optional, leave blank to use all)</label><br>
  <input name="num_inputs" style="width:120px" value="{{ session.get('num_inputs', '') }}"><br><br>

  <button type="submit">Run mixed flow</button>
</form>
</div>

<div class="side-console">
  <h3>Console Output <span class="small">(<em>real-time</em>)</span></h3>
  <div id="console-output" class="console-output">{% if initial_console %}{{ initial_console }}{% endif %}</div>
  <script>
    // SSE based real-time streaming
    (function() {
      // if there's an active run_id in session, start listening
      var runId = "{{ run_id or '' }}";
      if (!runId) {
        return;
      }
      var es = new EventSource("{{ url_for('stream_mixed_tx') }}?run_id=" + encodeURIComponent(runId));
      var el = document.getElementById('console-output');
      function appendLine(line) {
        // Escape HTML to avoid XSS (server also escapes)
        var div = document.createElement('div');
        div.textContent = line;
        el.appendChild(div);
        // auto-scroll
        el.scrollTop = el.scrollHeight;
      }
      es.onmessage = function(e) {
        var text = e.data || '';
        if (text === '<<__EOF__>>') {
          appendLine("=== Stream ended ===");
          es.close();
          return;
        }
        appendLine(text);
      };
      es.onerror = function(e) {
        // Try to close; browser will auto-reconnect to same URL by default
        // Show an error line and close after a short delay
        appendLine("[SSE] Connection error, retrying...");
      };
    })();
  </script>
</div>

</div>

{% if result is defined %}
  <h3>Result</h3>
  <p><pre>{{ result }}</pre></p>
{% endif %}
"""

SEND_SINGLE_TX_BODY = """
<h2>Send single transaction</h2>

{% if wallet is none %}
  <p><em>No wallet loaded or wallet file not found. Create or activate a wallet first.</em></p>
{% elif not wallet.get('keys') or wallet.get('keys')|length <= 1 %}
  <p><em>No usable keys found (only verification key index=0 exists). Add a derived key first.</em></p>
{% else %}
<form method="post">
  <label>Wallet file path</label><br>
  <input name="path" value="{{ wallet_path }}" style="width:60%"><br><br>

  <label>Master password</label><br>
  <input type="password" name="password" style="width:40%"><br><br>

  <label>Sender (key from this wallet)</label><br>
  <select name="key_index" style="width:60%">
    {% for k in wallet.get('keys', []) %}
      {% if k.get('index') != 0 %}
        <option value="{{ k.get('index') }}"
          {% if selected_key_index is not none and k.get('index') == selected_key_index %}selected{% endif %}>
          idx={{ k.get('index') }} | {{ k.get('label') }} | {{ k.get('address') }}
        </option>
      {% endif %}
    {% endfor %}
  </select><br><br>

  <label>Recipient address (txout)</label><br>
  <input name="tx_out" style="width:60%" value="{{ session.get('single_tx_out', '') }}"><br><br>

  <label>Amount</label><br>
  <input name="amount" style="width:160px" value="{{ session.get('single_amount', '1.0') }}"><br><br>

  <label>Fee</label><br>
  <input name="fee" style="width:160px" value="{{ session.get('single_fee', '0.01') }}"><br><br>

  <label>Node URL</label><br>
  <input name="node_url" style="width:60%" value="{{ session.get('single_node_url', session.get('node_url', 'http://127.0.0.1:4444')) }}"><br><br>

  <button type="submit">Send transaction</button>
</form>
{% endif %}

{% if tx_summary %}
  <h3>Created & signed transaction</h3>
  <div class="raw-json">{{ tx_summary|tojson(indent=2) }}</div>
{% endif %}
"""


# ---------- Routes ----------
@app.route("/")
def index():
    # Allow overriding with ?path=... (and persist it to session)
    req_path = request.args.get("path")
    if req_path:
        session['wallet_path'] = req_path

    wallet_path = session.get('wallet_path', DEFAULT_WALLET_PATH)
    wallet_json = read_wallet_json(wallet_path)
    full = BASE_HTML + INDEX_BODY
    return render_template_string(full, wallet=wallet_json, wallet_path=wallet_path)


@app.route('/change_wallet_path', methods=['POST'])
def change_wallet_path():
    path = request.form.get('path')
    if path:
        session['wallet_path'] = path
        session['path'] = path  # persist as last-used in forms
        flash(f"Active wallet changed to {path}", "success")
    else:
        session.pop('wallet_path', None)
        flash("Active wallet reset to default", "success")
    return redirect(url_for('index'))


@app.route("/load_wallet", methods=["GET", "POST"])
def load_wallet():
    wallet_path = session.get('wallet_path', DEFAULT_WALLET_PATH)
    if request.method == "POST":
        path = request.form.get("path") or DEFAULT_WALLET_PATH
        try:
            if path:
                if os.path.exists(path):
                    session['wallet_path'] = path
                    session['path'] = path
                    flash(f"Activated wallet at {path}", "success")
                    return redirect(url_for('index'))
                else:
                    flash("Specified path does not exist or is not readable by the server", "error")
        except Exception as e:
            traceback.print_exc()
            flash(f"Failed to activate wallet: {e}", "error")

    full = BASE_HTML + LOAD_WALLET_BODY
    return render_template_string(full, wallet_path=wallet_path)


@app.route("/create_wallet", methods=["GET", "POST"])
def create_wallet():
    if request.method == "GET":
        full = BASE_HTML + CREATE_WALLET_BODY
        return render_template_string(full, wallet_path=session.get('wallet_path', DEFAULT_WALLET_PATH))
    path = request.form.get("path") or DEFAULT_WALLET_PATH
    password = request.form.get("password") or ""
    try:
        dirn = os.path.dirname(path)
        if dirn and not os.path.exists(dirn):
            os.makedirs(dirn, exist_ok=True)

        original_getpass = getattr(wallet_module, "getpass", None)
        wallet_module.getpass = lambda prompt="": password

        w = wallet_module.Wallet(path)

        if original_getpass is not None:
            wallet_module.getpass = original_getpass

        # activate the new wallet
        session['wallet_path'] = path
        session['path'] = path
        flash("Wallet created and activated", "success")
        return redirect(url_for("index", path=path))
    except Exception as e:
        try:
            if original_getpass is not None:
                wallet_module.getpass = original_getpass
        except Exception:
            pass
        traceback.print_exc()
        flash(f"Failed to create wallet: {e}", "error")
        return redirect(url_for("create_wallet"))


@app.route("/add_key", methods=["GET", "POST"])
def add_key():
    if request.method == "GET":
        full = BASE_HTML + ADD_KEY_BODY
        return render_template_string(full, wallet_path=session.get('wallet_path', DEFAULT_WALLET_PATH))
    path = request.form.get("path") or session.get('wallet_path', DEFAULT_WALLET_PATH)
    password = request.form.get("password") or ""
    label = request.form.get("label") or None
    try:
        w = wallet_module.Wallet(path)
        entry = w.wallet_add_derived_key(password, label)
        flash(f"Added key index={entry['index']} address={entry['address']}", "success")
        # keep active wallet
        session['wallet_path'] = path
        session['path'] = path
        return redirect(url_for("index", path=path))
    except Exception as e:
        traceback.print_exc()
        flash(f"Failed to add key: {e}", "error")
        return redirect(url_for("add_key"))


@app.route("/unlock_key", methods=["GET", "POST"])
def unlock_key():
    private_b64 = None
    address = None
    public = None
    if request.method == "POST":
        path = request.form.get("path") or session.get('wallet_path', DEFAULT_WALLET_PATH)
        password = request.form.get("password") or ""
        index_raw = request.form.get("index") or "1"
        try:
            index = int(index_raw)
            w = wallet_module.Wallet(path)
            priv_obj = w.wallet_unlock_private_key(password, index)
            raw = priv_obj.private_bytes(
                encoding=wallet_module.serialization.Encoding.Raw,
                format=wallet_module.serialization.PrivateFormat.Raw,
                encryption_algorithm=wallet_module.serialization.NoEncryption()
            )
            private_b64 = base64.urlsafe_b64encode(raw).decode("ascii")
            wallet_json = read_wallet_json(path)
            entry = next((k for k in wallet_json.get("keys", []) if k.get("index") == index), {})
            public = entry.get("public", {}).get("public_key")
            address = entry.get("address")
            session['wallet_path'] = path
            session['path'] = path
            flash(f"Unlocked key index={index}", "success")
        except Exception as e:
            traceback.print_exc()
            flash(f"Failed to unlock key: {e}", "error")
    full = BASE_HTML + UNLOCK_KEY_BODY
    return render_template_string(full, wallet_path=session.get('wallet_path', DEFAULT_WALLET_PATH),
                                  private_b64=private_b64, address=address, public=public)


@app.route("/get_balance", methods=["GET", "POST"])
def get_balance():
    balance = None
    if request.method == "POST":
        node_url = request.form.get("node_url") or ""
        address = request.form.get("address") or ""
        sync_nonce = request.form.get("sync_nonce") == "1"

        # Persist last-entered values
        session['node_url'] = node_url
        session['address'] = address
        session['sync_nonce'] = sync_nonce

        try:
            w = get_wallet_instance()
            if w is None:
                flash("Wallet not found - create a wallet first", "error")
                return redirect(url_for("create_wallet"))

            # If user requested all balances
            if request.form.get("all_balances"):
                # Call the wallet method. It may return a dict or a float (older implementation returns float)
                try:
                    res = w.get_balances_for_all_addresses(node_url, sync_nonce)
                    # If the wallet method returned a dict with 'total', use it; otherwise try to handle float
                    if isinstance(res, dict):
                        total = res.get("total", None)
                        if total is None:
                            total = res.get("total", 0.0)
                        balance = total
                    elif isinstance(res, (float, int)):
                        balance = float(res)
                    else:
                        # unexpected type: convert to string for debugging
                        balance = str(res)
                except Exception as e:
                    # As a fallback, iterate addresses and sum balances (slower)
                    print("Fallback: per-address summation due to error from get_balances_for_all_addresses:", e)
                    total = 0.0
                    # read wallet file directly
                    with open(session.get('wallet_path', DEFAULT_WALLET_PATH), "r", encoding="utf-8") as f:
                        wallet_json = json.load(f)
                    addrs = [k["address"] for k in wallet_json.get("keys", []) if k.get("index") != 0 and "address" in k]
                    import requests
                    for a in addrs:
                        try:
                            r = requests.get(f"{node_url}/api/balance/{a}", timeout=10)
                            if r.status_code == 200:
                                d = r.json()
                                total += float(d.get("balance", 0.0))
                                if sync_nonce and "nonce" in d:
                                    try:
                                        ledger_nonce = int(d.get("nonce", 0))
                                        w.update_wallet_nonce(a, ledger_nonce)
                                    except Exception as ex:
                                        print("Nonce update failed (fallback):", ex)
                        except Exception as ex:
                            print(f"Failed to fetch balance for {a} during fallback: {ex}")
                    balance = float(total)

                flash("Total balance retrieved for all addresses", "success")

            else:
                # Single address balance
                balance = w.get_account_balance(address, node_url, nonce=sync_nonce)
                flash("Balance retrieved", "success")

            session['wallet_path'] = session.get('wallet_path', DEFAULT_WALLET_PATH)
        except Exception as e:
            traceback.print_exc()
            flash(f"Failed to get balance: {e}", "error")
    full = BASE_HTML + GET_BALANCE_BODY
    return render_template_string(full, wallet_path=session.get('wallet_path', DEFAULT_WALLET_PATH), balance=balance)


@app.route("/send_tx", methods=["GET", "POST"])
def send_tx():
    """
    When user submits the form (POST), we start a background thread that runs the mixed flow.
    We create a run_id, create a queue, start the background thread, persist form fields to session,
    and then render the send_tx page with the run_id present so client-side JS opens an EventSource
    to stream logs from /stream_mixed_tx?run_id=...
    """
    result = None
    initial_console = None
    run_id = request.args.get("run_id") or None

    if request.method == "POST":
        path = request.form.get("path") or session.get('wallet_path', DEFAULT_WALLET_PATH)
        password = request.form.get("password") or ""
        tx_out = request.form.get("tx_out") or ""
        node_url = request.form.get("node_url") or ""
        num_inputs_raw = request.form.get("num_inputs") or ""
        num_inputs = int(num_inputs_raw) if num_inputs_raw else None

        # persist last-entered values into session
        session['path'] = path
        session['tx_out'] = tx_out
        session['node_url'] = node_url
        session['num_inputs'] = num_inputs_raw
        # amount/fee persisted as strings to avoid float formatting surprises
        session['amount'] = request.form.get("amount") or ""
        session['fee'] = request.form.get("fee") or ""

        try:
            amount = float(request.form.get("amount") or 0.0)
            fee = float(request.form.get("fee") or 0.0)
        except ValueError:
            flash("Amount and fee must be numbers", "error")
            return redirect(url_for("send_tx"))

        # create a run id and a queue for streaming
        new_run_id = str(uuid.uuid4())
        q = queue.Queue()
        job_queues[new_run_id] = q
        job_locks[new_run_id] = threading.Lock()

        # start background thread that pushes lines to queue
        t = threading.Thread(target=start_mixed_tx_background, args=(
            new_run_id, path, password, tx_out, amount, fee, node_url, num_inputs
        ))
        t.daemon = True
        t.start()

        # redirect to same page but with run_id param so client JS will connect to SSE
        return redirect(url_for("send_tx", run_id=new_run_id))

    # When rendering page (GET), if there is a run_id param, try to show any already-collected lines
    if run_id:
        q = job_queues.get(run_id)
        if q:
            # drain up to some reasonable number of queued lines for initial display
            lines = []
            try:
                while True:
                    line = q.get_nowait()
                    if line == "<<__EOF__>>":
                        lines.append("<<__EOF__>>")
                        break
                    lines.append(line)
            except queue.Empty:
                pass
            # Put drained lines back to front by creating a new queue with them then extending with old contents
            # (But simplest approach: we will re-put drained lines back so SSE will still send them;
            #  however that complicates ordering if background continues to put; instead show drained lines once
            #  and let SSE deliver remaining lines — so we keep drained lines consumed.)
            # For robustness, we'll present whatever we drained as initial_console.
            if lines:
                # escape HTML on server side
                initial_console = "\n".join([html.escape(str(l)) for l in lines if l != "<<__EOF__>>"])
                if lines and lines[-1] == "<<__EOF__>>":
                    # job finished — we can cleanup soon
                    cleanup_job(run_id)

    full = BASE_HTML + SEND_TX_BODY
    return render_template_string(full,
                                  wallet_path=session.get('wallet_path', DEFAULT_WALLET_PATH),
                                  result=json.dumps(result, indent=2) if result else None,
                                  initial_console=initial_console,
                                  run_id=run_id)


@app.route("/send_single_tx", methods=["GET", "POST"])
def send_single_tx():
    wallet_path = session.get('wallet_path', DEFAULT_WALLET_PATH)

    # Allow overriding wallet path from the form
    if request.method == "POST":
        form_path = request.form.get("path")
        if form_path:
            wallet_path = form_path
            session['wallet_path'] = form_path
            session['path'] = form_path

    wallet_json = read_wallet_json(wallet_path)
    tx_summary = None
    selected_key_index = None

    if request.method == "POST":
        password = request.form.get("password") or ""
        node_url = request.form.get("node_url") or ""
        tx_out = request.form.get("tx_out") or ""
        key_index_raw = request.form.get("key_index") or ""
        amount_raw = request.form.get("amount") or "0"
        fee_raw = request.form.get("fee") or "0"

        # Persist last-entered form values for convenience
        session['single_tx_out'] = tx_out
        session['single_amount'] = amount_raw
        session['single_fee'] = fee_raw
        session['single_node_url'] = node_url

        try:
            key_index = int(key_index_raw)
            selected_key_index = key_index
        except ValueError:
            flash("Key index must be an integer", "error")
            full = BASE_HTML + SEND_SINGLE_TX_BODY
            return render_template_string(
                full,
                wallet=wallet_json,
                wallet_path=wallet_path,
                tx_summary=tx_summary,
                selected_key_index=selected_key_index,
            )

        try:
            amount = float(amount_raw)
            fee = float(fee_raw)
        except ValueError:
            flash("Amount and fee must be numbers", "error")
            full = BASE_HTML + SEND_SINGLE_TX_BODY
            return render_template_string(
                full,
                wallet=wallet_json,
                wallet_path=wallet_path,
                tx_summary=tx_summary,
                selected_key_index=selected_key_index,
            )

        if not wallet_json:
            flash("Wallet file not found. Create or load a wallet first.", "error")
            full = BASE_HTML + SEND_SINGLE_TX_BODY
            return render_template_string(
                full,
                wallet=wallet_json,
                wallet_path=wallet_path,
                tx_summary=tx_summary,
                selected_key_index=selected_key_index,
            )

        # Find the sender entry by key index
        sender_entry = None
        for k in wallet_json.get("keys", []):
            if k.get("index") == key_index:
                sender_entry = k
                break

        if sender_entry is None:
            flash(f"Key with index {key_index} not found in wallet", "error")
            full = BASE_HTML + SEND_SINGLE_TX_BODY
            return render_template_string(
                full,
                wallet=wallet_json,
                wallet_path=wallet_path,
                tx_summary=tx_summary,
                selected_key_index=selected_key_index,
            )

        sender_address = sender_entry.get("address")
        if not sender_address:
            flash("Selected key has no address field", "error")
            full = BASE_HTML + SEND_SINGLE_TX_BODY
            return render_template_string(
                full,
                wallet=wallet_json,
                wallet_path=wallet_path,
                tx_summary=tx_summary,
                selected_key_index=selected_key_index,
            )

        if not tx_out:
            flash("Recipient (txout) address is required", "error")
            full = BASE_HTML + SEND_SINGLE_TX_BODY
            return render_template_string(
                full,
                wallet=wallet_json,
                wallet_path=wallet_path,
                tx_summary=tx_summary,
                selected_key_index=selected_key_index,
            )

        try:
            # Build a single Transaction. Nonce will be set inside sign_transaction()
            tx = Transaction(
                txin=sender_address,
                txout=tx_out,
                amount=amount,
                fee=fee,
                nonce=None
            )

            # Use your existing wallet + send_transaction() method
            w = wallet_module.Wallet(wallet_path)
            w.send_transaction(tx, node_url, password, key_index)

            # After signing, tx.nonce and tx.txid are updated in sign_transaction()
            tx_summary = tx.convert_to_dict()
            tx_summary["txid"] = tx.txid

            flash(f"Transaction {tx.txid} submitted (check node for status)", "success")
        except Exception as e:
            import traceback
            traceback.print_exc()
            flash(f"Failed to send transaction: {e}", "error")

    full = BASE_HTML + SEND_SINGLE_TX_BODY
    return render_template_string(
        full,
        wallet=wallet_json,
        wallet_path=wallet_path,
        tx_summary=tx_summary,
        selected_key_index=selected_key_index,
    )



@app.route('/stream_mixed_tx')
def stream_mixed_tx():
    """
    SSE endpoint. Query param: run_id.
    Streams 'data: <line>\n\n' for each queued line. When sentinel '<<__EOF__>>' is seen, send it and close.
    """
    run_id = request.args.get("run_id")
    if not run_id:
        return "Missing run_id", 400
    q = job_queues.get(run_id)
    if q is None:
        return "Unknown run_id or job expired", 404

    def event_stream(q_local: queue.Queue, run_id_local: str):
        last_activity = time.time()
        try:
            while True:
                try:
                    # Wait for a line; if it times out, check for overall inactivity and exit if stale
                    line = q_local.get(timeout=1.0)
                except queue.Empty:
                    # check for inactivity
                    if time.time() - last_activity > JOB_QUEUE_TIMEOUT:
                        yield f"data: [server] job timeout/expired\n\n"
                        break
                    continue

                last_activity = time.time()
                if line is None:
                    # ignore
                    continue
                # Each line is sent as one SSE message.
                # Escape newlines and ensure the browser shows them.
                safe_line = str(line)
                yield f"data: {safe_line}\n\n"
                if line == "<<__EOF__>>":
                    break
        finally:
            # final cleanup attempt (but background thread also schedules cleanup)
            cleanup_job(run_id_local)

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no"  # for some reverse proxies to disable buffering
    }
    return Response(event_stream(q, run_id), mimetype="text/event-stream", headers=headers)


# ---------- run ----------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Wallet web frontend")
    parser.add_argument(
        "--wallet-path",
        help="Path to default wallet.json file for this instance",
        default=DEFAULT_WALLET_PATH,
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5000,
        help="Port to bind HTTP server on (use different ports for multiple instances)",
    )

    args = parser.parse_args()

    # Override the module-level default so all routes that use DEFAULT_WALLET_PATH
    # will see the per-instance path.
    DEFAULT_WALLET_PATH = args.wallet_path
    app.config["SESSION_COOKIE_NAME"] = f"hugowallet_session_{args.port}"

    ensure_wallet_file(DEFAULT_WALLET_PATH)
    app.run(host="0.0.0.0", port=args.port, debug=True)