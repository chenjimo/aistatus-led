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
from ai_status.mapper import map_hook
from ai_status.service import StatusService
from ai_status.state import StateMachine

APP_DIR = Path(__file__).resolve().parent

DEFAULT_CONFIG: dict = {
    "listen_host": "127.0.0.1",
    "listen_port": 8765,
    "heartbeat_ms": 5000,
    "min_dwell_ms": 300,
    "done_hold_ms": 5000,
    "idle_after_ms": 180000,
    "infer_outputting": True,
    "outputting_after_ms": 3500,
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
    "source_id": "",                       # 留空则自动生成并持久化到 client_id.json
    "source_field": "src",                 # 帧里的来源字段名，可随固件约定调整
    "tag_source_in_msg": True,             # 把 [来源ID] 写进 msg，设备会原样广播
    "client_id_file": str(APP_DIR / "client_id.json"),
    "last_ip_file": str(APP_DIR / "last_ip.json"),
    "log_file": str(APP_DIR / "ai_status.log"),
}

IDE_EVENTS = [
    ("SessionStart", "startup"),
    ("UserPromptSubmit", None),
    ("PreToolUse", ".*"),
    ("PostToolUse", ".*"),
    ("Stop", None),
    ("SessionEnd", "other"),
    ("PreCompact", ""),
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

def _hook_command(event: str, port: int) -> str:
    exe = sys.executable.replace("\\", "/")
    script = str(APP_DIR / "main.py").replace("\\", "/")
    return f'"{exe}" "{script}" hook --event {event} --port {port}'


def _hooks_block(events, port: int) -> dict:
    hooks = {}
    for event, matcher in events:
        group: dict = {"hooks": [{"type": "command",
                                  "command": _hook_command(event, port),
                                  "timeout": 5}]}
        if matcher is not None:
            group["matcher"] = matcher
        hooks[event] = [group]
    return {"hooks": hooks}


def generate_hook_files(cfg: dict) -> dict[str, Path]:
    port = int(cfg.get("listen_port", 8765))
    paths = {
        "ide": APP_DIR / "hooks.settings.ide.json",
        "cli": APP_DIR / "hooks.settings.cli.json",
    }
    paths["ide"].write_text(json.dumps(_hooks_block(IDE_EVENTS, port),
                                       ensure_ascii=False, indent=2), "utf-8")
    paths["cli"].write_text(json.dumps(_hooks_block(CLI_EXTRA_EVENTS, port),
                                       ensure_ascii=False, indent=2), "utf-8")
    return paths


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


# ---------------- 子命令 ----------------

def cmd_start(args) -> int:
    cfg = load_config(args)
    log = setup_logging(cfg, args.verbose)
    svc = StatusService(cfg, log)
    print("=" * 66)
    print(f" AI 工作状态灯中间件 v{__version__}  协议 {P.PROTO}")
    print(f" 事件总线  http://{cfg['listen_host']}:{cfg['listen_port']}/event")
    print(f" 设备策略  {'模拟模式' if cfg['simulate'] else '串口优先 -> 局域网回落'}")
    print(" 停止服务  Ctrl+C（会释放 LED 并回 idle）")
    print("=" * 66)
    try:
        svc.run()
    except KeyboardInterrupt:
        log.info("收到中断，正在释放设备 LED…")
        svc.shutdown()
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
    print(f"已生成 IDE 配置片段：{paths['ide']}")
    print(f"已生成 CLI 增量片段：{paths['cli']}")
    if not args.write:
        print("\n（预演模式）确认无误后加 --write 写入 settings.json")
        print("  python main.py install-hooks --write --scope user")
        return 0
    root = Path(args.project_root) if args.project_root else Path.cwd()
    settings_path = (Path.home() / ".codebuddy" / "settings.json"
                     if args.scope == "user"
                     else root / ".codebuddy" / "settings.json")
    try:
        ide = json.loads(paths["ide"].read_text("utf-8"))
        if args.with_cli:
            ide["hooks"].update(json.loads(paths["cli"].read_text("utf-8"))["hooks"])
        written = merge_hooks(settings_path, ide)
        print(f"已写入：{written}")
        print("请重启 CodeBuddy 会话使 Hooks 生效（CLI 用户还需在 /hooks 面板审核）。")
    except Exception as exc:
        print(f"写入失败：{exc}", file=sys.stderr)
        return 1
    return 0


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

    print("[1/4] Hooks 事件映射")
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
        ({"hook_event_name": "PostToolUse", "tool_name": "Bash",
          "tool_response": {"success": False}}, "error"),
        ({"hook_event_name": "Notification",
          "notification_type": "permission_prompt"}, "need_confirm"),
        ({"hook_event_name": "Notification",
          "notification_type": "idle_prompt"}, "need_input"),
        ({"hook_event_name": "Stop"}, "done"),
        ({"hook_event_name": "PreToolUse", "tool_name": "Bash",
          "ai_status_state": "outputting"}, "outputting"),
    ]
    for payload, expected in cases:
        check(map_hook(payload).state == expected, f"{payload['hook_event_name']} -> {expected}")

    print("[2/4] 协议帧")
    frame = P.serial_frame("status", "thinking", 7, "测试", 15000, True)
    check(frame["t"] == "status" and frame["v"] == 1 and frame["seq"] == 7
          and frame["force"] == 1, "串口帧字段")
    body = P.http_payload("need_confirm", 8, "确认?", 0, True)
    check("t" not in body and "v" not in body and body["force"] == 1, "HTTP 请求体字段")
    check(len(P.encode_line(frame).decode("utf-8").strip()) < 512, "串口单行长度")
    check(P.is_sticky("error") and not P.is_sticky("done"), "粘性状态判定")
    tagged = P.serial_frame("status", "thinking", 9, src="PC-A")
    check(tagged["src"] == "PC-A", "串口帧携带来源 ID")
    tagged_body = P.http_payload("thinking", 9,
                                 msg=P.tag_message("测试", "PC-A"), src="PC-A")
    check(tagged_body["src"] == "PC-A"
          and tagged_body["msg"].startswith("[PC-A]"), "HTTP 帧与 msg 携带来源 ID")

    print("[3/4] 状态机仲裁")
    machine = StateMachine(min_dwell_ms=0, infer_outputting=False)
    machine.update(map_hook({"hook_event_name": "Notification",
                             "notification_type": "permission_prompt", "session_id": "s1"}))
    check(machine.current == "need_confirm", "授权请求转 need_confirm")
    machine.update(map_hook({"hook_event_name": "Stop", "session_id": "s1"}))
    check(machine.current == "need_confirm", "粘性状态不被 done 冲掉")
    tr = machine.update(map_hook({"hook_event_name": "UserPromptSubmit",
                                  "session_id": "s1", "prompt": "继续"}))
    check(machine.current == "thinking" and bool(tr) and tr[0].force,
          "用户回复解除粘性并 force 覆盖设备优先级")
    done_machine = StateMachine(min_dwell_ms=0, done_hold_ms=0, infer_outputting=False)
    done_machine.update(map_hook({"hook_event_name": "Stop"}))
    check(any(t.state == "idle" for t in done_machine.tick(time.monotonic() + 0.1)),
          "done 超时自动回 idle")

    print("[4/4] 事件总线端到端（模拟设备）")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({"simulate": True, "listen_port": port, "log_file": "",
                "infer_outputting": False, "source_id": "SELFTEST-PC"})
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

    hook = sub.add_parser("hook", help="供 CodeBuddy Hooks 调用（stdin JSON）")
    hook.add_argument("--port", type=int, default=8765)
    hook.add_argument("--event", default="")

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

    hooks_cfg = sub.add_parser("hooks-config", help="打印推荐 Hooks 配置")
    hooks_cfg.add_argument("--config")
    hooks_cfg.add_argument("--port", type=int)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    handlers = {
        "start": cmd_start, "hook": cmd_hook, "probe": cmd_probe,
        "emit": cmd_emit, "clear": cmd_clear, "demo": cmd_demo,
        "identity": cmd_identity, "selftest": cmd_selftest,
        "install-hooks": cmd_install_hooks, "hooks-config": cmd_hooks_config,
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
