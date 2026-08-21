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
from .util import (CAPTURES_DIR, FITS_HEADER_MEAN, FITS_HEADER_STD,
                   FITS_HEADER_SATPCT, SNAPSHOT_DIR, auto_contrast_8bit,
                   frame_stats, now_iso, now_stamp, safe_name)

PHASE_TYPES = ("bias", "dark", "flat", "light")
ACTIVE_STATUSES = {"running", "waiting_user", "capturing"}

PROMPTS = {
    "bias": "请盖上镜头盖（传感器完全无光），确认后开始拍摄 Bias（最短曝光）",
    "dark": "请保持镜头盖盖上，Dark 曝光与 Light 一致，确认后开始拍摄 Dark",
    "flat": "请提供均匀照明（散焦束流或面光源），画面中不要出现衍射斑点，确认后开始拍摄 Flat",
    "light": "请恢复正常衍射条件。对照实时预览与直方图人工确认曝光/增益参数，确认后开始连续拍摄 Light",
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
                raise RuntimeError("拍摄进行中，不能重新创建计划")
            normalized = []
            for ph in phases:
                ptype = ph["type"]
                count = int(ph["count"])
                if ptype not in PHASE_TYPES:
                    raise RuntimeError(f"未知相位类型：{ptype}")
                if count < 1 or count > 10000:
                    raise RuntimeError(f"{ptype} 的 count 必须在 1–10000 之间")
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
            self._plan = {
                "plan_id": plan_id,
                "session_name": session_name.strip() or "session",
                "phases": normalized,
            }
            return dict(self._plan)

    # ------------------------------------------------------------ 控制
    def start(self) -> dict:
        with self._lock:
            if self._plan is None:
                raise RuntimeError("请先创建拍摄计划（POST /api/capture/plan）")
            if self._state["status"] in ACTIVE_STATUSES:
                raise RuntimeError("拍摄已在运行")
            if not self.camera.is_open():
                raise RuntimeError("设备未打开，请先打开相机")
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
                "message": "拍摄启动",
                "needs_user": False,
                "log": [{"t": now_iso(), "msg": "拍摄启动"}],
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
                raise RuntimeError("当前不在 waiting_user 状态，无需 resume")
            if not self._state.get("needs_user"):
                raise RuntimeError("当前相位无需人工确认，会自动继续")
            self._resume_event.set()
            self._state["status"] = "capturing"
            self._state["prompt"] = None
            self._state["message"] = "已人工确认参数，开始拍摄 LIGHT"
            self._state["needs_user"] = False
            self._push()
            return {"status": "capturing"}

    def abort(self) -> dict:
        with self._lock:
            if self._state["status"] not in ACTIVE_STATUSES:
                raise RuntimeError("当前无进行中的拍摄")
            self._abort_event.set()
            self._resume_event.set()   # 若正在等待人工确认，立即结束
            self._state["message"] = "收到中止请求"
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
        session_name = safe_name(plan["session_name"])
        ts = now_stamp()
        session_id = f"{ts}_{session_name}"
        session_dir = CAPTURES_DIR / session_id
        try:
            session_dir.mkdir(parents=True, exist_ok=False)
            for g in PHASE_TYPES:
                (session_dir / g / "thumbs").mkdir(parents=True, exist_ok=True)
            self._set(session_name=plan["session_name"], message="创建会话目录")
            self._write_meta(session_dir, session_id, plan, "running")
        except FileExistsError:
            session_id = f"{ts}_{session_name}_{uuid.uuid4().hex[:4]}"
            session_dir = CAPTURES_DIR / session_id
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
                    self._finish("aborted", "拍摄已中止")
                    return
                ptype = phase["type"]
                self._enter_phase(idx, len(plan["phases"]), phase)
                if ptype == "light":
                    # LIGHT 前强制人工确认：needs_user=true，等待 resume
                    if not self._wait_resume():
                        self._finish("aborted", "拍摄已中止")
                        return
                else:
                    # 非 light 相位：waiting_user 展示 0.2s（prompt 转入 message）
                    # 后自动进入 capturing，无需 resume
                    deadline = time.monotonic() + 0.2
                    while time.monotonic() < deadline:
                        if self._abort_event.is_set():
                            self._finish("aborted", "拍摄已中止")
                            return
                        time.sleep(0.02)
                    self._set(status="capturing", message=PROMPTS[ptype],
                              prompt=None, needs_user=False)
                    self._push()

                if not self._capture_phase(phase, ptype):
                    return
                self._log(f"{ptype.upper()} 完成 {phase['count']}/{phase['count']}")
                self._write_meta(session_dir, session_id, plan,
                                 self.get_state()["status"])
            self._finish("finished", "拍摄完成")
        except Exception as exc:  # noqa: BLE001
            self._log(f"拍摄异常：{exc}")
            self._finish("error", f"拍摄异常：{exc}")

    def _enter_phase(self, index: int, total: int, phase: dict) -> None:
        ptype = phase["type"]
        # 曝光覆盖：非 null 时拍摄前设置（相位结束不恢复，按契约）
        if phase.get("exposure_us") is not None:
            try:
                self.camera.set_property("exposure", float(phase["exposure_us"]))
            except Exception as exc:  # noqa: BLE001
                self._log(f"设置曝光失败（继续使用当前值）：{exc}")
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
        self._log(f"准备拍摄 {ptype.upper()}：{prompt}")
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
        # 进入实际拍摄：status 必须是 capturing（非 LIGHT 由 _run 切换，
        # LIGHT 由 resume 切换；此处兜底保证口径一致）
        self._set(status="capturing", needs_user=False)
        for n in range(1, int(phase["count"]) + 1):
            if self._abort_event.is_set():
                self._finish("aborted", "拍摄已中止")
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
                self._save_fits(group_dir / f"{name}.fits", frame, stats,
                                ptype, exposure, gain, black, temp, cam, n)
                thumb = auto_contrast_8bit(frame)
                Image.fromarray(thumb, mode="L").save(
                    thumbs_dir / f"{name}.png", optimize=False)
                consecutive_failures = 0
                with self._lock:
                    self._state["frames_done"] += 1
                self._log(f"{ptype.upper()} {n}/{phase['count']} 完成 "
                          f"(mean={stats['mean']:.1f}, saturated_pct="
                          f"{stats['saturated_pct']:.2f})")
                self._push()
            except Exception as exc:  # noqa: BLE001
                consecutive_failures += 1
                self._log(f"{ptype.upper()} 第 {n} 帧失败：{exc}")
                self._push()
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    self._log("连续 5 帧抓取失败，自动中止")
                    self._finish("error", "连续 5 帧抓取失败，自动中止")
                    return False
                time.sleep(0.05)
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
        header["SESSION"] = str(self._session_id or "")
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
                  props: dict | None = None) -> dict:
    """单帧快照 → data/snapshots/。FITS 头补齐契约关键字。"""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    ts = now_stamp()
    cam = cam or {}
    props = props or {}
    if fmt == "fits":
        name = f"SNP_{ts}.fits"
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
        hdu.writeto(SNAPSHOT_DIR / name, overwrite=True)
    elif fmt == "tiff":
        name = f"SNP_{ts}.tiff"
        Image.fromarray(np.asarray(frame, dtype=np.uint16), mode="I;16").save(
            SNAPSHOT_DIR / name)
    elif fmt == "png":
        name = f"SNP_{ts}.png"
        Image.fromarray(auto_contrast_8bit(frame), mode="L").save(
            SNAPSHOT_DIR / name)
    else:
        raise RuntimeError(f"不支持的快照格式：{fmt}（仅支持 fits|tiff|png）")
    return {"file": name, "url": f"/api/files/snapshots/{name}"}
