"""通用工具：路径、时间、图像编码、统计。"""
from __future__ import annotations

import json
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


def histogram_256(img8: np.ndarray) -> list[int]:
    """8-bit 预览图 256 bin 直方图。"""
    counts = np.bincount(np.asarray(img8, dtype=np.uint8).ravel(), minlength=256)
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
