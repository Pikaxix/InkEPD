# InkEPD — 把 647 张照片装进墨水屏相框

> **AI 策展 + 全栈极客项目**  
> Qwen VLM 给老照片打分写诗，7 色电子墨水屏轮播  
> 硬件：`ESP32-C3` + `GxEPD2_7C` + `TTP223` 触摸  
> 软件：`Python PIL` + `Qwen 3.7 Plus` + `SQLite` + `Floyd-Steinberg` 抖动  
> 架构：`拉模型` — ESP32 主动拉取，Server 全权渲染，电池续航 2 年

---

## 一、版本历史

| 版本 | 架构 | 状态 |
|------|------|------|
| v5 | 推模型：NAS 定时向 ESP32 HTTP Server 推送 | ✅ 稳定版 |
| v6 | 拉模型：ESP32 主动向 NAS Server 请求 | ✅ 稳定版 |
| v7 | 拉模型 + BiRefNet + 统一配置 | ✅ 稳定版 |
| v8 | 开发中… | 🚧 开发版 |

### v5 → v6 核心变化

| 方面 | v5 | v6 |
|------|-----|-----|
| **通信方向** | NAS → ESP32（推） | ESP32 → NAS（拉） |
| **ESP32 角色** | HTTP Server 等推送 | HTTP Client 发 POST |
| **ESP32 代码** | 533 行 | 407 行 |
| **NTP 校时** | ESP32 自管 | Server 管 |
| **上传窗口** | 10 分钟空转 | 无需，秒级完成 |
| **活跃时间** | 26 分钟/天 | ~10 秒/天 |
| **续航(5000mAh)** | ~8 个月 | ~2 年 |
| **新增** | — | server.py (207 行) |
| **新增** | — | OTA 无线升级 |
| **新增** | — | 确认框 + 长按刷新 |

---

## 二、系统架构（v6）

```
                         ┌──────────────────────────┐
                         │     飞牛 NAS              │
                         │  server.py (Flask 常驻)    │
                         │  /ink/refresh 端点        │
                         │  /health 监控             │
                         │  /firmware OTA 分发       │
                         └─────────┬─────────────────┘
                                   │
                    POST /ink/refresh (定时/触摸唤醒)
                         ┌─────────▼─────────────────┐
                         │       ESP32-C3              │
                         │  WiFi Client → HTTP POST    │
                         │  收 framebuffer → 写SPIFFS  │
                         │  → 刷屏 → OTA检查 → 深睡   │
                         └────────────────────────────┘
```

**核心理念**：
- Server 常驻 NAS，管控全部逻辑（选图、渲染、时间计算）
- ESP32 极简化——不跑 Server、不管时间、不校 NTP，Server 告诉它睡多久
- 每天定时唤醒 2 次，触摸可手动唤醒，每次活动 ~5 秒

---

## 三、二进制协议

```
ESP32 POST /ink/refresh  {"info_visible": true}
       ↓
Server 响应 (little-endian):
  [4B: header_json_len]
  {"ok":true, "next_wake":84600, "filename":"...", "firmware_url":"..."}
  [4B: clean_fb_len=384000]
  [clean_fb ...384000 bytes]
  [4B: info_fb_len=384000]
  [info_fb ...384000 bytes]
```

`next_wake` 由 Server 根据 `REFRESH_TIMES` 计算，ESP32 直接用它配置定时器入睡。

---

## 四、硬件接线

```
GPIO5  → MOSI/DIN    GPIO6  → SCLK
GPIO7  → CS          GPIO10 → DC
GPIO20 → RST         GPIO21 → BUSY
GPIO3  → TTP223 触摸按键
```

| 组件 | 型号 | 用途 |
|------|------|------|
| 主控 | ESP32-C3 | WiFi + SPI + 深度睡眠 |
| 屏幕 | GxEPD2_7C（7色） | 480×800，实际用 6 色 |
| 触摸 | TTP223（GPIO3） | ext0 唤醒 + 触摸交互 |

### 烧录参数

- **分区方案**：Custom → `partitions.csv`（OTA 双分区 1.5MB+1.5MB+SPIFFS 960KB）
- **依赖库**：GxEPD2、ArduinoJson

---

## 五、触摸逻辑

| 操作 | 时长 | 动作 |
|------|------|------|
| **短按** | <1s | 切换信息栏显示/隐藏 + 刷屏 |
| **长按** | ≥10s | 联网刷新换图（失败则闪 LED 提示，不重画旧图） |
| 唤醒后 | 15s 无操作 | 自动睡眠 |

> **方案A（唤醒后先测按压时长再决定，不立即刷屏）**：旧逻辑"唤醒即刷屏"会阻塞十几秒（7色屏全刷慢），把长按计时吃掉 → 长按被误判成短按。方案A 把刷屏挪到判定按压之后，长按才能被准确识别。短按计时带 60ms 松手去抖，滤掉 TTP223 长按中的瞬时掉电平毛刺。

---

## 六、照片评分：AI 策展人

用 Qwen 3.7 Plus 从两个维度打分：

**回忆度（memory_score）**：值不值得被记住？
- 人物合影、亲密瞬间 → +8~15
- 婚礼、新生儿、毕业、团聚 → 加分
- 宠物/孩子 → 基础 75 分起跳
- 随手拍、截图 → 0~39 分

**美观度（beauty_score）**：纯粹视觉品质
- 三分法构图、黄金光线 → 加分
- 过曝欠曝、对焦不准 → 减分

**异地加分**：距离青海省 >600km 的照片 memory_score +5

### 评分结果

```
647 张 → 646 张评分完成
平均回忆度 63.4  ·  平均美观度 62.5
最高分 97.5 / 95.0  ·  可用照片 532 张（≥40 分）
已有附言 173 张（147 张可直接上传无需 API 调用）
```

---

## 七、选图策略

```python
def pick_candidate():
    ① 历史上的今天（同月同日）
    ② 前后 ±3 天
    ③ 全库轮换（按综合分 = 回忆×0.7 + 美观×0.3 排序）
```

- **冷却期**：30 天，选中过的照片在此期间不再重复
- **低分过滤**：<40 分的照片跳过
- **附言按需生成**：选中的照片无附言时才调用 VLM 生成，存入数据库复用

---

## 八、附言生成

**路线**：Qwen 看图描述 → DeepSeek（纯文本）据描述写一句。两步是因为 DeepSeek 看不了图，必须先有 Qwen 的文字描述。

```
Prompt 原则：
- 不描述画面，写"看完画面后心里多出的一句话"
- 优先引用契合的歌词（限定用户音乐库的 35 位华语歌手），无合适的再原创
- 引用句末标「——歌手《歌名》」，渲染时小字右对齐
- 8~50 字，自然有趣，避免鸡汤/禁词
```

- **预生成入库**：`analyze_image.py --captions --batch N` 多线程给 ≥40 分缺附言的照片批量生成附言存库（并发调 LLM，DB 写入加锁）。do_refresh 优先用库存附言，命中则 ~0.5s 出图。
- **为何要预生成**：DeepSeek-v4-pro 带 reasoning，单次附言生成 7~14s。若现场生成会阻塞 do_refresh 到十几秒，导致固件读超时显示旧图。预生成后 do_refresh 不再现场调 LLM。
- **已知风险**：放开引用后 LLM 可能记错歌词或反复引用同几首热门歌（撞车），批量后建议抽查 "——" 引用句。

> **不数星星了，直接用手电筒和银河连个线。** — DSC_1437，95/95  
> **晚归的车灯，在立交桥上弹奏着一首夜曲。** — DSC_4201，90/90  
> **在荒野走四步，抬头刚好撞见一整条银河。** — DSC_1624，95/95  

---

## 九、图片处理

### 横竖图策略

| 类型 | 纯图 (clean) | 带文图 (info) |
|------|-------------|--------------|
| 竖图 | 等比缩放 + 居中裁切 | 同一张 + 底栏 |
| 横图 | 旋转90° + 等比缩放 + 居中裁切 | 主体分割智能裁切 + 底栏 |

### 横图智能裁切（BiRefNet 主体分割）

按高度缩放到 800px 后，需水平裁成 480px 竖构图。用 **rembg + BiRefNet-general** 抠出前景主体 mask，取主体包围盒（列方向峰值 5% 阈值定左右边界）居中裁切，保证主体完整进框。

- 失败回退：mask 全黑（纯风景无主体）或 rembg 不可用时，退回旧的"水平梯度幅值 × 高斯中心偏置"滑窗法。
- 模型惰性加载 + server 启动后台预热，消除首张横图加载延迟。
- 代价：BiRefNet CPU 推理 ~8-11s/张（低频刷新可接受）；竖图不走此路径。
- 旧的纯梯度法易被高纹理背景（树皮/砖墙/水波）吸走、把主体裁出画面，故升级为主体分割。

### 色彩管线

```
原图 → 饱和度 1.4x → 高斯模糊叠加 → Gamma 1.3 → Floyd-Steinberg 抖动 → 6 色 framebuffer
```

### 6 色调色板

```
0: 深蓝(≈黑)  1: 白     2: 黄
3: 浅红        4: 蓝     5: 浅绿
```

硬件支持 7 色（含橙色），但橙色在屏上偏棕偏暗，舍弃不用。墨水屏社区（InkTime 等）通用做法。

---

## 十、离线城市查询

`world_cities_zh.csv` — 1709 个中国城市  
GPS → 1° 网格索引 → Haversine 距离 → 最近城市名  
单次查询 <1ms，零外部依赖，搜索半径 150km

---

## 十一、OTA 无线升级

**前提**：首次必须用 USB 烧录一次 v6 固件（选 `partitions.csv`），之后永久无线升级。

### 升级步骤

```
1. Arduino IDE → 草图 → 导出已编译的二进制文件
   → 在项目目录生成 eink-test-v6.ino.bin

2. 把 .bin 文件丢进 NAS 上的 InkEPD/firmware/ 目录
   scp eink-test-v6.ino.bin user@nas:/vol1/1000/AI\ Agent/openclaw/InkEPD/firmware/

3. 等 ESP32 自己醒来（每天 8:00 / 17:00 定时，或触摸长按 10s 手动刷新）
   → 自动检测到新固件 → 下载 → 烧录 → 重启
   → 无需操作 ESP32
```

### 确认升级

```bash
curl http://nas-ip:8765/health
# {"firmware": "eink-test-v6.ino.bin", "esp32_alive": true, ...}
```

### 安全机制

- **版本身份 = 固件文件名**：固件把已刷版本名存进 NVS（`fw_name`，掉电不丢），与服务端发来的 `firmware_name`（= firmware/ 目录里最新 .bin 文件名）比对，**不同才刷**，刷完先存名再重启 → 杜绝重刷/启动循环。
- **发版铁律**：每次发版必须换文件名（如 `inkepd_v13.bin`）；同名 = 永不更新。强制重刷需清 NVS。
- 双分区（ota_0 / ota_1）交替写入，烧录失败自动回退旧固件。
- OTA 在刷屏之后静默执行，全程 `showStatus` 屏幕提示"Firmware update / Do not power off"。
- **firmware_name 解析坑（已修）**：固件读 header 字段曾用 `hdr["firmware_name"] | nullptr`，ArduinoJson v6 该重载类型推导歧义会返回 null（数字字段不受影响，故曾出现"size 收到、name 丢"），导致 OTA 永不触发。已改用 `.as<const char*>()` 显式取字符串。

---

## 十二、配置文件

`config.env` — 所有参数统一管理：

```env
# VLM API
DASHSCOPE_API_KEY = "sk-your-api-key"
VLM_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
VLM_MODEL = "qwen3.7-plus"
VLM_MODEL_FALLBACK = "qwen3.5-plus"

# NAS 照片库
NAS_PATH = "/vol2/1000/Aesthetic/Gallery/Reveal"

# ESP32
ESP32_IP = "10.10.1.9"
ESP32_PORT = 80

# 刷新时间 (HH:MM 逗号分隔)
REFRESH_TIMES = "08:00,17:00"

# Server 端口
SERVER_PORT = 8765
```

---

## 十三、文件结构

```
InkEPD/
├── eink-test-v6.ino     # ESP32-C3 固件 (407 行)
├── server.py            # Flask 服务 (207 行)
├── send_image.py        # 选图 + 渲染 + 量化 (795 行)
├── analyze_image.py     # 批量评分 (613 行)
├── config.env           # 所有配置
├── photo_scores.db      # SQLite 照片库
├── partitions.csv       # OTA 分区表
├── firmware/            # .bin 放此自动 OTA
├── res/
│   ├── 默认字体.TTF
│   ├── world_cities_zh.csv
│   └── camera_names.csv
└── README.md
```

---

## 十四、部署

### 首次部署（USB 烧录）

```bash
# 1. 上传 InkEPD/ 到飞牛 NAS
scp -r InkEPD/ user@nas:/vol1/1000/AI\ Agent/openclaw/

# 2. NAS 上安装 Python 依赖
pip3 install flask pillow pillow-heif numpy piexif

# 3. 确认 NAS_PATH 路径正确
cat config.env

# 4. 起 Server
cd /vol1/1000/AI\ Agent/openclaw/InkEPD
python3 server.py   # 或 systemd 守护

# 5. 烧录 ESP32（需 USB）
#    Arduino IDE → 选 eink-test-v6.ino → 分区 Custom → 烧录
#    改 SERVER_IP 为 NAS 实际 IP
```

### systemd 守护（推荐）

```ini
# /etc/systemd/system/inkepd.service
[Unit]
Description=InkEPD Server
After=network.target

[Service]
Type=simple
WorkingDirectory=/vol1/1000/AI Agent/openclaw/InkEPD
ExecStart=/usr/bin/python3 server.py
Restart=always

[Install]
WantedBy=multi-user.target
```

### 监控

```bash
curl http://nas-ip:8765/health
# {"ok":true, "esp32_alive":true, "last_contact_sec":3420, "firmware":"v6.bin"}
```

`esp32_alive` 基于 `REFRESH_TIMES` 计算超时阈值，超时变 false 可触发 OpenCLAW 通知。

### OpenCLAW 集成

- `server.py` 常驻 → 不需要 cron
- 定期 GET `/health` → `esp32_alive` 为 false 时微信通知

---

## 十五、项目统计

| 指标 | 数据 |
|------|------|
| 照片总量 / 已评分 | 647 / 646 |
| 可用照片（≥40分） | 532 |
| 已有附言 | 173 |
| 平均回忆度 / 美观度 | 63.4 / 62.5 |
| 冷却期 | 30 天 |
| ESP32 代码 | 407 行 |
| Server 代码 | 207 行 |
| Python 代码总量 | 1615 行 |
| framebuffer | 384000 bytes |
| 电池续航（5000mAh） | ~2 年 |

---

## 十六、可改进方向

- [ ] 调色板校准：实际打印色块后校色
- [ ] 自适应色彩增强：根据直方图动态调整饱和度
- [ ] 橙色通道可开关：暖色调照片可启用
- [ ] 照片删除同步：NAS 删了照片后自动清理数据库
- [ ] Web 管理界面：浏览器预览 + 手动选图

---

## 十七、写在最后

墨水屏是电子设备里少有的"慢媒介"。

它不闪烁、不发光。一张照片可以安静地待一整天。不像手机屏幕，它不会催促你"往下划"。

当 AI 从 600+ 张照片里挑出一张 6 年前的星空，在屏幕下方写下：  
> *不数星星了，直接用手电筒和银河连个线。*

你会觉得，这些代码写得值。

---

*硬件：ESP32-C3 + GxEPD2_7C + TTP223*  
*软件：Python PIL + Flask + Qwen 3.7 Plus + SQLite*  
*NAS：飞牛 / OpenCLAW 部署*
