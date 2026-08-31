"""通用工具：路径、时间、图像编码、统计。"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import unicodedata
from datetime import datetime
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_FILE = BACKEND_ROOT / ".data_root.json"
DEFAULT_DATA_ROOT = REPO_ROOT / "data"
DIFF_DATA_ROOT_ENV = "DIFF_DATA_ROOT"

MAX_ADU = 4095
FITS_HEADER_MEAN = "MEAN"
FITS_HEADER_STD = "STD"
FITS_HEADER_SATPCT = "SATPCT"

# session 名称清洗：路径分隔符 + Windows 非法字符 + 空白 → "_"
_ILLEGAL_SESSION_CHARS = re.compile(r'[<>:"/\\|?*\s]')

# snapshot prefix 清洗（v1.4）：与 session 同口径，但允许结果为空
_ILLEGAL_PREFIX_CHARS = re.compile(r'[<>:"/\\|?*\s]')


def ok(data):
    """统一成功响应包裹。"""
    return {"ok": True, "data": data, "error": None}


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%dT%H-%M-%S")


def sanitize_session_name(name: str) -> str:
    """v1.2：清洗用户原始 session_name。

    去除/替换路径分隔符与 Windows 非法字符 < > : " / \\ | ? * 及空白；
    空输入返回 "session"。非 ASCII 字符（如中文）与下划线等合法字符保留。
    Windows 不允许目录名以点结尾，因此清洗后去掉尾部点。
    """
    raw = (name or "").strip()
    if not raw:
        return "session"
    cleaned = _ILLEGAL_SESSION_CHARS.sub("_", raw).rstrip(".")
    return cleaned or "session"


def sanitize_prefix(prefix: str | None) -> str:
    """v1.4：清洗快照文件名 prefix。

    路径分隔符、Windows 非法字符 `< > : " | ? *` 与空白统一替换为 `_`；
    缺省/空输入返回 ""（允许为空）。非 ASCII 字符（如中文）保留。
    """
    return _ILLEGAL_PREFIX_CHARS.sub("_", prefix or "")


def ascii_fits_value(value: str) -> str:
    """把任意字符串转为 FITS 头可写的 printable ASCII（v1.2 FIX）。

    - 优先 transliterate：NFKD 去重音（é→e），组合附加符丢弃；
    - 其余非 ASCII 字符逐个替换为 "_"；
    - 非可打印 ASCII 替换为 "_"；
    - 结果为空时返回 "SESSION"。
    """
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    out: list[str] = []
    for ch in normalized:
        if ch.isascii() and ch.isprintable():
            out.append(ch)
        elif unicodedata.combining(ch):
            continue
        else:
            out.append("_")
    return "".join(out) or "SESSION"


def safe_name(name: str) -> str:
    """兼容旧版内部清洗口径（仅 ASCII 文件名安全字符）。"""
    s = re.sub(r"[^0-9A-Za-z_\-\.]", "_", name).strip("_")
    return s or "session"


# =========================================================================
# 动态数据目录（v1.2）
# =========================================================================
def _load_persisted_data_root() -> Path | None:
    """读取 backend/.data_root.json；损坏或缺失返回 None。"""
    try:
        if SETTINGS_FILE.is_file():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            raw = data.get("data_root") if isinstance(data, dict) else None
            if isinstance(raw, str) and raw.strip():
                return Path(raw).resolve()
    except Exception:  # noqa: BLE001
        pass
    return None


class DataRootSettings:
    """单例、线程安全的动态数据目录设置。

    启动优先级：DIFF_DATA_ROOT > backend/.data_root.json > <repo>/data。
    运行期 PUT /api/settings 立即切换当前值并持久化到 .data_root.json。
    """

    def __init__(self):
        self._lock = threading.RLock()
        env = os.environ.get(DIFF_DATA_ROOT_ENV)
        persisted = _load_persisted_data_root()
        if env:
            root = Path(env).resolve()
        elif persisted is not None:
            root = persisted
        else:
            root = DEFAULT_DATA_ROOT.resolve()
        self._root = root

    def get(self) -> Path:
        with self._lock:
            return Path(self._root)

    def set(self, path: Path | str) -> None:
        """持久化并切换当前数据根目录（先落盘、后生效）。"""
        root = Path(path).resolve()
        payload = {"data_root": str(root)}
        with self._lock:
            SETTINGS_FILE.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            self._root = root


_settings = DataRootSettings()


def get_data_root() -> Path:
    """动态访问器：每次调用读取当前生效的 data_root。"""
    return _settings.get()


def set_data_root(path: Path | str) -> None:
    """运行期切换并持久化 data_root（线程安全）。"""
    _settings.set(path)


def get_snapshots_dir() -> Path:
    return get_data_root() / "snapshots"


def get_captures_dir() -> Path:
    return get_data_root() / "captures"


def is_data_root_writable(path: Path | None = None) -> bool:
    root = Path(path) if path is not None else get_data_root()
    try:
        return os.access(root, os.W_OK)
    except OSError:
        return False


def ensure_dirs() -> None:
    snapshots = get_snapshots_dir()
    captures = get_captures_dir()
    snapshots.mkdir(parents=True, exist_ok=True)
    captures.mkdir(parents=True, exist_ok=True)


def auto_contrast_8bit(frame: np.ndarray) -> np.ndarray:
    """uint16(≤4095) → uint8 灰阶：1%~99% 分位线性映射。"""
    lo, hi = np.percentile(frame, (1.0, 99.0))
    if hi <= lo:
        lo = float(np.min(frame))
        hi = float(np.max(frame))
    if hi <= lo:
        hi = lo + 1.0
    mapped = (frame.astype(np.float32) - lo) * (255.0 / (hi - lo))
    return np.clip(mapped, 0, 255).astype(np.uint8)


def frame_stats(frame: np.ndarray) -> dict:
    """12-bit 帧统计（min/max/mean/std/saturated_pct 均基于 uint16 原始值）。"""
    arr = np.asarray(frame)
    total = arr.size
    return {
        "min": int(arr.min()),
        "max": int(arr.max()),
        "mean": float(arr.mean()),
        "std": float(arr.std()),
        "saturated_pct": float((arr >= MAX_ADU).sum() * 100.0 / max(total, 1)),
    }


def histogram_256(raw12: np.ndarray) -> list[int]:
    """256-bin histogram of the raw 12-bit frame, adapted to the frame's actual
    ADU min/max so the displayed shape matches the auto-contrast image.

    The fixed full 0..4095 range collapses short-exposure images into one or
    two low-ADU bins; using the current frame's min/max keeps the same 256
    bins while highlighting the signal range that is actually present. Values
    are still raw ADU counts (not the stretched preview).
    """
    arr = np.asarray(raw12)
    lo = float(np.min(arr))
    hi = float(np.max(arr))
    if hi <= lo:
        hi = lo + 1.0
    counts = np.histogram(arr, bins=256, range=(lo, hi + 1.0))[0]
    return [int(v) for v in counts]


CONTENT_TYPES = {
    ".fits": "application/fits",
    ".fit": "application/fits",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".png": "image/png",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
}


# =========================================================================
# FITS/TIFF 采集元数据（v1.6：全量相机参数 + 用户 comment）
# =========================================================================

# 扩展参数名 → FITS HIERARCH CAM 键（值来自 props dict；Mock 专属键仅在存在时写）。
CAM_HIERARCH_KEYS = (
    ("width", "WIDTH"),
    ("height", "HEIGHT"),
    ("offset_x", "OFFSETX"),
    ("offset_y", "OFFSETY"),
    ("reverse_x", "REVERSX"),
    ("reverse_y", "REVERSY"),
    ("trigger_mode", "TRIGGER"),
    ("auto_exposure", "AUTOEXP"),
    ("auto_gain", "AUTOGAIN"),
    ("mock_pattern", "MOCKPAT"),
    ("mock_scene", "MOCKSCE"),
    ("mock_drift", "MOCKDRIFT"),
)

# TIFF 私有 tag：完整采集元数据 JSON（UTF-8 字节，含中文 comment）。
TIFF_METADATA_TAG = 65000
TIFF_SOFTWARE = "Static AstroPhoto"


def _meta_float(value, default=0.0) -> float:
    """把 props 里的数值安全转 float；非数值/非有限值（inf/nan）回退默认值，
    避免一帧/整个文件写入因此失败。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return float(default)
    return f if math.isfinite(f) else float(default)


def capture_metadata(props: dict, cam_name: str, cam_serial: str,
                     frametype: str, session: str, nframe: int,
                     comment: str = "") -> dict:
    """构建「采集元数据 dict」：FITS/TIFF 文件头的统一数据源。

    - ``props``：camera.get_properties() 的全量 ``{name: value}`` 映射
      （13 个契约属性 + Mock 的 mock_pattern/mock_scene/mock_drift）；
    - 相机身份（name/serial）、frametype、session、nframe、comment 单独存放；
    - ``comment`` 保留用户原始文本（可含中文）：FITS 头写入时经
      ``ascii_fits_value()`` 转 printable ASCII，TIFF 私有 tag 65000 直接以
      UTF-8 字节保存（二者都不丢原始输入，只是存储口径不同）。
    """
    return {
        "camera": {"name": str(cam_name or "unknown"),
                   "serial": str(cam_serial or "unknown")},
        "frametype": str(frametype or ""),
        "session": str(session or ""),
        "nframe": int(nframe or 0),
        "comment": str(comment or ""),
        "props": {str(k): v for k, v in dict(props or {}).items()},
    }


def fits_header_entries(meta: dict, stats: dict | None = None,
                        date_obs: str | None = None) -> list:
    """生成 FITS 头键值对列表 ``list[(key, value)]``（可直接写入 astropy 头）。

    - 契约 §3.2 的 8 字符键：DATE-OBS/EXPOSURE/GAIN/BLACKLEV/TEMP/FRAMETYP/
      CAMERA/SERIAL/SESSION/NFRAME/MEAN/STD/SATPCT/BITPIX（数值取自
      ``meta["props"]``，缺失时按 0.0/unknown 兜底）；
    - 扩展参数写 HIERARCH 键：``HIERARCH CAM WIDTH`` 等（仅当 props 中存在
      对应键时写入，Mock 专属键 MOCKPAT/MOCKSCE/MOCKDRIFT 亦如此）；
    - 用户 comment：经 ``ascii_fits_value()`` 转 printable ASCII（FITS 头只收
      ASCII），多行按 ``\\n`` 拆成多条 ``COMMENT``；空行跳过。
    """
    props = dict((meta or {}).get("props") or {})
    camera = (meta or {}).get("camera") or {}
    stats = stats or {}
    entries: list = [
        ("DATE-OBS", date_obs or datetime.now().isoformat(timespec="milliseconds")),
        ("EXPOSURE", _meta_float(props.get("exposure"))),
        ("GAIN", _meta_float(props.get("gain"))),
        ("BLACKLEV", _meta_float(props.get("black_level"))),
        ("TEMP", _meta_float(props.get("device_temperature"))),
        ("FRAMETYP", str((meta or {}).get("frametype") or "").upper()),
        ("CAMERA", ascii_fits_value(camera.get("name") or "unknown")),
        ("SERIAL", ascii_fits_value(camera.get("serial") or "unknown")),
        ("SESSION", ascii_fits_value(str((meta or {}).get("session") or ""))),
        ("NFRAME", int((meta or {}).get("nframe") or 0)),
        (FITS_HEADER_MEAN, _meta_float(stats.get("mean"))),
        (FITS_HEADER_STD, _meta_float(stats.get("std"))),
        (FITS_HEADER_SATPCT, _meta_float(stats.get("saturated_pct"))),
        ("BITPIX", 16),
    ]
    for prop_key, hier_key in CAM_HIERARCH_KEYS:
        if prop_key in props:
            entries.append((f"HIERARCH CAM {hier_key}", props[prop_key]))
    comment = str((meta or {}).get("comment") or "")
    for line in comment.split("\n"):
        if line.strip():
            entries.append(("COMMENT", ascii_fits_value(line)))
    return entries


def tiff_metadata(meta: dict) -> tuple:
    """生成 tifffile.imwrite 的元数据参数。

    返回 ``(description, extratags, software)``：
    - ``description``（tag 270 ImageDescription）：参数+身份的 ASCII JSON
      （可机读；不含中文 comment —— 中文统一走 tag 65000 的 UTF-8 字节）；
    - ``extratags``：私有 tag 65000 = 完整 meta JSON 的 UTF-8 字节
      （含中文 comment；dtype 'B'，writeonce=False）；
    - ``software``（tag 305）："Static AstroPhoto"。
    """
    camera = (meta or {}).get("camera") or {}
    props = dict((meta or {}).get("props") or {})
    description = json.dumps(
        {"camera": camera, "props": props},
        ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    payload = json.dumps(
        meta or {}, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")
    extratags = [(TIFF_METADATA_TAG, "B", len(payload), payload, False)]
    return description, extratags, TIFF_SOFTWARE
