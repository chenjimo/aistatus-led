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

# 主动向用户提问
INPUT_TOOLS = frozenset({"ask_followup_question", "AskUserQuestion"})

# 等待拍板 / 授权
CONFIRM_EVENTS = frozenset({"PermissionRequest"})
# 异常 / 失败
FAILURE_EVENTS = frozenset({"PostToolUseFailure", "PermissionDenied", "StopFailure", "TaskFailed"})
# 需要用户补充信息
INPUT_EVENTS = frozenset({"Elicitation"})

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


def _tool_state(tool_name: str, tool_input: dict) -> tuple[str, str]:
    name = tool_name or ""
    if name in INPUT_TOOLS:
        return "need_input", f"向用户提问：{name}"
    if tool_input.get("requires_approval") is True:
        return "need_confirm", f"等待授权：{name}"
    if name in CODE_TOOLS:
        return "coding", f"执行工具：{name}"
    if name in SEARCH_TOOLS or name.startswith("mcp__") or name.startswith("mcp."):
        return "searching", f"检索资料：{name}"
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
                           _short(payload.get("msg")), bool(payload.get("force")))

    if event == "SessionStart":
        return StatusEvent("idle", "会话开始", session, force=True, release_sticky=True)
    if event == "SessionEnd":
        return StatusEvent("idle", "会话结束", session, force=True, release_sticky=True)
    if event == "UserPromptSubmit":
        return StatusEvent("thinking", "收到新需求：" + _short(payload.get("prompt")),
                           session, force=True, release_sticky=True)

    if event in ("PreToolUse", "PostToolUse"):
        if event == "PostToolUse" and _response_failed(payload.get("tool_response")):
            return StatusEvent("error", f"工具执行失败：{tool}", session)
        if event == "PostToolUse":
            # 工具结束：保持当前状态，仅刷新活跃时间（供“输出中”推断使用）
            return StatusEvent(None, f"工具完成：{tool}", session)
        state, msg = _tool_state(tool, tool_input)
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
        return StatusEvent("done", "本轮任务完成", session)

    if event in NOOP_EVENTS:
        return StatusEvent(None, f"事件：{event}", session)

    return StatusEvent(None, f"未处理事件：{event or 'unknown'}", session)
