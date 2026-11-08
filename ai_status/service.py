"""常驻中间件：本地事件总线（HTTP）+ 状态机 + 设备链路调度。

Hooks 每次事件都会启动一个短命进程，无法持有串口/WS 长连接或维持 5s 心跳，
因此由本服务常驻负责 seq、心跳、重连与粘性状态解除。
"""
from __future__ import annotations

import json
import logging
import socket
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .device import DeviceLink
from .mapper import map_hook
from .state import StateMachine, Transition

# 客户端提前断开（探活超时、浏览器刷新/关闭、脚本取消）在 Windows 上表现为
# ConnectionAbortedError / ConnectionResetError / BrokenPipeError。它们不是服务端
# 错误，只是客户端已走，写响应必然失败。默认 ThreadingHTTPServer 会把 traceback
# 打到 stderr，造成大量噪音；这里统一静默。
_CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


def _port_in_use(host: str, port: int) -> bool:
    """端口是否已有进程在监听（用于单实例保护）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False   # Windows 上 True 会让多实例绑定同一端口

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, _CLIENT_GONE):
            return
        super().handle_error(request, client_address)


class StatusService:
    def __init__(self, cfg: dict, log: logging.Logger | None = None):
        self.cfg = cfg
        self.log = log or logging.getLogger("ai_status")
        self.machine = StateMachine(
            min_dwell_ms=int(cfg.get("min_dwell_ms", 300)),
            done_hold_ms=int(cfg.get("done_hold_ms", 10000)),
            idle_after_ms=int(cfg.get("idle_after_ms", 600000)),
            stalled_after_ms=int(cfg.get("stalled_after_ms", 0)),
            error_hold_ms=int(cfg.get("error_hold_ms", 3000)),
            infer_outputting=bool(cfg.get("infer_outputting", False)),
            outputting_after_ms=int(cfg.get("outputting_after_ms", 3500)),
            lock_human=bool(cfg.get("lock_human", True)),
        )
        self.link = DeviceLink(cfg, self.log)
        self.lock = threading.RLock()
        self.events: deque = deque(maxlen=200)
        self.started = time.monotonic()
        self.last_heartbeat = 0.0
        self.last_push = 0.0
        self.reconnect_at = 0.0
        self.backoff = 1.0
        self._stop = threading.Event()
        self._httpd: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None

    # ---------- 事件入口 ----------

    def handle_event(self, payload: dict, event_hint: str = "") -> dict:
        with self.lock:
            ev = map_hook(payload, event_hint)
            self.events.append({
                "ts": round(time.time(), 3),
                "event": payload.get("hook_event_name") or event_hint,
                "session": ev.session_id,
                "state": ev.state,
                "reason": ev.reason,
                "release_sticky": ev.release_sticky,
                # 诊断用：IDE 的 tool_response 结构不统一，保留原文才能核对失败判定
                "tool": str(payload.get("tool_name") or ""),
                "tool_response": str(payload.get("tool_response"))[:260],
                "keys": list(payload.keys()),
            })
            returns = self._apply(self.machine.update(ev))
            return {"ok": True, "state": self.machine.snapshot(), "push": returns}

    def handle_manual(self, state: str, msg: str = "", force: bool = False) -> dict:
        with self.lock:
            returns = self._apply(self.machine.force_state(state, msg, force))
            return {"ok": True, "state": self.machine.snapshot(), "push": returns}

    def handle_clear(self) -> dict:
        with self.lock:
            returns = self._apply(self.machine.clear_sticky())
            released = self.link.clear() if self.link.connected else None
            return {"ok": True, "state": self.machine.snapshot(),
                    "push": returns, "device_clear": released}

    def _apply(self, transitions: list[Transition]) -> list[dict]:
        out = []
        for tr in transitions:
            reply = self.link.push(tr.state, tr.msg, tr.force, tr.ttl)
            if reply.get("ok") is not False:
                self.last_push = time.monotonic()
                self.last_heartbeat = self.last_push
            self.log.info("状态 -> %-13s %-24s force=%s  %s", tr.state, tr.reason,
                          int(tr.force), P_reply(reply))
            out.append({"state": tr.state, "reply": reply})
        return out

    # ---------- 生命周期 ----------

    def _start_http(self) -> None:
        host = str(self.cfg.get("listen_host", "127.0.0.1"))
        port = int(self.cfg.get("listen_port", 8765))
        # 单实例保护：端口已被占用说明已有服务在跑，直接退出。否则 Windows 的
        # SO_REUSEADDR 会让多个实例同时绑定 8765，互相抢连接、把 /health 拖到超时。
        if _port_in_use(host, port):
            self.log.error("端口 %d 已被占用：检测到已有服务在运行，本实例退出。", port)
            raise SystemExit(0)
        handler = _make_handler(self)
        try:
            self._httpd = QuietThreadingHTTPServer((host, port), handler)
        except OSError as exc:
            self.log.error("绑定端口 %d 失败（可能已有实例）：%s", port, exc)
            raise SystemExit(0)
        self._httpd.daemon_threads = True
        self._server_thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
        self._server_thread.start()
        self.log.info("本地事件总线已启动：http://%s:%d/event（/state /clear /health）",
                      host, port)

    def run(self) -> None:
        self._start_http()
        if self.link.connect(simulate=bool(self.cfg.get("simulate", False))):
            self.backoff = 1.0
            self._apply([Transition("idle", "服务启动", "startup", True, None)])
        else:
            self.reconnect_at = time.monotonic() + self.backoff
        heartbeat_s = float(self.cfg.get("heartbeat_ms", 5000)) / 1000

        while not self._stop.wait(0.1):
            try:
                self._tick_once(heartbeat_s)
            except Exception as exc:  # 主循环不能因单次异常退出
                self.log.warning("主循环异常：%s", exc)
        self.shutdown()

    def _tick_once(self, heartbeat_s: float) -> None:
        # 1) 状态机 tick + 心跳：只在锁内做「快」操作
        with self.lock:
            now = time.monotonic()
            transitions = self.machine.tick(now)
            if transitions:
                self._apply(transitions)
            if self.link.connected and now - self.last_heartbeat >= heartbeat_s:
                reply = self.link.heartbeat(self.machine.current)
                self.last_heartbeat = now
                self.log.debug("心跳 %s seq=%d %s", self.machine.current,
                               self.link.seq, P_reply(reply))
            need_reconnect = (not self.link.connected and now >= self.reconnect_at)
        if not need_reconnect:
            return
        # 2) 设备发现可能要几十秒（串口扫描 / LAN 扫描 / mDNS）。必须放在锁外，
        #    否则 /health 的请求线程会一直拿不到锁而超时。
        self.log.info("尝试重新发现设备（退避 %ss）…", self.backoff)
        ok = self.link.connect(simulate=bool(self.cfg.get("simulate", False)))
        with self.lock:
            if ok:
                self.backoff = 1.0
                self._apply([Transition(self.machine.current,
                                        "重连后补发当前状态", "reconnect", True, None)])
            else:
                self.backoff = min(self.backoff * 2, 8)
                self.reconnect_at = time.monotonic() + self.backoff

    def stop(self) -> None:
        self._stop.set()

    def shutdown(self) -> None:
        with self.lock:
            if self.link.connected:
                self.link.clear()
            self.link.close(send_clear=False)
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        self.log.info("服务已停止")

    # ---------- 查询 ----------

    def health(self) -> dict:
        # 诊断端点不持全局锁：设备推送可能耗时，health 必须始终秒回。
        # 读的是弱一致快照，用于诊断足够，也不阻塞 AI 主流程。
        return {
            "ok": True,
            "version": __version__,
            "proto": "ai-status/1",
            "source_id": self.link.source_id,
            "source_field": self.link.source_field,
            "tag_source_in_msg": self.link.tag_source,
            "uptime_sec": int(time.monotonic() - self.started),
            "state": self.machine.snapshot(),
            "device": {
                "kind": self.link.kind,
                "connected": self.link.connected,
                "port": self.link.port.port if self.link.port else None,
                "ip": self.link.ip,
                "seq": self.link.seq,
                "info": self.link.info,
                "last_error": self.link.last_error,
                "last_frame": self.link.last_frame,
            },
            "last_push_ms": int((time.monotonic() - self.last_push) * 1000)
            if self.last_push else None,
            "event_count": len(self.events),
        }

    def recent_events(self, limit: int = 50) -> list[dict]:
        # 不持锁：deque.append 与 list() 在当前用法下安全
        return list(self.events)[-limit:]


def P_reply(reply: dict) -> str:
    if not reply:
        return ""
    if reply.get("reason"):
        return "reply=" + str(reply.get("reason"))
    return "ok" if reply.get("ok") else "no_reply"


def _make_handler(service: StatusService):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AIStatusHub/1.0"

        def log_message(self, fmt, *args):
            pass

        def _json(self, code: int, obj) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()
                self.wfile.write(body)
            except _CLIENT_GONE:
                # 客户端已断开：写不出去是正常的，不是服务端错误
                pass

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > 65536:
                raise ValueError("payload_too_large")
            return json.loads(self.rfile.read(length).decode("utf-8", "ignore"))

        def do_OPTIONS(self):
            self._json(204, {})

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            try:
                body = self._body()
                if path in ("/event", "/hook"):
                    result = service.handle_event(body)
                elif path == "/state":
                    result = service.handle_manual(
                        str(body.get("state", "")), str(body.get("msg", "")),
                        bool(body.get("force", False)))
                elif path == "/clear":
                    result = service.handle_clear()
                else:
                    return self._json(404, {"ok": False, "reason": "not_found"})
                return self._json(200, result)
            except ValueError as exc:
                return self._json(400, {"ok": False, "reason": "invalid_json",
                                        "detail": str(exc)})
            except Exception as exc:
                return self._json(500, {"ok": False, "reason": "internal_error",
                                        "detail": str(exc)})

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/health", "/api/health"):
                return self._json(200, service.health())
            if path == "/identity":
                return self._json(200, {
                    "ok": True,
                    "source_id": service.link.source_id,
                    "source_field": service.link.source_field,
                    "tag_source_in_msg": service.link.tag_source,
                    "hint": "把 source_id 填进 ESP32 端的绑定白名单即可只接受这台电脑",
                })
            if path == "/events":
                return self._json(200, {"ok": True, "events": service.recent_events()})
            return self._json(404, {"ok": False, "reason": "not_found"})

    return Handler
