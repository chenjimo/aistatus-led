/***********************************************************************
 *  ESP32-S3 全能 Web 调试面板 v1.5.0
 *  目标板  : ESP32-S3-N8R2 (8MB Flash / 2MB QSPI PSRAM)
 *  框架    : Arduino-ESP32 Core 3.2.1（底层 ESP-IDF 5.x）；直接调用 IDF 的
 *            RMT / LEDC / 温度传感器 / esp_flash HAL，避免重型外部依赖
 *
 *  v1.1 变更:
 *  1. WiFi 三级容错: 首选网络 -> 备用网络 -> 本地 AP，全部失败仍可访问 Web 面板
 *  2. 移除未安装的 FastLED 依赖，改用 Core 内置 rgbLedWrite() 并自行实现 HSV 转换
 *  3. 修复 WS 回调越界写 data[len]、未处理分片、在异步任务里 restart/delay
 *  4. WiFi 扫描改为异步，不再在 async_tcp 任务中阻塞数秒
 *  5. WiFi 掉线重连改为非阻塞状态机，失败后自动降级到 AP
 *  6. 修复 usb_echo 开关无效、PWM 改频率重复 attach 泄漏通道、温度恒为 N/A
 *  7. 恢复 ESP32-S3 真实芯片温度读取，串口接收改为非阻塞按行解析
 *
 *  v1.2 变更:
 *  8. 主网络改 YOUR_WIFI_SSID 固定为永久备用
 *  9. 自定义 WiFi 通过 ESP-IDF 原生 NVS API 持久化，开机默认优先连接
 * 10. Web 面板新增配置表单，AP 模式下亦可保存/清除并即时生效
 *
 *  v1.3 变更:
 * 11. Serial 改指向原生 USB Serial/JTAG CDC (COM16)，UART0 用 Serial0 独立桥接
 * 12. 网页串口支持双通道收发、无换行数据实时上送、HEX 显示、UART0 波特率热配置
 * 13. 修复「收不到对端数据」：旧版只监听 UART0，且必须等换行才上送
 *
 *  v1.4.0 变更 (协议 ai-status/1):
 * 14. 新增 AI 工作状态灯引擎：10 个状态枚举 / 优先级 / 粘性 / TTL / 心跳超时
 * 15. 三通道统一承载：串口 JSON Lines、HTTP POST、WebSocket
 * 16. 设备发现 GET /api/hello、状态 GET/POST /api/status、CORS + OPTIONS 预检
 * 17. 新增 5 条 WS 命令与 status / status_ack 两类推送
 * 18. system 增加 17 个机器可读字段与可选 reqId 回显
 * 19. Web 面板新增「AI 状态灯」卡片，可开关引擎与快捷切换状态
 *
 *  v1.4.1 变更:
 * 20. 状态灯引擎开启时也可独立调节亮度（不再被 led_* 接管逻辑屏蔽）
 * 21. 亮度写入 ESP-IDF NVS(status_cfg/brightness)，开机自动恢复
 * 22. 新增 WS 命令 status_brightness、HTTP/串口可选 brightness 字段
 * 23. system 增加 statusBrightness，status/hello 增加亮度字段
 *
 *  v1.5.0 变更 (协议 ai-status/1 不变，新增可选 src 字段):
 * 24. 按 src 为每台 PC / 每个 AI 窗口独立维护状态、seq、优先级、TTL、15s 超时
 * 25. 绑定白名单：mode=0 全部接受(默认) / mode=1 仅绑定；可多选、可清空，NVS 持久化
 * 26. 单灯混合显示：取已绑定且在线来源中优先级最高者驱动唯一 WS2812
 * 27. 网页新增「来源与绑定」卡片，逐来源显示状态/消息/心跳/在线并可勾选
 * 28. 新增 GET /api/sources、GET|POST /api/bind 与 4 条 WS 绑定命令
 * 29. 串口新增 sources / bind / bind_mode 帧，ping 回执带 bindMode/sourceField/mdns
 * 30. 设备发现新增 mDNS esp32s3-ai.local 与 UDP 255.255.255.255:4210 广播(30s)
 * 31. status/hello/system/pong 增加 src、bindMode、sourceCount、mdns 等机器可读字段
 *
 *  v1.5.0 BUGFIX (fw 版本号不变，以源码 hash 区分):
 * 32. 修复无「已绑定且在线」来源时 sinceMs 每轮滚动，导致 status 洪泛 ->
 *     WebSocket 队列溢出 -> ESP_RST_PANIC 反复重启（网页打不开的主因）
 * 33. publishSources() 无客户端短路 + 500ms 合帧；MDNS.begin() 失败改 5s 节流重试
 * 34. UDP 发现改用数值 IP(不再走 gethostbyname)，有限广播 + 子网定向广播双发，
 *     启动先快发 3 次；检查 beginPacket/endPacket 返回值并累计成功/失败
 * 35. hello 增加 udpBeaconCount/Ok/Fail/udpLastErrno/broadcastIP；
 *     新增 GET /api/discover 主动触发(可选 peer 单播对照)
 *
 *  v1.5.0 灯效可配置版 (协议 ai-status/1、fw 版本号不变，以源码 hash 区分):
 * 36. 默认灯效重做：工作态(思考/找资料/编写/输出/完成)偏暗，交互态
 *     (需补充/需确认/失败)明显更亮；新增「双色渐变呼吸」效果 FX_GRADIENT
 * 37. 空闲暗蓝、思考深紫慢渐变呼吸、找资料绿黄渐变呼吸、编写青蓝呼吸、
 *     输出渐变流动、完成绿色慢闪 10s、需补充粉色快闪、需确认橙色快闪、失败红色急闪
 * 38. 每个状态的颜色1/颜色2/效果/亮度/周期均可在网页单独配置，NVS blob 持久化，
 *     开机自动恢复；新增 GET|POST /api/fx 与「恢复默认灯效」
 * 39. 网页「AI 状态灯」卡片每个状态名旁显示配置色块与闪烁方式，点开可编辑并保存
 * 40. 网页卡片标题可拖动排序，localStorage 记忆；页头「↺ 重置排序」恢复默认顺序
 * 41. 灯效配置收进默认折叠面板，缩略行显示状态数与全局亮度，点开才展开编辑
 ***********************************************************************/

#include <Arduino.h>
#include <math.h>
#include <string.h>
#include <WiFi.h>
#include <ESPAsyncWebServer.h>
#include <AsyncJson.h>           // AsyncCallbackJsonWebHandler：处理 JSON POST body
#include <AsyncTCP.h>
#include <ArduinoJson.h>
#include <esp_system.h>
#include <esp_chip_info.h>
#include <esp_flash.h>
#include <nvs_flash.h>           // ESP-IDF 原生 NVS：持久化自定义 WiFi
#include <nvs.h>
#include "esp32-hal-rgb-led.h"   // rgbLedWrite(): Core 内置 WS2812 驱动 (IDF RMT)
#include <ESPmDNS.h>             // v1.5.0：esp32s3-ai.local，IP 变了也能解析
#include <WiFiUdp.h>             // v1.5.0：局域网发现广播
#include <errno.h>               // v1.5.0 BUGFIX：读取 UDP sendto 失败原因

// ==================== 配置区 ====================

// WiFi 连接优先级: ① NVS里保存的自定义网络 → ② 默认主网络 → ③ 备用网络 → ④ AP 兜底
// 注意: ESP32-S3 只有 2.4GHz WiFi，5GHz SSID 永远扫描不到。
#define WIFI_DEFAULT_SSID     "YOUR_WIFI_SSID"
#define WIFI_DEFAULT_PASS     "YOUR_WIFI_PASSWORD"
#define WIFI_BACKUP_SSID      "YOUR_BACKUP_SSID"
#define WIFI_BACKUP_PASS      "YOUR_BACKUP_PASSWORD"

#define WIFI_MAX_SSID         33     // 802.11 SSID 上限 32 字节 + '\0'
#define WIFI_MAX_PASS         65     // WPA2 密码上限 63 字节 + '\0'
#define WIFI_NVS_NAMESPACE    "wifi_cfg"

struct WifiCredential {
  char ssid[WIFI_MAX_SSID];
  char password[WIFI_MAX_PASS];
};
#define WIFI_MAX_CANDIDATES   3     // 自定义 + 默认 + 备用

#define WIFI_CONNECT_TIMEOUT  10000    // 单个网络启动连接超时 (ms)
#define WIFI_RETRY_INTERVAL   5000     // 掉线后重连检查周期 (ms)
#define WIFI_SCAN_TIMEOUT     12000    // 异步扫描看门狗 (ms)

// 双串口桥接：Serial = 原生 USB Serial/JTAG CDC(COM16)，Serial0 = UART0(CH343/COM17)
#define UART0_BAUD_DEFAULT    115200
#define SERIAL_IDLE_FLUSH_MS  40       // 无换行数据静默 40ms 即上送，避免一直攒着不显示
#define SERIAL_LINE_MAX       256
#define SERIAL_TARGET_CDC     0
#define SERIAL_TARGET_UART0   1
#define SERIAL_TARGET_BOTH    2

// ---- AI 工作状态灯 (ai-status/1) ----
#define FW_VERSION            "1.5.0"
#define PROTO_VERSION         "ai-status/1"
#define SOURCE_FIELD          "src"       // 与 PC 端 protocol.py 的 SOURCE_FIELD 一致
#define STATUS_BRIGHTNESS_DEFAULT 255   // 状态灯全局亮度(0~255)，255=与 v1.4.0 一致
#define STATUS_NVS_NAMESPACE  "status_cfg"
#define STATUS_NVS_KEY        "brightness"
#define STATUS_BIND_MODE_KEY  "bind_mode"
#define STATUS_BIND_LIST_KEY  "bind_srcs"
#define STATUS_HEARTBEAT_MS   5000     // 建议心跳周期
#define STATUS_OFFLINE_MS     15000    // 无帧超时转 offline
#define STATUS_SERIAL_MAX     512      // 串口协议行上限
#define STATUS_MSG_MAX        121      // msg 上限(含 '\0')
#define STATUS_SRC_LEN        40       // 来源 ID 上限(含 '\0')
#define STATUS_SRC_MAX        8        // 最多同时跟踪 8 个来源
#define STATUS_BIND_MAX       8        // 白名单最多 8 台电脑
#define STATUS_ANON_SRC       "*"      // 不带 src 的旧客户端 / 网页手动操作
#define DISCOVERY_UDP_PORT    4210     // 发现广播端口
#define DISCOVERY_BEACON_MS   30000    // 发现广播周期(稳定运行后)
#define DISCOVERY_FAST_BEACONS 3       // 启动后先快发 3 次，便于 PC 尽快发现
#define DISCOVERY_FAST_MS     3000     // 快发间隔
#define DISCOVERY_HOST        "esp32s3-ai"   // mDNS: esp32s3-ai.local

#define FX_SOLID        0    // 常亮
#define FX_BREATHE      1    // 单色明暗呼吸
#define FX_BLINK        2    // 亮/暗闪烁
#define FX_FLOW         3    // 双色渐变流动
#define FX_DOUBLE_BLINK 4    // 双闪
#define FX_GRADIENT     5    // 双色渐变呼吸（v1.5.0-fx 新增）
#define FX_COUNT        6

// 效果名：网页下拉与状态列表显示用，顺序与上面宏一致
static const char *const FX_NAMES[FX_COUNT] = {
  "常亮", "呼吸", "闪烁", "渐变流动", "双闪", "渐变呼吸"
};

// ---- 每个状态的显示配置（网页可编辑，NVS blob 持久化） ----
// 颜色用 RGB，便于网页 <input type="color"> 直接编辑；level 是相对亮度百分比
struct StatusFx {
  uint8_t  r1, g1, b1;      // 主色
  uint8_t  r2, g2, b2;      // 辅色（渐变/流动时用到）
  uint8_t  fx;              // 效果枚举
  uint8_t  level;           // 亮度 0~100（工作态低、交互态高）
  uint16_t periodMs;        // 动效周期
};

struct StatusDef {
  const char *name;         // 协议状态名
  const char *label;        // 网页中文名
  uint8_t  prio;
  bool     sticky;
  uint32_t ttlMs;
};

// 顺序即索引，0=offline / 1=idle 供内部转移使用
static const StatusDef STATUS_DEFS[] = {
  {"offline",      "离线",   0,  false, 0},
  {"idle",         "空闲",  10,  false, 0},
  {"thinking",     "思考",  20,  false, 0},
  {"searching",    "找资料",25,  false, 0},
  {"coding",       "编写",  30,  false, 0},
  {"outputting",   "输出",  30,  false, 0},
  {"done",         "完成",  40,  false, 10000},  // 绿色慢闪 10s 后自动回 idle
  {"need_input",   "需补充",80,  true,  0},
  {"need_confirm", "需确认",85,  true,  0},
  {"error",        "失败",  90,  true,  0},
};
#define STATUS_COUNT (sizeof(STATUS_DEFS) / sizeof(STATUS_DEFS[0]))
#define ST_OFFLINE 0
#define ST_IDLE    1

#define STATUS_FX_KEY   "fx_v1"    // NVS blob 键
#define STATUS_FX_VER   1          // 配置结构版本

// v1.5.0-fx 默认灯效：工作态偏暗、需要交互时明显更亮
static const StatusFx STATUS_FX_DEFAULT[STATUS_COUNT] = {
  // 主色 R G B      辅色 R G B     效果         亮度  周期ms
  {  5, 10, 20,      5, 10, 20,  FX_SOLID,       18,     0},  // 离线：极暗蓝
  { 10, 30, 90,     10, 30, 90,  FX_SOLID,       30,     0},  // 空闲：暗蓝
  { 42,  8, 70,     90, 24,144,  FX_GRADIENT,    38,  3000},  // 思考：深紫慢渐变呼吸
  { 10, 90, 42,    122,106, 10,  FX_GRADIENT,    38,  1800},  // 找资料：绿黄渐变呼吸
  { 10,110,140,     10,110,140,  FX_BREATHE,     40,  1200},  // 编写：青蓝呼吸
  { 10, 58,106,     16,160,160,  FX_FLOW,        40,  3000},  // 输出：渐变流动
  { 26,160, 58,     26,160, 58,  FX_BLINK,       55,  1000},  // 完成：绿色慢闪 10s
  {255, 79,168,    255, 79,168,  FX_BLINK,       95,   500},  // 需补充：粉色快闪
  {255,138, 26,    255,138, 26,  FX_BLINK,      100,   500},  // 需确认：橙色快闪
  {255, 32, 32,    255, 32, 32,  FX_BLINK,      100,   240},  // 失败：红色急闪
};
static StatusFx g_statusFx[STATUS_COUNT];   // 运行时配置：开机从 NVS 恢复，否则用默认值

#define AP_SSID               "ESP32S3-Fallback"
#define AP_PASSWORD           "YOUR_AP_PASSWORD"

#define LED_PIN               48     // 根据实际板子修改 (常见为 48 或 38)

#define NUM_TOUCH_PINS        6
const int touchPins[NUM_TOUCH_PINS] = {1, 2, 3, 4, 5, 6};
#define TOUCH_THRESHOLD       40

#define GPIO_OUT_PIN          38
#define GPIO_PWM_PIN          39
#define GPIO_ADC_PIN          10
#define GPIO_IN_PIN           11

#define PWM_FREQ_DEFAULT      1000
#define PWM_RESOLUTION        8

#define AI_INPUT_SIZE         16
#define AI_HIDDEN_SIZE        8
#define AI_OUTPUT_SIZE        4

// ==================== 全局变量 ====================

AsyncWebServer server(80);
AsyncWebSocket ws("/ws");

struct RgbColor { uint8_t r, g, b; };

uint8_t ledHue = 128;
uint8_t ledSat = 255;
uint8_t ledVal = 128;
bool    ledOn  = true;
volatile bool g_rainbow = false;
volatile bool g_breathe = false;

// WiFi 运行时候选列表（开机由 NVS + 默认配置重建）
static WifiCredential g_wifiList[WIFI_MAX_CANDIDATES];
static uint8_t        g_wifiCount      = 0;
static bool           g_hasCustomWifi = false;
static uint32_t       g_wifiApplyAtMs = 0;   // 延迟应用新配置，让回执先发给网页

// WiFi 状态
bool     wifiConnected    = false;
bool     wifiInApMode     = false;
uint8_t  wifiCurrentIndex = 0;
String   deviceIP         = "";
bool     g_wifiTrying     = false;
uint32_t g_wifiTryStart   = 0;
uint8_t  g_wifiTryIndex   = 0;

// 异步 WiFi 扫描
bool     g_scanRequested  = false;
bool     g_scanRunning    = false;
uint32_t g_scanStartMs    = 0;

// USB / 串口双通道
bool     g_usbEcho        = false;   // 默认不回显，避免把收到的数据又发回对端
uint32_t g_uartBaud       = UART0_BAUD_DEFAULT;
uint8_t  g_serialTarget   = SERIAL_TARGET_CDC;
bool     g_serialHex      = false;
uint32_t g_rxBytes        = 0;
uint32_t g_txBytes        = 0;
String   g_cdcLine, g_uartLine;
uint32_t g_cdcLastMs = 0, g_uartLastMs = 0;
volatile bool g_restartRequested = false;
uint32_t g_restartAtMs    = 0;

// AI 状态灯运行时状态（聚合结果：驱动唯一的 WS2812，保持 v1.4.x 字段语义）
uint8_t  g_statusIdx        = ST_OFFLINE;
uint32_t g_statusSeq        = 0;
uint32_t g_statusSinceMs    = 0;
uint32_t g_statusLastFrameMs = 0;
uint32_t g_statusTtlMs     = 0;
bool     g_statusEngine    = true;
bool     g_statusLink      = false;
uint8_t  g_statusBrightness = STATUS_BRIGHTNESS_DEFAULT;   // NVS 持久化，状态引擎下仍可调
String   g_statusMsg;
char     g_statusSrc[STATUS_SRC_LEN] = STATUS_ANON_SRC;    // 当前灯由哪个来源驱动

// ---- v1.5.0 多来源：每台 PC / 每个窗口独立状态槽，按 src 区分 ----
struct SourceSlot {
  char     src[STATUS_SRC_LEN];
  uint8_t  stateIdx;
  uint32_t seq;
  uint32_t sinceMs;
  uint32_t lastSeenMs;
  uint32_t ttlMs;
  char     msg[STATUS_MSG_MAX];
  bool     used;
};
SourceSlot g_sources[STATUS_SRC_MAX];
char       g_bindList[STATUS_BIND_MAX][STATUS_SRC_LEN];
uint8_t    g_bindCount = 0;
uint8_t    g_bindMode  = 0;   // 0=全部接受(兼容 v1.4.x) / 1=仅绑定白名单
uint32_t   g_lastSourcesPushMs = 0;
WiFiUDP    g_discoveryUdp;
// v1.5.0 BUGFIX：UDP 发现诊断计数，可用 GET /api/hello 或 /api/discover 读取
uint32_t   g_udpBeaconCount = 0;
uint32_t   g_udpBeaconOk    = 0;
uint32_t   g_udpBeaconFail  = 0;
int        g_udpLastErrno   = 0;
String     g_udpBroadcastIp = "255.255.255.255";

bool gpioOutState = false;
int  gpioPwmValue = 128;
int  gpioPwmFreq  = PWM_FREQ_DEFAULT;

float aiWeights1[AI_INPUT_SIZE][AI_HIDDEN_SIZE];
float aiWeights2[AI_HIDDEN_SIZE][AI_OUTPUT_SIZE];
float aiBias1[AI_HIDDEN_SIZE];
float aiBias2[AI_OUTPUT_SIZE];
bool  aiModelLoaded = false;
const char* aiLabels[AI_OUTPUT_SIZE] = {"圆形", "方形", "三角", "星形"};

// ==================== 前向声明 (Core 3.x 自动原型易失效) ====================

bool nvsInit();
bool nvsLoadWifi(char *ssid, size_t ssidLen, char *pass, size_t passLen);
bool nvsSaveWifi(const char *ssid, const char *pass);
bool nvsClearWifi();
void rebuildWifiList();
RgbColor hsvToRgb(uint8_t h, uint8_t s, uint8_t v);
void showRgb(uint8_t r, uint8_t g, uint8_t b);
void showHsv(uint8_t h, uint8_t s, uint8_t v);
void refreshLed();
bool tryConnectSta(const char *ssid, const char *pass, uint32_t timeoutMs);
void beginStaAttempt(uint8_t index);
void startFallbackAp();
void setupWifi();
void serviceWifi();
void requestWifiScan();
void serviceWifiScan();
void onWsEvent(AsyncWebSocket *server, AsyncWebSocketClient *client,
               AwsEventType type, void *arg, uint8_t *data, size_t len);
void handleWsMessage(AsyncWebSocketClient *client, char *data);
void sendTouchData(AsyncWebSocketClient *client);
void sendGpioData(AsyncWebSocketClient *client);
String getSystemInfoJson();
void initAIModel();
void doAIInference(AsyncWebSocketClient *client, JsonDocument &doc);
void broadcastUSB(String msg);
void flushSerialLine(String &line, Stream &port, const char *tag);
void processSerialStream(Stream &port, String &line, uint32_t &lastMs, const char *tag);
void writeSerialTarget(const String &text, uint8_t target, bool addNL);
int  findStatusIndex(const char *state);
bool applyStatus(const char *state, uint32_t seq, uint32_t ttl,
                 const char *msg, bool force, const char *reqId, String &reason);
void publishStatus(const char *reqId, bool unicastOnly, AsyncWebSocketClient *client);
void renderStatus(uint32_t now);
void checkStatusTimeout(uint32_t now);
void clearStatusToIdle();
String getStatusJson();
String getHelloJson();
String statusAckJson(bool ok, const String &reason, const char *reqId);
String statusWsAckJson(bool ok, const String &reason, const char *reqId);
bool handleSerialProtocolLine(const String &raw, Stream &port, const char *tag);
void sendJsonResponse(AsyncWebServerRequest *request, int code, const String &body);
void loadStatusBrightness();
bool setStatusBrightness(uint8_t value);
void loadStatusFx();                 // v1.5.0-fx：开机恢复每个状态的灯效配置
bool saveStatusFx();
bool resetStatusFx();
String getStatusFxJson();
bool setStatusFxItem(JsonObject o, String &reason);
void loadBindConfig();
bool saveBindConfig();
void normalizeSrc(const char *raw, char *out, size_t outLen);
int  findSourceSlot(const char *src, bool create);
bool isSourceBound(const char *src);
bool setSourceBind(const char *src, bool on);
void clearBindList();
void touchSource(const char *src, const char *state);
bool applyStatusSrc(const char *state, uint32_t seq, uint32_t ttl,
                    const char *msg, bool force, const char *reqId,
                    const char *src, String &reason);
void recomputeAggregate(uint32_t now);
void checkSourcesTimeout(uint32_t now);
void clearAllSources();
void fillSources(JsonObject d);
String getSourcesJson();
String sourcesWsJson();
void publishSources();
void serviceDiscoveryBeacon(uint32_t now);
bool sendDiscoveryBeacon(IPAddress target);
String getDiscoveryDiagJson();

// ==================== 网页 HTML ====================

const char INDEX_HTML[] PROGMEM = R"rawliteral(
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ESP32-S3 全能调试面板</title>
<style>
:root{--bg:#0f172a;--card:#1e293b;--border:#334155;--text:#e2e8f0;--text2:#94a3b8;--accent:#3b82f6;--accent2:#8b5cf6;--green:#22c55e;--red:#ef4444;--orange:#f59e0b;--cyan:#06b6d4}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;padding:10px}
.header{text-align:center;padding:20px 10px;background:linear-gradient(135deg,#1e293b,#0f172a);border:1px solid var(--border);border-radius:16px;margin-bottom:16px}
.header h1{font-size:1.5em;background:linear-gradient(90deg,var(--accent),var(--accent2));-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:5px}
.header .subtitle{color:var(--text2);font-size:.85em}
.status-bar{display:flex;gap:10px;justify-content:center;flex-wrap:wrap;margin-top:10px}
.status-item{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:5px 12px;font-size:.75em}
.status-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px;animation:pulse 2s infinite}
.status-dot.online{background:var(--green)}.status-dot.offline{background:var(--red)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:20px;transition:transform .2s}
.card:hover{transform:translateY(-2px)}
.card-title{font-size:1em;font-weight:600;margin-bottom:15px;display:flex;align-items:center;gap:8px}
.card-title .icon{font-size:1.3em}
.wifi-list{max-height:250px;overflow-y:auto}
.wifi-item{display:flex;justify-content:space-between;align-items:center;padding:8px 12px;border-bottom:1px solid var(--border);font-size:.85em}
.wifi-item:last-child{border-bottom:none}
.wifi-name{font-weight:500}
.wifi-rssi{display:flex;align-items:center;gap:5px}
.rssi-bar{width:40px;height:16px;background:var(--bg);border-radius:3px;overflow:hidden}
.rssi-fill{height:100%;border-radius:3px;transition:width .3s}
.wifi-enc{font-size:.7em;padding:2px 6px;border-radius:4px;background:var(--bg)}
.slider-group{margin-bottom:14px}
.slider-label{display:flex;justify-content:space-between;font-size:.8em;margin-bottom:5px;color:var(--text2)}
input[type=range]{-webkit-appearance:none;width:100%;height:8px;border-radius:4px;outline:none;cursor:pointer}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:20px;height:20px;border-radius:50%;background:#fff;box-shadow:0 2px 6px rgba(0,0,0,.3);cursor:pointer}
#hueSlider{background:linear-gradient(to right,#f00,#ff0,#0f0,#0ff,#00f,#f0f,#f00)}
#satSlider{background:linear-gradient(to right,#888,hsl(0,100%,50%))}
#valSlider{background:linear-gradient(to right,#000,#fff)}
.led-preview{width:60px;height:60px;border-radius:50%;margin:10px auto;box-shadow:0 0 20px rgba(0,0,0,.5);transition:all .2s}
.gpio-row{display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border);font-size:.85em}
.gpio-row:last-child{border-bottom:none}
.btn{padding:6px 16px;border:none;border-radius:8px;cursor:pointer;font-size:.8em;font-weight:500;transition:all .2s;color:#fff}
.btn-primary{background:var(--accent)}.btn-primary:hover{background:#2563eb}
.btn-danger{background:var(--red)}.btn-danger:hover{background:#dc2626}
.btn-success{background:var(--green)}.btn-success:hover{background:#16a34a}
.btn-warning{background:var(--orange)}.btn-warning:hover{background:#d97706}
.btn-purple{background:var(--accent2)}.btn-purple:hover{background:#7c3aed}
.btn-sm{padding:4px 10px;font-size:.75em}
.touch-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.touch-pad{background:var(--bg);border:2px solid var(--border);border-radius:12px;padding:12px;text-align:center;transition:all .3s}
.touch-pad.touched{border-color:var(--cyan);background:rgba(6,182,212,.1);box-shadow:0 0 15px rgba(6,182,212,.3)}
.touch-pin{font-size:.7em;color:var(--text2)}
.touch-value{font-size:1.4em;font-weight:700;margin:4px 0;font-family:'Courier New',monospace}
.touch-bar{width:100%;height:4px;background:var(--border);border-radius:2px;margin-top:5px}
.touch-bar-fill{height:100%;background:var(--cyan);border-radius:2px;transition:width .2s}
.sys-info{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.sys-item{background:var(--bg);border-radius:8px;padding:10px}
.sys-item-label{font-size:.7em;color:var(--text2);text-transform:uppercase}
.sys-item-value{font-size:1em;font-weight:600;margin-top:3px;font-family:'Courier New',monospace}
.ai-panel{text-align:center}
#aiCanvas{border:2px solid var(--border);border-radius:8px;cursor:crosshair;touch-action:none}
.ai-results{display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin:10px 0}
.ai-result-item{background:var(--bg);border-radius:8px;padding:8px 14px;min-width:70px}
.ai-result-label{font-size:.7em;color:var(--text2)}
.ai-result-bar{width:100%;height:6px;background:var(--border);border-radius:3px;margin:4px 0}
.ai-result-fill{height:100%;border-radius:3px;transition:width .3s}
.ai-result-pct{font-size:.85em;font-weight:600;font-family:'Courier New',monospace}
.usb-cfg{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:8px;font-size:.75em;color:var(--text2)}
.usb-cfg label{display:flex;align-items:center;gap:4px}
.usb-cfg select{background:var(--card);border:1px solid var(--border);border-radius:6px;padding:4px 6px;color:var(--text);font-size:.95em;outline:none}
.usb-cfg input[type=checkbox]{accent-color:var(--accent)}
.usb-stats{font-size:.72em;color:var(--text2);margin-top:8px;font-family:'Courier New',monospace}
.status-grid{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}
.status-pill{padding:3px 9px;border-radius:12px;font-size:.72em;font-weight:600;border:1px solid var(--border);background:var(--bg)}
.fx-list{display:flex;flex-direction:column;gap:6px;margin-bottom:8px}
.fx-row{border:1px solid var(--border);border-radius:10px;background:var(--bg);padding:6px 8px}
.fx-head{display:flex;align-items:center;gap:8px;cursor:pointer;flex-wrap:wrap}
.fx-swatch{width:18px;height:18px;border-radius:5px;border:1px solid var(--border);flex:none;box-shadow:0 0 8px rgba(255,255,255,.12)}
.fx-name{font-size:.8em;font-weight:600;min-width:52px}
.fx-desc{font-size:.7em;color:var(--text2);flex:1;min-width:110px}
.fx-edit{display:none;margin-top:8px;padding-top:8px;border-top:1px solid var(--border);gap:8px;flex-wrap:wrap;align-items:center}
.fx-edit.open{display:flex}
.fx-edit label{font-size:.72em;color:var(--text2);display:flex;align-items:center;gap:4px}
.fx-edit input[type=color]{width:38px;height:26px;border:1px solid var(--border);border-radius:5px;background:none;cursor:pointer;padding:1px}
.fx-edit input[type=range]{width:90px}
.fx-edit input[type=number]{width:66px;background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:5px;padding:3px}
.fx-edit select{background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:5px;padding:3px;font-size:.75em}
.usb-terminal{background:#000;border-radius:8px;padding:10px;font-family:'Courier New',monospace;font-size:.8em;max-height:150px;overflow-y:auto;margin-bottom:10px;min-height:80px;color:#0f0}
.usb-input-row{display:flex;gap:8px}
.usb-input-row input{flex:1;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:8px 12px;color:var(--text);font-size:.85em;outline:none}
.usb-input-row input:focus{border-color:var(--accent)}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.7em;font-weight:600}
.badge-green{background:rgba(34,197,94,.2);color:var(--green)}
.scan-btn{margin-bottom:10px}
.wifi-cfg{margin-top:12px;padding:10px;background:var(--bg);border-radius:8px;border:1px solid var(--border)}
.wifi-cfg-title{font-size:.78em;font-weight:600;margin-bottom:8px;color:var(--text2)}
.wifi-cfg-row{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px}
.wifi-cfg input{background:var(--card);border:1px solid var(--border);border-radius:6px;padding:6px 9px;color:var(--text);font-size:.8em;outline:none;flex:1;min-width:110px}
.wifi-cfg input:focus{border-color:var(--accent)}
.card-title[draggable]{cursor:grab;user-select:none}
.card-title[draggable]:active{cursor:grabbing}
.drag-handle{margin-left:6px;color:var(--text2);font-size:1.05em;letter-spacing:1px;opacity:.75}
.card.dragging{opacity:.45;outline:2px dashed var(--accent)}
.card.drag-over{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent),0 0 16px rgba(59,130,246,.35)}
.fold{border:1px solid var(--border);border-radius:10px;background:var(--bg);padding:6px 10px;margin-bottom:8px}
.fold>summary{cursor:pointer;list-style:none;display:flex;align-items:center;gap:6px;font-size:.8em;font-weight:600;user-select:none}
.fold>summary::-webkit-details-marker{display:none}
.fold>summary::before{content:'▸';color:var(--text2);transition:transform .2s}
.fold[open]>summary::before{transform:rotate(90deg)}
.fold-body{padding-top:8px}
::-webkit-scrollbar{width:6px}::-webkit-scrollbar-track{background:var(--bg)}::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
</style>
</head>
<body>
<div class="header">
  <h1>🔧 ESP32-S3 全能调试面板</h1>
  <div class="subtitle">WebSocket 实时通信 · 全功能硬件调试 · 拖动卡片标题可排序</div>
  <div class="status-bar">
    <div class="status-item"><span class="status-dot online" id="wsDot"></span><span id="wsStatus">已连接</span></div>
    <div class="status-item" style="color:var(--cyan);font-weight:600" id="ipDisplay">IP: --</div>
    <div class="status-item" id="uptimeInfo">运行: --</div>
    <div class="status-item" id="freeMem">内存: --</div>
    <div class="status-item" id="chipTemp">温度: --</div>
    <div class="status-item" style="cursor:pointer" onclick="resetCardOrder()" title="恢复默认卡片顺序">↺ 重置排序</div>
  </div>
</div>
<div class="grid">
  <div class="card">
    <div class="card-title"><span class="icon">📡</span> WiFi 扫描器 <span class="badge badge-green" style="margin-left:auto" id="wifiCount">0 个</span></div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;">
      <button class="btn btn-primary scan-btn" onclick="requestWifiScan()">🔄 扫描附近WiFi</button>
      <button class="btn btn-success" onclick="sendCmd('wifi_retry',1)">🔁 重试连接</button>
    </div>
    <div class="wifi-list" id="wifiList"><div style="text-align:center;color:var(--text2);padding:20px;">点击上方按钮开始扫描...</div></div>
    <div class="wifi-cfg">
      <div class="wifi-cfg-title">⚙️ 自定义 WiFi（写入 NVS，开机默认优先连接；AP 模式下同样可用）</div>
      <div class="wifi-cfg-row">
        <input id="wifiSsid" type="text" placeholder="WiFi 名称 (仅 2.4GHz)" maxlength="32">
        <input id="wifiPass" type="password" placeholder="密码" maxlength="63">
      </div>
      <div class="wifi-cfg-row">
        <button class="btn btn-success btn-sm" onclick="saveWifi()">💾 保存并连接</button>
        <button class="btn btn-warning btn-sm" onclick="clearWifi()">🗑 清除自定义</button>
      </div>
    </div>
  </div>
  <div class="card">
    <div class="card-title"><span class="icon">🤖</span> AI 状态灯 <span class="badge badge-green" style="margin-left:auto" id="statusBadge">offline</span></div>
    <div class="status-grid">
      <button class="btn btn-success btn-sm" onclick="sendCmd('status_enable',1)">启用引擎</button>
      <button class="btn btn-warning btn-sm" onclick="sendCmd('status_enable',0)">停用</button>
      <button class="btn btn-danger btn-sm" onclick="sendCmd('status_clear',1)">清除状态</button>
    </div>
    <details class="fold" id="fxFold">
      <summary><span id="fxSummary">灯效配置 · 正在读取…</span></summary>
      <div class="fold-body">
        <div class="fx-list" id="fxList"><div style="text-align:center;color:var(--text2);padding:12px;font-size:.8em">正在读取灯效配置…</div></div>
        <div class="status-grid">
          <button class="btn btn-primary btn-sm" onclick="loadFx()">刷新灯效</button>
          <button class="btn btn-warning btn-sm" onclick="if(confirm('把所有状态恢复为默认灯效？'))resetFx()">恢复默认灯效</button>
        </div>
        <div class="usb-stats" style="margin-top:0;margin-bottom:8px">点状态名可切灯预览；「配置」可改颜色与闪烁方式并保存到设备</div>
      </div>
    </details>
    <div class="slider-group" style="margin-top:10px;margin-bottom:4px">
      <div class="slider-label"><span>💡 状态灯亮度（写入 NVS，开机保留）</span><span id="statusBrightVal">100%</span></div>
      <input type="range" id="statusBrightness" min="0" max="255" value="255" style="background:linear-gradient(to right,#000,#fff)" oninput="setStatusBrightness()">
    </div>
    <div class="usb-stats" id="statusInfo">seq=0 | link=false | sticky=false | engine=on</div>
  </div>
  <div class="card">
    <div class="card-title"><span class="icon">🖥️</span> 来源与绑定（按 src 区分） <span class="badge badge-green" style="margin-left:auto" id="bindModeBadge">全部接受</span></div>
    <div class="status-grid">
      <button class="btn btn-primary btn-sm" onclick="sendCmd('bind_mode',0)">全部接受</button>
      <button class="btn btn-warning btn-sm" onclick="sendCmd('bind_mode',1)">仅绑定来源</button>
      <button class="btn btn-danger btn-sm" onclick="if(confirm('清空绑定白名单？'))sendCmd('bind_clear',1)">清空绑定</button>
      <button class="btn btn-success btn-sm" onclick="sendCmd('sources_get',1)">刷新</button>
    </div>
    <div class="usb-stats" id="sourcesHint">灯 = 已绑定来源中优先级最高的状态；未绑定来源只显示、不影响灯</div>
    <div id="sourcesList" style="margin-top:8px;display:flex;flex-direction:column;gap:6px;"></div>
  </div>
  <div class="card">
    <div class="card-title"><span class="icon">💡</span> NeoPixel RGB 控制</div>
    <div class="led-preview" id="ledPreview"></div>
    <div class="slider-group"><div class="slider-label"><span>色相 (Hue)</span><span id="hueVal">180°</span></div><input type="range" id="hueSlider" min="0" max="255" value="128" oninput="updateLED()"></div>
    <div class="slider-group"><div class="slider-label"><span>饱和度 (Sat)</span><span id="satVal">100%</span></div><input type="range" id="satSlider" min="0" max="255" value="255" oninput="updateLED()"></div>
    <div class="slider-group"><div class="slider-label"><span>亮度 (Val)</span><span id="valVal">50%</span></div><input type="range" id="valSlider" min="0" max="255" value="128" oninput="updateLED()"></div>
    <div style="display:flex;gap:8px;justify-content:center;margin-top:10px;">
      <button class="btn btn-success" onclick="sendCmd('led_toggle',1)">开灯</button>
      <button class="btn btn-danger" onclick="sendCmd('led_toggle',0)">关灯</button>
      <button class="btn btn-warning" onclick="sendCmd('led_rainbow',1)">彩虹模式</button>
      <button class="btn btn-purple" onclick="sendCmd('led_breathe',1)">呼吸灯</button>
      <button class="btn btn-primary" onclick="sendCmd('led_effect',0)">关闭特效</button>
    </div>
  </div>
  <div class="card">
    <div class="card-title"><span class="icon">👆</span> 触摸传感器实时数据</div>
    <div class="touch-grid" id="touchGrid"></div>
    <div style="margin-top:10px;font-size:.75em;color:var(--text2);">* ESP32-S3 触摸引脚: GPIO1~GPIO14 | 阈值: 40</div>
  </div>
  <div class="card">
    <div class="card-title"><span class="icon">🔌</span> GPIO 引脚调试</div>
    <div class="gpio-row"><span>DOUT (GPIO38)</span><button class="btn btn-primary btn-sm" id="gpioOutBtn" onclick="toggleGpioOut()">OFF</button></div>
    <div class="gpio-row"><span>DIN (GPIO11)</span><span id="gpioInVal" style="font-family:monospace;font-weight:600">--</span></div>
    <div class="gpio-row"><span>PWM (GPIO39)</span><div style="display:flex;align-items:center;gap:8px;"><input type="range" id="pwmSlider" min="0" max="255" value="128" style="width:100px" oninput="updatePWM()"><span id="pwmVal" style="font-size:.8em;width:35px;">128</span></div></div>
    <div class="gpio-row"><span>ADC (GPIO10)</span><span id="adcVal" style="font-family:monospace;font-weight:600">--</span></div>
    <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;">
      <button class="btn btn-primary btn-sm" onclick="sendCmd('gpio_scan',1)">刷新读数</button>
      <button class="btn btn-warning btn-sm" onclick="sendCmd('gpio_pwm_freq',500)">PWM 500Hz</button>
      <button class="btn btn-warning btn-sm" onclick="sendCmd('gpio_pwm_freq',5000)">PWM 5kHz</button>
    </div>
  </div>
  <div class="card">
    <div class="card-title"><span class="icon">🖥️</span> 系统信息</div>
    <div class="sys-info" id="sysInfo">
      <div class="sys-item"><div class="sys-item-label">芯片</div><div class="sys-item-value" id="sysChip">ESP32-S3</div></div>
      <div class="sys-item"><div class="sys-item-label">核心数</div><div class="sys-item-value" id="sysCores">2</div></div>
      <div class="sys-item"><div class="sys-item-label">CPU频率</div><div class="sys-item-value" id="sysCpu">240 MHz</div></div>
      <div class="sys-item"><div class="sys-item-label">Flash</div><div class="sys-item-value" id="sysFlash">--</div></div>
      <div class="sys-item"><div class="sys-item-label">PSRAM</div><div class="sys-item-value" id="sysPsram">--</div></div>
      <div class="sys-item"><div class="sys-item-label">空闲堆</div><div class="sys-item-value" id="sysHeap">--</div></div>
      <div class="sys-item"><div class="sys-item-label">芯片温度</div><div class="sys-item-value" id="sysTemp">--</div></div>
      <div class="sys-item"><div class="sys-item-label">运行时间</div><div class="sys-item-value" id="sysUptime">--</div></div>
      <div class="sys-item"><div class="sys-item-label">WiFi模式</div><div class="sys-item-value" id="sysWifi">--</div></div>
      <div class="sys-item"><div class="sys-item-label">连接优先级</div><div class="sys-item-value" id="sysPlan" style="font-size:.8em">--</div></div>
      <div class="sys-item"><div class="sys-item-label">已存自定义</div><div class="sys-item-value" id="sysCustom" style="font-size:.8em">--</div></div>
      <div class="sys-item"><div class="sys-item-label">当前IP</div><div class="sys-item-value" id="sysIP" style="color:var(--cyan)">--</div></div>
      <div class="sys-item"><div class="sys-item-label">连接SSID</div><div class="sys-item-value" id="sysSSID">--</div></div>
      <div class="sys-item"><div class="sys-item-label">信号强度</div><div class="sys-item-value" id="sysRSSI">--</div></div>
      <div class="sys-item"><div class="sys-item-label">MAC地址</div><div class="sys-item-value" id="sysMac">--</div></div>
      <div class="sys-item"><div class="sys-item-label">网关</div><div class="sys-item-value" id="sysGW">--</div></div>
      <div class="sys-item"><div class="sys-item-label">SDK版本</div><div class="sys-item-value" id="sysSdk">--</div></div>
      <div class="sys-item"><div class="sys-item-label">重启原因</div><div class="sys-item-value" id="sysReset">--</div></div>
    </div>
    <div style="margin-top:12px;display:flex;gap:8px;">
      <button class="btn btn-danger btn-sm" onclick="sendCmd('sys_restart',1)">🔄 重启</button>
      <button class="btn btn-primary btn-sm" onclick="sendCmd('sys_info',1)">刷新信息</button>
    </div>
  </div>
  <div class="card">
    <div class="card-title"><span class="icon">🔗</span> 双串口调试（USB CDC + UART0）</div>
    <div class="usb-cfg">
      <label>发送目标
        <select id="serialTarget" onchange="setSerialConfig()">
          <option value="0">USB CDC (原生USB)</option>
          <option value="1">UART0 (CH343)</option>
          <option value="2">两者</option>
        </select>
      </label>
      <label>UART0 波特率
        <select id="serialBaud" onchange="setUartBaud()">
          <option>9600</option><option>19200</option><option>38400</option><option>57600</option>
          <option selected>115200</option><option>230400</option><option>460800</option><option>921600</option>
          <option>1000000</option><option>2000000</option>
        </select>
      </label>
      <label><input type="checkbox" id="serialHex" onchange="setSerialConfig()"> HEX 显示</label>
      <label><input type="checkbox" id="serialEcho" onchange="setSerialConfig()"> 回显</label>
      <label><input type="checkbox" id="serialNL" checked> 追加换行</label>
    </div>
    <div class="usb-terminal" id="usbTerminal">[等待数据...]</div>
    <div class="usb-input-row">
      <input type="text" id="usbInput" placeholder="输入要发送的数据(支持无换行)..." onkeydown="if(event.key==='Enter')sendUSB()">
      <button class="btn btn-primary" onclick="sendUSB()">发送</button>
      <button class="btn btn-danger btn-sm" onclick="clearTerminal()">清空</button>
    </div>
    <div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;">
      <button class="btn btn-purple btn-sm" onclick="sendCmd('usb_info',1)">端口信息</button>
      <button class="btn btn-success btn-sm" onclick="sendCmd('sys_info',1)">刷新统计</button>
    </div>
    <div class="usb-stats" id="usbStats">RX: 0 B | TX: 0 B | 端口: --</div>
  </div>
  <div class="card" style="grid-column:span 1;">
    <div class="card-title"><span class="icon">🤖</span> AI 推理测试 (模拟分类器)</div>
    <div class="ai-panel">
      <div style="font-size:.8em;color:var(--text2);margin-bottom:8px;">在画布上绘制图案，点击推理进行分类识别</div>
      <canvas id="aiCanvas" width="160" height="160"></canvas>
      <div style="display:flex;gap:8px;justify-content:center;margin:8px 0;">
        <button class="btn btn-primary" onclick="runAIInference()">🧠 推理</button>
        <button class="btn btn-danger btn-sm" onclick="clearCanvas()">清除画布</button>
        <button class="btn btn-purple btn-sm" onclick="loadRandomPattern()">随机图案</button>
      </div>
      <div class="ai-results" id="aiResults"></div>
      <div style="margin-top:8px;font-size:.75em;color:var(--text2);"><span id="aiInferTime">推理耗时: --</span> | <span id="aiModelStatus">模型: 已加载(随机权重演示)</span></div>
      <div style="margin-top:12px;padding:10px;background:var(--bg);border-radius:8px;font-size:.75em;color:var(--text2);text-align:left;">
        <strong style="color:var(--accent);">💡 关于ESP32-S3 AI推理:</strong><br>
        ESP32-S3 双核 LX7 支持<strong>向量指令扩展(SIMD)</strong>，可通过 <strong>ESP-DL</strong> 或 <strong>TensorFlow Lite Micro</strong> 部署量化模型。本Demo使用模拟全连接神经网络演示推理流程。
      </div>
    </div>
  </div>
</div>
<div style="text-align:center;padding:20px;font-size:.75em;color:var(--text2);">ESP32-S3 全能调试面板 v1.5.0 | 多来源绑定 | <span id="msgCount">0</span> 条消息已交换</div>
<script>
let ws,msgCount=0,ledTimer=null;
function connectWS(){const p=location.protocol==='https:'?'wss:':'ws:';ws=new WebSocket(p+'//'+location.host+'/ws');ws.onopen=()=>{document.getElementById('wsDot').className='status-dot online';document.getElementById('wsStatus').textContent='已连接';sendCmd('init',1);sendCmd('sources_get',1)};ws.onclose=()=>{document.getElementById('wsDot').className='status-dot offline';document.getElementById('wsStatus').textContent='断开';setTimeout(connectWS,2000)};ws.onmessage=(e)=>{msgCount++;document.getElementById('msgCount').textContent=msgCount;try{handleMessage(JSON.parse(e.data))}catch(err){console.warn('WS消息解析失败',err)}}}
function sendCmd(c,v){if(ws&&ws.readyState===1)ws.send(JSON.stringify({cmd:c,val:v}))}
function handleMessage(d){switch(d.type){case'wifi_scan':updateWifiList(d.data);break;case'touch':updateTouch(d.data);break;case'gpio':updateGPIO(d.data);break;case'system':updateSystem(d.data);break;case'usb_recv':appendTerminal('← '+d.data);break;case'usb_info':appendTerminal(d.data);break;case'ai_result':updateAIResults(d.data);break;case'status':updateStatus(d.data);break;case'status_ack':if(!d.data.ok)appendTerminal('← 状态被拒绝: '+d.data.reason);break;case'sources':updateSources(d.data);break}}
function requestWifiScan(){sendCmd('wifi_scan',1);document.querySelector('.scan-btn').textContent='⏳ 扫描中...';document.querySelector('.scan-btn').disabled=true;setTimeout(()=>{document.querySelector('.scan-btn').textContent='🔄 扫描附近WiFi';document.querySelector('.scan-btn').disabled=false},3000)}
function saveWifi(){const s=document.getElementById('wifiSsid').value.trim(),p=document.getElementById('wifiPass').value;if(!s){appendTerminal('[WiFi] 请填写 SSID');return}sendCmd('wifi_save',{ssid:s,pass:p});appendTerminal('→ 已提交配置: '+s+'（写入 NVS 并自动连接）')}
function clearWifi(){sendCmd('wifi_clear',1);document.getElementById('wifiSsid').value='';document.getElementById('wifiPass').value='';appendTerminal('→ 已清除自定义网络')}
function updateWifiList(n){const sb=document.querySelector('.scan-btn');if(sb){sb.textContent='🔄 扫描附近WiFi';sb.disabled=false}const l=document.getElementById('wifiList');if(!n||!n.length){l.innerHTML='<div style="text-align:center;color:var(--text2);padding:20px;">未发现WiFi</div>';document.getElementById('wifiCount').textContent='0 个';return}document.getElementById('wifiCount').textContent=n.length+' 个';l.innerHTML=n.map(w=>{const p=Math.min(100,Math.max(0,(w.rssi+100)*2)),c=p>60?'var(--green)':p>30?'var(--orange)':'var(--red)',e=w.enc?'🔒':'🔓';return`<div class="wifi-item"><div><div class="wifi-name">${esc(w.ssid)}</div><div style="font-size:.7em;color:var(--text2);">CH${w.ch} | ${w.bssid}</div></div><div style="display:flex;align-items:center;gap:8px;"><span class="wifi-enc">${e}</span><div class="wifi-rssi"><div class="rssi-bar"><div class="rssi-fill" style="width:${p}%;background:${c}"></div></div><span style="font-size:.75em;width:45px;">${w.rssi}dBm</span></div></div></div>`}).join('')}
function updateLED(){const h=+document.getElementById('hueSlider').value,s=+document.getElementById('satSlider').value,v=+document.getElementById('valSlider').value;document.getElementById('hueVal').textContent=Math.round(h/255*360)+'°';document.getElementById('satVal').textContent=Math.round(s/255*100)+'%';document.getElementById('valVal').textContent=Math.round(v/255*100)+'%';const rgb=hsv2rgb(h/255,s/255,v/255),p=document.getElementById('ledPreview');p.style.background=`rgb(${rgb[0]},${rgb[1]},${rgb[2]})`;p.style.boxShadow=`0 0 30px rgba(${rgb[0]},${rgb[1]},${rgb[2]},.6)`;clearTimeout(ledTimer);ledTimer=setTimeout(()=>sendCmd('led_color',{h,s,v}),50)}
function hsv2rgb(h,s,v){let r,g,b;const i=Math.floor(h*6),f=h*6-i,p=v*(1-s),q=v*(1-f*s),t=v*(1-(1-f)*s);switch(i%6){case 0:r=v;g=t;b=p;break;case 1:r=q;g=v;b=p;break;case 2:r=p;g=v;b=t;break;case 3:r=p;g=q;b=v;break;case 4:r=t;g=p;b=v;break;case 5:r=v;g=p;b=q;break}return[Math.round(r*255),Math.round(g*255),Math.round(b*255)]}
function initTouchGrid(){document.getElementById('touchGrid').innerHTML=[1,2,3,4,5,6].map(p=>`<div class="touch-pad" id="tp${p}"><div class="touch-pin">GPIO ${p}</div><div class="touch-value" id="tv${p}">--</div><div class="touch-bar"><div class="touch-bar-fill" id="tb${p}" style="width:0%"></div></div></div>`).join('')}
function updateTouch(d){if(!d)return;d.forEach(t=>{const p=document.getElementById('tp'+t.pin),v=document.getElementById('tv'+t.pin),b=document.getElementById('tb'+t.pin);if(p&&v&&b){v.textContent=t.value;b.style.width=Math.min(100,t.value)+'%';t.touched?p.classList.add('touched'):p.classList.remove('touched')}})}
function toggleGpioOut(){const b=document.getElementById('gpioOutBtn'),on=b.textContent==='ON';sendCmd('gpio_out',on?0:1);b.textContent=on?'OFF':'ON';b.className=on?'btn btn-primary btn-sm':'btn btn-danger btn-sm'}
function updatePWM(){const v=document.getElementById('pwmSlider').value;document.getElementById('pwmVal').textContent=v;sendCmd('gpio_pwm',+v)}
function updateGPIO(d){if(d.din!==undefined){const e=document.getElementById('gpioInVal');e.textContent=d.din?'HIGH (1)':'LOW (0)';e.style.color=d.din?'var(--green)':'var(--red)'}if(d.adc!==undefined)document.getElementById('adcVal').textContent=d.adc+' ('+(d.adc/4095*3.3).toFixed(2)+'V)'}
function updateSystem(d){if(!d)return;if(d.chip)document.getElementById('sysChip').textContent=d.chip;if(d.cores)document.getElementById('sysCores').textContent=d.cores;if(d.cpuFreq)document.getElementById('sysCpu').textContent=d.cpuFreq+' MHz';if(d.flash)document.getElementById('sysFlash').textContent=d.flash;if(d.psram)document.getElementById('sysPsram').textContent=d.psram;if(d.heap){document.getElementById('sysHeap').textContent=d.heap;document.getElementById('freeMem').textContent='内存: '+d.heap}if(d.temp){document.getElementById('sysTemp').textContent=d.temp;document.getElementById('chipTemp').textContent='温度: '+d.temp}if(d.uptime){document.getElementById('sysUptime').textContent=d.uptime;document.getElementById('uptimeInfo').textContent='运行: '+d.uptime}if(d.wifiMode)document.getElementById('sysWifi').textContent=d.wifiMode;if(d.wifiPlan)document.getElementById('sysPlan').textContent=d.wifiPlan;if(d.wifiCustom)document.getElementById('sysCustom').textContent=d.wifiCustom;if(d.serialInfo)document.getElementById('usbStats').textContent=d.serialInfo;if(d.statusBrightness!==undefined)initStatusBrightness(d.statusBrightness);if(d.wifiIP){document.getElementById('sysIP').textContent=d.wifiIP;document.getElementById('ipDisplay').textContent='🌐 '+d.wifiIP}if(d.wifiSSID)document.getElementById('sysSSID').textContent=d.wifiSSID;if(d.wifiRSSI)document.getElementById('sysRSSI').textContent=d.wifiRSSI;if(d.wifiGW)document.getElementById('sysGW').textContent=d.wifiGW;if(d.mac)document.getElementById('sysMac').textContent=d.mac;if(d.sdk)document.getElementById('sysSdk').textContent=d.sdk;if(d.reset)document.getElementById('sysReset').textContent=d.reset}
function sendUSB(){const i=document.getElementById('usbInput'),t=i.value.trim();if(!t)return;appendTerminal('→ '+t);sendCmd('usb_send',{text:t,target:+document.getElementById('serialTarget').value,nl:document.getElementById('serialNL').checked?1:0});i.value=''}
function setUartBaud(){const b=+document.getElementById('serialBaud').value;sendCmd('usb_baud',b);appendTerminal('→ 设置 UART0 波特率: '+b)}
function setSerialConfig(){sendCmd('usb_config',{target:+document.getElementById('serialTarget').value,hex:document.getElementById('serialHex').checked?1:0,echo:document.getElementById('serialEcho').checked?1:0})}
let statusSeq=0;function setStatus(s){statusSeq++;sendCmd('status_set',{state:s,seq:statusSeq,msg:'Web面板手动切换'})}
function updateStatus(d){document.getElementById('statusBadge').textContent=d.state;document.getElementById('statusInfo').textContent='seq='+d.seq+' | prio='+d.prio+' | link='+d.link+' | sticky='+d.sticky+' | src='+(d.src||'*')+' | since='+d.since_ms+'ms'}
function updateSources(d){if(!d)return;const b=document.getElementById('bindModeBadge');if(b)b.textContent=d.mode==1?'仅绑定来源':'全部接受';const l=document.getElementById('sourcesList');if(!l)return;const sq=t=>esc(t==null?'':t).replace(/'/g,'&#39;');const list=d.sources||[];if(!list.length){l.innerHTML='<div style="text-align:center;color:var(--text2);padding:12px;">暂无来源，等待 PC 端上报状态…</div>';return}l.innerHTML=list.map(s=>{const age=Math.round((s.age_ms||0)/1000),on=!!s.bound,c=s.online?'var(--green)':'var(--text2)',bd=on?'var(--accent)':'rgba(255,255,255,.12)';return`<div style="display:flex;align-items:center;gap:8px;padding:8px;border:1px solid ${bd};border-radius:8px;flex-wrap:wrap;"><label style="display:flex;align-items:center;gap:6px;cursor:pointer;min-width:120px;flex:1;"><input type="checkbox" ${on?'checked':''} onchange="toggleBind('${sq(s.src)}',this.checked)"><span style="font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(s.src)}</span></label><span style="color:${c};font-size:.8em;">● ${esc(s.state)}</span><span style="font-size:.7em;color:var(--text2);">${age}s前</span><span style="font-size:.7em;color:var(--text2);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(s.msg||'')}">${esc(s.msg||'')}</span></div>`}).join('')}
function toggleBind(src,on){sendCmd('status_bind',{src:src,on:on?1:0})}
let dragCard=null;
const CARD_ORDER_KEY='esp32_card_order_v1';
function resetCardOrder(){localStorage.removeItem(CARD_ORDER_KEY);location.reload()}
function saveCardOrder(){const g=document.querySelector('.grid');if(!g)return;localStorage.setItem(CARD_ORDER_KEY,JSON.stringify([...g.querySelectorAll(':scope > .card')].map(c=>c.dataset.card||'')))}
function initCardDrag(){
  const grid=document.querySelector('.grid');if(!grid)return;
  [...grid.querySelectorAll(':scope > .card')].forEach((c,i)=>{
    const t=c.querySelector('.card-title');if(!t)return;
    const key=(t.textContent||'').replace(/\s+/g,'').slice(0,24)||('card'+i);
    c.dataset.card=key;
    t.setAttribute('draggable','true');
    t.title='按住拖动可调整这张卡片的位置';
    if(!t.querySelector('.drag-handle')){const h=document.createElement('span');h.className='drag-handle';h.textContent='⠿';t.appendChild(h)}
    t.addEventListener('dragstart',e=>{dragCard=c;c.classList.add('dragging');e.dataTransfer.effectAllowed='move';try{e.dataTransfer.setData('text/plain',key)}catch(_){}});
    t.addEventListener('dragend',()=>{c.classList.remove('dragging');document.querySelectorAll('.card.drag-over').forEach(x=>x.classList.remove('drag-over'));saveCardOrder()});
    c.addEventListener('dragover',e=>{if(!dragCard||dragCard===c)return;e.preventDefault();c.classList.add('drag-over');const r=c.getBoundingClientRect();const after=(e.clientY-r.top)>r.height/2;grid.insertBefore(dragCard,after?c.nextSibling:c)});
    c.addEventListener('dragleave',()=>c.classList.remove('drag-over'));
    c.addEventListener('drop',e=>{e.preventDefault();c.classList.remove('drag-over')});
  });
  try{const order=JSON.parse(localStorage.getItem(CARD_ORDER_KEY)||'[]');order.forEach(k=>{const el=[...grid.querySelectorAll(':scope > .card')].find(x=>x.dataset.card===k);if(el)grid.appendChild(el)})}catch(_){}
}
let fxData=null;
function fxPeriodText(p){return p>0?(p/1000).toFixed(1)+'s':'—'}
function loadFx(){fetch('/api/fx').then(r=>r.json()).then(d=>{if(d&&d.states)renderFx(d);else document.getElementById('fxList').textContent='配置读取失败'}).catch(e=>{document.getElementById('fxList').textContent='灯效配置读取失败：'+e.message})}
function renderFx(d){
  fxData=d;const el=document.getElementById('fxList');
  const sum=document.getElementById('fxSummary');
  if(sum)sum.textContent='灯效配置（'+d.states.length+' 个状态）· 全局亮度 '+Math.round((d.brightness||0)/255*100)+'% · 点开可改颜色与闪烁';
  el.innerHTML=d.states.map((s,i)=>{
    const grad='linear-gradient(135deg,'+s.color1+' 0%,'+(s.color2||s.color1)+' 100%)';
    const opts=d.fxNames.map((n,j)=>'<option value="'+j+'"'+(j===s.fx?' selected':'')+'>'+esc(n)+'</option>').join('');
    return `<div class="fx-row">
      <div class="fx-head" onclick="setStatus('${s.key}')" title="点一下把灯切到这个状态预览">
        <span class="fx-swatch" style="background:${grad}"></span>
        <span class="fx-name">${esc(s.label)}</span>
        <span class="fx-desc">${esc(s.fxName)} · ${fxPeriodText(s.periodMs)} · 亮度 ${s.level}%</span>
        <button class="btn btn-primary btn-sm" onclick="event.stopPropagation();toggleFxEdit(${i})">配置</button>
      </div>
      <div class="fx-edit" id="fxEdit${i}">
        <label>主色 <input type="color" id="fxC1_${i}" value="${s.color1}"></label>
        <label>辅色 <input type="color" id="fxC2_${i}" value="${s.color2}"></label>
        <label>效果 <select id="fxSel_${i}">${opts}</select></label>
        <label>亮度 <input type="range" id="fxLv_${i}" min="5" max="100" value="${s.level}" oninput="document.getElementById('fxLvT_${i}').textContent=this.value+'%'"><span id="fxLvT_${i}">${s.level}%</span></label>
        <label>周期ms <input type="number" id="fxPd_${i}" min="0" max="10000" step="100" value="${s.periodMs}"></label>
        <button class="btn btn-success btn-sm" onclick="saveFx(${i})">保存到设备</button>
        <button class="btn btn-warning btn-sm" onclick="setStatus('${s.key}')">预览灯效</button>
      </div>
    </div>`;
  }).join('');
}
function toggleFxEdit(i){const e=document.getElementById('fxEdit'+i);if(e)e.classList.toggle('open')}
function saveFx(i){
  const s=fxData.states[i];
  const body={key:s.key,c1:document.getElementById('fxC1_'+i).value,c2:document.getElementById('fxC2_'+i).value,fx:+document.getElementById('fxSel_'+i).value,level:+document.getElementById('fxLv_'+i).value,periodMs:+document.getElementById('fxPd_'+i).value};
  fetch('/api/fx',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
    .then(r=>r.json()).then(d=>{if(d.ok){renderFx(d);appendTerminal('→ 灯效已保存：'+s.label)}else{appendTerminal('← 保存失败：'+(d.reason||'unknown'))}})
    .catch(e=>appendTerminal('← 保存失败：'+e.message));
}
function resetFx(){
  fetch('/api/fx',{method:'POST',headers:{'Content-Type':'application/json'},body:'{"reset":1}'})
    .then(r=>r.json()).then(d=>{if(d.ok){renderFx(d);appendTerminal('→ 已恢复默认灯效')}else{appendTerminal('← 恢复失败：'+(d.reason||'unknown'))}})
    .catch(e=>appendTerminal('← 恢复失败：'+e.message));
}
let statusBrightTimer=null,statusBrightInit=false;
function setStatusBrightness(){const v=+document.getElementById('statusBrightness').value;document.getElementById('statusBrightVal').textContent=Math.round(v/255*100)+'%';clearTimeout(statusBrightTimer);statusBrightTimer=setTimeout(()=>sendCmd('status_brightness',v),80)}
function initStatusBrightness(v){if(statusBrightInit)return;const e=document.getElementById('statusBrightness');if(e){e.value=v;document.getElementById('statusBrightVal').textContent=Math.round(v/255*100)+'%';statusBrightInit=true}}
function appendTerminal(t){const e=document.getElementById('usbTerminal'),l=document.createElement('div');l.textContent=t;e.appendChild(l);e.scrollTop=e.scrollHeight;while(e.children.length>100)e.removeChild(e.firstChild)}
function clearTerminal(){document.getElementById('usbTerminal').innerHTML='[已清空]'}
const aiCanvas=document.getElementById('aiCanvas'),aiCtx=aiCanvas.getContext('2d');let isDrawing=false;aiCtx.fillStyle='#000';aiCtx.fillRect(0,0,160,160);
aiCanvas.addEventListener('mousedown',e=>{isDrawing=true;draw(e)});aiCanvas.addEventListener('mousemove',e=>{if(isDrawing)draw(e)});aiCanvas.addEventListener('mouseup',()=>isDrawing=false);aiCanvas.addEventListener('mouseleave',()=>isDrawing=false);
aiCanvas.addEventListener('touchstart',e=>{e.preventDefault();isDrawing=true;draw(e.touches[0])});aiCanvas.addEventListener('touchmove',e=>{e.preventDefault();if(isDrawing)draw(e.touches[0])});aiCanvas.addEventListener('touchend',()=>isDrawing=false);
function draw(e){const r=aiCanvas.getBoundingClientRect(),x=(e.clientX-r.left)*(160/r.width),y=(e.clientY-r.top)*(160/r.height);aiCtx.beginPath();aiCtx.arc(x,y,6,0,Math.PI*2);aiCtx.fillStyle='#fff';aiCtx.fill()}
function clearCanvas(){aiCtx.fillStyle='#000';aiCtx.fillRect(0,0,160,160);document.getElementById('aiResults').innerHTML=''}
function loadRandomPattern(){clearCanvas();aiCtx.strokeStyle='#fff';aiCtx.lineWidth=8;const s=['c','s','t','r'][Math.floor(Math.random()*4)];aiCtx.beginPath();if(s==='c'){aiCtx.arc(80,80,50,0,Math.PI*2);aiCtx.stroke()}else if(s==='s'){aiCtx.rect(30,30,100,100);aiCtx.stroke()}else if(s==='t'){aiCtx.moveTo(80,20);aiCtx.lineTo(140,140);aiCtx.lineTo(20,140);aiCtx.closePath();aiCtx.stroke()}else{for(let i=0;i<5;i++){const a=(i*4*Math.PI/5)-Math.PI/2,x=80+55*Math.cos(a),y=80+55*Math.sin(a);i===0?aiCtx.moveTo(x,y):aiCtx.lineTo(x,y)}aiCtx.closePath();aiCtx.stroke()}}
function runAIInference(){const img=aiCtx.getImageData(0,0,160,160),f=[];for(let by=0;by<4;by++)for(let bx=0;bx<4;bx++){let s=0;for(let dy=0;dy<40;dy++)for(let dx=0;dx<40;dx++)s+=img.data[((by*40+dy)*160+(bx*40+dx))*4];f.push(s/(40*40*255))}document.getElementById('aiInferTime').textContent='推理中...';sendCmd('ai_infer',f)}
function updateAIResults(d){document.getElementById('aiInferTime').textContent='推理耗时: '+(d.time_ms||'--')+'ms';const c=['var(--green)','var(--accent)','var(--orange)','var(--accent2)'],l=d.labels||['圆形','方形','三角','星形'],p=d.probs||[.25,.25,.25,.25],m=p.indexOf(Math.max(...p));document.getElementById('aiResults').innerHTML=p.map((v,i)=>`<div class="ai-result-item" style="${i===m?'border:1px solid '+c[i]:''}"><div class="ai-result-label">${esc(l[i])}</div><div class="ai-result-bar"><div class="ai-result-fill" style="width:${(v*100).toFixed(0)}%;background:${c[i]}"></div></div><div class="ai-result-pct" style="color:${c[i]}">${(v*100).toFixed(1)}%</div></div>`).join('')}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
window.addEventListener('load',()=>{connectWS();loadFx();initCardDrag();initTouchGrid();updateAIResults({probs:[.25,.25,.25,.25],labels:['圆形','方形','三角','星形'],time_ms:0});updateLED();setInterval(()=>sendCmd('touch_read',1),200);setInterval(()=>sendCmd('gpio_read',1),1000);setInterval(()=>sendCmd('sys_info',1),3000)});
</script>
</body>
</html>
)rawliteral";

// ==================== setup() ====================

void setup() {
  Serial.begin(115200);        // Serial = 原生 USB Serial/JTAG CDC (COM16)
  Serial.setTimeout(10);
  Serial0.begin(g_uartBaud);   // 第二通道：UART0 / GPIO43(TX) GPIO44(RX) / CH343
  Serial0.setTimeout(10);
  delay(200);                  // 等待 USB CDC 枚举完成

  Serial.println("\n========================================");
  Serial.println("  ESP32-S3 全能 Web 调试面板 v1.5.0 (多来源绑定)");
  Serial.printf("[SYS] Firmware %s / Protocol %s\n", FW_VERSION, PROTO_VERSION);
  Serial.println("========================================");
  #if defined(ARDUINO_USB_CDC_ON_BOOT) && ARDUINO_USB_CDC_ON_BOOT
  Serial.println("[USB] Serial -> USB Serial/JTAG CDC (VID_303A:1001)");
  #else
  Serial.println("[USB] Serial -> UART0 (USB 转串口)");
  #endif
  Serial.printf("[UART0] Serial0 @ %lu baud (CH343 / GPIO43,44)\n", (unsigned long)g_uartBaud);
  Serial0.println("[SYS] 日志走 USB CDC；本口为 UART0 数据桥接通道");
  Serial.printf("[SYS] Flash: %lu MB / PSRAM: %lu MB\n",
                (unsigned long)(ESP.getFlashChipSize() / (1024 * 1024)),
                (unsigned long)(ESP.getPsramSize() / (1024 * 1024)));
  Serial.println();

  // 板载 WS2812 (Core 内置 RMT 驱动，无需 FastLED)
  for (int i = 0; i < 3; i++) {
    showRgb(0, 0, 255); delay(150);
    showRgb(0, 0, 0);   delay(150);
  }

  // 初始化 GPIO
  pinMode(GPIO_OUT_PIN, OUTPUT);
  digitalWrite(GPIO_OUT_PIN, LOW);
  pinMode(GPIO_IN_PIN, INPUT_PULLUP);

  // ESP32 Arduino Core 3.x LEDC API
  if (!ledcAttach(GPIO_PWM_PIN, gpioPwmFreq, PWM_RESOLUTION)) {
    Serial.println("[PWM] ⚠️ ledcAttach 失败");
  }
  ledcWrite(GPIO_PWM_PIN, gpioPwmValue);

  // 初始化 AI 模型
  initAIModel();

  loadStatusBrightness();     // 从 NVS 恢复状态灯亮度
  loadStatusFx();             // v1.5.0-fx：恢复每个状态的颜色/动效配置
  loadBindConfig();           // v1.5.0：恢复绑定模式与白名单

  // ============ WiFi: 自定义 -> 默认 -> 备用网络 -> AP 兜底 ============
  rebuildWifiList();
  setupWifi();

  // ---- v1.5.0 设备发现：mDNS + UDP 广播，IP 变化后仍可被找到 ----
  g_discoveryUdp.begin(DISCOVERY_UDP_PORT);
  Serial.printf("[发现] mDNS=http://%s.local / UDP 广播 255.255.255.255:%u 每 %lus\n",
                DISCOVERY_HOST, (unsigned)DISCOVERY_UDP_PORT,
                (unsigned long)(DISCOVERY_BEACON_MS / 1000));

  // WebSocket + HTTP 路由
  ws.onEvent(onWsEvent);
  server.addHandler(&ws);

  server.on("/", HTTP_GET, [](AsyncWebServerRequest *request) {
    request->send(200, "text/html; charset=utf-8", INDEX_HTML);
  });

  server.on("/api/system", HTTP_GET, [](AsyncWebServerRequest *request) {
    sendJsonResponse(request, 200, getSystemInfoJson());
  });

  // ---- ai-status/1：设备发现 / 状态查询 / 状态设置 / CORS 预检 ----
  server.on("/api/hello", HTTP_GET, [](AsyncWebServerRequest *request) {
    sendJsonResponse(request, 200, getHelloJson());
  });

  server.on("/api/status", HTTP_GET, [](AsyncWebServerRequest *request) {
    sendJsonResponse(request, 200, getStatusJson());
  });

  server.on("/api/sources", HTTP_GET, [](AsyncWebServerRequest *request) {
    sendJsonResponse(request, 200, getSourcesJson());
  });

  // ---- v1.5.0 BUGFIX：立即触发一次发现广播；peer=单播地址可做广播/单播对照 ----
  server.on("/api/discover", HTTP_GET, [](AsyncWebServerRequest *request) {
    if (request->hasParam("peer")) {
      IPAddress peer;
      if (peer.fromString(request->getParam("peer")->value())) sendDiscoveryBeacon(peer);
    }
    sendDiscoveryBeacon(IPAddress(255, 255, 255, 255));
    IPAddress bcast = wifiInApMode ? WiFi.softAPBroadcastIP() : WiFi.broadcastIP();
    sendDiscoveryBeacon(bcast);
    sendJsonResponse(request, 200, getDiscoveryDiagJson());
  });

  // ---- v1.5.0-fx：每个状态的颜色/动效配置（网页可编辑，NVS 持久化） ----
  server.on("/api/fx", HTTP_GET, [](AsyncWebServerRequest *request) {
    sendJsonResponse(request, 200, getStatusFxJson());
  });
  server.on("/api/fx", HTTP_OPTIONS, [](AsyncWebServerRequest *request) {
    AsyncWebServerResponse *r = request->beginResponse(204);
    r->addHeader("Access-Control-Allow-Origin", "*");
    r->addHeader("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
    r->addHeader("Access-Control-Allow-Headers", "Content-Type");
    request->send(r);
  });
  server.addHandler(new AsyncCallbackJsonWebHandler("/api/fx",
    [](AsyncWebServerRequest *request, JsonVariant &json) {
      JsonObject o = json.as<JsonObject>();
      if (o.isNull()) {
        sendJsonResponse(request, 200, "{\"ok\":false,\"reason\":\"invalid_json\"}");
        return;
      }
      String reason;
      bool ok = (o["reset"].as<int>() != 0)
              ? resetStatusFx() : setStatusFxItem(o, reason);
      if (!ok && !reason.length()) reason = "nvs_write_failed";
      if (ok) {
        sendJsonResponse(request, 200, getStatusFxJson());
      } else {
        JsonDocument e;
        e["ok"] = false;
        e["reason"] = reason;
        String out; serializeJson(e, out);
        sendJsonResponse(request, 200, out);
      }
    }));

  server.on("/api/status", HTTP_OPTIONS, [](AsyncWebServerRequest *request) {
    AsyncWebServerResponse *r = request->beginResponse(204);
    r->addHeader("Access-Control-Allow-Origin", "*");
    r->addHeader("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
    r->addHeader("Access-Control-Allow-Headers", "Content-Type");
    request->send(r);
  });

  // ---- v1.5.0：来源列表与绑定白名单 ----
  server.on("/api/bind", HTTP_GET, [](AsyncWebServerRequest *request) {
    sendJsonResponse(request, 200, getSourcesJson());
  });
  server.on("/api/bind", HTTP_OPTIONS, [](AsyncWebServerRequest *request) {
    AsyncWebServerResponse *r = request->beginResponse(204);
    r->addHeader("Access-Control-Allow-Origin", "*");
    r->addHeader("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
    r->addHeader("Access-Control-Allow-Headers", "Content-Type");
    request->send(r);
  });
  server.addHandler(new AsyncCallbackJsonWebHandler("/api/bind",
    [](AsyncWebServerRequest *request, JsonVariant &json) {
      JsonObject o = json.as<JsonObject>();
      if (o.isNull()) {
        sendJsonResponse(request, 200, "{\"ok\":false,\"reason\":\"invalid_json\"}");
        return;
      }
      if (!o["mode"].isNull()) {
        g_bindMode = (o["mode"].as<int>() != 0) ? 1 : 0;
        saveBindConfig();
      }
      const char *src = o["src"].as<const char*>();
      if (src && src[0] && !o["on"].isNull()) {
        setSourceBind(src, o["on"].as<int>() != 0);
      }
      if (o["clear"].as<int>() != 0) clearBindList();
      recomputeAggregate(millis());
      publishSources();
      sendJsonResponse(request, 200, getSourcesJson());
    }));

  // POST /api/status 要求 Content-Type: application/json
  server.addHandler(new AsyncCallbackJsonWebHandler("/api/status",
    [](AsyncWebServerRequest *request, JsonVariant &json) {
      JsonObject o = json.as<JsonObject>();
      if (o.isNull()) {
        sendJsonResponse(request, 200, statusAckJson(false, "invalid_json", ""));
        return;
      }
      const char *rid = o["reqId"].as<const char*>();
      if (!rid) rid = "";
      if (!o["brightness"].isNull()) {
        setStatusBrightness((uint8_t)constrain(o["brightness"].as<int>(), 0, 255));
      }
      String reason;
      bool ok = applyStatusSrc(o["state"].as<const char*>(),
                               o["seq"].as<uint32_t>(),
                               o["ttl"].as<uint32_t>(),
                               o["msg"].as<const char*>(),
                               (o["force"].as<int>() != 0),
                               rid, o["src"].as<const char*>(), reason);
      sendJsonResponse(request, 200, statusAckJson(ok, reason, rid));
    }));

  server.begin();

  Serial.println("[HTTP] ✅ Web服务器已启动");
  Serial.println("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
  Serial.printf("  🌐 请在浏览器访问: http://%s\n", deviceIP.c_str());
  Serial.println("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n");

  refreshLed();
}

// ==================== loop() ====================

void loop() {
  ws.cleanupClients();

  // ---- AI 状态灯：按来源做心跳/TTL 超时检查 + 聚合 ----
  checkSourcesTimeout(millis());

  // ---- v1.5.0 设备发现：mDNS 就绪 + UDP 广播 ----
  serviceDiscoveryBeacon(millis());

  // ---- LED 动画（30ms 刷新，非阻塞） ----
  static uint32_t lastLedAnim = 0;
  static uint8_t  animHue = 0;
  static uint8_t  breatheVal = 10;
  static int8_t   breatheDir = 1;

  if (millis() - lastLedAnim > 30) {
    lastLedAnim = millis();
    if (g_statusEngine) {
      renderStatus(millis());          // 状态引擎接管 WS2812
    } else if (g_rainbow) {
      showHsv(animHue++, 255, 200);
    } else if (g_breathe) {
      int16_t next = (int16_t)breatheVal + breatheDir * 3;
      if (next >= 250) { next = 250; breatheDir = -1; }
      if (next <= 10)  { next = 10;  breatheDir = 1; }
      breatheVal = (uint8_t)next;
      showHsv(ledHue, ledSat, breatheVal);
    }
  }

  // ---- 双串口桥接：USB CDC + UART0（无换行数据也实时上送） ----
  processSerialStream(Serial,  g_cdcLine,  g_cdcLastMs,  "USB");
  processSerialStream(Serial0, g_uartLine, g_uartLastMs, "UART0");

  // ---- WiFi 非阻塞重连状态机 ----
  serviceWifi();

  // ---- 异步 WiFi 扫描结果 ----
  serviceWifiScan();

  // ---- 延迟应用新保存的 WiFi 配置（让回执先送达网页） ----
  if (g_wifiApplyAtMs && (int32_t)(millis() - g_wifiApplyAtMs) >= 0) {
    g_wifiApplyAtMs = 0;
    g_wifiTrying = false;
    beginStaAttempt(0);                 // 从新的优先级列表第 ① 项开始
  }

  // ---- 延迟重启（避免在 async_tcp 回调里直接 ESP.restart()） ----
  if (g_restartRequested && (int32_t)(millis() - g_restartAtMs) >= 0) {
    g_restartRequested = false;
    Serial.println("[SYS] 正在重启...");
    delay(100);
    ESP.restart();
  }

  delay(1);
}

// ==================== WebSocket 事件处理 ====================

void onWsEvent(AsyncWebSocket *server, AsyncWebSocketClient *client,
               AwsEventType type, void *arg, uint8_t *data, size_t len) {
  if (type == WS_EVT_CONNECT) {
    Serial.printf("[WS] 客户端 #%lu 已连接\n", (unsigned long)client->id());
    String sysInfo = getSystemInfoJson();
    client->text(sysInfo);
  }
  else if (type == WS_EVT_DISCONNECT) {
    Serial.printf("[WS] 客户端 #%lu 已断开\n", (unsigned long)client->id());
  }
  else if (type == WS_EVT_DATA) {
    AwsFrameInfo *info = (AwsFrameInfo*)arg;
    if (!info || info->opcode != WS_TEXT) return;
    // 只处理完整单帧消息（面板指令都很短），避免分片拼接的复杂度
    if (!info->final || info->index != 0) return;
    if (len == 0 || len > 4096) return;

    // 修复: 原代码 data[len]=0 会越界写 AsyncWebSocket 内部缓冲区
    char *msg = (char*)malloc(len + 1);
    if (!msg) return;
    memcpy(msg, data, len);
    msg[len] = '\0';
    handleWsMessage(client, msg);
    free(msg);
  }
  else if (type == WS_EVT_ERROR) {
    Serial.printf("[WS] 客户端 #%lu 错误\n", (unsigned long)client->id());
  }
}

void handleWsMessage(AsyncWebSocketClient *client, char *data) {
  JsonDocument doc;
  DeserializationError error = deserializeJson(doc, data);
  if (error) {
    Serial.printf("[WS] JSON解析错误: %s\n", error.c_str());
    return;
  }

  const char *cmd = doc["cmd"];
  if (!cmd) return;

  if (strcmp(cmd, "wifi_scan") == 0) {
    requestWifiScan();
  }
  else if (strcmp(cmd, "wifi_retry") == 0) {
    if (!g_wifiTrying) beginStaAttempt(0);
    broadcastUSB("[WiFi] 已触发重新连接: 自定义 → 默认 → 备用网络 → AP");
  }
  else if (strcmp(cmd, "wifi_save") == 0) {
    JsonObject cfg = doc["val"];
    if (cfg.isNull()) { broadcastUSB("[WiFi] ❌ 配置格式错误"); return; }
    const char *ssid = cfg["ssid"].as<const char*>();
    const char *pass = cfg["pass"].as<const char*>();
    if (!ssid) ssid = "";
    if (!pass) pass = "";

    size_t sl = strlen(ssid), pl = strlen(pass);
    bool hasControlChar = false;
    for (size_t i = 0; i < sl; i++) {
      if ((unsigned char)ssid[i] < 0x20) { hasControlChar = true; break; }
    }
    if (sl == 0 || sl >= WIFI_MAX_SSID || pl >= WIFI_MAX_PASS || hasControlChar) {
      broadcastUSB("[WiFi] ❌ SSID(1~32字节) 或密码(0~64字节) 不合法");
      return;
    }
    if (!nvsSaveWifi(ssid, pass)) {
      broadcastUSB("[WiFi] ❌ NVS 写入失败，配置未保存");
      return;
    }

    rebuildWifiList();
    broadcastUSB(String("[WiFi] ✅ 已保存: ") + ssid + "，即将优先连接；失败则依次降级 → AP");
    g_wifiApplyAtMs = millis() + 600;      // 留时间把回执推送到网页
  }
  else if (strcmp(cmd, "wifi_clear") == 0) {
    nvsClearWifi();
    rebuildWifiList();
    broadcastUSB("[WiFi] ✅ 已清除自定义网络，恢复: YOUR_WIFI_SSID → 备用网络 → AP");
    g_wifiApplyAtMs = millis() + 600;
  }
  else if (strcmp(cmd, "led_color") == 0) {
    JsonObject color = doc["val"];
    if (!color.isNull()) {
      ledHue = (uint8_t)constrain(color["h"].as<int>(), 0, 255);
      ledSat = (uint8_t)constrain(color["s"].as<int>(), 0, 255);
      ledVal = (uint8_t)constrain(color["v"].as<int>(), 0, 255);
      if (!g_rainbow && !g_breathe) refreshLed();
    }
  }
  else if (strcmp(cmd, "led_toggle") == 0) {
    ledOn = (doc["val"].as<int>() != 0);
    refreshLed();
  }
  else if (strcmp(cmd, "led_rainbow") == 0) {
    g_rainbow = (doc["val"].as<int>() != 0);
    g_breathe = false;
    refreshLed();
  }
  else if (strcmp(cmd, "led_breathe") == 0) {
    g_breathe = (doc["val"].as<int>() != 0);
    g_rainbow = false;
    refreshLed();
  }
  else if (strcmp(cmd, "led_effect") == 0) {
    g_rainbow = false;
    g_breathe = false;
    refreshLed();
  }
  else if (strcmp(cmd, "gpio_out") == 0) {
    gpioOutState = doc["val"].as<int>();
    digitalWrite(GPIO_OUT_PIN, gpioOutState ? HIGH : LOW);
  }
  else if (strcmp(cmd, "gpio_pwm") == 0) {
    gpioPwmValue = constrain(doc["val"].as<int>(), 0, (1 << PWM_RESOLUTION) - 1);
    ledcWrite(GPIO_PWM_PIN, gpioPwmValue);
  }
  else if (strcmp(cmd, "gpio_pwm_freq") == 0) {
    int f = doc["val"].as<int>();
    if (f >= 10 && f <= 10000000) {
      gpioPwmFreq = f;
      // 修复: 重复 ledcAttach 会持续占用新通道，改用 Core 3.x 的频率更新 API
      ledcChangeFrequency(GPIO_PWM_PIN, gpioPwmFreq, PWM_RESOLUTION);
      ledcWrite(GPIO_PWM_PIN, gpioPwmValue);
    }
  }
  else if (strcmp(cmd, "gpio_read") == 0 || strcmp(cmd, "gpio_scan") == 0) {
    sendGpioData(client);
  }
  else if (strcmp(cmd, "touch_read") == 0) {
    sendTouchData(client);
  }
  else if (strcmp(cmd, "sys_info") == 0) {
    String sysInfo = getSystemInfoJson();
    client->text(sysInfo);
  }
  else if (strcmp(cmd, "sys_restart") == 0) {
    client->text("{\"type\":\"system\",\"data\":{\"reset\":\"重启中...\"}}");
    g_restartAtMs = millis() + 500;
    g_restartRequested = true;
  }
  else if (strcmp(cmd, "usb_send") == 0) {
    // 兼容旧格式（纯字符串）与新格式（{text,target,nl}）
    JsonVariant v = doc["val"];
    String text;
    uint8_t target = g_serialTarget;
    bool addNL = true;
    if (v.is<JsonObject>()) {
      JsonObject o = v.as<JsonObject>();
      const char *t = o["text"].as<const char*>();
      if (t) text = t;
      if (!o["target"].isNull()) target = (uint8_t)constrain(o["target"].as<int>(), 0, SERIAL_TARGET_BOTH);
      if (!o["nl"].isNull())     addNL = (o["nl"].as<int>() != 0);
    } else {
      const char *t = v.as<const char*>();
      if (t) text = t;
    }
    if (text.length() > 0 && text.length() < SERIAL_LINE_MAX) {
      writeSerialTarget(text, target, addNL);
      const char *dst = (target == SERIAL_TARGET_CDC) ? "USB"
                      : (target == SERIAL_TARGET_UART0) ? "UART0" : "双口";
      broadcastUSB(String("[发送→") + dst + "] " + text);
    }
  }
  else if (strcmp(cmd, "usb_baud") == 0) {
    uint32_t baud = doc["val"].as<uint32_t>();
    if (baud >= 1200 && baud <= 2000000) {
      g_uartBaud = baud;
      Serial0.updateBaudRate(baud);       // 真实 UART 热切换，无需重启
      broadcastUSB(String("[UART0] 波特率已切换为 ") + baud);
    } else {
      broadcastUSB("[UART0] ❌ 波特率需在 1200~2000000 之间");
    }
  }
  else if (strcmp(cmd, "usb_config") == 0) {
    JsonObject o = doc["val"];
    if (!o.isNull()) {
      if (!o["target"].isNull()) g_serialTarget = (uint8_t)constrain(o["target"].as<int>(), 0, SERIAL_TARGET_BOTH);
      if (!o["hex"].isNull())    g_serialHex    = (o["hex"].as<int>() != 0);
      if (!o["echo"].isNull())   g_usbEcho      = (o["echo"].as<int>() != 0);
    }
    broadcastUSB(String("[串口] 目标=") + g_serialTarget + " HEX=" + (g_serialHex ? "开" : "关")
                 + " 回显=" + (g_usbEcho ? "开" : "关"));
  }
  else if (strcmp(cmd, "usb_echo") == 0) {
    g_usbEcho = (doc["val"].as<int>() != 0);
    broadcastUSB(g_usbEcho ? "[回环模式已开启]" : "[回环模式已关闭]");
  }
  else if (strcmp(cmd, "usb_info") == 0) {
    String info = "串口 / USB 设备信息:\n";
    #if defined(ARDUINO_USB_CDC_ON_BOOT) && ARDUINO_USB_CDC_ON_BOOT
    info += "  Serial  : ESP32-S3 USB Serial/JTAG CDC (VID_303A:1001)\n";
    info += "            波特率为虚拟值，改主机波特率不影响实际速率\n";
    #else
    info += "  Serial  : UART0 via USB 转串口\n";
    #endif
    info += "  Serial0 : UART0 / GPIO43(TX) GPIO44(RX) / CH343\n";
    info += String("            ") + g_uartBaud + " baud, 8N1 (网页可热切换)\n";
    info += String("  接收模式: ") + (g_serialHex ? "HEX" : "文本") + " / 静默 40ms 自动上送\n";
    info += String("  RX/TX   : ") + g_rxBytes + " / " + g_txBytes + " 字节\n";
    info += String("  WiFi    : ") + (wifiInApMode ? String("AP 兜底模式") : deviceIP) + "\n";
    broadcastUSB(info);
  }
  else if (strcmp(cmd, "status_set") == 0) {
    JsonObject o = doc["val"];
    const char *rid = doc["reqId"].as<const char*>();
    if (!rid) rid = "";
    String reason;
    bool ok = false;
    if (!o.isNull()) {
      ok = applyStatusSrc(o["state"].as<const char*>(), o["seq"].as<uint32_t>(),
                          o["ttl"].as<uint32_t>(), o["msg"].as<const char*>(),
                          (o["force"].as<int>() != 0), rid,
                          o["src"].as<const char*>(), reason);
    } else {
      reason = "invalid_json";
    }
    client->text(statusWsAckJson(ok, reason, rid));
  }
  else if (strcmp(cmd, "status_heartbeat") == 0) {
    JsonObject o = doc["val"];
    const char *rid = doc["reqId"].as<const char*>();
    if (!rid) rid = "";
    const char *st = o.isNull() ? nullptr : o["state"].as<const char*>();
    touchSource(o.isNull() ? nullptr : o["src"].as<const char*>(), st);
    client->text(statusWsAckJson(true, "", rid));
  }
  else if (strcmp(cmd, "status_clear") == 0) {
    clearAllSources();
    client->text(statusWsAckJson(true, "", ""));
  }
  else if (strcmp(cmd, "status_get") == 0) {
    publishStatus(nullptr, true, client);
  }
  else if (strcmp(cmd, "sources_get") == 0) {
    client->text(sourcesWsJson());
  }
  else if (strcmp(cmd, "status_bind") == 0) {
    JsonObject o = doc["val"];
    if (!o.isNull()) {
      const char *src = o["src"].as<const char*>();
      if (src && src[0]) setSourceBind(src, o["on"].as<int>() != 0);
    }
    recomputeAggregate(millis());
    publishSources();
    client->text(sourcesWsJson());
  }
  else if (strcmp(cmd, "bind_mode") == 0) {
    g_bindMode = (doc["val"].as<int>() != 0) ? 1 : 0;
    saveBindConfig();
    recomputeAggregate(millis());
    publishSources();
    client->text(sourcesWsJson());
  }
  else if (strcmp(cmd, "bind_clear") == 0) {
    clearBindList();
    recomputeAggregate(millis());
    publishSources();
    client->text(sourcesWsJson());
  }
  else if (strcmp(cmd, "status_enable") == 0) {
    g_statusEngine = (doc["val"].as<int>() != 0);
    if (!g_statusEngine) refreshLed();
    broadcastUSB(String("[状态灯] 引擎") + (g_statusEngine ? "已启用" : "已停用"));
    client->text(statusWsAckJson(true, "", ""));
  }
  else if (strcmp(cmd, "status_brightness") == 0) {
    int v = constrain(doc["val"].as<int>(), 0, 255);
    bool saved = setStatusBrightness((uint8_t)v);
    client->text(statusWsAckJson(true, "", ""));
    broadcastUSB(String("[状态灯] 亮度 ") + v + "/255 (" + (v * 100 / 255) + "%)"
                 + (saved ? "，已写入 NVS" : "，NVS 写入失败(本次仍生效)"));
  }
  else if (strcmp(cmd, "ai_infer") == 0) {
    doAIInference(client, doc);
  }
  else if (strcmp(cmd, "init") == 0) {
    String sysInfo = getSystemInfoJson();
    client->text(sysInfo);
    sendTouchData(client);
    sendGpioData(client);
  }
}

// ==================== RGB LED (IDF RMT 驱动) ====================

RgbColor hsvToRgb(uint8_t h, uint8_t s, uint8_t v) {
  // 8bit HSV -> RGB，整数运算，无浮点/无查表；与 FastLED CHSV 视觉一致
  uint8_t region    = h / 43;                    // 0..5
  uint8_t remainder = (h - region * 43) * 6;     // 0..252
  uint8_t p = (uint8_t)((v * (255 - s)) >> 8);
  uint8_t q = (uint8_t)((v * (255 - ((s * remainder) >> 8))) >> 8);
  uint8_t t = (uint8_t)((v * (255 - ((s * (255 - remainder)) >> 8))) >> 8);
  uint8_t r, g, b;
  switch (region) {
    case 0:  r = v; g = t; b = p; break;
    case 1:  r = q; g = v; b = p; break;
    case 2:  r = p; g = v; b = t; break;
    case 3:  r = p; g = q; b = v; break;
    case 4:  r = t; g = p; b = v; break;
    default: r = v; g = p; b = q; break;
  }
  return {r, g, b};
}

void showRgb(uint8_t r, uint8_t g, uint8_t b) {
  rgbLedWrite(LED_PIN, r, g, b);
}

void showHsv(uint8_t h, uint8_t s, uint8_t v) {
  if (!ledOn) { showRgb(0, 0, 0); return; }
  RgbColor c = hsvToRgb(h, s, v);
  showRgb(c.r, c.g, c.b);
}

void refreshLed() {
  if (g_statusEngine) { renderStatus(millis()); return; }
  showHsv(ledHue, ledSat, ledVal);
}

// ==================== 双串口桥接 ====================

void flushSerialLine(String &line, Stream &port, const char *tag) {
  if (line.length() == 0) return;
  g_rxBytes += line.length();

  // ai-status/1 协议帧优先处理，避免污染网页串口终端
  if (handleSerialProtocolLine(line, port, tag)) { line = ""; return; }

  String payload;
  if (g_serialHex) {
    char b[6];
    for (size_t i = 0; i < line.length(); i++) {
      snprintf(b, sizeof(b), "%02X ", (uint8_t)line[i]);
      payload += b;
    }
    payload.trim();
  } else {
    payload = line;
    payload.replace("\r", "\\r");
    payload.replace("\n", "\\n");   // 终端按单行显示，不撑爆 DOM
  }

  broadcastUSB(String("[") + tag + "] " + payload);

  if (g_usbEcho) {
    port.print("[回环] ");
    port.println(line);
  }
  line = "";
}

void processSerialStream(Stream &port, String &line, uint32_t &lastMs, const char *tag) {
  bool got = false;
  while (port.available() > 0) {
    char c = (char)port.read();
    got = true;
    line += c;
    if (c == '\n' || c == '\r' || line.length() >= SERIAL_LINE_MAX) {
      flushSerialLine(line, port, tag);
    }
  }
  if (got) lastMs = millis();

  // 关键修复：对端发送不带换行的数据时，静默 40ms 也上送，不再无限攒着
  if (line.length() > 0 && (millis() - lastMs) >= SERIAL_IDLE_FLUSH_MS) {
    flushSerialLine(line, port, tag);
  }
}

void writeSerialTarget(const String &text, uint8_t target, bool addNL) {
  if (target > SERIAL_TARGET_BOTH) target = SERIAL_TARGET_CDC;

  if (target == SERIAL_TARGET_CDC || target == SERIAL_TARGET_BOTH) {
    addNL ? Serial.println(text) : Serial.print(text);
  }
  if (target == SERIAL_TARGET_UART0 || target == SERIAL_TARGET_BOTH) {
    addNL ? Serial0.println(text) : Serial0.print(text);
  }
  g_txBytes += text.length();
}

// ==================== 自定义 WiFi 持久化 (直接调用 ESP-IDF NVS API) ====================

bool nvsInit() {
  esp_err_t err = nvs_flash_init();
  if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
    nvs_flash_erase();                    // 分区满或版本升级时重建
    err = nvs_flash_init();
  }
  return err == ESP_OK;
}

bool nvsLoadWifi(char *ssid, size_t ssidLen, char *pass, size_t passLen) {
  nvs_handle_t h;
  if (nvs_open(WIFI_NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) return false;
  ssid[0] = '\0';
  pass[0] = '\0';
  esp_err_t e1 = nvs_get_str(h, "ssid", ssid, &ssidLen);
  nvs_get_str(h, "pass", pass, &passLen);  // 允许开放网络：密码为空
  nvs_close(h);
  return e1 == ESP_OK && ssid[0] != '\0';
}

bool nvsSaveWifi(const char *ssid, const char *pass) {
  nvs_handle_t h;
  if (nvs_open(WIFI_NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) return false;
  esp_err_t e1 = nvs_set_str(h, "ssid", ssid);
  esp_err_t e2 = nvs_set_str(h, "pass", pass);
  esp_err_t e3 = (e1 != ESP_OK) ? e1 : (e2 != ESP_OK ? e2 : nvs_commit(h));
  nvs_close(h);
  return e3 == ESP_OK;
}

bool nvsClearWifi() {
  nvs_handle_t h;
  if (nvs_open(WIFI_NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) return false;
  nvs_erase_key(h, "ssid");
  nvs_erase_key(h, "pass");
  esp_err_t err = nvs_commit(h);
  nvs_close(h);
  return err == ESP_OK;
}

static void addWifiCandidate(const char *ssid, const char *pass) {
  if (g_wifiCount >= WIFI_MAX_CANDIDATES) return;
  strncpy(g_wifiList[g_wifiCount].ssid, ssid, WIFI_MAX_SSID - 1);
  strncpy(g_wifiList[g_wifiCount].password, pass, WIFI_MAX_PASS - 1);
  g_wifiList[g_wifiCount].ssid[WIFI_MAX_SSID - 1]     = '\0';
  g_wifiList[g_wifiCount].password[WIFI_MAX_PASS - 1] = '\0';
  g_wifiCount++;
}

void rebuildWifiList() {
  g_wifiCount = 0;
  char ssid[WIFI_MAX_SSID] = {0};
  char pass[WIFI_MAX_PASS] = {0};

  g_hasCustomWifi = nvsInit() && nvsLoadWifi(ssid, sizeof(ssid), pass, sizeof(pass));
  if (g_hasCustomWifi) {
    addWifiCandidate(ssid, pass);                            // ① 用户保存的网络
    Serial.printf("[WiFi] NVS 已保存自定义网络: %s\n", ssid);
  }
  addWifiCandidate(WIFI_DEFAULT_SSID, WIFI_DEFAULT_PASS);   // ② 默认主网络
  addWifiCandidate(WIFI_BACKUP_SSID,  WIFI_BACKUP_PASS);    // ③ 永久备用

  Serial.println("[WiFi] 连接优先级:");
  for (uint8_t i = 0; i < g_wifiCount; i++) {
    Serial.printf("  %u) %s\n", (unsigned)(i + 1), g_wifiList[i].ssid);
  }
  Serial.printf("  %u) AP(%s) 兜底\n", (unsigned)(g_wifiCount + 1), AP_SSID);
}

// ==================== WiFi 三级容错 ====================

bool tryConnectSta(const char *ssid, const char *pass, uint32_t timeoutMs) {
  Serial.println("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
  Serial.printf("[WiFi] 正在连接: %s\n", ssid);

  WiFi.disconnect(true, true);       // 关闭并清除上一次的 AP 配置
  delay(150);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  WiFi.begin(ssid, pass);

  uint32_t start = millis();
  int16_t breath = 10;               // 用有符号数，避免 uint8_t 反向递减下溢
  int8_t  dir = 1;
  while (WiFi.status() != WL_CONNECTED && (millis() - start) < timeoutMs) {
    delay(50);
    breath += dir * 5;
    if (breath >= 200) { breath = 200; dir = -1; }
    if (breath <= 10)  { breath = 10;  dir = 1; }
    showHsv(60, 255, (uint8_t)breath);   // 黄色呼吸 = 正在连接
  }
  return WiFi.status() == WL_CONNECTED;
}

void startFallbackAp() {
  Serial.println("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
  Serial.println("[WiFi] ❌ 全部候选网络均失败，切换至 AP 兜底模式...");

  WiFi.disconnect(true, true);
  delay(200);
  WiFi.mode(WIFI_AP);
  bool ok = WiFi.softAP(AP_SSID, AP_PASSWORD);
  delay(200);                        // 等待 softAPIP() 就绪

  Serial.printf("[WiFi] AP启动: %s\n", ok ? "成功" : "失败");
  Serial.printf("[WiFi] AP名称: %s\n", AP_SSID);
  Serial.printf("[WiFi] AP密码: %s\n", AP_PASSWORD);
  Serial.printf("[WiFi] AP IP : %s\n", WiFi.softAPIP().toString().c_str());
  Serial.println("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");

  wifiConnected = false;
  wifiInApMode  = true;
  deviceIP      = WiFi.softAPIP().toString();
  showRgb(255, 0, 0);
  delay(1200);
}

void setupWifi() {
  for (uint8_t i = 0; i < g_wifiCount; i++) {
    wifiCurrentIndex = i;
    if (tryConnectSta(g_wifiList[i].ssid, g_wifiList[i].password, WIFI_CONNECT_TIMEOUT)) {
      wifiConnected = true;
      wifiInApMode  = false;
      deviceIP      = WiFi.localIP().toString();

      Serial.println("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
      Serial.println("[WiFi] ✅ 连接成功!");
      Serial.printf("[WiFi] SSID: %s\n", WiFi.SSID().c_str());
      Serial.printf("[WiFi] IP地址: %s\n", deviceIP.c_str());
      Serial.printf("[WiFi] 网关: %s\n", WiFi.gatewayIP().toString().c_str());
      Serial.printf("[WiFi] 信号强度: %d dBm\n", WiFi.RSSI());
      Serial.println("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");

      showRgb(0, 255, 0);
      delay(800);
      return;
    }
    Serial.printf("[WiFi] ❌ %s 连接失败 (%dms)\n",
                  g_wifiList[i].ssid, (int)WIFI_CONNECT_TIMEOUT);
  }
  startFallbackAp();
}

void beginStaAttempt(uint8_t index) {
  // 允许从 AP 兜底模式手动回到 STA
  if (WiFi.getMode() & WIFI_MODE_AP) WiFi.softAPdisconnect(true);
  wifiInApMode     = false;
  wifiCurrentIndex = index;

  Serial.printf("[WiFi] 尝试连接: %s\n", g_wifiList[index].ssid);
  WiFi.disconnect(true, true);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  WiFi.begin(g_wifiList[index].ssid, g_wifiList[index].password);

  g_wifiTryIndex = index;
  g_wifiTryStart = millis();
  g_wifiTrying   = true;
}

void serviceWifi() {
  static uint32_t lastCheck = 0;

  if (g_wifiTrying) {
    if (WiFi.status() == WL_CONNECTED) {
      g_wifiTrying  = false;
      wifiConnected = true;
      wifiInApMode  = false;
      deviceIP      = WiFi.localIP().toString();
      Serial.printf("[WiFi] ✅ 连接成功: %s / %s\n", WiFi.SSID().c_str(), deviceIP.c_str());
      showRgb(0, 255, 0);
      delay(300);
      refreshLed();
      return;
    }
    if (millis() - g_wifiTryStart < WIFI_CONNECT_TIMEOUT) return;

    uint8_t next = g_wifiTryIndex + 1;
    if (next < g_wifiCount) {
      beginStaAttempt(next);
    } else {
      g_wifiTrying = false;
      lastCheck = millis();
      startFallbackAp();           // 全部失败 -> AP
    }
    return;
  }

  if (wifiInApMode) return;        // AP 兜底模式保持稳定，由网页手动重试

  if (wifiConnected && WiFi.status() == WL_CONNECTED) return;
  if (millis() - lastCheck < WIFI_RETRY_INTERVAL) return;
  lastCheck = millis();

  if (wifiConnected) {
    Serial.println("[WiFi] ⚠️ 连接断开，启动非阻塞重连...");
    wifiConnected = false;
    showHsv(60, 255, 120);
  }
  beginStaAttempt(wifiCurrentIndex);
}

// ==================== WiFi 异步扫描 ====================

void requestWifiScan() {
  if (!g_scanRunning) g_scanRequested = true;
}

void serviceWifiScan() {
  if (g_scanRequested) {
    g_scanRequested = false;
    Serial.println("[WiFi] 开始异步扫描...");
    WiFi.scanDelete();
    if (WiFi.scanNetworks(true, true) == WIFI_SCAN_RUNNING) {
      g_scanRunning = true;
      g_scanStartMs = millis();
    } else {
      Serial.println("[WiFi] ⚠️ 扫描启动失败");
      ws.textAll("{\"type\":\"wifi_scan\",\"data\":[]}");
    }
    return;
  }

  if (!g_scanRunning) return;

  int16_t n = WiFi.scanComplete();
  if (n == WIFI_SCAN_RUNNING) {
    if (millis() - g_scanStartMs > WIFI_SCAN_TIMEOUT) {
      Serial.println("[WiFi] ⚠️ 扫描超时，放弃本次结果");
      g_scanRunning = false;
      WiFi.scanDelete();
      ws.textAll("{\"type\":\"wifi_scan\",\"data\":[]}");
    }
    return;
  }

  g_scanRunning = false;
  if (n < 0) n = 0;                // WIFI_SCAN_FAILED

  JsonDocument doc;
  doc["type"] = "wifi_scan";
  JsonArray arr = doc["data"].to<JsonArray>();

  for (int i = 0; i < n && i < 20; i++) {
    JsonObject net = arr.add<JsonObject>();
    net["ssid"]  = WiFi.SSID(i);
    net["rssi"]  = WiFi.RSSI(i);
    net["ch"]    = WiFi.channel(i);
    net["enc"]   = (WiFi.encryptionType(i) != WIFI_AUTH_OPEN);
    net["bssid"] = WiFi.BSSIDstr(i);
  }
  WiFi.scanDelete();

  String output;
  serializeJson(doc, output);
  ws.textAll(output);
  Serial.printf("[WiFi] 扫描完成, 发现 %d 个网络\n", n);
}

// ==================== 触摸数据 ====================

void sendTouchData(AsyncWebSocketClient *client) {
  JsonDocument doc;
  doc["type"] = "touch";
  JsonArray arr = doc["data"].to<JsonArray>();
  
  for (int i = 0; i < NUM_TOUCH_PINS; i++) {
    JsonObject t = arr.add<JsonObject>();
    t["pin"] = touchPins[i];
    int val = touchRead(touchPins[i]);
    t["value"] = val;
    t["touched"] = (val < TOUCH_THRESHOLD);
  }
  
  String output;
  serializeJson(doc, output);
  client->text(output);
}

// ==================== GPIO 数据 ====================

void sendGpioData(AsyncWebSocketClient *client) {
  JsonDocument doc;
  doc["type"] = "gpio";
  doc["data"]["din"] = digitalRead(GPIO_IN_PIN);
  doc["data"]["adc"] = analogRead(GPIO_ADC_PIN);
  doc["data"]["out"] = gpioOutState;
  doc["data"]["pwm"] = gpioPwmValue;
  
  String output;
  serializeJson(doc, output);
  client->text(output);
}

// ==================== 系统信息 ====================

String getSystemInfoJson() {
  JsonDocument doc;
  doc["type"] = "system";
  JsonObject d = doc["data"].to<JsonObject>();
  
  esp_chip_info_t chip_info;
  esp_chip_info(&chip_info);
  
  d["chip"]   = "ESP32-S3";
  d["cores"]  = chip_info.cores;
  d["cpuFreq"] = getCpuFrequencyMhz();
  
  uint32_t flash_size;
  if (esp_flash_get_size(NULL, &flash_size) == ESP_OK) {
    d["flash"] = String(flash_size / (1024 * 1024)) + "MB";
  } else {
    d["flash"] = "未知";
  }
  
  size_t psramSize = ESP.getPsramSize();
  if (psramSize > 0) {
    d["psram"] = String(psramSize / (1024 * 1024)) + "MB";
  } else {
    d["psram"] = "无";
  }
  
  uint32_t heapFree = ESP.getFreeHeap();
  uint32_t heapTotal = ESP.getHeapSize();
  d["heap"] = String(heapFree / 1024) + "K/" + String(heapTotal / 1024) + "K";
  
  // ESP32-S3 带 TSENS，Core 3.x 的 temperatureRead() 底层调用 IDF temperature_sensor 驱动
  float temp = temperatureRead();
  d["temp"] = isnan(temp) ? "N/A" : (String(temp, 1) + " °C");
  
  unsigned long uptime = millis() / 1000;
  String uptimeStr;
  if (uptime >= 86400) {
    uptimeStr = String(uptime / 86400) + "天" + String((uptime % 86400) / 3600) + "时";
  } else if (uptime >= 3600) {
    uptimeStr = String(uptime / 3600) + "时" + String((uptime % 3600) / 60) + "分";
  } else if (uptime >= 60) {
    uptimeStr = String(uptime / 60) + "分" + String(uptime % 60) + "秒";
  } else {
    uptimeStr = String(uptime) + "秒";
  }
  d["uptime"] = uptimeStr;
  
  if (wifiInApMode) {
    d["wifiMode"] = "AP (兜底模式)";
    d["wifiSSID"] = AP_SSID;
    d["wifiIP"]   = WiFi.softAPIP().toString();
    d["wifiRSSI"] = "N/A";
    d["wifiGW"]   = "N/A";
  } else if (WiFi.status() == WL_CONNECTED) {
    d["wifiMode"] = "STA (已连接)";
    d["wifiSSID"] = WiFi.SSID();
    d["wifiIP"]   = WiFi.localIP().toString();
    d["wifiRSSI"] = String(WiFi.RSSI()) + " dBm";
    d["wifiGW"]   = WiFi.gatewayIP().toString();
  } else {
    d["wifiMode"] = "STA (连接中/已断开)";
    uint8_t idx = (wifiCurrentIndex < g_wifiCount) ? wifiCurrentIndex : 0;
    d["wifiSSID"] = g_wifiList[idx].ssid;
    d["wifiIP"]   = "0.0.0.0";
    d["wifiRSSI"] = "N/A";
    d["wifiGW"]   = "N/A";
  }

  String plan;
  for (uint8_t i = 0; i < g_wifiCount; i++) {
    if (i) plan += " → ";
    plan += g_wifiList[i].ssid;
  }
  plan += " → AP(" + String(AP_SSID) + ")";
  d["wifiPlan"] = plan;
  d["wifiCustom"] = g_hasCustomWifi
                    ? String("★ ") + g_wifiList[0].ssid + " (NVS)"
                    : "无 → 优先 " + String(WIFI_DEFAULT_SSID);

  const char *dst = (g_serialTarget == SERIAL_TARGET_CDC) ? "USB CDC"
                  : (g_serialTarget == SERIAL_TARGET_UART0) ? "UART0" : "双口";
  d["serialInfo"] = String("RX ") + g_rxBytes + " B | TX " + g_txBytes + " B | USB CDC 115200(虚拟) | UART0 "
                    + g_uartBaud + " baud | 目标 " + dst + " | " + (g_serialHex ? "HEX" : "文本");
  
  // ---- v1.4.0 机器可读字段（PC 端优先使用） ----
  d["fwVersion"]      = FW_VERSION;
  d["protoVersion"]   = PROTO_VERSION;
  d["uptimeSec"]      = (uint32_t)uptime;
  d["heapFreeBytes"]  = (uint32_t)heapFree;
  d["heapTotalBytes"] = (uint32_t)heapTotal;
  d["tempC"]          = (double)temp;      // TSENS 不可用时 ArduinoJson 输出 null
  d["rssiDbm"]        = (!wifiInApMode && WiFi.status() == WL_CONNECTED) ? (int)WiFi.RSSI() : -999;
  d["wifiState"]      = wifiInApMode ? "ap"
                      : (WiFi.status() == WL_CONNECTED ? "sta_connected" : "sta_connecting");
  d["uartBaud"]       = (uint32_t)g_uartBaud;
  d["rxBytes"]        = (uint32_t)g_rxBytes;
  d["txBytes"]        = (uint32_t)g_txBytes;
  d["statusState"]    = STATUS_DEFS[g_statusIdx].name;
  d["statusSeq"]      = (uint32_t)g_statusSeq;
  d["statusPrio"]     = (uint8_t)STATUS_DEFS[g_statusIdx].prio;
  d["statusSticky"]   = (bool)STATUS_DEFS[g_statusIdx].sticky;
  d["statusLink"]     = g_statusLink;
  d["statusEngine"]   = g_statusEngine;
  d["statusBrightness"] = (uint8_t)g_statusBrightness;
  d["statusSrc"]      = g_statusSrc;
  d["bindMode"]       = g_bindMode;
  d["boundCount"]     = g_bindCount;
  uint8_t sc2 = 0;
  for (uint8_t i = 0; i < STATUS_SRC_MAX; i++) if (g_sources[i].used) sc2++;
  d["sourceCount"]    = sc2;
  d["mdns"]           = String(DISCOVERY_HOST) + ".local";

  d["mac"] = WiFi.macAddress();
  d["sdk"] = String(ESP.getSdkVersion()).substring(0, 20);
  
  int resetReason = esp_reset_reason();
  switch (resetReason) {
    case ESP_RST_POWERON:  d["reset"] = "上电"; break;
    case ESP_RST_SW:       d["reset"] = "软件重启"; break;
    case ESP_RST_PANIC:    d["reset"] = "异常"; break;
    case ESP_RST_INT_WDT:  d["reset"] = "看门狗"; break;
    case ESP_RST_DEEPSLEEP:d["reset"] = "深度睡眠"; break;
    case ESP_RST_BROWNOUT: d["reset"] = "欠压"; break;
    default:               d["reset"] = "未知(" + String(resetReason) + ")"; break;
  }
  
  String output;
  serializeJson(doc, output);
  return output;
}

// ==================== AI 推理 ====================

void initAIModel() {
  randomSeed(analogRead(0));
  for (int i = 0; i < AI_INPUT_SIZE; i++) {
    for (int j = 0; j < AI_HIDDEN_SIZE; j++) {
      aiWeights1[i][j] = (random(-1000, 1000) / 1000.0);
    }
    aiBias1[i % AI_HIDDEN_SIZE] = (random(-500, 500) / 1000.0);
  }
  for (int i = 0; i < AI_HIDDEN_SIZE; i++) {
    for (int j = 0; j < AI_OUTPUT_SIZE; j++) {
      aiWeights2[i][j] = (random(-1000, 1000) / 1000.0);
    }
    aiBias2[i % AI_OUTPUT_SIZE] = (random(-500, 500) / 1000.0);
  }
  aiModelLoaded = true;
  Serial.println("[AI] 模型已加载 (随机权重演示模式)");
}

void doAIInference(AsyncWebSocketClient *client, JsonDocument &doc) {
  unsigned long startTime = micros();
  
  float input[AI_INPUT_SIZE] = {0};
  JsonArray features = doc["val"].as<JsonArray>();
  int idx = 0;
  for (JsonVariant v : features) {
    if (idx < AI_INPUT_SIZE) {
      input[idx++] = v.as<float>();
    }
  }
  
  // 隐藏层: ReLU
  float hidden[AI_HIDDEN_SIZE] = {0};
  for (int j = 0; j < AI_HIDDEN_SIZE; j++) {
    float sum = aiBias1[j];
    for (int i = 0; i < AI_INPUT_SIZE; i++) {
      sum += input[i] * aiWeights1[i][j];
    }
    hidden[j] = (sum > 0) ? sum : 0;
  }
  
  // 输出层: Softmax
  float output[AI_OUTPUT_SIZE] = {0};
  float maxVal = -1e9;
  for (int j = 0; j < AI_OUTPUT_SIZE; j++) {
    float sum = aiBias2[j];
    for (int i = 0; i < AI_HIDDEN_SIZE; i++) {
      sum += hidden[i] * aiWeights2[i][j];
    }
    output[j] = sum;
    if (sum > maxVal) maxVal = sum;
  }
  
  float expSum = 0;
  for (int j = 0; j < AI_OUTPUT_SIZE; j++) {
    output[j] = exp(output[j] - maxVal);
    expSum += output[j];
  }
  for (int j = 0; j < AI_OUTPUT_SIZE; j++) {
    output[j] /= expSum;
  }
  
  unsigned long elapsed = (micros() - startTime) / 1000;
  if (elapsed == 0) elapsed = 1;
  
  JsonDocument result;
  result["type"] = "ai_result";
  result["data"]["time_ms"] = elapsed;
  
  JsonArray labels = result["data"]["labels"].to<JsonArray>();
  for (int i = 0; i < AI_OUTPUT_SIZE; i++) {
    labels.add(aiLabels[i]);
  }
  
  JsonArray probs = result["data"]["probs"].to<JsonArray>();
  for (int i = 0; i < AI_OUTPUT_SIZE; i++) {
    probs.add(output[i]);
  }
  
  String outputStr;
  serializeJson(result, outputStr);
  client->text(outputStr);
  
  Serial.printf("[AI] 推理完成, 耗时 %lums\n", elapsed);
}

// ==================== AI 状态灯引擎 (ai-status/1) ====================

// ---- 状态灯亮度持久化（ESP-IDF NVS: status_cfg / brightness） ----
void loadStatusBrightness() {
  if (!nvsInit()) return;
  nvs_handle_t h;
  if (nvs_open(STATUS_NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) return;
  uint8_t v = STATUS_BRIGHTNESS_DEFAULT;
  if (nvs_get_u8(h, STATUS_NVS_KEY, &v) == ESP_OK) g_statusBrightness = v;
  nvs_close(h);
  Serial.printf("[状态灯] NVS 亮度恢复: %u (%u%%)\n",
                (unsigned)g_statusBrightness,
                (unsigned)((uint32_t)g_statusBrightness * 100 / 255));
}

bool setStatusBrightness(uint8_t value) {
  g_statusBrightness = value;                 // 立即生效，即使 NVS 写入失败
  if (!nvsInit()) return false;
  nvs_handle_t h;
  if (nvs_open(STATUS_NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) return false;
  esp_err_t e1 = nvs_set_u8(h, STATUS_NVS_KEY, value);
  esp_err_t e2 = (e1 == ESP_OK) ? nvs_commit(h) : e1;
  nvs_close(h);
  return e2 == ESP_OK;
}

// ---- v1.5.0-fx：每个状态的颜色/动效配置（NVS blob，开机恢复） ----
static uint8_t mixRgb(uint8_t a, uint8_t b, uint8_t t) {
  return (uint8_t)(((uint32_t)a * (255 - t) + (uint32_t)b * t) / 255);
}

static bool parseHexColor(const char *hex, uint8_t &r, uint8_t &g, uint8_t &b) {
  if (!hex || hex[0] != '#' || strlen(hex) != 7) return false;
  char *end = nullptr;
  uint32_t v = strtoul(hex + 1, &end, 16);
  if (!end || *end) return false;
  r = (v >> 16) & 0xFF; g = (v >> 8) & 0xFF; b = v & 0xFF;
  return true;
}

static String colorToHex(uint8_t r, uint8_t g, uint8_t b) {
  char buf[8];
  snprintf(buf, sizeof(buf), "#%02X%02X%02X", r, g, b);
  return String(buf);
}

static void sanitizeStatusFx() {
  for (size_t i = 0; i < STATUS_COUNT; i++) {
    StatusFx &f = g_statusFx[i];
    if (f.fx >= FX_COUNT) f.fx = FX_SOLID;
    if (f.level > 100) f.level = 100;
    if (f.periodMs > 10000) f.periodMs = 10000;
  }
}

void loadStatusFx() {
  memcpy(g_statusFx, STATUS_FX_DEFAULT, sizeof(g_statusFx));
  if (!nvsInit()) return;
  nvs_handle_t h;
  if (nvs_open(STATUS_NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) return;
  StatusFx tmp[STATUS_COUNT];
  size_t len = sizeof(tmp);
  if (nvs_get_blob(h, STATUS_FX_KEY, tmp, &len) == ESP_OK && len == sizeof(tmp)) {
    memcpy(g_statusFx, tmp, sizeof(g_statusFx));
    sanitizeStatusFx();
    Serial.println("[状态灯] NVS 灯效配置已恢复");
  } else {
    Serial.println("[状态灯] 使用默认灯效（工作态暗 / 交互态亮）");
  }
  nvs_close(h);
}

bool saveStatusFx() {
  if (!nvsInit()) return false;
  nvs_handle_t h;
  if (nvs_open(STATUS_NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) return false;
  esp_err_t e1 = nvs_set_blob(h, STATUS_FX_KEY, g_statusFx, sizeof(g_statusFx));
  esp_err_t e2 = (e1 == ESP_OK) ? nvs_commit(h) : e1;
  nvs_close(h);
  return e2 == ESP_OK;
}

bool resetStatusFx() {
  memcpy(g_statusFx, STATUS_FX_DEFAULT, sizeof(g_statusFx));
  return saveStatusFx();
}

String getStatusFxJson() {
  JsonDocument doc;
  doc["ok"]        = true;
  doc["version"]   = STATUS_FX_VER;
  doc["brightness"] = g_statusBrightness;
  JsonArray names = doc["fxNames"].to<JsonArray>();
  for (uint8_t i = 0; i < FX_COUNT; i++) names.add(FX_NAMES[i]);
  JsonArray arr = doc["states"].to<JsonArray>();
  for (size_t i = 0; i < STATUS_COUNT; i++) {
    const StatusDef &d = STATUS_DEFS[i];
    const StatusFx &f = g_statusFx[i];
    JsonObject o = arr.add<JsonObject>();
    o["key"]      = d.name;
    o["label"]    = d.label;
    o["prio"]     = d.prio;
    o["sticky"]   = d.sticky;
    o["ttlMs"]    = d.ttlMs;
    o["color1"]   = colorToHex(f.r1, f.g1, f.b1);
    o["color2"]   = colorToHex(f.r2, f.g2, f.b2);
    o["fx"]       = f.fx;
    o["fxName"]   = FX_NAMES[f.fx < FX_COUNT ? f.fx : 0];
    o["level"]    = f.level;
    o["periodMs"] = f.periodMs;
  }
  String out; serializeJson(doc, out); return out;
}

bool setStatusFxItem(JsonObject o, String &reason) {
  int idx = findStatusIndex(o["key"].as<const char*>());
  if (idx < 0) { reason = "unknown_state"; return false; }
  StatusFx f = g_statusFx[idx];
  const char *c1 = o["c1"].as<const char*>();
  const char *c2 = o["c2"].as<const char*>();
  if (c1 && !parseHexColor(c1, f.r1, f.g1, f.b1)) { reason = "bad_color1"; return false; }
  if (c2 && !parseHexColor(c2, f.r2, f.g2, f.b2)) { reason = "bad_color2"; return false; }
  if (!o["fx"].isNull()) {
    int fx = o["fx"].as<int>();
    if (fx < 0 || fx >= FX_COUNT) { reason = "bad_fx"; return false; }
    f.fx = (uint8_t)fx;
  }
  if (!o["level"].isNull()) {
    int lv = o["level"].as<int>();
    if (lv < 0 || lv > 100) { reason = "bad_level"; return false; }
    f.level = (uint8_t)lv;
  }
  if (!o["periodMs"].isNull()) {
    int p = o["periodMs"].as<int>();
    if (p < 0 || p > 10000) { reason = "bad_period"; return false; }
    f.periodMs = (uint16_t)p;
  }
  g_statusFx[idx] = f;                 // 本次立即生效
  if (!saveStatusFx()) { reason = "nvs_write_failed"; return false; }
  reason = "";
  return true;
}

int findStatusIndex(const char *state) {
  if (!state) return -1;
  for (size_t i = 0; i < STATUS_COUNT; i++) {
    if (strcmp(state, STATUS_DEFS[i].name) == 0) return (int)i;
  }
  return -1;
}

void sendJsonResponse(AsyncWebServerRequest *request, int code, const String &body) {
  AsyncWebServerResponse *r = request->beginResponse(code, "application/json; charset=utf-8", body);
  r->addHeader("Access-Control-Allow-Origin", "*");
  r->addHeader("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
  r->addHeader("Access-Control-Allow-Headers", "Content-Type");
  request->send(r);
}

String statusAckJson(bool ok, const String &reason, const char *reqId) {
  JsonDocument doc;
  doc["ok"] = ok;
  doc["state"] = STATUS_DEFS[g_statusIdx].name;
  doc["seq"] = (uint32_t)g_statusSeq;
  if (!ok) doc["reason"] = reason;
  else doc["prio"] = STATUS_DEFS[g_statusIdx].prio;
  if (reqId && reqId[0]) doc["reqId"] = reqId;
  String out; serializeJson(doc, out); return out;
}

String statusWsAckJson(bool ok, const String &reason, const char *reqId) {
  JsonDocument doc;
  doc["type"] = "status_ack";
  doc["data"]["ok"] = ok;
  if (!ok) doc["data"]["reason"] = reason;
  if (reqId && reqId[0]) doc["reqId"] = reqId;
  String out; serializeJson(doc, out); return out;
}

void publishStatus(const char *reqId, bool unicastOnly, AsyncWebSocketClient *client) {
  JsonDocument doc;
  doc["type"] = "status";
  JsonObject d = doc["data"].to<JsonObject>();
  d["state"]    = STATUS_DEFS[g_statusIdx].name;
  d["seq"]      = (uint32_t)g_statusSeq;
  d["prio"]     = (uint8_t)STATUS_DEFS[g_statusIdx].prio;
  d["sticky"]   = (bool)STATUS_DEFS[g_statusIdx].sticky;
  d["since_ms"] = (uint32_t)(millis() - g_statusSinceMs);
  d["link"]     = g_statusLink;
  d["msg"]      = g_statusMsg;
  d["src"]      = g_statusSrc;
  d["bindMode"] = g_bindMode;
  if (reqId && reqId[0]) doc["reqId"] = reqId;
  String out; serializeJson(doc, out);
  if (unicastOnly && client) client->text(out);
  else ws.textAll(out);
}

bool applyStatus(const char *state, uint32_t seq, uint32_t ttl,
                 const char *msg, bool force, const char *reqId, String &reason) {
  // v1.4.x 兼容入口：不带 src 的帧按匿名来源处理，语义交给多来源引擎
  return applyStatusSrc(state, seq, ttl, msg, force, reqId, nullptr, reason);
}

void clearStatusToIdle() {
  g_statusIdx = ST_IDLE;
  g_statusSeq++;
  g_statusSinceMs = g_statusLastFrameMs = millis();
  g_statusTtlMs = 0;
  g_statusMsg = "";
  publishStatus(nullptr, false, nullptr);
}

void checkStatusTimeout(uint32_t now) {
  if (!g_statusEngine) return;
  bool sticky  = STATUS_DEFS[g_statusIdx].sticky;
  bool changed = false;

  if (!sticky && g_statusTtlMs > 0 && (now - g_statusSinceMs) >= g_statusTtlMs) {
    g_statusIdx = ST_IDLE;
    g_statusTtlMs = 0;
    g_statusSinceMs = now;
    changed = true;
  }
  if ((now - g_statusLastFrameMs) >= STATUS_OFFLINE_MS) {
    if (!sticky && g_statusIdx != ST_OFFLINE) {
      g_statusIdx = ST_OFFLINE;
      g_statusTtlMs = 0;
      g_statusSinceMs = now;
      changed = true;
    }
    if (g_statusLink) { g_statusLink = false; changed = true; }
  }
  if (changed) publishStatus(nullptr, false, nullptr);
}

void renderStatus(uint32_t now) {
  if (!ledOn) { showRgb(0, 0, 0); return; }
  const StatusFx &f = g_statusFx[g_statusIdx];
  uint16_t period = (f.periodMs > 0) ? f.periodMs : 1000;
  uint32_t elapsed = now - g_statusSinceMs;
  uint32_t phase = elapsed % period;
  uint32_t half = (period > 1) ? period / 2 : 1;
  uint8_t scale = 255;    // 动效明暗系数
  uint8_t mix   = 0;      // 主色 -> 辅色插值

  switch (f.fx) {
    case FX_BREATHE: {                       // 单色明暗呼吸
      uint32_t tri = (phase < half) ? phase : (period - phase);
      scale = (uint8_t)((uint32_t)255 * tri / half);
      if (scale < 20) scale = 20;
      break;
    }
    case FX_BLINK:                           // 闪烁：亮 / 暗
      scale = (phase < half) ? 255 : 20;
      break;
    case FX_DOUBLE_BLINK: {                  // 双闪
      uint32_t p = elapsed % period;
      scale = (p < period / 6 || (p >= period / 3 && p < period / 2)) ? 255 : 20;
      break;
    }
    case FX_GRADIENT: {                      // 双色渐变呼吸：颜色来回过渡 + 轻微明暗
      uint32_t tri = (phase < half) ? phase : (period - phase);
      mix   = (uint8_t)((uint32_t)255 * tri / half);
      scale = (uint8_t)(110 + (uint32_t)145 * tri / half);
      break;
    }
    case FX_FLOW: {                          // 双色渐变流动：颜色连续来回变化
      uint32_t tri = (phase < half) ? phase : (period - phase);
      mix   = (uint8_t)((uint32_t)255 * tri / half);
      scale = 255;
      break;
    }
    default: break;                          // FX_SOLID 常亮
  }

  uint8_t r = mixRgb(f.r1, f.r2, mix);
  uint8_t g = mixRgb(f.g1, f.g2, mix);
  uint8_t b = mixRgb(f.b1, f.b2, mix);
  // 三级亮度叠加：动效明暗 × 该状态配置亮度 × 全局状态灯亮度
  uint32_t k = (uint32_t)scale * f.level * g_statusBrightness / (255 * 100);
  showRgb((uint8_t)((uint32_t)r * k / 255),
          (uint8_t)((uint32_t)g * k / 255),
          (uint8_t)((uint32_t)b * k / 255));
}

String getStatusJson() {
  JsonDocument doc;
  doc["state"]    = STATUS_DEFS[g_statusIdx].name;
  doc["seq"]      = (uint32_t)g_statusSeq;
  doc["prio"]     = (uint8_t)STATUS_DEFS[g_statusIdx].prio;
  doc["sticky"]   = (bool)STATUS_DEFS[g_statusIdx].sticky;
  doc["since_ms"] = (uint32_t)(millis() - g_statusSinceMs);
  doc["link"]     = g_statusLink;
  doc["msg"]      = g_statusMsg;
  doc["engine"]   = g_statusEngine;
  doc["brightness"] = g_statusBrightness;
  doc["src"]        = g_statusSrc;
  doc["bindMode"]   = g_bindMode;
  doc["boundCount"] = g_bindCount;
  uint8_t sc = 0;
  for (uint8_t i = 0; i < STATUS_SRC_MAX; i++) if (g_sources[i].used) sc++;
  doc["sourceCount"] = sc;
  doc["uptimeSec"] = (uint32_t)(millis() / 1000);
  String out; serializeJson(doc, out); return out;
}

String getHelloJson() {
  JsonDocument doc;
  doc["proto"] = PROTO_VERSION;
  doc["fw"]    = FW_VERSION;
  doc["chip"]  = "ESP32-S3";
  doc["mac"]   = WiFi.macAddress();
  doc["ip"]    = WiFi.localIP().toString();
  doc["wifiState"] = wifiInApMode ? "ap"
                    : (WiFi.status() == WL_CONNECTED ? "sta_connected" : "sta_connecting");
  JsonArray cap = doc["cap"].to<JsonArray>();
  cap.add("ws"); cap.add("http"); cap.add("serial"); cap.add("status");
  cap.add("sources"); cap.add("bind"); cap.add("mdns"); cap.add("udp");
  doc["statusEngine"] = g_statusEngine;
  doc["statusBrightness"] = g_statusBrightness;
  doc["bindMode"]   = g_bindMode;
  doc["mdns"]       = String(DISCOVERY_HOST) + ".local";
  doc["udpPort"]    = DISCOVERY_UDP_PORT;
  doc["sourceField"] = SOURCE_FIELD;
  // v1.5.0 BUGFIX：发现广播运行诊断，便于区分「设备没发」与「PC/网络没收」
  doc["udpBeaconCount"] = g_udpBeaconCount;
  doc["udpBeaconOk"]    = g_udpBeaconOk;
  doc["udpBeaconFail"]  = g_udpBeaconFail;
  doc["udpLastErrno"]   = g_udpLastErrno;
  doc["broadcastIP"]    = g_udpBroadcastIp;
  String out; serializeJson(doc, out); return out;
}

bool handleSerialProtocolLine(const String &raw, Stream &port, const char *tag) {
  (void)tag;
  String line = raw;
  line.trim();
  if (line.length() < 2 || line.charAt(0) != '{') return false;
  if (line.length() > STATUS_SERIAL_MAX) {
    port.println("{\"t\":\"ack\",\"v\":1,\"ok\":false,\"reason\":\"payload_too_large\"}");
    return true;
  }
  JsonDocument doc;
  if (deserializeJson(doc, line)) return false;     // 普通串口数据，交回桥接
  const char *t = doc["t"];
  if (!t) return false;

  const char *rid = doc["reqId"].as<const char*>();
  if (!rid) rid = "";
  // v1.5.0：所有帧统一从 SOURCE_FIELD("src") 取来源 ID
  const char *src = doc[SOURCE_FIELD].as<const char*>();

  if (strcmp(t, "ping") == 0) {
    JsonDocument p;
    p["t"] = "pong"; p["v"] = 1;
    p["fw"] = FW_VERSION; p["proto"] = PROTO_VERSION;
    p["mac"] = WiFi.macAddress(); p["ip"] = deviceIP;
    p["bindMode"] = g_bindMode; p["sourceField"] = SOURCE_FIELD;
    p["mdns"] = String(DISCOVERY_HOST) + ".local";
    String out; serializeJson(p, out);
    port.println(out);
    return true;
  }
  if (strcmp(t, "get") == 0) {
    String snapshot = getStatusJson();
    JsonDocument d;
    deserializeJson(d, snapshot);
    d["t"] = "status"; d["v"] = 1;
    String out; serializeJson(d, out);
    port.println(out);
    return true;
  }
  if (strcmp(t, "sources") == 0) {
    JsonDocument s;
    s["t"] = "sources"; s["v"] = 1;
    fillSources(s["data"].to<JsonObject>());
    String out; serializeJson(s, out);
    port.println(out);
    return true;
  }
  if (strcmp(t, "bind") == 0) {
    bool on = (doc["on"].as<int>() != 0);
    bool ok = (src && src[0]) ? setSourceBind(src, on) : false;
    recomputeAggregate(millis());
    publishSources();
    JsonDocument a;
    a["t"] = "ack"; a["v"] = 1; a["ok"] = ok;
    if (!ok) a["reason"] = "invalid_src";
    if (rid[0]) a["reqId"] = rid;
    String out; serializeJson(a, out);
    port.println(out);
    return true;
  }
  if (strcmp(t, "bind_mode") == 0) {
    g_bindMode = (doc["val"].as<int>() != 0) ? 1 : 0;
    saveBindConfig();
    recomputeAggregate(millis());
    publishSources();
    port.println("{\"t\":\"ack\",\"v\":1,\"ok\":true}");
    return true;
  }
  if (strcmp(t, "clear") == 0) {
    clearAllSources();
    port.println("{\"t\":\"ack\",\"v\":1,\"ok\":true}");
    return true;
  }
  if (strcmp(t, "status") == 0 || strcmp(t, "heartbeat") == 0) {
    const char *st  = doc["state"].as<const char*>();
    const char *msg = doc["msg"].as<const char*>();
    uint32_t seq    = doc["seq"].as<uint32_t>();
    uint32_t ttl    = doc["ttl"].as<uint32_t>();
    bool force      = (doc["force"].as<int>() != 0);

    if (!doc["brightness"].isNull()) {
      setStatusBrightness((uint8_t)constrain(doc["brightness"].as<int>(), 0, 255));
    }

    if (strcmp(t, "heartbeat") == 0) {
      touchSource(src, st);
      port.println("{\"t\":\"ack\",\"v\":1,\"ok\":true}");
      return true;
    }

    String reason;
    bool ok = applyStatusSrc(st, seq, ttl, msg, force, rid, src, reason);
    JsonDocument ack;
    ack["t"] = "ack"; ack["v"] = 1; ack["ok"] = ok;
    ack["seq"] = (uint32_t)g_statusSeq;
    ack["state"] = STATUS_DEFS[g_statusIdx].name;
    if (!ok) ack["reason"] = reason;
    if (rid[0]) ack["reqId"] = rid;
    String out; serializeJson(ack, out);
    port.println(out);
    return true;
  }
  return false;                                  // 未知 t：当作普通串口数据
}

// ==================== 辅助函数 ====================

void broadcastUSB(String msg) {
  JsonDocument doc;
  doc["type"] = "usb_recv";
  doc["data"] = msg;
  String output;
  serializeJson(doc, output);
  ws.textAll(output);
}

// ==================== v1.5.0 多来源 / 绑定白名单 / 发现广播 ====================
// 设计：每台 PC（或每个 AI 窗口）用 src 标识，独立维护状态、seq、优先级与 TTL；
//       「绑定」只决定该来源是否参与唯一的 WS2812 聚合；未绑定来源仍显示在网页上供勾选。

void normalizeSrc(const char *raw, char *out, size_t outLen) {
  if (!outLen) return;
  if (!raw || !raw[0]) {
    strncpy(out, STATUS_ANON_SRC, outLen - 1);
    out[outLen - 1] = 0;
    return;
  }
  size_t n = 0;
  while (raw[n] && n < outLen - 1) {
    char c = raw[n];
    if (c == ',' || (unsigned char)c < 0x20) c = '_';   // 白名单以逗号分隔
    out[n++] = c;
  }
  out[n] = 0;
}

int findSourceSlot(const char *src, bool create) {
  char key[STATUS_SRC_LEN];
  normalizeSrc(src, key, sizeof(key));
  for (int i = 0; i < STATUS_SRC_MAX; i++) {
    if (g_sources[i].used && strcmp(g_sources[i].src, key) == 0) return i;
  }
  if (!create) return -1;
  int victim = 0;
  uint32_t oldest = 0xFFFFFFFF;
  for (int i = 0; i < STATUS_SRC_MAX; i++) {
    if (!g_sources[i].used) { victim = i; oldest = 0; break; }
    if (g_sources[i].lastSeenMs < oldest) { oldest = g_sources[i].lastSeenMs; victim = i; }
  }
  SourceSlot &s = g_sources[victim];
  memset(&s, 0, sizeof(s));
  strncpy(s.src, key, sizeof(s.src) - 1);
  s.stateIdx = ST_IDLE;
  s.sinceMs = s.lastSeenMs = millis();
  s.used = true;
  return victim;
}

bool isSourceBound(const char *src) {
  if (g_bindMode == 0) return true;                  // 全部接受（v1.4.x 行为）
  char key[STATUS_SRC_LEN];
  normalizeSrc(src, key, sizeof(key));
  for (uint8_t i = 0; i < g_bindCount; i++) {
    if (strcmp(g_bindList[i], key) == 0) return true;
  }
  return false;
}

bool saveBindConfig() {
  if (!nvsInit()) return false;
  nvs_handle_t h;
  if (nvs_open(STATUS_NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) return false;
  char buf[STATUS_BIND_MAX * STATUS_SRC_LEN];
  buf[0] = 0;
  for (uint8_t i = 0; i < g_bindCount; i++) {
    if (i) strncat(buf, ",", sizeof(buf) - strlen(buf) - 1);
    strncat(buf, g_bindList[i], sizeof(buf) - strlen(buf) - 1);
  }
  esp_err_t e1 = nvs_set_u8(h, STATUS_BIND_MODE_KEY, g_bindMode);
  esp_err_t e2 = nvs_set_str(h, STATUS_BIND_LIST_KEY, buf);
  esp_err_t e3 = nvs_commit(h);
  nvs_close(h);
  return e1 == ESP_OK && e2 == ESP_OK && e3 == ESP_OK;
}

void loadBindConfig() {
  if (!nvsInit()) return;
  nvs_handle_t h;
  if (nvs_open(STATUS_NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) return;
  nvs_get_u8(h, STATUS_BIND_MODE_KEY, &g_bindMode);
  if (g_bindMode > 1) g_bindMode = 0;
  char buf[STATUS_BIND_MAX * STATUS_SRC_LEN];
  size_t len = sizeof(buf);
  if (nvs_get_str(h, STATUS_BIND_LIST_KEY, buf, &len) == ESP_OK && buf[0]) {
    g_bindCount = 0;
    char *save = nullptr;
    char *tok = strtok_r(buf, ",", &save);
    while (tok && g_bindCount < STATUS_BIND_MAX) {
      normalizeSrc(tok, g_bindList[g_bindCount], STATUS_SRC_LEN);
      if (g_bindList[g_bindCount][0]) g_bindCount++;
      tok = strtok_r(nullptr, ",", &save);
    }
  }
  nvs_close(h);
  Serial.printf("[状态灯] 绑定配置恢复: mode=%u (%s), 白名单 %u 台\n",
                (unsigned)g_bindMode, g_bindMode ? "仅绑定" : "全部接受",
                (unsigned)g_bindCount);
}

bool setSourceBind(const char *src, bool on) {
  char key[STATUS_SRC_LEN];
  normalizeSrc(src, key, sizeof(key));
  int found = -1;
  for (uint8_t i = 0; i < g_bindCount; i++) {
    if (strcmp(g_bindList[i], key) == 0) { found = i; break; }
  }
  if (on) {
    if (found >= 0) return true;
    if (g_bindCount >= STATUS_BIND_MAX) return false;
    strncpy(g_bindList[g_bindCount], key, STATUS_SRC_LEN - 1);
    g_bindList[g_bindCount][STATUS_SRC_LEN - 1] = 0;
    g_bindCount++;
  } else if (found >= 0) {
    for (uint8_t i = found; i + 1 < g_bindCount; i++) {
      strncpy(g_bindList[i], g_bindList[i + 1], STATUS_SRC_LEN);
    }
    g_bindCount--;
    g_bindList[g_bindCount][0] = 0;
  }
  saveBindConfig();
  return true;
}

void clearBindList() {
  g_bindCount = 0;
  saveBindConfig();
}

void touchSource(const char *src, const char *state) {
  int slot = findSourceSlot(src, true);
  if (slot < 0) return;
  SourceSlot &s = g_sources[slot];
  uint32_t now = millis();
  s.lastSeenMs = now;
  if (state && state[0]) {
    int idx = findStatusIndex(state);
    if (idx >= 0 && (uint8_t)idx != s.stateIdx) {
      s.stateIdx = (uint8_t)idx;
      s.sinceMs = now;
      s.ttlMs = STATUS_DEFS[idx].ttlMs;
    }
  }
  recomputeAggregate(now);
  publishSources();
}

bool applyStatusSrc(const char *state, uint32_t seq, uint32_t ttl,
                    const char *msg, bool force, const char *reqId,
                    const char *src, String &reason) {
  if (!g_statusEngine) { reason = "disabled"; return false; }
  int idx = findStatusIndex(state);
  if (idx < 0) { reason = "invalid_state"; return false; }

  int slot = findSourceSlot(src, true);
  if (slot < 0) { reason = "too_many_sources"; return false; }
  SourceSlot &s = g_sources[slot];

  // seq 与优先级都按「该来源自己的状态」判断，多台 PC 互不压制、互不混用
  if (seq != 0 && seq <= s.seq) { reason = "invalid_seq"; return false; }
  uint8_t newPrio = STATUS_DEFS[idx].prio;
  uint8_t curPrio = STATUS_DEFS[s.stateIdx].prio;
  if (!force && newPrio < curPrio) { reason = "lower_priority"; return false; }

  uint32_t now = millis();
  s.stateIdx = (uint8_t)idx;
  s.seq = (seq != 0) ? seq : s.seq + 1;
  s.sinceMs = s.lastSeenMs = now;
  s.ttlMs = (ttl > 0) ? ttl : STATUS_DEFS[idx].ttlMs;
  if (msg && msg[0]) {
    strncpy(s.msg, msg, STATUS_MSG_MAX - 1);
    s.msg[STATUS_MSG_MAX - 1] = 0;
  } else {
    s.msg[0] = 0;
  }

  bool bound = isSourceBound(s.src);
  recomputeAggregate(now);
  publishSources();
  if (!bound) { reason = "source_not_bound"; return false; }
  reason = "";
  return true;
}

void recomputeAggregate(uint32_t now) {
  int best = -1;
  uint8_t bestPrio = 0;
  uint32_t bestSince = 0;
  for (int i = 0; i < STATUS_SRC_MAX; i++) {
    SourceSlot &s = g_sources[i];
    if (!s.used || !isSourceBound(s.src)) continue;
    if ((now - s.lastSeenMs) >= STATUS_OFFLINE_MS) continue;
    uint8_t p = STATUS_DEFS[s.stateIdx].prio;
    // 混合显示：所有已绑定来源中优先级最高者驱动唯一的灯，同级取最近变化
    if (best < 0 || p > bestPrio || (p == bestPrio && s.sinceMs > bestSince)) {
      best = i; bestPrio = p; bestSince = s.sinceMs;
    }
  }

  uint8_t  newIdx;
  uint32_t newSeq, newSince, newTtl, newLast;
  bool     newLink;
  String   newMsg;
  char     newSrc[STATUS_SRC_LEN];

  if (best < 0) {
    // BUGFIX v1.5.0：无已绑定在线来源时必须保持 sinceMs 不变。
    // 若设成 now，每轮 loop 的 changed 判定都为真，会每毫秒广播一次 status，
    // 触发 "ERROR: Too many messages queued" 刷屏并可能 panic 重启。
    newIdx = ST_OFFLINE; newSeq = g_statusSeq; newSince = g_statusSinceMs;
    newTtl = 0; newLast = 0; newLink = false; newMsg = "";
    strncpy(newSrc, STATUS_ANON_SRC, sizeof(newSrc) - 1);
    newSrc[sizeof(newSrc) - 1] = 0;
  } else {
    SourceSlot &s = g_sources[best];
    newIdx = s.stateIdx;
    newSeq = (s.seq > g_statusSeq) ? s.seq : g_statusSeq;
    newSince = s.sinceMs; newTtl = s.ttlMs; newLast = s.lastSeenMs;
    newLink = true; newMsg = String(s.msg);
    strncpy(newSrc, s.src, sizeof(newSrc) - 1);
    newSrc[sizeof(newSrc) - 1] = 0;
  }

  bool changed = (g_statusIdx != newIdx) || (g_statusTtlMs != newTtl)
              || (g_statusLink != newLink) || (g_statusMsg != newMsg)
              || (strcmp(g_statusSrc, newSrc) != 0)
              || (g_statusSinceMs != newSince);
  g_statusIdx = newIdx;
  g_statusSeq = newSeq;
  g_statusSinceMs = newSince;
  g_statusTtlMs = newTtl;
  g_statusLastFrameMs = newLast;
  g_statusLink = newLink;
  g_statusMsg = newMsg;
  strncpy(g_statusSrc, newSrc, sizeof(g_statusSrc) - 1);
  g_statusSrc[sizeof(g_statusSrc) - 1] = 0;

  if (changed) publishStatus(nullptr, false, nullptr);
}

void checkSourcesTimeout(uint32_t now) {
  if (!g_statusEngine) return;
  for (int i = 0; i < STATUS_SRC_MAX; i++) {
    SourceSlot &s = g_sources[i];
    if (!s.used) continue;
    if (!STATUS_DEFS[s.stateIdx].sticky && s.ttlMs > 0
        && (now - s.sinceMs) >= s.ttlMs) {
      s.stateIdx = ST_IDLE;
      s.ttlMs = 0;
      s.sinceMs = now;          // done 等 TTL 状态按来源独立回落 idle
    }
  }
  recomputeAggregate(now);
  if (now - g_lastSourcesPushMs >= 3000) {
    g_lastSourcesPushMs = now;
    publishSources();          // 网页每 3s 刷新来源列表（含在线/离线与勾选状态）
  }
}

void clearAllSources() {
  for (int i = 0; i < STATUS_SRC_MAX; i++) {
    if (!g_sources[i].used) continue;
    g_sources[i].stateIdx = ST_IDLE;
    g_sources[i].seq++;
    g_sources[i].sinceMs = millis();
    g_sources[i].ttlMs = 0;
    g_sources[i].msg[0] = 0;
  }
  recomputeAggregate(millis());
  publishSources();
}

void fillSources(JsonObject d) {
  uint32_t now = millis();
  d["mode"] = g_bindMode;
  d["boundCount"] = g_bindCount;
  d["sourceField"] = SOURCE_FIELD;
  JsonArray binds = d["binds"].to<JsonArray>();
  for (uint8_t i = 0; i < g_bindCount; i++) binds.add(g_bindList[i]);
  JsonArray arr = d["sources"].to<JsonArray>();
  uint8_t count = 0;
  for (int i = 0; i < STATUS_SRC_MAX; i++) {
    SourceSlot &s = g_sources[i];
    if (!s.used) continue;
    count++;
    JsonObject o = arr.add<JsonObject>();
    o["src"] = s.src;
    o["state"] = STATUS_DEFS[s.stateIdx].name;
    o["seq"] = (uint32_t)s.seq;
    o["msg"] = s.msg;
    o["age_ms"] = (uint32_t)(now - s.lastSeenMs);
    o["online"] = ((now - s.lastSeenMs) < STATUS_OFFLINE_MS);
    o["bound"] = isSourceBound(s.src);
    o["sticky"] = (bool)STATUS_DEFS[s.stateIdx].sticky;
  }
  d["sourceCount"] = count;
}

String getSourcesJson() {
  JsonDocument doc;
  fillSources(doc.to<JsonObject>());
  String out; serializeJson(doc, out); return out;
}

String sourcesWsJson() {
  JsonDocument doc;
  doc["type"] = "sources";
  fillSources(doc["data"].to<JsonObject>());
  String out; serializeJson(doc, out); return out;
}

void publishSources() {
  // BUGFIX v1.5.0：无客户端不排队；高频变更合并为最多 2 次/秒，防止队列溢出
  if (ws.count() == 0) return;
  static uint32_t lastPush = 0;
  uint32_t now = millis();
  if (lastPush != 0 && (now - lastPush) < 500) return;
  lastPush = now;
  ws.textAll(sourcesWsJson());
}

String getDiscoveryDiagJson() {
  JsonDocument d;
  d["ok"]          = (g_udpBeaconFail == 0);
  d["udpPort"]     = DISCOVERY_UDP_PORT;
  d["beaconCount"] = g_udpBeaconCount;
  d["beaconOk"]    = g_udpBeaconOk;
  d["beaconFail"]  = g_udpBeaconFail;
  d["lastErrno"]   = g_udpLastErrno;
  d["limitedBroadcast"] = "255.255.255.255";
  d["broadcastIP"] = g_udpBroadcastIp;
  d["ip"]          = deviceIP;
  d["mdns"]        = String(DISCOVERY_HOST) + ".local";
  String out; serializeJson(d, out); return out;
}

bool sendDiscoveryBeacon(IPAddress target) {
  JsonDocument d;
  d["t"] = "esp32s3_hello";
  d["proto"] = PROTO_VERSION;
  d["fw"] = FW_VERSION;
  d["ip"] = deviceIP;
  d["mac"] = WiFi.macAddress();
  d["mdns"] = String(DISCOVERY_HOST) + ".local";
  d["udpPort"] = DISCOVERY_UDP_PORT;
  d["bindMode"] = g_bindMode;
  String payload; serializeJson(d, payload);
  // BUGFIX：必须传 IPAddress 数值。旧版传 "255.255.255.255" 字符串会走
  // gethostbyname()，解析失败时 remote_ip/remote_port 未赋值，
  // endPacket() 静默往 0.0.0.0:0 发，PC 端永远收不到 beacon。
  if (!g_discoveryUdp.beginPacket(target, DISCOVERY_UDP_PORT)) {
    g_udpBeaconCount++; g_udpBeaconFail++; g_udpLastErrno = -1;
    return false;
  }
  size_t written = g_discoveryUdp.write((const uint8_t *)payload.c_str(), payload.length());
  int sent = g_discoveryUdp.endPacket();
  g_udpBeaconCount++;
  if (sent && written == payload.length()) {
    g_udpBeaconOk++; g_udpLastErrno = 0;
    return true;
  }
  g_udpBeaconFail++;
  g_udpLastErrno = sent ? -2 : errno;
  return false;
}

void serviceDiscoveryBeacon(uint32_t now) {
  static bool mdnsOk = false;
  static uint32_t lastBeacon = 0;
  static uint32_t fastCount = 0;
  if (WiFi.status() != WL_CONNECTED && !wifiInApMode) return;
  static uint32_t lastMdnsTry = 0;
  if (!mdnsOk && (lastMdnsTry == 0 || (now - lastMdnsTry) >= 5000)) {
    lastMdnsTry = now;          // BUGFIX：失败后最多每 5s 重试，不能每轮 loop 调 MDNS.begin
    mdnsOk = MDNS.begin(DISCOVERY_HOST);
    if (mdnsOk) {
      MDNS.addService("http", "tcp", 80);
      Serial.printf("[发现] mDNS 就绪: http://%s.local\n", DISCOVERY_HOST);
    }
  }
  uint32_t interval = (fastCount < DISCOVERY_FAST_BEACONS) ? DISCOVERY_FAST_MS : DISCOVERY_BEACON_MS;
  if (lastBeacon != 0 && (now - lastBeacon) < interval) return;
  lastBeacon = now;
  fastCount++;
  // 有限广播 + 子网定向广播双发：部分 AP/防火墙只放行其中一种
  IPAddress bcast = wifiInApMode ? WiFi.softAPBroadcastIP() : WiFi.broadcastIP();
  g_udpBroadcastIp = bcast.toString();
  bool okLimited  = sendDiscoveryBeacon(IPAddress(255, 255, 255, 255));
  bool okDirected = sendDiscoveryBeacon(bcast);
  Serial.printf("[发现] UDP beacon #%u -> 255.255.255.255:%s / %s:%s (ok=%u fail=%u errno=%d)\n",
                (unsigned)g_udpBeaconCount, okLimited ? "OK" : "FAIL",
                g_udpBroadcastIp.c_str(), okDirected ? "OK" : "FAIL",
                (unsigned)g_udpBeaconOk, (unsigned)g_udpBeaconFail, g_udpLastErrno);
}