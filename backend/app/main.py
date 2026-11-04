"""FastAPI entry point: REST + /ws/live + /ws/events + static frontend hosting."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from typing import Any

from fastapi import (FastAPI, HTTPException, Query, Request, WebSocket,
                     WebSocketDisconnect)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from PIL import Image

from . import APP_VERSION
from . import capture_store
from .camera import CameraService, CameraError, ic4_available
from .capture import ACTIVE_STATUSES, CaptureService, save_snapshot
from .process import ProcessService
from .sessions import get_session, list_sessions
from .util import (CONTENT_TYPES, REPO_ROOT, auto_contrast_8bit, ensure_dirs,
                   frame_stats, get_captures_dir, get_data_root,
                   get_snapshots_dir, histogram_256,
                   is_data_root_writable, ok, sanitize_prefix, set_data_root)

DIST_DIR = REPO_ROOT / "frontend" / "dist"
INDEX_HTML = DIST_DIR / "index.html"

# index.html must always be fetched fresh: the frontend assets are rebuilt on
# every release, and a browser that keeps the cached page would keep loading
# the old UI after the program is restarted. Hashed assets may keep normal
# caching.
NO_CACHE_HEADERS = {"Cache-Control": "no-store"}


class NoCacheIndexStaticFiles(StaticFiles):
    """Static mount variant that adds no-store to index.html responses."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if path in ("", "index.html"):
            response.headers["Cache-Control"] = "no-store"
        return response


# =========================================================================
# Native folder picker dialogs (on the server machine)
# =========================================================================
PICKER_DEFAULT_TITLE = "Select Folder"


def _run_macos_picker(title: str) -> str | None:
    """macOS: osascript choose folder -> POSIX path. Returns None when cancelled."""
    safe_title = (title.replace("\\", "\\\\").replace('"', '\\"')
                  .replace("\r", " ").replace("\n", " "))
    script = f'POSIX path of (choose folder with prompt "{safe_title}")'
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Unable to launch macOS folder picker: {exc}")
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    logging.getLogger("uvicorn.error").warning(
        "osascript picker rc=%s stdout=%r stderr=%r",
        proc.returncode, stdout, stderr)
    if proc.returncode == 0:
        return stdout or None
    # Some AppleScript environments exit non-zero even after a successful pick, but the path is already on stdout.
    if stdout and os.path.isabs(stdout):
        return stdout
    stderr_lower = stderr.lower()
    if proc.returncode == -128 or "-128" in stderr_lower \
            or "cancel" in stderr_lower or "cancelled" in stderr_lower \
            or "已取消" in stderr:
        return None
    raise RuntimeError(f"macOS folder picker failed: {stderr}")


def _run_windows_picker(title: str) -> str | None:
    """Windows: PowerShell FolderBrowserDialog. Returns None when cancelled."""
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$f.Description = $env:PICKER_TITLE; "
        "$f.ShowNewFolderButton = $true; "
        "if ($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
        "{ Write-Output $f.SelectedPath }"
    )
    env = dict(os.environ)
    env["PICKER_TITLE"] = title
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps],
            capture_output=True, text=True, timeout=120, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Unable to launch Windows folder picker: {exc}")
    if proc.returncode != 0:
        raise RuntimeError(
            f"Windows folder picker failed: {(proc.stderr or '').strip()}")
    path = proc.stdout.strip()
    return path or None


def _run_linux_picker(title: str) -> str | None:
    """Linux: zenity first, then kdialog. Returns None when cancelled or closed."""
    candidates = (
        ["zenity", "--file-selection", "--directory", f"--title={title}"],
        ["kdialog", "--getexistingdirectory", title],
    )
    for cmd in candidates:
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120)
        except FileNotFoundError:
            continue
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            return proc.stdout.strip() or None
        # A non-zero exit from zenity/kdialog usually means the user cancelled or closed the dialog.
        return None
    raise RuntimeError(
        "Linux folder picker is not available (install zenity or kdialog)")


def _pick_folder(title: str) -> str | None:
    """Open the native directory picker for the current platform; return an absolute path or None (user cancelled)."""
    if sys.platform == "darwin":
        path = _run_macos_picker(title)
    elif sys.platform.startswith("win"):
        path = _run_windows_picker(title)
    elif sys.platform.startswith("linux"):
        path = _run_linux_picker(title)
    else:
        raise RuntimeError(
            "Native folder picker is not supported on this platform")
    if path:
        path = os.path.abspath(path)
    return path


# =========================================================================
# WebSocket manager (cross-thread push)
# =========================================================================
class EventsManager:
    def __init__(self):
        self.connections: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.connections.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.connections.discard(ws)

    def broadcast(self, topic: str, data: dict) -> None:
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(
            self._broadcast(topic, data), loop)

    async def _broadcast(self, topic: str, data: dict) -> None:
        text = json.dumps({"topic": topic, "data": data}, ensure_ascii=False)
        for ws in list(self.connections):
            try:
                await ws.send_text(text)
            except Exception:  # noqa: BLE001
                self.connections.discard(ws)


class LiveManager:
    def __init__(self):
        self.connections: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.connections.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.connections.discard(ws)

    def broadcast_frame(self, stats: dict, jpeg: bytes) -> None:
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(
            self._broadcast_frame(stats, jpeg), loop)

    async def _broadcast_frame(self, stats: dict, jpeg: bytes) -> None:
        text = json.dumps(stats, ensure_ascii=False)
        for ws in list(self.connections):
            try:
                await ws.send_text(text)
                await ws.send_bytes(jpeg)
            except Exception:  # noqa: BLE001
                self.connections.discard(ws)


# =========================================================================
# Live streaming service: grab thread + latest-frame queue (maxsize=1) + push thread
# =========================================================================
class LiveService:
    MAX_CONSECUTIVE_FAILURES = 5

    def __init__(self, camera_service: CameraService, manager: LiveManager,
                 events_broadcast=None):
        self.camera = camera_service
        self.manager = manager
        self.events_broadcast = events_broadcast
        self.streaming = False
        self.params: dict = {"fps": 15, "scale": 1.0, "quality": 80}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._queue: queue.Queue = queue.Queue(maxsize=1)
        self._grabber: threading.Thread | None = None
        self._pusher: threading.Thread | None = None
        self._error: str | None = None

    def get_state(self) -> dict:
        with self._lock:
            return {"streaming": self.streaming,
                    "error": self._error,
                    "params": dict(self.params)}

    def start(self, fps: int, scale: float, quality: int) -> dict:
        with self._lock:
            if self.streaming:
                raise RuntimeError("Live stream is already running")
            # Do not start a new stream until any previous threads have fully
            # exited; otherwise stop()/start() races can leave duplicate
            # grabber/pusher pairs running against the same queue.
            for t in (self._grabber, self._pusher):
                if t is not None and t.is_alive():
                    t.join(timeout=5.0)
            if any(t is not None and t.is_alive()
                   for t in (self._grabber, self._pusher)):
                raise RuntimeError("Previous live stream is still stopping")
            self._grabber = self._pusher = None
            self.camera.ensure_device()
            if not isinstance(fps, int) or not (1 <= fps <= 30):
                raise RuntimeError("fps must be between 1 and 30")
            if scale not in (1.0, 0.5, 0.25):
                raise RuntimeError("scale only supports 1.0 / 0.5 / 0.25")
            if not isinstance(quality, int):
                raise RuntimeError("quality must be an integer")
            quality = max(10, min(quality, 100))
            self.params = {"fps": fps, "scale": scale, "quality": quality}
            self._error = None
            self.streaming = True
            self._stop.clear()
            self._grabber = threading.Thread(target=self._grab_loop, daemon=True,
                                             name="live-grabber")
            self._pusher = threading.Thread(target=self._push_loop, daemon=True,
                                            name="live-pusher")
            self._grabber.start()
            self._pusher.start()
            return {"streaming": True, "fps": fps, "scale": scale,
                    "quality": quality}

    def stop(self) -> dict:
        with self._lock:
            self.streaming = False
            self._stop.set()
            for t in (self._grabber, self._pusher):
                if t is not None and t.is_alive():
                    t.join(timeout=2.0)
            # Keep references if threads are still winding down so start() can
            # join them before reusing the service.
            if (self._grabber is None or not self._grabber.is_alive()) and \
                    (self._pusher is None or not self._pusher.is_alive()):
                self._grabber = self._pusher = None
            # Stop the actual acquisition (IC4 stream_stop), otherwise the hardware keeps the ROI properties locked.
            try:
                self.camera.stop_acquisition()
            except Exception:  # noqa: BLE001
                logging.getLogger("uvicorn.error").warning(
                    "live stop_acquisition failed", exc_info=True)
            return {"streaming": False}

    def _fail(self, message: str) -> None:
        with self._lock:
            self._error = message
            self.streaming = False
        self._stop.set()
        if self.events_broadcast is not None:
            self.events_broadcast("live", {
                "status": "error",
                "error": message,
            })

    def _triggered(self) -> bool:
        try:
            return bool(self.camera.get_property("trigger_mode"))
        except Exception:  # noqa: BLE001
            return False

    def _grab_loop(self) -> None:
        interval = 1.0 / max(self.params["fps"], 1)
        consecutive_failures = 0
        while not self._stop.is_set():
            started = time.monotonic()
            if self._triggered():
                time.sleep(0.02)   # No automatic frames while trigger_mode=true
                continue
            try:
                frame = self.camera.grab_frame()
                consecutive_failures = 0
                scale = self.params["scale"]
                if scale == 0.5:
                    frame = frame[::2, ::2]
                elif scale == 0.25:
                    frame = frame[::4, ::4]
                try:
                    self._queue.get_nowait()      # Drop old frames and keep only the latest
                except queue.Empty:
                    pass
                self._queue.put_nowait(frame)
            except Exception as exc:  # noqa: BLE001
                consecutive_failures += 1
                if consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                    self._fail(f"Live stream failed: {exc}")
                    return
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, interval - elapsed))
        # Tell the push thread to exit
        self._stop.set()

    def _push_loop(self) -> None:
        started = time.monotonic()
        count = 0
        while not self._stop.is_set():
            try:
                frame = self._queue.get(timeout=0.3)
            except queue.Empty:
                continue
            stats = frame_stats(frame)
            preview = auto_contrast_8bit(frame)
            stats["fps"] = self._ema_fps(count, started)
            # Histogram is computed from the raw 12-bit frame (grab_frame output),
            # with 256 bins adapted to the frame's actual ADU min/max so it
            # matches the contrast range the user sees. It does NOT use the
            # auto-contrast/JPEG preview.
            stats["histogram"] = histogram_256(frame)
            stats["histogram_min"] = int(frame.min())
            stats["histogram_max"] = int(frame.max())
            buf = BytesIO()
            Image.fromarray(preview, mode="L").save(
                buf, format="JPEG", quality=self.params["quality"])
            self.manager.broadcast_frame(stats, buf.getvalue())
            count += 1

    @staticmethod
    def _ema_fps(count: int, started: float) -> float:
        elapsed = max(time.monotonic() - started, 0.001)
        return round(count / elapsed, 1)


# =========================================================================
# Service instances
# =========================================================================
camera_service = CameraService()
events_manager = EventsManager()
live_manager = LiveManager()
capture_service = CaptureService(camera_service, broadcast=events_manager.broadcast)
process_service = ProcessService(broadcast=events_manager.broadcast)
live_service = LiveService(camera_service, live_manager,
                            events_broadcast=events_manager.broadcast)


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    loop = asyncio.get_running_loop()
    events_manager.set_loop(loop)
    live_manager.set_loop(loop)
    yield
    live_service.stop()
    capture_service.shutdown()   # Abort an in-flight capture gracefully
    camera_service.close()


app = FastAPI(title="Static AstroPhoto", version=APP_VERSION,
              lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================================
# Uniform error envelope
# =========================================================================
@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=400, content={
        "ok": False, "data": None,
        "error": "Request validation failed: " + str(exc.errors()[:3])})


@app.exception_handler(Exception)
async def generic_handler(request: Request, exc: Exception):
    logging.getLogger("uvicorn.error").exception(
        "unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={
        "ok": False, "data": None, "error": "Internal server error"})


def raise_cam_err(exc: Exception) -> None:
    msg = str(exc)
    # IC4 path: camera.py already maps official ErrorCode values to 400/404/409/503/500,
    # exposed through CameraError.status and preferred here; the mock path keeps message matching.
    status = getattr(exc, "status", None)
    if status == 500:
        # Contract: 500 always reports an internal server error and never leaks exception details to clients
        logging.getLogger("uvicorn.error").error(
            "ic4 camera error: %s", exc, exc_info=True)
        raise HTTPException(500, "Internal server error")
    if status in (400, 404, 409, 503):
        raise HTTPException(status, msg)
    if "Device is not open" in msg or "Device is closed" in msg:
        raise HTTPException(409, msg)
    if "Unable to load imagingcontrol4" in msg:
        raise HTTPException(503, msg)
    if "Device does not exist" in msg:
        raise HTTPException(404, msg)
    if any(k in msg for k in ("only supports", "must be", "Unknown property",
                              "is read-only", "requires a numeric value",
                              "must be an integer")):
        raise HTTPException(400, msg)
    if "Live stream is already running" in msg \
            or "Previous live stream is still stopping" in msg:
        raise HTTPException(409, msg)
    # Unclassified camera errors: log only, never leak exception details to clients
    logging.getLogger("uvicorn.error").error(
        "unclassified camera error: %s", exc, exc_info=True)
    raise HTTPException(500, "Internal server error")


# =========================================================================
# Pydantic models
# =========================================================================
class OpenBody(BaseModel):
    id: str


class PropertyBody(BaseModel):
    value: Any


class LiveStartBody(BaseModel):
    fps: int = 15
    scale: float = 1.0
    quality: int = 80


class SnapshotBody(BaseModel):
    format: str = "fits"
    folder: str | None = None
    prefix: str | None = None
    comment: str | None = None


class SettingsBody(BaseModel):
    data_root: str


class PickFolderBody(BaseModel):
    title: str = PICKER_DEFAULT_TITLE


class PhaseIn(BaseModel):
    type: str
    count: int = Field(ge=1, le=10000)
    exposure_us: float | None = None
    note: str | None = ""


class PlanBody(BaseModel):
    session_name: str
    phases: list[PhaseIn] = Field(min_length=1)


class DraftPhaseIn(BaseModel):
    type: str
    count: int
    exposure_us: float | None = None
    # The draft also keeps note (written into each frame's FITS COMMENT); named sets do not store note
    note: str | None = None


class DraftBody(BaseModel):
    phases: list[DraftPhaseIn] = Field(default_factory=list)


class CalibrateBody(BaseModel):
    session_id: str
    sigma: float = 3
    method: str = "mean"
    exclude: dict[str, list[str]] | None = None


class StackBody(CalibrateBody):
    registration: bool = False


class StretchBody(BaseModel):
    session_id: str
    result_file: str
    stretch: str = "asinh"
    black: float = 0
    white: float = 4095


# =========================================================================
# Health and device
# =========================================================================
@app.get("/api/health")
def health():
    return ok({
        "status": "ok",
        "version": APP_VERSION,
        "platform": sys.platform,
        "camera_driver": "ic4" if ic4_available() else "mock",
    })


@app.get("/api/settings")
def settings_get():
    root = get_data_root()
    return ok({"data_root": str(root), "writable": is_data_root_writable(root)})


@app.put("/api/settings")
def settings_put(body: SettingsBody):
    raw = body.data_root.strip()
    if not raw:
        raise HTTPException(400, "data_root must not be empty")
    root = Path(raw)
    if not root.is_absolute():
        raise HTTPException(400, "data_root must be an absolute path")
    root = root.resolve()
    if capture_service.get_state()["status"] in ACTIVE_STATUSES:
        raise HTTPException(409, "Capture in progress, cannot change data directory")
    try:
        root.mkdir(parents=True, exist_ok=True)
        (root / "snapshots").mkdir(parents=True, exist_ok=True)
        (root / "captures").mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(400, f"Failed to create data directory: {exc}")
    try:
        set_data_root(root)
    except OSError as exc:
        raise HTTPException(400, f"Failed to save data directory setting: {exc}")
    return ok({"data_root": str(get_data_root()),
               "writable": is_data_root_writable(root)})


@app.post("/api/pick-folder")
def pick_folder(body: PickFolderBody):
    """Open the native directory picker on the server host (sync def -> executed by the FastAPI thread pool)."""
    title = (body.title or "").strip() or PICKER_DEFAULT_TITLE
    try:
        path = _pick_folder(title)
    except RuntimeError as exc:
        raise HTTPException(501, str(exc))
    return ok({"path": path, "cancelled": path is None})


@app.get("/api/devices")
def devices():
    return ok(camera_service.list_devices())


@app.post("/api/device/open")
def device_open(body: OpenBody):
    cam_id = body.id.strip()
    known = [d["id"] for d in camera_service.list_devices()]
    if cam_id not in known:
        raise HTTPException(404, f"Device does not exist: {cam_id}")
    if capture_service.get_state()["status"] in ACTIVE_STATUSES:
        raise HTTPException(409, "Capture in progress, cannot open device")
    try:
        cam = camera_service.open(cam_id)
    except Exception as exc:  # noqa: BLE001
        raise_cam_err(exc)
    return ok({"id": cam.id, "name": cam.name, "serial": cam.serial,
               "connected": True})


@app.post("/api/device/close")
def device_close():
    if capture_service.get_state()["status"] in ACTIVE_STATUSES:
        raise HTTPException(409, "Capture in progress, cannot close device")
    camera_service.close()
    return ok({"connected": False})


@app.get("/api/device/info")
def device_info():
    try:
        return ok(camera_service.device_info())
    except Exception as exc:  # noqa: BLE001
        raise_cam_err(exc)


# =========================================================================
# Properties
# =========================================================================
@app.get("/api/properties")
def properties():
    try:
        return ok({"properties": camera_service.get_properties()})
    except Exception as exc:  # noqa: BLE001
        raise_cam_err(exc)


@app.get("/api/properties/all")
def properties_all():
    """All camera parameters (extended shape), used by the Parameters tab."""
    try:
        return ok({"properties": camera_service.get_all_properties()})
    except Exception as exc:  # noqa: BLE001
        raise_cam_err(exc)


@app.put("/api/properties/{name}")
def set_property(name: str, body: PropertyBody):
    try:
        actual = camera_service.set_property(name, body.value)
    except Exception as exc:  # noqa: BLE001
        raise_cam_err(exc)
    return ok({"name": name, "value": body.value, "actual": actual})


# =========================================================================
# Live preview and snapshots
# =========================================================================
@app.post("/api/live/start")
def live_start(body: LiveStartBody):
    try:
        return ok(live_service.start(body.fps, body.scale, body.quality))
    except Exception as exc:  # noqa: BLE001
        raise_cam_err(exc)


@app.post("/api/live/stop")
def live_stop():
    return ok(live_service.stop())


@app.get("/api/live/state")
def live_state():
    return ok(live_service.get_state())


@app.websocket("/ws/live")
async def ws_live(websocket: WebSocket):
    await live_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        live_manager.disconnect(websocket)


# v1.4: folder browsing (snapshot folder selection).
@app.get("/api/browse/folders")
def browse_folders(path: str | None = Query(default=None)):
    """List the direct subdirectories (files excluded) of a directory inside data_root.

    path defaults to / is empty -> data_root; otherwise it must be an existing absolute directory
    that resolves to data_root or lies inside it. Anything else (relative path, missing, outside
    the root) returns 400 "Folder must be inside the data root".
    """
    root = get_data_root().resolve()
    raw = (path or "").strip()
    if not raw:
        current = root
    else:
        current = Path(raw).resolve() if Path(raw).is_absolute() else None
        if current is None or not current.is_dir() \
                or (current != root and root not in current.parents):
            raise HTTPException(400, "Folder must be inside the data root")
    folders: list[dict[str, str]] = []
    if current.is_dir():
        folders = [{"name": p.name, "path": str(p)}
                   for p in sorted(current.iterdir(), key=lambda p: p.name)
                   if p.is_dir()]
    parent = None if current == root else str(current.parent)
    return ok({"path": str(current), "parent": parent, "files": [],
               "folders": folders})


# v1.3: single-segment relative-name check for snapshot folders (the compatible relative path still rejects illegal characters).
_ILLEGAL_SNAPSHOT_FOLDER_CHARS = frozenset('<>:"|?*')


def _snapshot_folder_name(folder: str | None) -> str:
    """v1.3 compatibility: validate a single-segment relative subdirectory name; an empty value returns "" (the snapshots root directory).

    Windows does not allow a directory name to end with a dot or a space, so trailing dots/spaces are removed after validation.
    """
    raw = (folder or "").strip()
    if not raw:
        return ""
    if raw in (".", ".."):
        raise HTTPException(400, "folder must not contain . or .. segments")
    bad = sorted(ch for ch in set(raw) if ch in _ILLEGAL_SNAPSHOT_FOLDER_CHARS)
    if bad:
        raise HTTPException(400, f"folder contains illegal characters: {' '.join(bad)}")
    raw = raw.rstrip(" .")
    if raw in ("", ".", ".."):
        raise HTTPException(400, "folder must not contain . or .. segments")
    return raw


def _snapshot_folder(folder: str | None) -> Path:
    """v1.4: resolve the snapshot target directory (absolute path).

    - missing/empty -> data_root/snapshots (backwards compatible)
    - single-segment relative name (e.g. "test folder") -> data_root/snapshots/<name> (legacy callers)
    - absolute path: after resolve it must equal data_root or lie inside it, otherwise 400;
      when the directory does not exist, the caller does mkdir(parents=True).
    """
    root = get_data_root().resolve()
    raw = (folder or "").strip()
    if not raw:
        return get_snapshots_dir().resolve()
    if "/" in raw or "\\" in raw:
        if not Path(raw).is_absolute():
            raise HTTPException(
                400, "folder must be an absolute path inside the data root "
                     "or a single-segment relative name")
        resolved = Path(raw).resolve()
        if resolved != root and root not in resolved.parents:
            raise HTTPException(400, "Folder must be inside the data root")
        return resolved
    return (get_snapshots_dir() / _snapshot_folder_name(raw)).resolve()


@app.post("/api/snapshot")
def snapshot(body: SnapshotBody):
    fmt = body.format
    if fmt not in ("fits", "tiff", "png"):
        raise HTTPException(400, "format only supports fits|tiff|png")
    folder = _snapshot_folder(body.folder)
    prefix = sanitize_prefix(body.prefix)
    comment = body.comment or ""
    if len(comment) > 256:
        raise HTTPException(400, "comment must be at most 256 characters")
    try:
        frame = camera_service.grab_frame()
        cam = camera_service.device
        cam_info = {"name": getattr(cam, "name", "unknown"),
                    "serial": getattr(cam, "serial", "unknown")}
        # v1.6: full camera parameters go into the snapshot header (FITS HIERARCH CAM / TIFF tag 65000);
        # a failed full read falls back to an empty dict and the FITS header uses 0.0/unknown.
        props = {}
        try:
            props = {p["name"]: p["value"]
                     for p in camera_service.get_properties()}
        except Exception:  # noqa: BLE001
            props = {}
    except Exception as exc:  # noqa: BLE001
        raise_cam_err(exc)
    try:
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(400, f"Failed to create snapshot folder: {exc}")
        return ok(save_snapshot(frame, fmt, cam_info, props, folder, prefix,
                                comment=comment))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("uvicorn.error").error(
            "snapshot save failed: %s", exc, exc_info=True)
        raise HTTPException(500, "Internal server error")


@app.get("/api/snapshot/folders")
def snapshot_folders():
    """v1.3: list the direct subdirectories of snapshots; the root directory is expressed as name=''."""
    base = get_snapshots_dir()
    entries = [p for p in base.iterdir() if p.is_dir()] if base.is_dir() else []
    folders = [{"name": "", "files": sum(
        1 for p in base.iterdir() if p.is_file()) if base.is_dir() else 0}]
    for entry in sorted(entries, key=lambda p: p.name):
        folders.append({
            "name": entry.name,
            "files": sum(1 for p in entry.iterdir() if p.is_file()),
        })
    return ok({"folders": folders})


# =========================================================================
# Capture sequence
# =========================================================================
@app.post("/api/capture/plan")
def capture_plan(body: PlanBody):
    phases = [p.model_dump() for p in body.phases]
    for p in phases:
        if p["type"] not in ("bias", "dark", "flat", "light"):
            raise HTTPException(400, f"Unknown phase type: {p['type']}")
    try:
        plan = capture_service.plan(body.session_name, phases)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    return ok({"plan_id": plan["plan_id"], "session_name": plan["session_name"],
               "phases": plan["phases"]})


@app.post("/api/capture/start")
def capture_start():
    try:
        return ok(capture_service.start())
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))


@app.get("/api/capture/state")
def capture_state():
    return ok(capture_service.get_state())


@app.post("/api/capture/resume")
def capture_resume():
    try:
        return ok(capture_service.resume())
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/capture/abort")
def capture_abort():
    try:
        return ok(capture_service.abort())
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))


# =========================================================================
# Capture sets: phase-table draft + named sets
# =========================================================================
def _capture_store_error(exc: Exception) -> HTTPException:
    """Map store failures; a damaged file is a server-side 500, bad input 400."""
    if isinstance(exc, capture_store.CaptureStoreError):
        return HTTPException(500, str(exc))
    return HTTPException(400, str(exc))


@app.get("/api/capture/draft")
def capture_draft_get():
    try:
        return ok(capture_store.get_draft())
    except (ValueError, capture_store.CaptureStoreError) as exc:
        raise _capture_store_error(exc)


@app.put("/api/capture/draft")
def capture_draft_put(body: DraftBody):
    try:
        return ok(capture_store.save_draft([p.model_dump() for p in body.phases]))
    except (ValueError, capture_store.CaptureStoreError) as exc:
        raise _capture_store_error(exc)


@app.get("/api/capture/sets")
def capture_sets_list():
    try:
        return ok({"names": capture_store.list_sets()})
    except (ValueError, capture_store.CaptureStoreError) as exc:
        raise _capture_store_error(exc)


@app.get("/api/capture/set")
def capture_set_get(name: str = Query(...)):
    try:
        entry = capture_store.get_set(name)
    except (ValueError, capture_store.CaptureStoreError) as exc:
        raise _capture_store_error(exc)
    if entry is None:
        raise HTTPException(404, "Capture set does not exist")
    return ok({"name": capture_store.normalize_name(name), **entry})


@app.put("/api/capture/set")
def capture_set_put(body: DraftBody, name: str = Query(...)):
    try:
        entry = capture_store.put_set(name, [p.model_dump() for p in body.phases])
    except (ValueError, capture_store.CaptureStoreError) as exc:
        raise _capture_store_error(exc)
    return ok({"name": capture_store.normalize_name(name), **entry})


@app.delete("/api/capture/set")
def capture_set_delete(name: str = Query(...)):
    try:
        deleted = capture_store.delete_set(name)
    except (ValueError, capture_store.CaptureStoreError) as exc:
        raise _capture_store_error(exc)
    if not deleted:
        raise HTTPException(404, "Capture set does not exist")
    return ok({"name": capture_store.normalize_name(name), "deleted": True})


# =========================================================================
# Sessions and files
# =========================================================================
@app.get("/api/sessions")
def sessions():
    return ok(list_sessions())


@app.get("/api/sessions/{session_id}")
def session_detail(session_id: str):
    detail = get_session(session_id)
    if detail is None:
        raise HTTPException(404, f"Session does not exist: {session_id}")
    return ok(detail)


def _validate_rel_path(session_id: str, rel_path: str) -> Path:
    if not session_id or session_id in (".", "..") or "/" in session_id \
            or "\\" in session_id:
        raise HTTPException(400, "Invalid path: session_id is not allowed")
    if not rel_path or rel_path.startswith("/") or "\\" in rel_path \
            or any(part == ".." for part in rel_path.split("/")):
        raise HTTPException(400, "Invalid path")
    if session_id == "snapshots":
        # v1.3 compatibility: <file> (snapshots root) and <folder>/<file> both map to
        # <data_root>/snapshots/; v1.4: the target folder of POST /api/snapshot may be
        # any folder under data_root and the URL is <rel-to-data_root>/<file>, so both
        # are resolved against data_root. Both candidates must stay inside their root (traversal guard);
        # the existing legacy candidate wins so old URLs keep behaving as before.
        parts = rel_path.split("/")
        if any(not p for p in parts):
            raise HTTPException(400, "Invalid path")
        snapshots_base = get_snapshots_dir().resolve()
        data_base = get_data_root().resolve()
        candidates: list[Path] = []
        legacy = (snapshots_base / rel_path).resolve()
        if legacy == snapshots_base or snapshots_base in legacy.parents:
            candidates.append(legacy)
        current = (data_base / rel_path).resolve()
        if current == data_base or data_base in current.parents:
            candidates.append(current)
        if not candidates:
            raise HTTPException(400, "Invalid path")
        existing = [p for p in candidates if p.is_file()]
        return existing[0] if existing else candidates[0]
    captures_dir = get_captures_dir()
    parts = rel_path.split("/")
    groups = {"bias", "dark", "flat", "light", "masters", "calibrated"}
    if parts == ["meta.json"]:
        path = captures_dir / session_id / "meta.json"
    elif len(parts) == 2 and parts[0] in groups | {"results"}:
        if not parts[1]:
            raise HTTPException(400, "Invalid path")
        path = captures_dir / session_id / parts[0] / parts[1]
    elif len(parts) == 3 and parts[0] in groups and parts[1] == "thumbs":
        if not parts[2]:
            raise HTTPException(400, "Invalid path")
        path = captures_dir / session_id / parts[0] / "thumbs" / parts[2]
    else:
        raise HTTPException(400, "Invalid path")
    session_base = (captures_dir / session_id).resolve()
    resolved = path.resolve()
    if resolved != session_base and session_base not in resolved.parents:
        raise HTTPException(400, "Invalid path")
    return path


@app.get("/api/files/{session_id}/{rel_path:path}")
def files(session_id: str, rel_path: str):
    path = _validate_rel_path(session_id, rel_path)
    if not path.is_file():
        raise HTTPException(404, f"File does not exist: {rel_path}")
    media = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
    # v1.2: the frontend stretch preview and similar views need the latest file, so no caching.
    return FileResponse(path, media_type=media,
                        headers={"Cache-Control": "no-store"})


# =========================================================================
# Processing jobs
# =========================================================================
EXCLUDE_GROUPS = ("bias", "dark", "flat", "light")


def _spawn_job(job_id: str) -> None:
    asyncio.create_task(asyncio.to_thread(process_service.run_job, job_id))


def _check_session(session_id: str) -> None:
    if get_session(session_id) is None:
        raise HTTPException(404, f"Session does not exist: {session_id}")


def _ensure_capture_idle() -> None:
    status = capture_service.get_state()["status"]
    if status not in ("idle", "finished", "aborted", "error"):
        raise HTTPException(409, "Capture in progress, cannot submit processing job")


def _validate_exclude(session_id: str, exclude: dict | None) -> dict:
    """Validate exclude: filename matching only; returns 400 when the file does not exist."""
    if exclude is None:
        return {}
    if not isinstance(exclude, dict):
        raise HTTPException(400, "exclude must be an object")
    unknown = set(exclude.keys()) - set(EXCLUDE_GROUPS)
    if unknown:
        raise HTTPException(400, f"exclude has unknown groups: {sorted(unknown)}")
    normalized: dict[str, list[str]] = {}
    for group in EXCLUDE_GROUPS:
        names = exclude.get(group) or []
        if not isinstance(names, list):
            raise HTTPException(400, f"exclude.{group} must be an array")
        clean = []
        for name in names:
            if not isinstance(name, str) or not name:
                raise HTTPException(400, "exclude entries must be file names")
            if "/" in name or "\\" in name or ".." in name \
                    or Path(name).name != name:
                raise HTTPException(400, f"exclude accepts only file names (no paths): {name}")
            target = get_captures_dir() / session_id / group / name
            if not target.is_file():
                raise HTTPException(400, f"exclude file does not exist: {group}/{name}")
            clean.append(name)
        normalized[group] = list(dict.fromkeys(clean))   # Deduplicate, keep order
    return normalized


@app.post("/api/process/calibrate")
async def process_calibrate(body: CalibrateBody):
    _check_session(body.session_id)
    _ensure_capture_idle()
    params = body.model_dump()
    params["exclude"] = _validate_exclude(body.session_id, body.exclude)
    try:
        job = process_service.create_job("calibrate", params)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    _spawn_job(job["id"])
    return ok({"job_id": job["id"], "type": "calibrate", "status": "pending",
               "session_id": body.session_id})


@app.post("/api/process/stack")
async def process_stack(body: StackBody):
    _check_session(body.session_id)
    _ensure_capture_idle()
    params = body.model_dump()
    params["exclude"] = _validate_exclude(body.session_id, body.exclude)
    try:
        job = process_service.create_job("stack", params)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    _spawn_job(job["id"])
    return ok({"job_id": job["id"], "type": "stack", "status": "pending",
               "session_id": body.session_id})


@app.post("/api/process/stretch")
async def process_stretch(body: StretchBody):
    _check_session(body.session_id)
    _ensure_capture_idle()
    try:
        job = process_service.create_job("stretch", body.model_dump())
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    _spawn_job(job["id"])
    return ok({"job_id": job["id"], "type": "stretch", "status": "pending",
               "session_id": body.session_id})


@app.get("/api/process/jobs/{job_id}")
def process_job(job_id: str):
    job = process_service.get_job(job_id)
    if job is None:
        raise HTTPException(404, f"Job does not exist: {job_id}")
    return ok(job)


# =========================================================================
# WebSocket events
# =========================================================================
@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket):
    await events_manager.connect(websocket)
    try:
        # Send the current capture state snapshot on connect
        await websocket.send_text(json.dumps(
            {"topic": "capture", "data": capture_service.get_state()},
            ensure_ascii=False))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        events_manager.disconnect(websocket)


# =========================================================================
# Root path and static frontend
# =========================================================================
@app.get("/")
def root():
    if INDEX_HTML.is_file():
        return FileResponse(INDEX_HTML, headers=NO_CACHE_HEADERS)
    return ok({
        "frontend_built": False,
        "message": ("Frontend is not built. Run `cd frontend && npm run build` "
                    "to generate dist/, or use the Vite dev server during "
                    "development. API docs: /docs and docs/api.md."),
    })


@app.exception_handler(StarletteHTTPException)
async def spa_fallback(request: Request, exc: StarletteHTTPException):
    # A 404 for static assets falls back to the SPA; /api and /ws keep the uniform error envelope
    if exc.status_code == 404 and INDEX_HTML.is_file() \
            and not request.url.path.startswith("/api") \
            and not request.url.path.startswith("/ws"):
        return FileResponse(INDEX_HTML, headers=NO_CACHE_HEADERS)
    return JSONResponse(status_code=exc.status_code, content={
        "ok": False, "data": None, "error": str(exc.detail)})


if DIST_DIR.is_dir():
    app.mount("/", NoCacheIndexStaticFiles(directory=str(DIST_DIR), html=True),
              name="static")
