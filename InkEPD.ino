/**
 * InkEPD — ESP32-C3 拉模型固件
 *
 * 定时唤醒 → WiFi → POST /ink/refresh → 收图 → 刷屏 → 睡
 * 触摸唤醒 → 短按切换信息栏 / 长按10s联网刷新
 *
 * 接线: GPIO5→MOSI  GPIO6→SCLK  GPIO7→CS  GPIO10→DC
 *       GPIO20→RST  GPIO21→BUSY  GPIO3→TTP223
 *
 * 分区: OTA 双分区 (ota_0/ota_1 各 1.5MB) + SPIFFS 960KB (partitions.csv)
 * 依赖: GxEPD2, ArduinoJson
 */
#include <SPI.h>
#include <GxEPD2_7C.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <LittleFS.h>
#include <ArduinoJson.h>
#include "esp_sleep.h"
#include <Update.h>

// ─── 硬件 ───
#define DISPLAY_CLASS  GxEPD2_730c_GDEY073D46
#define PAGE_BUFFER    (DISPLAY_CLASS::HEIGHT / 4)
#define ROTATION       3
#define IMG_W  480
#define IMG_H  800
#define FB_SIZE (IMG_W * IMG_H)

#define PIN_MOSI  5
#define PIN_SCLK  6
#define PIN_CS    7
#define PIN_DC    10
#define PIN_RST   20
#define PIN_BUSY  21
#define PIN_TOUCH 3
#ifndef LED_BUILTIN
#define LED_BUILTIN 8
#endif

// ─── WiFi ───
#define WIFI_SSID     "your-wifi-ssid"
#define WIFI_PASS     "your-wifi-password"
#define WIFI_TIMEOUT  15000

// ─── 服务器 ───
#define SERVER_IP     "192.168.x.x"
#define SERVER_URL    "http://" SERVER_IP ":8765/ink/refresh"
#define HTTP_TIMEOUT  480000          // 数据读超时: 长, 覆盖服务端 BiRefNet 推理(~10s)+收 768KB
#define CONNECT_TIMEOUT 15000         // 连接建立超时: 短, 服务器不可达时 15s 快速失败(不再死等 8min)
#define DEFAULT_SLEEP (9UL * 3600UL)
#define RETRY_SLEEP   (30UL * 60UL)    // 定时刷新失败后的快重试间隔: 30 分钟(而非死等9h)

// ─── 触摸阈值 ───
#define LONG_PRESS       10000UL
#define TOUCH_IDLE_MS    15000

// ─── 全局 ───
GxEPD2_7C<DISPLAY_CLASS, PAGE_BUFFER> display(
  DISPLAY_CLASS(PIN_CS, PIN_DC, PIN_RST, PIN_BUSY));
Preferences prefs;

RTC_DATA_ATTR static uint32_t bootCount = 0;
RTC_DATA_ATTR static bool     rtcInfoVisible = true;
RTC_DATA_ATTR static uint64_t rtcSleepSec = DEFAULT_SLEEP;

static bool infoVisible = true;

// ─── 前置声明 (doNetworkRefresh 在 tryOTA 定义之前调用它;
//     Arduino IDE 会自动生成原型, 显式声明可兼容 PlatformIO/纯 g++) ───
static void tryOTA(const char* url, const char* name, uint32_t fwSize);
static bool doNetworkRefresh();
static void showImage();

// ─── NVS WiFi ───
struct WiFiConfig { String ssid, pass; bool valid; };
static void saveConfig(const WiFiConfig &c) {
  prefs.begin("epd_cfg", false);
  prefs.putString("ssid", c.ssid); prefs.putString("pass", c.pass);
  prefs.putBool("valid", c.valid); prefs.end();
}
static WiFiConfig loadConfig() {
  WiFiConfig c; prefs.begin("epd_cfg", true);
  c.ssid = prefs.getString("ssid", ""); c.pass = prefs.getString("pass", "");
  c.valid = prefs.getBool("valid", false); prefs.end();
  return c;
}

// ─── 已刷固件标识 (NVS 持久化, 掉电也不丢; 替代易失的 RTC fwSize) ───
static String loadFwName() {
  prefs.begin("epd_cfg", true);
  String n = prefs.getString("fw_name", "");
  prefs.end();
  return n;
}
static void saveFwName(const char* n) {
  prefs.begin("epd_cfg", false);
  prefs.putString("fw_name", n ? n : "");
  prefs.end();
}

// ─── WiFi ───
static bool connectWiFi(const WiFiConfig &c) {
  if (!c.valid) return false;
  WiFi.mode(WIFI_STA); WiFi.setHostname("InkEPD");
  WiFi.setSleep(false);                  // 关省电模式: 连接更稳定, 减少弱网丢包
  // 降发射功率: 电池供电下全功率(19.5dBm)电流尖峰大→电压跌落→WiFi发包失败。
  // 从 18.5dBm 起(仅降约1dB保守), 不够再往下调档位。
  WiFi.setTxPower(WIFI_POWER_18_5dBm);
  // DHCP 自动获取 IP (适配手机热点等网段不固定的环境, 无需静态 IP/网关)
  WiFi.begin(c.ssid.c_str(), c.pass.c_str());
  uint32_t s = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - s < WIFI_TIMEOUT) delay(200);
  if (WiFi.status() != WL_CONNECTED) return false;
  delay(300);                            // 连接后稳定延时: 等 WiFi/DHCP 就绪再发请求
  return true;
}

// ─── 颜色映射 ───
static uint16_t pixelToColor(uint8_t pv) {
  switch (pv) {
    case 0: return GxEPD_BLACK;
    case 1: return GxEPD_WHITE;
    case 2: return GxEPD_YELLOW;
    case 3: return GxEPD_RED;
    case 4: return GxEPD_BLUE;
    case 5: return GxEPD_GREEN;
    default: return GxEPD_WHITE;
  }
}

// ─── 画图 ───
static void drawFromFile(const char* path) {
  File f = LittleFS.open(path, "r");
  if (!f) return;
  uint8_t row[IMG_W];
  for (int y = 0; y < IMG_H; y++) {
    yield(); delay(1);   // 让出 CPU + 喂 Arduino 看门狗 (任务未注册 task_wdt, 不能调 esp_task_wdt_reset)
    if (f.read(row, IMG_W) != IMG_W) break;
    for (int x = 0; x < IMG_W; x++)
      display.drawPixel(x, y, pixelToColor(row[x]));
  }
  f.close();
}

static void drawImage() {
  if (infoVisible && LittleFS.exists("/current_i.fb"))
    { drawFromFile("/current_i.fb"); return; }
  if (LittleFS.exists("/current.fb"))
    { drawFromFile("/current.fb"); return; }
  display.fillScreen(GxEPD_WHITE);
  display.setTextColor(GxEPD_BLACK);
  display.setCursor(20, 100); display.print("Waiting...");
}

static void showImage() {
  display.setFullWindow();
  display.firstPage();
  do { drawImage(); } while (display.nextPage());
}

// 刷新失败提示: 快闪板载 LED 数次, 不动面板 (避免全刷重画旧图误导成"已换图")
static void blinkError(int times = 6) {
  for (int i = 0; i < times; i++) {
    digitalWrite(LED_BUILTIN, HIGH); delay(80);
    digitalWrite(LED_BUILTIN, LOW);  delay(120);
  }
}

// 全屏状态提示 (白底黑字, 用于 OTA/启动等过程提示)
static void showStatus(const char* line1, const char* line2) {
  display.setFullWindow();
  display.firstPage();
  do {
    display.fillScreen(GxEPD_WHITE);
    display.setTextColor(GxEPD_BLACK);
    display.setCursor(40, 360); display.print(line1);
    if (line2 && line2[0]) { display.setCursor(40, 410); display.print(line2); }
  } while (display.nextPage());
}

// 刷新失败提示: 重画当前画面 + 右下角叠红底白字英文小字 (内置字体仅英文)。
// 不换图(图本就没变, 不误导), 提示留到下次成功刷新被覆盖。本屏无快速局刷, 整屏全刷一次。
static void showImageError(const char* msg) {
  const int boxW = 150, boxH = 22;
  const int x0 = IMG_W - boxW, y0 = IMG_H - boxH;   // 右下角
  display.setFullWindow();
  display.firstPage();
  do {
    drawImage();                                     // 先画当前照片
    display.fillRect(x0, y0, boxW, boxH, GxEPD_RED); // 红底块
    display.setTextColor(GxEPD_WHITE);
    display.setCursor(x0 + 6, y0 + 7); display.print(msg);
  } while (display.nextPage());
}

// ─── 触摸 (按着计时, 松手判定; 带松手去抖, 防长按被瞬时毛刺打断) ───
// 返回: 0=无动作, 1=短按(50ms~1s 松开, 切换信息栏), 2=长按(按满10s, 联网刷新)
// 规则: 50ms~1s 松开=短按; 1s~10s 区间无论是否松开都不响应; 按满10s=长按(不必等松手)。
#define RELEASE_DEBOUNCE_MS 60   // 连续读到 LOW 超过此时长才算真松手, 滤掉 TTP223 瞬时掉电平

static unsigned long s_touchStart = 0;
static bool s_touchHeld = false;
static bool s_touchIgnore = false;   // 忽略当前这次按压(直到松手)
static unsigned long s_lowSince = 0; // 开始连续读到 LOW 的时刻 (0=当前为 HIGH)

// 唤醒后调用: 若手指仍在 pad 上, 把"唤醒那一下"标记为已消费,
// 松手时不再触发短按 → 避免与"唤醒即切换"叠加成双翻转(等于没切)。
static void primeTouchAfterWake() {
  if (digitalRead(PIN_TOUCH) == HIGH) {
    s_touchStart = millis();
    s_touchHeld = true;
    s_touchIgnore = true;
  } else {
    s_touchHeld = false;
    s_touchIgnore = false;
  }
  s_lowSince = 0;
}

static int checkTouch() {
  bool t = digitalRead(PIN_TOUCH) == HIGH;

  // 按下沿
  if (t && !s_touchHeld) { s_touchStart = millis(); s_touchHeld = true; s_touchIgnore = false; s_lowSince = 0; return 0; }

  // 按住中: 先判长按 (按满10s 立即触发, 不必等松手)
  if (t && s_touchHeld) {
    s_lowSince = 0;  // 仍是 HIGH, 清掉松手计时 (瞬时毛刺被吸收)
    if (!s_touchIgnore && int32_t(millis() - s_touchStart) >= int32_t(LONG_PRESS)) {
      s_touchHeld = false;
      return 2;
    }
    return 0;
  }

  // 读到 LOW: 需连续 LOW 超过去抖时长才算真松手 (滤掉长按中的瞬时掉电平)
  if (!t && s_touchHeld) {
    if (s_lowSince == 0) { s_lowSince = millis(); return 0; }       // 刚开始 LOW, 起计时
    if (millis() - s_lowSince < RELEASE_DEBOUNCE_MS) return 0;      // LOW 不够久, 可能是毛刺, 暂不判松手

    // 确认松手: 按住时长按"LOW 起始时刻"算, 不含去抖窗口, 保证 50ms~1s 判定精确
    int32_t h = int32_t(s_lowSince - s_touchStart);
    s_touchHeld = false;
    s_lowSince = 0;
    bool ignored = s_touchIgnore;
    s_touchIgnore = false;
    if (ignored) return 0;                              // 唤醒press的松手: 不动作
    if (h >= 50 && h < 1000) {                          // 50ms~1s: 短按切换
      infoVisible = !infoVisible;
      rtcInfoVisible = infoVisible;
      return 1;
    }
    // <50ms 抖动 或 1s~10s 中间态: 均不响应 (按你要求, 没按满10s又超过1s则忽略)
    return 0;
  }

  return 0;
}

// ─── HTTP 获取图片 ───
static char s_fwUrlBuf[256];
static char s_fwNameBuf[128];

static bool fetchImageOnce(uint64_t &nextWake, const char* &fwUrl, const char* &fwName,
                       uint32_t &fwSize, const char* reason, bool &changed) {
  HTTPClient http;
  http.setConnectTimeout(CONNECT_TIMEOUT); // 连接建立超时: 15s, 服务器不可达快速失败
  http.setTimeout(HTTP_TIMEOUT);          // 响应/数据间隔超时 (覆盖 POST 等首字节)
  http.begin(SERVER_URL);

  StaticJsonDocument<128> req;
  req["info_visible"] = infoVisible;
  req["reason"] = reason;
  req["fw_version"] = loadFwName();
  String reqStr;
  serializeJson(req, reqStr);

  Serial.printf("[HTTP] POST reason=%s info=%d\n", reason, infoVisible);  // 诊断: 每次请求发的 reason
  int code = http.POST(reqStr);
  if (code != 200) { Serial.printf("[HTTP] %d\n", code); http.end(); return false; }

  WiFiClient* stream = http.getStreamPtr();
  if (!stream) { http.end(); return false; }
  stream->setTimeout(HTTP_TIMEOUT);   // 覆盖 readBytes 读超时 (服务端 BiRefNet 推理 ~10s 不会被判 -11)

  uint32_t hdrLen;
  if (stream->readBytes((uint8_t*)&hdrLen, 4) != 4 || hdrLen > 4096) { http.end(); return false; }
  char* hdrBuf = (char*)malloc(hdrLen + 1);
  if (!hdrBuf) { http.end(); return false; }
  if (stream->readBytes((uint8_t*)hdrBuf, hdrLen) != hdrLen) {  // 弱网读残: 显式校验, 防静默截断
    free(hdrBuf); http.end(); return false;
  }
  hdrBuf[hdrLen] = 0;

  StaticJsonDocument<512> hdr;
  DeserializationError err = deserializeJson(hdr, hdrBuf);
  // 注意: deserializeJson 对可变 char* 输入是零拷贝, 字符串值指向 hdrBuf。
  // 必须等所有字符串字段都 strncpy 拷出后再 free, 否则 firmware_name 等读到悬空指针(变null)。
  if (err) { free(hdrBuf); http.end(); return false; }

  // 先取 next_wake (即便 ok=false 也要带回, 让上层按服务端节奏重试而非死等 9h)
  nextWake = hdr["next_wake"] | DEFAULT_SLEEP;
  if (!hdr["ok"]) { free(hdrBuf); http.end(); return false; }

  changed  = hdr["changed"] | true;   // 缺省 true: 服务端没说就当作变了, 安全重绘

  const char* fwUrlRaw = hdr["firmware_url"].as<const char*>();
  if (fwUrlRaw && fwUrlRaw[0]) {
    strncpy(s_fwUrlBuf, fwUrlRaw, sizeof(s_fwUrlBuf) - 1);
    s_fwUrlBuf[sizeof(s_fwUrlBuf) - 1] = 0;
    fwUrl = s_fwUrlBuf;
  } else {
    s_fwUrlBuf[0] = 0; fwUrl = nullptr;
  }

  const char* fwNameRaw = hdr["firmware_name"].as<const char*>();
  if (fwNameRaw && fwNameRaw[0]) {
    strncpy(s_fwNameBuf, fwNameRaw, sizeof(s_fwNameBuf) - 1);
    s_fwNameBuf[sizeof(s_fwNameBuf) - 1] = 0;
    fwName = s_fwNameBuf;
  } else {
    s_fwNameBuf[0] = 0; fwName = nullptr;
  }
  fwSize = hdr["firmware_size"] | 0;

  char fnBuf[64];
  strncpy(fnBuf, hdr["filename"] | "?", sizeof(fnBuf) - 1);
  fnBuf[sizeof(fnBuf) - 1] = 0;
  free(hdrBuf);   // 所有字符串字段已拷出, 现在可安全释放

  Serial.printf("[HTTP] wake=%llu  %s\n", nextWake, fnBuf);

  uint32_t len;
  // clean: 先删 info 旧文件腾空间
  if (stream->readBytes((uint8_t*)&len, 4) != 4 || len != FB_SIZE) { http.end(); return false; }
  LittleFS.remove("/current_i.fb");
  LittleFS.remove("/current.tmp");
  File f = LittleFS.open("/current.tmp", "w");
  if (!f) { http.end(); return false; }
  uint8_t buf[1024]; uint32_t rem = len;
  while (rem) { size_t n = rem > 1024 ? 1024 : rem;
    if (stream->readBytes(buf, n) != n) { f.close(); LittleFS.remove("/current.tmp"); http.end(); return false; }
    f.write(buf, n); rem -= n; yield(); }
  f.close();
  LittleFS.remove("/current.fb");
  LittleFS.rename("/current.tmp", "/current.fb");

  // info
  if (stream->readBytes((uint8_t*)&len, 4) != 4 || len != FB_SIZE) { http.end(); return false; }
  LittleFS.remove("/current_i.tmp");
  f = LittleFS.open("/current_i.tmp", "w");
  if (!f) { http.end(); return false; }
  rem = len;
  while (rem) { size_t n = rem > 1024 ? 1024 : rem;
    if (stream->readBytes(buf, n) != n) { f.close(); LittleFS.remove("/current_i.tmp"); http.end(); return false; }
    f.write(buf, n); rem -= n; yield(); }
  f.close();
  LittleFS.remove("/current_i.fb");
  LittleFS.rename("/current_i.tmp", "/current_i.fb");

  http.end();
  return true;
}

// 整体重试包装: 弱网下 readBytes 偶发 -11 读超时, 重试整次请求 (最多3次)。
// reason=user 时, 重试改用 "user_retry" → 服务端返回首次刚选的同一张图,
// 避免每次重试都重新选图导致"图不一致"和轮换池浪费。
static bool fetchImage(uint64_t &nextWake, const char* &fwUrl, const char* &fwName,
                       uint32_t &fwSize, const char* reason, bool &changed) {
  bool isUser = (strcmp(reason, "user") == 0);
  for (int attempt = 1; attempt <= 3; attempt++) {
    const char* r = (attempt == 1 || !isUser) ? reason : "user_retry";
    if (fetchImageOnce(nextWake, fwUrl, fwName, fwSize, r, changed)) return true;
    Serial.printf("[HTTP] fetch fail, retry %d/3\n", attempt);
    delay(1000);
  }
  return false;
}

// ─── 深度睡眠 ───
static void goToSleep(uint64_t sec) {
  uint32_t tr = millis();
  while (digitalRead(PIN_TOUCH) == HIGH && millis() - tr < 2000) delay(10);

  rtcInfoVisible = infoVisible;
  rtcSleepSec = sec;

  Serial.printf("[SLEEP] %llus\n", (unsigned long long)sec);
  Serial.flush();
  WiFi.disconnect(true); WiFi.mode(WIFI_OFF);
  display.powerOff(); display.hibernate();
  esp_sleep_enable_timer_wakeup(sec * 1000000ULL);
  esp_deep_sleep_enable_gpio_wakeup(1ULL << PIN_TOUCH, ESP_GPIO_WAKEUP_GPIO_HIGH);
  delay(20);
  esp_deep_sleep_start();
}

// ─── 联网刷新 ───
static bool doNetworkRefresh() {
  WiFiConfig cfg = loadConfig();
  if (!cfg.valid || !connectWiFi(cfg)) return false;
  uint64_t nw = DEFAULT_SLEEP;
  const char* fwUrl = nullptr; const char* fwName = nullptr; uint32_t fwSize = 0;
  bool changed = true;
  if (fetchImage(nw, fwUrl, fwName, fwSize, "user", changed)) {
    infoVisible = true; rtcInfoVisible = infoVisible;
    showImage();   // 用户主动刷新: 服务端必返新图, 始终重绘
    tryOTA(fwUrl, fwName, fwSize);
    goToSleep(nw);
  }
  return false;
}

// ─── OTA ───
static void tryOTA(const char* url, const char* name, uint32_t fwSize) {
  Serial.printf("[OTA?] name='%s' size=%u nvs='%s'\n",
                name ? name : "(null)", fwSize, loadFwName().c_str());  // 版本比对: 触发或跳过 OTA
  if (!url || !name || !name[0] || fwSize == 0) return;
  // 版本身份 = 固件文件名 (NVS 持久化)。同名即视为已刷, 跳过, 杜绝重刷/启动循环
  if (loadFwName() == String(name)) return;
  Serial.printf("[OTA] Updating to '%s' (%u bytes) from %s\n", name, fwSize, url);
  showStatus("Firmware update...", "Do not power off");   // OTA 开始: 提示勿断电
  HTTPClient http;
  http.setConnectTimeout(CONNECT_TIMEOUT);  // 连接建立超时: 15s, 固件服务器不可达快速失败
  http.setTimeout(120000);
  http.begin(url);
  if (http.GET() != 200) { Serial.printf("[OTA] HTTP fail\n"); http.end(); showStatus("Update failed", "HTTP error"); return; }
  if (!Update.begin(fwSize)) { Serial.println("[OTA] begin fail"); http.end(); showStatus("Update failed", "no space"); return; }
  WiFiClient* stream = http.getStreamPtr();
  size_t written = Update.writeStream(*stream);
  http.end();
  if (written != fwSize) { Serial.printf("[OTA] size mismatch %d/%d\n", written, fwSize); showStatus("Update failed", "size mismatch"); return; }
  if (!Update.end()) { Serial.printf("[OTA] end error %d\n", Update.getError()); showStatus("Update failed", "verify error"); return; }
  // 成功: 先持久化新版本名 (掉电也不丢), 再重启进新固件
  saveFwName(name);
  Serial.println("[OTA] Done, rebooting...");
  showStatus("Update done", "Rebooting...");
  delay(500);
  ESP.restart();
}

// ─── setup ───
void setup() {
  Serial.begin(115200);
  // 关闭 loopTask 看门狗: 本固件"做完一件事就深睡", 写768KB+绘图800行偶尔>5s,
  // 会触发默认 Task WDT 软复位(rst:0xc), 导致取图后到不了 goToSleep。
  disableLoopWDT();
  bootCount++;
  infoVisible = rtcInfoVisible;
  esp_sleep_wakeup_cause_t wc = esp_sleep_get_wakeup_cause();
  Serial.printf("[BOOT] #%lu  wake=%d  info=%d\n", bootCount, wc, infoVisible);

  pinMode(LED_BUILTIN, OUTPUT); digitalWrite(LED_BUILTIN, bootCount % 2);
  pinMode(PIN_TOUCH, INPUT);
  SPI.end(); SPI.begin(PIN_SCLK, -1, PIN_MOSI, PIN_CS);
  display.init(0, true, 2, false); display.setRotation(ROTATION);
  if (!LittleFS.begin(false)) { LittleFS.format(); LittleFS.begin(false); }
  LittleFS.remove("/current.tmp");
  LittleFS.remove("/current_i.tmp");

  // ─── 触摸唤醒 ───
  // 方案A: 唤醒后先不刷屏, 先测这次按压时长:
  //   短按(<1s 松开)        → 切换信息栏 + 刷屏
  //   长按(按住满 LONG_PRESS) → 联网刷新换图
  //   这样唤醒时刷屏(十几秒)不会吃掉长按计时, 长按可被正确识别。
  if (wc == ESP_SLEEP_WAKEUP_GPIO || wc == ESP_SLEEP_WAKEUP_EXT0) {
    Serial.println("[WAKE] Touch");

    // 1) 判定唤醒这次按压: 等手指松开或按满 10s
    uint32_t pressStart = millis();
    bool longPress = false;
    unsigned long lowSince = 0;
    while (true) {
      bool t = (digitalRead(PIN_TOUCH) == HIGH);
      if (t) {
        lowSince = 0;
        if (millis() - pressStart >= LONG_PRESS) { longPress = true; break; }  // 按满10s
      } else {
        // 松手去抖: 连续 LOW 超过阈值才算真松开
        if (lowSince == 0) lowSince = millis();
        else if (millis() - lowSince >= RELEASE_DEBOUNCE_MS) break;            // 已松开
      }
      delay(10);
    }
    int32_t held = int32_t((longPress ? millis() : lowSince) - pressStart);
    Serial.printf("[WAKE] held=%dms longPress=%d\n", held, longPress);

    if (longPress) {
      // 长按: 联网刷新 (成功则内部 goToSleep 不返回)
      doNetworkRefresh();
      // 走到这里 = 3次重试均失败: 重画当前图+右下角叠错误提示(不换图,留到下次刷新)
      blinkError();
      showImageError("! refresh failed");
      goToSleep(rtcSleepSec);
      return;
    }

    // 短按(或抖动): 切换信息栏并刷屏
    infoVisible = !infoVisible;
    rtcInfoVisible = infoVisible;
    showImage();

    // 2) 之后进入空闲轮询, 支持继续短按切换 / 长按联网
    primeTouchAfterWake();
    uint32_t ts = millis();
    while (millis() - ts < TOUCH_IDLE_MS) {
      int a = checkTouch();
      if (a == 1) { showImage(); ts = millis(); }  // 短按: 再次切换 + 重置空闲
      if (a == 2) {                                  // 长按 >=10s: 联网刷新
        doNetworkRefresh();                          // 成功则内部 goToSleep 不返回
        blinkError();                                // 失败: LED 闪 + 屏上提示
        showImageError("! refresh failed");          // 重画当前图+右下角错误提示
        break;
      }
      delay(30);
    }
    goToSleep(rtcSleepSec);
    return;
  }

  // ─── 定时唤醒 / 冷启动 ───
  // 真·首次 (从未收过图): 先刷一屏启动提示, 消除"上电到首图"的残留真空期
  if (!LittleFS.exists("/current.fb")) {
    display.fillScreen(GxEPD_WHITE);
    display.setTextColor(GxEPD_BLACK);
    display.setCursor(20, 360); display.print("InkEPD starting...");
    display.setCursor(20, 400); display.print("Connecting WiFi");
    showImage();
  }

  WiFiConfig cfg = loadConfig();
  if (!cfg.valid) {
    cfg.ssid = WIFI_SSID; cfg.pass = WIFI_PASS; cfg.valid = true;
    saveConfig(cfg); delay(100); ESP.restart();
  }

  if (!connectWiFi(cfg)) {
    Serial.println("[BOOT] WiFi fail");
    showImageError("! refresh failed");   // 右下角红底白字(与长按失败一致)
    goToSleep(RETRY_SLEEP);                // 30 分钟后快重试, 不死等 9h
    return;   // goToSleep 正常不返回; 万一返回也不再继续往下取图
  }

  uint64_t nextWake = DEFAULT_SLEEP;
  const char* fwUrl = nullptr; const char* fwName = nullptr; uint32_t fwSize = 0;
  bool changed = true;
  bool coldBoot = (wc != ESP_SLEEP_WAKEUP_TIMER);   // 冷启动后屏内容未知, 必须重绘
  const char* reason = coldBoot ? "cold" : "timer";
  if (fetchImage(nextWake, fwUrl, fwName, fwSize, reason, changed)) {
    infoVisible = true; rtcInfoVisible = infoVisible;
    // 缓存命中(未换图)且非冷启动 → 跳过整屏刷新, 墨水屏保留睡前画面
    if (changed || coldBoot) showImage();
    else Serial.println("[SKIP] unchanged, keep panel");
    tryOTA(fwUrl, fwName, fwSize);
    goToSleep(nextWake);
  } else {
    // 定时/冷启动取图失败: 右下角红底白字提示(与长按失败一致) + 30分钟快重试
    Serial.println("[BOOT] fetch fail");
    showImageError("! refresh failed");
    goToSleep(RETRY_SLEEP);
  }
}

void loop() { delay(1000); }
