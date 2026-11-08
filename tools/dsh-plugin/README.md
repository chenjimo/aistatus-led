# aistatus-led（DeepSeek Harness 插件）

把 DSH 事件总线上的真实事件转发到本机 AI 状态灯中间件，驱动同一颗 ESP32 灯。

## 事件名已从 DSH 应用包核实

事件名直接从 `DeepSeek Harness\resources\app.asar` 的字符串中提取，与 `profiles/dsh.json` 一一对应：

| 类别 | 事件 | 灯具状态 |
| --- | --- | --- |
| 会话 | `session/created` | idle |
| 会话 | `session/close` / `session/disposed` | idle（对话关闭） |
| 本轮 | `turn/start` / `user/message` / `agent/request` | thinking |
| 本轮 | `session/event`（`type=turn/end`）/ `agent/turn-stopping` | **done 绿灯 10s** |

> `turn/end` **不是** Cordis 事件：DSH 用 `session.append("turn/end", ...)` 写会话日志，
> 并通过 `session/event` 的 `type` 字段发出。插件订阅 `session/event` 后按 `type` 重新分发，
> 另订阅 `agent/turn-stopping` 兜底。流式输出事件按 300ms 节流，避免刷满 `/events`。
| 工具 | `tools/pre-execute` / `tools/call` / `tool/call` / `tools/execute` | 按工具名 → searching / coding |
| 工具 | `tools/post-execute` / `tools/result` / `tool/result` | **保持当前工作态**（失败才转 error） |
| 审批 | `approval/request` / `approval/asked` | **need_confirm 橙闪** |
| 审批 | `approval/decided` | 放行 → 立即解除橙闪；拒绝 → error |
| 提问 | `notifications/elicitation` / `question/request` | need_input 粉闪 |
| 输出 | `assistant/message` / `assistant/chunk` / `assistant/live-chunk` / `agent/assistant-stream` | **outputting 渐变流动** |
| 异常 | `agent/error` / `agent/request-error` | error 红闪 |

### 状态闭环

- 工具跑完**不立刻回 thinking**，而是保持 `coding` / `searching`，直到下一个工具事件或 `turn/end`——避免"写代码"灯一闪而过；
- `approval/decided` 带 `release_sticky`，用户点完"允许/拒绝"橙闪立即消除，不会一直亮；
- `turn/end` → `done`（10s）→ 自动回 `idle`；`session/close` 直接回 `idle`。

插件是**纯旁路观察者**：对 Cordis waterfall 事件总是调用 `next()` 透传，绝不阻断 DSH 自己的审批/工具流程。

## 安装（自动）

```bat
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

脚本会备份 `<profile>/cordis.patch.yml`、把本目录复制到
`<profile>/node_modules/aistatus-led-dsh`，并在 `cordis.patch.yml` 末尾追加一个
**`insert` patch** 来声明新插件。然后**必须彻底退出并重启 DeepSeek Harness**（插件是启动时加载的）。

> 关键：DSH 的 patch 有两种条目。`{id,name,config}` 是 **id-targeted override**（覆盖已有条目，
> id 不存在会被跳过并告警 `entry not found`）；**新增插件必须用 `- insert: [...]`**。
> 相对路径以 `cordis.patch.yml` 所在目录为锚点，所以
> `./node_modules/aistatus-led-dsh/index.mjs` 会解析到本 profile 的插件副本。
>
> 可用官方命令自行验证组合树：
> `dsh --profile web --patch "%USERPROFILE%\.dsh\profiles\desktop\cordis.patch.yml" --dump-config`
> 输出里应能看到 `aistatus-led` 及 `file:///.../node_modules/aistatus-led-dsh/index.mjs`。

```bat
powershell -ExecutionPolicy Bypass -File .\install.ps1 -Status      :: 查看安装状态
powershell -ExecutionPolicy Bypass -File .\install.ps1 -Uninstall   :: 卸载并还原
```

## 验证是否加载成功

插件每次被加载时都会立刻上报一条 `aistatus/plugin-loaded`。重启 DSH 后查：

```bat
python -c "import urllib.request,json;e=json.load(urllib.request.urlopen('http://127.0.0.1:8765/events'))['events'];print([x['event'] for x in e[-10:]])"
```

- 看到 `aistatus/plugin-loaded` → 插件已加载，之后每类事件都应进来；
- 看不到 → 插件没被加载（检查是否真的重启了 DSH、`install.ps1 -Status` 是否 installed）。

想看插件自身日志：设置环境变量 `AISTATUS_DEBUG=1` 后重启 DSH，日志写到
`~/.dsh/aistatus-led.log`。

## 端口

默认 8765。改端口时设置 `AISTATUS_PORT`，或改 `cordis.patch.yml` 里的 `config.port`。

## 手动安装（若自动脚本不适用）

1. 把本目录整体复制到 `%USERPROFILE%\.dsh\profiles\desktop\node_modules\aistatus-led-dsh`；
2. 在 `%USERPROFILE%\.dsh\profiles\desktop\cordis.patch.yml` 末尾追加（先备份该文件）：

   ```yaml
   - insert:
       - id: aistatus-led
         name: "./node_modules/aistatus-led-dsh/index.mjs"
         config:
           port: 8765
   ```

3. 彻底退出并重启 DeepSeek Harness。
