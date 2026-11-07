"""CodeBuddy Hooks 事件 -> ai-status/1 状态映射。

IDE（Craft Agent）与 CLI 的工具名不同，这里同时兼容两套别名。
参考：CodeBuddy IDE Hooks User Guide 附录 E、CLI Hooks Reference 事件矩阵。
"""
from __future__ import annotations

from dataclasses import dataclass

from .protocol import is_valid

# 找资料 / 检索 / 读文档
SEARCH_TOOLS = frozenset({
    "web_search", "WebSearch", "web_fetch", "WebFetch", "RAG_search",
    "search_content", "Grep", "search_file", "list_dir", "Glob",
    "read_file", "Read", "codebase_search", "Task", "task",
    "mcp__*",  # 由前缀匹配单独处理
})

# 写脚本 / 改文件 / 执行命令
CODE_TOOLS = frozenset({
    "write_to_file", "Write", "replace_in_file", "Edit", "MultiEdit",
    "NotebookWrite", "delete_file", "execute_command", "Bash",
    "apply_patch", "create_file", "install_binary",
})

# 主动向用户提问（IDE / CLI / 常见别名）
INPUT_TOOLS = frozenset({
    "ask_followup_question", "AskUserQuestion", "AskUser", "ask_user",
    "askUser", "prompt_user", "Elicitation",
})

# 等待拍板 / 授权
CONFIRM_EVENTS = frozenset({
    "PermissionRequest", "PermissionPrompt", "ToolPermissionRequest",
})
# 异常 / 失败
FAILURE_EVENTS = frozenset({"PostToolUseFailure", "PermissionDenied", "StopFailure", "TaskFailed"})
# 需要用户补充信息
INPUT_EVENTS = frozenset({"Elicitation", "ElicitationDialog", "AskUser"})

# Hook 载荷里表示「需要审批」的字段别名。IDE 版字段不稳定（且 tool_name 常为空），
# 所以对 payload / tool_input / 常见嵌套层做宽匹配，只认键名不猜正文内容。
APPROVAL_KEYS = frozenset({
    "requires_approval", "requiresapproval", "need_approval", "needapproval",
    "approval_required", "approvalrequired", "requires_confirmation",
    "requiresconfirmation", "confirm", "confirmation", "permission",
    "permission_decision", "permissiondecision", "is_dangerous", "dangerous",
})
# 这些字段取反：值显式为 False 才表示需要人工确认
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
    # 本事件之后若持续静默，就推断为「等待人工处理」：
    # "confirm" -> need_confirm（工具卡在授权），"input" -> need_input（本轮结束等回复）
    watch: str = ""


def _short(value, limit: int = 60) -> str:
    text = str(value or "").strip().replace("\n", " ")
    return text[:limit]


def _response_failed(response) -> bool:
    if isinstance(response, dict):
        if response.get("success") is False or response.get("is_error") is True:
            return True
        code = response.get("exitCode", response.get("exit_code"))
        if isinstance(code, int) and code != 0:
            return True
        if response.get("error"):
            return True
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


def _tool_state(tool_name: str, tool_input: dict, payload: dict) -> tuple[str, str, str]:
    """返回 (state, msg, watch)。

    IDE 版 Hooks 不发送 PermissionRequest / Notification，且 tool_name 经常为空，
    因此除了显式审批字段，还要给每个 PreToolUse 挂一个「静默观察」：
    若 need_confirm_after_ms 内没有 PostToolUse，就判定工具卡在授权弹窗。
    """
    name = tool_name or ""
    if _input_hint(payload, tool_input, name):
        return "need_input", f"等待用户回复：{name or '提问'}", ""
    if _approval_hint(payload, tool_input):
        return "need_confirm", f"等待用户确认：{name or '授权'}", ""
    if name in CODE_TOOLS:
        return "coding", f"执行工具：{name}", "confirm"
    if name in SEARCH_TOOLS or name.startswith("mcp__") or name.startswith("mcp."):
        return "searching", f"检索资料：{name}", "confirm"
    return "thinking", f"调用工具：{name}", "confirm"


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

    if event == "SessionStart":
        return StatusEvent("idle", "会话开始", session, force=True, release_sticky=True)
    if event == "SessionEnd":
        return StatusEvent("idle", "会话结束", session, force=True, release_sticky=True)
    if event == "UserPromptSubmit":
        return StatusEvent("thinking", "收到新需求：" + _short(payload.get("prompt")),
                           session, force=True, release_sticky=True)

    if event in ("PreToolUse", "PostToolUse"):
        if event == "PostToolUse" and _response_failed(payload.get("tool_response")):
            # 工具报错不等于「用户处理完等待态」：仍受锁定保护，不能盖掉等待灯
            return StatusEvent("error", f"工具执行失败：{tool}", session)
        if event == "PostToolUse":
            # 双重含义：① 工具真的继续执行/结束了，之前卡住的授权已处理掉；
            # ② 之后若持续静默，说明 AI 本轮收尾、正在等用户回复（IDE 版往往
            #    根本不发 Stop，所以这个观察点比 Stop 更可靠）。
            return StatusEvent(None, f"工具完成：{tool}", session,
                               release_sticky=True, watch="input")
        state, msg, watch = _tool_state(tool, tool_input, payload)
        return StatusEvent(state, msg, session, watch=watch)

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
        # 本轮结束 => 之后只可能在等用户下一条消息。先保持 done 灯效，
        # 由状态机在 need_input_after_ms 后升级为 need_input（粘性、不可覆盖）。
        return StatusEvent("done", "本轮任务完成", session, watch="input")

    if event in NOOP_EVENTS:
        return StatusEvent(None, f"事件：{event}", session)

    return StatusEvent(None, f"未处理事件：{event or 'unknown'}", session)
