# ESP32-S3 端

本目录是设备端发布内容，对应固件 **v1.5.0 灯效可配置版**（`done` 绿色慢闪 10s、每个状态的主色/辅色/效果/亮度/周期都可在网页改并写入 NVS、`GET|POST /api/fx`）、协议 `ai-status/1`。

```
esp32/
├── README.md                    ← 本文件
└── firmware/
    └── ESP-S3-Demo1.ino         ← 可编译的完整固件源码（v1.5.0 灯效可配置版）
```

也可直接下载 `packages/aistatus-led-esp32-v1.5.0.zip`。

---

## 第一次使用：必须先改 WiFi

源码顶部这几个宏是**占位符**，不改成你自己的网络，设备连不上 WiFi（会退化到 AP 兜底热点）：

```cpp
#define WIFI_DEFAULT_SSID     "YOUR_WIFI_SSID"
#define WIFI_DEFAULT_PASS     "YOUR_WIFI_PASSWORD"
#define WIFI_BACKUP_SSID      "YOUR_BACKUP_SSID"      // 可留空或填第二个 2.4GHz 网络
#define WIFI_BACKUP_PASS      "YOUR_BACKUP_PASSWORD"
#define AP_SSID               "ESP32S3-Fallback"
#define AP_PASSWORD           "YOUR_AP_PASSWORD"      // 至少 8 位
```

> ESP32-S3 只支持 **2.4GHz**，5GHz SSID 永远扫描不到。
> 若把源码提交到公开仓库，**务必保持这些占位符**，不要写回真实密码。设备也支持在网页里保存自定义 WiFi（写入 NVS，优先级最高）。

## 编译与烧录（速查）

| 项 | 值 |
| --- | --- |
| 开发板 | ESP32-S3-N8R2（8MB Flash / 2MB QSPI PSRAM） |
| 框架 | Arduino-ESP32 Core 3.2.1（底层 ESP-IDF 5.x） |
| 原生 USB CDC | `VID_303A:1001`，Windows 常见 COM16，用于烧录与状态协议 |
| USB 转串口 | CH343 `VID_1A86:55D3`，常见 COM17，接 UART0 |
| 串口参数 | 115200 8N1；PC 端打开时 DTR/RTS 必须置 false |
| 状态灯 | 板载 WS2812，GPIO48（部分板子在 GPIO38） |

烧录成功的标志：`Hash of data verified` / `UPLOAD_EXIT=0`；串口 `ping` 应回 `pong` 且 `proto == "ai-status/1"`、`fw == "1.5.0"`。

## 功能

- 10 个 AI 状态的颜色与动效（单颗 WS2812）
- 三通道统一承载：串口 JSON Lines、HTTP、WebSocket
- 按 `src` 区分多台电脑 / 多个窗口，各自独立状态与超时
- 网页「来源与绑定」逐台勾选，`全部接受` / `仅绑定来源` 两种模式，NVS 持久化
- 亮度独立可调并持久化
- 设备发现：mDNS `esp32s3-ai.local`、UDP 4210 广播、`/api/hello`、AP 兜底
- 内置 Web 调试面板（WiFi / GPIO / 触摸 / 双串口 / 系统信息）

完整的接口总表、绑定接口、发现机制、防火墙说明与验收清单见 **[`docs/03-ESP32端固件与接口.md`](../docs/03-ESP32端固件与接口.md)**；换环境从零部署见 **[`docs/01-换环境从零部署.md`](../docs/01-换环境从零部署.md)**。

## 安全

设备接口无鉴权、无 TLS、开放 CORS，同网段任何设备都能改灯 / 改 WiFi / 重启。仅限可信局域网，不要暴露到公网。
