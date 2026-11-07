"""ai-status/1 协议常量与帧构造。

依据：API_Doc/AI_STATUS_Protocol.md v1.0.0、API_Doc/ESP32S3_Device_API.md v1.4.0。
PC 端只维护这一份状态模型，串口 / HTTP 三种承载的语义完全一致。
"""
from __future__ import annotations

PROTO = "ai-status/1"
FW_EXPECT = "1.4.1"
PROTO_V = 1

# 来源 ID：每台 PC 一个稳定标识，供局域网多电脑时在 ESP 端按 ID 绑定/过滤。
# 固件用 ArduinoJson 按 key 取值，未知字段会被安全忽略，因此该字段向后兼容。
SOURCE_FIELD = "src"

# state -> (prio, sticky, default_ttl_ms)；0 表示无 TTL（靠心跳续期）
STATE_META: dict[str, tuple[int, bool, int]] = {
    "offline": (0, False, 0),
    "idle": (10, False, 0),
    "thinking": (20, False, 0),
    "searching": (25, False, 0),
    "coding": (30, False, 0),
    "outputting": (30, False, 0),
    "done": (40, False, 10000),   # 绿色完成态保持 10s，TTL 随帧下发给固件
    "need_input": (80, True, 0),
    "need_confirm": (85, True, 0),
    "error": (90, True, 0),
}

STICKY_STATES = frozenset(s for s, meta in STATE_META.items() if meta[1])
VALID_STATES = frozenset(STATE_META)

# 需要人工介入的状态：一旦建立就锁定，普通事件（含 error）不得覆盖，
# 只有用户真正处理完（回复/授权/拒绝/会话结束/clear）才允许解除。
HUMAN_STATES = frozenset({"need_confirm", "need_input"})


def is_valid(state: str) -> bool:
    return state in VALID_STATES


def priority(state: str) -> int:
    return STATE_META.get(state, (0, False, 0))[0]


def is_sticky(state: str) -> bool:
    return state in STICKY_STATES


def default_ttl(state: str) -> int:
    return STATE_META.get(state, (0, False, 0))[2]


def clamp_msg(msg: str, limit: int = 120) -> str:
    """msg 仅用于日志/WS 推送，设备侧限制 120 字符、串口单行 <=512B。"""
    text = str(msg or "").replace("\n", " ").replace("\r", " ")
    return text[:limit]


def tag_message(msg: str, source_id: str) -> str:
    """把来源 ID 写进 msg。

    设备会把 msg 原样带进 WS 广播 status 与 GET /api/status，所以即使固件
    还不认识 src 字段，网页面板也能立刻看到「这盏灯是哪台电脑驱动的」。
    """
    if not source_id:
        return clamp_msg(msg)
    return clamp_msg(f"[{source_id}] {msg or ''}".strip())


def serial_frame(kind: str, state: str | None = None, seq: int = 0,
                 msg: str = "", ttl: int | None = None,
                 force: bool = False, src: str = "",
                 source_field: str = SOURCE_FIELD) -> dict:
    """通道 A：一行一个 JSON（调用方负责补 '\\n'）。所有帧都带来源 ID。"""
    frame: dict = {"t": kind, "v": PROTO_V}
    if src:
        frame[source_field] = src
    if kind in ("status", "heartbeat"):
        frame["state"] = state
        frame["seq"] = seq
    if kind == "status":
        if msg:
            frame["msg"] = clamp_msg(msg)
        if ttl:
            frame["ttl"] = int(ttl)
        if force:
            frame["force"] = 1
    return frame


def http_payload(state: str, seq: int, msg: str = "",
                 ttl: int | None = None, force: bool = False,
                 src: str = "", source_field: str = SOURCE_FIELD) -> dict:
    """通道 B：POST /api/status 的请求体（不含 t / v）。"""
    body: dict = {"state": state, "seq": seq}
    if src:
        body[source_field] = src
    if msg:
        body["msg"] = clamp_msg(msg)
    if ttl:
        body["ttl"] = int(ttl)
    if force:
        body["force"] = 1
    return body


def encode_line(frame: dict) -> bytes:
    return (json_dumps(frame) + "\n").encode("utf-8")


def json_dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
