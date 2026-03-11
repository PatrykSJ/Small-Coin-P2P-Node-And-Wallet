from typing import Tuple
from flask import Flask, request, jsonify, Response
from . import config
import socket

def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    finally:
        s.close()
    return ip

def parse_target(s: str, default_port: int) -> Tuple[str, int]:
    s = s.strip()
    if not s:
        raise ValueError("pusty adres")
    if ":" in s:
        host, p = s.rsplit(":", 1)
        return host.strip(), int(p.strip())
    return s, default_port

MY_IP = get_local_ip()

DASHBOARD_HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>P2P Node</title>
  <script src="https://unpkg.com/d3@7"></script>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 1200px; margin: 2rem auto; padding: 0 1rem; }
    h1 { margin-bottom: .25rem; }
    code { background: #f5f5f5; padding: .2rem .4rem; border-radius: .25rem; }
    .card { border: 1px solid #ddd; border-radius: 12px; padding: 1rem; margin: 1rem 0; }
    .row { display: flex; gap: 1rem; flex-wrap: wrap; align-items: center; }
    input[type=text] { padding: .5rem; width: 280px; }
    button { padding: .5rem .8rem; cursor: pointer; }
    table { border-collapse: collapse; width: 100%; }
    th, td { text-align: left; padding: .5rem; border-bottom: 1px solid #eee; vertical-align: top;}
    .muted { color: #666; }
    .pill { display: inline-block; padding: .1rem .5rem; border-radius: 999px; background: #f0f0f0; font-size: .85rem; }
    pre { white-space: pre-wrap; word-break: break-word; }

    .carousel { display: flex; overflow-x: auto; gap: .75rem; scroll-snap-type: x mandatory; padding: .5rem 0; }
    .carousel::-webkit-scrollbar { height: 8px; }
    .carousel::-webkit-scrollbar-thumb { background: #ddd; border-radius: 8px; }
    .card-sm { min-width: 260px; border: 1px solid #eee; border-radius: 12px; padding: .75rem; scroll-snap-align: start; background: #fff; }
    .card-sm .meta { color: #666; font-size: .8rem; margin-bottom: .35rem; }
    pre.json {
      margin: .5rem 0 0;
      max-height: 10rem;
      overflow: auto;
      background: #fafafa;
      padding: .75rem;
      border-radius: 10px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      font-size: .9rem;
      line-height: 1.25rem;
    }

    .cols3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: .75rem; margin-top: .75rem; }
    @media (max-width: 1100px) { .cols3 { grid-template-columns: 1fr; } }
    .col { border: 1px solid #eee; border-radius: 12px; padding: .75rem; background: #fff; }
    .col h4 { margin: 0 0 .5rem 0; }
    .list { border: 1px dashed #e5e5e5; border-radius: 10px; padding: .5rem; height: 220px; overflow: auto; background: #fafafa; }
    .tx { border: 1px solid #eaeaea; border-radius: 10px; padding: .5rem .6rem; background: #fff; margin-bottom: .5rem; }
    .tx .meta { color: #666; font-size: .8rem; margin-bottom: .25rem; }

    .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
    @media (max-width: 900px) { .grid2 { grid-template-columns: 1fr; } }

    #dagWrap {
      margin-top: .5rem; 
      padding-left: 20px;
      padding-right: 20px;
    }
    #dagChart { width: 100%; height: 420px; border: 1px solid #eee; border-radius: 12px; background:#fff; }
    .legend { font-size: .9rem; display:flex; gap:1rem; align-items:center; flex-wrap:wrap; }
    .dot { display:inline-block; width:10px; height:10px; border-radius:999px; margin-right:.4rem; }
    .c-main{ background:#16a085; }
    .c-stale{ background:#95a5a6; }
    .c-orphan{ background:#e67e22; }
    .pill.badge { background:#f5f5f5; border:1px solid #eee; }
    line.link { stroke:#cfd3d6; stroke-opacity:.8; stroke-width:1.2px; }
    g.node circle { stroke:#3331; stroke-width:1.2px; }
    g.node text { font-size: 11px; fill: #333; pointer-events:none; }

    #ledgerWrap { max-height: 300px; overflow: auto; background: #fafafa; border:1px dashed #e5e5e5; border-radius:10px; padding:.5rem; }
    #ledgerTbl th { position: sticky; top: 0; background: #fff; }
    #ledgerTbl td, #ledgerTbl th { font-variant-numeric: tabular-nums; }
  </style>
</head>
<body>
  <h1>P2P Node</h1>
  <div class="muted" id="myaddr"></div>

  <div class="card">
    <h3>Akcje globalne</h3>
    <div class="row">
      <input id="peerInput" type="text" placeholder="host[:port]" />
      <button onclick="addPeer()">Połącz</button>
      <button onclick="pingBroadcast()">Broadcast PING</button>
      <button id="mineBtn" onclick="mineBlock()">Kop blok</button>
      <button id="autoBtn" onclick="toggleAuto()">Auto: OFF</button>
      <span class="muted">miner: <code id="minerAddr"></code>, diff=<code id="diff"></code></span>
      <span id="miningState" class="pill">idle</span>
    </div>
    <div class="row muted" id="connectMsg"></div>

    <div id="carouselWrap" class="card" style="margin-top: .5rem;">
      <div class="row" style="justify-content: space-between;">
        <h3 style="margin:0;">Szybki podgląd</h3>
        <div class="nav">
          <button onclick="carouselPrev()">‹</button>
          <button onclick="carouselNext()">›</button>
        </div>
      </div>
      <div id="carousel" class="carousel"></div>

      <div class="cols3">
        <div class="col">
          <h4>Pending</h4>
          <div id="mempoolPending" class="list"></div>
        </div>
        <div class="col">
          <h4>OK</h4>
          <div id="mempoolOk" class="list"></div>
        </div>
        <div class="col">
          <h4>Invalid</h4>
          <div id="mempoolInvalid" class="list"></div>
        </div>
      </div>
    </div>
  </div>

  <div id="dagWrap" class="card">
    <h3>Forki / DAG (główna gałąź, stale i orphan)</h3>
    <div class="row">
      <span class="pill badge">tip: <code id="dagTip">–</code></span>
      <span class="pill badge">height: <code id="dagHeight">–</code></span>
      <span class="pill badge">main: <code id="dagCntMain">0</code></span>
      <span class="pill badge">stale: <code id="dagCntStale">0</code></span>
      <span class="pill badge">orphan: <code id="dagCntOrphan">0</code></span>
      <button onclick="refreshDag()">Odśwież DAG</button>
    </div>
    <div class="legend" style="margin:.5rem 0 1rem;">
      <span><span class="dot c-main"></span>main</span>
      <span><span class="dot c-stale"></span>stale</span>
      <span><span class="dot c-orphan"></span>orphan (bez znanego rodzica)</span>
    </div>
    <svg id="dagChart"></svg>
  </div>

  <div class="card">
    <h3>Peery</h3>
    <table id="peersTbl">
      <thead><tr><th>Adres</th><th>Kierunek</th><th>last_seen</th><th>Akcje</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>

  <div class="grid2">
    <div class="card">
      <h3>Inbox (odebrane DATA)</h3>
      <table id="inboxTbl">
        <thead><tr><th>czas</th><th>peer</th><th>payload</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>

    <div class="card">
      <h3>Ledger (saldo)</h3>
      <div id="ledgerWrap">
        <table id="ledgerTbl">
          <thead>
            <tr><th>address</th><th>amount</th><th>nonce</th></tr>
          </thead>
          <tbody id="ledgerTbody"></tbody>
        </table>
      </div>
    </div>
  </div>

<script>
let lastHead = null;
const DAG_LAST = 200;

function escapeHtml(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
function renderTxList(rootId, items){
  const root=document.getElementById(rootId);root.innerHTML='';
  for(const it of (items||[])){
    const el=document.createElement('div');el.className='tx';
    const pretty=escapeHtml(JSON.stringify(it.tx||it.payload||it,null,2));
    el.innerHTML=`<div class="meta">${it.time||''}${it.source?(' • <code>'+escapeHtml(it.source)+'</code>'):''}</div>
                  <pre style="margin:0;max-height:120px;overflow:auto">${pretty}</pre>`;
    root.appendChild(el);
  }
}

async function loadCarousel(){
  const items=await api('/api/carousel');
  const root=document.getElementById('carousel');root.innerHTML='';
  const arr=Array.isArray(items)?items:[items];
  arr.forEach((item,idx)=>{
    const el=document.createElement('div');el.className='card-sm';
    const pretty=escapeHtml(JSON.stringify(item,null,2));
    el.innerHTML=`<div class="meta">element #${idx+1}</div><pre class="json">${pretty}</pre>`;
    root.appendChild(el);
  });
}

function carouselNext(){document.getElementById('carousel').scrollBy({left:300,behavior:'smooth'})}
function carouselPrev(){document.getElementById('carousel').scrollBy({left:-300,behavior:'smooth'})}

async function api(path,opts={}){const r=await fetch(path,Object.assign({headers:{"Content-Type":"application/json"}},opts));return r.json();}

async function refreshMining(){
  const st=await api('/api/mining/status');
  document.getElementById('autoBtn').textContent = 'Auto: ' + (st.auto ? 'ON' : 'OFF');
  const pill=document.getElementById('miningState');
  if(st.busy){ pill.textContent='kopanie...'; } else { pill.textContent='idle'; }
  document.getElementById('mineBtn').disabled = !!st.busy;
}

async function refreshLedger(){
  const rows = await api('/api/ledger/all');
  const tb = document.getElementById('ledgerTbody');
  tb.innerHTML = '';
  rows.sort((a,b) => (b.balance||0) - (a.balance||0));
  for(const r of rows){
    const tr = document.createElement('tr');
    tr.innerHTML = `<td><code>${escapeHtml(r.address)}</code></td>
                    <td>${(r.balance ?? 0).toFixed(9)}</td>
                    <td>${r.nonce ?? 0}</td>`;
    tb.appendChild(tr);
  }
}

async function refresh(){
  const info=await api('/api/info');
  document.getElementById('myaddr').textContent='Mój adres: '+info.my_addr+' (hostname: '+info.hostname+')';
  document.getElementById('minerAddr').textContent=info.miner_address||'';
  document.getElementById('diff').textContent=info.difficulty||'';

  await refreshMining();

  const peers=await api('/api/peers');
  const tb=document.querySelector('#peersTbl tbody');tb.innerHTML='';
  for(const p of peers){
    const tr=document.createElement('tr');
    tr.innerHTML=`<td><code>${p.addr}</code></td>
                  <td><span class="pill">${p.direction}</span></td>
                  <td>${p.last_seen_s}s ago</td>
                  <td><button onclick="sendPing('${p.addr}')">Ping</button>
                      <button onclick="disconnectPeer('${p.addr}')">Disconnect</button></td>`;
    tb.appendChild(tr);
  }

  const inbox=await api('/api/inbox');
  const ib=document.querySelector('#inboxTbl tbody');ib.innerHTML='';
  for(const item of inbox){
    const tr=document.createElement('tr');
    tr.innerHTML=`<td>${item.time}</td><td><code>${item.peer}</code></td>
                  <td><pre style="margin:0;max-height:8rem;overflow:auto">${escapeHtml(JSON.stringify(item.payload,null,2))}</pre></td>`;
    ib.appendChild(tr);
  }

  await loadCarousel();

  const [pending, ok, invalid] = await Promise.all([
    api('/api/mempool/pending'),
    api('/api/mempool/ok'),
    api('/api/mempool/invalid')
  ]);
  renderTxList('mempoolPending', pending);
  renderTxList('mempoolOk', ok);
  renderTxList('mempoolInvalid', invalid);

  await refreshLedger();
}

async function pollHead(){
  try{
    const head=await api('/api/chain/head');
    const cur=(head&&head.hash)?head.hash:null;
    if(cur && cur!==lastHead){
      lastHead=cur;
      await refresh();
      await refreshDag();
    }
  }catch(e){}
}

async function mineBlock(){
  const res=await fetch('/api/mine',{method:'POST'});
  await res.json();
  setTimeout(()=>{ refresh(); refreshDag(); }, 300);
}

async function toggleAuto(){
  const st=await api('/api/mining/status');
  const next = !st.auto;
  await fetch('/api/mining/auto',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enable:next})});
  setTimeout(refresh, 200);
}

async function addPeer(){const inp=document.getElementById('peerInput');const v=inp.value.trim();if(!v)return;
  document.getElementById('connectMsg').textContent='Łączenie...';
  const res=await api('/api/connect',{method:'POST',body:JSON.stringify({target:v})});
  document.getElementById('connectMsg').textContent=res.ok?'OK':'Błąd: '+(res.error||''); inp.value=''; setTimeout(()=>{ refresh(); refreshDag(); },500);
}
async function pingBroadcast(){await api('/api/ping_broadcast',{method:'POST'});setTimeout(refresh,300);}
async function sendPing(addr){await api('/api/ping',{method:'POST',body:JSON.stringify({addr})});setTimeout(refresh,300);}
async function disconnectPeer(addr){await api('/api/disconnect',{method:'POST',body:JSON.stringify({addr})});setTimeout(refresh,300);}

function dagColor(status){
  if(status==='main') return '#16a085';
  if(status==='stale') return '#95a5a6';
  return '#e67e22';
}
function trimHash(h){ if(!h) return '–'; return h.slice(0,6)+'…'+h.slice(-4); }

async function refreshDag(){
  const data = await api('/api/dag?last='+DAG_LAST);

  document.getElementById('dagTip').textContent = data.meta.best_tip ? trimHash(data.meta.best_tip) : '–';
  document.getElementById('dagHeight').textContent = (data.meta.best_tip_height ?? '–');
  document.getElementById('dagCntMain').textContent = data.meta.counts.main;
  document.getElementById('dagCntStale').textContent = data.meta.counts.stale;
  document.getElementById('dagCntOrphan').textContent = data.meta.counts.orphan;

  const svg = d3.select('#dagChart');
  const width = svg.node().clientWidth;
  const height = svg.node().clientHeight;
  svg.selectAll('*').remove();

  const nodes = data.nodes.map(d=>Object.create(d));
  const links = data.edges.map(d=>Object.create(d));

  const xByHeight = d3.scaleLinear()
    .domain(d3.extent(nodes, d => d.height < 0 ? 0 : d.height))
    .range([60, width-60]);

  const groups = d3.groups(nodes.filter(n=>n.height>=0), d=>d.height).sort((a,b)=>a[0]-b[0]);
  const rowStep = 22, topPad = 16;
  for (const [, group] of groups) {
    group.forEach((n,i)=>{ n.x = xByHeight(n.height); n.y = topPad + i*rowStep; });
  }
  const orphans = nodes.filter(n=>n.height<0);
  orphans.forEach((n,i)=>{ n.x = width - 40; n.y = topPad + i*rowStep; });

  const byId = new Map(nodes.map(n=>[n.id, n]));
  const link = svg.append('g').selectAll('line')
    .data(links).join('line').attr('class','link')
    .attr('x1', d=>byId.get(d.source).x).attr('y1', d=>byId.get(d.source).y)
    .attr('x2', d=>byId.get(d.target).x).attr('y2', d=>byId.get(d.target).y);

  const node = svg.append('g').selectAll('g')
    .data(nodes).join('g').attr('class', d=>'node '+d.status)
    .attr('transform', d=>`translate(${d.x},${d.y})`);

  node.append('circle')
      .attr('r', d=> d.status==='main' ? 7.5 : 6)
      .attr('fill', d=> dagColor(d.status));

  if(nodes.length <= 300){
    node.append('text').attr('x', 10).attr('y', 4)
      .text(d=> `${trimHash(d.hash)} [${d.height}]`);
  }

  node.append('title')
      .text(d=> `hash: ${d.hash}\nprev: ${d.prev_hash}\nheight: ${d.height}\nwork: ${d.total_work}\nstatus: ${d.status}`);
}

setInterval(refresh,15000);
setInterval(pollHead,3000);
setInterval(refreshMining,3000);

refresh();
pollHead();
refreshDag();
</script>
</body>
</html>
"""

def create_app(peers, inbox, mempool, ledger, miner_ctrl):
    app = Flask(__name__)

    @app.get("/")
    def index():
        return Response(DASHBOARD_HTML, mimetype="text/html")

    @app.get("/api/info")
    def api_info():
        return jsonify({
            "hostname": socket.gethostname(),
            "my_addr": f"{MY_IP}:{config.P2P_PORT}",
            "miner_address": config.MINER_ADDRESS,
            "difficulty": f"0x{config.NBITS:08x}",
            "nBits": config.NBITS
        })

    @app.get("/api/mining/status")
    def api_mining_status():
        return jsonify(miner_ctrl.status())

    @app.post("/api/mining/auto")
    def api_mining_auto():
        data = request.get_json(silent=True) or {}
        enable = bool(data.get("enable", False))
        miner_ctrl.set_auto(enable)
        return jsonify(miner_ctrl.status())

    @app.post("/api/mine")
    def api_mine():
        started = miner_ctrl.mine_once()
        return jsonify({"ok": True, "started": started}), (202 if started else 200)

    @app.get("/api/peers")
    def api_peers():
        return jsonify(peers.status_lines())

    @app.get("/api/inbox")
    def api_inbox():
        return jsonify(inbox.list())

    @app.post("/api/connect")
    def api_connect():
        data = (request.get_json(silent=True) or {})
        target = str(data.get("target", "")).strip()
        if not target:
            return jsonify({"ok": False, "error": "brak target"}), 400
        try:
            host, port = parse_target(target, config.P2P_PORT)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        peer = peers.connect(host, port)
        return jsonify({"ok": bool(peer), "peer": peer})

    @app.post("/api/ping_broadcast")
    def api_ping_broadcast():
        peers.broadcast_json({"type": "PING"})
        return jsonify({"ok": True, "sent": "ping-broadcast"})

    @app.post("/api/ping")
    def api_ping():
        data = (request.get_json(silent=True) or {})
        addr = str(data.get("addr", "")).strip()
        if not addr:
            return jsonify({"ok": False, "error": "brak addr"}), 400
        ok = peers.send_json(addr, {"type": "PING"})
        return jsonify({"ok": ok, "addr": addr})

    @app.post("/api/disconnect")
    def api_disconnect():
        data = (request.get_json(silent=True) or {})
        addr = str(data.get("addr", "")).strip()
        if not addr:
            return jsonify({"ok": False, "error": "brak addr"}), 400
        peers.remove(addr)
        return jsonify({"ok": True, "addr": addr})

    @app.get("/api/carousel")
    def api_carousel():
        return jsonify(inbox.getBlockChain())

    @app.get("/api/mempool/pending")
    def api_mempool_pending():
        return jsonify(mempool.list_pending())

    @app.get("/api/mempool/ok")
    def api_mempool_ok():
        return jsonify(mempool.list_ok())

    @app.get("/api/mempool/invalid")
    def api_mempool_invalid():
        return jsonify(mempool.list_invalid())

    @app.get("/api/chain/head")
    def api_chain_head():
        bc = inbox.getBlockChain()
        if not bc:
            return jsonify({"height": -1, "hash": None})
        last = bc[-1]
        height = len(bc) - 1
        return jsonify({"height": height, "hash": last.get("hash")})

    @app.get("/api/ledger")
    def api_ledger():
        return jsonify(ledger.balances())

    @app.get("/api/ledger/all")
    def api_ledger_all():
        bals = ledger.balances()
        out = []
        for addr, val in (bals.items() if isinstance(bals, dict) else []):
            if isinstance(val, dict):
                bal = float(val.get("balance", 0.0))
                n   = int(val.get("nonce", 0))
            else:
                bal = float(val or 0.0)
                n   = int(getattr(ledger, "get_nonce")(addr))
            out.append({"address": addr, "balance": bal, "nonce": n})
        if not out and hasattr(ledger, "get_all_accounts"):
            for addr in ledger.get_all_accounts():
                out.append({"address": addr, "balance": float(ledger.get_balance(addr)), "nonce": int(ledger.get_nonce(addr))})
        return jsonify(out)

    @app.post("/api/tx")
    def api_tx():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "bad-json"}), 400
        txid = str(data.get("Hash") or data.get("txid") or "")
        if txid and mempool.contains(txid):
            return jsonify({"ok": True, "duplicated": True}), 200
        mempool.add("user", data)
        return jsonify({"ok": True, "duplicated": False}), 200

    @app.get("/api/balance/<path:addr>")
    def api_balance_path(addr):
        addr = (addr or "").strip()
        return jsonify({
            "address": addr,
            "balance": ledger.get_confirmed_balance(addr),
            "nonce":   ledger.get_nonce(addr),
        })
        
    @app.post("/api/balances")
    def api_balances():
        try:
            data = request.get_json(force=True)
            addresses = data.get("addresses", [])
            if not isinstance(addresses, list):
                return jsonify({"error": "addresses must be a list"}), 400

            results = []
            for addr in addresses:
                addr = (addr or "").strip()
                results.append({
                    "address": addr,
                    "balance": ledger.get_balance(addr),
                    "nonce":   ledger.get_nonce(addr),
                })

            return jsonify(results)

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.get("/api/tx/status/<path:txid>")
    def api_tx_status_path(txid):
        txid = (txid or "").strip()
        if not txid:
            return jsonify({"ok": False, "error": "no-txid"}), 400
        return _tx_status_payload(txid)

    def _tx_status_payload(txid: str):
        # mempool
        mp_state = mempool.state_of(txid) if hasattr(mempool, "state_of") else None

        # blockchain
        info = inbox.tx_lookup(txid)
        in_chain = bool(info)
        height = int(info["height"]) if info else None
        depth = inbox.tx_depth(height) if height is not None else 0
        final = (depth >= 3)

        present = "none"
        if in_chain:
            present = "blockchain"
        elif mp_state in ("pending", "ok", "invalid"):
            present = "mempool"

        return jsonify({
            "ok": True,
            "txid": txid,
            "present": present, 
            "mempool_state": mp_state,   
            "in_blockchain": in_chain,
            "height": height,
            "depth": depth,       
            "final": final       
        })

    @app.get("/api/dag")
    def api_dag():
        last = request.args.get("last", type=int)
        return jsonify(inbox.export_dag(last=last))

    return app

def run_http(peers, inbox, mempool, ledger, miner_ctrl):
    app = create_app(peers, inbox, mempool, ledger, miner_ctrl)
    app.run(host="0.0.0.0", port=config.HTTP_PORT, threaded=True, use_reloader=False)
