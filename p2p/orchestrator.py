import subprocess
import threading
import json
import math
import socket
import time
from flask import Flask, request, jsonify, Response

DOCKER_IMAGE = "p2p-node:latest"
DOCKER_NETWORK = "mynet"
BIND_HOST = "127.0.0.1"
ORCH_HTTP_PORT = 7000
BASE_NODE_HTTP_PORT = 7001
BASE_NODE_P2P_PORT = 5001
DEFAULT_MINER_ADDRESS = "Hx58472357bc3a95a2513a278b0ebeac775bdc2d62"

app = Flask(__name__)

nodes_lock = threading.Lock()
nodes = {}
edges = []
next_id = 1

def run_cmd(args):
    try:
        p = subprocess.run(args, capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return 1, "", str(e)

def start_node(name=None, http_port=None, p2p_port=None, miner_address=None):
    global next_id
    with nodes_lock:
        nid = next_id
        next_id += 1
    if name is None:
        name = f"node{nid}"
    if http_port is None:
        http_port = BASE_NODE_HTTP_PORT + nid - 1
    if p2p_port is None:
        p2p_port = BASE_NODE_P2P_PORT + nid - 1
    if miner_address is None:
        miner_address = DEFAULT_MINER_ADDRESS
    run_cmd(["docker", "rm", "-f", name])
    cmd = [
        "docker", "run", "-d",
        "--name", name,
        "--network", DOCKER_NETWORK,
        "-e", f"NODE_NAME={name}",
        "-e", f"MINER_ADDRESS={miner_address}",
        "-p", f"{http_port}:7000",
        "-p", f"{p2p_port}:5000",
        DOCKER_IMAGE,
    ]
    rc, out, err = run_cmd(cmd)
    if rc != 0:
        return None, err or "docker run failed"
    with nodes_lock:
        nodes[nid] = {
            "id": nid,
            "name": name,
            "http_host": BIND_HOST,
            "http_port": http_port,
            "p2p_host_port": p2p_port,
            "p2p_internal": 5000,
            "miner_address": miner_address,
        }
    return nodes[nid], None

def node_url(node, path):
    return f"http://{node['http_host']}:{node['http_port']}{path}"

def http_post(node, path, body):
    import urllib.request
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(node_url(node, path), data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        raw = resp.read().decode()
    return json.loads(raw)

def http_get(node, path):
    import urllib.request
    req = urllib.request.Request(node_url(node, path))
    with urllib.request.urlopen(req, timeout=5) as resp:
        raw = resp.read().decode()
    return json.loads(raw)

def open_peer_conn(host, port):
    s = socket.create_connection((host, port), timeout=5)
    s.settimeout(2)
    hello = json.dumps({"type": "HELLO"})
    s.sendall((hello + "\n").encode())
    return s

def send_block_msg(sock, block):
    msg = {"type": "BLOCK", "payload": block}
    line = json.dumps(msg, separators=(",", ":"), ensure_ascii=False) + "\n"
    sock.sendall(line.encode())

def reload_nodes_from_docker():
    global next_id
    rc, out, err = run_cmd(["docker", "ps", "--filter", f"ancestor={DOCKER_IMAGE}", "--format", "{{json .}}"])
    if rc != 0:
        return False, (err or "docker ps failed")
    container_names = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            name = obj.get("Names") or obj.get("Name") or line
        except Exception:
            name = line
        if name:
            container_names.append(name)
    container_names = sorted(set(container_names))
    discovered = []
    for idx, cname in enumerate(container_names, start=1):
        rc_i, out_i, err_i = run_cmd(["docker", "inspect", cname])
        if rc_i != 0:
            continue
        try:
            info_list = json.loads(out_i)
            if not info_list:
                continue
            info = info_list[0]
        except Exception:
            continue
        ns = info.get("NetworkSettings", {}) or {}
        ports = ns.get("Ports") or {}
        http_bind = ports.get("7000/tcp")
        if not http_bind or not isinstance(http_bind, list) or not http_bind[0].get("HostPort"):
            continue
        try:
            http_port = int(http_bind[0].get("HostPort"))
        except Exception:
            continue
        p2p_bind = ports.get("5000/tcp")
        p2p_host_port = None
        if p2p_bind and isinstance(p2p_bind, list) and p2p_bind[0].get("HostPort"):
            try:
                p2p_host_port = int(p2p_bind[0].get("HostPort"))
            except Exception:
                p2p_host_port = None
        ip_addr = None
        networks = ns.get("Networks") or {}
        if DOCKER_NETWORK in networks:
            ip_addr = networks[DOCKER_NETWORK].get("IPAddress")
        if not ip_addr:
            ip_addr = ns.get("IPAddress")
        stub = {"http_host": BIND_HOST, "http_port": http_port}
        try:
            info_api = http_get(stub, "/api/info")
        except Exception:
            info_api = {}
        miner_address = info_api.get("miner_address") or DEFAULT_MINER_ADDRESS
        discovered.append({
            "id": idx,
            "name": cname,
            "http_host": BIND_HOST,
            "http_port": http_port,
            "p2p_host_port": p2p_host_port or (BASE_NODE_P2P_PORT + idx - 1),
            "p2p_internal": 5000,
            "miner_address": miner_address,
            "ip_addr": ip_addr,
        })
    host_map = {}
    for n in discovered:
        if n.get("ip_addr"):
            host_map[str(n["ip_addr"])] = n["id"]
        host_map[str(n["name"])] = n["id"]
    new_edges = []
    for n in discovered:
        stub = {"http_host": BIND_HOST, "http_port": n["http_port"]}
        try:
            peers_list = http_get(stub, "/api/peers")
        except Exception:
            peers_list = []
        if not isinstance(peers_list, list):
            continue
        for p in peers_list:
            if not isinstance(p, dict):
                continue
            addr = p.get("addr")
            if not isinstance(addr, str) or not addr:
                continue
            if ":" in addr:
                host = addr.split(":", 1)[0]
            else:
                host = addr
            to_id = host_map.get(host)
            if not to_id:
                continue
            e = {"from": n["id"], "to": to_id}
            if e not in new_edges:
                new_edges.append(e)
    with nodes_lock:
        nodes.clear()
        for n in discovered:
            entry = dict(n)
            nodes[entry["id"]] = entry
        edges.clear()
        edges.extend(new_edges)
        next_id = len(nodes) + 1
        state = {"nodes": list(nodes.values()), "edges": list(edges)}
    return True, state

def ensure_source_blocks_node(node, min_height, max_iters=20):
    h = -1
    for _ in range(max_iters):
        head = http_get(node, "/api/chain/head")
        h = head.get("height", -1)
        if h >= min_height:
            return h
        http_post(node, "/api/mine", {})
        time.sleep(0.5)
    return h

def test_invalid_block(target_node, count):
    s = open_peer_conn(target_node["http_host"], target_node["p2p_host_port"])
    try:
        for i in range(count):
            payload = {"foo": "bar", "i": i}
            msg = {"type": "BLOCK", "payload": payload}
            line = json.dumps(msg, separators=(",", ":"), ensure_ascii=False) + "\n"
            s.sendall(line.encode())
            time.sleep(0.1)
    finally:
        try:
            s.close()
        except Exception:
            pass

def test_orphan_blocks(source_node, target_node):
    h = ensure_source_blocks_node(source_node, 3)
    chain = http_get(source_node, "/api/carousel")
    if not isinstance(chain, list) or len(chain) < 4:
        return {"ok": False, "error": "chain too short", "height": h, "length": len(chain) if isinstance(chain, list) else None}
    b1 = chain[1]
    b2 = chain[2]
    b3 = chain[3]
    s = open_peer_conn(target_node["http_host"], target_node["p2p_host_port"])
    try:
        send_block_msg(s, b1)
        time.sleep(0.5)
        send_block_msg(s, b3)
        time.sleep(15)
        send_block_msg(s, b2)
        time.sleep(0.5)
    finally:
        try:
            s.close()
        except Exception:
            pass
    return {"ok": True, "height": h, "hashes": [b1.get("Hash"), b2.get("Hash"), b3.get("Hash")]}

def test_fork(node_a, node_b, blocks_a, blocks_b):
    steps = max(blocks_a, blocks_b)
    for i in range(steps):
        if i < blocks_a:
            try:
                http_post(node_a, "/api/mine", {})
            except Exception:
                pass
        if i < blocks_b:
            try:
                http_post(node_b, "/api/mine", {})
            except Exception:
                pass
    head_a = http_get(node_a, "/api/chain/head")
    head_b = http_get(node_b, "/api/chain/head")
    return {"ok": True, "a_head": head_a, "b_head": head_b}

@app.get("/")
def index():
    html = """
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8">
      <title>P2P Orchestrator</title>
      <style>
        body { font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif; margin: 0; padding: 0; background: #f4f5f7; color: #222; }
        header { padding: 1rem 2rem; background: #2c3e50; color: #ecf0f1; display:flex; align-items:center; justify-content:space-between; }
        header h1 { margin: 0; font-size: 1.2rem; }
        main { padding: 1rem 2rem; display:flex; gap:1.5rem; align-items:flex-start; }
        .column { flex:1; min-width: 320px; }
        button { padding: .4rem .7rem; border-radius: 6px; border: 1px solid #ccc; background:#fff; cursor:pointer; font-size:.9rem; }
        button.primary { background:#3498db; border-color:#2980b9; color:#fff; }
        button.danger { background:#e74c3c; border-color:#c0392b; color:#fff; }
        button.small { font-size:.8rem; padding:.3rem .6rem; }
        .toolbar { display:flex; gap:.5rem; flex-wrap:wrap; align-items:center; }
        .card { background:#fff; border-radius:10px; box-shadow:0 1px 3px rgba(0,0,0,.06); padding:.75rem 1rem; margin-bottom:.75rem; border:1px solid #e1e4e8; }
        .card h2 { margin:.1rem 0 .3rem; font-size:1rem; }
        .card h3 { margin:.1rem 0 .3rem; font-size:.95rem; }
        .card small { color:#777; }
        .node-actions { margin-top:.5rem; display:flex; flex-wrap:wrap; gap:.4rem; align-items:center; }
        select { padding:.3rem .4rem; border-radius:6px; border:1px solid #ccc; font-size:.85rem; }
        label { font-size:.85rem; color:#555; margin-right:.25rem; }
        #graph { width:100%; height:480px; background:#fff; border-radius:12px; box-shadow:0 1px 3px rgba(0,0,0,.06); border:1px solid #e1e4e8; }
        #status { font-size:.85rem; color:#555; margin-top:.5rem; }
        #testStatus { font-size:.8rem; color:#222; }
        input[type=text], input[type=number] { padding:.4rem .6rem; border-radius:6px; border:1px solid #ccc; font-size:.85rem; }
        .tabs { padding:0 2rem; margin-top:.3rem; display:flex; gap:.5rem; }
        .tab-btn { padding:.3rem .7rem; border-radius:6px 6px 0 0; border:1px solid #ccc; border-bottom:none; background:#ecf0f1; cursor:pointer; font-size:.85rem; }
        .tab-btn.active { background:#fff; font-weight:600; }
      </style>
    </head>
    <body>
      <header>
        <h1>P2P Orchestrator</h1>
        <div class="toolbar">
          <input id="minerAddressInput" type="text" placeholder="Miner address (opcjonalnie)" />
          <button class="primary" onclick="addNode()">Dodaj node</button>
          <button onclick="mineAllOnce()">Kopnij blok na wszystkich</button>
          <button onclick="setAutoAll(true)">Auto mining ON</button>
          <button onclick="setAutoAll(false)">Auto mining OFF</button>
          <button onclick="reloadAll()">Przeładuj topologię</button>
          <button class="danger" onclick="killAll()">Kill all</button>
        </div>
      </header>
      <div class="tabs">
        <button id="tabBtnNetwork" class="tab-btn active" onclick="showTab('network')">Sieć</button>
        <button id="tabBtnTests" class="tab-btn" onclick="showTab('tests')">Testy</button>
      </div>
      <main id="networkMain">
        <div class="column">
          <h2>Node</h2>
          <div id="nodes"></div>
          <div id="status"></div>
        </div>
        <div class="column">
          <h2>Mapa połączeń</h2>
          <svg id="graph"></svg>
        </div>
      </main>
      <main id="testsMain" style="display:none;">
        <div class="column">
          <h2>Testy protokołu</h2>
          <div class="card">
            <h3>Błędne dane (invalid BLOCK)</h3>
            <div class="node-actions">
              <label>Node docelowy</label>
              <select id="invalidTarget"></select>
              <label>Ilość</label>
              <input id="invalidCount" type="number" min="1" max="100" value="5" />
              <button class="small" onclick="runInvalidTest()">Wyślij</button>
            </div>
          </div>
          <div class="card">
            <h3>Orphan block</h3>
            <div class="node-actions">
              <label>Źródło</label>
              <select id="orphanSource"></select>
              <label>Cel</label>
              <select id="orphanTarget"></select>
              <button class="small" onclick="runOrphanTest()">Uruchom</button>
            </div>
          </div>
          <div class="card">
            <h3>Fork</h3>
            <div class="node-actions">
              <label>Node A</label>
              <select id="forkA"></select>
              <label>Bloki A</label>
              <input id="forkBlocksA" type="number" min="1" max="50" value="3" />
            </div>
            <div class="node-actions">
              <label>Node B</label>
              <select id="forkB"></select>
              <label>Bloki B</label>
              <input id="forkBlocksB" type="number" min="1" max="50" value="3" />
              <button class="small" onclick="runForkTest()">Uruchom</button>
            </div>
          </div>
        </div>
        <div class="column">
          <h2>Wynik testów</h2>
          <pre id="testStatus"></pre>
        </div>
      </main>
      <script>
        var nodePositions = {};
        var graphSvg = null;
        var draggingId = null;
        var dragOffset = {x:0, y:0};
        var lastState = null;

        function setStatus(msg) {
          var el = document.getElementById("status");
          if (el) {
            el.textContent = msg || "";
          }
        }

        function setTestStatus(msg, obj) {
          var el = document.getElementById("testStatus");
          if (!el) {
            return;
          }
          if (typeof obj !== "undefined") {
            try {
              el.textContent = (msg || "") + "\\n" + JSON.stringify(obj, null, 2);
            } catch (e) {
              el.textContent = msg || "";
            }
          } else {
            el.textContent = msg || "";
          }
        }

        function fetchState() {
          return fetch("/api/state").then(function(res) { return res.json(); });
        }

        function updateTestSelects(data) {
          var nodes = data.nodes || [];
          var ids = ["invalidTarget","orphanSource","orphanTarget","forkA","forkB"];
          for (var i = 0; i < ids.length; i++) {
            var id = ids[i];
            var sel = document.getElementById(id);
            if (!sel) {
              continue;
            }
            var prev = sel.value;
            sel.innerHTML = "";
            for (var j = 0; j < nodes.length; j++) {
              var n = nodes[j];
              var opt = document.createElement("option");
              opt.value = n.id;
              opt.textContent = n.name + " (id " + n.id + ")";
              sel.appendChild(opt);
            }
            if (prev) {
              for (var k = 0; k < sel.options.length; k++) {
                if (String(sel.options[k].value) === String(prev)) {
                  sel.value = prev;
                }
              }
            }
          }
        }

        function renderNodes(data) {
          var wrap = document.getElementById("nodes");
          wrap.innerHTML = "";
          var list = data.nodes || [];
          for (var i = 0; i < list.length; i++) {
            var n = list[i];
            var div = document.createElement("div");
            div.className = "card";
            var h = document.createElement("h2");
            h.textContent = n.name + " (id " + n.id + ")";
            div.appendChild(h);
            var info = document.createElement("div");
            info.innerHTML = "<small>HTTP: " + n.http_host + ":" + n.http_port + " | P2P host: " + n.p2p_host_port + " | miner: " + n.miner_address + "</small>";
            div.appendChild(info);
            var act = document.createElement("div");
            act.className = "node-actions";

            var bMine = document.createElement("button");
            bMine.className = "small";
            bMine.textContent = "Mine once";
            bMine.onclick = (function(id) { return function() { mineOnceNode(id); }; })(n.id);
            act.appendChild(bMine);

            var bAutoOn = document.createElement("button");
            bAutoOn.className = "small";
            bAutoOn.textContent = "Auto ON";
            bAutoOn.onclick = (function(id) { return function() { setAutoNode(id, true); }; })(n.id);
            act.appendChild(bAutoOn);

            var bAutoOff = document.createElement("button");
            bAutoOff.className = "small";
            bAutoOff.textContent = "Auto OFF";
            bAutoOff.onclick = (function(id) { return function() { setAutoNode(id, false); }; })(n.id);
            act.appendChild(bAutoOff);

            if (list.length > 1) {
              var lbl = document.createElement("label");
              lbl.textContent = "Połącz z";
              act.appendChild(lbl);
              var sel = document.createElement("select");
              for (var j = 0; j < list.length; j++) {
                var m = list[j];
                if (m.id === n.id) {
                  continue;
                }
                var opt = document.createElement("option");
                opt.value = m.id;
                opt.textContent = m.name;
                sel.appendChild(opt);
              }
              act.appendChild(sel);
              var bConn = document.createElement("button");
              bConn.className = "small";
              bConn.textContent = "Połącz";
              bConn.onclick = (function(fromId, selectEl) { return function() {
                var targetId = parseInt(selectEl.value);
                connectNodes(fromId, targetId);
              }; })(n.id, sel);
              act.appendChild(bConn);
            }

            div.appendChild(act);
            wrap.appendChild(div);
          }
        }

        function renderGraph(data) {
          var svg = document.getElementById("graph");
          graphSvg = svg;
          var list = data.nodes || [];
          var eList = data.edges || [];
          var w = svg.clientWidth || 600;
          var h = svg.clientHeight || 480;
          while (svg.firstChild) {
            svg.removeChild(svg.firstChild);
          }
          if (list.length === 0) {
            return;
          }
          var cx = w / 2;
          var cy = h / 2;
          var r = Math.min(w, h) * 0.35;
          var positions = {};
          for (var i = 0; i < list.length; i++) {
            var n = list[i];
            var pos = nodePositions[n.id];
            if (!pos) {
              if (typeof n.x === "number" && typeof n.y === "number") {
                pos = {x:n.x, y:n.y};
              } else {
                var angle = (2 * Math.PI * i) / list.length;
                pos = {x: cx + r * Math.cos(angle), y: cy + r * Math.sin(angle)};
              }
              nodePositions[n.id] = pos;
            }
            positions[n.id] = pos;
          }
          for (var j = 0; j < eList.length; j++) {
            var e = eList[j];
            var a = positions[e.from];
            var b = positions[e.to];
            if (!a || !b) {
              continue;
            }
            var line = document.createElementNS("http://www.w3.org/2000/svg", "line");
            line.setAttribute("x1", a.x);
            line.setAttribute("y1", a.y);
            line.setAttribute("x2", b.x);
            line.setAttribute("y2", b.y);
            line.setAttribute("stroke", "#bdc3c7");
            line.setAttribute("stroke-width", "1.5");
            svg.appendChild(line);
          }
          for (var k = 0; k < list.length; k++) {
            var nn = list[k];
            var p = positions[nn.id];
            if (!p) {
              continue;
            }
            var g = document.createElementNS("http://www.w3.org/2000/svg", "g");
            g.setAttribute("data-id", nn.id);
            g.setAttribute("transform", "translate(" + p.x + "," + p.y + ")");
            var c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
            c.setAttribute("cx", 0);
            c.setAttribute("cy", 0);
            c.setAttribute("r", 16);
            c.setAttribute("fill", "#3498db");
            c.setAttribute("stroke", "#2c3e50");
            c.setAttribute("stroke-width", "1.5");
            g.appendChild(c);
            var t = document.createElementNS("http://www.w3.org/2000/svg", "text");
            t.setAttribute("x", 0);
            t.setAttribute("y", 4);
            t.setAttribute("text-anchor", "middle");
            t.setAttribute("font-size", "11");
            t.setAttribute("fill", "#ecf0f1");
            t.textContent = nn.name;
            g.appendChild(t);
            (function(id) {
              g.addEventListener("mousedown", function(evt) { startDrag(evt, id); });
            })(nn.id);
            svg.appendChild(g);
          }
        }

        function renderGraphFromCache() {
          if (!lastState) {
            return;
          }
          renderGraph(lastState);
        }

        function startDrag(evt, id) {
          if (!graphSvg) {
            return;
          }
          evt.preventDefault();
          draggingId = id;
          var rect = graphSvg.getBoundingClientRect();
          var x = evt.clientX - rect.left;
          var y = evt.clientY - rect.top;
          var pos = nodePositions[id] || {x:x, y:y};
          dragOffset.x = x - pos.x;
          dragOffset.y = y - pos.y;
        }

        function onSvgMouseMove(evt) {
          if (!draggingId || !graphSvg) {
            return;
          }
          var rect = graphSvg.getBoundingClientRect();
          var x = evt.clientX - rect.left;
          var y = evt.clientY - rect.top;
          var pos = {x: x - dragOffset.x, y: y - dragOffset.y};
          nodePositions[draggingId] = pos;
          renderGraphFromCache();
        }

        function onSvgMouseUp(evt) {
          if (!draggingId) {
            return;
          }
          draggingId = null;
          sendLayout();
        }

        function sendLayout() {
          if (!lastState) {
            return;
          }
          var list = lastState.nodes || [];
          var payload = {positions: []};
          for (var i = 0; i < list.length; i++) {
            var n = list[i];
            var pos = nodePositions[n.id];
            if (pos) {
              payload.positions.push({id:n.id, x:pos.x, y:pos.y});
            }
          }
          fetch("/api/layout", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(payload)
          }).catch(function(e) {});
        }

        function refresh() {
          fetchState().then(function(data) {
            lastState = data;
            updateTestSelects(data);
            renderNodes(data);
            renderGraph(data);
            setStatus("");
          }).catch(function(e) {
            setStatus("Błąd pobierania stanu: " + e);
          });
        }

        function addNode() {
          setStatus("Tworzenie node...");
          try {
            var inp = document.getElementById("minerAddressInput");
            var minerAddr = inp ? inp.value.trim() : "";
            var payload = {};
            if (minerAddr) {
              payload.miner_address = minerAddr;
            }
            fetch("/api/nodes", {
              method: "POST",
              headers: {"Content-Type": "application/json"},
              body: JSON.stringify(payload)
            }).then(function(res) {
              return res.json();
            }).then(function(data) {
              if (!data.ok) {
                setStatus("Błąd tworzenia node: " + (data.error || ""));
              } else {
                setStatus("Utworzono " + data.node.name);
                refresh();
              }
            }).catch(function(e) {
              setStatus("Błąd: " + e);
            });
          } catch (e) {
            setStatus("Błąd: " + e);
          }
        }

        function connectNodes(fromId, toId) {
          setStatus("Łączenie " + fromId + " -> " + toId + "...");
          fetch("/api/connect", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({from_id: fromId, to_id: toId})
          }).then(function(res) {
            return res.json();
          }).then(function(data) {
            if (!data.ok) {
              setStatus("Błąd połączenia: " + (data.error || ""));
            } else {
              setStatus("Połączono " + fromId + " -> " + toId);
              refresh();
            }
          }).catch(function(e) {
            setStatus("Błąd: " + e);
          });
        }

        function mineAllOnce() {
          setStatus("Kopanie na wszystkich node...");
          fetch("/api/actions/mine_all", {method: "POST"}).then(function(res) {
            return res.json();
          }).then(function(data) {
            setStatus("Mine_all: " + JSON.stringify(data));
          }).catch(function(e) {
            setStatus("Błąd: " + e);
          });
        }

        function setAutoAll(enable) {
          setStatus("Auto=" + enable + " na wszystkich...");
          fetch("/api/actions/auto_all", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({enable: enable})
          }).then(function(res) {
            return res.json();
          }).then(function(data) {
            setStatus("Auto_all: " + JSON.stringify(data));
          }).catch(function(e) {
            setStatus("Błąd: " + e);
          });
        }

        function mineOnceNode(id) {
          setStatus("Mine once id=" + id);
          fetch("/api/actions/mine_one", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({id: id})
          }).then(function(res) {
            return res.json();
          }).then(function(data) {
            setStatus("Mine_one: " + JSON.stringify(data));
          }).catch(function(e) {
            setStatus("Błąd: " + e);
          });
        }

        function setAutoNode(id, enable) {
          setStatus("Auto=" + enable + " id=" + id);
          fetch("/api/actions/auto_one", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({id: id, enable: enable})
          }).then(function(res) {
            return res.json();
          }).then(function(data) {
            setStatus("Auto_one: " + JSON.stringify(data));
          }).catch(function(e) {
            setStatus("Błąd: " + e);
          });
        }

        function reloadAll() {
          setStatus("Przeładowanie topologii...");
          fetch("/api/reload", {method: "POST"}).then(function(res) {
            return res.json();
          }).then(function(data) {
            if (!data.ok) {
              setStatus("Błąd reload: " + (data.error || ""));
            } else {
              var cnt = data.state && data.state.nodes ? data.state.nodes.length : 0;
              setStatus("Przeładowano, node: " + cnt);
              lastState = data.state;
              updateTestSelects(lastState);
              renderNodes(lastState);
              renderGraph(lastState);
            }
          }).catch(function(e) {
            setStatus("Błąd: " + e);
          });
        }

        function killAll() {
          if (!confirm("Na pewno zabić wszystkie node?")) {
            return;
          }
          setStatus("Zatrzymywanie wszystkich node...");
          fetch("/api/kill_all", {method: "POST"}).then(function(res) {
            return res.json();
          }).then(function(data) {
            if (!data.ok) {
              setStatus("Błąd kill_all: " + (data.error || ""));
            } else {
              setStatus("Usunięto kontenerów: " + (data.killed || 0));
              lastState = {nodes: [], edges: []};
              nodePositions = {};
              renderNodes(lastState);
              renderGraph(lastState);
              updateTestSelects(lastState);
            }
          }).catch(function(e) {
            setStatus("Błąd: " + e);
          });
        }

        function showTab(name) {
          var netMain = document.getElementById("networkMain");
          var testsMain = document.getElementById("testsMain");
          var btnNet = document.getElementById("tabBtnNetwork");
          var btnTests = document.getElementById("tabBtnTests");
          if (name === "network") {
            netMain.style.display = "flex";
            testsMain.style.display = "none";
            btnNet.classList.add("active");
            btnTests.classList.remove("active");
          } else {
            netMain.style.display = "none";
            testsMain.style.display = "flex";
            btnNet.classList.remove("active");
            btnTests.classList.add("active");
          }
        }

        function runInvalidTest() {
          var sel = document.getElementById("invalidTarget");
          var cnt = document.getElementById("invalidCount");
          if (!sel || !cnt) {
            setTestStatus("Brak danych do testu invalid");
            return;
          }
          var targetId = parseInt(sel.value);
          var count = parseInt(cnt.value) || 1;
          setTestStatus("Start test invalid...");
          fetch("/api/tests/invalid", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({target_id: targetId, count: count})
          }).then(function(res) {
            return res.json();
          }).then(function(data) {
            setTestStatus("Wynik testu invalid", data);
          }).catch(function(e) {
            setTestStatus("Błąd testu invalid: " + e);
          });
        }

        function runOrphanTest() {
          var s = document.getElementById("orphanSource");
          var t = document.getElementById("orphanTarget");
          if (!s || !t) {
            setTestStatus("Brak danych do testu orphan");
            return;
          }
          var sid = parseInt(s.value);
          var tid = parseInt(t.value);
          if (sid === tid) {
            setTestStatus("Źródło i cel nie mogą być takie same");
            return;
          }
          setTestStatus("Start test orphan...");
          fetch("/api/tests/orphan", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({source_id: sid, target_id: tid})
          }).then(function(res) {
            return res.json();
          }).then(function(data) {
            setTestStatus("Wynik testu orphan", data);
          }).catch(function(e) {
            setTestStatus("Błąd testu orphan: " + e);
          });
        }

        function runForkTest() {
          var a = document.getElementById("forkA");
          var b = document.getElementById("forkB");
          var ba = document.getElementById("forkBlocksA");
          var bb = document.getElementById("forkBlocksB");
          if (!a || !b || !ba || !bb) {
            setTestStatus("Brak danych do testu fork");
            return;
          }
          var aid = parseInt(a.value);
          var bid = parseInt(b.value);
          if (aid === bid) {
            setTestStatus("Node A i B muszą być różne");
            return;
          }
          var blocksA = parseInt(ba.value) || 1;
          var blocksB = parseInt(bb.value) || 1;
          setTestStatus("Start test fork...");
          fetch("/api/tests/fork", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({a_id: aid, b_id: bid, a_blocks: blocksA, b_blocks: blocksB})
          }).then(function(res) {
            return res.json();
          }).then(function(data) {
            setTestStatus("Wynik testu fork", data);
          }).catch(function(e) {
            setTestStatus("Błąd testu fork: " + e);
          });
        }

        window.addEventListener("load", function() {
          graphSvg = document.getElementById("graph");
          if (graphSvg) {
            graphSvg.addEventListener("mousemove", onSvgMouseMove);
            graphSvg.addEventListener("mouseup", onSvgMouseUp);
            graphSvg.addEventListener("mouseleave", onSvgMouseUp);
          }
          showTab("network");
          refresh();
          setInterval(refresh, 5000);
        });
      </script>
    </body>
    </html>
    """
    return Response(html, mimetype="text/html")

@app.get("/api/state")
def api_state():
    with nodes_lock:
        return jsonify({"nodes": list(nodes.values()), "edges": edges})

@app.post("/api/nodes")
def api_nodes():
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    http_port = data.get("http_port")
    p2p_port = data.get("p2p_port")
    miner_address = data.get("miner_address") or DEFAULT_MINER_ADDRESS
    node, err = start_node(name=name, http_port=http_port, p2p_port=p2p_port, miner_address=miner_address)
    if node is None:
        return jsonify({"ok": False, "error": err}), 500
    return jsonify({"ok": True, "node": node})

@app.post("/api/connect")
def api_connect():
    data = request.get_json(silent=True) or {}
    from_id = int(data.get("from_id", -1))
    to_id = int(data.get("to_id", -1))
    with nodes_lock:
        a = nodes.get(from_id)
        b = nodes.get(to_id)
    if not a or not b:
        return jsonify({"ok": False, "error": "node not found"}), 400
    target = f"{b['name']}:{b['p2p_internal']}"
    try:
        res = http_post(a, "/api/connect", {"target": target})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    if not res.get("ok"):
        return jsonify({"ok": False, "error": res}), 400
    with nodes_lock:
        if not any(e for e in edges if e["from"] == from_id and e["to"] == to_id):
            edges.append({"from": from_id, "to": to_id})
    return jsonify({"ok": True, "peer": res.get("peer")})

@app.post("/api/actions/mine_all")
def api_mine_all():
    results = []
    with nodes_lock:
        lst = list(nodes.values())
    for n in lst:
        try:
            r = http_post(n, "/api/mine", {})
        except Exception as e:
            r = {"error": str(e)}
        results.append({"id": n["id"], "result": r})
    return jsonify({"ok": True, "results": results})

@app.post("/api/actions/auto_all")
def api_auto_all():
    data = request.get_json(silent=True) or {}
    enable = bool(data.get("enable", False))
    results = []
    with nodes_lock:
        lst = list(nodes.values())
    for n in lst:
        try:
            r = http_post(n, "/api/mining/auto", {"enable": enable})
        except Exception as e:
            r = {"error": str(e)}
        results.append({"id": n["id"], "result": r})
    return jsonify({"ok": True, "results": results})

@app.post("/api/actions/mine_one")
def api_mine_one():
    data = request.get_json(silent=True) or {}
    nid = int(data.get("id", -1))
    with nodes_lock:
        n = nodes.get(nid)
    if not n:
        return jsonify({"ok": False, "error": "node not found"}), 400
    try:
        r = http_post(n, "/api/mine", {})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "result": r})

@app.post("/api/actions/auto_one")
def api_auto_one():
    data = request.get_json(silent=True) or {}
    nid = int(data.get("id", -1))
    enable = bool(data.get("enable", False))
    with nodes_lock:
        n = nodes.get(nid)
    if not n:
        return jsonify({"ok": False, "error": "node not found"}), 400
    try:
        r = http_post(n, "/api/mining/auto", {"enable": enable})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "result": r})

@app.post("/api/reload")
def api_reload():
    ok, state = reload_nodes_from_docker()
    if not ok:
        return jsonify({"ok": False, "error": state}), 500
    return jsonify({"ok": True, "state": state})

@app.post("/api/kill_all")
def api_kill_all():
    global next_id
    with nodes_lock:
        names = [n["name"] for n in nodes.values()]
    results = []
    killed = 0
    for name in names:
        rc, out, err = run_cmd(["docker", "rm", "-f", name])
        results.append({"name": name, "rc": rc, "stdout": out, "stderr": err})
        if rc == 0:
            killed += 1
    with nodes_lock:
        nodes.clear()
        edges.clear()
        next_id = 1
    return jsonify({"ok": True, "killed": killed, "results": results})

@app.post("/api/layout")
def api_layout():
    data = request.get_json(silent=True) or {}
    positions = data.get("positions") or []
    with nodes_lock:
        for p in positions:
            try:
                nid = int(p.get("id"))
            except Exception:
                continue
            n = nodes.get(nid)
            if not n:
                continue
            x = p.get("x")
            y = p.get("y")
            try:
                n["x"] = float(x)
                n["y"] = float(y)
            except Exception:
                continue
    return jsonify({"ok": True})

@app.post("/api/tests/invalid")
def api_tests_invalid():
    data = request.get_json(silent=True) or {}
    target_id = int(data.get("target_id", -1))
    count = int(data.get("count", 5))
    with nodes_lock:
        n = nodes.get(target_id)
    if not n:
        return jsonify({"ok": False, "error": "target not found"}), 400
    try:
        test_invalid_block(n, count)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "target_id": target_id, "count": count})

@app.post("/api/tests/orphan")
def api_tests_orphan():
    data = request.get_json(silent=True) or {}
    source_id = int(data.get("source_id", -1))
    target_id = int(data.get("target_id", -1))
    with nodes_lock:
        s_node = nodes.get(source_id)
        t_node = nodes.get(target_id)
    if not s_node or not t_node:
        return jsonify({"ok": False, "error": "node not found"}), 400
    try:
        info = test_orphan_blocks(s_node, t_node)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    if not info.get("ok"):
        return jsonify(info), 400
    return jsonify(info)

@app.post("/api/tests/fork")
def api_tests_fork():
    data = request.get_json(silent=True) or {}
    a_id = int(data.get("a_id", -1))
    b_id = int(data.get("b_id", -1))
    blocks_a = int(data.get("a_blocks", 3))
    blocks_b = int(data.get("b_blocks", 3))
    with nodes_lock:
        node_a = nodes.get(a_id)
        node_b = nodes.get(b_id)
    if not node_a or not node_b:
        return jsonify({"ok": False, "error": "node not found"}), 400
    try:
        info = test_fork(node_a, node_b, blocks_a, blocks_b)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify(info)

if __name__ == "__main__":
    app.run(host=BIND_HOST, port=ORCH_HTTP_PORT, debug=True)
