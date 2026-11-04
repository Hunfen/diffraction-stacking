"""CaptureService: four-phase sequence acquisition state machine.

State machine (per the contract):
    idle -> running -> waiting_user (before each phase) -> capturing
         -> ... -> finished | aborted | error
The waiting_user before the LIGHT phase is a **mandatory manual confirmation point**;
acquisition continues only after resume.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

import numpy as np
import tifffile
from PIL import Image

from .camera import CameraService
from .camera_props import (CAMPARS_NONE, PROPS_FILENAME,
                           ensure_session_props, ensure_snapshot_props)
from .fits_io import write_fits
from .util import (auto_contrast_8bit, capture_metadata, fits_header_entries,
                   frame_stats, get_captures_dir, get_data_root,
                   get_snapshots_dir, now_iso, now_stamp,
                   sanitize_session_name, tiff_metadata)

PHASE_TYPES = ("bias", "dark", "flat", "light")
ACTIVE_STATUSES = {"running", "waiting_user", "capturing"}

PROMPTS = {
    "bias": "Cover the lens (sensor in complete darkness), then confirm to start capturing Bias (shortest exposure)",
    "dark": "Keep the lens covered. Dark uses the same exposure as Light. Confirm to start capturing Dark",
    "flat": "Provide uniform illumination (defocused beam or flat light source) with no diffraction spots visible, then confirm to start capturing Flat",
    "light": "Restore normal diffraction conditions. Check the exposure/gain parameters against the live preview and histogram, then confirm to start the continuous Light capture",
}

MAX_CONSECUTIVE_FAILURES = 5


def _empty_state() -> dict:
    return {
        "status": "idle",
        "session_name": None,
        "current_phase": None,
        "frames_done": 0,
        "total_frames": 0,
        "prompt": None,
        "message": None,
        "needs_user": False,
        "log": [],
    }


class CaptureService:
    def __init__(self, camera_service: CameraService, broadcast=None):
        self.camera = camera_service
        self.broadcast = broadcast
        self._lock = threading.RLock()
        self._state = _empty_state()
        self._plan: dict | None = None
        self._session_id: str | None = None
        self._session_dir: Path | None = None
        self._resume_event = threading.Event()
        self._abort_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ state
    def _set(self, **fields):
        with self._lock:
            self._state.update(fields)

    def get_state(self) -> dict:
        with self._lock:
            state = json.loads(json.dumps(self._state, ensure_ascii=False))
        return state

    def _log(self, msg: str) -> None:
        with self._lock:
            self._state["log"].append({"t": now_iso(), "msg": msg})
            self._state["log"] = self._state["log"][-200:]
            self._state["message"] = msg

    def _push(self) -> None:
        if self.broadcast is None:
            return
        state = self.get_state()
        self.broadcast("capture", {
            "status": state["status"],
            "session_name": state["session_name"],
            "current_phase": state["current_phase"],
            "frames_done": state["frames_done"],
            "total_frames": state["total_frames"],
            "prompt": state["prompt"],
            "message": state["message"],
            "needs_user": state["needs_user"],
        })

    # ------------------------------------------------------------ plan
    def plan(self, session_name: str, phases: list[dict]) -> dict:
        with self._lock:
            if self._state["status"] in ACTIVE_STATUSES:
                raise RuntimeError("Capture in progress, cannot create a new plan")
            normalized = []
            for ph in phases:
                ptype = ph["type"]
                count = int(ph["count"])
                if ptype not in PHASE_TYPES:
                    raise RuntimeError(f"Unknown phase type: {ptype}")
                if count < 1 or count > 10000:
                    raise RuntimeError(f"count for {ptype} must be between 1 and 10000")
                exposure_us = ph.get("exposure_us")
                if exposure_us is None and self.camera.is_open():
                    exposure_us = self.camera.get_property("exposure")
                normalized.append({
                    "type": ptype,
                    "count": count,
                    "exposure_us": None if exposure_us is None else float(exposure_us),
                    "note": ph.get("note") or "",
                })
            plan_id = "plan-" + uuid.uuid4().hex[:8]
            # v1.2: session_name is the raw user input; effective = local timestamp_sanitized name.
            effective = f"{now_stamp()}_{sanitize_session_name(session_name)}"
            self._plan = {
                "plan_id": plan_id,
                "session_name": effective,
                "phases": normalized,
            }
            return dict(self._plan)

    # ------------------------------------------------------------ control
    def start(self) -> dict:
        with self._lock:
            if self._plan is None:
                raise RuntimeError("Create a capture plan first (POST /api/capture/plan)")
            if self._state["status"] in ACTIVE_STATUSES:
                raise RuntimeError("Capture is already running")
            if not self.camera.is_open():
                raise RuntimeError("Device is not open. Open the camera first.")
            self._abort_event.clear()
            self._resume_event.clear()
            self._session_id = None
            self._session_dir = None
            phases = self._plan["phases"]
            total = sum(int(p["count"]) for p in phases)
            self._state.update({
                "status": "running",
                "session_name": self._plan["session_name"],
                "current_phase": None,
                "frames_done": 0,
                "total_frames": total,
                "prompt": None,
                "message": "Capture started",
                "needs_user": False,
                "log": [{"t": now_iso(), "msg": "Capture started"}],
            })
            self._push()
            self._thread = threading.Thread(
                target=self._run, args=(dict(self._plan),), daemon=True,
                name="capture-runner")
            self._thread.start()
            return {"status": "running"}

    def resume(self) -> dict:
        with self._lock:
            if self._state["status"] != "waiting_user":
                raise RuntimeError("Not in waiting_user state, no resume needed")
            if not self._state.get("needs_user"):
                raise RuntimeError("Current phase needs no manual confirmation and will continue automatically")
            self._resume_event.set()
            self._state["status"] = "capturing"
            self._state["prompt"] = None
            self._state["message"] = "Parameters confirmed, starting LIGHT capture"
            self._state["needs_user"] = False
            self._push()
            return {"status": "capturing"}

    def abort(self) -> dict:
        with self._lock:
            if self._state["status"] not in ACTIVE_STATUSES:
                raise RuntimeError("No capture in progress")
            self._abort_event.set()
            self._resume_event.set()   # End immediately when waiting for manual confirmation
            # Switch to aborted synchronously so the API does not report aborted while state still shows
            # running/waiting_user/capturing.
            self._state.update(
                status="aborted", prompt=None, message="Capture aborted",
                needs_user=False)
            self._push()
            return {"status": "aborted"}

    def shutdown(self) -> None:
        """Service shutdown: gracefully abort an in-progress capture and wait for the acquisition thread to exit."""
        with self._lock:
            if self._state["status"] in ACTIVE_STATUSES:
                self._abort_event.set()
                self._resume_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)

    # ------------------------------------------------------------ execution
    def _run(self, plan: dict) -> None:
        self._created = now_iso()
        # v1.2: plan already built the effective session_name (timestamp_sanitized name),
        # and the session folder id stays identical to it.
        session_id = plan["session_name"]
        session_dir = get_captures_dir() / session_id
        try:
            session_dir.mkdir(parents=True, exist_ok=False)
            for g in PHASE_TYPES:
                (session_dir / g / "thumbs").mkdir(parents=True, exist_ok=True)
            self._set(session_name=plan["session_name"], message="Creating session directory")
            self._write_meta(session_dir, session_id, plan, "running")
        except FileExistsError:
            session_id = f"{plan['session_name']}_{uuid.uuid4().hex[:4]}"
            session_dir = get_captures_dir() / session_id
            try:
                session_dir.mkdir(parents=True, exist_ok=False)
                for g in PHASE_TYPES:
                    (session_dir / g / "thumbs").mkdir(parents=True, exist_ok=True)
                self._set(session_name=session_id)
                self._write_meta(session_dir, session_id, plan, "running")
            except Exception as exc:  # noqa: BLE001
                self._log(f"Failed to create session directory: {exc}")
                self._finish("error", f"Failed to create session directory: {exc}")
                return
        except Exception as exc:  # noqa: BLE001
            self._log(f"Failed to create session directory: {exc}")
            self._finish("error", f"Failed to create session directory: {exc}")
            return

        with self._lock:
            self._session_id = session_id
            self._session_dir = session_dir

        # v1.7: snapshot the full camera parameter set once per session; every frame then only carries
        # a CAMPARS pointer (a full read inside the frame loop would cost ~164 property reads per frame).
        try:
            ensure_session_props(session_dir, self.camera, session_id, now_iso())
            self._log(f"Camera parameters snapshot written to {PROPS_FILENAME}")
        except Exception as exc:  # noqa: BLE001
            self._log(f"Failed to snapshot camera parameters: {exc}")

        try:
            for idx, phase in enumerate(plan["phases"], start=1):
                if self._abort_event.is_set():
                    self._finish("aborted", "Capture aborted")
                    return
                ptype = phase["type"]
                self._enter_phase(idx, len(plan["phases"]), phase)
                if ptype == "light":
                    # LIGHT requires manual confirmation: needs_user=true, wait for resume
                    if not self._wait_resume():
                        self._finish("aborted", "Capture aborted")
                        return
                else:
                    # Non-light phases: waiting_user is shown for 0.2s (prompt moves into message)
                    # and then capturing starts automatically, with no resume needed
                    deadline = time.monotonic() + 0.2
                    while time.monotonic() < deadline:
                        if self._abort_event.is_set():
                            self._finish("aborted", "Capture aborted")
                            return
                        time.sleep(0.02)
                    self._set(status="capturing", message=PROMPTS[ptype],
                              prompt=None, needs_user=False)
                    self._push()

                if not self._capture_phase(phase, ptype):
                    return
                self._log(f"{ptype.upper()} done {phase['count']}/{phase['count']}")
                self._write_meta(session_dir, session_id, plan,
                                 self.get_state()["status"])
            self._finish("finished", "Capture finished")
        except Exception as exc:  # noqa: BLE001
            self._log(f"Capture error: {exc}")
            self._finish("error", f"Capture error: {exc}")

    def _enter_phase(self, index: int, total: int, phase: dict) -> None:
        ptype = phase["type"]
        # Exposure override: set before capturing when not null (not restored afterwards, per contract)
        if phase.get("exposure_us") is not None:
            try:
                self.camera.set_property("exposure", float(phase["exposure_us"]))
            except Exception as exc:  # noqa: BLE001
                self._log(f"Failed to set exposure (continuing with current value): {exc}")
        # Mock camera scene sync: bias/dark = dark field, flat = uniform illumination, light = diffraction pattern.
        # A real camera without that property is skipped silently (the operator prepares the physical conditions).
        scene = {"bias": "dark", "dark": "dark", "flat": "uniform",
                 "light": "diffraction"}[ptype]
        try:
            self.camera.set_property("mock_scene", scene)
        except Exception:  # noqa: BLE001
            pass
        self._resume_event.clear()
        prompt = PROMPTS[ptype]
        self._set(
            status="waiting_user",
            current_phase={"type": ptype, "index": index, "total": total},
            prompt=prompt,
            needs_user=(ptype == "light"),
        )
        self._log(f"Preparing {ptype.upper()}: {prompt}")
        self._push()

    def _wait_resume(self) -> bool:
        while True:
            if self._abort_event.is_set():
                return False
            if self._resume_event.wait(timeout=0.1):
                return True

    def _optional_property(self, name: str, default=0.0):
        """Read an optional camera property; some real-hardware properties (e.g. device_temperature) may not
        exist or may return a non-finite value (inf/nan), which must not make a frame or the whole capture fail."""
        try:
            return self.camera.get_property(name)
        except Exception as exc:  # noqa: BLE001
            self._log(f"Skipping property {name}: {exc}")
            return default

    def _capture_phase(self, phase: dict, ptype: str) -> bool:
        session_dir = self._session_dir
        group_dir = session_dir / ptype
        thumbs_dir = group_dir / "thumbs"
        consecutive_failures = 0
        successful_frames = 0
        # Start the actual capture: status must be capturing (non-LIGHT is switched by _run,
        # LIGHT is switched by resume; this guard keeps the two paths consistent)
        self._set(status="capturing", needs_user=False)
        for n in range(1, int(phase["count"]) + 1):
            if self._abort_event.is_set():
                self._finish("aborted", "Capture aborted")
                return False
            try:
                frame = self.camera.grab_frame()
                if self._abort_event.is_set():
                    self._finish("aborted", "Capture aborted")
                    return False
                stats = frame_stats(frame)
                name = f"{ptype.upper()}_{n:04d}"
                exposure = self.camera.get_property("exposure")
                gain = self.camera.get_property("gain")
                black = self.camera.get_property("black_level")
                # Not every real camera provides device_temperature reliably; when missing or non-finite
                # a default goes into the FITS header instead of failing the whole frame.
                temp = self._optional_property("device_temperature", 0.0)
                cam = self.camera.device
                # v1.6: every frame stores the full camera parameter set in the FITS header (HIERARCH CAM ...);
                # a failed full read falls back to an empty dict and _save_fits uses the per-frame values.
                props = {}
                try:
                    props = {p["name"]: p["value"]
                             for p in self.camera.get_properties()}
                except Exception as exc:  # noqa: BLE001
                    self._log(f"Failed to read full camera properties: {exc}")
                try:
                    self._save_fits(group_dir / f"{name}.fits", frame, stats,
                                    ptype, exposure, gain, black, temp, cam, n,
                                    props=props,
                                    comment=phase.get("note") or "")
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(f"Failed to write FITS: {exc}") from exc
                consecutive_failures = 0
                successful_frames += 1
                with self._lock:
                    self._state["frames_done"] += 1
                self._log(f"{ptype.upper()} {n}/{phase['count']} done "
                          f"(mean={stats['mean']:.1f}, saturated_pct="
                          f"{stats['saturated_pct']:.2f})")
                self._push()
                # A thumbnail failure must not fail a FITS frame that was already written.
                try:
                    thumb = auto_contrast_8bit(frame)
                    Image.fromarray(thumb, mode="L").save(
                        thumbs_dir / f"{name}.png", optimize=False)
                except Exception as exc:  # noqa: BLE001
                    self._log(f"{ptype.upper()} thumbnail {n} failed: {exc}")
            except Exception as exc:  # noqa: BLE001
                consecutive_failures += 1
                self._log(f"{ptype.upper()} frame {n} failed: {exc}")
                self._push()
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    self._log("5 consecutive frames failed, auto-aborting")
                    self._finish("error", "5 consecutive frames failed, auto-aborting")
                    return False
                time.sleep(0.05)
        if successful_frames == 0 and consecutive_failures > 0:
            # Guard against a false completion where every frame was lost but the state says finished:
            # when a phase wrote no frame at all, abort with error instead of moving on to finished.
            self._log(f"{ptype.upper()} all frames failed, auto-aborting")
            self._finish("error", f"{ptype.upper()} all frames failed, auto-aborting")
            return False
        return True

    def _finish(self, status: str, message: str) -> None:
        self._set(status=status, prompt=None, message=message, needs_user=False)
        self._log(message)
        if self._session_dir is not None and self._plan is not None \
                and self._session_id is not None:
            try:
                self._write_meta(self._session_dir, self._session_id,
                                 self._plan, status)
            except Exception:  # noqa: BLE001
                pass
        self._push()

    # ------------------------------------------------------------ file output
    def _save_fits(self, path: Path, frame: np.ndarray, stats: dict,
                   frametype: str, exposure, gain, black, temp, cam, nframe,
                   props: dict | None = None, comment: str = "") -> None:
        """Write one FITS frame: contract section 3.2 keys + full HIERARCH CAM parameters + COMMENT.

        ``props`` is the full mapping from ``camera.get_properties()``; the four missing contract
        numeric keys (exposure/gain/black_level/device_temperature) fall back to the values read for
        this frame (keeping the old behaviour, for compatibility/logging). ``comment`` is the note of
        the current phase (the user comment, which may contain Chinese; on the FITS side it is
        converted to ASCII through ascii_fits_value).
        """
        props = dict(props or {})
        props.setdefault("exposure", float(exposure))
        props.setdefault("gain", float(gain))
        props.setdefault("black_level", float(black))
        props.setdefault("device_temperature", float(temp))
        meta = capture_metadata(
            props,
            str(getattr(cam, "name", "unknown")),
            str(getattr(cam, "serial", "unknown")),
            frametype.upper(),
            str(self._session_id or ""),
            int(nframe),
            comment or "",
        )
        entries = fits_header_entries(meta, stats, campers=self._campers_value())
        write_fits(path, np.asarray(frame, dtype=np.uint16), entries)

    def _campers_value(self) -> str:
        """``CAMPARS`` pointer of the current session (``none`` when the snapshot file is missing)."""
        session_dir = self._session_dir
        if session_dir is not None and (session_dir / PROPS_FILENAME).is_file():
            return PROPS_FILENAME
        return CAMPARS_NONE

    def _write_meta(self, session_dir: Path, session_id: str,
                    plan: dict, status: str) -> None:
        cam = self.camera.device
        meta = {
            "id": session_id,
            # Keep the name and the folder id identical; on a FileExistsError suffix collision also use
            # the session_id that actually took effect, not the raw plan name.
            "name": session_id,
            "created": getattr(self, "_created", None) or now_iso(),
            "status": status,
            "phases": plan["phases"],
            "camera": {
                "id": getattr(cam, "id", ""),
                "name": getattr(cam, "name", ""),
                "serial": getattr(cam, "serial", ""),
                "kind": getattr(cam, "kind", ""),
            },
        }
        (session_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def save_snapshot(frame: np.ndarray, fmt: str, cam: dict | None = None,
                  props: dict | None = None, folder: str | Path = "",
                  prefix: str = "", comment: str = "",
                  all_props: dict | None = None) -> dict:
    """v1.4: single-frame snapshot whose filename always carries a timestamp.

    v1.6: the FITS header holds the full camera parameters (HIERARCH CAM ...) plus the user
    comment (COMMENT, converted to printable ASCII through ascii_fits_value); TIFF now uses
    tifffile to write ImageDescription(270)/Software(305) plus private tag 65000 (the UTF-8
    bytes of the full meta JSON, including the Chinese comment).

    folder is an already-resolved absolute directory (the caller guarantees it is inside
    data_root); prefix was sanitised per the v1.4 rule. Filenames:
      with prefix -> <prefix>_<YYYYMMDDTHH-MM-SS>.<ext>
      without prefix -> <YYYYMMDDTHH-MM-SS>.<ext>
    """
    base = get_snapshots_dir().resolve()
    root = get_data_root().resolve()
    snap_dir = Path(folder).resolve() if folder else base
    snap_dir.mkdir(parents=True, exist_ok=True)
    ts = now_stamp()
    prefix = prefix or ""
    cam = cam or {}
    props = props or {}
    meta = capture_metadata(props, cam.get("name", "unknown"),
                            cam.get("serial", "unknown"), "SNAPSHOT",
                            "SNAPSHOT", 1, comment or "")
    # v1.7: snapshots share one parameter snapshot under the snapshots root; the FITS header only
    # carries the CAMPARS pointer, and the file is written on the spot when it is still missing.
    campers = ensure_snapshot_props(
        base,
        all_props if all_props is not None else props,
        {"id": "", "name": cam.get("name", "unknown"),
         "serial": cam.get("serial", "unknown"), "kind": ""})
    if fmt == "fits":
        ext = "fits"
        name = f"{prefix}_{ts}.{ext}" if prefix else f"{ts}.{ext}"
        entries = fits_header_entries(meta, frame_stats(frame), campers=campers)
        write_fits(snap_dir / name, np.asarray(frame, dtype=np.uint16), entries)
    elif fmt == "tiff":
        ext = "tiff"
        name = f"{prefix}_{ts}.{ext}" if prefix else f"{ts}.{ext}"
        description, extratags, software = tiff_metadata(meta)
        tifffile.imwrite(snap_dir / name, np.asarray(frame, dtype=np.uint16),
                         description=description, extratags=extratags,
                         software=software)
    elif fmt == "png":
        ext = "png"
        name = f"{prefix}_{ts}.{ext}" if prefix else f"{ts}.{ext}"
        Image.fromarray(auto_contrast_8bit(frame), mode="L").save(
            snap_dir / name)
    else:
        raise RuntimeError(f"Unsupported snapshot format: {fmt} (only fits|tiff|png)")

    # URL: the legacy form (inside the snapshots folder) keeps /api/files/snapshots/<rel>/<file>;
    # the v1.4 form (any folder under data_root) uses /api/files/snapshots/<rel-to-root>/<file>.
    try:
        rel = str(snap_dir.relative_to(base))
    except ValueError:
        rel = str(snap_dir.relative_to(root))
    if rel == ".":
        rel = ""
    url = f"/api/files/snapshots/{rel}/{name}" if rel \
        else f"/api/files/snapshots/{name}"
    return {"file": name, "url": url, "folder": str(snap_dir),
            "prefix": prefix}
