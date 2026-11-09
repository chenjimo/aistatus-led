#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V2 · Windows 输入注入引擎（ctypes 调用 SendInput，零第三方依赖）

只做「模拟键鼠」，不读取屏幕、不截屏、不记录按键内容。
限制：
  - 仅 Windows；且必须是有交互桌面的会话（服务/计划任务里注入无效）。
  - 普通权限进程无法注入到「以管理员身份运行」的窗口（UIPI）。
  - 默认关闭，必须由 bridge.py 显式启用；空闲超时自动关闭。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Optional

# --------------------------------------------------------------------------- #
# Win32 结构体
# --------------------------------------------------------------------------- #

ULONG_PTR = ctypes.POINTER(wintypes.ULONG)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", INPUTUNION)]


INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120

_user32: Optional[ctypes.WinDLL] = None


def _api():
    global _user32
    if _user32 is None:
        _user32 = ctypes.WinDLL("user32", use_last_error=True)
        _user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        _user32.SendInput.restype = wintypes.UINT
    return _user32


def _send(*items: INPUT) -> bool:
    n = len(items)
    if n == 0:
        return True
    arr = (INPUT * n)(*items)
    sent = _api().SendInput(n, arr, ctypes.sizeof(INPUT))
    return sent == n


def _kbd(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=None)
    return INPUT(type=INPUT_KEYBOARD, u=INPUTUNION(ki=ki))


def _mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
    mi = MOUSEINPUT(dx=dx, dy=dy, mouseData=data, dwFlags=flags, time=0,
                    dwExtraInfo=None)
    return INPUT(type=INPUT_MOUSE, u=INPUTUNION(mi=mi))


# --------------------------------------------------------------------------- #
# 虚拟键表
# --------------------------------------------------------------------------- #

VK = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12,
    "win": 0x5B, "meta": 0x5B, "enter": 0x0D, "return": 0x0D,
    "esc": 0x1B, "escape": 0x1B, "tab": 0x09, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pgup": 0x21, "pgdn": 0x22,
    "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "capslock": 0x14, "printscreen": 0x2C,
}
for i in range(1, 13):
    VK[f"f{i}"] = 0x70 + i - 1
for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    VK[c.lower()] = ord(c)
for i in range(10):
    VK[str(i)] = 0x30 + i
for k, v in [("`", 0xC0), ("-", 0xBD), ("=", 0xBB), ("[", 0xDB), ("]", 0xDD),
             ("\\", 0xDC), (";", 0xBA), ("'", 0xDE), (",", 0xBC), (".", 0xBE),
             ("/", 0xBF)]:
    VK[k] = v


def vk_of(key: str) -> int:
    return VK.get(key.strip().lower(), 0)


# --------------------------------------------------------------------------- #
# 原子操作
# --------------------------------------------------------------------------- #

def key_down(vk: int) -> bool:
    return _send(_kbd(vk=vk))


def key_up(vk: int) -> bool:
    return _send(_kbd(vk=vk, flags=KEYEVENTF_KEYUP))


def key(name: str) -> bool:
    vk = vk_of(name)
    return False if vk == 0 else (key_down(vk) and key_up(vk))


def combo(keys: list) -> bool:
    """组合键，如 ['ctrl','shift','s']；找不到任何键时返回 False。"""
    vks = [vk_of(str(k)) for k in keys]
    if not vks or 0 in vks:
        return False
    ok = True
    for v in vks:
        ok = key_down(v) and ok
    for v in reversed(vks):
        ok = key_up(v) and ok
    return ok


def type_text(text: str) -> bool:
    """按 Unicode 逐字符输入（支持中文）；不经过剪贴板，不读取系统内容。"""
    ok = True
    for ch in text:
        code = ord(ch)
        #  surrogate pair
        if code > 0xFFFF:
            code -= 0x10000
            units = [0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)]
        else:
            units = [code]
        for u in units:
            ok = _send(_kbd(scan=u, flags=KEYEVENTF_UNICODE)) and ok
            ok = _send(_kbd(scan=u, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)) and ok
    return ok


def wheel(clicks: int) -> bool:
    """正=向上滚，负=向下滚。clicks 为滚轮格数。"""
    if clicks == 0:
        return True
    n = max(-20, min(20, int(clicks)))
    # mouseData 是 DWORD，负向滚动需要补码
    data = (WHEEL_DELTA * n) & 0xFFFFFFFF
    return _send(_mouse(MOUSEEVENTF_WHEEL, data=data))


def move(dx: int, dy: int) -> bool:
    return _send(_mouse(MOUSEEVENTF_MOVE, dx=int(dx), dy=int(dy)))


def click(button: str = "left") -> bool:
    down, up = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }.get(button, (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP))
    return _send(_mouse(down)) and _send(_mouse(up))


def swipe(direction: str, distance: int = 300) -> bool:
    """上下滑动 → 滚轮；左右滑动 → Alt+←/→（浏览器前进后退）。"""
    direction = direction.lower()
    if direction in ("up", "down"):
        steps = max(1, min(8, int(abs(distance) / 120)))
        return wheel(steps if direction == "up" else -steps)
    if direction == "left":
        return combo(["alt", "left"])
    if direction == "right":
        return combo(["alt", "right"])
    return False


# --------------------------------------------------------------------------- #
# 带安全策略的执行器
# --------------------------------------------------------------------------- #

# 手势 → 键位映射（手机端和 PC 端保持一致，可在 actions.json 覆盖）
DEFAULT_GESTURE_MAP = {
    "tilt_left": ["pgup"],
    "tilt_right": ["pgdn"],
    "tilt_up": ["home"],
    "tilt_down": ["end"],
    "shake": ["esc"],
    "flick_left": ["alt", "left"],
    "flick_right": ["alt", "right"],
}

MAX_TEXT_LEN = 200
MAX_OPS_PER_SEC = 30


class InputEngine:
    """远端输入执行器。

    安全策略（三重）：
      1. enabled 默认 False，只能由 PC 端本地启动参数/配置打开；
      2. 令牌由 /v1/pair 下发，所有输入请求必须带 token；
      3. 空闲 timeout 秒后自动关闭，防止忘记关；并有每秒操作次数限流。
    """

    def __init__(self, enabled: bool = False, timeout: float = 600.0,
                 gesture_map: Optional[dict] = None) -> None:
        self.enabled = bool(enabled)
        self.timeout = float(timeout)
        self.gesture_map = dict(DEFAULT_GESTURE_MAP)
        if gesture_map:
            self.gesture_map.update(gesture_map)
        self.last_used = time.time()
        self.window_start = time.time()
        self.ops_in_window = 0
        self.last_error = ""
        self.last_action = ""

    # ---- 状态 ----

    def status(self) -> dict:
        idle = time.time() - self.last_used
        return {
            "enabled": self.auto_expire(),
            "idleSec": int(idle),
            "timeoutSec": int(self.timeout),
            "lastAction": self.last_action,
            "lastError": self.last_error,
        }

    def auto_expire(self) -> bool:
        # timeout <= 0 表示永不过期：在 bridge_config.json 里设 "input_idle_timeout_sec": 0
        # 即可让远控长期保持开启，无需每次闲置后重新启用。
        if self.enabled and self.timeout > 0 and time.time() - self.last_used > self.timeout:
            self.enabled = False
            self.last_error = "空闲超时，远控已自动关闭"
        return self.enabled

    def enable(self, on: bool) -> None:
        self.enabled = bool(on)
        self.last_used = time.time()
        self.last_error = ""
        self.last_action = "enable" if on else "disable"

    # ---- 限流 ----

    def _allow(self) -> bool:
        now = time.time()
        if now - self.window_start >= 1.0:
            self.window_start = now
            self.ops_in_window = 0
        if self.ops_in_window >= MAX_OPS_PER_SEC:
            self.last_error = "rate_limited"
            return False
        self.ops_in_window += 1
        return True

    # ---- 分发 ----

    def apply(self, req: dict) -> tuple[bool, str]:
        if not self.auto_expire():
            return False, "input_disabled：请在电脑端用 --enable-input 开启"
        if not self._allow():
            return False, "rate_limited：操作过于频繁"
        kind = str(req.get("kind") or "").lower()
        self.last_used = time.time()
        self.last_action = kind
        try:
            if kind == "key":
                ok = key(str(req.get("key") or ""))
                return (True, "ok") if ok else (False, "unknown_key")
            if kind == "combo":
                keys = req.get("keys") or []
                ok = combo(list(keys))
                return (True, "ok") if ok else (False, "unknown_key")
            if kind == "text":
                text = str(req.get("text") or "")[:MAX_TEXT_LEN]
                return (True, "ok") if type_text(text) else (False, "send_failed")
            if kind == "swipe":
                ok = swipe(str(req.get("dir") or "up"), int(req.get("distance") or 300))
                return (True, "ok") if ok else (False, "bad_direction")
            if kind == "scroll":
                ok = wheel(int(req.get("clicks") or 0))
                return (True, "ok") if ok else (False, "send_failed")
            if kind == "move":
                ok = move(int(req.get("dx") or 0), int(req.get("dy") or 0))
                return (True, "ok") if ok else (False, "send_failed")
            if kind == "click":
                ok = click(str(req.get("button") or "left"))
                return (True, "ok") if ok else (False, "send_failed")
            if kind == "gesture":
                name = str(req.get("name") or "")
                keys = self.gesture_map.get(name)
                if not keys:
                    return False, "unknown_gesture"
                return (True, "ok") if combo(list(keys)) else (False, "send_failed")
            return False, "unknown_kind"
        except Exception as exc:
            self.last_error = str(exc)
            return False, f"send_failed：{exc}"


def available() -> bool:
    """当前环境是否支持输入注入（Windows + 交互桌面）。"""
    try:
        _api()
        return True
    except Exception:
        return False
