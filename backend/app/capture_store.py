"""Capture plan persistence: the working draft and named capture sets.

Both live in the dynamic data root as separate JSON files:

- ``capture_draft.json`` always holds exactly one draft (the phase table
  currently shown on the Capture page) and is overwritten on every save.
- ``capture_sets.json`` holds the named sets the user saved explicitly.

The two files are written independently: saving a draft never rewrites the
named sets, so an unsaved edit cannot clobber a saved set.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .capture import PHASE_TYPES
from .util import get_data_root, now_iso

DRAFT_FILENAME = "capture_draft.json"
SETS_FILENAME = "capture_sets.json"
MAX_COUNT = 10000

_lock = threading.RLock()


class CaptureStoreError(RuntimeError):
    """A persisted capture file exists but cannot be used.

    The message always names the offending file so the API layer can report it
    without leaking arbitrary internals. Callers must not treat this as an
    empty store: writing over a damaged file would destroy saved data.
    """


def normalize_name(name: object) -> str:
    """Trim a user supplied set name; raise ValueError when nothing is left."""
    clean = str(name).strip() if name is not None else ""
    if not clean:
        raise ValueError("Capture set name must not be empty")
    return clean


def _normalize_phase(phase: object) -> dict:
    """Validate one phase with the same rules as CaptureService.plan()."""
    if not isinstance(phase, dict):
        raise ValueError("Each phase must be an object")
    ptype = phase.get("type")
    if ptype not in PHASE_TYPES:
        raise ValueError(f"Unknown phase type: {ptype}")
    try:
        count = int(phase.get("count"))
    except (TypeError, ValueError):
        raise ValueError(f"count for {ptype} must be an integer")
    if count < 1 or count > MAX_COUNT:
        raise ValueError(f"count for {ptype} must be between 1 and {MAX_COUNT}")
    exposure_us = phase.get("exposure_us")
    if exposure_us is not None:
        try:
            exposure_us = float(exposure_us)
        except (TypeError, ValueError):
            raise ValueError(f"exposure_us for {ptype} must be a number or null")
    return {"type": ptype, "count": count, "exposure_us": exposure_us}


def normalize_phases(phases: object) -> list[dict]:
    """Validate phases for a named set: exactly type/count/exposure_us."""
    if not isinstance(phases, list):
        raise ValueError("phases must be a list")
    return [_normalize_phase(phase) for phase in phases]


def normalize_draft_phases(phases: object) -> list[dict]:
    """Validate the working draft, which additionally carries the phase note.

    The note ends up in the FITS COMMENT of every frame, so it must survive a
    page refresh. Named sets deliberately stay note-free.
    """
    if not isinstance(phases, list):
        raise ValueError("phases must be a list")
    normalized: list[dict] = []
    for phase in phases:
        item = _normalize_phase(phase)
        note = phase.get("note")  # type: ignore[union-attr]
        item["note"] = None if note is None else str(note)
        normalized.append(item)
    return normalized


def _path(filename: str) -> Path:
    return get_data_root() / filename


def _read_json(path: Path, filename: str) -> dict:
    """Load one persisted JSON object.

    A missing file is the normal first-run state and yields ``{}``. A file
    that exists but cannot be parsed raises :class:`CaptureStoreError` instead
    of being treated as empty, because the next write would otherwise replace
    data the user already saved.
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CaptureStoreError(f"Cannot read {filename}: {exc}") from exc
    if not isinstance(data, dict):
        raise CaptureStoreError(f"{filename} must contain a JSON object")
    return data


def _write(path: Path, payload: dict) -> None:
    """Write JSON through a temporary file so a crash cannot truncate it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def _draft_payload(data: dict) -> dict:
    phases = data.get("phases", [])
    if not isinstance(phases, list):
        raise CaptureStoreError(f"{DRAFT_FILENAME} has an invalid phases list")
    updated_at = data.get("updated_at")
    return {
        "phases": phases,
        "updated_at": updated_at if isinstance(updated_at, str) else None,
    }


def get_draft() -> dict:
    """Return the persisted draft; an absent draft yields empty phases."""
    with _lock:
        return _draft_payload(_read_json(_path(DRAFT_FILENAME), DRAFT_FILENAME))


def save_draft(phases: object) -> dict:
    """Persist the draft. Only capture_draft.json is written."""
    payload = {"phases": normalize_draft_phases(phases), "updated_at": now_iso()}
    with _lock:
        # A draft that cannot be read must never be replaced by this save.
        _draft_payload(_read_json(_path(DRAFT_FILENAME), DRAFT_FILENAME))
        _write(_path(DRAFT_FILENAME), payload)
    return payload


def _sets() -> dict:
    data = _read_json(_path(SETS_FILENAME), SETS_FILENAME)
    sets = data.get("sets", {})
    if not isinstance(sets, dict):
        raise CaptureStoreError(f"{SETS_FILENAME} has an invalid sets object")
    for name, entry in sets.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("phases", []), list):
            raise CaptureStoreError(f"{SETS_FILENAME} has an invalid entry for set {name!r}")
    return sets


def list_sets() -> list[str]:
    with _lock:
        names = list(_sets().keys())
    return sorted(names)


def get_set(name: object) -> dict | None:
    clean = normalize_name(name)
    with _lock:
        entry = _sets().get(clean)
    if not isinstance(entry, dict):
        return None
    phases = entry.get("phases")
    updated_at = entry.get("updated_at")
    return {
        "phases": phases if isinstance(phases, list) else [],
        "updated_at": updated_at if isinstance(updated_at, str) else None,
    }


def put_set(name: object, phases: object) -> dict:
    """Create or overwrite a named set."""
    clean = normalize_name(name)
    entry = {"phases": normalize_phases(phases), "updated_at": now_iso()}
    with _lock:
        sets = dict(_sets())
        sets[clean] = entry
        _write(_path(SETS_FILENAME), {"sets": sets})
    return entry


def delete_set(name: object) -> bool:
    """Delete a named set; False when it does not exist."""
    clean = normalize_name(name)
    with _lock:
        sets = dict(_sets())
        if clean not in sets:
            return False
        del sets[clean]
        _write(_path(SETS_FILENAME), {"sets": sets})
    return True
