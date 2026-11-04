"""Shared utilities: paths, time, image encoding, statistics."""
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

# Session name sanitizing: path separators + Windows-illegal characters + whitespace -> "_"
_ILLEGAL_SESSION_CHARS = re.compile(r'[<>:"/\\|?*\s]')

# Snapshot prefix sanitizing (v1.4): same rules as a session name, but the result may be empty
_ILLEGAL_PREFIX_CHARS = re.compile(r'[<>:"/\\|?*\s]')


def ok(data):
    """Wrap a successful response."""
    return {"ok": True, "data": data, "error": None}


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%dT%H-%M-%S")


def sanitize_session_name(name: str) -> str:
    """v1.2: sanitise the raw user session_name.

    Strip or replace path separators, the Windows-invalid characters < > : " / \\ | ? * and whitespace;
    empty input returns "session". Non-ASCII characters (e.g. Chinese) and legal characters such as
    underscores are preserved.
    Windows does not allow a directory name to end with a dot, so trailing dots are removed after sanitising.
    """
    raw = (name or "").strip()
    if not raw:
        return "session"
    cleaned = _ILLEGAL_SESSION_CHARS.sub("_", raw).rstrip(".")
    return cleaned or "session"


def sanitize_prefix(prefix: str | None) -> str:
    """v1.4: sanitise the snapshot filename prefix.

    Path separators, the Windows-invalid characters `< > : " | ? *` and whitespace are all replaced with `_`;
    missing/empty input returns "" (empty is allowed). Non-ASCII characters (e.g. Chinese) are preserved.
    """
    return _ILLEGAL_PREFIX_CHARS.sub("_", prefix or "")


def ascii_fits_value(value: str) -> str:
    """Convert an arbitrary string to printable ASCII that a FITS header can hold (v1.2 FIX).

    - Prefer transliteration: NFKD strips accents (é -> e) and combining marks are dropped;
    - Every remaining non-ASCII character is replaced with "_";
    - Non-printable ASCII is replaced with "_";
    - Returns "SESSION" when the result is empty.
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
    """Legacy-compatible internal sanitising rule (ASCII filename-safe characters only)."""
    s = re.sub(r"[^0-9A-Za-z_\-\.]", "_", name).strip("_")
    return s or "session"


# =========================================================================
# Dynamic data root (v1.2)
# =========================================================================
def _load_persisted_data_root() -> Path | None:
    """Read backend/.data_root.json; return None when it is corrupt or missing."""
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
    """Singleton, thread-safe dynamic data-directory setting.

    Startup priority: DIFF_DATA_ROOT > backend/.data_root.json > <repo>/data.
    A runtime PUT /api/settings switches the current value immediately and persists it to .data_root.json.
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
        """Persist and switch the current data root (write to disk first, then take effect)."""
        root = Path(path).resolve()
        payload = {"data_root": str(root)}
        with self._lock:
            SETTINGS_FILE.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            self._root = root


_settings = DataRootSettings()


def get_data_root() -> Path:
    """Dynamic accessor: reads the currently effective data_root on every call."""
    return _settings.get()


def set_data_root(path: Path | str) -> None:
    """Switch and persist the data_root at runtime (thread-safe)."""
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
    """uint16 (<=4095) -> uint8 greyscale: linear mapping between the 1% and 99% percentiles."""
    lo, hi = np.percentile(frame, (1.0, 99.0))
    if hi <= lo:
        lo = float(np.min(frame))
        hi = float(np.max(frame))
    if hi <= lo:
        hi = lo + 1.0
    mapped = (frame.astype(np.float32) - lo) * (255.0 / (hi - lo))
    return np.clip(mapped, 0, 255).astype(np.uint8)


def frame_stats(frame: np.ndarray) -> dict:
    """12-bit frame statistics (min/max/mean/std/saturated_pct are all based on raw uint16 values)."""
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
# FITS/TIFF capture metadata (v1.6: full camera parameters + user comment)
# =========================================================================

# Extension parameter names -> FITS HIERARCH CAM keys (values come from the props dict; mock-only keys are written only when present).
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

# TIFF private tag: the full capture metadata JSON (UTF-8 bytes, including a Chinese comment).
TIFF_METADATA_TAG = 65000
TIFF_SOFTWARE = "Static AstroPhoto"


def _meta_float(value, default=0.0) -> float:
    """Safely convert a value in props to float; fall back to the default for non-numeric or
    non-finite values (inf/nan), so that writing a frame or the whole file does not fail because of it."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return float(default)
    return f if math.isfinite(f) else float(default)


def capture_metadata(props: dict, cam_name: str, cam_serial: str,
                     frametype: str, session: str, nframe: int,
                     comment: str = "") -> dict:
    """Build the capture metadata dict: the single source of truth for FITS/TIFF headers.

    - ``props``: the full ``{name: value}`` mapping from camera.get_properties()
      (the 13 contract properties plus Mock's mock_pattern/mock_scene/mock_drift);
    - Camera identity (name/serial), frametype, session, nframe and comment are stored separately;
    - ``comment`` keeps the user's raw text (Chinese allowed): the FITS header converts it to
      printable ASCII through ``ascii_fits_value()``, while TIFF private tag 65000 stores it
      directly as UTF-8 bytes (neither loses the original input; only the storage form differs).
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
                        date_obs: str | None = None,
                        campers: str | None = None) -> list:
    """Build the FITS header key/value list ``list[(key, value)]`` (directly writable into an astropy header).

    - The 8-character keys of contract section 3.2: DATE-OBS/EXPOSURE/GAIN/BLACKLEV/TEMP/FRAMETYP/
      CAMERA/SERIAL/SESSION/NFRAME/MEAN/STD/SATPCT/BITPIX (numeric values come from
      ``meta["props"]``, falling back to 0.0/unknown when missing);
    - Extended parameters are written as HIERARCH keys: ``HIERARCH CAM WIDTH`` and so on (only when
      the corresponding key exists in props; the Mock-only keys MOCKPAT/MOCKSCE/MOCKDRIFT likewise);
    - User comment: converted to printable ASCII through ``ascii_fits_value()`` (FITS headers only
      accept ASCII); multiple lines are split on ``\\n`` into several ``COMMENT`` cards; empty lines are skipped.
    - ``campers``: when given, a ``CAMPARS`` card points at the session ``camera_props.json`` (the
      full 164-item parameter snapshot); see ``camera_props`` for the pointer convention.
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
    if campers:
        entries.append(("CAMPARS", str(campers)))
    comment = str((meta or {}).get("comment") or "")
    for line in comment.split("\n"):
        if line.strip():
            entries.append(("COMMENT", ascii_fits_value(line)))
    return entries


def tiff_metadata(meta: dict) -> tuple:
    """Build the metadata arguments for tifffile.imwrite.

    Returns ``(description, extratags, software)``:
    - ``description`` (tag 270 ImageDescription): ASCII JSON of the parameters and identity
      (machine readable; it does not include the Chinese comment -- Chinese always goes through
      the UTF-8 bytes of tag 65000);
    - ``extratags``: private tag 65000 = UTF-8 bytes of the full meta JSON
      (including the Chinese comment; dtype 'B', writeonce=False);
    - ``software`` (tag 305): "Static AstroPhoto".
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
