# 变更记录

本文件记录对外发布的版本。开发过程中的临时笔记、逐版调试记录已清理，只保留有参考价值的功能与兼容性结论。

## PC 端中间件

### v1.2.0（2026-11-08，当前发布版）

修复「需要人处理」状态从未被上报的问题。此前 IDE 版 Hooks 不发 `PermissionRequest` / `Notification` / `Elicitation`，`tool_name` 又常为空，导致 `need_confirm` / `need_input` 映射分支永远命中不到，PC 端一帧都没发给设备。

- **静默推断**（不依赖 IDE 专属事件）：每个 `PreToolUse` 开启等待确认观察，4s 内没有 `PostToolUse` 就判定卡在授权弹窗 → `need_confirm`；`PostToolUse` 与 `Stop` 开启等待回复观察，静默 2.5s → `need_input`。实测 IDE 常连 `Stop` 都不发，因此 `PostToolUse` 是关键观察点。
- **最高优先级锁定**：`need_confirm` / `need_input` 建立后，`coding` / `done` / `error` / 工具失败等任何普通事件都不能覆盖；只有用户回复（`UserPromptSubmit`）、工具继续执行（`PostToolUse`）、会话结束、`/clear` 或 `force` 才解除，解除时以 `force=1` 压过设备端 80/85/90 的优先级检查。
- **允许升级不允许降级**：`need_input`(80) 可被 `need_confirm`(85) 升级，反之不可；`error`(90) 不再抢占等待态。
- **审批/提问字段宽匹配**：识别 `requires_approval` / `requiresApproval` / `autoApprove:false` / `permission:"ask"` / `questions` 等多种写法，以及 `ask_followup_question` 等工具别名；命中即直接上报，无需等超时。
- **Hooks 补强事件默认注册**：`Notification`、`PermissionRequest`、`PermissionDenied`、`PostToolUseFailure`、`Elicitation` 并入默认安装片段，IDE 不发无副作用、发出即精确命中。
- **两段式灯效**：工具结束后先短暂 `outputting`（1.2s），再进入 `need_input`，避免灯直接跳变。
- **可配置 / 可关闭**：新增 `infer_need_confirm`、`need_confirm_after_ms`、`infer_need_input`、`need_input_after_ms`、`lock_human`；`outputting_after_ms` 默认调整为 1200ms。
- 修复多会话下解除等待态时 `sticky` 已清但 `current` 仍被别的会话等待态顶回来的失配。
- `/health` 增加 `waiting_for_human` / `watch`；`/events` 增加 `watch` / `release_sticky` 字段便于排错。
- 自检扩充到 67 项；用 50 条真实 Hooks 事件离线回放，PC 端从「0 帧等待态」变为打出 23 帧（22 `need_input` + 1 `need_confirm`，均带 `force`）。
- 协议 `ai-status/1` 与状态枚举**未改动**，固件无需升级；升级 PC 端后需重启 `start.bat`。

### v1.1.0（2026-11-08）

- 新增**来源 ID**：每台电脑首次运行生成稳定 ID 并持久化到 `client_id.json`；`python main.py identity`、`GET /identity`、`GET /health` 均可查。
- 所有串口帧（`status` / `heartbeat` / `clear` / `get` / `ping`）与 HTTP `POST /api/status` 统一带 `src` 字段；字段名可用 `source_field` 配置。
- `msg` 自动带 `[来源ID]` 前缀。设备会把 `msg` 原样放进 WS 广播 `status` 与 `/api/status`，因此**无需改固件即可在网页看到这盏灯由哪台电脑驱动**。
- 设备发现加入 mDNS `esp32s3-ai.local`，适配 v1.5.0 固件的 IP 变化场景。
- 识别 v1.5.0 的 `source_not_bound` 回执，给出「本机未被 ESP 绑定」提示，不再盲目重发。
- 自检扩充到 33 项（含来源 ID、标签、identity、Hook 子进程链路）。

### v1.0.0（2026-11-06）

- 首个可用版本：CodeBuddy Hooks → 本地事件总线 → 状态机 → ESP32-S3。
- 串口优先 / 局域网回落、`/api/hello` 发现、ARP 反查、/24 并发扫描、AP 兜底。
- 5s 心跳、15s 超时、seq 单调与自动重同步补发、指数退避重连。
- 多会话按优先级仲裁、粘性状态、300ms 最短驻留、`done` 5s 回落。
- 一键生成/安装 Hooks 配置、自检、真机演示命令。

## ESP32-S3 固件

| 版本 | 日期 | 主要内容 |
| --- | --- | --- |
| **v1.5.0 修复版** | 2026-11-08 | 按 `src` 为每个来源独立维护状态/seq/优先级/TTL/超时；可勾选绑定白名单（NVS 持久化）；网页「来源与绑定」卡片；mDNS + UDP 4210 广播发现；修复 WS 队列洪泛与 UDP 广播地址解析 |
| v1.5.0 | 2026-11-07 | 多来源与绑定初版、mDNS、UDP 发现 |
| v1.4.1 | 2026-11-07 | 状态灯亮度独立可调（`status_brightness`），NVS 持久化 |
| v1.4.0 | 2026-11-06 | AI 状态灯引擎、10 个状态、三通道统一承载、`/api/hello`、`/api/status`、CORS、`reqId` |
| v1.3 | 2026-11-06 | 原生 USB CDC + UART0 双串口桥接、Web 调试面板基线 |

## 协议

`ai-status/1` v1.0.0 自 2026-11-06 起保持不变；v1.5.0 固件**只新增可选字段 `src`**，旧字段语义一字未改，不带 `src` 的旧客户端统一视为匿名来源 `*`，完全向后兼容。
