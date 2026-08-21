"""相机抽象层：MockCamera（macOS/无真机开发用）与 IC4Camera（Windows 真机）。

接口约定（前后端契约之外的后端内部接口）：
    open() / close() / grab_frame() -> np.uint16(2D, <=4095)
    get_properties() -> list[dict] / get_property(name) / set_property(name, value)
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any

import numpy as np

from .util import MAX_ADU

MOCK_ID = "mock-1"
MOCK_NAME = "Mock LEED/RHEED Camera"
MOCK_SERIAL = "MOCK-0001"
FULL_WIDTH = 1920
FULL_HEIGHT = 1200
READOUT_NOISE_ADU = 1.5
HOT_PIXEL_FRACTION = 0.0005
# 泊松噪声混合口径：低计数用精确泊松，高计数用高斯近似（σ=√λ），
# 与契约“泊松噪声（√signal）”一致，并把全分辨率帧生成压进 15fps 预算
POISSON_EXACT_MAX_LAMBDA = 24.0


class CameraError(RuntimeError):
    """相机操作错误（信息直接面向用户，中文）。"""


def _prop(name, label, value, ptype, *, min_=None, max_=None, step=None,
          unit="", readonly=False, options=None):
    p = {"name": name, "label": label, "value": value, "type": ptype,
         "readonly": readonly}
    if ptype in ("int", "float"):
        p["min"] = min_
        p["max"] = max_
        p["step"] = step
        p["unit"] = unit
    elif ptype == "enum":
        p["options"] = options or []
    return p


class BaseCamera:
    kind = ""

    def __init__(self, cam_id: str, name: str, serial: str):
        self.id = cam_id
        self.name = name
        self.serial = serial
        self._opened = False

    @property
    def opened(self) -> bool:
        return self._opened

    def open(self) -> None:  # pragma: no cover - 子类实现
        raise NotImplementedError

    def close(self) -> None:
        self._opened = False

    def grab_frame(self) -> np.ndarray:  # pragma: no cover - 子类实现
        raise NotImplementedError

    def get_properties(self) -> list[dict]:  # pragma: no cover - 子类实现
        raise NotImplementedError

    def get_property(self, name: str) -> Any:
        for p in self.get_properties():
            if p["name"] == name:
                return p["value"]
        raise CameraError(f"未知属性：{name}")

    def set_property(self, name: str, value: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    def info(self) -> dict:
        return {"id": self.id, "name": self.name, "serial": self.serial,
                "kind": self.kind, "connected": self.opened}


class MockCamera(BaseCamera):
    """合成 LEED/RHEED 静态衍射点阵。

    契约要点：
    - uint16、数值 ≤ 4095；规则格点高斯斑 + 亮 00 斑 + 背景辉光
      + 读出噪声（高斯 ~1.5 ADU）+ 泊松噪声 + 固定热像素（~0.05%）
    - exposure 线性放大信号并放大泊松噪声；gain(dB) 倍乘信号与噪声；
      black_level 为加性偏置；均按 12-bit 截断
    - 默认 ROI 1024×768，上限 1920×1200；温度随运行时间缓慢上升
    - 可选亚像素级慢漂移（供 registration 演示）
    """

    kind = "mock"

    def __init__(self):
        super().__init__(MOCK_ID, MOCK_NAME, MOCK_SERIAL)
        self._lock = threading.RLock()
        self._rng = np.random.default_rng(20240822)
        self._started_monotonic = None
        self._frame_count = 0
        self._drift_y = 0.0
        self._drift_x = 0.0
        self._base_cache: dict[str, np.ndarray] = {}
        self._drift_cache: dict[str, tuple] = {}
        self._hot_full = self._make_hot_pixels()

        self._props: dict[str, dict] = {
            "exposure": _prop("exposure", "曝光", 20000, "int",
                              min_=30, max_=30_000_000, step=1, unit="us"),
            "gain": _prop("gain", "增益", 0.0, "float",
                          min_=0.0, max_=48.0, step=0.1, unit="dB"),
            "black_level": _prop("black_level", "黑电平", 0, "int",
                                 min_=0, max_=255, step=1, unit="ADU"),
            "width": _prop("width", "ROI 宽度", 1024, "int",
                           min_=320, max_=FULL_WIDTH, step=8, unit="px"),
            "height": _prop("height", "ROI 高度", 768, "int",
                            min_=240, max_=FULL_HEIGHT, step=8, unit="px"),
            "offset_x": _prop("offset_x", "ROI X 偏移", (FULL_WIDTH - 1024) // 2,
                              "int", min_=0, max_=FULL_WIDTH - 1024, step=8, unit="px"),
            "offset_y": _prop("offset_y", "ROI Y 偏移", (FULL_HEIGHT - 768) // 2,
                              "int", min_=0, max_=FULL_HEIGHT - 768, step=8, unit="px"),
            "reverse_x": _prop("reverse_x", "水平镜像", False, "bool"),
            "reverse_y": _prop("reverse_y", "垂直镜像", False, "bool"),
            "trigger_mode": _prop("trigger_mode", "触发模式", False, "bool"),
            "mock_pattern": _prop("mock_pattern", "衍射图案", "leed", "enum",
                                  options=["leed", "rheed"]),
            "mock_scene": _prop("mock_scene", "模拟场景", "diffraction", "enum",
                                options=["diffraction", "uniform", "dark"]),
            "mock_drift": _prop("mock_drift", "模拟慢漂移", False, "bool"),
            "device_temperature": _prop("device_temperature", "传感器温度", 25.0,
                                        "float", min_=0.0, max_=100.0, step=0.1,
                                        unit="°C", readonly=True),
        }

    # ------------------------------------------------------------------ 基础
    def open(self) -> None:
        with self._lock:
            self._opened = True
            if self._started_monotonic is None:
                self._started_monotonic = time.monotonic()

    def close(self) -> None:
        with self._lock:
            self._opened = False

    # ---------------------------------------------------------------- 属性
    def get_properties(self) -> list[dict]:
        with self._lock:
            props = [dict(p) for p in self._props.values()]
            for p in props:
                if p["name"] == "device_temperature":
                    p["value"] = self._temperature()
                if p["name"] in ("offset_x",):
                    p["max"] = max(0, FULL_WIDTH - int(self._props["width"]["value"]))
                    p["value"] = min(p["value"], p["max"])
                if p["name"] == "offset_y":
                    p["max"] = max(0, FULL_HEIGHT - int(self._props["height"]["value"]))
                    p["value"] = min(p["value"], p["max"])
            return props

    def get_property(self, name: str) -> Any:
        with self._lock:
            if name not in self._props:
                raise CameraError(f"未知属性：{name}")
            if name == "device_temperature":
                return self._temperature()
            return self._props[name]["value"]

    def set_property(self, name: str, value: Any) -> Any:
        with self._lock:
            if name not in self._props:
                raise CameraError(f"未知属性：{name}")
            p = self._props[name]
            if p["readonly"]:
                raise CameraError(f"属性 {name} 只读")
            ptype = p["type"]
            if ptype in ("int", "float"):
                try:
                    v = float(value)
                except (TypeError, ValueError):
                    raise CameraError(f"属性 {name} 需要数值")
                v = min(max(v, float(p["min"])), float(p["max"]))
                actual = int(round(v)) if ptype == "int" else round(v, 3)
            elif ptype == "bool":
                actual = bool(value)
            elif ptype == "enum":
                if value not in p["options"]:
                    raise CameraError(f"属性 {name} 仅支持：{', '.join(p['options'])}")
                actual = value
            else:
                raise CameraError(f"不支持的属性类型：{ptype}")

            p["value"] = actual
            if name in ("width", "height", "offset_x", "offset_y"):
                self._clamp_offsets()
            return actual

    def _clamp_offsets(self) -> None:
        w = int(self._props["width"]["value"])
        h = int(self._props["height"]["value"])
        self._props["offset_x"]["value"] = min(
            self._props["offset_x"]["value"], max(0, FULL_WIDTH - w))
        self._props["offset_y"]["value"] = min(
            self._props["offset_y"]["value"], max(0, FULL_HEIGHT - h))

    def _temperature(self) -> float:
        if self._started_monotonic is None:
            minutes = 0.0
        else:
            minutes = max(0.0, (time.monotonic() - self._started_monotonic) / 60.0)
        return round(25.0 + 0.05 * minutes, 1)

    # ---------------------------------------------------------------- 图案
    def _make_hot_pixels(self) -> np.ndarray:
        n = int(FULL_WIDTH * FULL_HEIGHT * HOT_PIXEL_FRACTION)
        hot = np.zeros((FULL_HEIGHT, FULL_WIDTH), dtype=np.float32)
        idx = self._rng.choice(FULL_WIDTH * FULL_HEIGHT, n, replace=False)
        hot.flat[idx] = self._rng.uniform(2800.0, float(MAX_ADU), n)
        return hot

    def _pattern(self, kind: str) -> np.ndarray:
        with self._lock:
            cached = self._base_cache.get(kind)
            if cached is not None:
                return cached
        h, w = FULL_HEIGHT, FULL_WIDTH
        cx, cy = w / 2.0, h / 2.0
        y, x = np.mgrid[0:h, 0:w].astype(np.float32)
        # 荧光屏背景辉光：弱径向衰减 + 轻微亮度梯度（斑点信号占主导）
        base = 2.0 + 6.0 * np.exp(
            -(((x - cx) ** 2) / (2.0 * 640.0 ** 2)
              + ((y - cy) ** 2) / (2.0 * 460.0 ** 2)))
        base += 2.0 * (1.0 - y / float(h))
        if kind == "leed":
            ax, ay = 96.0, 96.0          # 正方倒格子
            sx, sy = 2.4, 2.4            # 圆形高斯斑
            order_norm = 0.30
        else:  # rheed：拉长的类条纹斑（椭圆高斯）
            ax, ay = 150.0, 54.0
            sx, sy = 5.2, 1.7
            order_norm = 0.18
        spot_amp = 60.0

        def _add_spot(i: int, j: int, amp: float) -> None:
            px, py = cx + i * ax, cy + j * ay
            rx, ry = 5.0 * sx, 5.0 * sy
            x0 = max(0, int(math.floor(px - rx)))
            x1 = min(w, int(math.ceil(px + rx)) + 1)
            y0 = max(0, int(math.floor(py - ry)))
            y1 = min(h, int(math.ceil(py + ry)) + 1)
            if x1 <= x0 or y1 <= y0:
                return
            xs = x[y0:y1, x0:x1]
            ys = y[y0:y1, x0:x1]
            g = amp * np.exp(
                -0.5 * (((xs - px) / sx) ** 2 + ((ys - py) / sy) ** 2))
            base[y0:y1, x0:x1] += g

        max_i = int(math.ceil((w + 6 * sx) / ax)) + 1
        max_j = int(math.ceil((h + 6 * sy) / ay)) + 1
        for i in range(-max_i, max_i + 1):
            for j in range(-max_j, max_j + 1):
                order2 = i * i + j * j
                if kind == "leed" and order2 == 0:
                    amp = spot_amp               # 亮 00 斑
                elif kind == "leed":
                    amp = spot_amp / (1.0 + order_norm * order2)
                else:
                    amp = spot_amp / (1.0 + order_norm * (order2 + abs(i) * 2))
                _add_spot(i, j, amp)

        base = np.clip(base, 0.0, None)
        mx = float(base.max())
        if mx > 0:
            base = base / mx
        with self._lock:
            self._base_cache[kind] = base.astype(np.float32)
            return self._base_cache[kind]

    @staticmethod
    def _shift_bilinear(img: np.ndarray, dy: float, dx: float) -> np.ndarray:
        """亚像素平移（边缘绕回，供慢漂移演示）。"""
        iy, fy = int(math.floor(dy)), dy - math.floor(dy)
        ix, fx = int(math.floor(dx)), dx - math.floor(dx)
        a00 = np.roll(img, (iy, ix), axis=(0, 1))
        a01 = np.roll(img, (iy, ix + 1), axis=(0, 1))
        a10 = np.roll(img, (iy + 1, ix), axis=(0, 1))
        a11 = np.roll(img, (iy + 1, ix + 1), axis=(0, 1))
        return ((1.0 - fy) * ((1.0 - fx) * a00 + fx * a01)
                + fy * ((1.0 - fx) * a10 + fx * a11))

    def _shifted_pattern(self, kind: str, dy: float, dx: float) -> np.ndarray:
        """漂移平移复用：整数平移的 4 个双线性基底按 (kind, iy, ix) 缓存，
        整数部分变化时才重新 np.roll，每帧只做加权求和。"""
        iy = int(math.floor(dy))
        ix = int(math.floor(dx))
        fy = dy - iy
        fx = dx - ix
        cached = self._drift_cache.get(kind)
        if cached is None or cached[0] != iy or cached[1] != ix:
            base = self._pattern(kind)
            with self._lock:
                a00 = np.roll(base, (iy, ix), axis=(0, 1))
                a01 = np.roll(base, (iy, ix + 1), axis=(0, 1))
                a10 = np.roll(base, (iy + 1, ix), axis=(0, 1))
                a11 = np.roll(base, (iy + 1, ix + 1), axis=(0, 1))
                cached = (iy, ix, a00, a01, a10, a11)
                self._drift_cache[kind] = cached
        _, _, a00, a01, a10, a11 = cached
        return ((1.0 - fy) * ((1.0 - fx) * a00 + fx * a01)
                + fy * ((1.0 - fx) * a10 + fx * a11))

    def _uniform(self) -> np.ndarray:
        """模拟平场：均匀照明 + 轻微固定 PRNU 梯度（归一化到约 0.5）。"""
        cached = self._base_cache.get("_uniform")
        if cached is not None:
            return cached
        h, w = FULL_HEIGHT, FULL_WIDTH
        y, x = np.mgrid[0:h, 0:w].astype(np.float32)
        uni = 0.52 + 0.03 * ((x - w / 2.0) / w + (y - h / 2.0) / h)
        uni = np.clip(uni, 0.0, 1.0)
        self._base_cache["_uniform"] = uni.astype(np.float32)
        return self._base_cache["_uniform"]

    # ---------------------------------------------------------------- 帧
    def grab_frame(self) -> np.ndarray:
        with self._lock:
            if not self._opened:
                raise CameraError("设备未打开")
            w = int(self._props["width"]["value"])
            h = int(self._props["height"]["value"])
            ox = int(self._props["offset_x"]["value"])
            oy = int(self._props["offset_y"]["value"])
            kind = self._props["mock_pattern"]["value"]
            scene = self._props["mock_scene"]["value"]
            drift = bool(self._props["mock_drift"]["value"])
            reverse_x = bool(self._props["reverse_x"]["value"])
            reverse_y = bool(self._props["reverse_y"]["value"])
            exposure = float(self._props["exposure"]["value"])
            gain_db = float(self._props["gain"]["value"])
            black = float(self._props["black_level"]["value"])
            # 漂移累计与帧计数统一在锁内更新（线程安全）
            if drift:
                self._drift_y += 0.03   # 亚像素级缓慢漂移
                self._drift_x += 0.02
            drift_y, drift_x = self._drift_y, self._drift_x
            self._frame_count += 1

        if scene == "dark":
            # 镜头盖模拟：仅暗电流（随曝光缓慢增加）+ 读出噪声 + 热像素
            img = np.full((h, w), 0.001, dtype=np.float32)
        elif scene == "uniform":
            base_full = self._uniform()
            img = base_full[oy:oy + h, ox:ox + w]
        else:
            if drift:
                base_full = self._shifted_pattern(kind, drift_y, drift_x)
            else:
                base_full = self._pattern(kind)
            img = base_full[oy:oy + h, ox:ox + w]
        if reverse_x:
            img = img[:, ::-1]
        if reverse_y:
            img = img[::-1, :]

        gain_lin = 10.0 ** (gain_db / 20.0)
        exposure_factor = exposure / 20000.0
        signal_e = img * (MAX_ADU * exposure_factor)
        signal_e = np.maximum(signal_e, 0.0, out=signal_e)

        with self._lock:
            phot = self._poisson_noise(signal_e)
            readout = self._rng.normal(0.0, READOUT_NOISE_ADU,
                                       size=img.shape).astype(np.float32)
            hot = self._hot_full[oy:oy + h, ox:ox + w]
        if reverse_x:
            hot = hot[:, ::-1]
        if reverse_y:
            hot = hot[::-1, :]

        out = phot + readout
        out *= gain_lin
        out += black
        out += hot
        np.clip(out, 0, MAX_ADU, out=out)
        return out.astype(np.uint16)

    def _poisson_noise(self, lam: np.ndarray) -> np.ndarray:
        """泊松噪声（√signal 口径，向量化）：
        - λ ≥ 24：高斯近似（σ=√λ，泊松在高计数下的标准近似）；
        - 0.5 ≤ λ < 24：精确 rng.poisson；
        - λ < 0.5：Bernoulli 近似（低计数下二事件概率可忽略）。
        """
        low = lam < POISSON_EXACT_MAX_LAMBDA
        phot = self._rng.normal(lam, np.sqrt(lam)).astype(np.float32)
        if low.any():
            tiny = lam < 0.5
            mid = low & ~tiny
            if mid.any():
                phot[mid] = self._rng.poisson(lam[mid]).astype(np.float32)
            if tiny.any():
                phot[tiny] = (self._rng.random(size=int(tiny.sum()))
                              < lam[tiny]).astype(np.float32)
        return phot


# =========================================================================
# IC4 真机封装（Windows/Linux；macOS 懒加载失败时给出清晰中文错误）
# =========================================================================

_IC4_LIB = None
_IC4_LIB_ERROR: str | None = None
_IC4_INFOS: list = []


def _load_ic4():
    """懒加载 imagingcontrol4；不可用时返回 (module|None, error)。"""
    global _IC4_LIB, _IC4_LIB_ERROR
    if _IC4_LIB is not None:
        return _IC4_LIB, None
    if _IC4_LIB_ERROR is not None:
        return None, _IC4_LIB_ERROR
    try:
        import imagingcontrol4 as ic4  # type: ignore
    except Exception as exc:  # noqa: BLE001
        _IC4_LIB_ERROR = (
            "该平台无法加载 imagingcontrol4。请确认在 Windows 采集主机上已安装 "
            "TIS ic4 驱动并执行 pip install imagingcontrol4。原始错误："
            f"{exc.__class__.__name__}: {exc}")
        return None, _IC4_LIB_ERROR
    try:
        ic4.Library.init()
        _IC4_LIB = ic4
        return ic4, None
    except Exception as exc:  # noqa: BLE001
        _IC4_LIB_ERROR = f"imagingcontrol4 初始化失败：{exc}"
        return None, _IC4_LIB_ERROR


def ic4_available() -> bool:
    lib, _ = _load_ic4()
    return lib is not None


def enumerate_ic4_devices() -> list[dict]:
    """枚举 ic4 设备（失败返回空列表，不影响 mock 路径）。"""
    global _IC4_INFOS
    lib, _ = _load_ic4()
    if lib is None:
        return []
    try:
        infos = list(lib.DeviceEnum.devices())
    except Exception:  # noqa: BLE001
        return []
    _IC4_INFOS = infos
    out = []
    for info in infos:
        serial = getattr(info, "serial", "") or "unknown"
        name = getattr(info, "model_name", "") or "IC4 Camera"
        out.append({"id": f"ic4-{serial}", "name": str(name),
                    "serial": str(serial), "kind": "ic4", "connected": False})
    return out


class IC4Camera(BaseCamera):
    """imagingcontrol4 真机封装（与 MockCamera 同一接口）。"""

    kind = "ic4"

    def __init__(self, cam_id: str, name: str, serial: str):
        super().__init__(cam_id, name, serial)
        self._grabber = None
        self._sink = None
        self._streaming = False

    # ---------------------------------------------------------------- 开关
    def open(self) -> None:
        lib, err = _load_ic4()
        if lib is None:
            raise CameraError(err or "该平台无法加载 imagingcontrol4")
        try:
            infos = list(lib.DeviceEnum.devices())
            target = None
            for info in infos:
                if str(getattr(info, "serial", "")) == self.serial:
                    target = info
                    break
            if target is None:
                raise CameraError(f"未找到序列号为 {self.serial} 的 ic4 设备")
            self._grabber = lib.Grabber()
            self._grabber.device_open(target)
            self._sink = lib.QueueSink(
                getattr(lib.PixelFormat, "Mono12p", None)
                or getattr(lib.PixelFormat, "Mono12"))
            self._opened = True
        except CameraError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._grabber = None
            raise CameraError(f"打开 ic4 设备失败（{self.name}）：{exc}")

    def close(self) -> None:
        try:
            if self._streaming and self._grabber is not None:
                self._grabber.stream_stop()
        finally:
            self._streaming = False
            self._grabber = None
            self._sink = None
            self._opened = False

    def _ensure_open(self):
        if not self._opened or self._grabber is None:
            raise CameraError("设备未打开")

    # ---------------------------------------------------------------- 帧
    def grab_frame(self) -> np.ndarray:
        self._ensure_open()
        try:
            if not self._streaming:
                self._grabber.stream_setup(self._sink)
                self._grabber.stream_start()
                self._streaming = True
            buf = self._sink.pop_output_buffer(timeout_ms=5000)
            arr = buf.numpy_wrap()
            img = np.asarray(arr, dtype=np.uint16)
            return np.array(img, dtype=np.uint16, copy=True)
        except Exception as exc:  # noqa: BLE001
            raise CameraError(f"ic4 抓帧失败：{exc}")

    # ---------------------------------------------------------------- 属性
    def get_properties(self) -> list[dict]:
        self._ensure_open()
        props = []
        pmap = self._grabber.device_property_map
        spec = [
            ("exposure", "曝光", "EXPOSURE", "float", "us"),
            ("gain", "增益", "GAIN", "float", "dB"),
            ("black_level", "黑电平", "BLACK_LEVEL", "int", "ADU"),
            ("reverse_x", "水平镜像", "REVERSE_X", "bool", ""),
            ("reverse_y", "垂直镜像", "REVERSE_Y", "bool", ""),
            ("trigger_mode", "触发模式", "TRIGGER_MODE", "bool", ""),
            ("device_temperature", "传感器温度", "DEVICE_TEMPERATURE", "float", "°C"),
        ]
        for name, label, propid, ptype, unit in spec:
            try:
                pid = getattr(__import__("imagingcontrol4").PropId, propid)
            except Exception:  # noqa: BLE001
                pid = propid
            try:
                raw = pmap.get_value(pid)
                p = _prop(name, label, raw, ptype,
                          min_=pmap.get_range(pid)[0], max_=pmap.get_range(pid)[1],
                          step=pmap.get_range(pid)[2] if hasattr(pmap, "get_range") else None,
                          unit=unit, readonly=pmap.get_visibility(pid) != "rw")
                props.append(p)
            except Exception:  # noqa: BLE001
                continue
        # ROI（尽力枚举，失败时跳过）
        for name, label, unit in [("width", "ROI 宽度", "px"),
                                  ("height", "ROI 高度", "px"),
                                  ("offset_x", "ROI X 偏移", "px"),
                                  ("offset_y", "ROI Y 偏移", "px")]:
            try:
                pid = getattr(__import__("imagingcontrol4").PropId, name.upper())
                v = int(pmap.get_value(pid))
                props.append(_prop(name, label, v, "int", min_=0, max_=1_000_000,
                                   step=1, unit=unit))
            except Exception:  # noqa: BLE001
                continue
        return props

    def set_property(self, name: str, value: Any) -> Any:
        self._ensure_open()
        pmap = self._grabber.device_property_map
        pid_map = {
            "exposure": "EXPOSURE", "gain": "GAIN", "black_level": "BLACK_LEVEL",
            "reverse_x": "REVERSE_X", "reverse_y": "REVERSE_Y",
            "trigger_mode": "TRIGGER_MODE", "width": "WIDTH", "height": "HEIGHT",
            "offset_x": "OFFSET_X", "offset_y": "OFFSET_Y",
        }
        if name not in pid_map:
            raise CameraError(f"未知属性：{name}")
        try:
            ic4 = __import__("imagingcontrol4")
            pid = getattr(ic4.PropId, pid_map[name])
            pmap.set_value(pid, value)
            actual = pmap.get_value(pid)
            return actual
        except Exception as exc:  # noqa: BLE001
            raise CameraError(f"设置属性 {name} 失败：{exc}")


class CameraService:
    """设备服务：枚举、打开/关闭、属性读写、抓帧。"""

    def __init__(self):
        self._device: BaseCamera | None = None
        self._lock = threading.RLock()

    @property
    def device(self) -> BaseCamera | None:
        return self._device

    def is_open(self, cam_id: str | None = None) -> bool:
        with self._lock:
            if self._device is None or not self._device.opened:
                return False
            if cam_id is None:
                return True
            return self._device.id == cam_id

    def list_devices(self) -> list[dict]:
        devices = [{
            "id": MOCK_ID, "name": MOCK_NAME, "serial": MOCK_SERIAL,
            "kind": "mock", "connected": self.is_open(MOCK_ID),
        }]
        for d in enumerate_ic4_devices():
            d = dict(d)
            d["connected"] = self.is_open(d["id"])
            devices.append(d)
        return devices

    def _find(self, cam_id: str) -> BaseCamera:
        if cam_id == MOCK_ID:
            return MockCamera()
        if cam_id.startswith("ic4-"):
            serial = cam_id[len("ic4-"):]
            found = None
            for d in enumerate_ic4_devices():
                if d["id"] == cam_id:
                    found = d
                    break
            if found is None:
                raise CameraError(f"设备不存在：{cam_id}")
            return IC4Camera(found["id"], found["name"], found["serial"])
        raise CameraError(f"设备不存在：{cam_id}")

    def open(self, cam_id: str) -> BaseCamera:
        with self._lock:
            if self._device is not None and self._device.id == cam_id \
                    and self._device.opened:
                return self._device
            if self._device is not None:      # 自动关闭旧设备
                try:
                    self._device.close()
                except Exception:  # noqa: BLE001
                    pass
            cam = self._find(cam_id)
            cam.open()
            self._device = cam
            return cam

    def close(self) -> None:
        with self._lock:
            if self._device is not None:
                try:
                    self._device.close()
                finally:
                    self._device = None

    def ensure_device(self) -> BaseCamera:
        with self._lock:
            if self._device is None or not self._device.opened:
                raise CameraError("设备未打开")
            return self._device

    def grab_frame(self) -> np.ndarray:
        return self.ensure_device().grab_frame()

    def get_properties(self) -> list[dict]:
        return self.ensure_device().get_properties()

    def get_property(self, name: str) -> Any:
        return self.ensure_device().get_property(name)

    def set_property(self, name: str, value: Any) -> Any:
        return self.ensure_device().set_property(name, value)

    def device_info(self) -> dict:
        if self._device is None or not self._device.opened:
            return {"id": "", "name": "", "serial": "",
                    "connected": False, "properties": []}
        d = self._device
        return {"id": d.id, "name": d.name, "serial": d.serial,
                "connected": True, "properties": d.get_properties()}
