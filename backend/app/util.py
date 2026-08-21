"""通用工具：路径、时间、图像编码、统计。"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = Path(os.environ.get("DIFF_DATA_ROOT", str(REPO_ROOT / "data")))
SNAPSHOT_DIR = DATA_ROOT / "snapshots"
CAPTURES_DIR = DATA_ROOT / "captures"

MAX_ADU = 4095
FITS_HEADER_MEAN = "MEAN"
FITS_HEADER_STD = "STD"
FITS_HEADER_SATPCT = "SATPCT"


def ok(data):
    """统一成功响应包裹。"""
    return {"ok": True, "data": data, "error": None}


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%dT%H-%M-%S")


def safe_name(name: str) -> str:
    s = re.sub(r"[^0-9A-Za-z_\-\.]", "_", name).strip("_")
    return s or "session"


def ensure_dirs() -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)


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
