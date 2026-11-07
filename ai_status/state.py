"""多会话状态仲裁：优先级、粘性状态、最短驻留、自动回落。

设备侧只有 1 颗灯且按 prio 拒绝低优先级帧，因此 PC 端必须先做仲裁。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .mapper import StatusEvent
from .protocol import default_ttl, is_sticky, is_valid, priority


@dataclass
class Transition:
    state: str
    msg: str
    reason: str
    force: bool = False
    ttl: int | None = None


@dataclass
class SessionState:
    state: str
    msg: str
    updated: float


@dataclass
class StateMachine:
    min_dwell_ms: int = 300
    done_hold_ms: int = 5000
    idle_after_ms: int = 180000
    infer_outputting: bool = True
    outputting_after_ms: int = 3500

    current: str = "idle"
    current_msg: str = ""
    current_reason: str = "init"
    last_change: float = field(default_factory=time.monotonic)
    last_push: float = 0.0
    last_activity: float = field(default_factory=time.monotonic)
    sessions: dict[str, SessionState] = field(default_factory=dict)
    sticky: tuple[str, str, str] | None = None   # (state, session, msg)
    pending: Transition | None = None
    _inferred_outputting: bool = False

    def _resolve(self, now: float) -> tuple[str, str, str]:
        if self.sticky:
            return self.sticky[0], self.sticky[2], "sticky"
        best: SessionState | None = None
        for item in self.sessions.values():
            if best is None or (priority(item.state), item.updated) >= (
                    priority(best.state), best.updated):
                best = item
        if best is None:
            return "idle", "", "no_session"
        return best.state, best.msg, "session"

    def _transition(self, state: str, msg: str, reason: str,
                    force: bool, now: float) -> list[Transition]:
        if not is_valid(state) or state == "offline":
            return []   # offline 由设备心跳超时自动判定，PC 不主动发
        self._inferred_outputting = (state == "outputting")
        if state == self.current and not force:
            self.current_msg = msg
            return []
        # 最短驻留：低/同级状态切换太快会让灯失去辨识度，稍后补发
        if (not force and not is_sticky(state)
                and priority(state) <= priority(self.current)
                and now - self.last_push < self.min_dwell_ms / 1000):
            self.pending = Transition(state, msg, reason, force, default_ttl(state) or None)
            return []
        self.pending = None
        self.current = state
        self.current_msg = msg
        self.current_reason = reason
        self.last_change = now
        return [Transition(state, msg, reason, force, default_ttl(state) or None)]

    def update(self, ev: StatusEvent) -> list[Transition]:
        now = time.monotonic()
        self.last_activity = now
        sid = ev.session_id or "default"

        if ev.state is None:
            # 只刷新活跃度；保持已有状态，等待 tick 做“输出中”推断
            return []

        # 粘性状态只能由用户响应 / 会话结束 / 手动 clear 解除，
        # 工具完成、Stop 等普通事件不能把它冲掉（协议第二节）。
        if ev.release_sticky:
            self.sticky = None
            self.sessions.pop(sid, None)

        self.sessions[sid] = SessionState(ev.state, ev.msg, now)
        if is_sticky(ev.state):
            self.sticky = (ev.state, sid, ev.msg)

        target, msg, reason = self._resolve(now)
        # 从粘性状态被用户响应后，设备端仍停在 80/85/90，必须 force
        force = ev.force or (self.sticky is None and reason != "sticky" and ev.release_sticky)
        return self._transition(target, msg, ev.reason, force, now)

    def tick(self, now: float | None = None) -> list[Transition]:
        now = time.monotonic() if now is None else now

        if self.pending and now - self.last_push >= self.min_dwell_ms / 1000:
            tr = self.pending
            self.pending = None
            self.current, self.current_msg, self.current_reason = tr.state, tr.msg, tr.reason
            self.last_change = now
            return [tr]

        if self.sticky:
            return []

        quiet = now - self.last_activity

        if self.current == "done" and now - self.last_change >= self.done_hold_ms / 1000:
            for item in self.sessions.values():
                item.state = "idle"
            return self._transition("idle", "完成状态到期", "done_timeout", False, now)

        # Hooks 没有“流式输出中”事件：无工具活动一段时间后推断为输出中（可关闭）
        if (self.infer_outputting and not self._inferred_outputting
                and self.current in ("thinking", "searching", "coding")
                and quiet >= self.outputting_after_ms / 1000):
            return self._transition("outputting", "正在输出回答（推断）",
                                    "output_inferred", False, now)

        if (self.current not in ("idle", "done")
                and quiet >= self.idle_after_ms / 1000):
            self.sessions.clear()
            return self._transition("idle", "长时间无事件", "idle_timeout", False, now)
        return []

    def clear_sticky(self) -> list[Transition]:
        now = time.monotonic()
        self.sticky = None
        self.sessions.clear()
        self.pending = None
        return self._transition("idle", "手动解除粘性状态", "manual_clear", True, now)

    def force_state(self, state: str, msg: str = "", force: bool = False) -> list[Transition]:
        if not is_valid(state) or state == "offline":
            return []
        now = time.monotonic()
        self.last_activity = now
        ev = StatusEvent(state, "manual", "manual", msg, force,
                         release_sticky=force)
        return self.update(ev)

    def snapshot(self) -> dict:
        return {
            "state": self.current,
            "msg": self.current_msg,
            "reason": self.current_reason,
            "sticky": self.sticky[0] if self.sticky else None,
            "sessions": {k: {"state": v.state, "msg": v.msg}
                         for k, v in self.sessions.items()},
            "last_change_ms": int((time.monotonic() - self.last_change) * 1000),
            "pending": self.pending.state if self.pending else None,
        }
