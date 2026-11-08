#!/usr/bin/env python3
"""AI 工作状态灯 · PC 端中间件（CodeBuddy Hooks -> ESP32-S3）

常用命令：
  python main.py start                     启动常驻服务
  python main.py probe                     探测串口 / 局域网设备
  python main.py install-hooks --write     生成并合并 CodeBuddy Hooks 配置
  python main.py emit thinking --msg "测试" 手工发一个状态
  python main.py selftest                  全链路自检（不依赖硬件）
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from ai_status import __version__, protocol as P
from ai_status.device import find_lan, list_serial_candidates, probe_serial
from ai_status.mapper import list_profiles, map_hook
from ai_status.service import StatusService
from ai_status.state import StateMachine

APP_DIR = Path(__file__).resolve().parent

DEFAULT_CONFIG: dict = {
    # 0.0.0.0 = 允许手机/小程序在同一局域网直连 8765；
    # 只想本机使用（更安全）时改回 127.0.0.1
    "listen_host": "0.0.0.0",
    "listen_port": 8765,
    "heartbeat_ms": 5000,
    "min_dwell_ms": 300,
    "done_hold_ms": 10000,          # 完成绿色常亮 10s 后回 idle
    "idle_after_ms": 600000,        # 非工作态长时间无事件回 idle
    "stalled_after_ms": 0,          # 默认关闭：IDE 事件不全，用时间猜「卡住」会误报
    "error_hold_ms": 3000,          # 工具失败红闪的最短显示时长，到期自动消退
    "infer_outputting": False,      # 无「流式输出」事件，默认不靠静默猜测
    "outputting_after_ms": 3500,
    "lock_human": True,             # 等待态建立后不允许普通事件覆盖
    "prefer_serial": True,
    "serial_vid_pid": ["303A:1001", "1A86:55D3"],
    "baud": 115200,
    "ping_timeout_ms": 800,
    "cached_ip": "192.168.1.13",
    "mdns_host": "esp32s3-ai.local",       # v1.5.0 固件 mDNS，IP 变了也能找到
    "arp_mac_prefix": "8c-fd-49",
    "subnet": "",
    "lan_scan": True,
    "scan_workers": 32,
    "hello_timeout_ms": 350,
    "http_timeout_ms": 1500,
    "simulate": False,
    # 手机桥接（HarmonyOS App）：随 start 一起启动，局域网 UDP 发现 + HTTP 服务
    "phone_bridge_enabled": True,
    "phone_bridge_port": 8770,
    "phone_bridge_discovery_port": 4211,
    "phone_bridge_pin": "123456",
    "phone_bridge_pair_required": True,
    "phone_bridge_enable_input": True,
    "phone_bridge_firewall": True,
    "phone_bridge_dir": str(APP_DIR / "tools" / "pc-bridge"),
    "source_id": "",                       # 留空则自动生成并持久化到 client_id.json
    "source_field": "src",                 # 帧里的来源字段名，可随固件约定调整
    "tag_source_in_msg": True,             # 把 [来源ID] 写进 msg，设备会原样广播
    "client_id_file": str(APP_DIR / "client_id.json"),
    "last_ip_file": str(APP_DIR / "last_ip.json"),
    "log_file": str(APP_DIR / "ai_status.log"),
}

# IDE 版当前只发前 7 个；后 5 个属于「有则精确命中、无则忽略」的补强事件，
# 一并注册后，IDE 升级或 CLI 混用时无需重装 Hooks 即可直接拿到确认/提问信号。
IDE_EVENTS = [
    ("SessionStart", "startup"),
    ("UserPromptSubmit", None),
    ("PreToolUse", ".*"),
    ("PostToolUse", ".*"),
    ("Stop", None),
    ("SessionEnd", "other"),
    ("PreCompact", ""),
    ("Notification", ""),
    ("PermissionRequest", ""),
    ("PermissionDenied", ""),
    ("PostToolUseFailure", ".*"),
    ("Elicitation", ""),
]
CLI_EXTRA_EVENTS = [
    ("Notification", ""),
    ("PermissionRequest", ""),
    ("PermissionDenied", ""),
    ("PostToolUseFailure", ".*"),
    ("StopFailure", ""),
    ("SubagentStop", ""),
    ("Elicitation", ""),
    ("ElicitationResult", ""),
]

# OpenAI Codex（CLI / Desktop）的事件（~/.codex/hooks.json）。
# 这些钩子均为观测用途，生成配置时会以 async 方式发送，绝不阻塞 agent。
CODEX_EVENTS = [
    ("SessionStart", None),
    ("SessionEnd", "other"),
    ("UserPromptSubmit", None),
    ("PreModelRequest", None),
    ("PostModelRequest", None),
    ("PreToolUse", ".*"),
    ("PostToolUse", ".*"),
    ("PermissionRequest", ".*"),
    ("Stop", None),
    ("Interrupt", None),
    ("Notification", ""),
    ("PreCompact", ""),
    ("PostCompact", ""),
    ("SubagentStart", ""),
    ("SubagentStop", ""),
]


def resolve_source_id(cfg: dict) -> str:
    """每台电脑一个稳定 ID：配置优先 -> 本地持久化 -> 首次生成(主机名+随机后缀)。

    稳定很重要：ESP32 端按 ID 绑定后，PC 重启不能换 ID，否则白名单失效。
    """
    configured = str(cfg.get("source_id") or "").strip()
    if configured:
        return configured[:32]
    path = Path(str(cfg.get("client_id_file") or ""))
    if not path.is_absolute():
        return "PC"
    try:
        if path.exists():
            saved = json.loads(path.read_text("utf-8")).get("source_id", "")
            if saved:
                return str(saved)[:32]
    except Exception:
        pass
    host = re.sub(r"[^A-Za-z0-9_-]", "-", socket.gethostname()).strip("-")[:20] or "PC"
    generated = f"{host}-{secrets.token_hex(3).upper()}"[:32]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"source_id": generated}, ensure_ascii=False,
                                   indent=2), "utf-8")
    except OSError:
        pass
    return generated


def load_config(args) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    path = Path(args.config) if getattr(args, "config", None) else APP_DIR / "config.json"
    if path.exists():
        try:
            cfg.update(json.loads(path.read_text("utf-8")))
        except Exception as exc:
            print(f"[warn] 配置文件解析失败，使用默认值：{exc}", file=sys.stderr)
    if getattr(args, "port", None):
        cfg["listen_port"] = int(args.port)
    if getattr(args, "simulate", False):
        cfg["simulate"] = True
    cfg["source_id"] = resolve_source_id(cfg)
    return cfg


def setup_logging(cfg: dict, verbose: bool = False) -> logging.Logger:
    log = logging.getLogger("ai_status")
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    log.addHandler(stream)
    if cfg.get("log_file"):
        try:
            handler = logging.FileHandler(str(cfg["log_file"]), encoding="utf-8")
            handler.setFormatter(fmt)
            log.addHandler(handler)
        except OSError:
            pass
    log.propagate = False
    return log


def _post(url: str, payload: dict, timeout: float = 2.0) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read(65536).decode("utf-8", "ignore"))


def _get(url: str, timeout: float = 2.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read(65536).decode("utf-8", "ignore"))


# ---------------- Hook 配置生成 ----------------

def _hook_command(event: str, port: int, profile: str = "codebuddy") -> str:
    exe = sys.executable.replace("\\", "/")
    script = str(APP_DIR / "main.py").replace("\\", "/")
    # codebuddy 是默认 profile，省略参数以兼容旧 settings.json 的去重逻辑
    suffix = f" --profile {profile}" if profile and profile != "codebuddy" else ""
    return f'"{exe}" "{script}" hook --event {event}{suffix} --port {port}'


def _hooks_block(events, port: int, profile: str, *, background: bool = False) -> dict:
    hooks = {}
    for event, matcher in events:
        group: dict = {"hooks": [{"type": "command",
                                  "command": _hook_command(event, port, profile),
                                  "timeout": 5}]}
        if background:
            # 状态灯是旁路观测，不应把网络/串口抖动带入 Codex 的关键路径。
            group["hooks"][0]["async"] = True
        if matcher is not None:
            group["matcher"] = matcher
        hooks[event] = [group]
    return {"hooks": hooks}


def generate_hook_files(cfg: dict) -> dict[str, Path]:
    port = int(cfg.get("listen_port", 8765))
    paths = {
        "ide": APP_DIR / "hooks.settings.ide.json",
        "cli": APP_DIR / "hooks.settings.cli.json",
        "codex": APP_DIR / "hooks.codex.json",
    }
    paths["ide"].write_text(json.dumps(_hooks_block(IDE_EVENTS, port, "codebuddy"),
                                       ensure_ascii=False, indent=2), "utf-8")
    paths["cli"].write_text(json.dumps(_hooks_block(CLI_EXTRA_EVENTS, port, "codebuddy"),
                                       ensure_ascii=False, indent=2), "utf-8")
    paths["codex"].write_text(json.dumps(_hooks_block(CODEX_EVENTS, port, "codex",
                                                         background=True),
                                         ensure_ascii=False, indent=2), "utf-8")
    return paths


DSH_PLUGIN_JS = r'''// aistatus-led :: DeepSeek (DSH) 插件骨架
// 把 DSH 事件总线上的真实事件转发到本地中间件 http://127.0.0.1:8765/event
// 依赖：Node 18+（内置 fetch）。事件回调参数名以你所装 DSH 版本为准，必要时调整下面的取值。
const PORT = process.env.AISTATUS_PORT || 8765
const ENDPOINT = `http://127.0.0.1:${PORT}/event`

function post(payload) {
  const body = JSON.stringify({ _profile: 'dsh', ...payload })
  try {
    const p = fetch(ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
    })
    if (p && typeof p.catch === 'function') p.catch(() => {})
  } catch (_) { /* 中间件未启动时静默跳过，绝不影响 AI 主流程 */ }
}

export const name = 'aistatus-led'

export function apply(ctx) {
  const on = (event, mapper) => {
    try {
      ctx.on(event, (...args) => {
        try { post(mapper(...args)) } catch (_) {}
      })
    } catch (_) {}
  }

  on('session/created', () => ({ hook_event_name: 'session/created' }))
  on('session/ended', () => ({ hook_event_name: 'session/ended' }))
  on('agent/request', () => ({ hook_event_name: 'agent/request' }))

  on('tools/pre-execute', (execution) => ({
    hook_event_name: 'tools/pre-execute',
    tool_name: execution?.tool?.name || execution?.name || execution?.toolName,
    tool_input: execution?.args || execution?.input || execution?.toolInput || {},
  }))
  on('tools/post-execute', (execution, result) => ({
    hook_event_name: 'tools/post-execute',
    tool_name: execution?.tool?.name || execution?.name || execution?.toolName,
    tool_response: result,
  }))

  on('approval/request', () => ({ hook_event_name: 'approval/request' }))
  on('approval/denied', () => ({ hook_event_name: 'approval/denied' }))
  on('turn/end', () => ({ hook_event_name: 'turn/end' }))
}
'''

DSH_PLUGIN_PKG = r'''{
  "name": "aistatus-led-dsh",
  "version": "1.0.0",
  "private": true,
  "type": "module",
  "main": "index.mjs",
  "description": "Forward DeepSeek (DSH) harness events to the AI status LED middleware"
}
'''

DSH_PLUGIN_README = r'''# aistatus-led（DSH 插件）

把 DeepSeek (DSH) 的事件总线转发到本机 AI 状态灯中间件，让 ESP32 灯在 DSH 下也能显示
思考 / 找资料 / 编写 / 需要确认 / 完成 / 失败 等状态。

## 原理

- DSH 是 Cordis 插件架构，插件用 `ctx.on(event, handler)` 订阅真实事件；
- 本插件订阅 `approval/request`、`tools/pre-execute`、`tools/post-execute`、
  `session/created`、`agent/request`、`turn/end`，把事件 POST 到
  `http://127.0.0.1:8765/event`（带 `_profile: dsh`）；
- 中间件按 `profiles/dsh.json` 映射成状态帧，经串口 / WiFi 推给 ESP32。

## 安装

1. 先确保 PC 中间件在跑：`python main.py start`（或已安装自启动）；
2. 把本目录装进 DSH（路径按你本机实际调整）：

   ```bash
   dsh plugin add <本目录绝对路径>
   ```

3. 若你的 DSH 版本事件参数不同，改 `index.mjs` 里 `on(...)` 的取值即可，事件名不用改。

## 端口

默认 8765。中间件换了端口时，设置环境变量 `AISTATUS_PORT`。
'''

def generate_dsh_plugin() -> Path:
    """确保 DSH 插件目录存在。

    tools/dsh-plugin/ 是已按 DSH 真实事件名校正过的正式插件，不能让
    install-hooks 用内置模板把它覆盖回旧骨架；仅当文件缺失时才补建。
    """
    plugin_dir = APP_DIR / "tools" / "dsh-plugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    templates = {"index.mjs": DSH_PLUGIN_JS, "package.json": DSH_PLUGIN_PKG,
                 "README.md": DSH_PLUGIN_README}
    for name, content in templates.items():
        target = plugin_dir / name
        if not target.exists():
            target.write_text(content, "utf-8")
    return plugin_dir


def merge_hooks(settings_path: Path, block: dict, logger=print) -> Path | None:
    existing: dict = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"已有 settings.json 无法解析：{exc}") from exc
        backup = settings_path.with_name(
            settings_path.name + ".bak-" + time.strftime("%Y%m%d%H%M%S"))
        shutil.copy2(settings_path, backup)
        logger(f"已备份原配置：{backup}")
    dest = existing.setdefault("hooks", {})
    for event, groups in block["hooks"].items():
        arr = dest.setdefault(event, [])
        for group in groups:
            command = group["hooks"][0]["command"]
            known = [g.get("hooks", [{}])[0].get("command")
                     for g in arr if isinstance(g, dict) and g.get("hooks")]
            if command not in known:
                arr.append(group)
        if not arr:
            dest.pop(event, None)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), "utf-8")
    return settings_path


# ---------------- 手机桥接（HarmonyOS App） ----------------

def phone_bridge_paths(cfg: dict) -> tuple[Path, Path, Path]:
    d = Path(str(cfg.get("phone_bridge_dir") or APP_DIR / "tools" / "pc-bridge"))
    return d, d / "bridge.py", d / "bridge_config.json"


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _tcp_open(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()


def write_bridge_config(cfg: dict) -> dict:
    """把中间件配置写成 tools/pc-bridge/bridge_config.json（固定密钥/端口/状态源）。"""
    _, _, path = phone_bridge_paths(cfg)
    data = {
        "listen_host": "0.0.0.0",
        "listen_port": int(cfg.get("phone_bridge_port", 8770)),
        "discovery_port": int(cfg.get("phone_bridge_discovery_port", 4211)),
        "pc_name": "",
        "service_url": f"http://127.0.0.1:{int(cfg.get('listen_port', 8765))}/health",
        "poll_ms": 500,
        "pair_required": bool(cfg.get("phone_bridge_pair_required", True)),
        "pin": str(cfg.get("phone_bridge_pin") or ""),
        "heartbeat_ms": 5000,
        "enable_input": bool(cfg.get("phone_bridge_enable_input", False)),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    return data


def _netsh(args: list[str]) -> tuple[bool, str]:
    try:
        p = subprocess.run(["netsh"] + args, capture_output=True, text=True, timeout=8)
        return p.returncode == 0, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:
        return False, str(exc)


def ensure_firewall_rules(cfg: dict) -> dict:
    """放行 TCP 服务端口与 UDP 发现端口；需要管理员，失败不阻断启动。"""
    if sys.platform != "win32":
        return {"ok": False, "reason": "not_windows"}
    api = int(cfg.get("listen_port", 8765))
    tcp = int(cfg.get("phone_bridge_port", 8770))
    udp = int(cfg.get("phone_bridge_discovery_port", 4211))
    result = {}
    for name, proto, port in (("AIStatusLed-TCP-API", "TCP", api),
                              ("AIStatusPhoneBridge-TCP", "TCP", tcp),
                              ("AIStatusPhoneBridge-UDP", "UDP", udp)):
        exists, _ = _netsh(["advfirewall", "firewall", "show", "rule", f"name={name}"])
        if exists:
            result[name] = "exists"
            continue
        ok, _ = _netsh(["advfirewall", "firewall", "add", "rule", f"name={name}",
                        "dir=in", "action=allow", f"protocol={proto}",
                        f"localport={port}"])
        result[name] = "added" if ok else "need_admin"
    return result


class PhoneBridgeManager:
    """把 tools/pc-bridge/bridge.py 作为子进程随中间件一起启停。"""

    def __init__(self, cfg: dict, log: logging.Logger):
        self.cfg, self.log = cfg, log
        self.proc: subprocess.Popen | None = None
        self.stop_evt = threading.Event()
        self.thread: threading.Thread | None = None

    def _spawn(self):
        d, script, _ = phone_bridge_paths(self.cfg)
        if not script.exists():
            return None
        kwargs: dict = {"cwd": str(d), "stdout": subprocess.DEVNULL,
                        "stderr": subprocess.DEVNULL}
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return subprocess.Popen([sys.executable, str(script)], **kwargs)

    def start(self) -> None:
        if not self.cfg.get("phone_bridge_enabled", True):
            self.log.info("手机桥接已关闭（phone_bridge_enabled=false）")
            return
        _, script, _ = phone_bridge_paths(self.cfg)
        if not script.exists():
            self.log.warning("手机桥接未启用：缺少 %s", script)
            return
        bcfg = write_bridge_config(self.cfg)
        if self.cfg.get("phone_bridge_firewall", True):
            self.log.info("手机桥接防火墙规则：%s", ensure_firewall_rules(self.cfg))
        if _tcp_open(int(bcfg["listen_port"])):
            self.log.warning("端口 %s 已被其它桥接服务占用，集成版未启动；"
                             "请关闭旧的 start_bridge.bat / bridge.py 后重启本服务",
                             bcfg["listen_port"])
            return
        self.proc = self._spawn()
        if self.proc is None:
            return
        self.log.info("手机桥接已启动：http://%s:%s  UDP 发现 :%s  PIN=%s",
                      lan_ip(), bcfg["listen_port"], bcfg["discovery_port"],
                      bcfg["pin"] or "(随机)")
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()

    def _watch(self) -> None:
        while not self.stop_evt.wait(5):
            if self.proc is not None and self.proc.poll() is not None:
                # 端口已被别的服务占用时不要再拉一个，避免双实例抢连接
                if _tcp_open(int(self.cfg.get("phone_bridge_port", 8770))):
                    continue
                self.log.warning("手机桥接子进程退出，自动重启")
                self.proc = self._spawn()

    def stop(self) -> None:
        self.stop_evt.set()
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except Exception:
                self.proc.kill()
            self.log.info("手机桥接已停止")


# ---------------- 子命令 ----------------

def cmd_start(args) -> int:
    cfg = load_config(args)
    log = setup_logging(cfg, args.verbose)
    bridge = PhoneBridgeManager(cfg, log)
    bridge.start()
    svc = StatusService(cfg, log)
    print("=" * 66)
    print(f" AI 工作状态灯中间件 v{__version__}  协议 {P.PROTO}")
    print(f" 事件总线  http://{cfg['listen_host']}:{cfg['listen_port']}/event")
    print(f" 设备策略  {'模拟模式' if cfg['simulate'] else '串口优先 -> 局域网回落'}")
    if cfg.get("phone_bridge_enabled", True):
        print(f" 手机桥接  http://{lan_ip()}:{cfg.get('phone_bridge_port', 8770)}"
              f"   UDP 发现 :{cfg.get('phone_bridge_discovery_port', 4211)}"
              f"   PIN {cfg.get('phone_bridge_pin') or '(随机)'}")
    print(" 停止服务  Ctrl+C（会释放 LED 并停止手机桥接）")
    print("=" * 66)
    try:
        svc.run()
    except KeyboardInterrupt:
        log.info("收到中断，正在释放设备 LED…")
        svc.shutdown()
    finally:
        bridge.stop()
    return 0


def cmd_phone_bridge(args) -> int:
    """手机桥接运维：status / firewall / pin <6位>。"""
    cfg = load_config(args)
    action = str(args.action or "status")

    if action == "firewall":
        result = ensure_firewall_rules(cfg)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if any(v == "need_admin" for v in result.values()):
            print("部分规则未能添加，请用管理员身份重跑："
                  "python main.py phone-bridge firewall", file=sys.stderr)
            return 1
        return 0

    if action == "pin":
        pin = str(args.value or "").strip()
        if not (pin.isdigit() and len(pin) == 6):
            print("配对码必须是 6 位数字，例如：python main.py phone-bridge pin 123456",
                  file=sys.stderr)
            return 2
        cfg_path = (Path(args.config) if getattr(args, "config", None)
                    else APP_DIR / "config.json")
        data = {}
        if cfg_path.exists():
            try:
                data = json.loads(cfg_path.read_text("utf-8"))
            except Exception:
                data = {}
        data["phone_bridge_pin"] = pin
        cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        cfg["phone_bridge_pin"] = pin
        write_bridge_config(cfg)
        print(f"配对码已设为 {pin}（{cfg_path}）")
        print("重启服务后生效：python main.py start")
        return 0

    d, script, cpath = phone_bridge_paths(cfg)
    port = int(cfg.get("phone_bridge_port", 8770))
    if script.exists():
        write_bridge_config(cfg)
    pin_file = d / "pin.txt"
    pin_in_use = ""
    if pin_file.exists():
        try:
            pin_in_use = pin_file.read_text("utf-8").strip()
        except OSError:
            pass
    print(json.dumps({
        "enabled": bool(cfg.get("phone_bridge_enabled", True)),
        "bridge_dir": str(d),
        "script_exists": script.exists(),
        "config_file": str(cpath),
        "lan_ip": lan_ip(),
        "phone_url": f"http://{lan_ip()}:{port}",
        "http_port": port,
        "discovery_udp_port": int(cfg.get("phone_bridge_discovery_port", 4211)),
        "pin": str(cfg.get("phone_bridge_pin") or ""),
        "pin_file": str(pin_file),
        "pin_in_use": pin_in_use,
        "pair_required": bool(cfg.get("phone_bridge_pair_required", True)),
        "http_listening": _tcp_open(port),
        "firewall": "run: python main.py phone-bridge firewall",
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_hook(args) -> int:
    """被 CodeBuddy Hooks 调用：stdin JSON -> 本地事件总线。永不阻断 AI。"""
    payload: dict = {}
    try:
        raw = sys.stdin.read()
        if raw.strip():
            payload = json.loads(raw)
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if args.event:
        payload.setdefault("hook_event_name", args.event)
    if getattr(args, "profile", ""):
        payload.setdefault("_profile", args.profile)
    try:
        _post(f"http://127.0.0.1:{args.port}/event", payload, timeout=0.4)
    except Exception:
        pass  # daemon 未启动时静默跳过，绝不影响 AI 主流程
    sys.stdout.write(json.dumps({"continue": True, "suppressOutput": True}))
    return 0


def cmd_probe(args) -> int:
    cfg = load_config(args)
    log = setup_logging(cfg, args.verbose)
    report: dict = {"python": sys.version.split()[0], "protocol": P.PROTO,
                    "serial_candidates": [], "serial_hits": [], "lan": None}
    ports = list_serial_candidates(cfg, log)
    report["serial_candidates"] = [
        {"port": p.port, "vid_pid": p.vid_pid, "kind": p.kind} for p in ports]
    for port in ports:
        hit = probe_serial(port, cfg, log)
        if hit:
            ser, info = hit
            ser.close()
            report["serial_hits"].append({"port": port.port, "pong": info})
    if not report["serial_hits"] and not args.no_lan:
        ip, info = find_lan(cfg, log)
        report["lan"] = {"ip": ip, "hello": info} if ip else None
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_emit(args) -> int:
    try:
        result = _post(f"http://127.0.0.1:{args.port}/state",
                       {"state": args.state, "msg": args.msg,
                        "force": bool(args.force)})
    except Exception as exc:
        print(f"发送失败（服务是否已启动？）：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_clear(args) -> int:
    try:
        result = _post(f"http://127.0.0.1:{args.port}/clear", {})
    except Exception as exc:
        print(f"发送失败（服务是否已启动？）：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_identity(args) -> int:
    """打印这台电脑的来源 ID，供 ESP32 端绑定白名单使用。"""
    cfg = load_config(args)
    sid = cfg["source_id"]
    field = str(cfg.get("source_field") or P.SOURCE_FIELD)
    msg = (P.tag_message("正在分析需求", sid)
           if cfg.get("tag_source_in_msg", True) else "正在分析需求")
    print(json.dumps({
        "source_id": sid,
        "source_field": field,
        "tag_source_in_msg": bool(cfg.get("tag_source_in_msg", True)),
        "client_id_file": cfg["client_id_file"],
        "serial_frame_example": P.serial_frame("status", state="thinking", seq=1,
                                               msg=msg, src=sid, source_field=field),
        "http_body_example": P.http_payload("thinking", 1, msg=msg,
                                            src=sid, source_field=field),
        "hint": "把 source_id 填进 ESP32 绑定白名单即可只接受这台电脑；多台电脑各填各的",
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_hooks_config(args) -> int:
    cfg = load_config(args)
    paths = generate_hook_files(cfg)
    for name, path in paths.items():
        print(f"[{name}] {path}")
    print(paths["ide"].read_text("utf-8"))
    return 0


def cmd_install_hooks(args) -> int:
    cfg = load_config(args)
    paths = generate_hook_files(cfg)
    harness = str(getattr(args, "harness", "codebuddy") or "codebuddy").lower()
    print(f"已生成 IDE 片段    ：{paths['ide']}")
    print(f"已生成 CLI 片段    ：{paths['cli']}")
    print(f"已生成 Codex 片段  ：{paths['codex']}")
    if not args.write:
        print("\n（预演模式）确认无误后加 --write 写入：")
        print("  python main.py install-hooks --write --harness codebuddy")
        print("  python main.py install-hooks --write --harness codex")
        print("  python main.py install-hooks --write --harness dsh")
        print("  python main.py install-hooks --write --harness all")
        return 0
    targets = ["codebuddy", "codex", "dsh"] if harness == "all" else [harness]
    ok = 0
    for target in targets:
        try:
            if target == "codebuddy":
                root = Path(args.project_root) if args.project_root else Path.cwd()
                settings_path = (Path.home() / ".codebuddy" / "settings.json"
                                 if args.scope == "user"
                                 else root / ".codebuddy" / "settings.json")
                ide = json.loads(paths["ide"].read_text("utf-8"))
                if args.with_cli:
                    ide["hooks"].update(json.loads(paths["cli"].read_text("utf-8"))["hooks"])
                written = merge_hooks(settings_path, ide)
                print(f"[codebuddy] 已写入：{written}")
                print("  请重启 CodeBuddy 会话使 Hooks 生效（CLI 还需在 /hooks 面板审核）。")
            elif target == "codex":
                codex_path = Path.home() / ".codex" / "hooks.json"
                block = json.loads(paths["codex"].read_text("utf-8"))
                written = merge_hooks(codex_path, block)
                print(f"[codex] 已写入：{written}")
                print("  请重启 Codex 会话使 hooks 生效。")
            elif target == "dsh":
                plugin_dir = generate_dsh_plugin()
                print(f"[dsh] 已生成插件：{plugin_dir}")
                print(f"  安装：dsh plugin add \"{plugin_dir}\"")
            else:
                print(f"未知 harness：{target}", file=sys.stderr)
                continue
            ok += 1
        except Exception as exc:
            print(f"[{target}] 失败：{exc}", file=sys.stderr)
    return 0 if ok == len(targets) else 1


def cmd_demo(args) -> int:
    """真机验收：按状态表依次点亮，最后发 clear 释放 LED。"""
    cfg = load_config(args)
    log = setup_logging(cfg, args.verbose)
    svc = StatusService(cfg, log)
    worker = threading.Thread(target=svc.run, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{cfg['listen_port']}"
    try:
        health = None
        for _ in range(150):
            try:
                health = _get(base + "/health", 0.5)
                if ((health["device"]["connected"] or health["device"]["last_error"])
                        and health["last_push_ms"] is not None):
                    break
            except Exception:
                pass
            time.sleep(0.2)
        if not health:
            raise RuntimeError("服务未就绪")
        device = health["device"]
        print(f"设备通道：{device['kind']}  connected={device['connected']}  "
              f"port={device['port']}  info={json.dumps(device['info'], ensure_ascii=False)}")
        sequence = [
            ("thinking", "思考中"), ("searching", "找资料"),
            ("coding", "脚本编写中"), ("outputting", "输出中"),
            ("need_confirm", "需要确定"), ("need_input", "需要回复信息"),
            ("done", "完成"),
        ]
        for state, label in sequence:
            result = _post(base + "/state",
                           {"state": state, "msg": label, "force": True})
            reply = result["push"][0]["reply"] if result.get("push") else {}
            print(f"  {state:<13} {label:<8} -> {json.dumps(reply, ensure_ascii=False)}")
            time.sleep(args.dwell / 1000)
        _post(base + "/clear", {})
        print("已发送 clear，LED 释放并回到 idle")
    finally:
        svc.stop()
        worker.join(timeout=5)
    return 0


def cmd_selftest(args) -> int:
    """不依赖硬件/网络的全链路自检。"""
    checks = 0

    def check(cond: bool, label: str) -> None:
        nonlocal checks
        if not cond:
            raise AssertionError(label)
        checks += 1
        print(f"  PASS  {label}")

    print("[1/4] Hooks 事件映射（零猜测：只认真实事件与明确字段）")
    cases = [
        ({"hook_event_name": "SessionStart"}, "idle"),
        ({"hook_event_name": "UserPromptSubmit", "prompt": "写代码"}, "thinking"),
        ({"hook_event_name": "PreToolUse", "tool_name": "web_search"}, "searching"),
        ({"hook_event_name": "PreToolUse", "tool_name": "WebFetch"}, "searching"),
        ({"hook_event_name": "PreToolUse", "tool_name": "read_file"}, "searching"),
        ({"hook_event_name": "PreToolUse", "tool_name": "execute_command"}, "coding"),
        ({"hook_event_name": "PreToolUse", "tool_name": "write_to_file"}, "coding"),
        ({"hook_event_name": "PreToolUse", "tool_name": "Bash",
          "tool_input": {"requires_approval": True}}, "need_confirm"),
        ({"hook_event_name": "PreToolUse", "tool_name": "Bash",
          "tool_input": {"requiresApproval": True}}, "need_confirm"),
        ({"hook_event_name": "PreToolUse", "tool_name": "Bash",
          "tool_input": {"autoApprove": False}}, "need_confirm"),
        ({"hook_event_name": "PreToolUse", "tool_name": "Bash",
          "tool_input": {"permission": "ask"}}, "need_confirm"),
        ({"hook_event_name": "PreToolUse",
          "tool_name": "ask_followup_question"}, "need_input"),
        ({"hook_event_name": "PreToolUse", "tool_name": "",
          "tool_input": {"questions": [{"q": "选哪个?"}]}}, "need_input"),
        ({"hook_event_name": "PostToolUse", "tool_name": "Bash"}, "thinking"),
        ({"hook_event_name": "PostToolUse", "tool_name": "Bash",
          "tool_response": {"success": False}}, "error"),
        ({"hook_event_name": "PostToolUse", "tool_name": "Read",
          "tool_response": "Error calling tool: Could not find file 'x'."}, "error"),
        ({"hook_event_name": "PostToolUse", "tool_name": "Read",
          "tool_response": "Error calling tool: permission denied"}, "error"),
        ({"hook_event_name": "PostToolUse", "tool_name": "Read",
          "tool_response": "Contents of x. error handling notes."}, "thinking"),
        ({"hook_event_name": "Notification",
          "notification_type": "permission_prompt"}, "need_confirm"),
        ({"hook_event_name": "Notification",
          "notification_type": "idle_prompt"}, "need_input"),
        ({"hook_event_name": "PermissionRequest"}, "need_confirm"),
        ({"hook_event_name": "Elicitation"}, "need_input"),
        ({"hook_event_name": "Stop"}, "done"),
        ({"hook_event_name": "PreToolUse", "tool_name": "Bash",
          "ai_status_state": "outputting"}, "outputting"),
    ]
    for payload, expected in cases:
        check(map_hook(payload).state == expected, f"{payload['hook_event_name']} -> {expected}")
    check(map_hook({"hook_event_name": "PreToolUse", "tool_name": ""}).state == "thinking",
          "tool_name 为空归为思考，不猜成需要人处理")
    check(map_hook({"hook_event_name": "PostToolUse"}).state == "thinking",
          "工具完成回到思考（紫色呼吸）")
    check(map_hook({"hook_event_name": "PostToolUse"}).release_sticky is True,
          "PostToolUse 是等待态的解除信号")
    stop_ev = map_hook({"hook_event_name": "Stop"})
    check(stop_ev.state == "done" and stop_ev.release_sticky is True,
          "Stop 上报完成并解除等待态")
    check(map_hook({"hook_event_name": "UserPromptSubmit"}).release_sticky is True,
          "UserPromptSubmit 代表用户已回复")

    print("[2/4] 协议帧")
    frame = P.serial_frame("status", "thinking", 7, "测试", 15000, True)
    check(frame["t"] == "status" and frame["v"] == 1 and frame["seq"] == 7
          and frame["force"] == 1, "串口帧字段")
    body = P.http_payload("need_confirm", 8, "确认?", 0, True)
    check("t" not in body and "v" not in body and body["force"] == 1, "HTTP 请求体字段")
    check(len(P.encode_line(frame).decode("utf-8").strip()) < 512, "串口单行长度")
    check(P.is_sticky("error") and not P.is_sticky("done"), "粘性状态判定")
    check(P.HUMAN_STATES == {"need_confirm", "need_input"}
          and P.priority("need_confirm") > P.priority("done"),
          "等待态优先级高于全部普通状态")
    check(P.default_ttl("done") == 10000, "完成态 TTL 10 秒随帧下发给固件")
    tagged = P.serial_frame("status", "thinking", 9, src="PC-A")
    check(tagged["src"] == "PC-A", "串口帧携带来源 ID")
    tagged_body = P.http_payload("thinking", 9,
                                 msg=P.tag_message("测试", "PC-A"), src="PC-A")
    check(tagged_body["src"] == "PC-A"
          and tagged_body["msg"].startswith("[PC-A]"), "HTTP 帧与 msg 携带来源 ID")

    print("[3/4] 状态机：零误报、完成 10s、卡住归红色错误")

    def new_machine(**kw) -> StateMachine:
        base = {"min_dwell_ms": 0, "infer_outputting": False}
        base.update(kw)
        return StateMachine(**base)

    # 1) 授权请求 -> need_confirm；无关通知不覆盖，用户处理完立即解除
    machine = new_machine()
    machine.update(map_hook({"hook_event_name": "Notification",
                             "notification_type": "permission_prompt", "session_id": "s1"}))
    check(machine.current == "need_confirm", "授权请求转 need_confirm")
    machine.update(map_hook({"hook_event_name": "PreCompact", "session_id": "s1"}))
    check(machine.current == "need_confirm", "无关通知不覆盖等待态")
    check(machine.snapshot()["waiting_for_human"] is True, "等待态被标记")

    tr = machine.update(map_hook({"hook_event_name": "PreToolUse",
                                  "tool_name": "read_file", "session_id": "s1"}))
    check(machine.current == "searching" and machine.sticky is None,
          "确认后新的 PreToolUse 立即解除等待态（不再一直橙闪）")
    check(bool(tr) and tr[0].force, "解除时 force 覆盖设备端高优先级")

    # 1b) Stop 结束本轮也必须解除等待态
    stop_m = new_machine()
    stop_m.update(map_hook({"hook_event_name": "PermissionRequest", "session_id": "s9"}))
    check(stop_m.current == "need_confirm", "PermissionRequest -> need_confirm")
    stop_m.update(map_hook({"hook_event_name": "Stop", "session_id": "s9"}))
    check(stop_m.current == "done" and stop_m.sticky is None,
          "Stop 结束本轮时解除等待态并进入完成")

    # 1c) 用户新需求解除
    rel = new_machine()
    rel.update(map_hook({"hook_event_name": "Elicitation", "session_id": "s8"}))
    check(rel.current == "need_input", "Elicitation -> need_input")
    tr = rel.update(map_hook({"hook_event_name": "UserPromptSubmit",
                              "session_id": "s8", "prompt": "继续"}))
    check(rel.current == "thinking" and bool(tr) and tr[0].force,
          "用户回复解除粘性并 force 覆盖设备优先级")

    # 2) 核心回归：深度思考 / 长时间静默绝不误报「需要人处理」
    quiet_m = new_machine(stalled_after_ms=0, idle_after_ms=10 ** 9)
    quiet_m.update(map_hook({"hook_event_name": "UserPromptSubmit",
                             "session_id": "s2", "prompt": "请深度分析"}))
    for sec in (1, 10, 60, 300):
        quiet_m.tick(time.monotonic() + sec)
        check(quiet_m.current == "thinking"
              and quiet_m.sticky is None
              and quiet_m.snapshot()["waiting_for_human"] is False,
              f"静默 {sec}s 仍是 thinking，不误报 need_input/need_confirm")

    # 3) IDE 实测 PreToolUse 远多于 PostToolUse：缺完成事件也不能误判
    mismatch = new_machine(stalled_after_ms=0, idle_after_ms=10 ** 9)
    mismatch.update(map_hook({"hook_event_name": "PreToolUse",
                              "tool_name": "Grep", "session_id": "s3"}))
    check(mismatch.current == "searching", "检索工具 -> searching 黄色闪")
    mismatch.tick(time.monotonic() + 120)
    check(mismatch.current == "searching" and mismatch.sticky is None,
          "PreToolUse 后 120s 无 PostToolUse 也不误报确认")

    # 4) 完成 10s -> idle（force 压过设备端 done(40)->idle(10) 降级检查）
    done_m = new_machine(done_hold_ms=10000, stalled_after_ms=0)
    done_m.update(map_hook({"hook_event_name": "Stop"}))
    check(done_m.current == "done", "Stop -> done 绿色完成态")
    check(not done_m.tick(time.monotonic() + 9), "done 9s 内保持不变")
    tr = done_m.tick(time.monotonic() + 11)
    check(any(t.state == "idle" and t.force for t in tr),
          "done 10s 到期 force 回 idle")

    # 5) 唯一保留的超时：卡住 3 分钟 -> error 红闪，而不是「等人」
    stalled = new_machine(stalled_after_ms=180000, idle_after_ms=10 ** 9)
    stalled.update(map_hook({"hook_event_name": "UserPromptSubmit", "prompt": "长任务"}))
    check(not stalled.tick(time.monotonic() + 179), "卡住 179s 不报警")
    tr = stalled.tick(time.monotonic() + 181)
    check(any(t.state == "error" and t.force for t in tr),
          "卡住 3 分钟 -> error 红色快闪")
    check(stalled.sticky[0] == "error"
          and stalled.snapshot()["waiting_for_human"] is False,
          "处理时间过长属于异常，不冒充需要人确认/回复")
    stalled.update(map_hook({"hook_event_name": "UserPromptSubmit", "prompt": "重试"}))
    check(stalled.sticky is None and stalled.current == "thinking",
          "用户新需求立即解除卡住红闪")

    # 5b) IDE 实测的真实失败格式（纯字符串）必须立刻红闪
    fail_m = new_machine()
    fail_m.update(map_hook({"hook_event_name": "PostToolUse", "tool_name": "Read",
                            "session_id": "f1",
                            "tool_response": "Error calling tool: Could not find file 'x'."}))
    check(fail_m.current == "error" and fail_m.sticky[0] == "error",
          "真实失败字符串 -> error 红闪（v1.3.0 完全识别不到）")
    fail_m.update(map_hook({"hook_event_name": "PreToolUse", "tool_name": "Grep",
                            "session_id": "f1"}))
    check(fail_m.current == "searching" and fail_m.sticky is None,
          "失败后 AI 继续工作，error 立即解除")

    # 5c) 没有后续事件时红闪到期自动消退，不会永久红灯
    fade_m = new_machine(error_hold_ms=1000)
    fade_m.update(map_hook({"hook_event_name": "PostToolUse", "tool_name": "Read",
                            "session_id": "f2",
                            "tool_response": "Error calling tool: boom"}))
    check(fade_m.current == "error", "失败 -> error")
    check(not fade_m.tick(time.monotonic() + 0.5), "红闪至少保持 error_hold_ms")
    tr = fade_m.tick(time.monotonic() + 1.5)
    check(bool(tr) and fade_m.sticky is None and fade_m.current != "error",
          "红闪到期自动消退，不会永久红灯")

    # 5d) 文件内容里含 error 字样不能误判成失败
    ok_m = new_machine()
    ok_m.update(map_hook({"hook_event_name": "PostToolUse", "tool_name": "Read",
                          "session_id": "f3",
                          "tool_response": "Contents: try/except error handling"}))
    check(ok_m.current == "thinking",
          "内容含 error 字样不误报（只认 Error calling tool 前缀）")

    # 6) 需要人处理的三种快闪都只由明确信号触发
    for payload, expected in [
        ({"hook_event_name": "PermissionRequest"}, "need_confirm"),
        ({"hook_event_name": "Elicitation"}, "need_input"),
        ({"hook_event_name": "PostToolUseFailure"}, "error"),
    ]:
        one = new_machine()
        one.update(map_hook(payload))
        check(one.current == expected, f"{payload['hook_event_name']} -> {expected}")

    # 6b) 多 harness profile：Codex / DSH 的专属事件与工具名
    check({"codebuddy", "codex", "dsh"} <= set(list_profiles()), "三套 profile 可加载")
    for payload, expected, label in [
        ({"_profile": "codex", "hook_event_name": "PreToolUse",
          "tool_name": "apply_patch"}, "coding", "Codex apply_patch -> coding"),
        ({"_profile": "codex", "hook_event_name": "PreToolUse", "tool_name": "Bash",
          "tool_input": {"command": "grep -rn x ."}}, "searching", "Codex Bash 检索 -> searching"),
        ({"_profile": "codex", "hook_event_name": "PreToolUse", "tool_name": "Bash",
          "tool_input": {"command": "npm test"}}, "coding", "Codex Bash 执行 -> coding"),
        ({"_profile": "codex", "hook_event_name": "PermissionRequest"}, "need_confirm",
         "Codex PermissionRequest -> need_confirm"),
        ({"_profile": "codex", "hook_event_name": "PreModelRequest"}, "thinking",
         "Codex PreModelRequest -> thinking"),
        ({"_profile": "codex", "hook_event_name": "PostModelRequest", "status": "failed"}, "error",
         "Codex failed model request -> error"),
        ({"_profile": "codex", "hook_event_name": "Interrupt"}, "idle",
         "Codex Interrupt -> idle"),
        ({"_profile": "dsh", "hook_event_name": "approval/request"}, "need_confirm",
         "DSH approval/request -> need_confirm"),
        ({"_profile": "dsh", "hook_event_name": "tools/pre-execute", "tool_name": "bash",
          "tool_input": {"command": "ls -la"}}, "searching", "DSH bash ls -> searching"),
        ({"_profile": "dsh", "hook_event_name": "turn/end"}, "done",
         "DSH turn/end -> done"),
    ]:
        check(map_hook(payload).state == expected, label)

    codex_hooks = _hooks_block(CODEX_EVENTS, 8765, "codex", background=True)["hooks"]
    check({"PreModelRequest", "PostModelRequest", "Interrupt", "SessionEnd"} <= set(codex_hooks),
          "Codex Desktop 工作态事件已注册")
    check(all(groups[0]["hooks"][0].get("async") is True for groups in codex_hooks.values()),
          "Codex 状态钩子后台运行，不阻塞 agent")

    # 6c) DSH 状态闭环：工具完成保持工作态、输出、审批放行、完成/关闭
    dsh_m = new_machine()
    dsh_m.update(map_hook({"_profile": "dsh", "hook_event_name": "tools/pre-execute",
                           "tool_name": "write_file"}))
    check(dsh_m.current == "coding", "DSH 写代码 -> coding")
    dsh_m.update(map_hook({"_profile": "dsh", "hook_event_name": "tools/post-execute",
                           "tool_name": "write_file"}))
    check(dsh_m.current == "coding", "DSH 工具完成保持 coding（不冲回 thinking）")
    dsh_m.update(map_hook({"_profile": "dsh", "hook_event_name": "assistant/message"}))
    check(dsh_m.current == "outputting", "DSH assistant/message -> outputting")
    dsh_m.update(map_hook({"_profile": "dsh", "hook_event_name": "approval/request"}))
    dsh_m.update(map_hook({"_profile": "dsh", "hook_event_name": "approval/decided",
                           "approval_result": "allowed-once"}))
    check(dsh_m.current == "thinking" and dsh_m.sticky is None,
          "DSH 审批放行立即解除橙闪")
    dsh_m.update(map_hook({"_profile": "dsh", "hook_event_name": "turn/end"}))
    check(dsh_m.current == "done", "DSH turn/end -> done")
    dsh_m.update(map_hook({"_profile": "dsh", "hook_event_name": "session/close"}))
    check(dsh_m.current == "idle", "DSH session/close -> idle")

    # 7) clear 强制解除
    locked = new_machine()
    locked.update(map_hook({"hook_event_name": "PermissionRequest"}))
    check(any(t.state == "idle" for t in locked.clear_sticky()), "clear 强制解除等待态")

    print("[4/4] 事件总线端到端（模拟设备）")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({"simulate": True, "listen_port": port, "log_file": "",
                "infer_outputting": False, "source_id": "SELFTEST-PC",
                "stalled_after_ms": 0, "done_hold_ms": 400})
    quiet = logging.getLogger("ai_status_selftest")
    quiet.setLevel(logging.CRITICAL)
    quiet.addHandler(logging.NullHandler())
    svc = StatusService(cfg, quiet)
    worker = threading.Thread(target=svc.run, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(50):
            try:
                ready = _get(base + "/health", 0.5)
                if (ready.get("ok") and ready["device"]["connected"]
                        and ready["last_push_ms"] is not None):
                    break
            except Exception:
                time.sleep(0.1)
        else:
            raise AssertionError("事件总线未就绪")
        check(True, "本地事件总线启动")
        _post(base + "/event", {"hook_event_name": "UserPromptSubmit",
                                "session_id": "selftest", "prompt": "hi"})
        _post(base + "/event", {"hook_event_name": "PreToolUse",
                                "session_id": "selftest", "tool_name": "write_to_file"})
        health = _get(base + "/health")
        check(health["state"]["state"] == "coding", "Hook 事件 -> 状态机 -> coding")
        check(health["device"]["kind"] == "sim" and health["device"]["seq"] >= 2,
              "模拟设备收到状态帧且 seq 递增")
        check(health["source_id"] == "SELFTEST-PC", "health 暴露来源 ID")
        check(_get(base + "/identity")["source_id"] == "SELFTEST-PC",
              "/identity 可查询来源 ID")
        last = health["device"]["last_frame"]
        check(bool(last) and last.get("src") == "SELFTEST-PC"
              and str(last.get("msg", "")).startswith("[SELFTEST-PC]"),
              "状态帧带来源 ID 且 msg 带可追溯标签")
        _post(base + "/state", {"state": "need_input", "msg": "请补充信息"})
        check(_get(base + "/health")["state"]["sticky"] == "need_input", "手工设置粘性状态")
        _post(base + "/clear", {})
        check(_get(base + "/health")["state"]["sticky"] is None, "/clear 解除粘性状态")
        hook_input = json.dumps({"hook_event_name": "Notification",
                                 "notification_type": "permission_prompt",
                                 "session_id": "hook-cli"})
        proc = subprocess.run(
            [sys.executable, str(APP_DIR / "main.py"), "hook",
             "--event", "Notification", "--port", str(port)],
            input=hook_input, text=True, capture_output=True, timeout=15)
        check(json.loads(proc.stdout).get("continue") is True,
              "Hook 客户端返回合法 HookOutput")
        check(_get(base + "/health")["state"]["sticky"] == "need_confirm",
              "Hooks 子进程经 stdin/HTTP 驱动状态机")
        _post(base + "/clear", {})

        # 零猜测回归：PreToolUse 后长时间静默，绝不能误报「需要人处理」
        _post(base + "/event", {"hook_event_name": "PreToolUse",
                                "session_id": "wait", "tool_name": "Grep"})
        time.sleep(1.2)
        health = _get(base + "/health")
        check(health["state"]["state"] == "searching"
              and health["state"]["waiting_for_human"] is False,
              "端到端：长时间静默不误报 need_input/need_confirm")
        check(health["device"]["last_frame"].get("state") == "searching",
              "设备最后一帧仍是找资料，未被等待态抢占")
        _post(base + "/event", {"hook_event_name": "PostToolUse", "session_id": "wait"})
        check(_get(base + "/health")["state"]["state"] == "thinking",
              "端到端：工具完成回到思考（紫色呼吸）")

        # 真实失败格式（IDE 实测是纯字符串）必须立即红闪
        _post(base + "/event", {"hook_event_name": "PostToolUse", "session_id": "fail",
                                "tool_name": "Read",
                                "tool_response": "Error calling tool: Could not find file 'x'."})
        health = _get(base + "/health")
        check(health["state"]["state"] == "error", "端到端：真实失败立即打出 error")
        check(health["device"]["last_frame"].get("state") == "error",
              "端到端：设备最后一帧为 error 红闪")
        _post(base + "/event", {"hook_event_name": "PreToolUse", "session_id": "fail",
                                "tool_name": "Grep"})
        check(_get(base + "/health")["state"]["state"] == "searching",
              "端到端：失败后继续工作，error 立即解除")
        _post(base + "/clear", {})

        # 完成态：Stop -> done（TTL 10s）-> 到期 force 回 idle
        _post(base + "/event", {"hook_event_name": "Stop", "session_id": "wait"})
        health = _get(base + "/health")
        check(health["state"]["state"] == "done", "端到端：Stop -> 完成（绿色）")
        check(health["device"]["last_frame"].get("ttl") == 10000,
              "完成帧携带 10 秒 TTL，固件侧同步生效")
        time.sleep(0.7)
        check(_get(base + "/health")["state"]["state"] == "idle",
              "端到端：完成到期回到空闲（暗色深蓝）")
        _post(base + "/clear", {})
    finally:
        svc.stop()
        worker.join(timeout=5)
    print(f"\n自检完成：{checks} 项全部通过")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CodeBuddy 运行状态 -> ESP32-S3 状态灯中间件（ai-status/1）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    start = sub.add_parser("start", help="启动常驻服务")
    start.add_argument("--config")
    start.add_argument("--port", type=int)
    start.add_argument("--simulate", action="store_true", help="不访问真实设备，只打印状态帧")
    start.add_argument("--verbose", action="store_true")

    hook = sub.add_parser("hook", help="供各 harness Hooks 调用（stdin JSON）")
    hook.add_argument("--port", type=int, default=8765)
    hook.add_argument("--event", default="")
    hook.add_argument("--profile", default="",
                      choices=["", "codebuddy", "codex", "dsh"],
                      help="harness 映射规则；留空自动检测")

    probe = sub.add_parser("probe", help="探测串口 / 局域网 ESP32-S3")
    probe.add_argument("--config")
    probe.add_argument("--verbose", action="store_true")
    probe.add_argument("--no-lan", action="store_true", help="只探测串口")

    emit = sub.add_parser("emit", help="手工向服务上报一个状态")
    emit.add_argument("state", choices=sorted(P.VALID_STATES - {"offline"}))
    emit.add_argument("--msg", default="")
    emit.add_argument("--force", action="store_true")
    emit.add_argument("--port", type=int, default=8765)

    clear = sub.add_parser("clear", help="解除粘性状态并释放 LED")
    clear.add_argument("--port", type=int, default=8765)

    demo = sub.add_parser("demo", help="真机验收：依次演示全部状态灯效")
    demo.add_argument("--config")
    demo.add_argument("--port", type=int)
    demo.add_argument("--simulate", action="store_true")
    demo.add_argument("--dwell", type=int, default=1200, help="每个状态停留毫秒数")
    demo.add_argument("--verbose", action="store_true")

    identity = sub.add_parser("identity", help="查看本机来源 ID 与带 ID 的帧示例")
    identity.add_argument("--config")

    sub.add_parser("selftest", help="全链路自检（不需要硬件）")

    hooks = sub.add_parser("install-hooks", help="生成 / 安装 CodeBuddy Hooks 配置")
    hooks.add_argument("--config")
    hooks.add_argument("--port", type=int)
    hooks.add_argument("--write", action="store_true")
    hooks.add_argument("--scope", choices=["user", "project"], default="user")
    hooks.add_argument("--project-root", default="")
    hooks.add_argument("--with-cli", action="store_true", help="同时安装 CLI 增量事件")
    hooks.add_argument("--harness", default="codebuddy",
                       choices=["codebuddy", "codex", "dsh", "all"],
                       help="安装到哪个 harness（默认 codebuddy）")

    hooks_cfg = sub.add_parser("hooks-config", help="打印推荐 Hooks 配置")
    hooks_cfg.add_argument("--config")
    hooks_cfg.add_argument("--port", type=int)

    pb = sub.add_parser("phone-bridge", help="手机桥接：局域网发现 / 配对 / 端口放行")
    pb.add_argument("action", nargs="?", default="status",
                    choices=["status", "firewall", "pin"])
    pb.add_argument("value", nargs="?", default="", help="pin 动作时的 6 位配对码")
    pb.add_argument("--config")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    handlers = {
        "start": cmd_start, "hook": cmd_hook, "probe": cmd_probe,
        "emit": cmd_emit, "clear": cmd_clear, "demo": cmd_demo,
        "identity": cmd_identity, "selftest": cmd_selftest,
        "install-hooks": cmd_install_hooks, "hooks-config": cmd_hooks_config,
        "phone-bridge": cmd_phone_bridge,
    }
    try:
        return handlers[args.cmd](args)
    except AssertionError as exc:
        print(f"自检失败：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
