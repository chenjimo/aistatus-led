"""多会话状态仲裁：优先级、粘性状态、最短驻留、自动回落。

v1.3.0 起不再用「静默」推断等待态。IDE 版 PostToolUse 经常缺失，深度思考
（长时间无事件）会被误判成 need_input / need_confirm。现在状态只由真实事件
驱动；只有真正卡住超过 stalled_after_ms 才归为 error（红色快闪）。

设备侧只有 1 颗灯且按 prio 拒绝低优先级帧，因此 PC 端必须先做仲裁。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .mapper import StatusEvent
from .protocol import HUMAN_STATES, default_ttl, is_sticky, is_valid, priority

# AI 仍在本轮工作中（这些状态下长时间静默才算「卡住」）
WORK_STATES = frozenset({"thinking", "searching", "coding", "outputting"})


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
    done_hold_ms: int = 10000        # done 绿色常亮 10s 后回 idle
    idle_after_ms: int = 600000      # 非工作态长时间无事件回 idle
    stalled_after_ms: int = 180000   # 工作中卡住 3 分钟 -> error 红色快闪；0 = 关闭
    error_hold_ms: int = 3000        # 红闪至少显示这么久，之后可由工作事件自动恢复
    infer_outputting: bool = False   # 默认关闭：无「流式输出」事件，不靠静默猜
    outputting_after_ms: int = 3500
    lock_human: bool = True          # 等待态建立后，普通事件不得覆盖

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

    def _human_locked(self, ev: StatusEvent, release: bool = False) -> bool:
        """等待人工处理期间，普通事件（含 error）不得覆盖灯效。

        只有 release_sticky / force / 更高优先级的等待态（need_input→need_confirm）
        可以突破；这保证「不处理掉就不解除」。
        """
        if not self.lock_human or self.sticky is None:
            return False
        if self.sticky[0] not in HUMAN_STATES:
            return False
        if release or ev.force:
            return False
        if (ev.state in HUMAN_STATES
                and priority(ev.state) > priority(self.sticky[0])):
            return False
        return True

    def _release(self, sid: str, now: float, reason: str) -> list[Transition]:
        """用户已处理完等待态：解除粘性并回到工作态，force 覆盖设备端高优先级。"""
        self.sticky = None
        # 清掉所有会话里残留的粘性态，否则多会话仲裁会把它重新顶上来
        self.sessions.clear()
        self.sessions[sid] = SessionState("thinking", reason, now)
        target, msg, _ = self._resolve(now)
        return self._transition(target, msg, reason, True, now)

    def update(self, ev: StatusEvent) -> list[Transition]:
        now = time.monotonic()
        self.last_activity = now
        sid = ev.session_id or "default"

        # 解除条件全部来自真实事件，没有任何基于时间的猜测：
        # ① Hook 明确带 release_sticky（用户新需求 / 工具完成 / 一轮完成 / 会话切换）
        # ② need_input 期间 AI 又开始工作，说明用户已回复
        # ③ error 红闪显示够 error_hold_ms 后，AI 恢复工作即可自动清除
        error_recovered = (
            self.sticky is not None and self.sticky[0] == "error"
            and ev.state is not None and not is_sticky(ev.state)
            and now - self.last_change >= self.error_hold_ms / 1000)
        input_resolved = (
            self.sticky is not None and self.sticky[0] == "need_input"
            and ev.state in WORK_STATES)
        release = ev.release_sticky or error_recovered or input_resolved
        locked = self._human_locked(ev, release)

        if ev.state is None:
            if (release and self.sticky is not None
                    and self.sticky[0] in HUMAN_STATES):
                return self._release(sid, now, ev.reason)
            return []

        if locked:
            # 等待人工处理期间不推任何状态帧，设备端也不会被低优先级帧影响
            return []

        if release and self.sticky is not None:
            self.sticky = None
            self.sessions.clear()

        self.sessions[sid] = SessionState(ev.state, ev.msg, now)
        if is_sticky(ev.state):
            if not (self.sticky and self.sticky[0] in HUMAN_STATES
                    and priority(ev.state) < priority(self.sticky[0])):
                self.sticky = (ev.state, sid, ev.msg)

        target, msg, reason = self._resolve(now)
        # 从粘性状态被用户响应后，设备端仍停在 80/85/90，必须 force
        force = ev.force or (self.sticky is None and reason != "sticky" and release)
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

        # 完成 -> 空闲：10s 到期，force 以压过设备端 done(40) -> idle(10) 的降级检查
        if self.current == "done" and now - self.last_change >= self.done_hold_ms / 1000:
            for item in self.sessions.values():
                item.state = "idle"
            return self._transition("idle", "完成状态到期（10s）", "done_timeout", True, now)

        # 唯一保留的超时：工作中卡住太久 -> 红色快闪，而不是谎称「需要用户确认/回复」。
        # 阈值默认 3 分钟，深度思考不会触发；0 可关闭。
        if (self.stalled_after_ms > 0 and self.current in WORK_STATES
                and quiet >= self.stalled_after_ms / 1000):
            msg = "处理时间过长 / 无响应，请检查 AI 是否卡住"
            self.sticky = ("error", "default", msg)
            self.sessions.clear()
            self.sessions["default"] = SessionState("error", msg, now)
            return self._transition("error", msg, "stalled_timeout", True, now)

        # 可选近似：无「流式输出」事件时按静默推断 outputting，默认关闭
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
        self.last_activity = now   # 手动解除后重新给一个完整的卡住检测窗口
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
            "waiting_for_human": bool(self.sticky and self.sticky[0] in HUMAN_STATES),
            "quiet_ms": int((time.monotonic() - self.last_activity) * 1000),
            "stalled_after_ms": self.stalled_after_ms,
            "sessions": {k: {"state": v.state, "msg": v.msg}
                         for k, v in self.sessions.items()},
            "last_change_ms": int((time.monotonic() - self.last_change) * 1000),
            "pending": self.pending.state if self.pending else None,
        }
