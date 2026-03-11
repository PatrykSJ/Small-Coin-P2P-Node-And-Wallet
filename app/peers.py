import json
import time
import socket
import threading
from typing import Tuple, Dict, Optional, List
from . import config
from .inbox import Inbox
import datetime

def now_str() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")

def send_json_line(sock: socket.socket, obj: dict):
    data = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    if len(data) > config.MAX_LINE_BYTES:
        obj = {"type": "ECHO", "truncated": True}
        data = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    sock.sendall((data + "\n").encode())

class PeerInfo:
    def __init__(self, sock: socket.socket, addr_str: str, outgoing: bool):
        self.sock = sock
        self.addr = addr_str
        self.outgoing = outgoing
        self.last_seen = time.time()
        self.lock = threading.Lock()

class PeerManager:
    def __init__(self, inbox: Inbox):
        self._peers: Dict[str, PeerInfo] = {}
        self._lock = threading.Lock()
        self._inbox = inbox
        self._misbehavior: Dict[str, int] = {}
        self._banned_until: Dict[str, float] = {}
        self._rate_bucket: Dict[str, tuple[int, int]] = {}
        try:
            self._inbox.set_misbehavior_cb(self.report_misbehavior)
        except Exception:
            pass

    def report_misbehavior(self, addr: str, points: int = 1, reason: str = ""):
        now = time.time()
        ban_thr = int(getattr(config, "MISBEHAVIOR_BAN_THRESHOLD", 3))
        ban_sec = float(getattr(config, "MISBEHAVIOR_BAN_SECONDS", 300))
        with self._lock:
            cur = self._misbehavior.get(addr, 0) + int(points)
            self._misbehavior[addr] = cur
            if cur >= ban_thr:
                self._banned_until[addr] = now + ban_sec
                pi = self._peers.pop(addr, None)
                if pi:
                    try:
                        pi.sock.close()
                    except Exception:
                        pass
                print(f"[{now_str()}] PEER banned: {addr} ({reason})")

    def add(self, sock: socket.socket, addr: Tuple[str, int], outgoing: bool):
        addr_str = f"{addr[0]}:{addr[1]}"
        if self._banned_until.get(addr_str, 0) > time.time():
            try:
                sock.close()
            except Exception:
                pass
            print(f"[{now_str()}] PEER rejected (banned): {addr_str}")
            return
        pi = PeerInfo(sock, addr_str, outgoing)
        with self._lock:
            old = self._peers.get(addr_str)
            self._peers[addr_str] = pi
        if old:
            try:
                old.sock.close()
            except Exception:
                pass
        t = threading.Thread(target=self._recv_loop, args=(pi,), daemon=True)
        t.start()
        print(f"[{now_str()}] PEER added: {addr_str} ({'out' if outgoing else 'in'})")

    def remove(self, addr_str: str):
        with self._lock:
            pi = self._peers.pop(addr_str, None)
        if pi:
            try:
                pi.sock.close()
            except Exception:
                pass
            print(f"[{now_str()}] PEER removed: {addr_str}")

    def connect(self, host: str, port: int) -> Optional[str]:
        peer = f"{host}:{port}"
        print(f"[{now_str()}] OUT: łączenie do {peer}…")
        if self._banned_until.get(peer, 0) > time.time():
            print(f"[{now_str()}] OUT: zablokowano łączenie do {peer} (ban)")
            return None
        try:
            s = socket.create_connection((host, port), timeout=10)
            s.settimeout(None)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except OSError:
                pass
        except Exception as e:
            print(f"[{now_str()}] OUT: nieudane połączenie do {peer}: {e}")
            return None
        self.add(s, (host, port), outgoing=True)
        self.send_json(peer, {"type": "HELLO"})
        self.send_json(peer, {"type": "HEALTHCHECK", "ts": int(time.time())})
        self.send_json(peer, {"type": "GET_CHAIN_HEAD"})
        return peer

    def _safe_send(self, pi: PeerInfo, obj: dict) -> bool:
        try:
            with pi.lock:
                send_json_line(pi.sock, obj)
            return True
        except Exception as e:
            print(f"[{now_str()}] SEND error {pi.addr}: {e}")
            self.remove(pi.addr)
            return False

    def send_json(self, addr_str: str, obj: dict) -> bool:
        with self._lock:
            pi = self._peers.get(addr_str)
        if not pi:
            print(f"[{now_str()}] WARN: peer {addr_str} nieznany")
            return False
        return self._safe_send(pi, obj)

    def broadcast_json(self, obj: dict):
        for addr in list(self.snapshot().keys()):
            self.send_json(addr, obj)

    def broadcast_json_except(self, obj: dict, except_addr: Optional[str] = None):
        snap = self.snapshot()
        excluded_host = None
        if except_addr:
            excluded_host = except_addr.split(":", 1)[0]
        for addr in list(snap.keys()):
            host = addr.split(":", 1)[0]
            if excluded_host and host == excluded_host:
                continue
            self.send_json(addr, obj)

    def broadcast_health(self):
        ts = int(time.time())
        self.broadcast_json({"type": "HEALTHCHECK", "ts": ts})

    def cleanup_dead(self):
        peers = self.snapshot()
        now_ts = time.time()
        for addr, pi in peers.items():
            if now_ts - pi.last_seen > config.HEALTH_TIMEOUT:
                print(f"[{now_str()}] {addr} nieaktywny {int(now_ts - pi.last_seen)}s — usuwam")
                self.remove(addr)

    def snapshot(self) -> Dict[str, PeerInfo]:
        with self._lock:
            return dict(self._peers)

    def status_lines(self) -> List[dict]:
        out = []
        snap = self.snapshot()
        for addr, pi in snap.items():
            out.append({
                "addr": addr,
                "direction": "OUT" if pi.outgoing else "IN",
                "last_seen_s": int(time.time() - pi.last_seen)
            })
        return sorted(out, key=lambda x: x["addr"])

    def _recv_loop(self, pi: PeerInfo):
        sock = pi.sock
        addr = pi.addr
        rate_limit = int(getattr(config, "RATE_LIMIT_MSGS_PER_SEC", 50))
        try:
            sock.settimeout(None)
            with sock:
                buff = b""
                while True:
                    data = sock.recv(4096)
                    if not data:
                        break
                    buff += data
                    if len(buff) > config.MAX_LINE_BYTES and b"\n" not in buff:
                        print(f"[{now_str()}] DROP overlong line from {addr}")
                        self.report_misbehavior(addr, 1, "overlong-line")
                        self.remove(addr)
                        return
                    while b"\n" in buff:
                        line, buff = buff.split(b"\n", 1)
                        text = line.decode(errors="replace").rstrip("\r")
                        pi.last_seen = time.time()
                        t_now = int(time.time())
                        w, c = self._rate_bucket.get(addr, (t_now, 0))
                        if t_now != w:
                            w, c = t_now, 0
                        c += 1
                        self._rate_bucket[addr] = (w, c)
                        if c > rate_limit:
                            self.report_misbehavior(addr, 1, "rate-limit")
                            continue
                        try:
                            msg = json.loads(text)
                            if not isinstance(msg, dict):
                                msg = {"type": "text", "value": msg}
                        except Exception:
                            msg = {"type": "text", "value": text}
                        mtype = str(msg.get("type", "")).upper()
                        if mtype == "HEALTHCHECK":
                            self._safe_send(pi, {"type": "HEALTHACK"})
                        elif mtype == "HEALTHACK":
                            pass
                        elif mtype == "PING":
                            self._inbox.add(addr, {"event": "PING"})
                            self._safe_send(pi, {"type": "PONG"})
                        elif mtype == "PONG":
                            self._inbox.add(addr, {"event": "PONG"})
                        elif mtype == "HELLO":
                            self._safe_send(pi, {"type": "HELLO-ACK"})
                            self._safe_send(pi, {"type": "GET_CHAIN_HEAD"})
                        elif mtype == "HELLO-ACK":
                            pass
                        elif mtype == "GET_CHAIN_HEAD":
                            h, hh = self._inbox._local_head()
                            self._safe_send(pi, {
                                "type": "CHAIN_HEAD",
                                "height": h,
                                "hash": hh,
                                "recent": self._inbox._recent(64)
                            })
                        elif mtype == "CHAIN_HEAD":
                            try:
                                r_height = int(msg.get("height", -1))
                            except Exception:
                                r_height = -1
                            r_recent = msg.get("recent") or []
                            r_hash = msg.get("hash")
                            my_h, my_hash = self._inbox._local_head()
                            if r_height > my_h:
                                fork_h = self._inbox._find_fork_height(r_recent)
                                self._safe_send(pi, {
                                    "type": "GET_BLOCKS_FROM",
                                    "from": fork_h,
                                    "to": r_height
                                })
                            elif r_height == my_h and r_hash and my_hash and r_hash != my_hash:
                                fork_h = self._inbox._find_fork_height(r_recent)
                                self._safe_send(pi, {
                                    "type": "GET_BLOCKS_FROM",
                                    "from": fork_h,
                                    "to": r_height
                                })
                        elif mtype == "GET_BLOCKS_FROM":
                            frm = int(msg.get("from", -1))
                            to = int(msg.get("to", -1))
                            bc = self._inbox.getBlockChain()
                            if not bc:
                                continue
                            for idx, b in enumerate(bc):
                                h = idx
                                if h > frm and (to < 0 or h <= to):
                                    ok = self._safe_send(pi, {"type": "BLOCK", "payload": b})
                                    if not ok:
                                        break
                        elif mtype == "TX":
                            payload = msg.get("payload", {})
                            if isinstance(payload, dict):
                                self._inbox.add_tx(pi.addr, payload)
                            self._safe_send(pi, {"type": "DATA-ACK"})
                        elif mtype == "BLOCK":
                            payload = msg.get("payload", {})
                            if isinstance(payload, dict):
                                self._inbox.add(addr, payload)
                            self._safe_send(pi, {"type": "DATA-ACK"})
                        elif mtype == "ECHO":
                            pass
                        else:
                            val = msg if not isinstance(msg, dict) else msg.get("value", msg)
                            self._safe_send(pi, {"type": "ECHO", "value": val})
        except Exception as e:
            print(f"[{now_str()}] RECV error {addr}: {e}")
        finally:
            self.remove(addr)

def p2p_listener(peers: PeerManager):
    print(f"[{now_str()}] LISTEN start on {config.P2P_HOST}:{config.P2P_PORT}")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((config.P2P_HOST, config.P2P_PORT))
        s.listen(64)
        while True:
            conn, addr = s.accept()
            peers.add(conn, addr, outgoing=False)

def health_monitor(peers: PeerManager):
    while True:
        time.sleep(config.HEALTH_INTERVAL)
        peers.broadcast_health()
        peers.cleanup_dead()

def p2p_boot(peers: PeerManager):
    threading.Thread(target=p2p_listener, args=(peers,), daemon=True).start()
    threading.Thread(target=health_monitor, args=(peers,), daemon=True).start()
