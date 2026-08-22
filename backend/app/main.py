"""FastAPI 入口：REST + /ws/live + /ws/events + 静态前端托管。"""
from __future__ import annotations

import asyncio
import json
import logging
import queue
import sys
import threading
import time
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from typing import Any

from fastapi import (FastAPI, HTTPException, Request, WebSocket,
                     WebSocketDisconnect)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from PIL import Image

from . import APP_VERSION
from .camera import CameraService, CameraError, ic4_available
from .capture import CaptureService, save_snapshot
from .process import ProcessService
from .sessions import get_session, list_sessions
from .util import (CONTENT_TYPES, REPO_ROOT, auto_contrast_8bit, ensure_dirs,
                   frame_stats, get_captures_dir, get_data_root,
                   get_snapshots_dir, histogram_256,
                   is_data_root_writable, ok, set_data_root)

DIST_DIR = REPO_ROOT / "frontend" / "dist"
INDEX_HTML = DIST_DIR / "index.html"


# =========================================================================
# WebSocket 管理器（跨线程推送）
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
# 实时流服务：抓帧线程 + 最新帧队列(maxsize=1) + 推流线程
# =========================================================================
class LiveService:
    def __init__(self, camera_service: CameraService, manager: LiveManager):
        self.camera = camera_service
        self.manager = manager
        self.streaming = False
        self.params: dict = {"fps": 15, "scale": 1.0, "quality": 80}
        self._stop = threading.Event()
        self._queue: queue.Queue = queue.Queue(maxsize=1)
        self._grabber: threading.Thread | None = None
        self._pusher: threading.Thread | None = None

    def start(self, fps: int, scale: float, quality: int) -> dict:
        if self.streaming:
            raise RuntimeError("实时流已在运行")
        self.camera.ensure_device()
        if not isinstance(fps, int) or not (1 <= fps <= 30):
            raise RuntimeError("fps 必须在 1–30 之间")
        if scale not in (1.0, 0.5, 0.25):
            raise RuntimeError("scale 仅支持 1.0 / 0.5 / 0.25")
        if not isinstance(quality, int):
            raise RuntimeError("quality 必须是整数")
        quality = max(10, min(quality, 100))
        self.params = {"fps": fps, "scale": scale, "quality": quality}
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
        self.streaming = False
        self._stop.set()
        for t in (self._grabber, self._pusher):
            if t is not None and t.is_alive():
                t.join(timeout=2.0)
        self._grabber = self._pusher = None
        return {"streaming": False}

    def _triggered(self) -> bool:
        try:
            return bool(self.camera.get_property("trigger_mode"))
        except Exception:  # noqa: BLE001
            return False

    def _grab_loop(self) -> None:
        interval = 1.0 / max(self.params["fps"], 1)
        while not self._stop.is_set():
            started = time.monotonic()
            if self._triggered():
                time.sleep(0.02)   # trigger_mode=true 时不自动出帧
                continue
            try:
                frame = self.camera.grab_frame()
                scale = self.params["scale"]
                if scale == 0.5:
                    frame = frame[::2, ::2]
                elif scale == 0.25:
                    frame = frame[::4, ::4]
                try:
                    self._queue.get_nowait()      # 丢弃旧帧，只保留最新
                except queue.Empty:
                    pass
                self._queue.put_nowait(frame)
            except Exception:  # noqa: BLE001
                pass
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, interval - elapsed))
        # 通知推流线程退出
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
            stats["histogram"] = histogram_256(preview)
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
# 服务实例
# =========================================================================
camera_service = CameraService()
events_manager = EventsManager()
live_manager = LiveManager()
capture_service = CaptureService(camera_service, broadcast=events_manager.broadcast)
process_service = ProcessService(broadcast=events_manager.broadcast)
live_service = LiveService(camera_service, live_manager)


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    loop = asyncio.get_running_loop()
    events_manager.set_loop(loop)
    live_manager.set_loop(loop)
    yield
    live_service.stop()
    capture_service.shutdown()   # 优雅中止进行中的拍摄
    camera_service.close()


app = FastAPI(title="Diffraction Stacking 后端", version=APP_VERSION,
              lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================================
# 统一错误包裹
# =========================================================================
@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=400, content={
        "ok": False, "data": None,
        "error": "请求参数校验失败：" + str(exc.errors()[:3])})


@app.exception_handler(Exception)
async def generic_handler(request: Request, exc: Exception):
    logging.getLogger("uvicorn.error").exception(
        "unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={
        "ok": False, "data": None, "error": "服务器内部错误"})


def raise_cam_err(exc: Exception) -> None:
    msg = str(exc)
    # IC4 路径：camera.py 已按官方 ErrorCode 归类为 400/404/409/503/500，
    # 通过 CameraError.status 传递，优先使用；Mock 路径继续走信息匹配。
    status = getattr(exc, "status", None)
    if status == 500:
        # 契约：500 统一返回“服务器内部错误”，不向客户端泄漏异常细节
        logging.getLogger("uvicorn.error").error(
            "ic4 camera error: %s", exc, exc_info=True)
        raise HTTPException(500, "服务器内部错误")
    if status in (400, 404, 409, 503):
        raise HTTPException(status, msg)
    if "设备未打开" in msg or "设备已关闭" in msg:
        raise HTTPException(409, msg)
    if "无法加载 imagingcontrol4" in msg:
        raise HTTPException(503, msg)
    if "设备不存在" in msg:
        raise HTTPException(404, msg)
    if any(k in msg for k in ("仅支持", "必须在", "未知属性", "只读", "需要数值",
                              "必须是整数")):
        raise HTTPException(400, msg)
    if "实时流已在运行" in msg:
        raise HTTPException(409, msg)
    # 未分类的相机错误：只记日志，不向客户端泄漏异常细节
    logging.getLogger("uvicorn.error").error(
        "unclassified camera error: %s", exc, exc_info=True)
    raise HTTPException(500, "服务器内部错误")


# =========================================================================
# Pydantic 模型
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


class SettingsBody(BaseModel):
    data_root: str


class PhaseIn(BaseModel):
    type: str
    count: int = Field(ge=1, le=10000)
    exposure_us: float | None = None
    note: str | None = ""


class PlanBody(BaseModel):
    session_name: str
    phases: list[PhaseIn] = Field(min_length=1)


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
# 健康与设备
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
        raise HTTPException(400, "data_root 不能为空")
    root = Path(raw)
    if not root.is_absolute():
        raise HTTPException(400, "data_root 必须是绝对路径")
    root = root.resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
        (root / "snapshots").mkdir(parents=True, exist_ok=True)
        (root / "captures").mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(400, f"无法创建数据目录：{exc}")
    try:
        set_data_root(root)
    except OSError as exc:
        raise HTTPException(400, f"保存数据目录设置失败：{exc}")
    return ok({"data_root": str(get_data_root()),
               "writable": is_data_root_writable(root)})


@app.get("/api/devices")
def devices():
    return ok(camera_service.list_devices())


@app.post("/api/device/open")
def device_open(body: OpenBody):
    cam_id = body.id.strip()
    known = [d["id"] for d in camera_service.list_devices()]
    if cam_id not in known:
        raise HTTPException(404, f"设备不存在：{cam_id}")
    try:
        cam = camera_service.open(cam_id)
    except Exception as exc:  # noqa: BLE001
        raise_cam_err(exc)
    return ok({"id": cam.id, "name": cam.name, "serial": cam.serial,
               "connected": True})


@app.post("/api/device/close")
def device_close():
    camera_service.close()
    return ok({"connected": False})


@app.get("/api/device/info")
def device_info():
    try:
        return ok(camera_service.device_info())
    except Exception as exc:  # noqa: BLE001
        raise_cam_err(exc)


# =========================================================================
# 属性
# =========================================================================
@app.get("/api/properties")
def properties():
    try:
        return ok({"properties": camera_service.get_properties()})
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
# 实时预览与抓拍
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


@app.post("/api/snapshot")
def snapshot(body: SnapshotBody):
    fmt = body.format
    if fmt not in ("fits", "tiff", "png"):
        raise HTTPException(400, "format 仅支持 fits|tiff|png")
    try:
        frame = camera_service.grab_frame()
        cam = camera_service.device
        cam_info = {"name": getattr(cam, "name", "unknown"),
                    "serial": getattr(cam, "serial", "unknown")}
        props = {}
        for key in ("exposure", "gain", "black_level", "device_temperature"):
            try:
                props[key] = camera_service.get_property(key)
            except Exception:  # noqa: BLE001
                props[key] = 0.0
    except Exception as exc:  # noqa: BLE001
        raise_cam_err(exc)
    try:
        return ok(save_snapshot(frame, fmt, cam_info, props))
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("uvicorn.error").error(
            "snapshot save failed: %s", exc, exc_info=True)
        raise HTTPException(500, "服务器内部错误")


# =========================================================================
# 序列拍摄
# =========================================================================
@app.post("/api/capture/plan")
def capture_plan(body: PlanBody):
    phases = [p.model_dump() for p in body.phases]
    for p in phases:
        if p["type"] not in ("bias", "dark", "flat", "light"):
            raise HTTPException(400, f"未知相位类型：{p['type']}")
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
# Session 与文件
# =========================================================================
@app.get("/api/sessions")
def sessions():
    return ok(list_sessions())


@app.get("/api/sessions/{session_id}")
def session_detail(session_id: str):
    detail = get_session(session_id)
    if detail is None:
        raise HTTPException(404, f"会话不存在：{session_id}")
    return ok(detail)


def _validate_rel_path(session_id: str, rel_path: str) -> Path:
    if not session_id or ".." in session_id or "/" in session_id \
            or "\\" in session_id:
        raise HTTPException(400, "非法路径：session_id 不合法")
    if not rel_path or rel_path.startswith("/") or ".." in rel_path \
            or "\\" in rel_path:
        raise HTTPException(400, "非法路径")
    if session_id == "snapshots":
        if "/" in rel_path:
            raise HTTPException(400, "非法路径")
        base = get_snapshots_dir()
        path = base / rel_path
        if path.resolve().parent != base.resolve():
            raise HTTPException(400, "非法路径")
        return path
    captures_dir = get_captures_dir()
    parts = rel_path.split("/")
    groups = {"bias", "dark", "flat", "light", "masters", "calibrated"}
    if parts == ["meta.json"]:
        path = captures_dir / session_id / "meta.json"
    elif len(parts) == 2 and parts[0] in groups | {"results"}:
        if not parts[1]:
            raise HTTPException(400, "非法路径")
        path = captures_dir / session_id / parts[0] / parts[1]
    elif len(parts) == 3 and parts[0] in groups and parts[1] == "thumbs":
        if not parts[2]:
            raise HTTPException(400, "非法路径")
        path = captures_dir / session_id / parts[0] / "thumbs" / parts[2]
    else:
        raise HTTPException(400, "非法路径")
    session_base = (captures_dir / session_id).resolve()
    resolved = path.resolve()
    if resolved != session_base and session_base not in resolved.parents:
        raise HTTPException(400, "非法路径")
    return path


@app.get("/api/files/{session_id}/{rel_path:path}")
def files(session_id: str, rel_path: str):
    path = _validate_rel_path(session_id, rel_path)
    if not path.is_file():
        raise HTTPException(404, f"文件不存在：{rel_path}")
    media = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
    # v1.2：前端拉伸预览等场景依赖最新文件，禁止缓存。
    return FileResponse(path, media_type=media,
                        headers={"Cache-Control": "no-store"})


# =========================================================================
# 处理任务
# =========================================================================
EXCLUDE_GROUPS = ("bias", "dark", "flat", "light")


def _spawn_job(job_id: str) -> None:
    asyncio.create_task(asyncio.to_thread(process_service.run_job, job_id))


def _check_session(session_id: str) -> None:
    if get_session(session_id) is None:
        raise HTTPException(404, f"会话不存在：{session_id}")


def _ensure_capture_idle() -> None:
    status = capture_service.get_state()["status"]
    if status not in ("idle", "finished", "aborted", "error"):
        raise HTTPException(409, "拍摄进行中，不能提交处理任务")


def _validate_exclude(session_id: str, exclude: dict | None) -> dict:
    """校验 exclude：仅文件名匹配；不存在返回 400。"""
    if exclude is None:
        return {}
    if not isinstance(exclude, dict):
        raise HTTPException(400, "exclude 必须是对象")
    unknown = set(exclude.keys()) - set(EXCLUDE_GROUPS)
    if unknown:
        raise HTTPException(400, f"exclude 含未知分组：{sorted(unknown)}")
    normalized: dict[str, list[str]] = {}
    for group in EXCLUDE_GROUPS:
        names = exclude.get(group) or []
        if not isinstance(names, list):
            raise HTTPException(400, f"exclude.{group} 必须是数组")
        clean = []
        for name in names:
            if not isinstance(name, str) or not name:
                raise HTTPException(400, "exclude 中只能填文件名")
            if "/" in name or "\\" in name or ".." in name \
                    or Path(name).name != name:
                raise HTTPException(400, f"exclude 只接受文件名（不含路径）：{name}")
            target = get_captures_dir() / session_id / group / name
            if not target.is_file():
                raise HTTPException(400, f"exclude 文件不存在：{group}/{name}")
            clean.append(name)
        normalized[group] = list(dict.fromkeys(clean))   # 去重保序
    return normalized


@app.post("/api/process/calibrate")
async def process_calibrate(body: CalibrateBody):
    _check_session(body.session_id)
    _ensure_capture_idle()
    params = body.model_dump()
    params["exclude"] = _validate_exclude(body.session_id, body.exclude)
    job = process_service.create_job("calibrate", params)
    _spawn_job(job["id"])
    return ok({"job_id": job["id"], "type": "calibrate", "status": "pending"})


@app.post("/api/process/stack")
async def process_stack(body: StackBody):
    _check_session(body.session_id)
    _ensure_capture_idle()
    params = body.model_dump()
    params["exclude"] = _validate_exclude(body.session_id, body.exclude)
    job = process_service.create_job("stack", params)
    _spawn_job(job["id"])
    return ok({"job_id": job["id"], "type": "stack", "status": "pending"})


@app.post("/api/process/stretch")
async def process_stretch(body: StretchBody):
    _check_session(body.session_id)
    _ensure_capture_idle()
    job = process_service.create_job("stretch", body.model_dump())
    _spawn_job(job["id"])
    return ok({"job_id": job["id"], "type": "stretch", "status": "pending"})


@app.get("/api/process/jobs/{job_id}")
def process_job(job_id: str):
    job = process_service.get_job(job_id)
    if job is None:
        raise HTTPException(404, f"任务不存在：{job_id}")
    return ok(job)


# =========================================================================
# WebSocket 事件
# =========================================================================
@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket):
    await events_manager.connect(websocket)
    try:
        # 连接即发送当前拍摄状态快照
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
# 根路径与静态前端
# =========================================================================
@app.get("/")
def root():
    if INDEX_HTML.is_file():
        return FileResponse(INDEX_HTML)
    return ok({
        "frontend_built": False,
        "message": "前端未构建。请执行 cd frontend && npm run build 生成 dist/，"
                   "或开发模式下访问 Vite dev server。API 文档见 /docs 与 docs/api.md。",
    })


@app.exception_handler(StarletteHTTPException)
async def spa_fallback(request: Request, exc: StarletteHTTPException):
    # 静态资源 404 做 SPA fallback；/api、/ws 保持统一错误包裹
    if exc.status_code == 404 and INDEX_HTML.is_file() \
            and not request.url.path.startswith("/api") \
            and not request.url.path.startswith("/ws"):
        return FileResponse(INDEX_HTML)
    return JSONResponse(status_code=exc.status_code, content={
        "ok": False, "data": None, "error": str(exc.detail)})


if DIST_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(DIST_DIR), html=True),
              name="static")
