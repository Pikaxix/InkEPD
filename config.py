"""
统一配置解析 (config.env) — 全项目唯一的配置入口

server.py / send_image.py / analyze_image.py 都通过 config.load() 读取，
避免三份各自为政、键集不一致的解析逻辑。

config.env 格式: KEY = value  (# 开头为注释, 值两侧的引号会被去掉)
"""
import os

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_SCRIPT_DIR, "config.env")

# 所有已知键及默认值 (未在 config.env 出现时生效)
_DEFAULTS = {
    "DASHSCOPE_API_KEY": "",
    "VLM_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    "VLM_MODEL": "qwen3.7-plus",
    "VLM_MODEL_FALLBACK": "qwen3.5-plus",
    "DEEPSEEK_API_KEY": "",
    "DEEPSEEK_BASE_URL": "https://api.deepseek.com/chat/completions",
    "DEEPSEEK_MODEL": "deepseek-v4-pro",
    "NAS_PATH": "",
    "ESP32_IP": "",
    "ESP32_PORT": 80,
    "SERVER_PORT": 8765,
    "SERVER_HOST": "0.0.0.0",
    "REFRESH_TIMES": "08:00,17:00",
    "ACTIVE_FONT": "默认字体.TTF",
}
_INT_KEYS = {"ESP32_PORT", "SERVER_PORT"}

_cache = None


def save(updates: dict):
    """将 key-value 更新写回 config.env，保留注释/空行/顺序。

    updates 的 key 必须在 _DEFAULTS 中；值如果是 int 则写裸数字，否则写 "引号值"。
    写完后刷新内部缓存。
    """
    if not os.path.exists(_CONFIG_PATH):
        # 文件不存在时从 _DEFAULTS 生成一份
        lines = [f"{k} = \"{v}\"\n" for k, v in _DEFAULTS.items()]
    else:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()

    seen_keys = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        k, v = stripped.split("=", 1)
        k = k.strip()
        if k not in _DEFAULTS:
            new_lines.append(line)
            continue
        if k in updates:
            val = updates[k]
            if isinstance(val, str):
                line = f"{k} = \"{val}\"\n"
            else:
                line = f"{k} = {val}\n"
        seen_keys.add(k)
        new_lines.append(line)

    # 新增键（updates 中有但 config.env 中不存在）
    for k, v in updates.items():
        if k not in seen_keys and k in _DEFAULTS:
            if isinstance(v, str):
                new_lines.append(f"{k} = \"{v}\"\n")
            else:
                new_lines.append(f"{k} = {v}\n")

    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    # 刷新缓存
    global _cache
    _cache = None
    load()


def load(force=False):
    """读取 config.env, 返回包含全部键的 dict (带缓存)。"""
    global _cache
    if _cache is not None and not force:
        return _cache
    d = dict(_DEFAULTS)
    if os.path.exists(_CONFIG_PATH):
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip("\"'")
                if k not in d:
                    continue
                if k in _INT_KEYS:
                    try:
                        v = int(v)
                    except ValueError:
                        continue
                d[k] = v
    _cache = d
    return d
