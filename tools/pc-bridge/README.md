# pc-bridge · 手机桥接端（Python，零依赖）

把 `AI_STATUIS_APP` 的 AI 状态搬到局域网上给鸿蒙手机看，并在 V2 中接收手机的键鼠输入注入。

## 集成版（本项目内的这份）

这份已由 **`AI_STATUIS_APP` 的 `start.bat` 自动拉起**，不用再单独双击 `start_bridge.bat`：

| 项 | 默认值 | 说明 |
| --- | --- | --- |
| 手机访问 | `http://<局域网IP>:8770` | TCP，监听 `0.0.0.0` |
| 局域网发现 | UDP `4211` 广播 | 手机发 `{"t":"discover","proto":"ai-status-phone/1"}` 即可搜到 |
| **配对码** | **`123456`（固定）** | 由启动器写入 `bridge_config.json` 的 `pin`，不再每次随机 |
| 状态源 | `http://127.0.0.1:8765/health` | 自动指向本机中间件 |
| 远控 V2 | **默认开启** | `phone_bridge_enable_input` 默认 `true`（上层 `config.json`）；手机端也需打开开关 |

配置改在**上层** `AI_STATUIS_APP/config.json` 的 `phone_bridge_*` 项（不要直接改这里生成的
`bridge_config.json`，每次启动会被覆盖）：

```json
{
  "phone_bridge_enabled": true,
  "phone_bridge_port": 8770,
  "phone_bridge_discovery_port": 4211,
  "phone_bridge_pin": "123456",
  "phone_bridge_pair_required": true,
  "phone_bridge_enable_input": true,
  "phone_bridge_firewall": true
}
```

运维：

```bat
python main.py phone-bridge status             :: 查看地址/端口/实际生效的配对码
python main.py phone-bridge pin 888888         :: 改配对码（6 位数字，重启服务生效）
python main.py phone-bridge firewall           :: 放行端口（需管理员）
tools\open-ports-admin.bat                     :: 右键「以管理员身份运行」一键放行
```

`start` 会自动尝试放行防火墙；若当前会话不是管理员会记 `need_admin`，此时手机上若搜不到，
用管理员跑一次上面的脚本即可。服务退出时桥接子进程会一起停止；子进程意外退出会自动拉起。
关闭手机桥接：在 `config.json` 设 `"phone_bridge_enabled": false`。

---

以下为原独立版说明（单独运行 `bridge.py` 时适用）。

## 快速开始

```powershell
cd HarmonyOS_App\pc-bridge

# A. 不依赖 AI_STATUIS_APP，先验证链路（推荐第一步）
python bridge.py --demo

# B. 接真实数据（前提：AI_STATUIS_APP 的 start.bat 正在跑，8765 在监听）
python bridge.py

# C. V2 远控：默认关闭，必须显式开
python bridge.py --enable-input

# D. 双击 start_bridge.bat 也行；要远控就编辑 bat 加参数
```

启动后控制台会打印：

```
本机名称 : chenjimo
手机访问 : 192.168.1.4:8770
配对码   : 482913
远控 V2  : 已开启，空闲 600s 自动关闭
```

## 文件说明

| 文件 | 说明 |
| --- | --- |
| `bridge.py` | V1 状态桥接 + V2 路由，单文件，只用标准库 |
| `input_win.py` | V2：ctypes 调 `SendInput` 的键鼠注入引擎，默认关闭 |
| `bridge_config.example.json` | 复制成 `bridge_config.json` 后生效（含 `enable_input`、`gesture_map`） |
| `actions.example.json` | 复制成 `actions.json` 后生效，定义 V1 自定义动作 |
| `tools_ws_probe.py` | 命令行 WS 探针 |
| `test_v2.py` | V2 回归测试（打桩 SendInput，不注入真实按键） |

运行时生成（已 `.gitignore`）：`phones.json`、`pin.txt`、`bridge_config.json`、`actions.json`。

## 不用手机也能测

```powershell
# 终端 1
python bridge.py --demo

# 终端 2
$pin = Get-Content .\pin.txt
$r = Invoke-RestMethod -Uri http://127.0.0.1:8770/v1/pair -Method Post `
     -ContentType 'application/json' -Body (@{pin=$pin; deviceId='cli'; deviceName='CLI'} | ConvertTo-Json)
python tools_ws_probe.py $r.token
```

V2 引擎回归测试（**不会真的按键**，`_send` 已打桩）：

```powershell
python test_v2.py
```

## 运维命令

```powershell
python bridge.py status              # 当前状态快照
python bridge.py revoke hm-9f2c1a    # 撤销某台手机（deviceId 在手机「设置 > 关于」里看）
```

## V2 远控怎么开

1. 电脑：`python bridge.py --enable-input`（或在 `bridge_config.json` 里设 `"enable_input": true`）
2. 手机：「远控 V2」页打开「手机端允许远控」
3. 两端都开才有输入注入；任一端关闭，请求被拒绝

安全策略：Token 鉴权、每秒最多 30 次操作、空闲 600 秒自动关闭、文本长度上限 200 字。
手势映射可在 `bridge_config.json` 的 `gesture_map` 里覆盖，完整定义见 `../docs/04-V2远控开发规范.md`。

## 已知限制

- 明文 HTTP/WS，无 TLS：仅限可信局域网，**不要做端口映射**。
- `SendInput` 只能在交互桌面会话工作；服务/计划任务里无效。
- **UIPI**：普通权限进程无法注入到「以管理员身份运行」的窗口，目标 IDE 若管理员启动，桥接也要管理员启动。
- `need_confirm` 的「允许」按钮不能替你点 IDE 的确认弹窗，V2 也一样（系统模态窗口无对外 API）。
- 仅支持 Windows（V1 状态桥接部分本身是跨平台的，只有 `input_win.py` 限 Windows）。
