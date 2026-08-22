"""CaptureService：四相位序列拍摄状态机。

状态机（按契约）：
    idle → running → waiting_user（每个相位开始前）→ capturing
         → … → finished | aborted | error
LIGHT 相位前的 waiting_user 为**强制人工确认点**，resume 后才继续拍摄。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import numpy as np
from astropy.io import fits
from PIL import Image

from .camera import CameraService
from .util import (FITS_HEADER_MEAN, FITS_HEADER_STD,
                   FITS_HEADER_SATPCT, ascii_fits_value, auto_contrast_8bit,
                   frame_stats, get_captures_dir, get_data_root,
                   get_snapshots_dir, now_iso, now_stamp,
                   sanitize_session_name)

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

    # ------------------------------------------------------------ 状态
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
            # v1.2：session_name 为用户原始输入；effective = 本地时间戳_清洗名。
            effective = f"{now_stamp()}_{sanitize_session_name(session_name)}"
            self._plan = {
                "plan_id": plan_id,
                "session_name": effective,
                "phases": normalized,
            }
            return dict(self._plan)

    # ------------------------------------------------------------ 控制
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
            self._resume_event.set()   # 若正在等待人工确认，立即结束
            self._state["message"] = "Abort requested"
            return {"status": "aborted"}

    def shutdown(self) -> None:
        """服务停机：优雅中止进行中的拍摄并等待采集线程退出。"""
        with self._lock:
            if self._state["status"] in ACTIVE_STATUSES:
                self._abort_event.set()
                self._resume_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)

    # ------------------------------------------------------------ 执行
    def _run(self, plan: dict) -> None:
        self._created = now_iso()
        # v1.2：plan 阶段已生成 effective session_name（时间戳_清洗名），
        # 会话目录 id 与其保持一致。
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
            session_dir.mkdir(parents=True, exist_ok=False)
            for g in PHASE_TYPES:
                (session_dir / g / "thumbs").mkdir(parents=True, exist_ok=True)
            self._set(session_name=plan["session_name"])
            self._write_meta(session_dir, session_id, plan, "running")

        with self._lock:
            self._session_id = session_id
            self._session_dir = session_dir

        try:
            for idx, phase in enumerate(plan["phases"], start=1):
                if self._abort_event.is_set():
                    self._finish("aborted", "Capture aborted")
                    return
                ptype = phase["type"]
                self._enter_phase(idx, len(plan["phases"]), phase)
                if ptype == "light":
                    # LIGHT 前强制人工确认：needs_user=true，等待 resume
                    if not self._wait_resume():
                        self._finish("aborted", "Capture aborted")
                        return
                else:
                    # 非 light 相位：waiting_user 展示 0.2s（prompt 转入 message）
                    # 后自动进入 capturing，无需 resume
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
        # 曝光覆盖：非 null 时拍摄前设置（相位结束不恢复，按契约）
        if phase.get("exposure_us") is not None:
            try:
                self.camera.set_property("exposure", float(phase["exposure_us"]))
            except Exception as exc:  # noqa: BLE001
                self._log(f"Failed to set exposure (continuing with current value): {exc}")
        # Mock 相机场景同步：bias/dark=暗场、flat=均匀照明、light=衍射图案。
        # 真机无该属性时静默跳过（物理条件由操作者准备）。
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

    def _capture_phase(self, phase: dict, ptype: str) -> bool:
        session_dir = self._session_dir
        group_dir = session_dir / ptype
        thumbs_dir = group_dir / "thumbs"
        consecutive_failures = 0
        successful_frames = 0
        # 进入实际拍摄：status 必须是 capturing（非 LIGHT 由 _run 切换，
        # LIGHT 由 resume 切换；此处兜底保证口径一致）
        self._set(status="capturing", needs_user=False)
        for n in range(1, int(phase["count"]) + 1):
            if self._abort_event.is_set():
                self._finish("aborted", "Capture aborted")
                return False
            try:
                frame = self.camera.grab_frame()
                stats = frame_stats(frame)
                name = f"{ptype.upper()}_{n:04d}"
                exposure = self.camera.get_property("exposure")
                gain = self.camera.get_property("gain")
                black = self.camera.get_property("black_level")
                temp = self.camera.get_property("device_temperature")
                cam = self.camera.device
                try:
                    self._save_fits(group_dir / f"{name}.fits", frame, stats,
                                    ptype, exposure, gain, black, temp, cam, n)
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(f"Failed to write FITS: {exc}") from exc
                thumb = auto_contrast_8bit(frame)
                Image.fromarray(thumb, mode="L").save(
                    thumbs_dir / f"{name}.png", optimize=False)
                consecutive_failures = 0
                successful_frames += 1
                with self._lock:
                    self._state["frames_done"] += 1
                self._log(f"{ptype.upper()} {n}/{phase['count']} done "
                          f"(mean={stats['mean']:.1f}, saturated_pct="
                          f"{stats['saturated_pct']:.2f})")
                self._push()
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
            # 防止“帧全丢但状态 finished”的假完成：
            # 该相位一帧都没落盘时按 error 中止，而非继续走 finished。
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

    # ------------------------------------------------------------ 落盘
    def _save_fits(self, path: Path, frame: np.ndarray, stats: dict,
                   frametype: str, exposure, gain, black, temp, cam, nframe) -> None:
        hdu = fits.PrimaryHDU(np.asarray(frame, dtype=np.uint16))
        header = hdu.header
        header["DATE-OBS"] = datetime.now().isoformat(timespec="milliseconds")
        header["EXPOSURE"] = float(exposure)
        header["GAIN"] = float(gain)
        header["BLACKLEV"] = float(black)
        header["TEMP"] = float(temp)
        header["FRAMETYP"] = frametype.upper()
        header["CAMERA"] = str(getattr(cam, "name", "unknown"))
        header["SERIAL"] = str(getattr(cam, "serial", "unknown"))
        # FITS 头只接受 printable ASCII：transliterate/替换非 ASCII，
        # 目录名、meta.json、URL、sessions 列表仍保留原始 effective 中文名。
        header["SESSION"] = ascii_fits_value(str(self._session_id or ""))
        header["NFRAME"] = int(nframe)
        header[FITS_HEADER_MEAN] = float(stats["mean"])
        header[FITS_HEADER_STD] = float(stats["std"])
        header[FITS_HEADER_SATPCT] = float(stats["saturated_pct"])
        header["BITPIX"] = 16  # astropy 默认，显式声明：数据有效位 12
        hdu.writeto(path, overwrite=True)

    def _write_meta(self, session_dir: Path, session_id: str,
                    plan: dict, status: str) -> None:
        cam = self.camera.device
        meta = {
            "id": session_id,
            "name": plan["session_name"],
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
                  prefix: str = "") -> dict:
    """v1.4：单帧快照，文件名恒带时间戳。

    folder 为已解析的绝对目录（调用方保证位于 data_root 内）；
    prefix 已按 v1.4 口径 sanitize。文件名：
      有 prefix → <prefix>_<YYYYMMDDTHH-MM-SS>.<ext>
      无 prefix → <YYYYMMDDTHH-MM-SS>.<ext>
    """
    base = get_snapshots_dir().resolve()
    root = get_data_root().resolve()
    snap_dir = Path(folder).resolve() if folder else base
    snap_dir.mkdir(parents=True, exist_ok=True)
    ts = now_stamp()
    prefix = prefix or ""
    cam = cam or {}
    props = props or {}
    if fmt == "fits":
        ext = "fits"
        name = f"{prefix}_{ts}.{ext}" if prefix else f"{ts}.{ext}"
        hdu = fits.PrimaryHDU(np.asarray(frame, dtype=np.uint16))
        hdu.header["DATE-OBS"] = datetime.now().isoformat(timespec="milliseconds")
        hdu.header["EXPOSURE"] = float(props.get("exposure", 0.0))
        hdu.header["GAIN"] = float(props.get("gain", 0.0))
        hdu.header["BLACKLEV"] = float(props.get("black_level", 0.0))
        hdu.header["TEMP"] = float(props.get("device_temperature", 0.0))
        hdu.header["FRAMETYP"] = "SNAPSHOT"
        hdu.header["CAMERA"] = str(cam.get("name", "unknown"))
        hdu.header["SERIAL"] = str(cam.get("serial", "unknown"))
        hdu.header["SESSION"] = "SNAPSHOT"
        hdu.header["NFRAME"] = 1
        hdu.writeto(snap_dir / name, overwrite=True)
    elif fmt == "tiff":
        ext = "tiff"
        name = f"{prefix}_{ts}.{ext}" if prefix else f"{ts}.{ext}"
        Image.fromarray(np.asarray(frame, dtype=np.uint16), mode="I;16").save(
            snap_dir / name)
    elif fmt == "png":
        ext = "png"
        name = f"{prefix}_{ts}.{ext}" if prefix else f"{ts}.{ext}"
        Image.fromarray(auto_contrast_8bit(frame), mode="L").save(
            snap_dir / name)
    else:
        raise RuntimeError(f"Unsupported snapshot format: {fmt} (only fits|tiff|png)")

    # URL：旧口径（snapshots 目录内）保持 /api/files/snapshots/<rel>/<file>；
    # v1.4 新口径（data_root 内任意目录）用 /api/files/snapshots/<rel-to-root>/<file>。
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
