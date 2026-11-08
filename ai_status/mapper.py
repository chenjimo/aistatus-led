"""CodeBuddy Hooks 事件 -> ai-status/1 状态映射。

v1.3.0 原则：**只按真实事件 / 明确字段上报，绝不用「静默」猜用户要不要操作**。
实测 IDE 版 PreToolUse 数量远大于 PostToolUse（工具完成事件经常根本不发），
任何「X 秒内没等到某个事件就判定需要人处理」的推断，都会把深度思考误报成
need_input / need_confirm，因此 v1.2.0 的静默推断已整体移除。

IDE（Craft Agent）与 CLI 的工具名不同，这里同时兼容两套别名。
"""
from __future__ import annotations

from dataclasses import dataclass

from .protocol import is_valid

# 找资料 / 检索 / 读文档 -> searching（黄色慢闪）
SEARCH_TOOLS = frozenset({
    "web_search", "WebSearch", "web_fetch", "WebFetch", "RAG_search",
    "search_content", "Grep", "search_file", "list_dir", "Glob",
    "read_file", "Read", "codebase_search", "Task", "task",
    "mcp__*",  # 由前缀匹配单独处理
})

# 写脚本 / 改文件 / 执行命令 -> coding（浅蓝青呼吸）
CODE_TOOLS = frozenset({
    "write_to_file", "Write", "replace_in_file", "Edit", "MultiEdit",
    "NotebookWrite", "delete_file", "execute_command", "Bash",
    "apply_patch", "create_file", "install_binary",
})

# 主动向用户提问 -> need_input（粉白双闪）
INPUT_TOOLS = frozenset({
    "ask_followup_question", "AskUserQuestion", "AskUser", "ask_user",
    "askUser", "prompt_user", "Elicitation",
})

# 等待拍板 / 授权 -> need_confirm（橙色快闪）
CONFIRM_EVENTS = frozenset({
    "PermissionRequest", "PermissionPrompt", "ToolPermissionRequest",
})
# 异常 / 失败 -> error（红色快闪）
FAILURE_EVENTS = frozenset({"PostToolUseFailure", "PermissionDenied", "StopFailure", "TaskFailed"})
# 需要用户补充信息 -> need_input
INPUT_EVENTS = frozenset({"Elicitation", "ElicitationDialog", "AskUser"})

# Hook 载荷里表示「需要审批」的字段别名。只认键名，不猜正文内容。
APPROVAL_KEYS = frozenset({
    "requires_approval", "requiresapproval", "need_approval", "needapproval",
    "approval_required", "approvalrequired", "requires_confirmation",
    "requiresconfirmation", "confirm", "confirmation", "permission",
    "permission_decision", "permissiondecision", "is_dangerous", "dangerous",
})
AUTO_APPROVE_KEYS = frozenset({
    "auto_approve", "autoapprove", "autoapproved", "skip_approval",
    "skipapproval", "autoaccept",
})
APPROVAL_VALUES = frozenset({
    "true", "1", "yes", "ask", "always_ask", "confirm", "required",
    "prompt", "pending", "manual", "request", "wait",
})
FALSE_VALUES = frozenset({"false", "0", "no", "off", "none", "auto", "allow"})
# tool_input 里出现这些键且非空，说明 AI 在向用户发问（而非普通参数）
INPUT_KEYS = frozenset({"questions", "question_list", "ask_user", "followup", "options"})

# 仅记录活跃度、不改变当前状态
NOOP_EVENTS = frozenset({
    "SubagentStart", "SubagentStop", "PreCompact", "PostCompact",
    "TaskCreated", "TaskCompleted", "TeammateIdle", "FileChanged",
    "CwdChanged", "WorktreeCreate", "WorktreeRemove", "Setup",
    "InstructionsLoaded", "ConfigChange", "ElicitationResult",
})


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


# CodeBuddy IDE 实测：PostToolUse 的 tool_response 是**纯字符串**而不是 dict，
# 失败时形如 "Error calling tool: Could not find file 'x'."。
# 只认这些明确前缀，避免把文件内容里的 "error" 字样误判成失败。
FAILURE_PREFIXES = (
    "error calling tool",
    "tool execution failed",
    "command failed",
    "command exited with code",
    "failed to execute",
    "permission denied",
    "operation cancelled",
    "operation canceled",
    "request interrupted",
)


def _response_failed(response) -> bool:
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
        return response.strip().lower()[:160].startswith(FAILURE_PREFIXES)
    return False


def _norm_key(key) -> str:
    return str(key).strip().replace("-", "_").lower()


def _flag(value) -> bool:
    """把审批字段的各种写法规范成 bool；"allow"/"auto" 等放行值一律算 False。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in APPROVAL_VALUES
    return False


def _is_false(value) -> bool:
    if value is False:
        return True
    if isinstance(value, str):
        return value.strip().lower() in FALSE_VALUES
    return False


def _hint_dicts(payload: dict, tool_input: dict):
    """只扫固定的两层 + 少数元数据包，避免把用户正文里的文字误判成审批字段。"""
    yield payload
    yield tool_input
    for key in ("permission_context", "permissionContext", "metadata", "extra", "context"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            yield nested


def _approval_hint(payload: dict, tool_input: dict) -> bool:
    for obj in _hint_dicts(payload, tool_input):
        for key, value in obj.items():
            norm = _norm_key(key)
            if norm in AUTO_APPROVE_KEYS:
                if _is_false(value):
                    return True
                continue
            if norm in APPROVAL_KEYS and _flag(value):
                return True
    return False


def _input_hint(payload: dict, tool_input: dict, tool_name: str) -> bool:
    if tool_name in INPUT_TOOLS:
        return True
    name = tool_name.lower()
    if any(tag in name for tag in ("ask_followup", "askuser", "ask_user", "elicitation")):
        return True
    for obj in (payload, tool_input):
        for key, value in obj.items():
            if _norm_key(key) in INPUT_KEYS and value:
                return True
    return False


def _tool_state(tool_name: str, tool_input: dict, payload: dict) -> tuple[str, str]:
    """返回 (state, msg)。只按工具名与明确的审批/提问字段分类，不做静默推断。"""
    name = tool_name or ""
    if _input_hint(payload, tool_input, name):
        return "need_input", f"等待用户回复：{name or '提问'}"
    if _approval_hint(payload, tool_input):
        return "need_confirm", f"等待用户确认：{name or '授权'}"
    if name in CODE_TOOLS:
        return "coding", f"执行工具：{name}"
    if name in SEARCH_TOOLS or name.startswith("mcp__") or name.startswith("mcp."):
        return "searching", f"检索资料：{name}"
    # IDE 版 tool_name 经常为空：宁可显示「思考」，也不猜成「需要人处理」
    return "thinking", f"调用工具：{name}"


def map_hook(payload: dict, event_hint: str = "") -> StatusEvent:
    """把一次 Hook 的 stdin JSON 转成状态事件。任何异常都不得影响 AI 主流程。"""
    if not isinstance(payload, dict):
        return StatusEvent(None, "invalid_payload")
    event = str(payload.get("hook_event_name") or event_hint or "")
    session = str(payload.get("session_id") or "default")
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    # 显式覆盖：供 MCP / 自定义插件 / 手工脚本精确上报（如真实流式输出事件）
    override = payload.get("ai_status_state") or payload.get("status_state")
    if isinstance(override, str) and is_valid(override):
        return StatusEvent(override, "explicit_override", session,
                           _short(payload.get("msg")), bool(payload.get("force")),
                           bool(payload.get("release_sticky")))

    if event in ("SessionStart", "SessionEnd"):
        return StatusEvent("idle", "会话开始" if event == "SessionStart" else "会话结束",
                           session, force=True, release_sticky=True)
    if event == "UserPromptSubmit":
        return StatusEvent("thinking", "收到新需求：" + _short(payload.get("prompt")),
                           session, force=True, release_sticky=True)

    if event in ("PreToolUse", "PostToolUse"):
        if event == "PostToolUse" and _response_failed(payload.get("tool_response")):
            # 工具真的失败了：必须立刻红闪，并解除之前的等待态（能执行说明已放行）
            return StatusEvent("error", f"工具执行失败：{tool}", session,
                               release_sticky=True)
        if event == "PostToolUse":
            # 工具真的跑完了：回到「思考」紫色呼吸，同时说明此前若卡在确认已被处理掉。
            # 这一条也是 need_confirm / need_input / error 的主要解除信号。
            return StatusEvent("thinking", "工具完成，继续处理：" + tool, session,
                               release_sticky=True)
        state, msg = _tool_state(tool, tool_input, payload)
        return StatusEvent(state, msg, session)

    if event == "Notification":
        kind = str(payload.get("notification_type") or "")
        if kind == "permission_prompt":
            return StatusEvent("need_confirm", "等待用户授权", session,
                               _short(payload.get("message")))
        if kind in ("idle_prompt", "elicitation_dialog"):
            return StatusEvent("need_input", "等待用户回复", session,
                               _short(payload.get("message")))
        return StatusEvent(None, f"通知：{kind}", session)

    if event in CONFIRM_EVENTS:
        return StatusEvent("need_confirm", "等待用户确认", session)
    if event in FAILURE_EVENTS:
        return StatusEvent("error", f"任务异常：{event}", session)
    if event in INPUT_EVENTS:
        return StatusEvent("need_input", "等待用户补充信息", session)

    if event == "Stop":
        # 本轮完成 -> 绿色 10s，到期回 idle。
        # 同时解除等待态：一轮已经结束，说明之前的确认/追问都已处理完。
        return StatusEvent("done", "本轮任务完成", session, release_sticky=True)

    if event in NOOP_EVENTS:
        return StatusEvent(None, f"事件：{event}", session)

    return StatusEvent(None, f"未处理事件：{event or 'unknown'}", session)
