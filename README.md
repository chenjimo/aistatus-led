# aistatus-led

把 CodeBuddy（或其它支持 Hooks 的 AI 编辑器）的运行状态，通过 **ESP32-S3 板载的一颗 WS2812 彩灯**实时显示出来：不用盯着屏幕，也能知道 AI 是在思考、查资料、写代码、等你确认，还是已经完成。

- PC 端：常驻中间件，采集 Hooks 事件 → 状态机 → 串口 / 局域网发给设备
- 设备端：ESP32-S3 固件，按状态显示颜色与动效，支持多台电脑来源绑定
- 协议：`ai-status/1`，串口 JSON Lines / HTTP / WebSocket 三通道语义一致

> 仓库：<https://gitee.com/chenjimo/aistatus-led.git>

---

## 一、它能显示什么

| 状态 | 含义 | 灯效 |
| --- | --- | --- |
| `idle` | 空闲待命 | 暗蓝常亮 |
| `thinking` | 思考中 / 收到需求 | 紫色慢呼吸 |
| `searching` | 找资料 / 检索 / 读文件 | 黄色慢闪 |
| `coding` | 写脚本 / 改文件 / 执行命令 | 蓝青快呼吸 |
| `outputting` | 输出回答中 | 渐变流动 |
| `done` | 本轮完成 | 绿色常亮，10 秒后回 `idle` |
| `need_input` | 需要你补充信息 | 粉白双闪（粘性快闪） |
| `need_confirm` | 需要你确认 / 授权 | 橙色快闪（粘性） |
| `error` | 工具失败 / 处理时间过长 | 红色急闪（粘性） |

**v1.3.0 起状态只认真实事件，不做静默猜测**：深度思考（长时间无事件）稳定显示紫色呼吸，不会再被误判成「需要人处理」。等待态只由明确的确认 / 提问 / 失败信号触发；真卡住超过 3 分钟归为红色 `error`。代价是 IDE 版实测不发确认 / 提问事件，这两个灯在 IDE 下基本不会自动亮——宁可不亮，也不乱亮，详见 `docs/02` 第七节。

等待态仍是**最高优先级**：一旦亮起，`coding`、`done`、工具报错等任何状态都盖不掉，只有你回复、点了确认（工具继续执行）、会话结束或 `python main.py clear` 才会解除。其余粘性状态不会被超时冲掉；PC 停止心跳 15 秒后设备自动转 `offline`（极暗蓝）。

---

## 二、版本矩阵（先看这个，别拿错版本）

| 组件 | 版本 | 位置 |
| --- | --- | --- |
| **PC 端中间件** | **v1.3.0** | `main.py` + `ai_status/` |
| **ESP32-S3 固件** | **v1.5.0（修复版）** | `esp32/firmware/ESP-S3-Demo1.ino` |
| 通信协议 | `ai-status/1` v1.0.0（+ 可选 `src`） | `docs/04-协议与来源ID绑定.md` |
| Python | 3.10+（实测 3.10.6） | 需加入 PATH |
| 操作系统 | Windows（串口直连）；局域网模式见下方限制 | — |
| Arduino 框架 | Arduino-ESP32 Core 3.2.1 / ESP-IDF 5.x | 编译固件时用 |
| 目标板 | ESP32-S3-N8R2（8MB Flash / 2MB PSRAM） | — |

详细对应关系见 `docs/05-版本矩阵与兼容性.md`。

---

## 三、目录结构

```
aistatus-led/
├── README.md                    ← 本文件：总览与快速开始
├── CHANGELOG.md                 ← 版本变更记录
├── docs/
│   ├── 01-换环境从零部署.md     ★ 换电脑/换网络/下载后要改什么，看这篇
│   ├── 02-PC端功能与配置.md     ← 全部命令、配置项、状态机行为
│   ├── 03-ESP32端固件与接口.md  ← 编译烧录、接口总表、绑定白名单
│   ├── 04-协议与来源ID绑定.md   ← 状态枚举、帧格式、src 约定
│   └── 05-版本矩阵与兼容性.md   ← 版本对应与升级注意
├── main.py                      ← PC 端入口（Hooks 命令指向它，别随意挪位置）
├── ai_status/                   ← PC 端源码（协议/映射/状态机/串口/服务）
├── config.example.json          ← 配置样例，复制成 config.json 后修改
├── requirements.txt             ← 可选依赖（pyserial），不装也能跑
├── start.bat                    ← Windows 一键启动
├── tools/
│   └── codebuddy-autostart.ps1  ← 打开 CodeBuddy 自动启动服务（含安装/卸载）
├── esp32/
│   ├── README.md                ← 设备端说明入口
│   └── firmware/
│       └── ESP-S3-Demo1.ino     ← v1.5.0 固件源码（WiFi 已脱敏为占位符）
└── packages/
    ├── aistatus-led-pc-v1.3.0.zip      ← PC 端当前发布包
    ├── aistatus-led-pc-v1.2.0.zip      ← 历史包（静默推断版，误报多，勿用）
    ├── aistatus-led-pc-v1.1.0.zip      ← 历史包
    └── aistatus-led-esp32-v1.5.0.zip   ← 设备端独立发布包
```

> 发布包与仓库内容一致，只是去掉了 Git 元数据，方便直接拷走。

---

## 四、5 分钟快速开始（Windows + 已有设备）

```powershell
# 1. 获取代码
git clone https://gitee.com/chenjimo/aistatus-led.git
cd aistatus-led

# 2. 确认 Python 可用（3.10+）
python --version

# 3. 查看/生成这台电脑的来源 ID，并记下来（ESP 网页绑定要用）
python main.py identity

# 4. 安装 Hooks（自动写入本机的 Python 路径和项目路径，会备份原配置）
python main.py install-hooks --write --scope user

# 5. 重启或新建一个 CodeBuddy 会话（CLI 用户还需在 /hooks 面板审核）

# 6. 启动常驻服务
start.bat
```

然后随便给 AI 发一句需求，灯应当依次变化。手动验证灯效：

```powershell
python main.py probe          # 看设备是否找到（串口优先 → 局域网）
python main.py demo           # 依次演示全部状态灯效
python main.py emit need_confirm   # 橙色快闪，会一直保持
python main.py clear          # 看完后解除
```

服务窗口不要关；关掉后设备 15 秒无心跳会自动转 `offline`。

> **从旧版本升级**：务必关掉旧窗口、重新运行 `start.bat`，旧进程不会自动加载新代码。可先跑 `python main.py selftest`（74 项，不需要硬件）确认升级无误。**v1.2.0 用户请务必升级**：该版会把深度思考误报成等待态。

> **不想每次手动开服务**：跑一次 `powershell -ExecutionPolicy Bypass -File .\tools\codebuddy-autostart.ps1 -Install`，之后每次登录并打开 CodeBuddy 会自动拉起服务（免管理员、幂等、可卸载）。详见 `docs/01` 第 10 节。

---

## 五、换电脑 / 换环境

**这是最容易踩坑的部分，请完整阅读 `docs/01-换环境从零部署.md`**。三个必做点先列在这里：

1. **Hooks 每台电脑都要重装一次**：配置在各电脑自己的 `~/.codebuddy/settings.json`，且命令里含本机 Python 与项目绝对路径，复制目录不会带过去。新电脑跑一次 `python main.py install-hooks --write --scope user` 即可自动生成。
2. **不要复制 `client_id.json`**：它是本机运行时生成的来源 ID。复制会让两台电脑 ID 相同，ESP 端白名单分不清谁是谁；不复制则自动生成新 ID。
3. **ESP32 固件下载后必须先改 WiFi**：`esp32/firmware/ESP-S3-Demo1.ino` 顶部的 `YOUR_WIFI_SSID` / `YOUR_WIFI_PASSWORD` 等占位符要换成你自己 2.4GHz 网络的凭据再编译烧录。

配置逐项说明、换网络/换 IP/换板子/换端口的改法、验收清单和排错表都在 `docs/01`。

---

## 六、多电脑 / 多窗口

- 每台电脑各跑一份 `start.bat`，各自生成不同的 `src`（来源 ID）。
- v1.5.0 固件为每个 `src` 独立维护状态、`seq`、优先级、TTL 与 15s 超时，A 电脑的 `thinking` 不会覆盖 B 电脑的 `need_confirm`。
- 设备网页「来源与绑定」卡片可逐台勾选：`全部接受`（默认）或 `仅绑定来源`。未绑定来源仍会被记录显示，但不参与灯效聚合，状态帧返回 `source_not_bound`。
- 灯只有一颗，最终显示所有「已绑定且在线」来源中优先级最高的状态。
- 想在同一台 PC 区分多个窗口，可给不同窗口分配不同的 `src`（如 `PC-A#win1`），无需改固件；当前 PC 端默认仍是所有窗口混在一起。

---

## 七、安全与隐私（公开仓库必读）

- 本仓库**不含任何真实 WiFi 账号密码**；固件里的凭据已替换为占位符，使用前请自行填写，且**不要把填好真实密码的源码再次提交到公开仓库**。
- 设备 HTTP/WS 接口**无鉴权、无 TLS、`/api/*` 开放 CORS**，同网段任何设备都能改灯、改 WiFi、重启设备。仅可在可信局域网使用，不要暴露到公网。
- PC 端本地事件总线只监听 `127.0.0.1:8765`，不对外暴露。
- 运行时生成物 `client_id.json` / `last_ip.json` / `config.json` / 日志 / `hooks.settings.*.json` 已加入 `.gitignore`，不会上传。

---

## 八、已知限制

- 串口直连实现是 **Windows 专用**（Win32 API + PowerShell 枚举串口）；Mac/Linux 需安装 `pyserial` 并小改串口枚举，纯局域网模式可设 `prefer_serial: false`。
- 广播、mDNS、`/24` 扫描都**过不了路由器**：PC 与设备必须在同一网段；跨网段时只剩 USB 串口直连可用。
- `outputting` 没有对应的 Hook 事件，默认**不做**推断（`infer_outputting: false`）；需要时在 Hook 载荷里带 `ai_status_state: "outputting"` 显式上报。
- **IDE 版确认框 / 提问框不发任何 Hooks 事件**，`tool_name` 经常为空，`PostToolUse` 也经常缺失。所以 PC 端**不猜测**「等人」：`need_input` / `need_confirm` 只由明确信号触发，在 IDE 下基本不会自动亮。工作中卡住超过 3 分钟会归为 `error` 红色快闪（`stalled_after_ms` 可调或设 `0` 关闭）。
- `Notification` / `PermissionRequest` / `PermissionDenied` / `PostToolUseFailure` / `Elicitation` 等补强事件已默认注册：IDE 不发时无副作用，一旦发出即精确命中；CLI 用户仍可用 `--with-cli` 补齐其余事件。
- 灯效的颜色与动效（如「黄色呼吸」「绿色慢闪」）由固件 `STATUS_DEFS` 决定，只改 PC 端改不了；PC 端能改的是状态判定与 `ttl` 时长。
- 设备端 UDP 4210 广播发现需要 PC 防火墙放行入站规则（需管理员操作）；不改防火墙时优先用 mDNS `esp32s3-ai.local`，PC 端已支持。

---

## 九、文档导航

| 我想… | 看这里 |
| --- | --- |
| 换台电脑 / 刚下载下来 / 改配置 | `docs/01-换环境从零部署.md` |
| 查某个命令或配置项 | `docs/02-PC端功能与配置.md` |
| 烧录固件、查接口、设置绑定 | `docs/03-ESP32端固件与接口.md` |
| 对接协议、写自己的客户端 | `docs/04-协议与来源ID绑定.md` |
| 确认版本兼容、升级 | `docs/05-版本矩阵与兼容性.md` |
