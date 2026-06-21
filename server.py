"""
InkEPD Server — 接收 ESP32 握手, 选图渲染, 返回帧缓存
常驻运行: python server.py 或 systemd 守护

接口:
  POST /ink/refresh  → ESP32 唤醒来取图
  GET  /health       → 健康检查
"""
import sys, os, json, struct, logging
from pathlib import Path
from datetime import datetime, timedelta
from flask import Flask, request, Response, render_template, jsonify, send_file
import config

sys.stdout.reconfigure(line_buffering=True)


class _Tee:
    """同时写多个流 (控制台 + 日志文件), 任一失败不影响其它。"""
    def __init__(self, *streams):
        self._streams = streams

    def write(self, s):
        for st in self._streams:
            try:
                st.write(s)
                st.flush()
            except Exception:
                pass
        return len(s)

    def flush(self):
        for st in self._streams:
            try:
                st.flush()
            except Exception:
                pass


def _setup_file_logging():
    """把 print/异常/Werkzeug 访问日志同时落到 server_live.log (追加模式)。
    无论谁用 `python server.py` 启动, 都能拿到持久日志。"""
    log_path = os.path.join(SCRIPT_DIR, "log", "server_live.log")
    fp = open(log_path, "a", encoding="utf-8", buffering=1)  # 行缓冲
    sys.stdout = _Tee(sys.__stdout__, fp)
    sys.stderr = _Tee(sys.__stderr__, fp)
    # Werkzeug 访问日志走 logging, 单独接到同一文件 + 控制台
    wlog = logging.getLogger("werkzeug")
    wlog.setLevel(logging.INFO)
    wlog.handlers.clear()
    for stream in (sys.__stderr__, fp):
        h = logging.StreamHandler(stream)
        h.setFormatter(logging.Formatter("%(message)s"))
        wlog.addHandler(h)
    wlog.propagate = False
    return log_path

app = Flask(__name__)

def _safe_join(base, rel):
    """防目录穿越：只允许 base 下的路径"""
    p = (Path(base) / rel).resolve()
    if not str(p).startswith(str(Path(base).resolve()) + os.sep) and p != Path(base).resolve():
        raise ValueError("path traversal blocked")
    return p

# ─── 加载配置 (统一走 config.py) ───
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
cfg = config.load()

# ─── 固件 OTA ───
FW_DIR = os.path.join(SCRIPT_DIR, "firmware")

def _latest_firmware():
    """扫描 firmware/ 目录找最新 .bin，返回 (filename, full_path, size) 或 None"""
    if not os.path.isdir(FW_DIR):
        return None
    bins = [f for f in os.listdir(FW_DIR) if f.endswith(".bin")]
    if not bins:
        return None
    # 按修改时间取最新
    bins.sort(key=lambda f: os.path.getmtime(os.path.join(FW_DIR, f)), reverse=True)
    fn = bins[0]
    fp = os.path.join(FW_DIR, fn)
    return (fn, fp, os.path.getsize(fp))

# ─── 最后通讯时间 / 超时计算 ───
_last_contact = datetime.now()
_server_start = datetime.now()

# ─── ESP32 固件版本 (从 POST body 的 fw_version 解析) ───
_esp32_version = ""  # "inkepd_v17.bin" 或空串 (未上报)

# ─── 6 色调色板 (与 send_image.py PALETTE_RGB 一致) ───
_PALETTE = [
    (30,  25,  50),   # 0 黑
    (255, 255, 255),  # 1 白
    (255, 230, 50),   # 2 黄
    (200, 100, 100),  # 3 红
    (50,  80,  200),  # 4 蓝
    (100, 200, 100),  # 5 绿
]

# ─── 渲染结果缓存 (data/ 目录) ───
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
_CLEAN_FB = os.path.join(DATA_DIR, "clean.fb")
_INFO_FB  = os.path.join(DATA_DIR, "info.fb")
_META_JSON = os.path.join(DATA_DIR, "meta.json")
_last_render = None  # {clean, info, header}
_last_refresh_dt = None  # 上次真正换图(NEW)的时间, 用于判定定时刷新点是否已服务

def _load_cache():
    """从 data/ 目录恢复缓存 (服务重启后有效)"""
    global _last_render, _last_refresh_dt
    if _last_render is not None:
        return
    if not os.path.exists(_META_JSON) or not os.path.exists(_CLEAN_FB):
        return
    try:
        with open(_CLEAN_FB, "rb") as f:
            clean = f.read()
        with open(_INFO_FB, "rb") as f:
            info = f.read()
        if len(clean) != 480 * 800 or len(info) != 480 * 800:
            return
        with open(_META_JSON, "r", encoding="utf-8") as f:
            header = json.load(f)
        ts = header.get("_render_ts")
        if ts:
            try:
                _last_refresh_dt = datetime.fromisoformat(ts)
            except ValueError:
                pass
        _last_render = {"clean": clean, "info": info, "header": header}
        print(f"[CACHE] loaded: {header.get('filename','?')}")
    except:
        pass

def _save_cache():
    """持久化渲染结果到 data/ 目录"""
    if _last_render is None:
        return
    try:
        with open(_CLEAN_FB, "wb") as f:
            f.write(_last_render["clean"])
        with open(_INFO_FB, "wb") as f:
            f.write(_last_render["info"])
        with open(_META_JSON, "w", encoding="utf-8") as f:
            json.dump(_last_render["header"], f, ensure_ascii=False)
    except:
        pass

def _most_recent_scheduled(now):
    """返回 <= now 的最近一个刷新时点 datetime (可能落在昨天); 无配置返回 None"""
    times = parse_times(cfg["REFRESH_TIMES"])
    if not times:
        return None
    cands = []
    for h, m in times:
        t = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if t > now:
            t -= timedelta(days=1)  # 该时点今天还没到 → 取昨天的
        cands.append(t)
    return max(cands)

def _should_refresh(reason):
    """换图判定 — 服务端唯一权威

    reason 由 ESP32 上报:
      "user"       — 用户长按10s确认后请求 → 换新图
      "user_retry" — 固件弱网重试 → 返回上次刚选的同一张(不重选), 保证一致+不浪费轮换池
      "timer"      — 定时器唤醒
      "cold"       — 冷启动/崩溃恢复 (无 RTC 时间)
    """
    # 首次启动无缓存 → 必须渲染
    if _last_render is None:
        return True
    # 弱网重试: 已有缓存就复用, 不重新选图 (固件首发 user 选了图A, 重试用 user_retry 拿同一张)
    if reason == "user_retry":
        return False
    # 用户明确要求 → 换
    if reason == "user":
        return True
    # 定时唤醒 → 只要"最近一个已过的刷新时点"还没被服务过就换图。
    # 不再依赖窄时间窗口, 因此晚醒/晶振漂移(几分钟~数十分钟)都不会漏刷。
    if reason == "timer":
        pt = _most_recent_scheduled(datetime.now())
        if pt is None:
            return False
        return _last_refresh_dt is None or _last_refresh_dt < pt
    # 冷启动 / 未知 → 不换图, 用缓存
    return False

def max_silence_seconds():
    """基于 REFRESH_TIMES 计算允许的最长无通讯间隔"""
    times = parse_times(cfg["REFRESH_TIMES"])
    if not times:
        return 24 * 3600
    n = len(times)
    if n == 1:
        return 24 * 3600 + 3600  # 一次/天 + 1h 容错
    # 相邻间隔中最大的 + 最晚到次日最早的跨夜间隔
    gaps = []
    for i in range(n):
        h1, m1 = times[i]
        h2, m2 = times[(i + 1) % n]
        gap = ((h2 - h1) * 60 + (m2 - m1)) * 60
        if gap <= 0:
            gap += 24 * 3600
        gaps.append(gap)
    return max(gaps) + 3600  # 加 1h 容错

def _touch():
    global _last_contact
    _last_contact = datetime.now()

def silence_seconds():
    return int((datetime.now() - _last_contact).total_seconds())

# ─── 解析刷新时间 ───
def parse_times(raw):
    """'08:00,17:00' → [(8,0), (17,0)]，无效条目跳过"""
    times = []
    for t in raw.split(","):
        t = t.strip()
        if not t or ":" not in t:
            continue
        try:
            h, m = t.split(":", 1)
            h, m = int(h), int(m)
            if 0 <= h <= 23 and 0 <= m <= 59:
                times.append((h, m))
        except ValueError:
            continue
    return sorted(times)

# ─── 计算到下次刷新的秒数 ───
def seconds_until_next():
    """基于 REFRESH_TIMES 计算到下一个时点的秒数"""
    now = datetime.now()
    times = parse_times(cfg["REFRESH_TIMES"])
    if not times:
        return 24 * 3600
    candidates = []
    for h, m in times:
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        candidates.append(target)
    next_wake = min(candidates)
    return max(60, int((next_wake - now).total_seconds()))


@app.route("/ink/refresh", methods=["POST"])
def ink_refresh():
    """ESP32 握手 -> 选图渲染 -> 返回 framebuffer"""
    global _last_render, _last_refresh_dt
    _load_cache()

    # 解析请求: ESP32 上报唤醒原因 + 固件版本
    reason = "cold"  # 默认冷启动
    global _esp32_version
    try:
        body_raw = request.get_data(as_text=True)
        if body_raw:
            req_json = json.loads(body_raw)
            reason = req_json.get("reason", "cold")
            fw_ver = req_json.get("fw_version", "")
            if fw_ver:
                _esp32_version = fw_ver
    except:
        pass

    should_refresh = _should_refresh(reason)
    print(f"[REQ] reason={reason}  should_refresh={should_refresh}")

    try:
        import send_image
    except Exception as e:
        return _error_response(f"send_image 加载失败: {e}")

    if should_refresh:
        try:
            _t0 = datetime.now()
            result = send_image.do_refresh()
            _dt_ms = int((datetime.now() - _t0).total_seconds() * 1000)
            print(f"[TIMING] do_refresh 耗时 {_dt_ms}ms")  # 诊断: 含 BiRefNet 推理
        except Exception as e:
            import traceback
            traceback.print_exc()
            result = {"ok": False, "error": f"do_refresh 异常: {e}"}

        if result["ok"]:
            _last_refresh_dt = datetime.now()  # 记录本次换图时间, 供定时刷新点判定
            header = {"ok": True, "next_wake": seconds_until_next(),
                      "filename": result["filename"],
                      "path": result.get("path", ""),
                      "side_caption": result["side_caption"],
                      "city": result.get("city", ""),
                      "date": result.get("date", ""),
                      "camera": result.get("camera", ""),
                      "mem": result["mem"], "beau": result["beau"],
                      "_render_ts": _last_refresh_dt.isoformat()}
            _last_render = {
                "clean": result["clean"], "info": result["info"],
                "header": dict(header)
            }
            _save_cache()
            tag = "NEW"
        else:
            header = {"ok": False, "next_wake": seconds_until_next(),
                      "error": result["error"]}
            tag = "FAIL"
    else:
        header = dict(_last_render["header"])
        header["next_wake"] = seconds_until_next()
        tag = "CACHE"

    # OTA 固件信息
    fw = _latest_firmware()

    # ─── 线头: 只发固件实际读取的字段, 防止撑爆 ESP32 的 JSON 缓冲 ───
    # (city/date/camera/side_caption 等已渲染进图像, 固件无需感知)
    wire = {"ok": header["ok"], "next_wake": header["next_wake"]}
    if fw:
        wire["firmware_url"] = f"http://{request.host}/firmware/{fw[0]}"
        wire["firmware_size"] = fw[2]
        wire["firmware_name"] = fw[0]      # 版本身份 = 文件名 (固件据此判断是否需 OTA)

    if not header["ok"]:
        wire["error"] = str(header.get("error", ""))[:120]
        return _packed_response(wire)

    wire["filename"] = header.get("filename", "")
    wire["changed"] = (tag == "NEW")       # 仅真正换了新图才置 True; 缓存命中为 False

    clean = _last_render["clean"]
    info = _last_render["info"]

    hb = json.dumps(wire, ensure_ascii=False).encode("utf-8")
    body = struct.pack("<I", len(hb)) + hb \
         + struct.pack("<I", len(clean)) + clean \
         + struct.pack("<I", len(info)) + info

    print(f"[REFRESH:{tag}] reason={reason}  next_wake={wire['next_wake']}s  changed={wire['changed']}  "
          f"{header.get('filename','?')}  {header.get('side_caption','')[:20]}")
    _touch()
    return Response(body, mimetype="application/octet-stream")


def _error_response(msg):
    hdr = {"ok": False, "error": msg, "next_wake": seconds_until_next()}
    return _packed_response(hdr)


def _packed_response(header):
    hb = json.dumps(header, ensure_ascii=False).encode("utf-8")
    return Response(struct.pack("<I", len(hb)) + hb, mimetype="application/octet-stream")


@app.route("/firmware/<filename>")
def serve_firmware(filename):
    """OTA 固件下载"""
    try:
        fp = _safe_join(Path(FW_DIR), filename)
    except ValueError:
        return {"ok": False, "error": "invalid path"}, 400
    if not fp.exists() or not fp.is_file():
        return {"ok": False, "error": "firmware not found"}, 404
    return send_file(fp, mimetype="application/octet-stream")


@app.route("/health")
def health():
    s = silence_seconds()
    m = max_silence_seconds()
    fw = _latest_firmware()
    return {
        "ok": True,
        "next_wake": seconds_until_next(),
        "last_contact_sec": s,
        "max_silence_sec": m,
        "esp32_alive": s < m,
        "firmware": fw[0] if fw else None,
    }


# ═══════════════════════════════════════════════════
# V8 管理面板 — API
# ═══════════════════════════════════════════════════

import io
import numpy as np


def _fb_to_png(fb_bytes, palette=None):
    """384KB 6色索引 framebuffer → PNG bytes"""
    if palette is None:
        palette = _PALETTE
    arr = np.frombuffer(fb_bytes, dtype=np.uint8).reshape(800, 480)
    rgb = np.zeros((800, 480, 3), dtype=np.uint8)
    for idx, color in enumerate(palette):
        mask = arr == idx
        rgb[mask] = color
    from PIL import Image
    img = Image.fromarray(rgb, "RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _lookup_photo_path_in_db(path):
    """查 photo_scores.db 中指定 path 的记录，返回 sqlite3.Row 或 None"""
    import sqlite3
    db_path = os.path.join(SCRIPT_DIR, "data", "photo_scores.db")
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM photo_scores WHERE path=?", (path,))
        row = c.fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None


def _get_db_stats():
    """数据库统计信息"""
    import sqlite3
    db_path = os.path.join(SCRIPT_DIR, "data", "photo_scores.db")
    if not os.path.exists(db_path):
        return {"total": 0}
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        total = c.execute("SELECT COUNT(*) FROM photo_scores").fetchone()[0]
        scored = c.execute("SELECT COUNT(*) FROM photo_scores WHERE memory_score IS NOT NULL").fetchone()[0]
        usable = c.execute("SELECT COUNT(*) FROM photo_scores WHERE memory_score >= 40").fetchone()[0]
        caption_ready = c.execute(
            "SELECT COUNT(*) FROM photo_scores WHERE side_caption IS NOT NULL AND side_caption != ''"
        ).fetchone()[0]
        avg_mem = c.execute("SELECT AVG(memory_score) FROM photo_scores WHERE memory_score IS NOT NULL").fetchone()[0]
        avg_beau = c.execute("SELECT AVG(beauty_score) FROM photo_scores WHERE beauty_score IS NOT NULL").fetchone()[0]

        # 评分分布: 10 分一档 (0-9, 10-19, ..., 90-100)
        def _dist(col):
            rows = c.execute(
                f"SELECT CAST(ROUND({col}) / 10 AS INTEGER) * 10 AS bucket, COUNT(*) AS cnt "
                f"FROM photo_scores WHERE {col} IS NOT NULL "
                f"GROUP BY bucket ORDER BY bucket"
            ).fetchall()
            dist = {}
            for r in rows:
                dist[f"{int(r[0])}"] = r[1]
            return dist

        pending_score = c.execute(
            "SELECT COUNT(*) FROM photo_scores WHERE memory_score IS NULL AND (vlm_model IS NULL OR vlm_model = '' OR vlm_model = 'failed')"
        ).fetchone()[0]
        memory_dist = _dist("memory_score")
        beauty_dist = _dist("beauty_score")
        conn.close()
        return {
            "total": total,
            "scored": scored,
            "usable": usable,
            "caption_ready": caption_ready,
            "pending_score": pending_score,
            "avg_memory": round(avg_mem, 1) if avg_mem else 0,
            "avg_beauty": round(avg_beau, 1) if avg_beau else 0,
            "memory_dist": memory_dist,
            "beauty_dist": beauty_dist,
        }
    except Exception as e:
        return {"total": 0, "error": str(e)}


@app.route("/admin")
def admin_panel():
    """V8 管理面板页面"""
    return render_template("admin.html")


@app.route("/api/config", methods=["GET", "PUT"])
def api_config():
    """获取/更新系统配置"""
    global cfg
    if request.method == "GET":
        return {
            "refresh_times": cfg["REFRESH_TIMES"],
            "server_port": cfg["SERVER_PORT"],
            "nas_path": cfg["NAS_PATH"],
            "vlm_model": cfg["VLM_MODEL"],
            "deepseek_model": cfg["DEEPSEEK_MODEL"],
            "esp32_ip": cfg["ESP32_IP"],
            "active_font": cfg.get("ACTIVE_FONT", "默认字体.TTF"),
        }

    # PUT: 更新配置
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"ok": False, "error": "无效的 JSON"}), 400

    updates = {}
    # 允许修改的键 (只暴露安全字段)
    ALLOWED_KEYS = {"REFRESH_TIMES", "SERVER_PORT", "ESP32_IP", "ESP32_PORT", "ACTIVE_FONT"}
    for key in ALLOWED_KEYS:
        if key in data:
            updates[key] = data[key]

    if not updates:
        return jsonify({"ok": False, "error": "没有可更新的字段"}), 400

    config.save(updates)
    # 重新加载全局 cfg
    cfg = config.load(force=True)
    return jsonify({"ok": True, "updated": list(updates.keys())})


FONTS_DIR = os.path.join(SCRIPT_DIR, "static", "fonts")


@app.route("/api/fonts")
def api_fonts():
    """列出 static/fonts/ 中可用的字体文件"""
    available = []
    if os.path.isdir(FONTS_DIR):
        for f in sorted(os.listdir(FONTS_DIR)):
            if f.lower().endswith((".ttf", ".ttc", ".otf")):
                size = os.path.getsize(os.path.join(FONTS_DIR, f))
                available.append({"name": f, "size": size})
    active = cfg.get("ACTIVE_FONT", "默认字体.TTF")
    return {"fonts": available, "active": active}


@app.route("/api/status")
def api_status():
    """系统状态"""
    s = silence_seconds()
    m = max_silence_seconds()
    fw = _latest_firmware()
    uptime_sec = int((datetime.now() - _server_start).total_seconds())
    return {
        "ok": True,
        "uptime_sec": uptime_sec,
        "last_contact_sec": s,
        "max_silence_sec": m,
        "esp32_alive": s < m,
        "esp32_version": _esp32_version or "未上报",
        "latest_firmware": fw[0] if fw else None,
        "latest_firmware_size": fw[2] if fw else 0,
        "next_wake_sec": seconds_until_next(),
    }


@app.route("/api/stats")
def api_stats():
    """数据库统计"""
    return _get_db_stats()


@app.route("/api/current-photo")
def api_current_photo():
    """当前照片元数据"""
    _load_cache()
    if _last_render is None:
        return jsonify({"ok": False, "error": "没有缓存照片"}), 404

    h = _last_render["header"]
    info = {
        "ok": True,
        "filename": h.get("filename", ""),
        "path": h.get("path", ""),
        "side_caption": h.get("side_caption", ""),
        "city": h.get("city", ""),
        "date": h.get("date", ""),
        "camera": h.get("camera", ""),
        "mem": h.get("mem", 0),
        "beau": h.get("beau", 0),
        "render_ts": h.get("_render_ts", ""),
    }

    # 从 DB 补充数据库路径 (若 header 中 path 不存在)
    if not info["path"]:
        db_path = os.path.join(SCRIPT_DIR, "data", "photo_scores.db")
        if os.path.exists(db_path):
            import sqlite3
            try:
                conn = sqlite3.connect(db_path)
                c = conn.cursor()
                c.execute(
                    "SELECT path FROM photo_scores WHERE filename=?",
                    (info["filename"],),
                )
                row = c.fetchone()
                if row:
                    info["path"] = row[0]
                conn.close()
            except Exception:
                pass

    return info


@app.route("/api/current-photo/image")
def api_current_photo_image():
    """当前照片 PNG (variant=clean|info)"""
    _load_cache()
    if _last_render is None:
        return jsonify({"ok": False, "error": "没有缓存"}), 404

    variant = request.args.get("variant", "clean")
    fb_key = "clean" if variant == "clean" else "info"
    fb = _last_render.get(fb_key)
    if fb is None:
        return jsonify({"ok": False, "error": f"{variant} 帧缓存不存在"}), 404

    png = _fb_to_png(fb)
    return Response(png, mimetype="image/png",
                    headers={"Cache-Control": "no-store"})


@app.route("/api/current-photo/caption", methods=["POST"])
def api_current_photo_caption():
    """重新生成当前照片附言 → 更新 DB + 重渲染 info.fb 缓存"""
    _load_cache()
    if _last_render is None:
        return jsonify({"ok": False, "error": "没有缓存照片"}), 404

    h = _last_render["header"]
    photo_path = h.get("path", "")

    # 如果没有 path, 尝试按 filename 从 DB 查
    if not photo_path:
        db_path = os.path.join(SCRIPT_DIR, "data", "photo_scores.db")
        if os.path.exists(db_path):
            import sqlite3
            try:
                conn = sqlite3.connect(db_path)
                c = conn.cursor()
                c.execute(
                    "SELECT path FROM photo_scores WHERE filename=?",
                    (h.get("filename", ""),),
                )
                row = c.fetchone()
                if row:
                    photo_path = row[0]
                conn.close()
            except Exception:
                pass

    if not photo_path:
        return jsonify({"ok": False, "error": "无法确定当前照片路径"}), 400

    full_path = os.path.join(cfg["NAS_PATH"], photo_path)
    if not os.path.exists(full_path):
        return jsonify({"ok": False, "error": f"照片文件不存在: {full_path}"}), 404

    # 取附言: 优先用请求体中的 text (手动编辑), 否则 LLM 生成
    try:
        raw = request.get_data() or b"{}"
        data = json.loads(raw)
    except Exception:
        data = {}
    manual_text = data.get("text", "")

    if manual_text:
        new_caption = manual_text.strip()
        if not new_caption:
            return jsonify({"ok": False, "error": "附言不能为空"}), 400
    else:
        # 生成新附言
        try:
            import send_image
            new_caption = send_image.generate_side_caption(
                full_path, caption=h.get("caption"), photo_type=None
            )
        except Exception as e:
            return jsonify({"ok": False, "error": f"附言生成异常: {e}"}), 500

    if not new_caption:
        return jsonify({"ok": False, "error": "附言生成失败 (API 返回空)"}), 500

    # 更新数据库
    try:
        import sqlite3
        db_path = os.path.join(SCRIPT_DIR, "data", "photo_scores.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE photo_scores SET side_caption=? WHERE path=?",
            (new_caption, photo_path),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[CAPTION] DB 更新失败: {e}")

    # 重渲染 info.fb 缓存
    try:
        import send_image as si
        from PIL import Image, ImageOps

        img = Image.open(full_path)
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        ow, oh = img.size
        rotated = ow > oh

        city = h.get("city", "")
        dt = h.get("date", "")
        if dt:
            dt = dt.replace("-", ".")
        cam = h.get("camera", "")
        mem = h.get("mem", 0)
        beau = h.get("beau", 0)

        # 构造底栏 (复用 send_image 的 _split_caption_source)
        side_text, side_src = si._split_caption_source(new_caption)
        info_lines = [(city, 20, dt, mem, beau)]
        if cam:
            info_lines.append((cam, 14, ""))
        info_lines.append((side_text, 20, "", 0, 0, side_src))

        info_img = si.process_info(img, ow, oh, rotated, info_lines, no_text=True)
        info_fb = si.quantize(info_img)
        info_fb = si._overlay_solid_text(info_fb, info_lines)

        # 覆盖缓存
        _last_render["info"] = info_fb
        _last_render["header"]["side_caption"] = new_caption
        _save_cache()

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"重渲染失败: {e}"}), 500

    print(f"[CAPTION] 已更新: {photo_path} → {new_caption[:40]}")
    return {"ok": True, "side_caption": new_caption}


@app.route("/api/selection-params", methods=["GET", "PUT"])
def api_selection_params():
    """获取/更新选图策略参数"""
    import send_image
    if request.method == "GET":
        return send_image._load_selection_params(force=True)

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"ok": False, "error": "无效的 JSON"}), 400

    ok = send_image._save_selection_params(data)
    if ok:
        return jsonify({"ok": True})
    else:
        return jsonify({"ok": False, "error": "保存失败"}), 500


# ─── 扫描新照片 (只入DB，不VLM) ───
_scan_state = {"running": False, "total": 0, "done": 0, "current": "", "error": ""}

# ─── VLM 评分 (只跑待评分照片) ───
_score_state = {"running": False, "total": 0, "done": 0, "current": "", "error": ""}


def _filter_unscored_raw(conn, paths):
    """只找完全不在 DB 中的文件路径 (不依赖 memory_score)"""
    import sqlite3
    c = conn.cursor()
    known = set()
    for i in range(0, len(paths), 500):
        chunk = paths[i:i+500]
        placeholders = ",".join("?" for _ in chunk)
        rows = c.execute(
            f"SELECT path FROM photo_scores WHERE path IN ({placeholders})", chunk
        ).fetchall()
        known.update(r[0] for r in rows)
    return [p for p in paths if p not in known]


@app.route("/api/scan", methods=["POST"])
def api_scan_start():
    """后台扫描：发现新文件 → 提取EXIF → 入库 (不调VLM)"""
    global _scan_state
    if _scan_state["running"]:
        return jsonify({"ok": False, "error": "扫描已在运行"}), 400

    import analyze_image
    import threading

    _scan_state = {"running": True, "total": 0, "done": 0, "current": "", "error": ""}

    def _worker():
        global _scan_state
        db_lock = threading.Lock()
        try:
            conn = analyze_image.init_db()
            base = cfg["NAS_PATH"]
            all_files = analyze_image.scan_directory(base)
            new_files = _filter_unscored_raw(conn, all_files)
            _scan_state["total"] = len(new_files)
            if not new_files:
                print("[SCAN] 没有新照片")
                return

            for i, p in enumerate(new_files):
                _scan_state["current"] = p
                _scan_state["done"] = i
                try:
                    analyze_image.analyze_one(p, conn, use_vlm=False,
                                              base_dir=base, db_lock=db_lock)
                except Exception as ex:
                    print(f"[SCAN] 入库失败: {p} - {ex}")
                if i % 10 == 0:
                    _scan_state["done"] = i
            _scan_state["done"] = len(new_files)
            conn.close()
        except Exception as e:
            _scan_state["error"] = str(e)
        finally:
            _scan_state["running"] = False
            _scan_state["current"] = ""

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "扫描已开始"})


@app.route("/api/scan/status")
def api_scan_status():
    return {
        "running": _scan_state["running"],
        "total": _scan_state["total"],
        "done": _scan_state["done"],
        "current": _scan_state["current"],
        "error": _scan_state["error"],
    }


@app.route("/api/score", methods=["POST"])
def api_score_start():
    """后台评分：对 DB 中 memory_score IS NULL 的照片跑 VLM"""
    global _score_state
    if _score_state["running"]:
        return jsonify({"ok": False, "error": "评分已在运行"}), 400

    import analyze_image
    import sqlite3
    import threading

    _score_state = {"running": True, "total": 0, "done": 0, "current": "", "error": ""}

    def _worker():
        global _score_state
        db_lock = threading.Lock()
        try:
            conn = analyze_image.init_db()
            c = conn.cursor()
            # 只选从未评过的 (排除 vlm_model='failed' 防止反复重试)
            rows = c.execute(
                """SELECT path FROM photo_scores
                   WHERE memory_score IS NULL
                     AND (vlm_model IS NULL OR vlm_model = '')"""
            ).fetchall()
            paths = [r[0] for r in rows]
            _score_state["total"] = len(paths)
            if not paths:
                print("[SCORE] 没有待评分的照片")
                return

            base = cfg["NAS_PATH"]
            for i, p in enumerate(paths):
                _score_state["current"] = p
                _score_state["done"] = i
                try:
                    analyze_image.analyze_one(p, conn, use_vlm=True,
                                              base_dir=base, db_lock=db_lock)
                    # VLM 失败的照片标记为 failed，防止反复重试
                    if db_lock: db_lock.acquire()
                    try:
                        c.execute("UPDATE photo_scores SET vlm_model='failed' WHERE path=? AND memory_score IS NULL", (p,))
                        conn.commit()
                    finally:
                        if db_lock: db_lock.release()
                except Exception as ex:
                    print(f"[SCORE] 评分失败: {p} - {ex}")
                if i % 5 == 0:
                    _score_state["done"] = i
            _score_state["done"] = len(paths)
            conn.close()
        except Exception as e:
            _score_state["error"] = str(e)
        finally:
            _score_state["running"] = False
            _score_state["current"] = ""

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "评分已开始"})


@app.route("/api/score/status")
def api_score_status():
    return {
        "running": _score_state["running"],
        "total": _score_state["total"],
        "done": _score_state["done"],
        "current": _score_state["current"],
        "error": _score_state["error"],
    }


@app.route("/api/score/retry-failed", methods=["POST"])
def api_score_retry():
    """用回落模型重试 vlm_model='failed' 的照片"""
    import analyze_image
    import sqlite3
    import threading

    # 找到失败的照片
    db_path = os.path.join(SCRIPT_DIR, "data", "photo_scores.db")
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    rows = c.execute(
        "SELECT path FROM photo_scores WHERE vlm_model='failed' AND memory_score IS NULL"
    ).fetchall()
    conn.close()

    if not rows:
        return jsonify({"ok": True, "message": "没有失败照片需要重试"})

    paths = [r[0] for r in rows]

    def _worker():
        """强制用回落模型重试"""
        fallback = cfg["VLM_MODEL_FALLBACK"]  # "qwen3.5-plus"
        # 临时替换 analyze_image 的 VLM_MODEL
        original = analyze_image.VLM_MODEL
        analyze_image.VLM_MODEL = fallback
        try:
            conn2 = sqlite3.connect(db_path)
            db_lock = threading.Lock()
            for p in paths:
                print(f"[RETRY] 用 {fallback} 重试: {p}")
                try:
                    analyze_image.analyze_one(p, conn2, use_vlm=True,
                                              base_dir=cfg["NAS_PATH"], db_lock=db_lock)
                    # 如果还是 NULL，保持 failed 标记
                    c2 = conn2.cursor()
                    c2.execute(
                        "UPDATE photo_scores SET vlm_model='failed' WHERE path=? AND memory_score IS NULL",
                        (p,),
                    )
                    conn2.commit()
                except Exception as ex:
                    print(f"[RETRY] 重试失败: {p} - {ex}")
            conn2.close()
        finally:
            analyze_image.VLM_MODEL = original

    import threading as _th
    t = _th.Thread(target=_worker, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": f"已开始用 {cfg['VLM_MODEL_FALLBACK']} 重试 {len(paths)} 张"})


def _startup_check():
    """校验关键配置 — 无论直接运行还是被 gunicorn/systemd 以模块导入都会执行。
    NAS_PATH 缺失/无效视为致命 (SystemExit); 数据库缺失只告警。"""
    if not cfg.get("NAS_PATH"):
        raise SystemExit("[ERROR] NAS_PATH 未配置! 请在 config.env 中设置, 例: NAS_PATH = /path/to/photos")
    if not os.path.isdir(cfg["NAS_PATH"]):
        raise SystemExit(f"[ERROR] NAS_PATH 目录不存在: {cfg['NAS_PATH']}")
    db_path = os.path.join(SCRIPT_DIR, "data", "photo_scores.db")
    if not os.path.exists(db_path):
        print(f"[WARN] 数据库不存在: {db_path}")
        print("       请先运行: python analyze_image.py")


# 模块加载即校验 (覆盖 gunicorn/systemd 等非 __main__ 启动方式)
_startup_check()


if __name__ == "__main__":
    _log_path = _setup_file_logging()
    print(f"[SERVER] log -> {_log_path}")
    print(f"[SERVER] InkEPD server on {cfg['SERVER_HOST']}:{cfg['SERVER_PORT']}")
    print(f"[SERVER] NAS path: {cfg['NAS_PATH']}")
    print(f"[SERVER] Refresh times: {cfg['REFRESH_TIMES']}")
    print(f"[SERVER] Next wake in {seconds_until_next()} seconds")

    # 后台预热 BiRefNet: 提前把分割模型加载进内存, 消除"首张横图慢"。
    # 用线程, 不阻塞端口监听 (预热那几秒 ESP32 仍能连上, 顶多首请求走缓存)。
    def _warm_rembg():
        try:
            import send_image
            if send_image._rembg_session() is not None:
                print("[SERVER] rembg 预热完成")
            fc = send_image._face_cascade()
            if fc is not None:
                print("[SERVER] face cascade 预热完成")
        except Exception as e:
            print(f"[SERVER] BiRefNet 预热跳过: {e}")
    import threading
    threading.Thread(target=_warm_rembg, daemon=True).start()

    # threaded=True: 慢请求(BiRefNet ~10s)不阻塞其他连接的 accept,
    # 减少固件在推理期间重连导致的 -5 连接丢失 (单设备串行请求, 竞态风险低)
    app.run(host=cfg["SERVER_HOST"], port=cfg["SERVER_PORT"], debug=False, threaded=True)
