"""设备发现与状态上报：串口直连优先，局域网 HTTP 回落，均无设备时进入模拟模式。

依据：API_Doc/AI_STATUS_Protocol.md 第六节、Dev_Notes.md 第三/四/五节。
"""
from __future__ import annotations

import json
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from . import protocol as P
from .serial_win import open_serial

_PS_PORTS = (
    'Get-PnpDevice -Class Ports | ForEach-Object { '
    "if ($_.FriendlyName -match '\\((COM\\d+)\\)') { "
    '"$($matches[1])|$($_.InstanceId)" } }'
)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class PortInfo:
    port: str
    vid_pid: str
    kind: str
    instance: str = ""


def _run(args: list[str], timeout: float = 15) -> str:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                             encoding="utf-8", errors="replace",
                             creationflags=_NO_WINDOW)
        return out.stdout or ""
    except Exception:
        return ""


def _http_json(url: str, payload: dict | None = None, timeout: float = 1.0) -> dict:
    data = None
    if payload is not None:
        data = P.json_dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(16384)
    return json.loads(raw.decode("utf-8", "ignore")) if raw else {}


def list_serial_candidates(cfg: dict, log) -> list[PortInfo]:
    """枚举 VID_303A:1001（原生 USB CDC）与 VID_1A86:55D3（CH343 UART0）。"""
    wanted = {str(v).upper() for v in cfg.get("serial_vid_pid", [])}
    found: list[PortInfo] = []
    try:
        from serial.tools import list_ports  # type: ignore
        for item in list_ports.comports():
            vid = f"{item.vid:04X}:{item.pid:04X}" if item.vid and item.pid else ""
            if vid in wanted:
                found.append(PortInfo(item.device, vid,
                                      "usb_cdc" if vid.startswith("303A") else "uart",
                                      str(item.hwid or "")))
    except ImportError:
        for line in _run(["powershell", "-NoProfile", "-NonInteractive",
                          "-Command", _PS_PORTS], 20).splitlines():
            if "|" not in line:
                continue
            com, instance = line.split("|", 1)
            match = re.search(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})", instance)
            if not match:
                continue
            vid = f"{match.group(1).upper()}:{match.group(2).upper()}"
            if vid in wanted:
                found.append(PortInfo(com.strip(), vid,
                                      "usb_cdc" if vid.startswith("303A") else "uart",
                                      instance.strip()))
    except Exception as exc:
        log.warning("枚举串口失败：%s", exc)
    # 原生 USB CDC 延迟最低，优先探测
    found.sort(key=lambda p: 0 if p.kind == "usb_cdc" else 1)
    return found


def probe_serial(port: PortInfo, cfg: dict, log):
    """打开串口并 ping/pong 握手；只有固件实现 ai-status/1 才算命中。"""
    ser = None
    try:
        ser = open_serial(port.port, int(cfg.get("baud", 115200)), 100)
        time.sleep(0.15)
        ping = P.serial_frame("ping", src=str(cfg.get("source_id") or ""),
                              source_field=str(cfg.get("source_field") or P.SOURCE_FIELD))
        ser.write(P.encode_line(ping))
        deadline = time.monotonic() + float(cfg.get("ping_timeout_ms", 800)) / 1000
        while time.monotonic() < deadline:
            line = ser.read_line(deadline)
            if not line:
                break
            try:
                obj = json.loads(line.decode("utf-8", "ignore"))
            except (ValueError, UnicodeDecodeError):
                continue
            if obj.get("t") == "pong" and obj.get("proto") == P.PROTO:
                return ser, obj
        ser.close()
        return None
    except Exception as exc:
        log.debug("串口 %s 探测失败：%s", port.port, exc)
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        return None


def _local_ipv4() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("223.5.5.5", 80))
        return sock.getsockname()[0]
    except Exception:
        return "192.168.1.100"
    finally:
        sock.close()


def _arp_candidates(mac_prefix: str) -> list[str]:
    prefix = mac_prefix.lower().replace(":", "-")
    ips = []
    for line in _run(["arp", "-a"], 8).splitlines():
        match = re.match(r"\s*(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F-]{17})\b", line)
        if match and match.group(2).lower().startswith(prefix):
            ips.append(match.group(1))
    return ips


def _hello(ip: str, timeout: float) -> dict | None:
    try:
        obj = _http_json(f"http://{ip}/api/hello", timeout=timeout)
        return obj if obj.get("proto") == P.PROTO else None
    except Exception:
        return None


def find_lan(cfg: dict, log) -> tuple[str | None, dict | None]:
    """缓存 IP -> ARP 反查 -> 并发扫 /24 -> AP 兜底。"""
    timeout = float(cfg.get("hello_timeout_ms", 350)) / 1000
    cached = str(cfg.get("cached_ip") or "")
    last_ip_file = Path(str(cfg.get("last_ip_file") or ""))
    if not cached and last_ip_file.is_absolute() and last_ip_file.exists():
        try:
            cached = json.loads(last_ip_file.read_text("utf-8")).get("ip", "")
        except Exception:
            pass

    # v1.5.0 固件注册了 mDNS，IP 变化后优先靠名字找设备（实测可达）
    mdns_host = str(cfg.get("mdns_host") or "")
    candidates: list[str] = [ip for ip in [cached, mdns_host, *(_arp_candidates(
        str(cfg.get("arp_mac_prefix", "8c-fd-49"))))] if ip]
    for ip in candidates:
        info = _hello(ip, timeout)
        if info:
            return ip, info

    if cfg.get("lan_scan", True):
        local = str(cfg.get("subnet") or "").rstrip(".")
        if not local:
            local = ".".join(_local_ipv4().split(".")[:3])
        targets = [f"{local}.{i}" for i in range(1, 255)]
        with ThreadPoolExecutor(max_workers=int(cfg.get("scan_workers", 32))) as pool:
            futures = {pool.submit(_hello, ip, timeout): ip for ip in targets}
            try:
                for future in as_completed(futures, timeout=25):
                    info = future.result()
                    if info:
                        return futures[future], info
            except Exception as exc:
                log.debug("子网扫描结束：%s", exc)
    info = _hello("192.168.4.1", timeout)   # AP 兜底热点
    if info:
        return "192.168.4.1", info
    return None, None


class DeviceLink:
    """维护一条到设备的链路，负责 seq、心跳、clear 与回执处理。"""

    def __init__(self, cfg: dict, log):
        self.cfg = cfg
        self.log = log
        self.kind = "none"          # none / sim / serial / lan
        self.info: dict = {}
        self.port: PortInfo | None = None
        self.ip: str | None = None
        self.ser = None
        self.seq = 0
        self.connected = False
        self.last_error = ""
        self.last_frame: dict | None = None
        self.disabled_warned = False
        self.source_id = str(cfg.get("source_id") or "")
        self.source_field = str(cfg.get("source_field") or P.SOURCE_FIELD)
        self.tag_source = bool(cfg.get("tag_source_in_msg", True))
        self.bind_warned = False

    def _frame(self, kind: str, **kwargs) -> dict:
        """所有串口帧统一注入来源 ID。"""
        return P.serial_frame(kind, src=self.source_id,
                              source_field=self.source_field, **kwargs)

    def _payload(self, state: str, seq: int, **kwargs) -> dict:
        """所有 HTTP 状态请求统一注入来源 ID。"""
        return P.http_payload(state, seq, src=self.source_id,
                              source_field=self.source_field, **kwargs)

    def _tagged(self, msg: str) -> str:
        """msg 会被设备原样带进 WS 广播，因此把来源 ID 写在这里可立即追溯。"""
        if not self.tag_source:
            return P.clamp_msg(msg)
        return P.tag_message(msg, self.source_id)

    def connect(self, simulate: bool = False) -> bool:
        self.close(send_clear=False)
        self.connected = False
        self.kind = "none"
        if simulate:
            self.kind, self.connected = "sim", True
            self.info = {"proto": P.PROTO, "simulated": True}
            self.log.info("模拟模式：状态帧只打印日志，不访问真实设备")
            return True

        if self.cfg.get("prefer_serial", True):
            ports = list_serial_candidates(self.cfg, self.log)
            self.log.info("检测到 %d 个候选串口：%s", len(ports),
                          ", ".join(f"{p.port}({p.vid_pid})" for p in ports) or "无")
            for port in ports:
                hit = probe_serial(port, self.cfg, self.log)
                if hit:
                    self.ser, self.info = hit
                    self.port = port
                    self.kind = "serial"
                    self.connected = True
                    self.log.info("串口直连成功：%s %s fw=%s", port.port, port.kind,
                                  self.info.get("fw"))
                    break
            if not self.connected and ports:
                self.log.info("串口存在但未返回 ai-status/1 pong（固件可能仍是 v1.3），回落局域网")

        if not self.connected:
            self.log.info("未命中串口，开始局域网发现…")
            ip, info = find_lan(self.cfg, self.log)
            if ip:
                self.ip, self.info = ip, info or {}
                self.kind = "lan"
                self.connected = True
                self.log.info("局域网设备：%s fw=%s ip=%s", ip, self.info.get("fw"),
                              self.info.get("ip"))
                self._save_ip(ip)

        if self.connected:
            self._resync_seq()
        else:
            self.last_error = ("未检测到 ESP32-S3；本机也未实现 ai-status/1。"
                               "可连接 AP：ESP32S3-Fallback / 12345678 / 192.168.4.1")
            self.log.warning(self.last_error)
        return self.connected

    def _save_ip(self, ip: str) -> None:
        path = Path(str(self.cfg.get("last_ip_file") or ""))
        if not path.is_absolute():
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"ip": ip}), "utf-8")
        except Exception:
            pass

    def _mark_disconnected(self, exc: Exception) -> None:
        self.last_error = str(exc)
        self.connected = False
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self.log.warning("设备链路断开：%s", exc)

    def _read_serial_until(self, wanted: set[str], timeout_ms: int = 400) -> dict:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            line = self.ser.read_line(deadline)
            if not line:
                break
            try:
                obj = json.loads(line.decode("utf-8", "ignore"))
            except (ValueError, UnicodeDecodeError):
                continue
            if obj.get("t") in wanted:
                return obj
        return {"ok": None, "no_reply": True}

    def _send_serial(self, frame: dict, wanted: set[str] | None = None) -> dict:
        try:
            self.last_frame = frame
            self.ser.drain()
            self.ser.write(P.encode_line(frame))
            return self._read_serial_until(wanted or {"ack"}, 350)
        except Exception as exc:
            self._mark_disconnected(exc)
            return {"ok": False, "reason": "transport_error", "detail": str(exc)}

    def _send_http(self, payload: dict | None = None, timeout: float | None = None) -> dict:
        try:
            self.last_frame = payload
            return _http_json(f"http://{self.ip}/api/status", payload,
                              timeout or float(self.cfg.get("http_timeout_ms", 1500)) / 1000)
        except Exception as exc:
            self._mark_disconnected(exc)
            return {"ok": False, "reason": "transport_error", "detail": str(exc)}

    def _handle_reply(self, reply: dict) -> dict:
        reason = reply.get("reason")
        if reason == "invalid_seq":
            self._resync_seq()
        elif reason == "disabled" and not self.disabled_warned:
            self.disabled_warned = True
            self.log.warning("设备状态引擎未启用，请在网页面板打开「AI 状态灯」或发 status_enable")
        elif reason == "source_not_bound" and not self.bind_warned:
            self.bind_warned = True
            self.log.warning("本机来源 %s 未被 ESP32 绑定：请在设备网页「来源与绑定」勾选，"
                             "或切换为「全部接受」", self.source_id)
        elif reason == "lower_priority":
            self.log.debug("设备保留更高优先级状态：%s", reply.get("state"))
        return reply

    def push(self, state: str, msg: str = "", force: bool = False,
             ttl: int | None = None) -> dict:
        if not self.connected:
            return {"ok": False, "reason": "not_connected"}
        framed_msg = self._tagged(msg)
        # 设备会在 offline / done 回落等内部转移时自增 seq，PC 端 seq 可能落后；
        # 收到 invalid_seq 后重新同步并补发一次，保证状态不丢。
        for attempt in range(2):
            self.seq += 1
            frame = self._frame("status", state=state, seq=self.seq,
                                msg=framed_msg, ttl=ttl, force=force)
            if self.kind == "serial":
                reply = self._send_serial(frame, {"ack"})
            elif self.kind == "lan":
                reply = self._send_http(self._payload(state, self.seq,
                                                       msg=framed_msg, ttl=ttl,
                                                       force=force))
            else:
                self.last_frame = frame
                self.log.info("[SIM] %s", P.json_dumps(frame))
                reply = {"ok": True, "simulated": True}
            if reply.get("reason") == "invalid_seq" and attempt == 0:
                self.log.info("设备 seq 已前进，重新同步后补发 %s", state)
                self._resync_seq()
                continue
            break
        return self._handle_reply(reply)

    def heartbeat(self, state: str) -> dict:
        if not self.connected:
            return {"ok": False, "reason": "not_connected"}
        self.seq += 1
        if self.kind == "serial":
            frame = self._frame("heartbeat", state=state, seq=self.seq)
            self.last_frame = frame
            try:
                self.ser.write(P.encode_line(frame))
            except Exception as exc:
                self._mark_disconnected(exc)
                return {"ok": False, "reason": "transport_error"}
        elif self.kind == "lan":
            reply = self._send_http(self._payload(state, self.seq))
            return self._handle_reply(reply)
        else:
            self.log.debug("[SIM] heartbeat %s seq=%d src=%s", state,
                           self.seq, self.source_id)
        return {"ok": True}

    def clear(self) -> dict:
        if not self.connected:
            return {"ok": False, "reason": "not_connected"}
        self.seq += 1
        if self.kind == "serial":
            return self._send_serial(self._frame("clear"), {"ack", "status"})
        if self.kind == "lan":
            return self._send_http(self._payload("idle", self.seq, force=True))
        self.log.info("[SIM] clear src=%s", self.source_id)
        return {"ok": True, "simulated": True}

    def status_snapshot(self) -> dict:
        if not self.connected:
            return {}
        try:
            if self.kind == "serial":
                return self._send_serial(self._frame("get"), {"status"})
            if self.kind == "lan":
                return _http_json(f"http://{self.ip}/api/status", timeout=1.0)
        except Exception as exc:
            self.log.debug("读取设备状态失败：%s", exc)
        return {}

    def _resync_seq(self) -> None:
        snap = self.status_snapshot()
        try:
            device_seq = int(snap.get("seq", 0) or 0)
        except (TypeError, ValueError):
            device_seq = 0
        # 禁止本地 seq 倒退；设备会丢弃 seq <= lastSeq 的帧
        self.seq = max(self.seq, device_seq)
        if self.ser is not None:
            self.ser.drain(0.02)
        self.log.info("seq 同步：本地=%d 设备=%d", self.seq, device_seq)

    def close(self, send_clear: bool = True) -> None:
        if send_clear and self.connected and self.kind != "none":
            try:
                self.clear()
            except Exception:
                pass
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self.connected = False
        self.kind = "none"
