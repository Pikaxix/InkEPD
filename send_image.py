"""
服务端全渲染: 图片处理 + 中文文字 + 上传到 ESP32

流程:
  1. 读取图片
  2. 横图? 生成两个版本:
     a) /current.fb  → 旋转90°填满 480×800 (纯图, 默认)
     b) /current_i.fb → 智能裁切保持横图 + PIL 画中文底栏 (有信息时)
  3. 竖图? 生成两个版本:
     a) /current.fb  → 裁切填满 480×800 (纯图)
     b) /current_i.fb → 同一张图 + PIL 画中文底栏
  4. 分别 HTTP 上传到 ESP32

用法:
  python send_image.py <ESP_IP> <图片路径>

依赖:
  pip install pillow pillow-heif numpy
"""

import sys, os, io, re, base64, json, time, urllib.request, numpy as np, math, sqlite3, datetime, random
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageFilter, ImageOps
import pillow_heif
import config
pillow_heif.register_heif_opener()

try:
    import cv2
except ImportError:
    cv2 = None

# ─── 配置 ───
TARGET_W, TARGET_H = 480, 800
BTM_H = 110

# (城市/GPS 查询逻辑已移除: v7 渲染只用 photo_scores.db 里预先算好的 city 字段,
#  EXIF→城市的离线查找只在 analyze_image.py 入库时执行)

# 6 色调色板 (GDEY073D46 Spectra 6, 索引 = hw 代码)
PALETTE_RGB = [
    (30,  25,  50),   # 0 → hw 0x00 BLACK (黑)
    (255, 255, 255),  # 1 → hw 0x01 WHITE (白)
    (255, 230, 50),   # 2 → hw 0x02 YELLOW (黄)
    (200, 100, 100),  # 3 → hw 0x03 RED (红)
    (50,  80,  200),  # 4 → hw 0x05 BLUE (蓝)
    (100, 200, 100),  # 5 → hw 0x06 GREEN (绿)
]
# PIL quantize 输出索引 0-5, 与 ESP32 pixelToColor 值一致, 直接透传
PALETTE_INDEX = [0, 1, 2, 3, 4, 5]
BLACK_RGB = PALETTE_RGB[0]

# 调色板平铺 (PIL 要求 768 bytes)
palette_flat = []
for rgb in PALETTE_RGB:
    palette_flat.extend(rgb)
while len(palette_flat) < 768:
    palette_flat.extend(BLACK_RGB)

# ─── 底栏文字 (默认, 无附言/EXIF 读取失败时用) ───
_DEFAULT_NOTICE = "今天也要好好的。"
DEFAULT_INFO_LINES = [
    ("未知地点", 20, "", 0, 0),
    ("Nikon D7200  ISO100  f/2.8  50mm", 14, ""),
    (_DEFAULT_NOTICE, 20, ""),
]

# ─── 拍摄设备显示名称 (从 res/camera_names.csv 加载) ───
_CAMERA_NAMES = {}
_camera_csv = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "camera_names.csv")
if os.path.exists(_camera_csv):
    with open(_camera_csv, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("Make|"):
                continue
            parts = line.split("|", 2)
            if len(parts) == 3:
                _CAMERA_NAMES[(parts[0].strip(), parts[1].strip())] = parts[2].strip()
    print(f"[CAMERA] 已加载 {len(_CAMERA_NAMES)} 个设备名称")
else:
    print(f"[CAMERA] 名称表不存在: {_camera_csv}")


from functools import lru_cache

# load_font 手动缓存 (键=(size, font_name), 换字体自动刷新)
_font_cache = {}

# ─── 公共图像编码 (打分/附言共用, 统一参数) ───
def encode_image_b64(path, max_long_edge=1024, quality=85):
    """读图 → EXIF方向校正 → 等比缩到长边 → RGB → JPEG → base64。失败返回 None"""
    try:
        img = ImageOps.exif_transpose(Image.open(path))
        w, h = img.size
        if max(w, h) > max_long_edge:
            s = max_long_edge / max(w, h)
            img = img.resize((int(w * s), int(h * s)), Image.LANCZOS)
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        print(f"  [编码] 图片处理失败: {e}")
        return None



def load_font(size):
    """加载字体 (手动缓存, 键=(size,font_name), 换字体自动失效)"""
    global _font_cache
    script_dir = os.path.dirname(os.path.abspath(__file__))
    import config as _cfg
    fname = _cfg.load().get("ACTIVE_FONT", "默认字体.TTF")
    key = (size, fname)
    if key in _font_cache:
        return _font_cache[key]
    fp = os.path.join(script_dir, "static", "fonts", fname)
    font = None
    if os.path.exists(fp):
        font = ImageFont.truetype(fp, size)
    else:
        for alt in ["默认字体.TTF", "msyh.ttc", "simsun.ttc"]:
            ap = os.path.join(script_dir, "static", "fonts", alt)
            if os.path.exists(ap):
                font = ImageFont.truetype(ap, size)
                break
        if font is None:
            font = ImageFont.load_default()
    _font_cache[key] = font
    return font


def load_font_default(size):
    """加载默认字体 (默认字体.TTF)，不受 ACTIVE_FONT 影响，用于日期/评分/设备等西文"""
    global _font_cache
    script_dir = os.path.dirname(os.path.abspath(__file__))
    key = (size, "默认字体.TTF")
    if key in _font_cache:
        return _font_cache[key]
    fp = os.path.join(script_dir, "static", "fonts", "默认字体.TTF")
    if os.path.exists(fp):
        font = ImageFont.truetype(fp, size)
    else:
        font = ImageFont.load_default()
    _font_cache[key] = font
    return font


@lru_cache(maxsize=1)
@lru_cache(maxsize=1)
def _rembg_session():
    """惰性创建 rembg session (lru_cache 确保全程只建一次, 否则每次调用新建
    会话→onnxruntime 内存累积→泄漏到数 GB); 不可用时返回 None。
    用 u2netp(~4MB): 内存仅 ~250MB(BiRefNet-general 要 ~1GB), 推理 0.3~0.7s/张
    (BiRefNet 横图要 9s)。bbox 居中策略下裁切质量实测接近 BiRefNet, 且横图秒出、
    传输时间短不易超时。复杂场景 mask 略粗, 但只取主体大致位置, 影响小, 有梯度法回退。"""
    try:
        from rembg import new_session
        return new_session("u2netp")
    except Exception as e:
        print(f"[CROP] rembg 不可用, 回退梯度法: {e}")
        return None


@lru_cache(maxsize=1)
def _face_cascade():
    """惰性加载 OpenCV Haar 人脸级联分类器; 不可用时返回 None (触发回退)。
    仿 _rembg_session 模式: 惰性、缓存、容错, 首次 ~200ms 后续零开销。"""
    if cv2 is None:
        return None
    try:
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        if not os.path.exists(cascade_path):
            print("[FACE] Haar cascade 文件不存在, 回退")
            return None
        return cv2.CascadeClassifier(cascade_path)
    except Exception as e:
        print(f"[FACE] 级联加载失败, 回退: {e}")
        return None


def _detect_faces(im_rgb, min_size=60):
    """在已缩放到高~800 的临时图上检测人脸。
    返回 [(x,y,w,h), ...]; 无人脸或级联不可用返回 []。
    min_size=60: 800px 图上小于 60px 的人脸对构图无意义, 也过滤背景纹理误检。
    scaleFactor=1.1, minNeighbors=5: 偏保守, 低误检率 (鲁棒性优先)。"""
    cascade = _face_cascade()
    if cascade is None:
        return []
    try:
        gray = np.array(im_rgb.convert("L"), dtype=np.uint8)
        faces = cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5,
            minSize=(min_size, min_size)
        )
        if len(faces) == 0:
            return []
        print(f"[FACE] 检测到 {len(faces)} 张人脸 @ {faces.tolist()}")
        return faces.tolist()
    except Exception as e:
        print(f"[FACE] 检测失败, 回退: {e}")
        return []


def _face_center_x(faces, nw2):
    """从人脸列表计算水平居中锚点 (裁切窗口左上角 x)。
    多脸: 取包围盒中心, 不取均值 — 防止把边缘的人切出画面。
    返回 None 表示无人脸。"""
    if not faces:
        return None
    left = min(f[0] for f in faces)
    right = max(f[0] + f[2] for f in faces)
    center = (left + right) / 2.0
    bx = int(round(center - TARGET_W / 2))
    return max(0, min(bx, nw2 - TARGET_W))


def _face_center_y(faces, nh2):
    """从人脸列表计算垂直居中锚点 (裁切窗口左上角 y)。
    多脸: 取所有人脸垂直中心的中位数 — 抗离群 (一人远离群组不拖偏整体)。
    返回 None 表示无人脸。"""
    if not faces:
        return None
    centers = [f[1] + f[3] / 2.0 for f in faces]
    median_y = sorted(centers)[len(centers) // 2]
    by = int(round(median_y - TARGET_H / 2))
    return max(0, min(by, nh2 - TARGET_H))


def _subject_col_weights(im_rgb):
    """返回按列的前景质量数组(长度=im宽); rembg 不可用或无主体时返回 None。"""
    sess = _rembg_session()
    if sess is None:
        return None
    try:
        from rembg import remove
        _t = time.time()
        mask = remove(im_rgb, only_mask=True, session=sess)  # PIL 'L'
        print(f"[TIMING] 主体分割推理 {int((time.time()-_t)*1000)}ms (输入 {im_rgb.size})")
        m = np.asarray(mask, dtype=np.float32)               # H×W, 0~255
        if m.max() < 30:        # 几乎全黑 = 没识别到主体(纯风景) → 回退
            return None
        return m.sum(axis=0)    # 每列前景像素强度和
    except Exception as e:
        print(f"[CROP] 分割失败, 回退: {e}")
        return None


def _bbox_center_x(col, nw2):
    """主体包围盒(列方向)居中: 用峰值5%阈值定前景左右边界, 中心对齐裁切窗口中心。
    比"最大化窗口内前景像素和"更稳 — 后者会被主体密集的一侧吸走、切掉另一半。
    返回裁切窗口左上角 x (已 clamp 到 [0, nw2-TARGET_W])。"""
    if col is None or col.sum() <= 0:
        return (nw2 - TARGET_W) // 2
    idx = np.where(col > col.max() * 0.05)[0]
    if len(idx) == 0:
        return (nw2 - TARGET_W) // 2
    center = (int(idx[0]) + int(idx[-1])) / 2.0
    bx = int(round(center - TARGET_W / 2))
    return max(0, min(bx, nw2 - TARGET_W))


def process_base(img, ow, oh, rotated, rotate_landscape=False):
    """生成基础画面 (等比例缩放 + 裁切填满 480×800)
    横图 + rotate_landscape=True (纯图/隐藏信息栏): 旋转90°完整显示, 居中裁切
    横图 + rotate_landscape=False (带信息栏): 保持横构图 + 智能水平裁切
    竖图: 缩放填满 + 居中裁切 (两种状态一致)
    """
    def _fill_center(im, w, h):
        s = max(TARGET_W / w, TARGET_H / h)
        nw, nh = int(w * s), int(h * s)
        return im.resize((nw, nh), Image.LANCZOS).crop(
            ((nw - TARGET_W) // 2, (nh - TARGET_H) // 2,
             (nw + TARGET_W) // 2, (nh + TARGET_H) // 2))

    if rotated and rotate_landscape:
        # 纯图: 横图旋转90° → 当作竖图填满居中裁切 (类 V5)
        im = img.rotate(90, expand=True)
        return _fill_center(im, im.width, im.height)

    if rotated:
        s2 = TARGET_H / oh
        nw2, nh2 = int(ow * s2), TARGET_H
        tmp = img.resize((nw2, nh2), Image.LANCZOS)

        if nw2 <= TARGET_W:
            best_x = (nw2 - TARGET_W) // 2   # 居中 (过窄时为负, 两侧留白边)
            print(f"[智能] 横图过窄({nw2}px)，居中")
        else:
            faces = _detect_faces(tmp)          # 人脸检测优先 (~50ms, 无人脸则回退)
            if faces:
                best_x = _face_center_x(faces, nw2)
                print(f"[智能] 人脸裁切 x={best_x} (Haar Cascade)")
            else:
                col = _subject_col_weights(tmp)   # BiRefNet 主体分割
                if col is not None:
                    best_x = _bbox_center_x(col, nw2)   # 主体包围盒居中
                    print(f"[智能] 主体裁切 x={best_x} (主体分割bbox居中)")
                else:
                    # 回退: 边缘梯度密度 + 居中高斯 (rembg 不可用/无主体)
                    gray = np.array(tmp.convert("L"), dtype=np.float32)
                    grad = np.abs(np.diff(gray, axis=1))
                    grad = np.pad(grad, ((0, 0), (0, 1)), mode='edge')

                    best_x, best_score = 0, -1
                    cx = nw2 / 2.0
                    sigma = nw2 * 0.35

                    for x in range(0, nw2 - TARGET_W + 1, 10):
                        window = grad[:, x:x + TARGET_W]
                        edge_score = float(np.mean(window))
                        center_dist = abs((x + TARGET_W / 2) - cx)
                        gauss = math.exp(-(center_dist ** 2) / (2 * sigma ** 2))
                        score = edge_score * gauss
                        if score > best_score:
                            best_score, best_x = score, x
                    print(f"[智能] 横图裁切 x={best_x}, score={best_score:.1f}")
        im = tmp.crop((best_x, 0, best_x + TARGET_W, TARGET_H))
    else:
        # 竖图: 人脸检测驱动垂直+水平居中, 无人脸回退几何居中 (与原来 _fill_center 一致)
        s = max(TARGET_W / ow, TARGET_H / oh)
        nw, nh = int(ow * s), int(oh * s)
        tmp_v = img.resize((nw, nh), Image.LANCZOS)
        faces = _detect_faces(tmp_v)
        if faces:
            bx = _face_center_x(faces, nw) if nw > TARGET_W else (nw - TARGET_W) // 2
            by = _face_center_y(faces, nh)
            bx = max(0, min(bx, nw - TARGET_W)) if nw > TARGET_W else bx
            by = max(0, min(by, nh - TARGET_H)) if nh > TARGET_H else (nh - TARGET_H) // 2
            print(f"[智能] 竖图人脸居中对齐 face_cx={bx+TARGET_W//2}, face_cy={by+TARGET_H//2}")
        else:
            bx, by = (nw - TARGET_W) // 2, (nh - TARGET_H) // 2
        im = tmp_v.crop((bx, by, bx + TARGET_W, by + TARGET_H))
    return im


def process_clean(img, ow, oh, rotated):
    """生成纯图 (无信息栏): 横图旋转90°完整显示"""
    return process_base(img, ow, oh, rotated, rotate_landscape=True)


def process_info(img, ow, oh, rotated, info_lines, no_text=False):
    """生成带文图 (基础画面 + 底部信息栏): 横图智能裁切保持横构图。
    no_text=True 只画暗条不画字 (供 _overlay_solid_text 使用, 避免双层文字导致重影)"""
    im = process_base(img, ow, oh, rotated, rotate_landscape=False)
    return draw_text(im, info_lines, no_text=no_text)


def _split_caption_source(text):
    """拆出附言正文与出处后缀 (出处以 — 或 —— 起头)。返回 (正文, 出处含破折号)；无出处则出处为 ''
    正文末尾若无结束标点则补中文句号「。」(不动出处, 不改变歌名/歌手)。"""
    if not text:
        return text, ""
    for sep in ("——", "—"):
        idx = text.find(sep)
        if idx > 0:  # 破折号前需有正文
            return _ensure_period(text[:idx].rstrip()), text[idx:].strip()
    return _ensure_period(text), ""


# 正文已有这些结尾标点之一就不再补句号 (含中英文句末标点及省略号/引号收尾)
_CAPTION_END_PUNCT = "。！？!?…．.~～、，,；;：:”’\"'）)》」』】"

def _ensure_period(body):
    """正文非空且不以结束标点结尾时, 补一个中文句号。"""
    if not body:
        return body
    return body if body[-1] in _CAPTION_END_PUNCT else body + "。"


def draw_text(img, info_lines=None, no_text=False):
    """在图片底部画渐变暗条 + 白字。no_text=True 时只画暗条不画字(供 overlay_solid 使用)"""
    if info_lines is None:
        info_lines = DEFAULT_INFO_LINES

    canvas = img.copy().convert("RGBA")
    draw = ImageDraw.Draw(canvas)

    # 渐变暗条: 上沿几乎透明 → 下沿半透黑
    import numpy as np
    grad = np.zeros((BTM_H, TARGET_W, 4), dtype=np.uint8)
    for y in range(BTM_H):
        a = int(15 + (y / BTM_H) * 155)
        grad[y, :, 3] = min(a, 170)
    grad[:, :, :3] = (0, 0, 0)
    canvas.paste(Image.fromarray(grad, "RGBA"), (0, TARGET_H - BTM_H), Image.fromarray(grad, "RGBA"))

    if no_text:
        return canvas.convert("RGB")

    tc = (255, 255, 255)

    def wrap_line(text_str, font_obj, max_px=450):
        """按像素宽度换行, 返回 (line1, line2)，line2 为空表示不换行"""
        total_w = draw.textlength(text_str, font=font_obj)
        if total_w <= max_px:
            return text_str, ""
        n = len(text_str)
        cut = n
        while cut > 0:
            w = draw.textlength(text_str[:cut], font=font_obj)
            if w <= max_px:
                break
            cut -= 1
        if cut <= 0:
            cut = 1
        return text_str[:cut], text_str[cut:]

    y = TARGET_H - BTM_H + 10
    for i, item in enumerate(info_lines):
        text, size = item[0], item[1]
        date_str = item[2] if len(item) > 2 else ""
        mem_score = item[3] if len(item) > 3 else 0
        beauty_score = item[4] if len(item) > 4 else 0
        source = item[5] if len(item) > 5 else ""   # 附言出处, 小字右对齐放第2行
        font = load_font_default(size) if i == 1 else load_font(size)

        line1, line2 = wrap_line(text, font)

        # 第1行
        draw.text((14, y), line1, font=font, fill=tc)
        _, dy_bot = font.getbbox(line1 if line1 else "永")[1::2]

        # 第1行右侧: 日期 (用默认字体)
        if date_str:
            font_date = load_font(18)
            _, dd_bot = font_date.getbbox(date_str)[1::2]
            rx = 14 + draw.textlength(line1, font=font) + (32 if line1 else 0)
            draw.text((rx, y + (dy_bot - dd_bot)), date_str, font=font_date, fill=tc)

        # 评分显示 (用默认字体)
        if i == 0 and (mem_score > 0 or beauty_score > 0):
            score_text = f"回忆:{mem_score:.0f}    美观:{beauty_score:.0f}"
            font_score = load_font(18)
            sw = draw.textlength(score_text, font=font_score)
            sx = TARGET_W - 14 - sw
            _, sb_bot = font_score.getbbox(score_text)[1::2]
            draw.text((sx, y + (dy_bot - sb_bot)), score_text, font=font_score, fill=tc)

        # 第2行: 附言出处优先 (用默认字体); 否则正文换行
        if source:
            y += size + 2
            font_src = load_font(13)
            sw = draw.textlength(source, font=font_src)
            sx = max(14, TARGET_W - 14 - sw)
            draw.text((sx, y), source, font=font_src, fill=tc)
        elif line2:
            y += size + 2
            draw.text((14, y), line2.strip(), font=font, fill=tc)

        y += size + 8

    return canvas.convert("RGB")


def _overlay_solid_text(info_fb, info_lines):
    """在已抖动的 framebuffer 上覆盖纯白色文字（2x渲染+NEAREST降采样，消除抗锯齿过渡）"""
    import numpy as np
    from PIL import Image, ImageDraw

    # 2x 画布渲染文字，降采样后边缘无灰度过渡
    S = 2
    mask = Image.new("L", (TARGET_W * S, TARGET_H * S), 0)
    md = ImageDraw.Draw(mask)

    def _mw(txt, f, mp=450):
        tw = md.textlength(txt, font=f)
        if tw <= mp * S:
            return txt, ""
        n = len(txt)
        c = n
        while c > 0:
            if md.textlength(txt[:c], font=f) <= mp * S:
                break
            c -= 1
        return txt[:max(c, 1)], txt[c:]

    y = (TARGET_H - BTM_H + 10) * S
    for i, item in enumerate(info_lines):
        text, size = item[0], item[1]
        ds = item[2] if len(item) > 2 else ""
        ms = item[3] if len(item) > 3 else 0
        bs = item[4] if len(item) > 4 else 0
        src = item[5] if len(item) > 5 else ""
        font = load_font_default(size * S) if i == 1 else load_font(size * S)
        l1, l2 = _mw(text, font)
        _, dy = font.getbbox(l1 if l1 else "永")[1::2]

        md.text((14 * S, y), l1, font=font, fill=255)
        if ds:
            fd = load_font(18 * S)
            _, dd = fd.getbbox(ds)[1::2]
            rx = 14 * S + md.textlength(l1, font=font) + (32 * S if l1 else 0)
            md.text((rx, y + (dy - dd)), ds, font=fd, fill=255)
        if i == 0 and (ms > 0 or bs > 0):
            st = f"回忆:{ms:.0f}    美观:{bs:.0f}"
            fs = load_font(18 * S)
            sw = md.textlength(st, font=fs)
            sx = TARGET_W * S - 14 * S - sw
            _, sb = fs.getbbox(st)[1::2]
            md.text((sx, y + (dy - sb)), st, font=fs, fill=255)
        if src:
            y += (size + 2) * S
            fsrc = load_font(13 * S)
            sw = md.textlength(src, font=fsrc)
            sx = max(14 * S, TARGET_W * S - 14 * S - sw)
            md.text((sx, y), src, font=fsrc, fill=255)
        elif l2:
            y += (size + 2) * S
            md.text((14 * S, y), l2.strip(), font=font, fill=255)
        y += (size + 8) * S

    # NEAREST 降采样 → 无灰度过渡，纯黑白边缘
    mask_low = mask.resize((TARGET_W, TARGET_H), Image.NEAREST)
    fb = np.frombuffer(info_fb, dtype=np.uint8).copy().reshape(800, 480)
    fb[np.asarray(mask_low) > 0] = 1
    return fb.tobytes()


def quantize(img):
    """图像 → 6 色 framebuffer (Floyd-Steinberg 抖动)

    色彩管线 (V5版本):
      饱和度 1.4x → 高斯模糊+锐化 1.5x → Gamma 1.3 → FS 抖动
    """
    # 1. 色彩预处理 (V5算法)
    img = ImageEnhance.Color(img).enhance(1.4)  # 饱和度增强
    bl = img.filter(ImageFilter.GaussianBlur(1.0))
    img = Image.blend(bl, img, 1.5)  # 锐化

    arr = np.array(img, dtype=np.float32) / 255.0
    arr = arr ** 1.3  # gamma 压暗
    arr = (arr * 255).clip(0, 255).astype(np.uint8)

    # 2. 使用 PIL 内置的 Floyd-Steinberg 抖动
    p_img = Image.new("P", (1, 1))
    p_img.putpalette(palette_flat)
    q = Image.fromarray(arr).quantize(palette=p_img, dither=Image.Dither.FLOYDSTEINBERG)

    # 3. 转换为 framebuffer bytes (向量化: 索引经 LUT 映射, 等价于旧的逐像素循环)
    arr_idx = np.asarray(q, dtype=np.uint8)              # (H, W) 值域 0-5
    lut = np.zeros(256, dtype=np.uint8)
    lut[:len(PALETTE_INDEX)] = PALETTE_INDEX             # 越界索引 → 0 (黑)
    return lut[arr_idx].tobytes()


# ─── 附言生成 ───
_API_KEY = ""
_VLM_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
_VLM_MODEL = "qwen3.7-plus"
_VLM_MODEL_FALLBACK = "qwen3.5-plus"
_DEEPSEEK_KEY = ""
_DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
_DEEPSEEK_MODEL = "deepseek-v4-pro"
_ESP32_IP = ""
_ESP32_PORT = 80
_NAS_PATH = ""
_config_loaded = False

def load_config():
    """从统一的 config.py 填充模块级配置 (保持原有全局变量接口)"""
    global _API_KEY, _VLM_BASE_URL, _VLM_MODEL, _VLM_MODEL_FALLBACK, _ESP32_IP, _ESP32_PORT, _NAS_PATH, _config_loaded
    global _DEEPSEEK_KEY, _DEEPSEEK_URL, _DEEPSEEK_MODEL
    if _config_loaded:
        return True
    c = config.load()
    _API_KEY = c["DASHSCOPE_API_KEY"]
    _VLM_BASE_URL = c["VLM_BASE_URL"]
    _VLM_MODEL = c["VLM_MODEL"]
    _VLM_MODEL_FALLBACK = c["VLM_MODEL_FALLBACK"]
    _DEEPSEEK_KEY = c.get("DEEPSEEK_API_KEY", "")
    _DEEPSEEK_URL = c.get("DEEPSEEK_BASE_URL", _DEEPSEEK_URL)
    _DEEPSEEK_MODEL = c.get("DEEPSEEK_MODEL", _DEEPSEEK_MODEL)
    _ESP32_IP = c["ESP32_IP"]
    _ESP32_PORT = c["ESP32_PORT"]
    _NAS_PATH = c["NAS_PATH"]
    if _API_KEY and _NAS_PATH:
        _config_loaded = True
        return True
    if not _NAS_PATH: print("[WARN] 未配置 NAS_PATH，请在 config.env 中填入 NAS_PATH")
    return False


_SELECTION_PARAMS = None


def _load_selection_params(force=False):
    """从 data/selection_params.json 加载选图策略参数，文件不存在则自动创建默认值"""
    global _SELECTION_PARAMS
    if _SELECTION_PARAMS is not None and not force:
        return _SELECTION_PARAMS
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "selection_params.json")
    defaults = {
        "memory_weight": 0.6,
        "beauty_weight": 0.4,
        "min_score": 40,
        "tiers": [
            {"min": 85, "weight": 8, "cooldown_days": 14},
            {"min": 70, "weight": 4, "cooldown_days": 21},
            {"min": 55, "weight": 2, "cooldown_days": 35},
            {"min": 0,  "weight": 1, "cooldown_days": 60},
        ],
        "cooldown_min_factor": 0.05,
        "cooldown_bonus_cap": 1.0,
        "today_exact_multiplier": 5.0,
        "today_near_multiplier": 2.0,
        "today_near_days": 3,
        "today_min_comp": 50,
        "first_show_multiplier": 3.0,
    }
    if not os.path.exists(path):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(defaults, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        _SELECTION_PARAMS = defaults
        return defaults
    try:
        with open(path, "r", encoding="utf-8") as f:
            params = json.load(f)
        for k, v in defaults.items():
            params.setdefault(k, v)
        _SELECTION_PARAMS = params
        return params
    except Exception:
        _SELECTION_PARAMS = defaults
        return defaults


def _save_selection_params(params):
    """保存选图策略参数到 data/selection_params.json"""
    global _SELECTION_PARAMS
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "selection_params.json")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(params, f, ensure_ascii=False, indent=2)
        _SELECTION_PARAMS = params
        return True
    except Exception as e:
        print(f"[SEL] 保存参数失败: {e}")
        return False


_CAPTION_BAD_PREFIX = ("好的", "这是", "文案", "以下", "答案", "句子", "示例")
_CAPTION_BAN_WORDS = ("这张照片", "这一刻", "那天", "照片中", "图片中", "画面中")

def _clean_caption(raw):
    """清洗模型输出: 取首行、去前缀、去引号、校验长度与禁词。不合格返回 None"""
    if not raw:
        return None
    s = raw.strip().splitlines()[0].strip()              # 只取第一行
    s = s.strip("\"'“”「」『』 ").strip()                  # 去包裹引号
    # 去解释性前缀: 如 "文案：xxx" / "这是xxx" / "好的，这是：xxx"
    for _ in range(3):                                    # 最多剥3层
        changed = False
        for p in _CAPTION_BAD_PREFIX:
            if s.startswith(p):
                s = s[len(p):].lstrip("：:，,。.、 」』\"'“”").strip()
                changed = True
        if not changed:
            break
    if any(b in s for b in _CAPTION_BAN_WORDS):           # 含复述词 → 不合格
        return None
    # 长度只校验正文(剥掉 "——出处" 后), 避免长歌词+出处被误杀
    body_only = re.split(r'——|—', s)[0].strip()
    if not (8 <= len(body_only) <= 50):
        return None
    return s

_CAPTION_SYS = (
    "你是为「电子相框」写旁白短句的中文文案助手。我会给你一张照片的【文字描述】,"
    "你据此写一句配文。\n\n"
    "创作原则：\n"
    "1. 不要复述画面, 要补一点「画外之意」, 自然有趣、带点诗意或幽默, 不煽情不鸡汤。\n"
    "2. 避免词: 世界、梦、时光、岁月、温柔、治愈、刚刚好、悄悄、慢慢; 避免「……得像……」式简单比喻。\n"
    "3. 不要出现「这张照片」「这一刻」「那天」等词。\n\n"
    "【金句 vs 原创】\n"
    "- 【优先尝试引用】先想有没有一句与画面意境契合的歌词可引用; 有就用引用, 没有合适的再写原创。\n"
    "- 引用歌词时在句末标注「——歌手《歌名》」。\n"
    "- 【歌手范围】引用只能来自以下歌手(用户的音乐库), 不在此列的不要引用:\n"
    "  孫燕姿、鄧紫棋、王菲、孫盛希、飛兒樂團、梁靜茹、阿桑、郁可唯、告五人、蘇打綠、"
    "逃跑計劃、八三夭、宋雨琦、陳佩賢、楊丞琳、謝春花、小男孩樂團、王力宏、林俊傑、吳青峰、"
    "趙雷、家家、洪佩瑜、周杰倫、楊乃文、五月天、周深、郭采潔、陳芳語、潘瑋柏、張惠妹、"
    "陳潔儀、毛不易、張杰。\n\n"
    "【格式】只输出一行: 原创句不带出处; 引用歌词时在句末加「——歌手《歌名》」。"
    "正文 8~50 字, 不加引号包裹。"
)

def _caption_via_deepseek(desc):
    """用 DeepSeek 基于照片文字描述写附言 (纯文本)。返回清洗后的句子或 None。"""
    body = {"model": _DEEPSEEK_MODEL,
            "messages": [{"role": "system", "content": _CAPTION_SYS},
                         {"role": "user", "content": f"照片描述：{desc}\n\n请写一句配文："}],
            "temperature": 0.85, "max_tokens": 800}
    h = {"Authorization": f"Bearer {_DEEPSEEK_KEY}", "Content-Type": "application/json"}
    for a in range(3):
        try:
            resp = urllib.request.urlopen(
                urllib.request.Request(_DEEPSEEK_URL, data=json.dumps(body).encode(), headers=h),
                timeout=30)
            raw = json.loads(resp.read().decode())["choices"][0]["message"]["content"]
            c = _clean_caption(raw)
            if c:
                return c
            print(f"     [附言] DeepSeek 输出未通过清洗, 重试")
        except Exception as e:
            print(f"     [附言] DeepSeek 第{a+1}次失败: {repr(e)[:80]}")
        if a < 2:
            time.sleep(1.5)
    return None

def _describe_via_qwen(image_path):
    """无现成描述时, 用 qwen 看图生成一段画面描述 (给 DeepSeek 用)。"""
    b64 = encode_image_b64(image_path, max_long_edge=1024, quality=85)
    if not b64:
        return None
    body = {"model": _VLM_MODEL, "messages": [{"role": "user", "content": [
                {"type": "text", "text": "用60字内中文客观描述这张照片的主体、场景、光线、氛围。"},
                {"type": "image_url", "image_url": f"data:image/jpeg;base64,{b64}"}]}],
            "temperature": 0.3, "max_tokens": 150}
    h = {"Authorization": f"Bearer {_API_KEY}", "Content-Type": "application/json"}
    try:
        resp = urllib.request.urlopen(
            urllib.request.Request(_VLM_BASE_URL, data=json.dumps(body).encode(), headers=h), timeout=30)
        return json.loads(resp.read().decode())["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"     [附言] qwen 描述失败: {repr(e)[:80]}")
        return None

def generate_side_caption(image_path, caption=None, photo_type=None):
    """生成一句话附言 (路线1: 描述→DeepSeek写)。
    caption: qwen 评分时已得的画面描述; 没有则用 qwen 现描述一次。
    DeepSeek 据描述写: 多数原创, 偶尔引用确信的真歌词并注出处 (prompt 强约束出处正确性)。
    """
    if not load_config():
        return None
    desc = caption or _describe_via_qwen(image_path)
    if not desc:
        return None
    if _DEEPSEEK_KEY:
        return _caption_via_deepseek(desc)
    # 未配 DeepSeek 的回退: 用 qwen 据描述写 (极少走到)
    print("     [附言] 未配置 DeepSeek, 跳过")
    return None


# ─── 可导入的 API (server.py 用) ───

def do_refresh():
    """选图 → 生成附言 → 渲染 → 返回两个 framebuffer bytes

    返回: {"ok": True, "clean": bytes, "info": bytes,
            "filename": str, "side_caption": str,
            "city": str, "date": str, "camera": str,
            "mem": float, "beau": float}
    或 {"ok": False, "error": str}
    """
    if not load_config():
        return {"ok": False, "error": "配置加载失败"}

    db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "photo_scores.db")
    if not os.path.exists(db):
        return {"ok": False, "error": f"数据库不存在: {db}"}

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    sp = _load_selection_params()
    today = datetime.datetime.now()
    today_md = today.strftime("%m-%d")

    # ─── 选图策略 v3: 参数从 data/selection_params.json 读取 ───
    def _tier(comp):
        for t in sp["tiers"]:
            if comp >= t["min"]:
                return t["weight"], t["cooldown_days"]
        return sp["tiers"][-1]["weight"], sp["tiers"][-1]["cooldown_days"]

    def _days_since(used_at):
        if not used_at: return None
        try:
            return (today - datetime.datetime.fromisoformat(used_at)).total_seconds() / 86400.0
        except (ValueError, TypeError):
            return None

    def _time_factor(used_at, cooldown):
        """软冷却+时间衰减合一: 距上次 t 天, 冷却 C 天。
        t<C  → max(mf, t/C)        刚展示≈0, 线性恢复到1
        t>=C → 1 + min(bc, (t-C)/C)  继续升, 封顶 1+bc"""
        t = _days_since(used_at)
        if t is None: return 1.0          # 没用过, 中性(首次加成另算)
        mf = sp["cooldown_min_factor"]; bc = sp["cooldown_bonus_cap"]
        if t < cooldown: return max(mf, t / cooldown)
        return 1.0 + min(bc, (t - cooldown) / cooldown)

    def _today_bonus(taken_at):
        """历史上的今天: 当天命中×tm, ±N天×tn, 其余×1"""
        if not taken_at or len(taken_at) < 10: return 1.0
        md = taken_at[5:]  # MM-DD
        if md == today_md: return sp["today_exact_multiplier"]
        nd = sp["today_near_days"]
        for off in range(1, nd + 1):
            for s in (-1, 1):
                d = today + datetime.timedelta(days=off * s)
                if md == d.strftime("%m-%d"): return sp["today_near_multiplier"]
        return 1.0

    def build_cam(mk, md, lens, iso, aperture, _shutter, focal_length):
        cam = _CAMERA_NAMES.get((mk, md), f"{mk} {md}".strip()) if (mk or md) else ""
        if lens:
            clean = lens
            if cam and lens.startswith(cam.split("  ")[0]):
                clean = lens[len(cam.split("  ")[0]):].strip().lstrip(',').strip()
            cam += f"  {clean}"
        elif iso and aperture:
            try:
                params = f"ISO{iso}  f/{float(aperture):g}"
                if focal_length:
                    params += f"  {float(focal_length):g}mm"
                cam += f"  {params}"
            except (ValueError, TypeError):
                pass  # 参数异常时只显示设备名
        return cam

    # 一次性拉出全部候选, 之后在 Python 里算权重做加权随机
    SELECT_COLS = """path, filename, memory_score, beauty_score,
                   city, taken_at, camera_make, camera_model, lens,
                   iso, aperture, shutter, focal_length, side_caption, used_at,
                   caption, photo_type"""
    c.execute(f"""SELECT {SELECT_COLS} FROM photo_scores
                WHERE memory_score IS NOT NULL AND memory_score >= {sp["min_score"]}""")
    _all_candidates = c.fetchall()

    mw = sp["memory_weight"]; bw = sp["beauty_weight"]
    tm = sp["today_min_comp"]; fs = sp["first_show_multiplier"]

    def pick_candidate(label_extra=""):
        """加权随机选一张。权重 = 分档权重 × 时间因子 × 历史今天 × 首次加成。
        skip_paths 中的(重试已失败)照片排除。返回 (row, label)。"""
        pool, weights = [], []
        for r in _all_candidates:
            if r["path"] in skip_paths:
                continue
            comp = r["memory_score"] * mw + r["beauty_score"] * bw
            base, cooldown = _tier(comp)
            w = base * _time_factor(r["used_at"], cooldown)
            # 历史上的今天加成 (仅≥tm分)
            if comp >= tm:
                w *= _today_bonus(r["taken_at"])
            # 首次展示加成: 从没展示过的照片 ×fs 优先亮相
            if not r["used_at"]:
                w *= fs
            if w > 0:
                pool.append(r); weights.append(w)
        if not pool:
            return None, "无候选"
        row = random.choices(pool, weights=weights, k=1)[0]
        comp = row["memory_score"] * mw + row["beauty_score"] * bw
        md = (row["taken_at"] or "")[5:]
        is_today = (md == today_md)
        src = f"{'历史上的今天' if is_today else '加权随机'}(综合{comp:.0f}){label_extra}"
        return row, src

    row, source = None, ""
    final_side = None
    skip_paths = set()  # 重试期间排除已尝试但失败的照片
    # (pick_one 的 SQL 已过滤 memory_score>=40 并排除 skip_paths, 故无需再判重/判分)
    for retry in range(5):
        row, source = pick_candidate(f"(#{retry+1})" if retry > 0 else "")
        if not row: break

        side_try = row["side_caption"]
        if side_try: final_side = side_try; break

        full_path = os.path.join(_NAS_PATH, row["path"])
        if os.path.exists(full_path):
            # 复用评分时存的 caption/photo_type, 与 analyze 保持一致, 省一次 qwen 描述
            side_try = generate_side_caption(full_path, caption=row["caption"], photo_type=row["photo_type"])
            if side_try:
                c.execute("UPDATE photo_scores SET side_caption=? WHERE path=?", (side_try, row["path"]))
                conn.commit()
                final_side = side_try; break

        # 附言生成失败(或文件不存在), 加入跳过集合, 下一轮重试选下一张
        skip_paths.add(row["path"])

    if not row:
        conn.close()
        return {"ok": False, "error": "找不到符合条件的照片"}

    if not final_side:
        c.execute("SELECT side_caption FROM photo_scores WHERE path=?", (row["path"],))
        r2 = c.fetchone()
        final_side = (r2[0] if r2 and r2[0] else "") if r2 else ""

    rel_path = row["path"]; fn = row["filename"]
    mem = row["memory_score"]; beauty = row["beauty_score"]
    city = row["city"]; dt = row["taken_at"]
    cam = build_cam(row["camera_make"], row["camera_model"],
        row["lens"] or "", row["iso"], row["aperture"], row["shutter"], row["focal_length"])

    img_path = os.path.join(_NAS_PATH, rel_path)
    if not os.path.exists(img_path) and os.path.exists(rel_path):
        img_path = rel_path
    if not os.path.exists(img_path):
        conn.close()
        return {"ok": False, "error": f"找不到照片文件: {rel_path}"}

    # 渲染
    try:
        img = Image.open(img_path)
        img = ImageOps.exif_transpose(img)  # 应用 EXIF 方向校正
        img = img.convert("RGB")
    except Exception as e:
        conn.close()
        return {"ok": False, "error": f"图片打开失败: {e}"}

    ow, oh = img.size; rotated = ow > oh

    # 底栏 (存储格式 YYYY-MM-DD, 显示转成 YYYY.MM.DD)
    line1 = city or ""   # 无城市信息则留空, 不显示"未知地点"占位
    dt_display = dt.replace("-", ".") if dt else ""
    info_lines = [(line1, 22, dt_display, mem, beauty)]
    if cam: info_lines.append((cam, 14, ""))
    # 附言: 拆出正文与出处, 出处(若有)在第2行小字右对齐显示
    side_text, side_src = _split_caption_source(final_side or _DEFAULT_NOTICE)
    info_lines.append((side_text, 21, "", 0, 0, side_src))

    # 渲染: 渐变暗条 + 文字后渲染(fb层面纯色覆盖)
    clean = process_clean(img, ow, oh, rotated)
    clean_fb = quantize(clean)
    info_rgb = process_info(img, ow, oh, rotated, info_lines, no_text=True)
    info_fb = quantize(info_rgb)
    info_fb = _overlay_solid_text(info_fb, info_lines)

    c.execute("UPDATE photo_scores SET used_at = ? WHERE path = ?",
              (datetime.datetime.now().isoformat(), rel_path))
    conn.commit()
    conn.close()
    return {
        "ok": True,
        "clean": clean_fb, "info": info_fb,
        "path": rel_path,
        "filename": fn, "side_caption": final_side or "",
        "city": city, "date": dt, "camera": cam,
        "mem": mem, "beau": beauty,
    }


if __name__ == "__main__":
    try: sys.stdout.reconfigure(encoding='utf-8')
    except: pass

    # v7 为拉模型: ESP32 主动 POST /ink/refresh 取图, 此处仅做渲染自测
    result = do_refresh()
    if result["ok"]:
        print(f"[选图] {result['filename']}  {result['city']} {result['date']}")
        print(f"[评分] 回忆{result['mem']:.0f} 美观{result['beau']:.0f}  {result['camera']}")
        print(f"[附言] {result['side_caption']}")
        print(f"[量化] clean={len(result['clean'])} info={len(result['info'])}")
        print("[完成] (渲染测试; 实际由 ESP32 主动取图)")
        sys.exit(0)
    else:
        print(f"[失败] {result['error']}")
        sys.exit(1)