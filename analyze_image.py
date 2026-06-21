"""
照片分析服务 - NAS 照片批量/单张分析

流程:
  扫描目录 -> EXIF 提取(GPS/参数/设备) -> VLM 评分(回忆度+美观分) -> SQLite 入库

用法:
  python analyze_image.py                       # 批量扫描全部未评分的照片
  python analyze_image.py --batch 5             # 5 线程并发评分
  python analyze_image.py --single <图片路径>    # 单张分析 + 评分
"""

import sys, os, csv, math, sqlite3, time, argparse, threading, io, base64, json
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen
from PIL import Image
import pillow_heif
import config
pillow_heif.register_heif_opener()

# print 强制 flush 以实时显示进度
import builtins
_orig_print = builtins.print
def print(*args, **kwargs):
    kwargs.setdefault('flush', True)
    _orig_print(*args, **kwargs)
builtins.print = print

# ─── 配置 (读取 config.env) ───
DASHSCOPE_API_KEY = ""
VLM_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
VLM_MODEL = "qwen3.7-plus"
VLM_MODEL_FALLBACK = "qwen3.5-plus"
NAS_PATH = ""
_config_loaded = False

def load_config():
    """从统一的 config.py 填充模块级配置 (保持原有全局变量接口)"""
    global DASHSCOPE_API_KEY, VLM_BASE_URL, VLM_MODEL, VLM_MODEL_FALLBACK, NAS_PATH, _config_loaded
    if _config_loaded: return True
    c = config.load()
    DASHSCOPE_API_KEY = c["DASHSCOPE_API_KEY"]
    VLM_BASE_URL = c["VLM_BASE_URL"]
    VLM_MODEL = c["VLM_MODEL"]
    VLM_MODEL_FALLBACK = c["VLM_MODEL_FALLBACK"]
    NAS_PATH = c["NAS_PATH"]
    if DASHSCOPE_API_KEY and NAS_PATH:
        _config_loaded = True
        return True
    if not NAS_PATH:
        print("[WARN] 未配置 NAS_PATH，请在 config.env 中填入 NAS_PATH")
    else:
        print("[WARN] 未配置 API Key，请在 config.env 中填入 DASHSCOPE_API_KEY")
    return False

# ─── 项目路径 ───
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RES_DIR = os.path.join(SCRIPT_DIR, "static")
DB_PATH = os.path.join(SCRIPT_DIR, "data", "photo_scores.db")

# ─── 数据库初始化 ───
def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS photo_scores (
            path              TEXT PRIMARY KEY,
            filename          TEXT,
            caption           TEXT,
            photo_type        TEXT,
            memory_score      REAL,
            beauty_score      REAL,
            reason            TEXT,
            side_caption      TEXT,
            orientation       TEXT,
            taken_at          TEXT,
            camera_make       TEXT,
            camera_model      TEXT,
            iso               INTEGER,
            aperture          REAL,
            shutter           TEXT,
            focal_length      REAL,
            lens              TEXT,
            gps_lat           REAL,
            gps_lon           REAL,
            gps_alt           REAL,
            city              TEXT,
            raw_json          TEXT,
            vlm_model         TEXT,
            created_at        TEXT,
            updated_at        TEXT,
            used_at           TEXT
        )
    """)
    # 迁移: 旧版 taken_at 用点号分隔 (YYYY.MM.DD), 改为横杠 (YYYY-MM-DD)
    # 使 SUBSTR(taken_at, 6) 正确返回 MM-DD, "历史上的今天" 才能生效
    c.execute("UPDATE photo_scores SET taken_at = REPLACE(taken_at, '.', '-') WHERE taken_at LIKE '____.%%.%%'")
    conn.commit()
    return conn

# ─── EXIF 读取 (基于 piexif) ───
def read_exif(path):
    info = {"orientation": "landscape", "gps_lat": None, "gps_lon": None}
    try:
        from PIL import ImageOps
        img = Image.open(path)
        # 应用 EXIF 旋转, 确保 width/height 反映实际显示方向
        img = ImageOps.exif_transpose(img)
        info["orientation"] = "landscape" if img.width > img.height else "portrait"
    except:
        pass

    try:
        import piexif as _px
        _xd = _px.load(path)

        # 0th IFD (基础信息)
        _0th = _xd.get("0th", {})
        info["make"] = (_0th.get(271, b"") or b"").decode(errors='replace').strip()
        info["model"] = (_0th.get(272, b"") or b"").decode(errors='replace').strip()
        info["datetime"] = (_0th.get(306, b"") or b"").decode(errors='replace').strip()

        # Exif IFD (拍摄参数)
        _ex = _xd.get("Exif", {})
        if _ex:
            def _to(v):
                if not v: return None
                return v[0]/v[1] if isinstance(v, tuple) and len(v)==2 and v[1] else (float(v) if hasattr(v, "__float__") else None)
            info["iso"] = int(_ex.get(0x8827, 0)) if _ex.get(0x8827) else None
            ap = _to(_ex.get(0x829D))
            if ap: info["aperture"] = ap
            fl = _to(_ex.get(0x920A))
            if fl: info["focal_length"] = fl
            et = _ex.get(0x829A)
            if et:
                et_v = _to(et)
                if et_v and et_v < 1: info["shutter"] = f"1/{1/et_v:.0f}"
                elif et_v: info["shutter"] = f"{et_v:.0f}s"
            ln = _ex.get(0xA434, b"")
            if ln: info["lens"] = ln.decode().strip()

        # GPS IFD
        _gps = _xd.get("GPS", {})
        if _gps and 2 in _gps and 4 in _gps:
            _la = _gps[2][0][0]/_gps[2][0][1] + _gps[2][1][0]/_gps[2][1][1]/60.0 + _gps[2][2][0]/_gps[2][2][1]/3600.0
            _lo = _gps[4][0][0]/_gps[4][0][1] + _gps[4][1][0]/_gps[4][1][1]/60.0 + _gps[4][2][0]/_gps[4][2][1]/3600.0
            info["gps_lat"] = -_la if _gps.get(1, b"N") in (b"S", "S") else _la
            info["gps_lon"] = -_lo if _gps.get(3, b"E") in (b"W", "W") else _lo
            alt = _gps.get(6)
            if alt: info["gps_alt"] = alt[0]/alt[1] if isinstance(alt, tuple) and len(alt)==2 and alt[1] else float(alt)
    except Exception as e:
        pass
    return info

_city_cache = None

# ─── 离线城市查询 ───
CITY_CSV = os.path.join(RES_DIR, "world_cities_zh.csv")
CITY_MAX_KM = 150.0

def load_cities():
    global _city_cache
    if _city_cache: return _city_cache
    cities, grid = [], {}
    if not os.path.exists(CITY_CSV):
        _city_cache = (cities, grid)
        return _city_cache
    with open(CITY_CSV, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                lt, ln = float(row["lat"]), float(row["lon"])
            except: continue
            name = row.get("name_zh", "").strip()
            if name and row.get("country_code") == "CN":
                cities.append((lt, ln, name))
    for i, (lt, ln, nm) in enumerate(cities):
        k = (int(math.floor(lt)), int(math.floor(ln)))
        grid.setdefault(k, []).append(i)
    _city_cache = (cities, grid)
    return _city_cache

def find_city(lat, lon):
    """GPS -> 最近的中国城市名 (haversine + 网格缓存)"""
    cities, grid = load_cities()
    if not cities: return ""
    gx, gy = int(math.floor(lat)), int(math.floor(lon))
    cand = []
    for r in [1, 2, 3]:
        for dx in range(-r, r+1):
            for dy in range(-r, r+1):
                b = grid.get((gx+dx, gy+dy))
                if b: cand.extend(b)
        if cand: break
    if not cand: return ""
    best, best_d = -1, float("inf")
    for i in cand:
        d = haversine_km(lat, lon, cities[i][0], cities[i][1])
        if d < best_d: best_d, best = d, i
    if best < 0 or best_d > CITY_MAX_KM: return ""
    return cities[best][2]

# ─── 设备名映射表 (res/camera_names.csv) ───
_CAMERA_NAMES = {}
_cam_csv = os.path.join(RES_DIR, "camera_names.csv")
if os.path.exists(_cam_csv):
    with open(_cam_csv, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("Make|"): continue
            parts = line.split("|", 2)
            if len(parts) == 3:
                _CAMERA_NAMES[(parts[0].strip(), parts[1].strip())] = parts[2].strip()

# ─── 常驻地坐标 (青海省 - 省内照片不加异地分) ───
HOME_LAT = 36.5
HOME_LON = 96.0
HOME_RADIUS_KM = 600.0

def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlambda/2)**2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def in_home(lat, lon):
    if lat is None or lon is None:
        return False
    return haversine_km(lat, lon, HOME_LAT, HOME_LON) <= HOME_RADIUS_KM

# ─── VLM 评分 ───

def encode_image_to_b64(path, max_long_edge=1280):
    """图片缩放到长边后转 base64 (1280≈通用理解甜点档, 审美评分无需更高分辨率)"""
    try:
        from PIL import ImageOps
        img = Image.open(path)
        img = ImageOps.exif_transpose(img)  # 应用 EXIF 方向校正
        w, h = img.size
        if max(w, h) > max_long_edge:
            scale = max_long_edge / max(w, h)
            img = img.resize((int(w*scale), int(h*scale)), Image.LANCZOS)
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        print(f"  [VLM] 图片编码失败: {e}")
        return None

def call_vlm(image_path):
    """调用 Qwen 3.7 Plus 进行照片评分，失败时降级到 3.5 Plus

    返回: {caption, photo_type, memory_score, beauty_score, reason, usage, vlm_model}
    """
    if not load_config():
        return None

    img_b64 = encode_image_to_b64(image_path)
    if not img_b64:
        return None

    # InkTime 风格评分 prompt
    system_prompt = (
        "你是一个个人相册照片评估助手，擅长理解真实照片的内容，并从回忆价值和美观角度打分。\n"
        "你会收到一张照片（以 base64 形式提供），你的任务是：\n"
        "1）用中文详细描述照片内容（60~150 字），\n"
        "2）判断照片的大致类型，从以下选择（可多个）：人物/合影/自拍/家庭/宠物/旅行/风景/建筑/美食/活动/日常/截图/其他\n"
        "3）给出 0~100 的值得回忆度 memory_score（精确到一位小数），\n"
        "4）给出 0~100 的美观程度 beauty_score（精确到一位小数），\n"
        "5）用简短中文 reason 解释原因（不超过 60 字）。\n\n"
        "【值得回忆度（memory_score）评分方法】\n"
        "先按照片的值得回忆程度确定得分区间，再进行精调。\n\n"
        "得分区间（0~100 连续无空档，确定照片落在哪一档；区间上含下不含，如 70 归入 70~84）：\n"
        "- 0~19：负向/无价值。截图、收据、广告、证件翻拍、文档资料、测试图、违规内容。\n"
        "- 20~39：几乎无回忆价值。随手拍的杂物、严重重复的废片、无意义记录。\n"
        "- 40~54：一般记录。普通日常，有基本记录意义但不出彩。\n"
        "- 55~69：有一定价值。愿意保留，有小情节或小亮点。\n"
        "- 70~84：较高价值。明确值得回忆，让人愿意多看两眼。\n"
        "- 85~100：珍藏级。稀缺、动人、强烈值得珍藏的瞬间。\n"
        "请用满 0~100，避免把分数扎堆在整十。\n"
        "注意：孩子/猫咪/宠物照片应直接以 75 分为基础，再按精调规则加分。\n\n"
        "精调规则（可叠加）：\n"
        "大幅加分（+8~15）：\n"
        "- 画面含清晰人脸、人物互动、合影，人际关系越亲密加分越多\n"
        "- 明显稀缺、不可复现的瞬间（婚礼、新生儿、毕业、团聚等）\n\n"
        "小幅加分（+3~8）：\n"
        "- 有事件性（生日、聚会、仪式、舞台表演等）\n"
        "- 有强烈情绪（大笑、哭泣、惊喜、拥抱等）\n"
        "- 优美风景、壮丽自然、精致构图\n"
        "- 旅行异地、地标建筑、旅途情景\n"
        "- 画面信息密度高，能讲清楚发生了什么\n\n"
        "微微减分（-2~5）：\n"
        "- 画质差、模糊、虚焦、过曝/欠曝\n\n"
        "【明显低价值图片处理】\n"
        "对以下低价值图片，必须将 memory_score 压低到 0~25（最多不超过 39）：\n"
        "- 裸体、低俗、色情或违反公序良俗的图片\n"
        "- 账单、收据、广告、手机截图、系统界面截图\n"
        "- 文档照片、白底资料、证件翻拍、文件扫描件\n"
        "- 随手拍的杂物、测试图片、无意义记录\n\n"
        "【美观分（beauty_score）评分方法】\n"
        "美观分只评价视觉美感，不要被人物/孩子/猫/旅行等题材绑架分数。\n\n"
        "得分区间（0~100 连续无空档，上含下不含）：\n"
        "- 0~19：废片。严重模糊、虚焦、构图混乱，几乎无美感。\n"
        "- 20~39：较差。画面杂乱或有明显缺陷。\n"
        "- 40~54：普通快照。随手记录，构图平平。\n"
        "- 55~69：尚可。有基本取景，干净顺眼。\n"
        "- 70~84：良好。构图/光影讲究，有美感。\n"
        "- 85~100：专业级。视觉冲击强，有作品感。\n"
        "请用满 0~100，避免把分数扎堆在整十。\n\n"
        "加分项：\n"
        "- 构图讲究（三分法、对称、引导线、留白、框架构图等）：大幅加分\n"
        "- 光影优秀（黄金时段、逆光、剪影、氛围光等）：大幅加分\n"
        "- 色彩搭配协调或有独特风格：小幅加分\n"
        "- 主体突出、背景干净：小幅加分\n"
        "- 画面有层次感、纵深感：小幅加分\n"
        "- 抓拍到决定性瞬间：小幅加分\n\n"
        "减分项：\n"
        "- 过曝/欠曝、噪点明显：减分\n"
        "- 对焦不准、画面模糊：大幅减分\n"
        "- 构图杂乱、主体不突出：减分\n\n"
        "【标定样例】(用于校准打分尺度, 仅供参照, 按实际照片判断, 不要照抄这些分数)\n"
        "- 手机里的外卖订单截图：memory_score≈8，beauty_score≈10（负向无价值 / 废片）\n"
        "- 黄金时段海边剪影，构图讲究但纯风景无人物：memory_score≈68，beauty_score≈90\n"
        "  （回忆一般、但美观很高——两个分数相互独立，不要绑在一起打）\n"
        "- 婚礼上新人相拥、宾客欢笑、逆光氛围好：memory_score≈92，beauty_score≈80（珍藏级 / 良好）\n\n"
        "【输出格式】\n"
        "请严格只输出 JSON，格式如下：\n"
        "{\n"
        "  \"caption\": \"......\",\n"
        "  \"photo_type\": \"人物/家庭/旅行......\",\n"
        "  \"memory_score\": 0.0,\n"
        "  \"beauty_score\": 0.0,\n"
        "  \"reason\": \"不超过 60 字的理由\"\n"
        "}\n"
        "不要输出任何多余文字，不要加注释。"
    )

    body = {
        "model": VLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "请分析这张照片，输出 JSON。"},
                    {"type": "image_url", "image_url": f"data:image/jpeg;base64,{img_b64}"}
                ]
            }
        ],
        "temperature": 0.2,
        "max_tokens": 1024,
    }

    api_key = DASHSCOPE_API_KEY
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    model_name = VLM_MODEL

    def try_request(model, timeout=30):
        _body = dict(body)
        _body["model"] = model
        try:
            _req = Request(
                VLM_BASE_URL,
                data=json.dumps(_body).encode(),
                headers=headers
            )
            _resp = urlopen(_req, timeout=timeout)
            return json.loads(_resp.read().decode())
        except:
            return None

    # 先试主模型(30s), 失败则试备用(120s)
    data = try_request(VLM_MODEL, 30)
    if data is None:
        data = try_request(VLM_MODEL_FALLBACK, 120)
        if data is not None:
            model_name = VLM_MODEL_FALLBACK

    if data is None:
        return None

    # 提取并解析模型输出 (任何异常/坏响应都返回 None, 不让整批扫描崩溃)
    try:
        content = data["choices"][0]["message"]["content"].strip()
        usage = data.get("usage", {})
        token_info = f"{usage.get('total_tokens', 0)}t" if usage else ""

        # 去除 ```json ... ``` 代码块围栏 (兼容单行/多行)
        if content.startswith("```"):
            content = content.strip("`")
            if content[:4].lower() == "json":
                content = content[4:]
            content = content.strip()
        # 截取首个 { 到末个 } 之间, 容忍模型前后多余文字
        l, r = content.find("{"), content.rfind("}")
        if l != -1 and r != -1 and r > l:
            content = content[l:r + 1]
        result = json.loads(content)
    except Exception as e:
        print(f"  [VLM] 响应解析失败 ({model_name}): {e}")
        return None

    return {
        "caption": str(result.get("caption", "")),
        "photo_type": str(result.get("photo_type", "")),
        "memory_score": float(result.get("memory_score", 0)),
        "beauty_score": float(result.get("beauty_score", 0)),
        "reason": str(result.get("reason", "")),
        "usage": token_info,
        "vlm_model": model_name,
    }

# ─── 分析单张照片 ───
def analyze_one(path: str, db_conn=None, use_vlm=False, base_dir="", db_lock=None) -> dict:
    """提取 EXIF -> VLM 评分 -> 入库。path: 相对路径，base_dir: NAS 基目录"""
    full_path = os.path.join(base_dir, path) if base_dir else path
    fn = os.path.basename(path)
    exif = read_exif(full_path)

    # GPS -> 城市
    lat = exif.get("gps_lat")
    lon = exif.get("gps_lon")
    city = find_city(lat, lon) if (lat is not None and lon is not None) else ""

    # 设备名
    make = exif.get("make", "")
    model = exif.get("model", "")
    camera_name = _CAMERA_NAMES.get((make, model), f"{make} {model}".strip())

    # 拍摄日期 (用横杠分隔, 使 SUBSTR(taken_at, 6) = 'MM-DD' 可用于按月日匹配)
    dt = exif.get("datetime", "")
    if dt and ":" in dt:
        dt = dt.split(" ")[0].replace(":", "-")

    result = {
        "path": path,
        "filename": fn,
        "caption": "",
        "photo_type": "",
        "memory_score": None,
        "beauty_score": None,
        "reason": "",
        "side_caption": "",
        "orientation": exif.get("orientation"),
        "taken_at": dt,
        "camera_make": make,
        "camera_model": model,
        "camera_name": camera_name,
        "iso": exif.get("iso"),
        "aperture": exif.get("aperture"),
        "shutter": exif.get("shutter"),
        "focal_length": exif.get("focal_length"),
        "lens": exif.get("lens", ""),
        "gps_lat": lat,
        "gps_lon": lon,
        "gps_alt": exif.get("gps_alt"),
        "city": city,
        "raw_json": "",
        "vlm_model": "",
        "usage": "",
    }

    # VLM 评分
    if use_vlm:
        vlm_result = call_vlm(full_path)
        if vlm_result:
            result["caption"] = vlm_result["caption"]
            result["photo_type"] = vlm_result["photo_type"]
            result["memory_score"] = vlm_result["memory_score"]
            result["beauty_score"] = vlm_result["beauty_score"]
            result["reason"] = vlm_result["reason"]
            result["raw_json"] = json.dumps(vlm_result, ensure_ascii=False)
            result["vlm_model"] = vlm_result.get("vlm_model", VLM_MODEL)
            result["usage"] = vlm_result.get("usage", "")

            # 异地加分 (距离常驻地 > 600km +5 分)
            if lat is not None and lon is not None and result["memory_score"] is not None:
                if not in_home(lat, lon):
                    result["memory_score"] = min(result["memory_score"] + 5.0, 100.0)

            # 附言: 仅给够格上墙的照片(memory>=50)预生成, 离线一并入库, 避免取图时现场等
            if result["memory_score"] is not None and result["memory_score"] >= 50:
                try:
                    import send_image
                    sc = send_image.generate_side_caption(
                        full_path, caption=result["caption"], photo_type=result["photo_type"])
                    if sc:
                        result["side_caption"] = sc
                except Exception as e:
                    print(f"  [附言] 生成失败: {repr(e)[:80]}")

        # 入库
    if db_conn:
        if db_lock:
            db_lock.acquire()
        try:
            now = datetime.now().isoformat()
            c = db_conn.cursor()
            c.execute("""
                INSERT OR REPLACE INTO photo_scores
                (path, filename, caption, photo_type, memory_score, beauty_score, reason, side_caption,
                 orientation, taken_at, camera_make, camera_model,
                 iso, aperture, shutter, focal_length, lens,
                 gps_lat, gps_lon, gps_alt, city, raw_json, vlm_model,
                 created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,
                        ?,?,?,?,
                        ?,?,?,?,?,
                        ?,?,?,?,?,?,
                        ?,?)
            """, (
                path, fn, result["caption"], result["photo_type"],
                result["memory_score"], result["beauty_score"], result["reason"], result["side_caption"],
                result["orientation"], dt,
                make, model, exif.get("iso"), exif.get("aperture"),
                exif.get("shutter"), exif.get("focal_length"), exif.get("lens", ""),
                lat, lon, exif.get("gps_alt"), city,
                result["raw_json"], result["vlm_model"],
                now, now
            ))
            db_conn.commit()
        finally:
            if db_lock:
                db_lock.release()

    return result

def print_result(r: dict):
    """打印分析结果摘要"""
    print(f"  文件    : {r['filename']}")
    print(f"  设备    : {r['camera_name']}")
    print(f"  日期    : {r['taken_at'] or '-'}")
    print(f"  地点    : {r['city'] or '-'}")
    print(f"  方向    : {r['orientation']}")
    if r.get("iso"): print(f"  参数    : ISO{r['iso']} f/{r['aperture']} {r['shutter'] or ''} {r['focal_length'] or ''}mm".strip())
    if r.get("lens"): print(f"  镜头    : {r['lens']}")
    if r.get("gps_lat"): print(f"  GPS     : {r['gps_lat']:.4f}, {r['gps_lon']:.4f}")
    if r.get("memory_score") is not None:
        usage = r.get("usage", "")
        token = f"  tokens:{usage}" if usage else ""
        print(f"  VLM评分 : 回忆度={r['memory_score']:.1f}  美观度={r['beauty_score']:.1f}{token}")
        print(f"  类型    : {r['photo_type']}")
        print(f"  描述    : {r['caption'][:80]}")

# ─── 批量扫描 ───
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".bmp", ".webp"}

def scan_directory(path: str = "") -> list:
    """递归遍历 NAS 目录，返回所有图片的相对路径列表"""
    if not path:
        path = NAS_PATH
    files = []
    for root, dirs, fnames in os.walk(path):
        for f in sorted(fnames):
            if os.path.splitext(f)[1].lower() in IMAGE_EXTS:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, path)
                files.append(rel)
    return files

def filter_unscored(conn, paths: list) -> list:
    """过滤已评分的照片，只保留 memory_score IS NULL 的记录"""
    c = conn.cursor()
    scored = set()
    for i in range(0, len(paths), 500):
        chunk = paths[i:i+500]
        placeholders = ",".join("?" for _ in chunk)
        rows = c.execute(f"SELECT path FROM photo_scores WHERE path IN ({placeholders}) AND memory_score IS NOT NULL", chunk).fetchall()
        scored.update(r[0] for r in rows)
    return [p for p in paths if p not in scored]

# ─── 主流程 ───
def main():
    if not load_config():
        sys.exit(1)
    parser = argparse.ArgumentParser(description="照片分析服务")
    parser.add_argument("--single", help="单张照片路径")
    parser.add_argument("--batch", type=int, default=1, help="并发线程数")
    parser.add_argument("--reset-scores", action="store_true",
                        help="清空所有照片的 memory_score/beauty_score (换评分规则后重扫用), 然后退出")
    parser.add_argument("--captions", action="store_true",
                        help="只给已评分(memory>=40)但缺附言的照片补附言, 不重新评分 (省token), 然后退出。支持 --batch N 并发")
    args = parser.parse_args()

    conn = init_db()

    if args.captions:
        # 给已评分但 side_caption 为空的照片补附言 (不调评分, 只调附言, 省token)
        # 支持多线程: --batch N 控制并发数 (附言是纯网络IO调LLM, 并发收益大)
        import send_image
        c = conn.cursor()
        rows = c.execute("""SELECT path, caption, photo_type FROM photo_scores
                            WHERE memory_score >= 40
                              AND (side_caption IS NULL OR side_caption='')""").fetchall()
        total = len(rows)
        print(f"[CAPTIONS] 待补附言: {total} 张 (memory>=40 且无附言), 并发 {args.batch}")
        t0 = time.time()
        db_lock = threading.Lock()
        done = 0
        done_count = 0

        def caption_one(path, cap, ptype):
            full = os.path.join(NAS_PATH, path)
            if not os.path.exists(full):
                return (path, None, "文件不存在")
            try:
                sc = send_image.generate_side_caption(full, caption=cap, photo_type=ptype)
            except Exception as e:
                return (path, None, f"失败: {repr(e)[:60]}")
            if not sc:
                return (path, None, "附言生成失败")
            with db_lock:                       # SQLite 写入串行化
                c.execute("UPDATE photo_scores SET side_caption=? WHERE path=?", (sc, path))
                conn.commit()
            return (path, sc, None)

        if args.batch > 1:
            with ThreadPoolExecutor(max_workers=args.batch) as exe:
                futures = {exe.submit(caption_one, p, cap, pt): p for (p, cap, pt) in rows}
                for fut in as_completed(futures):
                    done_count += 1
                    path, sc, err = fut.result()
                    elapsed = time.time() - t0
                    eta = elapsed / done_count * (total - done_count) if done_count else 0
                    if sc:
                        done += 1
                        print(f"  [{done_count}/{total}] {os.path.basename(path)}: {sc}  [ETA:{eta:.0f}s]")
                    else:
                        print(f"  [{done_count}/{total}] {os.path.basename(path)}: ({err})")
        else:
            for (p, cap, pt) in rows:
                done_count += 1
                path, sc, err = caption_one(p, cap, pt)
                if sc:
                    done += 1
                    print(f"  [{done_count}/{total}] {os.path.basename(path)}: {sc}")
                else:
                    print(f"  [{done_count}/{total}] {os.path.basename(path)}: ({err})")
        conn.close()
        print(f"[CAPTIONS] 完成, 补了 {done} 张, 耗时 {time.time()-t0:.0f}s")
        return

    if args.reset_scores:
        # 换评分规则后的迁移: 把分数置空, 下次 --batch 会按新规则重新评分。
        # 仅清空分数相关字段, 保留 EXIF/GPS/city/side_caption/used_at。
        c = conn.cursor()
        n = c.execute("SELECT COUNT(*) FROM photo_scores WHERE memory_score IS NOT NULL").fetchone()[0]
        c.execute("""UPDATE photo_scores SET
                       memory_score=NULL, beauty_score=NULL,
                       caption='', photo_type='', reason='', raw_json='', vlm_model=''""")
        conn.commit()
        conn.close()
        print(f"[RESET] 已清空 {n} 张照片的评分。请运行 python analyze_image.py --batch N 重新评分。")
        return

    if args.single:
        # 单张分析
        spath = os.path.join(NAS_PATH, args.single) if not os.path.isabs(args.single) else args.single
        r = analyze_one(spath, conn, use_vlm=True, db_lock=None)
        print_result(r)
        if r.get("memory_score") is not None:
            print(f"  VLM评分: 回忆度={r['memory_score']:.1f}  美观度={r['beauty_score']:.1f}")
            print(f"  类型: {r['photo_type']}")
            print(f"  描述: {r['caption'][:80]}")
        conn.close()
        return

    # 批量评分
    print(f"[SCAN] {NAS_PATH}")
    all_files = scan_directory()
    print(f"[SCAN] 共 {len(all_files)} 张")

    all_files = filter_unscored(conn, all_files)
    print(f"[SCAN] 未评分: {len(all_files)} 张")

    t0 = time.time()
    db_lock = threading.Lock()
    n = len(all_files)

    def worker_one(p, idx, total):
        # 单张失败不得中断整批: 任何异常都吞掉并标记 (异常)
        try:
            r = analyze_one(p, conn, use_vlm=True, base_dir=NAS_PATH, db_lock=db_lock)
        except Exception as e:
            return f"{os.path.basename(p)}  (异常: {e})"
        parts = [r['filename']]
        if r.get('camera_name'): parts.append(r['camera_name'])
        if r.get('city'): parts.append(r['city'])
        if r.get('memory_score') is not None:
            parts.append(f"回忆{r['memory_score']:.0f} 美{r['beauty_score']:.0f}")
            if r.get('usage'): parts.append(r['usage'])
        else:
            parts.append("(失败)")
        return "  ".join(parts)

    if args.batch > 1:
        # 多线程并发评分
        with ThreadPoolExecutor(max_workers=args.batch) as exe:
            futures = {exe.submit(worker_one, p, i+1, n): (i+1, p) for i, p in enumerate(all_files)}
            done_count = 0
            for future in as_completed(futures):
                idx, _ = futures[future]
                done_count += 1
                line = future.result()
                elapsed = time.time() - t0
                eta = elapsed / done_count * (n - done_count)
                print(f"  [{idx}/{n}] {line}  [{done_count}/{n} {elapsed:.0f}s ETA:{eta:.0f}s]")
        print(f"\n[DONE] 已处理 {done_count} 张, 耗时 {time.time()-t0:.0f}s (并发 {args.batch})")
    else:
        # 单线程顺序评分
        for i, p in enumerate(all_files, 1):
            elapsed = time.time() - t0
            eta = elapsed / i * (n - i) if i > 0 else 0
            line = worker_one(p, i, n)
            print(f"  [{i}/{n}] {line}  [{i}/{n} {elapsed:.0f}s ETA:{eta:.0f}s]")
        print(f"\n[DONE] 已处理 {n} 张, 耗时 {time.time()-t0:.0f}s")

    conn.close()

if __name__ == "__main__":
    main()
