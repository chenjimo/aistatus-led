# 变更记录

本文件记录对外发布的版本。开发过程中的临时笔记、逐版调试记录已清理，只保留有参考价值的功能与兼容性结论。

## PC 端中间件

### v2.0.0（2026-11-09，当前发布版）

**V2 大版本：一套硬件，三套 AI 编辑器 + 手机端。**

- **手机桥接一体化**：`tools/pc-bridge` 纳入本仓库，由 `start.bat → main.py start` 自动拉起、随服务停止、崩溃自拉起；UDP `4211` 局域网发现 + TCP `8770` HTTP/WS；**固定配对码 `123456`**；远控 V2 默认开启；8765/8770 均单实例保护。
- **一键全开**：`start.bat` 自动检查并放行防火墙（缺失且非管理员时一次 UAC），随后启动中间件与手机桥接；后台自启动用 `--no-elevate` 不弹窗。
- **三套 harness 深度校准**：Codex 原生 `PermissionRequest`；DSH 按 `app.asar` 核实的 31 个真实事件（`approval/request`、`assistant/message`、`session/event(type=turn/end)`）；工具完成后保持 `coding/searching`，`done` 10s 回 `idle`，启动期生命周期事件不再误报思考。
- **DSH 插件按官方 schema 用 `insert` patch 安装**（`cordis.patch.yml`，相对路径），加载时上报 `aistatus/plugin-loaded` 自检。
- **稳定性**：HTTP 断连静默、`/health` 无锁、设备发现移出全局锁、流式事件节流、两处单实例保护。
- 自检 107 项。新增文档 `docs/06-V2功能与使用说明.md`。
- 关联手机项目：微信小程序 <https://gitee.com/chenjimo/we-chat-mini-demo>、鸿蒙 <https://gitee.com/chenjimo/harmony-bridge>。

### v1.4.0（2026-11-08）

**一套灯，三套 harness 通用**：把状态映射从代码抽到 `profiles/*.json`，CodeBuddy / OpenAI Codex CLI / DeepSeek (DSH) 任意一个打开，都能驱动同一套 ESP32 灯。

- **映射外置**：`profiles/codebuddy.json`、`profiles/codex.json`、`profiles/dsh.json`。`mapper.py` 只做「选 profile → 事件归一化 → 动作分发」，新增 harness 只需加一份 JSON。
- **Codex 适配**：`install-hooks --harness codex` 写入 `~/.codex/hooks.json`（11 个事件，命令带 `--profile codex`）。Codex 原生有 `PermissionRequest`，确认框弹窗时能亮橙灯。工具名差异已处理：`apply_patch` → coding，`Bash` 里跑 grep/rg/find 等 → searching，其余命令 → coding。
- **DSH 适配**：`install-hooks --harness dsh` 生成 `tools/dsh-plugin/`（Cordis 插件骨架），订阅 `approval/request`、`tools/pre-execute`、`tools/post-execute`、`session/*`、`turn/end` 转发到本地 `/event`（带 `_profile: dsh`）。
- **CodeBuddy 不变**：默认 profile，命令与旧版完全一致，`install-hooks --write` 幂等、不重复注册。
- **自启动一拖三**：`tools/codebuddy-autostart.ps1` 监听 CodeBuddy / Codex / DSH，任一进程出现就拉起服务；新增 `-Stop`（只停服务）、`-Status`（查看状态）、`-Uninstall`（移除自启动并停 watcher）、`-StopWhenAllClosed`（全部关闭时停服务）。
- 自检扩充到 107 项（新增三套 profile 的事件/工具映射用例）。
- **修复日志刷屏**：客户端提前断开（WinError 10053 / ConnectionAbortedError）时，`_json` 写响应与服务器 `handle_error` 都静默处理，不再打印 traceback（这类断连来自探活超时、浏览器刷新/关闭，不是服务端错误）。
- **修复 `/health` 超时**：设备发现（串口扫描 / LAN 扫描 / mDNS，可能几十秒）移出全局锁，改到 `_tick_once` 的锁外执行，`/health` 不再被重连阻塞；主循环单次异常也不会终止服务。
- **DSH 适配按真实事件名校正**：从 `DeepSeek Harness/resources/app.asar` 核实事件字符串，把不存在的 `turn/stop` 改为 `turn/end`、补 `approval/asked` / `approval/decided`；`approval/decided` 放行后立即解除橙闪、拒绝报 `error`。新增 `tools/dsh-plugin/install.ps1` 一键把插件装进 `~/.dsh/profiles/desktop`（备份 + 复制到 `node_modules` + 追加 `cordis.patch.yml`），插件对 Cordis waterfall 事件透传 `next()`，绝不阻断 DSH 自己的审批流程；`install-hooks` 不再覆盖已校正的插件。
- **DSH 精准识别与状态闭环（全量校正）**：从 `app.asar` 提取全部事件名，修正 `session/end`（实际不存在，真实是 `session/close` / `session/disposed`）；补全 `turn/start`、`tool/call` / `tools/call` / `tools/execute`、`tool/result`、`agent/pre-step`、`notifications/elicitation`、`assistant/message` / `assistant/chunk` 等共 31 个真实事件。新增 `outputting`（输出）与 `post_tool_keep`（工具完成后**保持** coding/searching，不瞬间冲回 thinking）；`approval/decided` 放行立即解除橙闪，`turn/end` → done → 10s → idle，`session/close` → idle，形成完整闭环。插件加载时上报 `aistatus/plugin-loaded` 自检事件，用于区分「插件没加载」与「事件名不对」；waterfall 事件一律透传 `next()`。自检 107 项。
- **修复 DSH 插件根本没被加载（根因：patch 语法）**：实测 DSH 重启后 `/events` 仍无任何 DSH 事件。用 DSH 自带 CLI 的 `--dump-config-schema` / `--dump-config` 取证：`cordis.patch.yml` 里的 `{id,name,config}` 是 **id-targeted override**，id 不存在会被跳过（`entry "aistatus-led" not found`）；`dsh.profile.bundles` 里的本地包名也解析不到。**新增插件必须用 `- insert: [...]`**，且 `name` 可用相对路径（以 patch 文件目录为锚点）。`install.ps1` 已改为追加 insert patch 指向 `./node_modules/aistatus-led-dsh/index.mjs`，并清理旧的 bundles 改动；用官方 `--dump-config` 验证组合树里出现 `aistatus-led` 且解析为正确的 `file:///` 路径，Node 从该 URL 加载成功并上报 `aistatus/plugin-loaded`。`desktop` profile 由 Electron 独占管理，CLI 只能用于只读诊断。
- **修复 DSH「任务完成不变绿灯」**：从 `app.asar` 源码确认 `turn/end` **不是 Cordis 事件**——DSH 用 `session.append("turn/end", ...)` 写会话日志，并通过 `session/event` 的 `type` 字段发出；原先订阅 `ctx.on('turn/end')` 永远收不到，灯一直停在 `outputting`。插件改为订阅 `session/event` 按 `type` 重新分发 `turn/start` / `turn/end`，并订阅 `agent/turn-stopping` 兜底 `done`；流式输出事件 300ms 节流避免刷满 `/events`。`profiles/dsh.json` 补 `agent/turn-stopping → done`、`session/event → noop`。本地模拟 `assistant-stream → outputting → turn/end → done` 通过，107 项自检。
- **修复 DSH「一启动就显示思考」**：`aistatus/plugin-loaded` 原为 `noop`（不改状态，继承上一轮的 thinking），而 `agent/created` / `session/agent-busy` 属于**启动期生命周期事件**却被映射成 `busy`（thinking）。现改为：插件加载事件 → `session_start`（idle，force 解除粘性）、`agent/created` / `session/agent-busy` → `noop`。只改 PC 侧 `profiles/dsh.json` 并重启中间件即可生效，**无需重启 DSH**。实测 `plugin-loaded→idle → agent/created→idle → user/message→thinking → tools/pre-execute→coding → turn/end→done` 全部通过。
- **集成手机桥接（HarmonyOS App）**：把 `HarmonyOS_App/pc-bridge` 纳入 `tools/pc-bridge/`，由 `start.bat → main.py start` 自动拉起、随服务停止、崩溃自动重启，不再需要单独双击 `start_bridge.bat`。启动器生成 `bridge_config.json`（**固定配对码 `123456`**、TCP `8770`、UDP 发现 `4211`、状态源 `8765`），并自动尝试放行 Windows 防火墙入站规则（非管理员记 `need_admin`，不阻断启动）；已占用 8770 时会明确告警而不是静默跳过。新增 `main.py phone-bridge status|firewall|pin <6位>` 与管理员一键脚本 `tools/open-ports-admin.bat`；配置在 `config.json` 的 `phone_bridge_*`，`phone_bridge_enabled=false` 可关闭。实测 UDP 广播发现 → `offer(192.168.1.4:8770)`、`/v1/hello`、用 `123456` 配对成功、`/v1/snapshot` 返回 `state=searching` 全部通过。
- **手机桥接单实例与远控开关**：实测发现 `8770` 曾被两个进程同时绑定（集成版 + 手动 `bridge.py --enable-input`），Windows `SO_REUSEADDR` 会把手机连接随机分给不同实例。给 `bridge.py` 加端口占用探测与 `allow_reuse_address=False`，`PhoneBridgeManager` 的 watchdog 重启前也检查端口；新增 `phone_bridge_enable_input`（默认 false），需要远控 V2 时在上层 `config.json` 打开即可，不必再单独跑 `start_bridge.bat`。清理后 8770 仅一个实例。
- **`start.bat` 一键全开 + 远控默认开启**：`phone_bridge_enable_input` 默认改为 `true`（`DEFAULT_CONFIG`、`config.example.json` 同步）；`start.bat` 先检查两条防火墙规则，缺失且非管理员时自动 UAC 提权一次放行 TCP 8770 / UDP 4211，规则建好后不再弹窗，随后启动中间件（自动拉起手机桥接与远控 V2）。后台自启动 watcher 传 `--no-elevate`，避免在隐藏窗口触发 UAC。实测 `--no-elevate` 启动后 `bridge_config.enable_input=true`、`/v1/hello` 返回 `input.enabled=true`、PIN `123456`、107 项自检通过；两条防火墙规则均已存在，双击 `start.bat` 不再需要提权。

### v1.3.1（2026-11-08）

修复「失败报不出来」和「处理完了灯不消」两个真实问题，全部基于实测取证。

- **失败识别（根因）**：CodeBuddy IDE 的 `PostToolUse.tool_response` 是**纯字符串**，失败时形如 `Error calling tool: Could not find file 'x'.`，而 v1.3.0 只处理 dict，导致工具失败**从未上报过 `error`**。现在认 `Error calling tool:` / `tool execution failed` / `command failed` / `permission denied` 等前缀，并兼容 dict 的 `success:false` / `isError` / `exitCode≠0`。只认前缀，文件内容里出现 `error` 字样不误报。
- **红闪不卡死**：`error` 立即以 `force=1` 推送，保持 `error_hold_ms`（3s）后若没有新事件就自动消退回工作态；AI 继续干活则立即解除。`stalled_timeout` 的 error 仍保持到有人处理。
- **解除不及时（根因）**：IDE 实测 `UserPromptSubmit` 经常不发、`PostToolUse` 也缺（某次 25 vs 18）。现在 `need_confirm` / `need_input` 一旦 AI 重新开始调工具（任意 `PreToolUse`）或本轮结束（`Stop`）就立即解除，不再依赖不一定存在的事件。
- **`stalled_after_ms` 默认改 0（关闭）**：它只能按「多久没事件」猜卡住，而 IDE 对 `execute_command` 根本不发 Hooks，长命令必然被误判成红闪。需要兜底时设 `300000`。
- **Hooks 补强事件真正落到配置**：`install-hooks --write --scope user` 会把 `Notification` / `PermissionRequest` / `PermissionDenied` / `PostToolUseFailure` / `Elicitation` 写进 `settings.json`（此前只有 7 个事件，确认框通道根本没开）。
- `/events` 增加 `tool` / `tool_response` / `keys` 字段，便于核对是「没发事件」还是「发了没认出来」。
- 自检扩充到 88 项，新增真实失败格式、内容含 error 不误报、红闪自动消退、`need_confirm` 被 `PreToolUse` 解除、`Stop` 解除等待态等回归用例。
- 固件同步为 **v1.5.0 灯效可配置版**（`done` 绿色慢闪 10s、每状态 RGB / 效果 / 亮度 / 周期可配 + `/api/fx`），仓库副本已脱敏。

### v1.3.0（2026-11-08）

**回退 v1.2.0 的静默推断，改为零猜测的状态分类**。v1.2.0 把「深度思考时长时间无事件」误判成 `need_input` / `need_confirm`，实际使用比 v1.1.0 更糟。

- 实测证据：某次 39 条事件中 `PreToolUse` 29 / `PostToolUse` 9（完成事件大量缺失）；另一批 50 条事件里静默窗口 5~60 秒共 20+ 个。v1.2.0 在这 50 条上打出 23 帧等待态，几乎全是误报；v1.3.0 为 **0 帧**。
- 移除 `watch` 观察窗口与 `_enter_human`；`infer_need_confirm` / `need_confirm_after_ms` / `infer_need_input` / `need_input_after_ms` 全部废弃（旧 `config.json` 里残留无害，会被忽略）。
- 状态只由真实事件驱动，按「空闲 / 工作中 / 完成 / 需要我处理」分清：
  - `UserPromptSubmit`、工具名为空、`PostToolUse` 成功 → `thinking`（紫色呼吸）；**深度思考不再变等待态**。
  - 检索 / 读文件类 → `searching`（黄色慢闪）；写文件 / 执行命令类 → `coding`（浅蓝青呼吸）。
  - `Stop` → `done`，**10 秒**后 `force` 回 `idle`；`done_hold_ms=10000`、帧 `ttl=10000` 覆盖固件默认 5000ms，协议与固件都不用改。
  - `outputting`（渐变流动）默认不再由静默推断，只认显式 `ai_status_state`；`infer_outputting` 默认 `false`。
  - `need_confirm` / `need_input` / `error` 三类快闪只由明确的审批字段、提问工具、补强 Hooks 事件、工具失败事件触发；仍保持最高优先级锁定与受控解除。
- 新增「处理时间过长」归为红色 `error`：工作态连续 `stalled_after_ms`（默认 180000ms = 3 分钟）无任何事件才触发，深度思考不会误报；`error_hold_ms`（3s）保证红闪看得见，之后 AI 恢复工作自动清除；设 `0` 可关闭。
- `idle_after_ms` 默认 600000；`/health` 用 `quiet_ms` / `stalled_after_ms` 取代 `watch`；`/events` 去掉 `watch` 字段。
- 自检扩充到 74 项，新增「静默 1/10/60/300 秒不误报」「缺 `PostToolUse` 不误判」「完成 10s 带 force/ttl」「卡住 3 分钟归 `error`」等回归用例。
- 协议 `ai-status/1` 状态枚举与字段**未改动**，固件无需升级；升级 PC 端后需重启 `start.bat`。

### v1.2.0（2026-11-08，已由 v1.3.0 取代，不建议使用）

> 本版的静默推断会把深度思考误报成等待态，请直接升级到 v1.3.0。以下内容仅作变更记录。

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
| **v1.5.0 灯效可配置版** | 2026-11-08 | 默认灯效重做（工作态偏暗、交互态偏亮）；新增双色渐变呼吸 `FX_GRADIENT`；每个状态的主色/辅色/效果/亮度/周期可在网页配置并写入 NVS（`GET|POST /api/fx`）；网页卡片可拖动排序（localStorage 记忆，页头可重置）、灯效配置默认折叠；协议与 `fw` 版本号不变 |
| v1.5.0 修复版 | 2026-11-08 | 按 `src` 为每个来源独立维护状态/seq/优先级/TTL/超时；可勾选绑定白名单（NVS 持久化）；网页「来源与绑定」卡片；mDNS + UDP 4210 广播发现；修复 WS 队列洪泛与 UDP 广播地址解析 |
| v1.5.0 | 2026-11-07 | 多来源与绑定初版、mDNS、UDP 发现 |
| v1.4.1 | 2026-11-07 | 状态灯亮度独立可调（`status_brightness`），NVS 持久化 |
| v1.4.0 | 2026-11-06 | AI 状态灯引擎、10 个状态、三通道统一承载、`/api/hello`、`/api/status`、CORS、`reqId` |
| v1.3 | 2026-11-06 | 原生 USB CDC + UART0 双串口桥接、Web 调试面板基线 |

## 附属工具（不改变中间件版本）

### codebuddy-autostart.ps1（2026-11-08）

- 位置 `tools/codebuddy-autostart.ps1`，随 PC 发布包一起分发；**不改变 `ai-status/1` 协议，也不改变 PC 端版本号**（中间件仍为 v1.3.0）。
- 免管理员的登录自启：写入 HKCU Run，打开 CodeBuddy 时确保 `start.bat` 运行；探 `/health` 做幂等，绝不重复起服务；服务意外退出时最多每 30 秒拉起一次。
- 自带 `-Install` / `-Uninstall`；`<APP>` 路径由 `$PSScriptRoot` 推导，无硬编码本机路径，整目录复制到新电脑即可用。
- 修复「安装进程自身命令行被误判为已有监视器」导致装完不启动的问题（匹配条件限定 `-WindowStyle Hidden` 且排除 `-Install` / `-Uninstall`）。
- 完整使用与验收见 `docs/01` 第 10 节。

## 协议

`ai-status/1` v1.0.0 自 2026-11-06 起保持不变；v1.5.0 固件**只新增可选字段 `src`**，旧字段语义一字未改，不带 `src` 的旧客户端统一视为匿名来源 `*`，完全向后兼容。
