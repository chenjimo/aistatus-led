"""多 harness（CodeBuddy / Codex / DSH）Hooks 事件 -> ai-status/1 状态映射。

映射规则外置在 ``profiles/*.json``：
  - ``events``                  事件名 -> 语义动作（session_start / prompt / pre_tool / ...）
  - ``search_tools``            检索类工具名 -> searching
  - ``code_tools``              编写 / 执行类工具名 -> coding
  - ``input_tools``             提问类工具名 -> need_input
  - ``failure_prefixes``        tool_response 字符串的失败前缀 -> error
  - ``notification``            Notification 子类型 -> 状态
  - ``command_search_patterns`` Bash / 命令类工具里出现这些片段时归为 searching

本文件只做三件事：选 profile -> 事件归一化 -> 动作分发。
v1.3.0 原则不变：**只按真实事件 / 明确字段上报，绝不用「静默」猜用户要不要操作**。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .protocol import is_valid

_PROFILE_DIR = Path(__file__).resolve().parent.parent / "profiles"
_PROFILE_CACHE: dict[str, dict] = {}

# profiles/ 丢失时的最小兜底（等价于 codebuddy 的关键规则）
_BUILTIN: dict = {
    "name": "codebuddy",
    "events": {
        "SessionStart": "session_start", "SessionEnd": "session_end",
        "UserPromptSubmit": "prompt", "PreToolUse": "pre_tool", "PostToolUse": "post_tool",
        "PostToolUseFailure": "failure_event", "PermissionRequest": "need_confirm",
        "PermissionPrompt": "need_confirm", "ToolPermissionRequest": "need_confirm",
        "PermissionDenied": "failure_event", "StopFailure": "failure_event",
        "TaskFailed": "failure_event", "Stop": "done", "Notification": "notification",
        "Elicitation": "need_input", "ElicitationDialog": "need_input", "AskUser": "need_input",
        "PreCompact": "noop", "PostCompact": "noop", "SubagentStart": "noop",
        "SubagentStop": "noop", "TaskCreated": "noop", "TaskCompleted": "noop",
        "PreModelRequest": "model_start", "PostModelRequest": "model_complete",
        "Interrupt": "interrupted",
    },
    "search_tools": ["web_search", "WebSearch", "web_fetch", "WebFetch", "RAG_search",
                     "search_content", "Grep", "search_file", "list_dir", "Glob",
                     "read_file", "Read", "codebase_search", "Task", "task"],
    "code_tools": ["write_to_file", "Write", "replace_in_file", "Edit", "MultiEdit",
                   "NotebookWrite", "delete_file", "execute_command", "Bash",
                   "apply_patch", "create_file", "install_binary"],
    "input_tools": ["ask_followup_question", "AskUserQuestion", "AskUser", "ask_user",
                    "askUser", "prompt_user", "Elicitation"],
    "failure_prefixes": ["error calling tool", "tool execution failed", "command failed",
                         "command exited with code", "failed to execute", "permission denied",
                         "operation cancelled", "operation canceled", "request interrupted"],
    "notification": {"permission_prompt": "need_confirm", "idle_prompt": "need_input",
                     "elicitation_dialog": "need_input"},
    "command_search_patterns": [],
}


@dataclass(frozen=True)
class StatusEvent:
    state: str | None          # None = 只刷新活跃时间，不触发状态切换
    reason: str
    session_id: str = "default"
    msg: str = ""
    force: bool = False        # 需要突破设备端优先级检查时置 1
    release_sticky: bool = False


def _short(value, limit: int = 60) -> str:
    text = str(value or "").strip().replace("\n", " ")
    return text[:limit]


def _norm_key(key) -> str:
    return str(key).strip().replace("-", "_").lower()


def _is_false(value) -> bool:
    if value is False:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"false", "0", "no", "off", "none", "auto", "allow"}
    return False


def _flag(value) -> bool:
    """把审批字段的各种写法规范成 bool；"allow"/"auto" 等放行值一律算 False。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {
            "true", "1", "yes", "ask", "always_ask", "confirm", "required",
            "prompt", "pending", "manual", "request", "wait"}
    return False


# ---------------- profile 加载 ----------------

def load_profile(name: str) -> dict:
    name = str(name or "codebuddy").strip().lower()
    if name in _PROFILE_CACHE:
        return _PROFILE_CACHE[name]
    data = None
    path = _PROFILE_DIR / f"{name}.json"
    if path.exists():
        try:
            data = json.loads(path.read_text("utf-8"))
        except Exception:
            data = None
    if not isinstance(data, dict):
        data = json.loads(json.dumps(_BUILTIN))
    data.setdefault("name", name)
    _PROFILE_CACHE[name] = data
    return data


def list_profiles() -> list[str]:
    names = set(_PROFILE_CACHE)
    if _PROFILE_DIR.exists():
        names.update(p.stem.lower() for p in _PROFILE_DIR.glob("*.json"))
    names.update({"codebuddy", "codex", "dsh"})
    return sorted(names)


def _pick_profile(payload: dict, profile_name: str | None) -> dict:
    name = str(payload.get("_profile") or profile_name or "").strip().lower()
    if name in ("codebuddy", "codex", "dsh"):
        return load_profile(name)
    # 自动检测（尽力而为）：apply_patch 是 Codex 的无歧义特征；DSH 事件名带斜杠
    tool = str(payload.get("tool_name") or payload.get("toolName") or "")
    if tool == "apply_patch":
        return load_profile("codex")
    event = str(payload.get("hook_event_name") or payload.get("event") or "")
    if "/" in event:
        return load_profile("dsh")
    return load_profile("codebuddy")


# ---------------- 字段判定 ----------------

def _hint_dicts(payload: dict, tool_input: dict):
    yield payload
    yield tool_input
    for key in ("permission_context", "permissionContext", "metadata", "extra", "context"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            yield nested


def _approval_hint(payload: dict, tool_input: dict) -> bool:
    approval_keys = {
        "requires_approval", "requiresapproval", "need_approval", "needapproval",
        "approval_required", "approvalrequired", "requires_confirmation",
        "requiresconfirmation", "confirm", "confirmation", "permission",
        "permission_decision", "permissiondecision", "is_dangerous", "dangerous",
    }
    auto_keys = {"auto_approve", "autoapprove", "autoapproved", "skip_approval",
                 "skipapproval", "autoaccept"}
    for obj in _hint_dicts(payload, tool_input):
        for key, value in obj.items():
            norm = _norm_key(key)
            if norm in auto_keys:
                if _is_false(value):
                    return True
                continue
            if norm in approval_keys and _flag(value):
                return True
    return False


def _input_hint(prof: dict, payload: dict, tool_input: dict, tool_name: str) -> bool:
    if tool_name in set(prof.get("input_tools") or ()):
        return True
    name = tool_name.lower()
    if any(tag in name for tag in ("ask_followup", "askuser", "ask_user", "elicitation")):
        return True
    input_keys = {"questions", "question_list", "ask_user", "followup", "options"}
    for obj in (payload, tool_input):
        for key, value in obj.items():
            if _norm_key(key) in input_keys and value:
                return True
    return False


def _command_is_search(prof: dict, tool_input: dict) -> bool:
    patterns = prof.get("command_search_patterns") or ()
    if not patterns:
        return False
    command = str((tool_input or {}).get("command") or (tool_input or {}).get("cmd") or "")
    low = command.lower()
    return any(str(p).lower() in low for p in patterns)


def _response_failed(prof: dict, response) -> bool:
    if isinstance(response, dict):
        for key, value in response.items():
            norm = _norm_key(key)
            if norm in ("success", "ok"):
                if _is_false(value):
                    return True
            elif norm in ("is_error", "iserror", "failed", "failure", "error"):
                if value is True or (isinstance(value, str) and value.strip()):
                    return True
            elif norm in ("exitcode", "exit_code"):
                if isinstance(value, int) and value != 0:
                    return True
        return False
    if isinstance(response, str):
        prefixes = tuple(str(p).lower() for p in (prof.get("failure_prefixes") or ()))
        return bool(prefixes) and response.strip().lower()[:160].startswith(prefixes)
    return False


def _tool_state(prof: dict, tool_name: str, tool_input: dict, payload: dict) -> tuple[str, str]:
    """返回 (state, msg)。只按工具名与明确的审批/提问字段分类，不做静默推断。"""
    name = tool_name or ""
    if _input_hint(prof, payload, tool_input, name):
        return "need_input", f"等待用户回复：{name or '提问'}"
    if _approval_hint(payload, tool_input):
        return "need_confirm", f"等待用户确认：{name or '授权'}"
    code_tools = set(prof.get("code_tools") or ())
    if name in code_tools:
        # Codex / DSH 的 Bash 既可能跑检索也可能真的写；命令内容命中检索模式时归 searching
        if name in ("Bash", "bash", "shell", "pwsh") and _command_is_search(prof, tool_input):
            return "searching", f"检索资料：{name}"
        return "coding", f"执行工具：{name}"
    search_tools = set(prof.get("search_tools") or ())
    if name in search_tools or name.startswith("mcp__") or name.startswith("mcp."):
        return "searching", f"检索资料：{name}"
    # tool_name 经常为空：宁可显示「思考」，也不猜成「需要人处理」
    return "thinking", f"调用工具：{name}"


def _map_notification(prof: dict, payload: dict, session: str) -> StatusEvent:
    kind = str(payload.get("notification_type") or payload.get("notificationType") or "").strip().lower()
    mapping = prof.get("notification") or {}
    state = mapping.get(kind)
    msg = _short(payload.get("message"))
    if state == "need_confirm":
        return StatusEvent("need_confirm", "等待用户授权", session, msg)
    if state == "need_input":
        return StatusEvent("need_input", "等待用户回复", session, msg)
    # 未知通知类型不猜，只记录活跃度
    return StatusEvent(None, f"通知：{kind}", session)


def map_hook(payload: dict, event_hint: str = "", profile_name: str | None = None) -> StatusEvent:
    """把一次 Hook 的 JSON 转成状态事件。任何异常都不得影响 AI 主流程。

    profile 优先级：payload['_profile'] > 参数 profile_name > 自动检测 > codebuddy。
    """
    if not isinstance(payload, dict):
        return StatusEvent(None, "invalid_payload")
    prof = _pick_profile(payload, profile_name)
    event = str(payload.get("hook_event_name") or payload.get("event")
                or payload.get("type") or event_hint or "")
    session = str(payload.get("session_id") or payload.get("sessionId") or "default")
    tool = str(payload.get("tool_name") or payload.get("toolName") or "")
    tool_input = payload.get("tool_input") or payload.get("toolInput") or payload.get("args") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    # 显式覆盖：供 MCP / 自定义插件 / 手工脚本精确上报（如真实流式输出事件）
    override = payload.get("ai_status_state") or payload.get("status_state")
    if isinstance(override, str) and is_valid(override):
        return StatusEvent(override, "explicit_override", session,
                           _short(payload.get("msg")), bool(payload.get("force")),
                           bool(payload.get("release_sticky")))

    action = (prof.get("events") or {}).get(event) or "unknown"

    if action == "session_start":
        return StatusEvent("idle", "会话开始", session, force=True, release_sticky=True)
    if action == "session_end":
        return StatusEvent("idle", "会话结束", session, force=True, release_sticky=True)
    if action == "prompt":
        return StatusEvent("thinking", "收到新需求：" + _short(payload.get("prompt")),
                           session, force=True, release_sticky=True)
    if action == "model_start":
        return StatusEvent("thinking", "模型推理中", session)
    if action == "model_complete":
        # Codex 只为失败的请求亮红灯；completed 往往紧接工具调用，不能误报完成。
        if str(payload.get("status") or "").lower() == "failed":
            return StatusEvent("error", "模型请求失败", session, release_sticky=True)
        return StatusEvent(None, "模型请求结束", session)
    if action == "busy":
        # 模型请求前后（Codex 的 PreModelRequest / PostModelRequest）：只是活跃，
        # 不解除等待态，所以不走 release。
        return StatusEvent("thinking", "模型处理中", session)
    if action == "pre_tool":
        state, msg = _tool_state(prof, tool, tool_input, payload)
        return StatusEvent(state, msg, session)
    if action in ("post_tool", "post_tool_keep"):
        if _response_failed(prof, payload.get("tool_response")):
            return StatusEvent("error", f"工具执行失败：{tool}", session, release_sticky=True)
        if action == "post_tool_keep":
            # 闭环：工具跑完不等于「写代码」结束。保持 coding/searching，
            # 直到下一个工具事件或本轮结束；同时解除可能存在的等待态。
            return StatusEvent(None, "工具完成（保持工作态）", session,
                               release_sticky=True)
        return StatusEvent("thinking", "工具完成，继续处理：" + tool, session,
                           release_sticky=True)
    if action == "outputting":
        return StatusEvent("outputting", "正在输出回答", session)
    if action == "failure_event":
        return StatusEvent("error", f"任务异常：{event}", session, release_sticky=True)
    if action == "approval_decided":
        # DSH 的 approval/decided：用户已点「允许一次 / 拒绝」，必须立刻解除橙闪
        result = str(payload.get("approval_result") or payload.get("decision")
                     or payload.get("result") or "").lower()
        if any(tag in result for tag in ("reject", "deny", "cancel", "unavailable")):
            return StatusEvent("error", "审批被拒绝", session, release_sticky=True)
        return StatusEvent("thinking", "用户已放行", session, release_sticky=True)
    if action == "need_confirm":
        return StatusEvent("need_confirm", "等待用户确认", session)
    if action == "need_input":
        return StatusEvent("need_input", "等待用户补充信息", session)
    if action == "done":
        # 本轮完成 -> 绿色 10s，到期回 idle；同时解除等待态
        return StatusEvent("done", "本轮任务完成", session, release_sticky=True)
    if action == "interrupted":
        return StatusEvent("idle", "本轮已中断", session, force=True, release_sticky=True)
    if action == "notification":
        return _map_notification(prof, payload, session)
    if action == "noop":
        return StatusEvent(None, f"事件：{event}", session)
    return StatusEvent(None, f"未处理事件：{event or 'unknown'}", session)


# 兼容旧代码 / 自检的模块级常量（均取自 codebuddy profile）
_CB = load_profile("codebuddy")
SEARCH_TOOLS = frozenset(_CB.get("search_tools") or ())
CODE_TOOLS = frozenset(_CB.get("code_tools") or ())
INPUT_TOOLS = frozenset(_CB.get("input_tools") or ())
CONFIRM_EVENTS = frozenset({"PermissionRequest", "PermissionPrompt", "ToolPermissionRequest"})
FAILURE_EVENTS = frozenset({"PostToolUseFailure", "PermissionDenied", "StopFailure", "TaskFailed"})
INPUT_EVENTS = frozenset({"Elicitation", "ElicitationDialog", "AskUser"})
