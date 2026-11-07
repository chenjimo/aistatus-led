"""多会话状态仲裁：优先级、粘性状态、最短驻留、自动回落。

设备侧只有 1 颗灯且按 prio 拒绝低优先级帧，因此 PC 端必须先做仲裁。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .mapper import StatusEvent
from .protocol import HUMAN_STATES, default_ttl, is_sticky, is_valid, priority


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
    # 需要人工介入的推断：IDE 版 Hooks 不发送确认/提问事件，只能靠静默窗口判断
    infer_need_confirm: bool = True
    need_confirm_after_ms: int = 3000
    infer_need_input: bool = True
    need_input_after_ms: int = 1500
    lock_human: bool = True        # 等待态建立后，普通事件一律不得覆盖

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
    _watch: tuple[str, float, str] | None = None  # (confirm|input, since, session)

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
        # 不清 _watch：解除事件常常自带新的观察窗口（如 PostToolUse -> 等回复）。
        # 但必须清掉所有会话里残留的等待态，否则多会话仲裁会把它重新顶上来，
        # 造成「sticky 已解除却仍显示 need_input」的失配。
        self.sessions.clear()
        self.sessions[sid] = SessionState("thinking", reason, now)
        target, msg, _ = self._resolve(now)
        return self._transition(target, msg, reason, True, now)

    def _enter_human(self, state: str, msg: str, reason: str,
                     sid: str, now: float) -> list[Transition]:
        """静默推断命中：建立最高优先级的粘性等待态。"""
        if self.sticky and self.sticky[0] in HUMAN_STATES:
            # 已处于等待态时只允许升级（need_input -> need_confirm）
            if priority(state) <= priority(self.sticky[0]):
                return []
        self._watch = None
        self.pending = None
        self.sticky = (state, sid, msg)
        self.sessions[sid] = SessionState(state, msg, now)
        # force=1：设备端可能停在 error(90)，必须突破其优先级检查
        return self._transition(state, msg, reason, True, now)

    def update(self, ev: StatusEvent) -> list[Transition]:
        now = time.monotonic()
        self.last_activity = now
        sid = ev.session_id or "default"

        # 静默观察：只有带 watch 的事件开启；任何其它事件都说明「还有活动」，取消观察
        self._watch = ((ev.watch, now, sid)
                       if ev.watch in ("confirm", "input") else None)
        # 新一轮工具调用（PreToolUse）说明用户已经回复、AI 重新开始工作，
        # 这是「need_input 已处理掉」的明确信号；need_confirm 不能被它解除。
        release = ev.release_sticky or (self.sticky is not None
                                       and self.sticky[0] == "need_input"
                                       and ev.watch == "confirm")
        locked = self._human_locked(ev, release)

        if ev.state is None:
            # 只刷新活跃度；但若等待态已被用户处理掉，要立刻解除并回到工作态
            if (release and self.sticky is not None
                    and self.sticky[0] in HUMAN_STATES):
                return self._release(sid, now, ev.reason)
            return []

        if locked:
            # 等待人工处理期间不推任何状态帧，设备端也不会被低优先级帧影响
            return []

        # 粘性状态只能由用户响应 / 会话结束 / 手动 clear 解除（协议第二节）
        if release:
            self.sticky = None
            self.sessions.clear()
            # 不清 _watch：PreToolUse 触发的解除仍要继续观察这个工具是否卡在授权

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

        # 人工介入推断：必须早于 outputting / idle 回落，否则等待态会被普通状态盖掉
        # need_input 尚未处理时仍允许推断升级为 need_confirm（85 > 80）
        if self._watch and (self.sticky is None
                            or self.sticky[0] != "need_confirm"):
            kind, since, sid = self._watch
            elapsed = now - since
            if kind == "confirm" and self.infer_need_confirm:
                if elapsed >= max(self.need_confirm_after_ms, 0) / 1000:
                    return self._enter_human("need_confirm", "等待用户确认 / 授权（推断）",
                                             "need_confirm_inferred", sid, now)
                # 观察窗口内不做 outputting 推断，避免工具仍在执行时被误判成输出中
                return []
            if kind == "input" and self.infer_need_input:
                if elapsed >= max(self.need_input_after_ms, 0) / 1000:
                    return self._enter_human("need_input", "等待用户回复（推断）",
                                             "need_input_inferred", sid, now)
                # 未到阈值：继续往下走，让 outputting 先短暂表现（两段式灯效）

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
        self._watch = None
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
            "waiting_for_human": bool(self.sticky and self.sticky[0] in HUMAN_STATES),
            "watch": self._watch[0] if self._watch else None,
            "sessions": {k: {"state": v.state, "msg": v.msg}
                         for k, v in self.sessions.items()},
            "last_change_ms": int((time.monotonic() - self.last_change) * 1000),
            "pending": self.pending.state if self.pending else None,
        }
