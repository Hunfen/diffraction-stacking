"""Camera-parameter snapshot: the session ``camera_props.json`` plus the FITS cards derived from it.

The full parameter set (about 164 entries on the real ic4 camera) is read once when a capture session
starts and stored as a flat ``{name: value}`` mapping next to the session directory. Frames and
derived products only carry a ``CAMPARS`` pointer card, so the per-frame acquisition path never pays
for a full property read.

Pointer convention: ``CAMPARS`` is always relative to its own root (the session directory for raw /
calibrated / masters / results, the ``snapshots`` directory for snapshots), so the value is the plain
file name ``camera_props.json``. A session that predates this convention has no file and gets
``CAMPARS = 'none'`` instead of an error.
"""
from __future__ import annotations

import json
import logging
import math
import statistics
from pathlib import Path

PROPS_FILENAME = "camera_props.json"
CAMPARS_KEY = "CAMPARS"
CAMPARS_NONE = "none"

# Cards copied from the raw frame headers onto derived products; the names match
# ``util.fits_header_entries`` so a reader sees the same keywords on every product.
SCALAR_KEYS = ("EXPOSURE", "GAIN", "BLACKLEV")
TEMPERATURE_KEY = "TEMP"
ROI_KEYS = ("WIDTH", "HEIGHT", "OFFSETX", "OFFSETY", "REVERSX", "REVERSY")
# Discrete acquisition settings: a mismatch between contributing frames raises MIXED and is recorded
# per distinct value in the matching results JSON.
MIXED_KEYS = ("EXPOSURE", "GAIN") + ROI_KEYS
INT_KEYS = set(ROI_KEYS)

logger = logging.getLogger("uvicorn.error")


# ---------------------------------------------------------------- properties file
def device_info(camera) -> dict:
    """Return the identity block stored in ``camera_props.json``.

    ``camera`` may be the camera service (which wraps the device) or the device itself, so the
    identity is taken from ``info()`` when available and from plain attributes otherwise.
    """
    info: dict = {}
    for candidate in (camera, getattr(camera, "device", None)):
        if candidate is None:
            continue
        getter = getattr(candidate, "info", None)
        if callable(getter):
            try:
                info = getter() or {}
            except Exception as exc:  # noqa: BLE001
                logger.warning("camera_props: device info() failed: %s", exc)
                info = {}
            if info:
                break
        if any(getattr(candidate, key, None) for key in ("id", "name", "serial", "kind")):
            info = {key: getattr(candidate, key, None)
                    for key in ("id", "name", "serial", "kind")}
            break
    return {key: str(info.get(key) or "") for key in ("id", "name", "serial", "kind")}


def collect_props(camera) -> dict | None:
    """Read the full property set once; ``None`` when the camera cannot provide it."""
    entries = camera.get_all_properties()
    if not isinstance(entries, (list, tuple)):
        raise RuntimeError("get_all_properties did not return a list")
    props = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not name:
            continue
        props[str(name)] = entry.get("value")
    return props


def write_props_file(path: Path, props: dict | None, device: dict | None,
                     session: str | None, generated_at: str | None = None) -> dict:
    """Write ``camera_props.json`` with the metadata block plus a flat ``props`` mapping."""
    payload = {
        "generated_at": generated_at,
        "device": device or {},
        "session": session,
        "props": props,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
    return payload


def ensure_session_props(session_dir: Path, camera, session_id: str,
                         generated_at: str | None = None) -> dict:
    """Snapshot the camera parameters into ``<session>/camera_props.json`` (never raises).

    A failed read does not block the acquisition: the file is still written with ``props: null`` so
    the ``CAMPARS`` pointer of every frame stays resolvable, and the failure is only logged.
    """
    device = {}
    props = None
    try:
        device = device_info(camera)
    except Exception as exc:  # noqa: BLE001
        logger.warning("camera_props: device identity unavailable: %s", exc)
    try:
        props = collect_props(camera)
    except Exception as exc:  # noqa: BLE001
        logger.warning("camera_props: full property read failed: %s", exc)
        props = None
    try:
        return write_props_file(Path(session_dir) / PROPS_FILENAME, props, device,
                                session_id, generated_at=generated_at)
    except OSError as exc:
        logger.warning("camera_props: failed to write %s: %s", PROPS_FILENAME, exc)
        return {"generated_at": generated_at, "device": device,
                "session": session_id, "props": props}


def read_session_props(session_dir: Path) -> dict | None:
    """Return the stored snapshot payload, or ``None`` when the session predates it."""
    path = Path(session_dir) / PROPS_FILENAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("camera_props: unreadable %s: %s", path, exc)
        return None
    return payload if isinstance(payload, dict) else None


def session_campers(session_dir: Path) -> str:
    """Value of the ``CAMPARS`` card for a session: the file name, or ``'none'`` for legacy sessions."""
    return PROPS_FILENAME if (Path(session_dir) / PROPS_FILENAME).is_file() else CAMPARS_NONE


def ensure_snapshot_props(snapshots_dir: Path, props: dict | None,
                          device: dict | None) -> str:
    """Make sure ``<snapshots>/camera_props.json`` exists and return the ``CAMPARS`` value."""
    path = Path(snapshots_dir) / PROPS_FILENAME
    if not path.is_file():
        try:
            write_props_file(path, props if props is not None else None, device, None)
        except OSError as exc:
            logger.warning("camera_props: failed to write %s: %s", path, exc)
    return PROPS_FILENAME


def camera_identity(session_dir: Path, rows: list[dict] | None = None) -> tuple[str, str]:
    """Return ``(name, serial)`` from the stored snapshot, falling back to the raw frame headers."""
    payload = read_session_props(session_dir) or {}
    device = payload.get("device") or {}
    name = str(device.get("name") or "")
    serial = str(device.get("serial") or "")
    if name and serial:
        return name, serial
    for row in rows or []:
        header = row.get("header") or {}
        name = name or str(header.get("CAMERA") or "")
        serial = serial or str(header.get("SERIAL") or "")
        if name and serial:
            break
    return name or "unknown", serial or "unknown"


# ---------------------------------------------------------------- header aggregation
def _raw_card(header, key: str):
    """Read a card from a raw frame header.

    Raw frames carry the ROI / reverse / trigger settings as ``HIERARCH CAM <KEY>`` cards (see
    ``util.CAM_HIERARCH_KEYS``), while the photometry settings are plain cards.
    """
    if not header:
        return None
    value = header.get(key)
    if value is None:
        value = header.get("HIERARCH CAM " + key)
    if value is None:
        # ``dict(header)`` normalises a HIERARCH card to its bare long keyword.
        value = header.get("CAM " + key)
    return value


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _values(rows: list[dict], key: str) -> list[tuple[int, float]]:
    out = []
    for row in rows:
        number = _number(_raw_card(row.get("header"), key))
        if number is not None:
            out.append((int(row.get("nframe") or 0), number))
    return out


def _card_value(key: str, value: float):
    return int(round(value)) if key in INT_KEYS else float(value)


def _median(values: list[tuple[int, float]]) -> float | None:
    if not values:
        return None
    return float(statistics.median([v for _, v in values]))


def temperature_entries(rows: list[dict]) -> list[tuple[str, object]]:
    """``TEMP_MED`` / ``TEMP_MIN`` / ``TEMP_MAX`` aggregated over the contributing raw frames."""
    temps = [v for _, v in _values(rows, TEMPERATURE_KEY)]
    if not temps:
        return [("TEMP_MED", 0.0), ("TEMP_MIN", 0.0), ("TEMP_MAX", 0.0)]
    med = float(statistics.median(temps))
    return [("TEMP_MED", med), ("TEMP_MIN", float(min(temps))),
            ("TEMP_MAX", float(max(temps)))]


def mixed_lists(rows: list[dict]) -> dict[str, dict[str, list[int]]]:
    """Map each discrete card with more than one distinct value to ``{value: [nframe, ...]}``."""
    mixed: dict[str, dict[str, list[int]]] = {}
    for key in MIXED_KEYS:
        buckets: dict[str, list[int]] = {}
        for nframe, value in _values(rows, key):
            text = str(_card_value(key, value))
            buckets.setdefault(text, []).append(nframe)
        if len(buckets) > 1:
            mixed[key] = buckets
    return mixed


def frame_entries(header, campers: str,
                  camera: tuple[str, str] | None = None) -> list[tuple[str, object]]:
    """Cards for one calibrated frame: the raw acquisition settings plus the CAMPARS pointer."""
    entries: list[tuple[str, object]] = []
    for key in SCALAR_KEYS + (TEMPERATURE_KEY,) + ROI_KEYS:
        number = _number(_raw_card(header, key))
        if number is not None:
            entries.append((key, _card_value(key, number)))
    name, serial = camera or ("unknown", "unknown")
    entries.append(("CAMERA", name))
    entries.append(("SERIAL", serial))
    entries.append((CAMPARS_KEY, campers))
    return entries


def master_entries(rows: list[dict], campers: str,
                   camera: tuple[str, str] | None = None) -> list[tuple[str, object]]:
    """Cards for a master frame: only the temperature range plus camera identity and CAMPARS."""
    entries = temperature_entries(rows)
    name, serial = camera or ("unknown", "unknown")
    entries.append(("CAMERA", name))
    entries.append(("SERIAL", serial))
    entries.append((CAMPARS_KEY, campers))
    return entries


def acquisition_entries(rows: list[dict], campers: str,
                        camera: tuple[str, str] | None = None) -> tuple[list[tuple[str, object]], dict]:
    """Cards for a multi-frame derived product (STACK): medians, temperature range and MIXED.

    Returns ``(entries, mixed)`` where ``mixed`` is empty unless a discrete setting differs between
    the contributing frames.
    """
    entries: list[tuple[str, object]] = []
    for key in SCALAR_KEYS:
        med = _median(_values(rows, key))
        if med is not None:
            entries.append((key, float(med)))
    entries.extend(temperature_entries(rows))
    for key in ROI_KEYS:
        med = _median(_values(rows, key))
        if med is not None:
            entries.append((key, _card_value(key, med)))
    name, serial = camera or ("unknown", "unknown")
    entries.append(("CAMERA", name))
    entries.append(("SERIAL", serial))
    mixed = mixed_lists(rows)
    if mixed:
        entries.append(("MIXED", True))
    entries.append((CAMPARS_KEY, campers))
    return entries, mixed


def copy_cards(source_header, keys: tuple[str, ...] | None = None) -> list[tuple[str, object]]:
    """Copy the camera cards of an existing product header (used by the STRETCH step)."""
    wanted = keys or (SCALAR_KEYS + (TEMPERATURE_KEY, "TEMP_MED", "TEMP_MIN", "TEMP_MAX")
                      + ROI_KEYS + ("CAMERA", "SERIAL", "NCOMBINE", "NREJECT", "MIXED",
                                    CAMPARS_KEY))
    entries = []
    for key in wanted:
        if key in source_header:
            entries.append((key, source_header[key]))
    return entries


def merge_mixed(existing, new: dict[str, dict[str, list[int]]]) -> dict:
    """Append ``new`` value/frame lists into an existing ``mixed`` section (no duplicates)."""
    merged = dict(existing) if isinstance(existing, dict) else {}
    for key, buckets in new.items():
        current = merged.get(key)
        if not isinstance(current, dict):
            current = {}
        for value, frames in buckets.items():
            frame_list = list(current.get(value) or [])
            for nframe in frames:
                if nframe not in frame_list:
                    frame_list.append(nframe)
            current[value] = sorted(frame_list)
        merged[key] = current
    return merged
