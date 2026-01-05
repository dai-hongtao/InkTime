#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from pathlib import Path
import base64
import json
import sqlite3
import os
import subprocess
import time
import requests
import io
from PIL import Image, ExifTags, ImageOps
import config as cfg
import shutil


# =======================
# NAS 掉盘守护（macOS /Volumes）
# =======================
NAS_MOUNT_URL = str(getattr(cfg, "NAS_MOUNT_URL", "") or "").strip()
NAS_MOUNT_POINT = Path(str(getattr(cfg, "NAS_MOUNT_POINT", "/Volumes/photo") or "/Volumes/photo")).expanduser()
NAS_RETRY_TIMES = int(getattr(cfg, "NAS_RETRY_TIMES", 3) or 3)
NAS_RETRY_SLEEP_SEC = float(getattr(cfg, "NAS_RETRY_SLEEP_SEC", 2.0) or 2.0)


def _is_mount_ok() -> bool:
    """判断 NAS 是否仍然挂载（尽量保守）。"""
    try:
        # /Volumes 下的网络卷一般是 mount point
        if NAS_MOUNT_POINT and NAS_MOUNT_POINT.exists():
            if os.path.ismount(str(NAS_MOUNT_POINT)):
                return True
        # 兜底：只要图片根目录可访问也算 OK
        return IMAGE_DIR.exists()
    except Exception:
        return False


def _try_remount_nas() -> bool:
    """尝试重挂载 NAS。

    只在配置了 NAS_MOUNT_URL 时执行；优先使用 macOS 的 AppleScript 挂载，
    这样可以复用钥匙串里保存的凭据。
    """
    if not NAS_MOUNT_URL:
        return False

    print(f"[WARN] 检测到 NAS 可能掉盘，尝试重挂载：{NAS_MOUNT_URL}")

    # 1) AppleScript（推荐）：mount volume "afp://..." / "smb://..."
    try:
        subprocess.run(
            ["osascript", "-e", f'mount volume "{NAS_MOUNT_URL}"'],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:
        pass

    # 2) 兜底：如果你更喜欢命令行挂载，可以自行改成 mount_afp / mount_smbfs。

    # 等待卷出现
    for _ in range(10):
        if _is_mount_ok():
            print("[INFO] NAS 重挂载成功，继续处理。")
            return True
        time.sleep(0.5)

    print("[WARN] NAS 重挂载失败（卷仍不可用）。")
    return False


def _read_bytes_with_nas_retry(path: Path) -> bytes:
    """读文件：遇到 NAS 掉盘类错误时，尝试重挂载并重试。"""
    last_err: Exception | None = None

    # 只对照片库路径内的文件启用重挂载逻辑，避免误伤本地文件。
    try:
        in_photo_dir = str(path).startswith(str(IMAGE_DIR))
    except Exception:
        in_photo_dir = False

    for attempt in range(1, max(1, NAS_RETRY_TIMES) + 1):
        try:
            return path.read_bytes()
        except OSError as e:
            last_err = e

            # 不在照片库目录内，或者没有配置 NAS URL，直接抛
            if not in_photo_dir or not NAS_MOUNT_URL:
                raise

            # 常见网络卷断连错误：57 Socket is not connected；也可能表现为 5/6 等 I/O 类错误。
            # 这里不做过度精确匹配，先判断挂载状态；如果掉盘就尝试重挂载。
            if not _is_mount_ok():
                print(f"[WARN] 读文件失败（第 {attempt}/{NAS_RETRY_TIMES} 次）：{e}")
                ok = _try_remount_nas()
                if ok:
                    # 重挂载后立刻重试
                    continue

            # 如果挂载看起来还 OK，但仍然读失败，按重试策略稍等再试
            if attempt < NAS_RETRY_TIMES:
                print(f"[WARN] 读文件失败（第 {attempt}/{NAS_RETRY_TIMES} 次），稍后重试：{e}")
                time.sleep(max(0.1, NAS_RETRY_SLEEP_SEC))
                continue

            raise

    # 理论上不会到这
    if last_err:
        raise last_err
    raise OSError("读取文件失败")


# ================== 配置区域（来自 config.py） ==================

ROOT_DIR = Path(__file__).resolve().parent

# 要扫描的图片目录
IMAGE_DIR = Path(str(getattr(cfg, "IMAGE_DIR", "") or "")).expanduser()
if not IMAGE_DIR.is_absolute():
    IMAGE_DIR = (ROOT_DIR / IMAGE_DIR).resolve()

# SQLite 数据库路径
DB_PATH = Path(str(getattr(cfg, "DB_PATH", "photos.db") or "photos.db")).expanduser()
if not DB_PATH.is_absolute():
    DB_PATH = (ROOT_DIR / DB_PATH).resolve()

# LM Studio/OpenAI 兼容接口（仍允许用环境变量覆盖）
API_URL = str(
    getattr(cfg, "API_URL", None)
    or os.environ.get("LMSTUDIO_URL", "http://127.0.0.1:1234/v1/chat/completions")
)

# 模型名称（仍允许用环境变量覆盖）
MODEL_NAME = str(
    getattr(cfg, "MODEL_NAME", None)
    or os.environ.get("LMSTUDIO_MODEL", "qwen3-vl-32b-instruct")
)

# API KEY（仍允许用环境变量覆盖）
API_KEY = str(getattr(cfg, "API_KEY", None) or os.environ.get("LMSTUDIO_API_KEY", ""))

# 每次处理多少张；None 为不限制
BATCH_LIMIT = getattr(cfg, "BATCH_LIMIT", None)

# 请求超时时间（秒）
TIMEOUT = float(getattr(cfg, "TIMEOUT", 600) or 600)

# 发送给 VLM 之前，先把图片长边缩放到该值（像素）。
# 0 表示不缩放。
# 本地推理可保持较高值；云端推理建议降低（减少 token/成本）。
VLM_MAX_LONG_EDGE = int(getattr(cfg, "VLM_MAX_LONG_EDGE", 2560) or 2560)

# 中文城市数据库位置
WORLD_CITIES_CSV = Path(str(getattr(cfg, "WORLD_CITIES_CSV", "data/world_cities_zh.csv") or "data/world_cities_zh.csv")).expanduser()
if not WORLD_CITIES_CSV.is_absolute():
    WORLD_CITIES_CSV = (ROOT_DIR / WORLD_CITIES_CSV).resolve()

CITY_GRID_DEG = float(getattr(cfg, "CITY_GRID_DEG", 1.0) or 1.0)
CITY_MAX_DISTANCE_KM = float(getattr(cfg, "CITY_MAX_DISTANCE_KM", 80.0) or 80.0)
HOME_LAT = float(getattr(cfg, "HOME_LAT", 22.543096) or 22.543096)
HOME_LON = float(getattr(cfg, "HOME_LON", 114.057865) or 114.057865)
HOME_RADIUS_KM = float(getattr(cfg, "HOME_RADIUS_KM", 60.0) or 60.0)
# ==================================================

# exiftool 是否可用：缺失时只降级 GPS/部分 EXIF，不中断流程
EXIFTOOL_AVAILABLE = False

def require_exiftool() -> None:
    global EXIFTOOL_AVAILABLE
    EXIFTOOL_AVAILABLE = shutil.which("exiftool") is not None
    if not EXIFTOOL_AVAILABLE:
        print(
            "[WARN] 未找到 exiftool，将跳过 exiftool 辅助的 GPS/EXIF 读取（不影响主流程）。\n"
            "       如需更完整的 GPS 信息，请安装：\n"
            "       macOS: brew install exiftool\n"
            "       Ubuntu/Debian: sudo apt-get install -y libimage-exiftool-perl\n"
            "       Windows: choco install exiftool"
        )

def encode_image_to_b64(path: Path) -> str:
    """读取图片并（可选）缩放长边后，重新编码为 JPEG，再转 base64。

    目的：
    1) 控制输入分辨率（尤其是 200MP 这类超大图），避免推理成本/延迟暴涨；
    2) 通过重新编码，尽量规避某些 JPEG 在 libvips 侧解码报错（如 marker 前多余字节）。
    """
    data = _read_bytes_with_nas_retry(path)

    # 尽量用 PIL 容错打开，然后统一转成干净的 JPEG bytes
    try:
        img = Image.open(io.BytesIO(data))
        # 处理 EXIF 旋转
        try:
            img = ImageOps.exif_transpose(img)  # type: ignore
        except Exception:
            pass

        # 统一色彩模式：JPEG 需要 RGB
        if img.mode in ("RGBA", "LA"):
            # 有透明通道时，用白底合成
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # 可选缩放
        try:
            w, h = img.size
            long_edge = max(w, h)
            if VLM_MAX_LONG_EDGE and long_edge > VLM_MAX_LONG_EDGE:
                scale = float(VLM_MAX_LONG_EDGE) / float(long_edge)
                new_w = max(1, int(round(w * scale)))
                new_h = max(1, int(round(h * scale)))
                img = img.resize((new_w, new_h), resample=Image.LANCZOS)
        except Exception:
            pass

        out = io.BytesIO()
        # quality 92 在观感和体积之间比较平衡；optimize 可能更慢但通常能降体积
        img.save(out, format="JPEG", quality=92, optimize=True)
        clean_bytes = out.getvalue()
        return base64.b64encode(clean_bytes).decode("utf-8")

    except Exception:
        # 兜底：如果 PIL 也打不开，就退回原始 bytes（让上游报错更直观）
        return base64.b64encode(data).decode("utf-8")


def ensure_table(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS photo_scores (
            path              TEXT PRIMARY KEY,
            caption           TEXT,
            type              TEXT,
            memory_score      REAL,
            beauty_score      REAL,
            reason            TEXT,
            width             INTEGER,
            height            INTEGER,
            orientation       TEXT,
            used_at           TEXT,
            exif_json         TEXT,
            raw_json          TEXT,
            exif_datetime     TEXT,
            exif_make         TEXT,
            exif_model        TEXT,
            exif_iso          INTEGER,
            exif_exposure_time REAL,
            exif_f_number     REAL,
            exif_focal_length REAL,
            exif_gps_lat      REAL,
            exif_gps_lon      REAL,
            exif_gps_alt      REAL,
            side_caption      TEXT,
            exif_city         TEXT
        )
        """
    )
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_json TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN width INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN height INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN orientation TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN used_at TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_datetime TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_make TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_model TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_iso INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_exposure_time REAL")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_f_number REAL")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_focal_length REAL")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_gps_lat REAL")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_gps_lon REAL")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_gps_alt REAL")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN side_caption TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE photo_scores ADD COLUMN exif_city TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()

# 生成一句话文案
def generate_side_caption(image_path: Path) -> str | None:
    system_prompt = (
        "你是一位为「电子相框」撰写旁白短句的中文文案助手。\n"
        "你的目标不是描述画面，而是为画面补上一点“画外之意”。\n\n"

        "创作原则：\n"
        "1. 避免使用以下词语：世界、梦、时光、岁月、温柔、治愈、刚刚好、悄悄、慢慢 等（但不是绝对禁止）。\n"
        "2. 严禁使用如下句式：……里……着整个世界；……里……着整个夏天；……得像……（简单的比喻）; ……比……还……； ……得比……更……。\n"
        "3. 只基于图片中能确定的信息进行联想，不要虚构时间、人物关系、事件背景。\n"
        "4. 文案应自然、有趣，带一点幽默或者诗意，但请避免煽情、鸡汤。\n"
        "5. 不要复述画面内容本身，而是写“看完画面后，心里多出来的一句话”。\n"
        "6. 可以偏向以下风格之一：\n"
        "   - 日常中的微妙情绪\n"
        "   - 轻微自嘲或冷幽默\n"
        "   - 对时间、记忆、瞬间的含蓄感受\n"
        "   - 看似平淡但有余味的一句判断\n"
        "7. 避免小学生作文式的、套路式的模板化表达\n"

        "格式要求：\n"
        "1. 只输出一句中文短句，不要换行，不要引号，不要任何解释。\n"
        "2. 建议长度 8～24 个汉字，最多不超过 30 个汉字。\n"
        "3. 不要出现“这张照片”“这一刻”“那天”等指代照片本身的词。\n"
    )
    user_prompt = "请基于这张照片，生成一句符合规则的中文文案。"
    try:
        img_b64 = encode_image_to_b64(image_path)
    except Exception:
        return None

    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"

    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                    },
                ],
            },
        ],
        "temperature": 0.7,
        "max_tokens": 64,
        "top_p": 0.9,
        "stream": False,
    }

    try:
        resp = requests.post(API_URL, headers=headers, json=payload, timeout=min(120, TIMEOUT))
    except Exception:
        return None

    if not resp.ok:
        return None

    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except Exception:
        return None

    if not isinstance(content, str):
        content = str(content)

    caption = content.strip().strip("“”\"'")
    return caption or None


def list_images(limit: int | None = None) -> list[Path]:
    exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
    files = []
    print("[INFO] 正在递归扫描图片目录，请稍候……")
    scanned = 0
    for p in IMAGE_DIR.rglob("*"):
        scanned += 1
        if scanned % 500 == 0:
            print(f"[SCAN] 已扫描文件数：{scanned} …")
        if p.is_file() and p.suffix.lower() in exts:
            if is_screenshot(p):
                continue
            files.append(p)
    print(f"[INFO] 扫描完成，共发现 {len(files)} 张图片（文件总数 {scanned}）。")
    if limit is not None:
        files = files[:limit]
    return files

# 排除 Screenshot 图片
def is_screenshot(path: Path) -> bool:
    s = str(path)
    return "screenshot" in s.lower()


def filter_unscored(conn: sqlite3.Connection, paths: list[Path]) -> list[Path]:
    if not paths:
        return []

    cur = conn.cursor()
    placeholders = ",".join("?" for _ in paths)
    rows = cur.execute(
        f"SELECT path FROM photo_scores WHERE path IN ({placeholders})",
        [str(p) for p in paths],
    ).fetchall()
    already = {row[0] for row in rows}
    return [p for p in paths if str(p) not in already]


def _convert_gps_to_deg(value):
    try:
        d, m, s = value
        return float(d[0]) / float(d[1]) + float(m[0]) / float(m[1]) / 60.0 + float(s[0]) / float(s[1]) / 3600.0
    except Exception:
        return None


def read_gps_with_exiftool(path: Path):
    if not EXIFTOOL_AVAILABLE:
        return None
    try:
        result = subprocess.run(
            ["exiftool", "-n", "-json", str(path)],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        # 没装 exiftool，则直接跳过
        return None
    except subprocess.CalledProcessError:
        return None

    try:
        data = json.loads(result.stdout)[0]
    except Exception:
        return None

    lat = data.get("GPSLatitude")
    lon = data.get("GPSLongitude")
    alt = data.get("GPSAltitude")
    if lat is None or lon is None:
        return None
    return {
        "lat": float(lat),
        "lon": float(lon),
        "alt": float(alt) if alt is not None else None,
    }


def read_exif(path: Path) -> dict:
    info: dict = {}
    try:
        img = Image.open(path)
        try:
            width, height = img.size
            info["width"] = int(width)
            info["height"] = int(height)
            if width > height:
                info["orientation"] = "landscape"
            elif height > width:
                info["orientation"] = "portrait"
            else:
                info["orientation"] = "square"
        except Exception:
            pass
        exif_raw = img._getexif() or {}
    except Exception:
        return info

    exif = {}
    for tag_id, value in exif_raw.items():
        tag = ExifTags.TAGS.get(tag_id, tag_id)
        exif[tag] = value

    # 基本字段
    info["datetime"] = exif.get("DateTimeOriginal") or exif.get("DateTime")
    info["make"] = exif.get("Make")
    info["model"] = exif.get("Model")
    info["iso"] = exif.get("ISOSpeedRatings") or exif.get("PhotographicSensitivity")
    info["exposure_time"] = exif.get("ExposureTime")
    info["f_number"] = exif.get("FNumber")
    info["focal_length"] = exif.get("FocalLength")

    gps_info = exif.get("GPSInfo")
    lat = lon = None
    if isinstance(gps_info, dict):
        # GPSInfo 的 key 可能是数字，需要映射
        gps_tags = {}
        for k, v in gps_info.items():
            name = ExifTags.GPSTAGS.get(k, k)
            gps_tags[name] = v

        lat_ref = gps_tags.get("GPSLatitudeRef")
        lat_raw = gps_tags.get("GPSLatitude")
        lon_ref = gps_tags.get("GPSLongitudeRef")
        lon_raw = gps_tags.get("GPSLongitude")

        if lat_raw and lat_ref:
            lat = _convert_gps_to_deg(lat_raw)
            if lat is not None and lat_ref in ["S", "s"]:
                lat = -lat
        if lon_raw and lon_ref:
            lon = _convert_gps_to_deg(lon_raw)
            if lon is not None and lon_ref in ["W", "w"]:
                lon = -lon

    info["gps_lat"] = lat
    info["gps_lon"] = lon

    if info.get("gps_lat") is None or info.get("gps_lon") is None:
        gps = read_gps_with_exiftool(path)
        if gps is not None:
            info["gps_lat"] = gps["lat"]
            info["gps_lon"] = gps["lon"]
            if gps.get("alt") is not None:
                info["gps_alt"] = gps["alt"]

    return info


def in_home(lat: float | None, lon: float | None) -> bool:
    """判断是否在“本地/常驻地”范围内。"""
    if lat is None or lon is None:
        return False
    try:
        d = haversine_km(float(lat), float(lon), float(HOME_LAT), float(HOME_LON))
        return d <= float(HOME_RADIUS_KM)
    except Exception:
        return False


def format_eta(seconds: float) -> str:
    if seconds <= 0:
        return "00:00:00"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


import csv
import math
from typing import Dict, List, Tuple, Optional

CityRecord = Tuple[float, float, str, str]  # (lat, lon, name_zh, name_en)

_CITY_CACHE_CITIES: List[CityRecord] | None = None
_CITY_CACHE_GRID: Dict[Tuple[int, int], List[int]] | None = None

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r * c

def grid_key(lat: float, lon: float) -> Tuple[int, int]:
    gx = int(math.floor(lat / CITY_GRID_DEG))
    gy = int(math.floor(lon / CITY_GRID_DEG))
    return gx, gy

def load_world_cities(csv_path: Path) -> Tuple[List[CityRecord], Dict[Tuple[int, int], List[int]]]:
    if not csv_path.exists():
        raise SystemExit(f"[FATAL] 找不到城市索引文件: {csv_path}")

    cities: List[CityRecord] = []
    grid_index: Dict[Tuple[int, int], List[int]] = {}

    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                lat = float((row.get("lat") or "").strip())
                lon = float((row.get("lon") or "").strip())
            except Exception:
                continue
            name_en = (row.get("name_en") or "").strip()
            name_zh = (row.get("name_zh") or "").strip()
            cities.append((lat, lon, name_zh, name_en))

    for idx, (lat, lon, name_zh, name_en) in enumerate(cities):
        key = grid_key(lat, lon)
        grid_index.setdefault(key, []).append(idx)

    print(f"[INFO] 已加载中文城市库: {csv_path}")
    return cities, grid_index

def find_nearest_city(
    lat: float,
    lon: float,
    cities: List[CityRecord],
    grid_index: Dict[Tuple[int, int], List[int]],
    max_km: float = 80.0,
) -> str:
    if not cities:
        return ""

    gx, gy = grid_key(lat, lon)

    def collect_candidates(radius: int) -> List[int]:
        cand: List[int] = []
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                bucket = grid_index.get((gx + dx, gy + dy))
                if bucket:
                    cand.extend(bucket)
        return cand

    candidates = collect_candidates(radius=1)
    if not candidates:
        candidates = collect_candidates(radius=2)
    if not candidates:
        return ""

    best_idx: Optional[int] = None
    best_dist = float("inf")

    for idx in candidates:
        city_lat, city_lon, name_zh, name_en = cities[idx]
        d = haversine_km(lat, lon, city_lat, city_lon)
        if d < best_dist:
            best_dist = d
            best_idx = idx

    if best_idx is None or best_dist > max_km:
        return ""

    _, _, name_zh, name_en = cities[best_idx]
    return name_zh or name_en or ""

def get_city_resolver():
    global _CITY_CACHE_CITIES, _CITY_CACHE_GRID
    if _CITY_CACHE_CITIES is None or _CITY_CACHE_GRID is None:
        _CITY_CACHE_CITIES, _CITY_CACHE_GRID = load_world_cities(WORLD_CITIES_CSV)

    def resolve(lat: float | None, lon: float | None) -> str:
        if lat is None or lon is None:
            return ""
        return find_nearest_city(lat, lon, _CITY_CACHE_CITIES, _CITY_CACHE_GRID, max_km=CITY_MAX_DISTANCE_KM)

    return resolve


def call_vlm(image_path: Path) -> dict:
    try:
        img_b64 = encode_image_to_b64(image_path)
    except Exception as e:
        raise RuntimeError(f"读取图片失败（可能 NAS 掉盘）：{e}")

    exif_info = read_exif(image_path)
    exif_json = json.dumps(exif_info, ensure_ascii=False, default=str)

    system_prompt = (
        "你是一个“个人相册照片评估助手”，擅长理解真实照片的内容，并从回忆价值和美观角度打分。\n"
        "你会收到一张照片（以 base64 形式提供），你的任务是：\n"
        "1）用中文详细描述照片内容（80~200 字），\n"
        "2）判断照片的大致类型：人物/孩子/猫咪/家庭/旅行/风景/美食/宠物/日常/文档/杂物/其他，一张照片可以有不止一个类型。\n"
        "3）给出 0~100 的“值得回忆度” memory_score（精确到一位小数），\n"
        "4）给出 0~100 的“美观程度” beauty_score（精确到一位小数），\n"
        "5）用简短中文 reason 解释原因（不超过 40 字）。\n\n"

        "【值得回忆度（memory_score）评分方法】\n"
        "请先按照值得回忆的程度，先确定照片的'得分区间'，再进行精调：\n"
        "如何判定值得回忆度（memory_score）的得分区间：\n"
        "- 垃圾/随手拍/无意义记录：40.0 分以下（常见为 0~25；若还能勉强辨认但无故事，也不要超过 39.9）。\n"
        "- 稍微有点可回忆价值：以 65.0 分为中心（大多落在 58.1~70.3）。\n"
        "- 不错的回忆价值：以 75 分为中心（大多落在 68.7~82.4）。\n"
        "- 特别精彩、强烈值得珍藏：以 85 分为中心（大多落在 79.1~95.9；\n"
        "如何继续精调memory_score得分（若同时符合几条加分项，加分可叠加）：\n"
        "- 人物与关系：画面中含有面积较大的人脸，有人物互动，或属于合影 → 大幅提高评分；\n"
        "- 事件性：生日/聚会/仪式/舞台/明显事件 → 少许提高评分；\n"
        "- 稀缺性与不可复现：明显“这一刻很难再来一次” → 大幅提高评分；\n"
        "- 情绪强度：笑、哭、惊喜、拥抱、互动、氛围强 → 少许提高评分；\n"
        "- 信息密度：画面能讲清楚发生了什么 → 微微提高评分；\n"
        "- 优美风景：画面中含有壮丽的自然风光，或精美、有秩序感的构图 → 少许提高评分；\n"
        "- 旅行意义：异地、地标、旅途情景 → 少许提高评分。\n\n"
        "- 画质：画面不清晰、模糊、有残影、虚焦 → 微微降低评分。\n\n"

        "【重点照片的处理】\n"
        "如果画面中含有：孩子/猫咪/宠物题材，这些主题更容易产生高回忆价值，请直接以75分为中心，大幅提高评分”。\n"

        "【明显低价值图片的处理】\n"
        "对以下低价值图片，必须将 memory_score 压低到 0~25（最多不超过 39）。\n"
        "- 裸露、低俗、色情或违反公序良俗的图片。\n\n"
        "- 账单、收据、广告、随手拍的杂物、测试图片、屏幕截图等。\n\n"
        

        "【美观分（beauty_score）】\n"
        "美观分只评价视觉：构图、光线、清晰度、色彩、主体突出。\n"
        "不要被“孩子/猫/旅行”主题绑架美观分：主题不等于好看。\n"

        "请严格只输出 JSON，格式如下：\n"
        "{\n"
        "  \"caption\": \"……\",\n"
        "  \"type\": \"人物/家庭/旅行/…… 可以带多个type\",\n"
        "  \"memory_score\": 0.0-100.0 的数字, 精确到 1 位小数\n"
        "  \"beauty_score\": 0.0-100.0 的数字, 精确到 1 位小数\n"
        "  \"reason\": \"不超过 60 字的中文理由\"\n"
        "}\n"
        "不要输出任何多余文字，不要加注释。"
    )

    user_text = (
        "下面是照片的内容，请结合图像本身完成上述任务。\n"
    )

    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"

    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{img_b64}"
                        },
                    },
                ],
            },
        ],
        "temperature": 0.2,
        "stream": False,
    }

    resp = requests.post(API_URL, headers=headers, json=payload, timeout=TIMEOUT)
    if not resp.ok:
        print("HTTP:", resp.status_code)
        print(resp.text)
        raise RuntimeError(f"LM Studio 请求失败: HTTP {resp.status_code}")

    data = resp.json()
    try:
        content = data["choices"][0]["message"]["content"].strip()
    except Exception:
        print("[DEBUG] 返回内容：", data)
        raise RuntimeError("解析失败：无法从 choices[0].message.content 读取内容")

    # content 应该是 JSON 字符串
    try:
        obj = json.loads(content)
    except Exception:
        print("[DEBUG] 非 JSON 输出：", content)
        raise RuntimeError("解析失败：模型未按 JSON 输出")

    return obj, exif_info


def main():
    filelist_path = ROOT_DIR / "filelist.txt"

    print("[INFO] 正在扫描图片目录……")
    imgs = list_images()
    filelist_path.write_text("\n".join(str(p) for p in imgs), encoding="utf-8")
    print(f"[INFO] 已更新文件列表 filelist.txt，共 {len(imgs)} 个文件。")
    if not imgs:
        raise SystemExit(f"目录下没有图片文件: {IMAGE_DIR}")

    imgs = [p for p in imgs if not is_screenshot(p)]
    if not imgs:
        raise SystemExit("[INFO] 所有图片都被 Screenshot 过滤规则排除了，没有可处理的图片。")

    conn = sqlite3.connect(DB_PATH)
    ensure_table(conn)
    city_resolver = get_city_resolver()

    cur_test = conn.cursor()
    # 只统计当前 IMAGE_DIR 下的已分析照片，避免数据库里其它路径/历史残留影响进度计算
    image_dir_prefix = str(IMAGE_DIR)
    counted = cur_test.execute(
        "SELECT COUNT(*) FROM photo_scores WHERE path LIKE ?",
        (image_dir_prefix + "%",),
    ).fetchone()[0]
    print(f"[INFO] 数据库中已有 {counted} 张已分析照片（仅统计当前目录）。")

    target_paths = filter_unscored(conn, imgs)
    if not target_paths:
        print("[INFO] 所有图片都已经在 photo_scores 中有记录。")
        conn.close()
        return

    if BATCH_LIMIT is not None:
        target_paths = target_paths[:BATCH_LIMIT]

    # 进度条口径：以“本次启动时的快照”为准。
    # total = 已分析(当前目录) + 本次待处理（filter_unscored 产生的目标集合）
    already_done = counted
    total = already_done + len(target_paths)
    print(f"[INFO] 本次准备处理 {len(target_paths)} 张图片（快照总数 {total}，已分析 {already_done}）。")

    cur = conn.cursor()
    start_time = time.time()

    for idx, path in enumerate(target_paths, start=1):
        t_photo_start = time.perf_counter()
        sep = "=" * 60
        print("\n" + sep)
        print(f"[{idx}/{len(target_paths)}] 处理: {path}")
        try:
            result, exif_info = call_vlm(path)
        except Exception as e:
            print(f"[WARN] 调用模型失败: {e}")
            continue
        t_after_vlm = time.perf_counter()
        vlm_cost = t_after_vlm - t_photo_start

        caption = str(result.get("caption", "")).strip()
        ptype = str(result.get("type", "")).strip()
        try:
            memory_score = float(result.get("memory_score", 0.0))
        except Exception:
            memory_score = 0.0
        try:
            beauty_score = float(result.get("beauty_score", 0.0))
        except Exception:
            beauty_score = 0.0
        reason = str(result.get("reason", "")).strip()

        side_caption = generate_side_caption(path)
        t_after_side = time.perf_counter()
        side_cost = t_after_side - t_after_vlm

        width = exif_info.get("width")
        height = exif_info.get("height")
        orientation = exif_info.get("orientation")

        exif_datetime = exif_info.get("datetime")
        exif_make = exif_info.get("make")
        exif_model = exif_info.get("model")

        def _to_int(v):
            try:
                if v is None:
                    return None
                return int(v)
            except Exception:
                return None

        def _to_float(v):
            try:
                if v is None:
                    return None
                return float(v)
            except Exception:
                return None

        exif_iso = _to_int(exif_info.get("iso"))
        exif_exposure_time = _to_float(exif_info.get("exposure_time"))
        exif_f_number = _to_float(exif_info.get("f_number"))
        exif_focal_length = _to_float(exif_info.get("focal_length"))
        exif_gps_lat = _to_float(exif_info.get("gps_lat"))
        exif_gps_lon = _to_float(exif_info.get("gps_lon"))
        exif_gps_alt = _to_float(exif_info.get("gps_alt"))

        if exif_gps_lat is not None and exif_gps_lon is not None:
            exif_city = city_resolver(exif_gps_lat, exif_gps_lon)
        else:
            exif_city = ""

        # 如果有 GPS 信息且不在本地范围内，略微提高回忆分（最多 +5，且不超过 100 分）
        lat = exif_info.get("gps_lat")
        lon = exif_info.get("gps_lon")
        if lat is not None and lon is not None and not in_home(lat, lon):
            memory_score = min(memory_score + 5.0, 100.0)

        exif_json = json.dumps(exif_info, ensure_ascii=False, default=str)

        print(f"  类型    ：{ptype}")
        print(f"  回忆分  ：{memory_score:.1f}")
        print(f"  美观分  ：{beauty_score:.1f}")
        if side_caption:
            print(f"  一句话文案：{side_caption}")
        else:
            print("  一句话文案：(无)")
        print(f"  画面描述：{caption}")
        print(f"  理由    ：{reason}")

        cur.execute(
            """
            INSERT OR REPLACE INTO photo_scores
            (path, caption, type, memory_score, beauty_score, reason,
             width, height, orientation, used_at,
             exif_json, raw_json,
             exif_datetime, exif_make, exif_model,
             exif_iso, exif_exposure_time, exif_f_number, exif_focal_length,
             exif_gps_lat, exif_gps_lon, exif_gps_alt, side_caption, exif_city)
            VALUES (?, ?, ?, ?, ?, ?,
                    ?, ?, ?, COALESCE((SELECT used_at FROM photo_scores WHERE path = ?), NULL),
                    ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?, ?)
            """,
            (
                str(path),
                caption,
                ptype,
                memory_score,
                beauty_score,
                reason,
                width,
                height,
                orientation,
                str(path),
                exif_json,
                json.dumps(result, ensure_ascii=False),
                exif_datetime,
                exif_make,
                exif_model,
                exif_iso,
                exif_exposure_time,
                exif_f_number,
                exif_focal_length,
                exif_gps_lat,
                exif_gps_lon,
                exif_gps_alt,
                side_caption,
                exif_city,
            ),
        )
        conn.commit()
        t_photo_end = time.perf_counter()
        total_cost = t_photo_end - t_photo_start
        # pretty timing summary

        # 进度条与预估时间（以本次启动时的快照为准，不受运行中新增照片影响）
        processed_now = already_done + idx

        denom = total if total > 0 else 1
        progress = processed_now / denom
        # 夹紧，确保不会超过 100%
        if progress < 0:
            progress = 0.0
        if progress > 1:
            progress = 1.0

        bar_width = 30
        filled = int(bar_width * progress)
        bar = "█" * filled + "░" * (bar_width - filled)

        elapsed = time.time() - start_time
        avg_per = elapsed / idx if idx > 0 else 0
        remaining = max(total - processed_now, 0)
        eta = format_eta(remaining * avg_per) if avg_per > 0 else "00:00:00"

        print(f"[进度] {bar} {progress*100:5.1f}%  {processed_now}/{total}  本张耗时 {total_cost:4.1f}s  预计剩余 {eta} ")

    conn.close()
    print("\n[完成] 本批次处理完成。")


if __name__ == "__main__":
    require_exiftool()
    main()