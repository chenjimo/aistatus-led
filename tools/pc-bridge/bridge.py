#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 状态机 · 手机桥接端 `ai-status-phone/1` 参考实现

职责：
  1. 轮询本机 AI_STATUIS_APP 的事件总线（默认 http://127.0.0.1:8765/health）
  2. 把状态转换成 ai-status-phone/1 的 status 帧
  3. 通过 HTTP + WebSocket 提供给局域网内的鸿蒙手机
  4. 接收手机回传的动作（action），支持内置动作与 actions.json 自定义命令

零依赖：只用 Python 标准库（3.10+）。
协议完整说明见 ../docs/02-PC端桥接接口规范.md

用法：
    python bridge.py                 # 启动
    python bridge.py revoke <devId>  # 撤销某台手机的配对
    python bridge.py status          # 打印当前状态与在线手机
    python bridge.py --demo          # 演示模式：不连 AI 服务，自动循环状态
    python bridge.py --enable-input  # V2：开启键鼠注入（默认关闭，空闲自动失效）
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    import input_win
except Exception:
    input_win = None

CONFIG_PATH = os.path.join(HERE, "bridge_config.json")
PHONES_PATH = os.path.join(HERE, "phones.json")
ACTIONS_PATH = os.path.join(HERE, "actions.json")
PIN_PATH = os.path.join(HERE, "pin.txt")

PROTO = "ai-status-phone/1"

DEFAULT_CONFIG = {
    "listen_host": "0.0.0.0",
    "listen_port": 8770,
    "discovery_port": 4211,
    "pc_name": "",                 # 空则用主机名
    "service_url": "http://127.0.0.1:8765/health",
    "poll_ms": 500,
    "pair_required": True,
    "pin": "",                      # 固定配对码；留空则每次启动随机生成
    "heartbeat_ms": 5000,
    "mute_default_minutes": 5,
    # V2：远控默认关闭，必须显式在电脑端打开
    "enable_input": False,
    "input_idle_timeout_sec": 600,
    "gesture_map": {},
}

# ai-status/1 的优先级表（与 docs/04 一致），手机端不再内置
PRIO = {
    "offline": 0, "idle": 10, "thinking": 20, "searching": 25,
    "coding": 30, "outputting": 30, "done": 40,
    "need_input": 80, "need_confirm": 85, "error": 90,
}
STICKY = {"need_input", "need_confirm", "error"}


def load_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def log(*args: Any) -> None:
    print(time.strftime("[%H:%M:%S]"), *args, flush=True)


# --------------------------------------------------------------------------- #
# 状态源：把 AI_STATUIS_APP 的 /health 变成 ai-status-phone/1 的 status.data
# --------------------------------------------------------------------------- #

class StatusSource:
    """轮询 /health，产出 status.data；变化时通知 Condition 等待者。"""

    def __init__(self, cfg: dict, demo: bool = False) -> None:
        self.cfg = cfg
        self.demo = demo
        self.cond = threading.Condition()
        self.seq = 0
        self.source_id = cfg.get("pc_name") or socket.gethostname()
        self.version = "-"
        self.muted_until = 0.0
        self.actions_cfg = load_json(ACTIONS_PATH, {})
        self.current: dict = self._make("offline", "", 0.0)
        self.last_change_ts = time.time()

    # ---- 对外 ----

    def snapshot(self) -> dict:
        with self.cond:
            return json.loads(json.dumps(self.current))

    def set_muted(self, minutes: float) -> None:
        with self.cond:
            self.muted_until = time.time() + max(0.0, minutes) * 60
            self.current["muted"] = self.muted_until > time.time()
            self.cond.notify_all()
        log(f"手机端静音 {minutes:g} 分钟")

    def run(self) -> None:
        if self.demo:
            self._run_demo()
            return
        while True:
            try:
                self._poll_once()
            except Exception as exc:
                self._apply("offline", f"无法连接 AI 服务：{exc}", None)
            time.sleep(max(0.1, self.cfg["poll_ms"] / 1000))

    # ---- 内部 ----

    def _poll_once(self) -> None:
        req = urllib.request.Request(self.cfg["service_url"], method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            health = json.loads(resp.read().decode("utf-8", "ignore"))
        self.version = str(health.get("version") or "-")
        st = health.get("state") or {}
        state = str(st.get("state") or "idle")
        msg = str(st.get("msg") or "")
        if not st.get("sticky"):
            # 非粘性且心跳超过 15s → 视为 PC 侧服务仍在，但任务静默，保持状态
            pass
        progress = st.get("progress")
        self._apply(state, msg, progress)

    def _run_demo(self) -> None:
        """演示模式：按固定节奏循环全部状态，方便调实况窗 / 悬浮窗。"""
        plan = [
            ("idle", "空闲待命", None, 3),
            ("thinking", "正在分析需求…", 0.05, 5),
            ("searching", "检索代码库 / 文档", 0.2, 5),
            ("coding", "修改 src/state.py", 0.55, 3),
            ("coding", "运行测试用例", 0.75, 2),
            ("need_confirm", "是否允许执行 execute_command?", 0.8, 8),
            ("outputting", "正在输出回答", 0.95, 3),
            ("done", "本轮任务完成", 1.0, 4),
        ]
        idx = 0
        while True:
            state, msg, prog, hold = plan[idx % len(plan)]
            self._apply(state, msg, prog)
            idx += 1
            time.sleep(hold)

    def _apply(self, state: str, msg: str, progress: Optional[float]) -> None:
        if state not in PRIO:
            state = "idle"
        now = time.time()
        with self.cond:
            muted = self.muted_until > now
            data = self._make(state, msg, progress)
            data["muted"] = muted
            changed = (
                data["state"] != self.current.get("state")
                or data["msg"] != self.current.get("msg")
                or data["progress"] != self.current.get("progress")
                or data["actions"] != self.current.get("actions")
                or muted != self.current.get("muted")
            )
            if not changed:
                return
            if data["state"] != self.current.get("state"):
                data["since"] = int(now * 1000)
            else:
                data["since"] = self.current.get("since")
            self.current = data
            self.seq += 1
            self.cond.notify_all()
        log(f"状态 -> {state:<13} {msg[:40]}")

    def _make(self, state: str, msg: str, progress: Optional[float]) -> dict:
        return {
            "src": self.source_id,
            "state": state,
            "prio": PRIO.get(state, 0),
            "sticky": state in STICKY,
            "since": int(time.time() * 1000),
            "msg": msg,
            "taskTitle": self._task_title(msg, state),
            "progress": progress,
            "online": state != "offline",
            "muted": False,
            "actions": self._actions_for(state),
        }

    @staticmethod
    def _task_title(msg: str, state: str) -> str:
        text = (msg or "").strip()
        if text.startswith("["):
            # 去掉 PC 端自动加的 [src] 前缀
            end = text.find("]")
            if end > 0:
                text = text[end + 1:].strip()
        if not text:
            return {"idle": "空闲", "done": "已完成"}.get(state, "AI 工作中")
        return text[:40]

    def _actions_for(self, state: str) -> list:
        acts = []
        if state in STICKY:
            acts.append({"id": "clear", "label": "已处理", "style": "primary"})
        if "focus" in self.actions_cfg:
            acts.append({"id": "focus", "label": "切到窗口", "style": "normal"})
        if state in ("thinking", "searching", "coding", "outputting"):
            acts.append({"id": "mute", "label": "静音 5 分钟", "style": "normal"})
        return acts

    # ---- 执行动作 ----

    def run_action(self, action: str, src: str, payload: dict) -> tuple[bool, str]:
        if action == "clear":
            return self._call_service("/clear")
        if action == "refresh":
            with self.cond:
                self.cond.notify_all()
            return True, "已刷新"
        if action == "mute":
            minutes = float(payload.get("minutes") or self.cfg["mute_default_minutes"])
            self.set_muted(minutes)
            return True, f"已静音 {minutes:g} 分钟"
        cfg = self.actions_cfg.get(action)
        if not cfg:
            return False, "action_not_found"
        cmd = cfg.get("cmd")
        if not cmd:
            return False, "action_no_cmd"
        try:
            timeout = float(cfg.get("timeout", 5))
            subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, cwd=HERE)
            return True, "已执行"
        except Exception as exc:
            return False, f"执行失败：{exc}"

    def _call_service(self, path: str) -> tuple[bool, str]:
        base = self.cfg["service_url"].rsplit("/health", 1)[0]
        try:
            req = urllib.request.Request(base + path, data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                json.loads(resp.read().decode("utf-8", "ignore"))
            return True, "已下发到 AI 服务"
        except Exception as exc:
            return False, f"AI 服务无响应：{exc}"


# --------------------------------------------------------------------------- #
# 配对与 Token
# --------------------------------------------------------------------------- #

class Auth:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.lock = threading.Lock()
        self.phones: dict = load_json(PHONES_PATH, {})
        # 固定配对码（由 AI_STATUIS_APP 启动器写入 bridge_config.json）；
        # 留空时退回原来的每次随机生成，兼容单独运行 bridge.py。
        fixed = str(self.cfg.get("pin") or "").strip()
        self.pin = fixed or f"{random.randint(0, 999999):06d}"
        self.fails: dict = {}
        try:
            with open(PIN_PATH, "w", encoding="utf-8") as f:
                f.write(self.pin)
        except Exception:
            pass

    def check(self, token: Optional[str]) -> bool:
        if not self.cfg.get("pair_required"):
            return True
        if not token:
            return False
        with self.lock:
            return any(p.get("token") == token for p in self.phones.values())

    def pair(self, pin: str, device_id: str, device_name: str, ip: str) -> tuple[bool, dict]:
        now = time.time()
        with self.lock:
            rec = self.fails.get(ip)
            if rec and rec[1] > now and rec[0] >= 10:
                return False, {"ok": False, "reason": "too_many_attempts",
                               "message": "失败次数过多，请 5 分钟后再试"}
            if pin != self.pin:
                cnt = (rec[0] + 1) if rec and rec[1] > now else 1
                self.fails[ip] = (cnt, now + 300)
                return False, {"ok": False, "reason": "bad_pin", "message": "配对码错误"}
            self.fails.pop(ip, None)
            token = hashlib.sha256(os.urandom(32)).hexdigest()[:40]
            self.phones[device_id] = {
                "deviceId": device_id, "name": device_name, "token": token,
                "ip": ip, "pairedAt": int(now * 1000),
            }
            save_json(PHONES_PATH, self.phones)
            log(f"新设备配对：{device_name} ({ip})")
            return True, {"ok": True, "token": token,
                          "pcName": self.cfg.get("pc_name") or socket.gethostname(),
                          "proto": PROTO}

    def revoke(self, device_id: str) -> bool:
        with self.lock:
            if device_id in self.phones:
                del self.phones[device_id]
                save_json(PHONES_PATH, self.phones)
                return True
            return False


# --------------------------------------------------------------------------- #
# WebSocket（最小实现：文本帧 + ping/pong/close）
# --------------------------------------------------------------------------- #

class WsClient:
    def __init__(self, wfile, wlock: threading.Lock) -> None:
        self.wfile = wfile
        self.wlock = wlock
        self.alive = True

    def send_text(self, text: str) -> bool:
        payload = text.encode("utf-8")
        n = len(payload)
        if n < 126:
            header = struct.pack("!BB", 0x81, n)
        elif n < 65536:
            header = struct.pack("!BBH", 0x81, 126, n)
        else:
            header = struct.pack("!BBQ", 0x81, 127, n)
        try:
            with self.wlock:
                self.wfile.write(header + payload)
                self.wfile.flush()
            return True
        except Exception:
            self.alive = False
            return False


WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def ws_read_frame(rfile) -> tuple[int, bytes]:
    """读一帧，返回 (opcode, payload)。自动解掩码。"""
    b = rfile.read(2)
    if len(b) < 2:
        return 0x8, b""
    b0, b1 = b[0], b[1]
    opcode = b0 & 0x0F
    masked = b1 & 0x80
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack("!H", rfile.read(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", rfile.read(8))[0]
    mask = rfile.read(4) if masked else b""
    data = rfile.read(length) if length else b""
    if masked and data:
        data = bytes(data[i] ^ mask[i % 4] for i in range(len(data)))
    return opcode, data


# --------------------------------------------------------------------------- #
# HTTP 服务
# --------------------------------------------------------------------------- #

class Hub:
    def __init__(self, cfg: dict, source: StatusSource, auth: Auth) -> None:
        self.cfg = cfg
        self.source = source
        self.auth = auth
        self.clients: list[WsClient] = []
        self.clients_lock = threading.Lock()
        self.last_seq_sent = 0
        self.inputs = (input_win.InputEngine(
            enabled=bool(cfg.get("enable_input")),
            timeout=float(cfg.get("input_idle_timeout_sec", 600)),
            gesture_map=cfg.get("gesture_map") or {},
        ) if input_win is not None else None)

    # ---- V2 远控 ----

    def caps(self) -> list:
        base = ["status", "action", "progress", "poll"]
        if self.inputs is not None:
            base.append("input")
        return base

    def apply_input(self, body: dict) -> dict:
        req_id = str(body.get("reqId") or "")
        if self.inputs is None:
            return {"type": "ack", "reqId": req_id, "ok": False,
                    "action": "input", "message": "input_unavailable：本平台不支持"}
        ok, message = self.inputs.apply(body)
        out = {"type": "ack", "reqId": req_id, "ok": ok,
               "action": "input", "kind": body.get("kind"),
               "message": message, "input": self.inputs.status()}
        self.broadcast(json.dumps(out, ensure_ascii=False))
        return out

    # ---- 广播 ----

    def add_client(self, c: WsClient) -> None:
        with self.clients_lock:
            self.clients.append(c)
        c.send_text(json.dumps({"type": "ack", "ok": True, "action": "hello",
                                "proto": PROTO, "pcName": self.pc_name(),
                                "caps": self.caps(),
                                "input": self.inputs.status() if self.inputs is not None
                                         else {"enabled": False}},
                               ensure_ascii=False))
        # 立即补发当前状态
        self.source.seq += 1
        c.send_text(self._status_frame())

    def remove_client(self, c: WsClient) -> None:
        with self.clients_lock:
            if c in self.clients:
                self.clients.remove(c)

    def broadcast(self, text: str) -> None:
        with self.clients_lock:
            dead = [c for c in self.clients if not c.send_text(text)]
            for c in dead:
                self.clients.remove(c)

    def pc_name(self) -> str:
        return self.cfg.get("pc_name") or socket.gethostname()

    def _status_frame(self) -> str:
        return json.dumps({"type": "status", "seq": self.source.seq,
                           "data": self.source.snapshot()}, ensure_ascii=False)

    def broadcaster(self) -> None:
        """状态变化 → 推 status；空闲时每 heartbeat_ms 推 heartbeat。"""
        last_beat = time.time()
        while True:
            with self.source.cond:
                self.source.cond.wait(timeout=self.cfg["heartbeat_ms"] / 1000)
                frame = None
                if self.source.seq != self.last_seq_sent:
                    self.last_seq_sent = self.source.seq
                    frame = self._status_frame()
            now = time.time()
            if frame:
                self.broadcast(frame)
                last_beat = now
            elif (now - last_beat) * 1000 >= self.cfg["heartbeat_ms"]:
                self.broadcast(json.dumps({"type": "heartbeat", "seq": self.source.seq,
                                           "ts": int(now * 1000)}))
                last_beat = now


def make_handler(hub: Hub):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "AIStatusPhoneBridge/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            pass

        # ---------- 工具 ----------

        def _json(self, code: int, obj: dict) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type,X-Token")
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                pass

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8", "ignore"))

        def _token(self) -> Optional[str]:
            tok = self.headers.get("X-Token")
            if tok:
                return tok
            qs = parse_qs(urlparse(self.path).query)
            return (qs.get("token") or [None])[0]

        def _authed(self) -> bool:
            return hub.auth.check(self._token())

        # ---------- 路由 ----------

        def do_OPTIONS(self) -> None:
            self._json(204, {})

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path in ("/", "/v1/hello"):
                return self._json(200, {
                    "ok": True, "proto": PROTO, "v": 1,
                    "pcName": hub.pc_name(),
                    "pairRequired": bool(hub.cfg.get("pair_required")),
                    "caps": hub.caps(),
                    "input": hub.inputs.status() if hub.inputs is not None else
                             {"enabled": False},
                })
            if path == "/v1/snapshot":
                if not self._authed():
                    return self._json(401, {"ok": False, "reason": "unauthorized"})
                return self._json(200, self._snapshot())
            if path == "/v1/poll":
                if not self._authed():
                    return self._json(401, {"ok": False, "reason": "unauthorized"})
                return self._poll()
            if path == "/v1/stream":
                return self._ws()
            return self._json(404, {"ok": False, "reason": "not_found"})

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            try:
                body = self._body()
            except Exception as exc:
                return self._json(400, {"ok": False, "reason": "invalid_json",
                                        "message": str(exc)})
            if path == "/v1/pair":
                return self._pair(body)
            if not self._authed():
                return self._json(401, {"ok": False, "reason": "unauthorized"})
            if path == "/v1/action":
                return self._action(body)
            if path == "/v1/input":
                return self._json(200, hub.apply_input(body))
            return self._json(404, {"ok": False, "reason": "not_found"})

        # ---------- 具体处理 ----------

        def _snapshot(self) -> dict:
            data = hub.source.snapshot()
            return {
                "ok": True, "proto": PROTO,
                "pc": {"name": hub.pc_name(), "sourceId": data["src"],
                       "version": hub.source.version},
                "serverTime": int(time.time() * 1000),
                "sources": [data],
                "aggregate": {"src": data["src"], "state": data["state"],
                              "prio": data["prio"]},
                "v2": {"caps": hub.caps(),
                       "input": hub.inputs.status() if hub.inputs is not None else
                                {"enabled": False}},
            }

        def _poll(self) -> None:
            qs = parse_qs(urlparse(self.path).query)
            since = int((qs.get("since") or ["0"])[0])
            timeout = float((qs.get("timeout") or ["25"])[0])
            deadline = time.time() + min(max(timeout, 1.0), 30.0)
            with hub.source.cond:
                while hub.source.seq <= since and time.time() < deadline:
                    hub.source.cond.wait(timeout=max(0.1, deadline - time.time()))
                seq = hub.source.seq
            if seq > since:
                return self._json(200, json.loads(hub._status_frame()))
            return self._json(200, {"type": "heartbeat", "seq": seq,
                                    "ts": int(time.time() * 1000)})

        def _pair(self, body: dict) -> None:
            pin = str(body.get("pin") or "")
            dev_id = str(body.get("deviceId") or "")
            dev_name = str(body.get("deviceName") or "HarmonyOS")
            if not dev_id:
                return self._json(400, {"ok": False, "reason": "missing_device_id"})
            ok, res = hub.auth.pair(pin, dev_id, dev_name, self.client_address[0])
            return self._json(200 if ok else 403, res)

        def _action(self, body: dict) -> None:
            action = str(body.get("action") or "")
            req_id = str(body.get("reqId") or "")
            src = str(body.get("src") or "")
            payload = body.get("payload") or {}
            ok, message = hub.source.run_action(action, src, payload)
            reply = {"type": "ack", "reqId": req_id, "ok": ok,
                     "action": action, "message": message}
            hub.broadcast(json.dumps(reply, ensure_ascii=False))
            return self._json(200, reply)

        # ---------- WebSocket ----------

        def _ws(self) -> None:
            if not self._authed():
                return self._json(401, {"ok": False, "reason": "unauthorized"})
            key = self.headers.get("Sec-WebSocket-Key")
            if not key:
                return self._json(400, {"ok": False, "reason": "not_websocket"})
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", ws_accept_key(key))
            self.end_headers()
            self.wfile.flush()

            client = WsClient(self.wfile, threading.Lock())
            hub.add_client(client)
            log(f"手机已连接：{self.client_address[0]}")
            try:
                self.connection.settimeout(None)
                while client.alive:
                    opcode, data = ws_read_frame(self.rfile)
                    if opcode == 0x8:
                        break
                    if opcode == 0x9:
                        with client.wlock:
                            self.wfile.write(bytes([0x8A, len(data)]) + data)
                            self.wfile.flush()
                        continue
                    if opcode in (0x1, 0x2):
                        self._on_ws_message(client, data)
            except Exception:
                pass
            finally:
                client.alive = False
                hub.remove_client(client)
                log(f"手机断开：{self.client_address[0]}")
            self.close_connection = True

        def _on_ws_message(self, client: WsClient, data: bytes) -> None:
            try:
                msg = json.loads(data.decode("utf-8", "ignore"))
            except Exception:
                return
            typ = msg.get("type")
            req_id = str(msg.get("reqId") or "")
            if typ == "ping":
                client.send_text(json.dumps({"type": "ack", "reqId": req_id,
                                             "ok": True, "action": "ping"}))
            elif typ == "action":
                ok, message = hub.source.run_action(
                    str(msg.get("action") or ""), str(msg.get("src") or ""),
                    msg.get("payload") or {})
                reply = {"type": "ack", "reqId": req_id, "ok": ok,
                         "action": msg.get("action"), "message": message}
                hub.broadcast(json.dumps(reply, ensure_ascii=False))
                log(f"手机动作 {msg.get('action')} -> {message}")
            elif typ == "input":
                hub.apply_input(msg)

    return Handler


# --------------------------------------------------------------------------- #
# UDP 发现
# --------------------------------------------------------------------------- #

def discovery_loop(cfg: dict, hub: Hub) -> None:
    port = int(cfg["discovery_port"])
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.bind(("", port))
    except OSError as exc:
        log(f"UDP {port} 绑定失败（发现功能不可用）：{exc}")
        return
    log(f"UDP 发现已监听 :{port}")
    while True:
        try:
            data, addr = sock.recvfrom(2048)
        except Exception:
            continue
        try:
            msg = json.loads(data.decode("utf-8", "ignore"))
        except Exception:
            continue
        if msg.get("t") != "discover" or msg.get("proto") != PROTO:
            continue
        reply = {
            "t": "offer", "proto": PROTO, "v": 2,
            "name": hub.pc_name(),
            "ip": local_ip(),
            "port": int(cfg["listen_port"]),
            "pairRequired": bool(cfg.get("pair_required")),
            "token": "",
            "caps": hub.caps(),
            "input": hub.inputs.status() if hub.inputs is not None else
                     {"enabled": False},
        }
        try:
            sock.sendto(json.dumps(reply, ensure_ascii=False).encode(),
                        (addr[0], addr[1]))
        except Exception:
            pass


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def main() -> int:
    args = sys.argv[1:]
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(load_json(CONFIG_PATH, {}))
    auth = Auth(cfg)

    if args and args[0] == "revoke":
        if len(args) < 2:
            print("用法：python bridge.py revoke <deviceId>")
            return 2
        print("已撤销" if auth.revoke(args[1]) else "未找到该设备")
        return 0

    demo = "--demo" in args
    if "--enable-input" in args:
        cfg["enable_input"] = True
    source = StatusSource(cfg, demo=demo)
    hub = Hub(cfg, source, auth)

    threading.Thread(target=source.run, daemon=True).start()
    threading.Thread(target=hub.broadcaster, daemon=True).start()
    threading.Thread(target=discovery_loop, args=(cfg, hub), daemon=True).start()

    if args and args[0] == "status":
        time.sleep(0.6)
        print(json.dumps(source.snapshot(), ensure_ascii=False, indent=2))
        print(f"在线手机：{len(hub.clients)}，已配对：{len(auth.phones)}")
        return 0

    name = hub.pc_name()
    ip = local_ip()
    print("=" * 62)
    print(f"  AI 状态 · 手机桥接  ({'演示模式' if demo else '连接 AI 服务'})")
    print(f"  本机名称 : {name}")
    print(f"  手机访问 : {ip}:{cfg['listen_port']}")
    print(f"  配对码   : {auth.pin}   （手机端输入这 6 位数字）")
    print(f"  状态来源 : {'内置演示' if demo else cfg['service_url']}")
    if hub.inputs is None:
        print("  远控 V2  : 不可用（非 Windows 或 input_win 加载失败）")
    elif hub.inputs.enabled:
        print(f"  远控 V2  : 已开启，空闲 {int(cfg['input_idle_timeout_sec'])}s 自动关闭")
    else:
        print("  远控 V2  : 未开启（加 --enable-input 启用）")
    print("=" * 62)
    log("等待手机连接…  Ctrl+C 退出")

    # 单实例保护：Windows 的 SO_REUSEADDR 会允许多个进程绑定同一端口，
    # 手机连接会被随机分到不同实例（配对/状态错乱）。占用即退出。
    port = int(cfg["listen_port"])
    probe = socket.socket()
    probe.settimeout(0.3)
    try:
        probe.connect(("127.0.0.1", port))
        log(f"端口 {port} 已被其它桥接服务占用，本实例退出")
        return 1
    except OSError:
        pass
    finally:
        probe.close()

    class _NoReuseHTTPServer(ThreadingHTTPServer):
        allow_reuse_address = False

    handler = make_handler(hub)
    try:
        httpd = _NoReuseHTTPServer((cfg["listen_host"], port), handler)
    except OSError as exc:
        log(f"绑定端口 {port} 失败：{exc}")
        return 1
    httpd.daemon_threads = True
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        log("已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
