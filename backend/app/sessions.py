"""Session metadata reading (disk scan)."""
from __future__ import annotations

import json
from pathlib import Path

from .util import (FITS_HEADER_MEAN, FITS_HEADER_STD,
                   FITS_HEADER_SATPCT, get_captures_dir)

FRAME_GROUPS = ("bias", "dark", "flat", "light")


def _safe_session_id(session_id: str) -> bool:
    return bool(session_id) and "/" not in session_id and "\\" not in session_id \
        and session_id not in (".", "..")


def _count_group(session_dir: Path, group: str) -> int:
    d = session_dir / group
    if not d.is_dir():
        return 0
    return len(list(d.glob("*.fits")))


def list_sessions() -> list[dict]:
    captures_dir = get_captures_dir()
    if not captures_dir.is_dir():
        return []
    out = []
    for d in sorted(captures_dir.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        meta = {}
        meta_path = d / "meta.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                meta = {}
        name = meta.get("name") or d.name
        created = meta.get("created") or d.name[:8]
        results = d / "results"
        out.append({
            "id": d.name,
            "name": name,
            "created": created,
            "frame_counts": {g: _count_group(d, g) for g in FRAME_GROUPS},
            "has_results": results.is_dir() and any(results.iterdir()),
        })
    return out


def _frames_of_group(session_id: str, group_dir: Path) -> list[dict]:
    frames = []
    if not group_dir.is_dir():
        return frames
    for p in sorted(group_dir.glob("*.fits")):
        mean = std = sat = None
        try:
            from astropy.io import fits
            with fits.open(p) as hdul:
                hdr = hdul[0].header
                mean = float(hdr.get(FITS_HEADER_MEAN, float("nan")))
                std = float(hdr.get(FITS_HEADER_STD, float("nan")))
                sat = float(hdr.get(FITS_HEADER_SATPCT, float("nan")))
        except Exception:  # noqa: BLE001
            pass
        frames.append({
            "file": p.name,
            "url": f"/api/files/{session_id}/{group_dir.name}/{p.name}",
            "thumb": f"/api/files/{session_id}/{group_dir.name}/thumbs/"
                     f"{p.stem}.png",
            "mean": mean,
            "std": std,
            "saturated_pct": sat,
        })
    return frames


def _results_of_session(session_id: str, results_dir: Path) -> list[dict]:
    out = []
    if not results_dir.is_dir():
        return out
    for p in sorted(results_dir.glob("*.fits")):
        preview = None
        for cand in ("STACK_preview.png", f"{p.stem}.png", f"{p.stem}_preview.png"):
            if (results_dir / cand).is_file():
                preview = f"/api/files/{session_id}/results/{cand}"
                break
        out.append({
            "file": p.name,
            "url": f"/api/files/{session_id}/results/{p.name}",
            "preview": preview,
        })
    return out


def get_session(session_id: str) -> dict | None:
    if not _safe_session_id(session_id):
        return None
    d = get_captures_dir() / session_id
    if not d.is_dir():
        return None
    meta = {}
    meta_path = d / "meta.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            meta = {}
    frames = {g: _frames_of_group(session_id, d / g) for g in FRAME_GROUPS}
    qc = None
    qc_path = d / "results" / "qc_report.json"
    if qc_path.is_file():
        try:
            qc = json.loads(qc_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            qc = None
    masters_dir = d / "masters"
    calibrated_dir = d / "calibrated"
    return {
        "id": d.name,
        "name": meta.get("name") or d.name,
        "created": meta.get("created") or d.name[:8],
        "phases": meta.get("phases") or [],
        "frames": frames,
        "results": _results_of_session(session_id, d / "results"),
        "qc": qc,
        "has_masters": masters_dir.is_dir() and any(masters_dir.glob("*.fits")),
        "has_calibrated": calibrated_dir.is_dir() and any(calibrated_dir.glob("*.fits")),
    }
