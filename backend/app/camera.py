"""相机抽象层：MockCamera（macOS/无真机开发用）与 IC4Camera（Windows 真机）。

接口约定（前后端契约之外的后端内部接口）：
    open() / close() / grab_frame() -> np.uint16(2D, <=4095)
    get_properties() -> list[dict] / get_property(name) / set_property(name, value)
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from typing import Any

import numpy as np

from .util import MAX_ADU, REPO_ROOT

_log = logging.getLogger(__name__)

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
    """Camera operation error (messages are user-facing and in English).

    ``status`` is an optional HTTP semantic hint (400/404/409/503/500):
    the IC4 path maps official ErrorCode values; the Mock path does not set it
    and main.raise_cam_err falls back to English keyword matching.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


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


# 扩展属性形状（Parameters 标签页 / GET /api/properties/all）：
# 在 _prop 基础上增加 type=string/command、value=string|null、以及
# locked/available/visibility 三个可选字段。
def _prop_all(name, label, value, ptype, *, min_=None, max_=None, step=None,
              unit="", readonly=False, options=None, locked=False,
              available=True, visibility=None):
    p = {"name": name, "label": label, "value": value, "type": ptype,
         "readonly": readonly, "locked": locked, "available": available}
    if visibility:
        p["visibility"] = visibility
    if ptype in ("int", "float"):
        p["min"] = min_
        p["max"] = max_
        p["step"] = step
        p["unit"] = unit
    elif ptype == "enum":
        p["options"] = options or []
    return p


# GenICam 属性类型 → 扩展形状 type（与 dump_camera_props.py 的 _TYPE_MAP 对齐）。
_GENICAM_TYPE_MAP = {
    "INTEGER": "int",
    "FLOAT": "float",
    "ENUMERATION": "enum",
    "ENUM": "enum",
    "BOOLEAN": "bool",
    "BOOL": "bool",
    "STRING": "string",
    "COMMAND": "command",
    # Category/Register/Entry/Port 节点无标量值，按 string+null 呈现（不可写）。
    "CATEGORY": "string",
    "REGISTER": "string",
    "ENUMENTRY": "string",
    "PORT": "string",
    "UNKNOWN": "string",
}


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

    def stop_acquisition(self) -> None:
        """停止设备采集但保持设备打开（默认为空操作）。

        真机（IC4）在这里调用 stream_stop，让属性从 locked 状态解除；
        Mock 没有硬件采集锁，直接无操作即可。
        """

    def get_properties(self) -> list[dict]:  # pragma: no cover - 子类实现
        raise NotImplementedError

    def get_all_properties(self) -> list[dict]:  # pragma: no cover - 子类实现
        """返回相机【全部】可控参数（扩展形状），供 Parameters 标签页使用。"""
        raise NotImplementedError

    def get_property(self, name: str) -> Any:
        for p in self.get_properties():
            if p["name"] == name:
                return p["value"]
        raise CameraError(f"Unknown property: {name}")

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
    - 默认 capture region（Width × Height）1024×768，上限 1920×1200；温度随运行时间缓慢上升
    - 可选亚像素级慢漂移（供 registration 演示）
    """

    kind = "mock"

    # GenICam 原始属性名 → 已模拟的 snake_case 键（get_all_properties/get_property/
    # set_property 都按此映射把两者同步）。Exposure 是 PropId EXPOSURE_TIME 的别名，
    # JSON 里实际叫 ExposureTime，两者都映射到 exposure。
    _GENICAM_TO_SNAKE = {
        "Exposure": "exposure",
        "ExposureTime": "exposure",
        "Gain": "gain",
        "BlackLevel": "black_level",
        "Width": "width",
        "Height": "height",
        "OffsetX": "offset_x",
        "OffsetY": "offset_y",
        "ReverseX": "reverse_x",
        "ReverseY": "reverse_y",
        "TriggerMode": "trigger_mode",
        "ExposureAuto": "auto_exposure",
        "GainAuto": "auto_gain",
        "DeviceTemperature": "device_temperature",
    }
    # 仅存在于 Mock、不属于真实相机参数（不出现在 /all 的正常路径里）。
    _MOCK_ONLY_PROPS = frozenset(("mock_pattern", "mock_scene", "mock_drift"))
    _PROPS_JSON = REPO_ROOT / "docs" / "dmk-33gx249e-camera-props.json"

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
        # 扩展属性（JSON 里未映射到 snake_case 的“其余”属性），惰性加载。
        self._json_props: list[dict] | None = None
        self._extra_props: dict[str, dict] | None = None

        self._props: dict[str, dict] = {
            "exposure": _prop("exposure", "Exposure", 20000, "int",
                              min_=30, max_=30_000_000, step=1, unit="us"),
            "gain": _prop("gain", "Gain", 0.0, "float",
                          min_=0.0, max_=48.0, step=0.1, unit="dB"),
            "auto_exposure": _prop("auto_exposure", "Auto Exposure", True, "bool"),
            "auto_gain": _prop("auto_gain", "Auto Gain", True, "bool"),
            "black_level": _prop("black_level", "Black Level", 0, "int",
                                 min_=0, max_=255, step=1, unit="ADU"),
            "width": _prop("width", "Width", 1024, "int",
                           min_=480, max_=FULL_WIDTH, step=16, unit="px"),
            "height": _prop("height", "Height", 768, "int",
                            min_=4, max_=FULL_HEIGHT, step=4, unit="px"),
            "offset_x": _prop("offset_x", "Offset X", (FULL_WIDTH - 1024) // 2,
                              "int", min_=0, max_=FULL_WIDTH - 1024, step=4, unit="px"),
            "offset_y": _prop("offset_y", "Offset Y", (FULL_HEIGHT - 768) // 2,
                              "int", min_=0, max_=FULL_HEIGHT - 768, step=4, unit="px"),
            "reverse_x": _prop("reverse_x", "Reverse X", False, "bool"),
            "reverse_y": _prop("reverse_y", "Reverse Y", False, "bool"),
            "trigger_mode": _prop("trigger_mode", "Trigger Mode", False, "bool"),
            "mock_pattern": _prop("mock_pattern", "Diffraction Pattern", "leed", "enum",
                                  options=["leed", "rheed"]),
            "mock_scene": _prop("mock_scene", "Mock Scene", "diffraction", "enum",
                                options=["diffraction", "uniform", "dark"]),
            "mock_drift": _prop("mock_drift", "Mock Slow Drift", False, "bool"),
            "device_temperature": _prop("device_temperature", "Sensor Temperature", 25.0,
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
            snake = self._GENICAM_TO_SNAKE.get(name, name)
            if snake in self._props:
                if snake == "device_temperature":
                    return self._temperature()
                return self._props[snake]["value"]
            extra = self._ensure_extra_props()
            if name in extra:
                return extra[name]["value"]
            raise CameraError(f"Unknown property: {name}")

    def set_property(self, name: str, value: Any) -> Any:
        with self._lock:
            snake = self._GENICAM_TO_SNAKE.get(name, name)
            if snake in self._props:
                return self._set_mock_prop(snake, value)
            extra = self._ensure_extra_props()
            if name in extra:
                return self._set_extra_prop(name, value)
            raise CameraError(f"Unknown property: {name}")

    def _set_mock_prop(self, name: str, value: Any) -> Any:
        """现有 13 个 snake_case 契约属性（及 mock 专属属性）的读写路径，保持不变。"""
        p = self._props[name]
        if p["readonly"]:
            raise CameraError(f"Property {name} is read-only")
        # Manual exposure/gain control disables the corresponding auto
        # control first (mirrors the DMK 33GX249e behavior).
        if name == "exposure" and self._props["auto_exposure"]["value"]:
            self._props["auto_exposure"]["value"] = False
        if name == "gain" and self._props["auto_gain"]["value"]:
            self._props["auto_gain"]["value"] = False
        ptype = p["type"]
        if ptype in ("int", "float"):
            try:
                v = float(value)
            except (TypeError, ValueError):
                raise CameraError(f"Property {name} requires a numeric value")
            v = min(max(v, float(p["min"])), float(p["max"]))
            actual = int(round(v)) if ptype == "int" else round(v, 3)
        elif ptype == "bool":
            actual = bool(value)
        elif ptype == "enum":
            if value not in p["options"]:
                raise CameraError(f"Property {name} only supports: {', '.join(p['options'])}")
            actual = value
        else:
            raise CameraError(f"Unsupported property type: {ptype}")

        p["value"] = actual
        if name in ("width", "height", "offset_x", "offset_y"):
            self._clamp_offsets()
        return actual

    # ------------------------------------------------- 扩展属性（Parameters 标签页）
    def get_all_properties(self) -> list[dict]:
        """返回相机【全部】可控参数（扩展形状），供 Parameters 标签页使用。

        正常路径：从 docs/dmk-33gx249e-camera-props.json 加载 164 个属性，
        已映射的 GenICam 名转成 snake_case 键并同步到模拟值；其余属性用
        ``self._extra_props`` 存/取当前值（对帧无影响）。JSON 缺失时优雅回退
        到现有模拟属性（仍以扩展形状输出，不抛异常）。
        """
        with self._lock:
            extra = self._ensure_extra_props()
            raw = self._load_json_props()
            result: list[dict] = []
            if raw:
                seen: set[str] = set()
                for p in raw:
                    gname = p.get("name") or ""
                    snake = self._GENICAM_TO_SNAKE.get(gname)
                    if snake and snake in self._props:
                        result.append(self._merged_prop(snake, p))
                        seen.add(snake)
                    elif gname in extra:
                        result.append(dict(extra[gname]))
                        seen.add(gname)
                # 兜底：JSON 里缺了某个契约属性时，仍把模拟定义补上。
                for snake in self._props:
                    if snake in self._MOCK_ONLY_PROPS or snake in seen:
                        continue
                    result.append(self._merged_prop(snake, None))
                return result
            # 优雅回退：无 JSON → 现有模拟属性转扩展形状。
            return [self._merged_prop(snake, None) for snake in self._props]

    def _load_json_props(self) -> list[dict] | None:
        if self._json_props is not None:
            return self._json_props or None
        props: list[dict] = []
        try:
            data = json.loads(self._PROPS_JSON.read_text(encoding="utf-8"))
            props = [p for p in (data.get("properties") or [])
                     if isinstance(p, dict)]
        except Exception as exc:  # noqa: BLE001 - 缺文件/损坏时回退，不抛异常
            _log.warning("mock properties JSON unavailable (%s), falling back", exc)
        self._json_props = props
        return props or None

    def _ensure_extra_props(self) -> dict[str, dict]:
        """惰性把 JSON 里未映射的属性转成扩展形状（键 = GenICam 名）。"""
        if self._extra_props is not None:
            return self._extra_props
        extra: dict[str, dict] = {}
        raw = self._load_json_props()
        if raw:
            for p in raw:
                gname = p.get("name") or ""
                if gname in self._GENICAM_TO_SNAKE:
                    continue
                extra[gname] = self._json_to_extended(p)
        self._extra_props = extra
        return extra

    @staticmethod
    def _json_to_extended(p: dict) -> dict:
        """把 dump JSON 里的单个属性转成扩展形状（保留当前值）。"""
        ptype = _GENICAM_TYPE_MAP.get(str(p.get("type", "")).upper(), "string")
        name = p.get("name") or ""
        entry = _prop_all(
            name, p.get("display_name") or name, p.get("value"), ptype,
            readonly=bool(p.get("is_readonly")),
            locked=bool(p.get("is_locked")),
            available=bool(p.get("is_available", True)),
            visibility=p.get("visibility") or None,
        )
        if ptype in ("int", "float"):
            inc = p.get("increment")
            entry["min"] = p.get("min")
            entry["max"] = p.get("max")
            entry["step"] = inc if inc is not None else (1 if ptype == "int" else None)
            entry["unit"] = p.get("unit") or ""
        elif ptype == "enum":
            entry["options"] = [str(o) for o in (p.get("valid_value_set") or [])]
        return entry

    def _merged_prop(self, snake: str, json_p: dict | None) -> dict:
        """把模拟契约属性与 JSON 元数据合并成扩展形状（值取模拟实时值）。"""
        base = dict(self._props[snake])
        if snake == "device_temperature":
            base["value"] = self._temperature()
        base["locked"] = bool(json_p.get("is_locked")) if json_p else False
        base["available"] = bool(json_p.get("is_available", True)) if json_p else True
        if json_p:
            vis = json_p.get("visibility")
            if vis:
                base["visibility"] = str(vis)
        return base

    def _set_extra_prop(self, name: str, value: Any) -> Any:
        """设置“其余”属性（JSON 里未映射到 snake_case 的属性）。"""
        entry = self._ensure_extra_props()[name]
        if entry["readonly"]:
            raise CameraError(f"Property {name} is read-only", status=400)
        ptype = entry["type"]
        if ptype == "command":
            return None  # Mock 执行命令是无操作，契约 actual=null
        if ptype == "string":
            actual = str(value)
        elif ptype == "bool":
            if isinstance(value, str):
                v = value.strip().lower()
                if v in ("true", "on", "1", "yes"):
                    actual = True
                elif v in ("false", "off", "0", "no"):
                    actual = False
                else:
                    raise CameraError(f"Property {name} requires a boolean value",
                                      status=400)
            else:
                if not isinstance(value, (bool, int, float)):
                    raise CameraError(f"Property {name} requires a boolean value",
                                      status=400)
                actual = bool(value)
        elif ptype == "enum":
            options = entry.get("options", [])
            if value not in options:
                raise CameraError(f"Property {name} only supports: {', '.join(options)}",
                                  status=400)
            actual = value
        elif ptype in ("int", "float"):
            try:
                v = float(value)
            except (TypeError, ValueError):
                raise CameraError(f"Property {name} requires a numeric value", status=400)
            lo = entry.get("min")
            hi = entry.get("max")
            if lo is not None and hi is not None:
                try:
                    v = min(max(v, float(lo)), float(hi))
                except (TypeError, ValueError):
                    pass
            actual = int(round(v)) if ptype == "int" else float(v)
        else:
            raise CameraError(f"Unsupported property type: {ptype}", status=400)
        entry["value"] = actual
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
                raise CameraError("Device is not open")
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
_IC4_LOAD_LOCK = threading.Lock()

# ic4 官方 ErrorCode（api-reference.html → Error Handling → ErrorCode）到本服务
# HTTP 语义的映射；未列出的错误码回落 500。参照契约 docs/api.md §1：
# 400 参数错误 / 404 不存在 / 409 状态冲突 / 503 相机不可用 / 500 内部错误。
_IC4_CODE_STATUS = {
    3: 409,    # InvalidOperation：当前状态下不允许该操作
    4: 500,    # OutOfMemory
    5: 503,    # LibraryNotInitialized
    6: 500,    # DriverError
    7: 400,    # InvalidParamVal
    8: 500,    # ConversionNotSupported
    9: 503,    # NoData
    13: 503,   # DeviceInvalid：设备被拔出/失效
    16: 404,   # DeviceNotFound
    17: 503,   # DeviceError
    18: 400,   # Ambiguous：设备标识不唯一
    21: 400,   # ParseError
    27: 503,   # Timeout
    34: 500,   # Incomplete
    38: 409,   # SinkNotConnected
    39: 500,   # ImageTypeMismatch
    40: 409,   # SinkAlreadyAttached
    41: 409,   # SinkConnectAborted
    50: 500,   # BufferTooSmall
    52: 500,   # SinkTypeMismatch
    53: 409,   # SnapAborted
    101: 400,  # GenICamFeatureNotFound：属性不存在
    102: 409,  # GenICamDeviceError
    103: 400,  # GenICamTypeMismatch
    106: 409,  # GenICamAccessDenied：属性当前不可访问/被锁
    107: 400,  # GenICamNotImplemented
    108: 400,  # GenICamValueError：值非法/越界
    109: 500,  # GenICamChunkdataNotConnected
}


def _ic4_exc_status(exc: Exception) -> int | None:
    """从 ic4.IC4Exception.code 映射 HTTP 状态；非 ic4 异常返回 None。"""
    code = getattr(exc, "code", None)
    if code is None:
        return None
    try:
        return _IC4_CODE_STATUS.get(int(code))
    except (TypeError, ValueError):
        return None


def _device_serial(info) -> str:
    """DeviceInfo 序列号（serial 可能为空，此时回落到 unique_name）。

    依据 guide-device-enumeration.html：DeviceInfo 提供 model_name/serial/
    unique_name；serial 格式设备相关，并不保证非空。
    """
    serial = getattr(info, "serial", "")
    if serial:
        return str(serial)
    unique = getattr(info, "unique_name", "")
    return str(unique) if unique else "unknown"


def _load_ic4():
    """懒加载 imagingcontrol4；不可用时返回 (module|None, error)。

    Library.init() 只调用一次（guide-getting-started.html：初始化后才能
    DeviceEnum.devices()）；加锁防止并发首次加载时的重复 init。
    """
    global _IC4_LIB, _IC4_LIB_ERROR
    with _IC4_LOAD_LOCK:
        if _IC4_LIB is not None:
            return _IC4_LIB, None
        if _IC4_LIB_ERROR is not None:
            return None, _IC4_LIB_ERROR
        try:
            import imagingcontrol4 as ic4  # type: ignore
        except Exception as exc:  # noqa: BLE001
            _IC4_LIB_ERROR = (
                "Unable to load imagingcontrol4 on this platform. Install the "
                "matching ic4 driver (GenTL producer) and run pip install "
                "imagingcontrol4 (on Windows/Linux choose the wheel matching "
                "the host architecture). Original error: "
                f"{exc.__class__.__name__}: {exc}")
            return None, _IC4_LIB_ERROR
        try:
            ic4.Library.init()
            _IC4_LIB = ic4
            return ic4, None
        except Exception as exc:  # noqa: BLE001
            _IC4_LIB_ERROR = f"imagingcontrol4 initialization failed: {exc}"
            return None, _IC4_LIB_ERROR


def ic4_available() -> bool:
    lib, _ = _load_ic4()
    return lib is not None


def enumerate_ic4_devices() -> list[dict]:
    """枚举 ic4 设备（失败返回空列表，不影响 mock 路径）。"""
    lib, _ = _load_ic4()
    if lib is None:
        return []
    try:
        infos = list(lib.DeviceEnum.devices())
    except Exception:  # noqa: BLE001
        return []
    out = []
    for info in infos:
        serial = _device_serial(info)
        name = getattr(info, "model_name", "") or "IC4 Camera"
        out.append({"id": f"ic4-{serial}", "name": str(name),
                    "serial": str(serial), "kind": "ic4", "connected": False})
    return out


def _make_queue_sink(lib):
    """按官方 API 构造 QueueSink。

    api-reference.html → Sinks → QueueSink：
    - 第一个位置参数必须是 QueueSinkListener 实例（当前实现误传 PixelFormat）；
    - sink_connected 返回 True 时，sink 自动分配流所需的最小缓冲数；
    - accepted_pixel_formats=[Mono16] 让 ic4 在必要时插入 12-bit→Mono16 的
      自动转换（numpy_wrap/numpy_copy 仅支持 Mono8/Mono16 等格式，不支持
      Mono12p/Mono12Packed）；
    - max_output_buffers 限制输出队列，防止抓取线程跟不上时缓冲无限堆积。
    """
    class _Listener(lib.QueueSinkListener):
        def sink_connected(self, sink, image_type, min_buffers_required):
            # 官方示例：无需自定义缓冲分配时返回 True，sink 自动分配
            return True

        def frames_queued(self, sink):
            pass

        def sink_disconnected(self, sink):
            pass

    formats = []
    mono16 = getattr(lib.PixelFormat, "Mono16", None)
    if mono16 is not None:
        formats.append(mono16)
    return lib.QueueSink(_Listener(), accepted_pixel_formats=formats,
                         max_output_buffers=2)


class IC4Camera(BaseCamera):
    """imagingcontrol4 真机封装（与 MockCamera 同一接口）。

    ic4 Grabber 状态机（technical-grabber-states.html）：
        Initial --device_open--> Device Opened --stream_setup--> Streaming
        --ACQUISITION_START--> Acquisition Active
        stream_stop --> Device Opened；device_close --> Initial。
    本实现：open() 只 device_open；首次 grab_frame() 时 stream_setup()
    （默认 StreamSetupOption.ACQUISITION_START，官方 API 没有 stream_start）；
    close() 按 stream_stop → device_close 顺序回到 Initial。
    """

    kind = "ic4"

    # 契约属性名 → (PropId 常量名, 契约类型, 中文标签, 固定单位)
    # PropId 常量见 api-reference.html → PropId（EXPOSURE_TIME、GAIN…）；
    # 注意：官方常量是 EXPOSURE_TIME，不是 EXPOSURE。
    _SPEC = (
        ("exposure", "EXPOSURE_TIME", "float", "Exposure", "us"),
        ("gain", "GAIN", "float", "Gain", "dB"),
        ("auto_exposure", "EXPOSURE_AUTO", "bool", "Auto Exposure", ""),
        ("auto_gain", "GAIN_AUTO", "bool", "Auto Gain", ""),
        ("black_level", "BLACK_LEVEL", "int", "Black Level", "ADU"),
        ("width", "WIDTH", "int", "Width", "px"),
        ("height", "HEIGHT", "int", "Height", "px"),
        ("offset_x", "OFFSET_X", "int", "Offset X", "px"),
        ("offset_y", "OFFSET_Y", "int", "Offset Y", "px"),
        ("reverse_x", "REVERSE_X", "bool", "Reverse X", ""),
        ("reverse_y", "REVERSE_Y", "bool", "Reverse Y", ""),
        ("trigger_mode", "TRIGGER_MODE", "bool", "Trigger Mode", ""),
        ("device_temperature", "DEVICE_TEMPERATURE", "float", "Sensor Temperature", "°C"),
    )
    _SPEC_BY_NAME = {s[0]: s for s in _SPEC}
    _READONLY_NAMES = {"device_temperature"}
    GRAB_TIMEOUT_MS = 5000

    def __init__(self, cam_id: str, name: str, serial: str):
        super().__init__(cam_id, name, serial)
        self._grabber = None
        self._sink = None
        self._lib = None
        self._streaming = False
        self._lock = threading.RLock()
        self._close_requested = threading.Event()
        # 成功让设备原生输出 Mono16 后恒按 >>4 归一到低 12 位：
        # DMK 33GX249e 官方技术手册 §5.1.1.3：Mono16 像素数据在高 12 位。
        self._always_shift_mono16 = False

    @property
    def opened(self) -> bool:
        """IC4 连接状态必须反映 Grabber 的实际有效性。

        BaseCamera.opened 只看 _opened，设备被拔出后 _opened 仍为 True，
        会导致 CameraService.open 短路返回一个已经失效的 Grabber。
        """
        if not self._opened:
            return False
        grabber = self._grabber
        if grabber is None:
            return False
        try:
            return bool(grabber.is_device_open and grabber.is_device_valid)
        except Exception:  # noqa: BLE001
            return False

    # ---------------------------------------------------------------- 开关
    def open(self) -> None:
        with self._lock:
            if self._opened and self._grabber is not None:
                if self._grabber.is_device_open and self._grabber.is_device_valid:
                    return  # 幂等：防止重复 device_open
                # 设备已失效（DeviceInvalid 状态）：先释放旧 Grabber 再重开
                self._discard_grabber(self._grabber)
                self._reset_fields()
            lib, err = _load_ic4()
            if lib is None:
                raise CameraError(err or "Unable to load imagingcontrol4",
                                  status=503)
            self._close_requested.clear()
            grabber = None
            try:
                infos = list(lib.DeviceEnum.devices())
                target = None
                for info in infos:
                    if _device_serial(info) == self.serial:
                        target = info
                        break
                if target is None:
                    raise CameraError(f"No ic4 device found with serial {self.serial}",
                                      status=404)
                # device_open 支持 DeviceInfo 或字符串（api-reference → Grabber.
                # device_open(dev: DeviceInfo | str)）。用枚举到的 DeviceInfo 打开，
                # 避免字符串标识歧义（ErrorCode.Ambiguous）。
                grabber = lib.Grabber()
                grabber.device_open(target)
                if not grabber.is_device_open:
                    raise CameraError(f"ic4 device {self.name} is in an unexpected state after opening",
                                      status=503)
                sink = _make_queue_sink(lib)
                # 全部成功后才发布到实例状态，失败路径不会留下半开 Grabber
                self._always_shift_mono16 = self._try_set_native_mono16(grabber, lib)
                self._grabber = grabber
                self._sink = sink
                self._lib = lib
                self._opened = True
            except CameraError:
                self._discard_grabber(grabber)
                self._reset_fields()
                raise
            except Exception as exc:  # noqa: BLE001
                self._discard_grabber(grabber)
                self._reset_fields()
                raise CameraError(f"Failed to open ic4 device ({self.name}): {exc}",
                                  status=_ic4_exc_status(exc) or 503)

    def _reset_fields(self) -> None:
        self._grabber = None
        self._sink = None
        self._lib = None
        self._streaming = False
        self._opened = False

    def _try_set_native_mono16(self, grabber, lib) -> bool:
        """尽量让设备原生输出 Mono16；成功返回 True（高 12 位，需 >>4）。

        guide-configuring-device.html：在 Device Opened 状态通过
        device_property_map 配置；PropertyMap.try_set_value 对不存在的属性
        不抛异常、返回 False。api-reference → PropertyMap：int 参数对枚举属性
        仅在 PixelFormat 上生效，恰好适用这里。
        """
        try:
            mono16 = getattr(lib.PixelFormat, "Mono16", None)
            if mono16 is None:
                return False
            pmap = grabber.device_property_map
            if not pmap.try_set_value(lib.PropId.PIXEL_FORMAT, mono16):
                return False  # 设备不支持 Mono16 → 走 sink 的 12-bit→Mono16 转换
            try:
                fmt = pmap.get_value_str(lib.PropId.PIXEL_FORMAT)
                return fmt == "Mono16"
            except Exception:  # noqa: BLE001
                return True  # 已成功 set，回读失败仍按 Mono16 处理
        except Exception:  # noqa: BLE001
            _log.warning("ic4 PIXEL_FORMAT configuration failed, using sink auto-conversion",
                         exc_info=True)
            return False

    @staticmethod
    def _discard_grabber(grabber) -> None:
        """回收半初始化 Grabber（stream_stop → device_close）。"""
        if grabber is None:
            return
        try:
            if grabber.is_streaming:
                grabber.stream_stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            if grabber.is_device_open:
                grabber.device_close()
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        self._close_requested.set()   # 让阻塞中的 pop 循环尽快退出
        with self._lock:
            grabber = self._grabber
            self._streaming = False
            self._grabber = None
            self._sink = None
            self._lib = None
            self._opened = False
            if grabber is None:
                return
            # 状态机顺序：stream_stop（Streaming→Device Opened）→ device_close
            # （→Initial）。设备已失效时两调用可能抛错，忽略并继续清理。
            try:
                if grabber.is_streaming:
                    grabber.stream_stop()
            except Exception:  # noqa: BLE001
                _log.warning("ic4 stream_stop failed (device may be disconnected)",
                             exc_info=True)
            try:
                if grabber.is_device_open:
                    grabber.device_close()
            except Exception:  # noqa: BLE001
                _log.warning("ic4 device_close failed (device may be disconnected)",
                             exc_info=True)

    def stop_acquisition(self) -> None:
        """停止采集流但保持设备打开，解除 Width/Height 等采集区域属性的 locked 状态。"""
        with self._lock:
            grabber = self._grabber
            if grabber is None or not self._opened:
                return
            try:
                if grabber.is_streaming:
                    grabber.stream_stop()
            except Exception:  # noqa: BLE001
                _log.warning("ic4 stream_stop failed while stopping acquisition",
                             exc_info=True)
            self._streaming = False

    def _ensure_open(self):
        if not self._opened or self._grabber is None:
            raise CameraError("Device is not open", status=409)
        if not self._grabber.is_device_valid:
            raise CameraError("Camera connection lost. Reconnect and reopen the device.",
                              status=503)
        return self._grabber

    # ---------------------------------------------------------------- 帧
    def grab_frame(self) -> np.ndarray:
        with self._lock:
            grabber = self._ensure_open()
            try:
                if not grabber.is_streaming:
                    # 官方 API 无 stream_start()：stream_setup 默认
                    # StreamSetupOption.ACQUISITION_START，建流即开始采集。
                    grabber.stream_setup(self._sink)
                self._streaming = True
                buf = self._pop_with_timeout(self.GRAB_TIMEOUT_MS)
                try:
                    # numpy_copy 拷贝数据；随后立即 release 把缓冲还给 sink 的
                    # 空闲队列，避免长期占用设备内存。
                    arr = buf.numpy_copy()
                finally:
                    try:
                        buf.release()
                    except Exception:  # noqa: BLE001
                        pass
                img = np.asarray(arr, dtype=np.uint16)
                if img.ndim == 3 and img.shape[2] == 1:
                    img = img[:, :, 0]
                img = self._normalize_12bit(img)
                return np.ascontiguousarray(img, dtype=np.uint16)
            except CameraError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise CameraError(f"ic4 frame grab failed: {exc}",
                                  status=_ic4_exc_status(exc) or 503)

    def _pop_with_timeout(self, timeout_ms: int):
        """官方 pop_output_buffer() 无 timeout 参数且阻塞；用
        try_pop_output_buffer()（文档明确说明不抛异常、无帧返回 None）
        轮询实现超时语义。
        """
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            if self._close_requested.is_set():
                raise CameraError("Device is closed", status=409)
            try:
                buf = self._sink.try_pop_output_buffer()
            except Exception as exc:  # noqa: BLE001
                raise CameraError(f"Failed to read camera image: {exc}",
                                  status=_ic4_exc_status(exc) or 503)
            if buf is not None:
                return buf
            if time.monotonic() >= deadline:
                raise CameraError(
                    f"Timed out waiting for camera image ({timeout_ms}ms). "
                    "Check the camera connection or trigger mode settings.",
                    status=503)
            time.sleep(0.005)

    def _normalize_12bit(self, img: np.ndarray) -> np.ndarray:
        """把 Mono16 图像归一化为契约口径：uint16、数值 ≤4095（低 12 位）。

        依据 DMK 33GX249e 官方技术手册 §5.1.1.3：12-bit 传感器的 Mono16
        数据位于高 12 位，必须右移 4 位。设备原生 Mono16 成功时恒右移；
        sink 自动转换路径（设备不支持 Mono16 时）做最大值为依据的自适应，
        该分支未经真机验证，见 docs/reviews/ic4-doc-audit.md。
        """
        if self._always_shift_mono16:
            np.right_shift(img, 4, out=img)
            return img
        if img.size and int(img.max()) > 4095:
            np.right_shift(img, 4, out=img)
        return img

    # ---------------------------------------------------------------- 属性
    def _pmap(self):
        return self._ensure_open().device_property_map

    def _pid(self, name: str):
        lib = self._lib
        attr = self._SPEC_BY_NAME[name][1]
        return getattr(lib.PropId, attr)

    @staticmethod
    def _range_details(prop, lib):
        """读取 int/float 属性的 min/max/step（PropertyMap 没有 get_range）。

        api-reference：PropInteger/PropFloat 提供 minimum/maximum/
        increment_mode/increment/valid_value_set；Property 提供
        is_readonly/is_available/is_locked/type。
        """
        lo = float(prop.minimum)
        hi = float(prop.maximum)
        val = float(prop.value)
        step = 1.0
        try:
            mode = prop.increment_mode
        except Exception:  # noqa: BLE001
            mode = None
        if mode == lib.PropertyIncrementMode.INCREMENT:
            try:
                step = float(prop.increment)
            except Exception:  # noqa: BLE001
                step = 1.0
        elif mode == lib.PropertyIncrementMode.VALUE_SET:
            try:
                vals = sorted(float(v) for v in prop.valid_value_set)
                if len(vals) > 1:
                    step = min(b - a for a, b in zip(vals, vals[1:]))
            except Exception:  # noqa: BLE001
                step = 1.0
        elif prop.type == lib.PropertyType.FLOAT:
            # Float increment_mode==NONE：任意值合法，给出一个无害的 UI 步进
            step = max((hi - lo) / 1000.0, 1e-9)
        step = max(step, 1e-9 if prop.type == lib.PropertyType.FLOAT else 1.0)
        # 部分真机属性会把未实现的 range 暴露为 inf/nan（例如某些只读温度属性），
        # 直接进入 JSON 会变成 500：Out of range float values are not JSON compliant。
        # 这类属性无法给出可用的 UI 范围，按“当前不可用”跳过，避免拖垮整个列表。
        for label, value in (("value", val), ("minimum", lo),
                             ("maximum", hi), ("step", step)):
            if not math.isfinite(value):
                raise CameraError(
                    f"Property returned non-finite {label}: {value}", status=409)
        return val, lo, hi, step

    @staticmethod
    def _prop_unit(prop) -> str:
        try:
            return str(prop.unit or "")
        except Exception:  # noqa: BLE001
            return ""

    def _read_prop(self, pmap, name: str) -> dict:
        _, _, ptype, label, unit = self._SPEC_BY_NAME[name]
        lib = self._lib
        pid = self._pid(name)
        prop = pmap.find(pid)
        if not prop.is_available:
            raise CameraError(f"Property {name} is currently unavailable", status=409)
        readonly = bool(prop.is_readonly)
        if ptype == "bool":
            if name in ("auto_exposure", "auto_gain"):
                # ExposureAuto/GainAuto are GenICam enums (Continuous/Off).
                # Read them as strings so Continuous maps to True and Off to False;
                # fall back to the bool reader for cameras that expose a real bool.
                try:
                    s = str(pmap.get_value_str(pid)).strip().lower()
                    value = s in ("continuous", "on", "true", "1")
                except Exception:  # noqa: BLE001
                    value = pmap.get_value_bool(pid)
                return _prop(name, label, bool(value), "bool", readonly=readonly)
            try:
                value = pmap.get_value_bool(pid)
            except Exception:  # noqa: BLE001
                # TriggerMode 在部分相机上是枚举（On/Off），按字符串归一化
                s = str(pmap.get_value_str(pid))
                value = s.strip().lower() in ("on", "true", "1")
            return _prop(name, label, bool(value), "bool", readonly=readonly)
        val, lo, hi, step = self._range_details(prop, lib)
        if ptype == "int":
            actual = int(round(val))
        else:
            actual = float(val)
        return _prop(name, label, actual, ptype, min_=lo, max_=hi, step=step,
                     unit=unit or self._prop_unit(prop), readonly=readonly)

    def get_properties(self) -> list[dict]:
        with self._lock:
            pmap = self._pmap()
            props = []
            for name in (s[0] for s in self._SPEC):
                try:
                    props.append(self._read_prop(pmap, name))
                except Exception as exc:  # noqa: BLE001
                    # 相机不支持/暂时不可用/返回非有限值（inf/nan）的属性按契约跳过；
                    # 记录 warning 方便真机联调时定位是哪条属性被跳过。
                    _log.warning("ic4 property %s skipped: %s", name, exc)
                    continue
            return props

    # --------------------------------------------------- 全量属性（Parameters 标签页）
    def get_all_properties(self) -> list[dict]:
        """通用枚举全部相机属性并转成扩展形状（参照 scripts/dump_camera_props.py）。

        首选 ``PropertyMap.all`` / ``iter(pmap)`` 全量遍历；遍历不到时回退到
        13 个 _SPEC 契约属性（补上 locked/available/visibility）。单个属性读取
        失败只跳过并记录 warning，不拖垮整个列表。
        """
        with self._lock:
            pmap = self._pmap()
            lib = self._lib
            prop_objs = self._enumerate_all_props(pmap)
            if not prop_objs:
                return self._spec_extended_props(pmap)
            out: list[dict] = []
            for prop in prop_objs:
                try:
                    entry = self._read_all_prop(pmap, prop, lib)
                    if entry is not None:
                        out.append(entry)
                except Exception as exc:  # noqa: BLE001
                    _log.warning("ic4 property %s skipped: %s",
                                 getattr(prop, "name", prop), exc)
                    continue
            return out

    @staticmethod
    def _enumerate_all_props(pmap) -> list:
        """尽量返回 PropertyMap 里的全部属性对象（与 dump 脚本同思路）。"""
        all_attr = getattr(pmap, "all", None)
        if all_attr is not None:
            try:
                props = list(all_attr() if callable(all_attr) else all_attr)
                if props:
                    return props
            except Exception:  # noqa: BLE001
                pass
        try:
            props = list(iter(pmap))
            if props:
                return props
        except Exception:  # noqa: BLE001
            pass
        return []

    @staticmethod
    def _all_type(prop, lib) -> str:
        """按类型判别属性（先判更具体的 Enum/Command/Boolean/String 再判数值）。"""
        for cls_name, out in (("PropEnumeration", "enum"),
                              ("PropCommand", "command"),
                              ("PropBoolean", "bool"),
                              ("PropString", "string"),
                              ("PropFloat", "float"),
                              ("PropInteger", "int")):
            cls = getattr(lib, cls_name, None)
            if cls is not None and isinstance(prop, cls):
                return out
        tname = str(getattr(prop, "type", "")).upper()
        return _GENICAM_TYPE_MAP.get(tname, "string")

    @staticmethod
    def _visibility_text(prop) -> str | None:
        try:
            vis = prop.visibility
        except Exception:  # noqa: BLE001
            return None
        if vis is None:
            return None
        if isinstance(vis, str):
            return vis
        name = getattr(vis, "name", None)
        return str(name if name is not None else vis)

    @staticmethod
    def _finite_or_none(value):
        """转 JSON 安全的数值；非有限/不可转换返回 None。"""
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(f):
            return None
        if isinstance(value, int) or (isinstance(value, float) and f.is_integer()):
            return int(f)
        return f

    @staticmethod
    def _read_all_value(prop, pmap, ptype: str):
        """按类型读取单个属性当前值（command 恒返回 None）。"""
        if ptype == "command":
            return None
        raw = None
        try:
            raw = prop.value
        except Exception:  # noqa: BLE001
            raw = None
        if raw is None:
            name = getattr(prop, "name", None)
            if name:
                try:
                    raw = pmap.get_value_str(name)
                except Exception:  # noqa: BLE001
                    raw = None
        if raw is None:
            return None  # 读不到值 → value=null，不要误报 False/"None"
        if ptype == "bool":
            if isinstance(raw, str):
                return raw.strip().lower() in ("true", "on", "1", "yes")
            return bool(raw)
        if ptype == "int":
            try:
                return int(round(float(raw)))
            except (TypeError, ValueError):
                return None
        if ptype == "float":
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None
        if ptype == "enum":
            n = getattr(raw, "name", None)
            return str(n if n is not None else raw)
        if ptype == "string":
            return str(raw)
        return None

    def _read_all_prop(self, pmap, prop, lib) -> dict | None:
        """把一个 IC4 属性对象转成扩展形状；读不出名字/类型则跳过。"""
        try:
            name = str(prop.name or "")
        except Exception:  # noqa: BLE001
            return None
        if not name:
            return None
        ptype = self._all_type(prop, lib)
        label = str(getattr(prop, "display_name", "") or name)
        entry = _prop_all(
            name, label, None, ptype,
            readonly=bool(getattr(prop, "is_readonly", False)),
            locked=bool(getattr(prop, "is_locked", False)),
            available=bool(getattr(prop, "is_available", True)),
            visibility=self._visibility_text(prop),
        )
        if ptype in ("int", "float"):
            entry["min"] = self._finite_or_none(getattr(prop, "minimum", None))
            entry["max"] = self._finite_or_none(getattr(prop, "maximum", None))
            entry["unit"] = self._prop_unit(prop)
            step = 1.0
            try:
                mode = prop.increment_mode
            except Exception:  # noqa: BLE001
                mode = None
            inc_enum = getattr(lib, "PropertyIncrementMode", None)
            if inc_enum is not None and mode == inc_enum.INCREMENT:
                step = self._finite_or_none(getattr(prop, "increment", None)) or 1.0
            elif ptype == "float":
                step = None
            entry["step"] = step
        elif ptype == "enum":
            options: list[str] = []
            try:
                for e in prop.entries:
                    options.append(str(getattr(e, "name", e)))
            except Exception:  # noqa: BLE001
                options = []
            entry["options"] = options
        if entry["available"]:
            entry["value"] = self._read_all_value(prop, pmap, ptype)
        return entry

    def _spec_extended_props(self, pmap) -> list[dict]:
        """全量遍历不可用时的回退：13 个契约属性 + 扩展元数据。"""
        out: list[dict] = []
        for name in (s[0] for s in self._SPEC):
            try:
                base = self._read_prop(pmap, name)
                prop = pmap.find(self._pid(name))
                base.setdefault("available", bool(getattr(prop, "is_available", True)))
                base.setdefault("locked", bool(getattr(prop, "is_locked", False)))
                vis = self._visibility_text(prop)
                if vis:
                    base["visibility"] = vis
                out.append(base)
            except Exception as exc:  # noqa: BLE001
                _log.warning("ic4 property %s skipped: %s", name, exc)
                continue
        return out

    def get_property(self, name: str) -> Any:
        with self._lock:
            if name not in self._SPEC_BY_NAME:
                return self._get_any_prop(name)
            try:
                return self._read_prop(self._pmap(), name)["value"]
            except CameraError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise CameraError(f"Failed to read property {name}: {exc}",
                                  status=_ic4_exc_status(exc) or 400)

    def _coerce_value(self, prop, name: str, value: Any, ptype: str):
        lib = self._lib
        if ptype == "bool":
            if isinstance(value, str):
                v = value.strip().lower()
                if v in ("true", "on", "1", "yes"):
                    return True
                if v in ("false", "off", "0", "no"):
                    return False
                raise CameraError(f"Property {name} requires a boolean value", status=400)
            if not isinstance(value, (bool, int, float)):
                raise CameraError(f"Property {name} requires a boolean value", status=400)
            return bool(value)
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise CameraError(f"Property {name} requires a numeric value", status=400)
        val, lo, hi, _ = self._range_details(prop, lib)
        v = min(max(v, lo), hi)              # 契约：越界值自动钳制
        try:
            mode = prop.increment_mode
        except Exception:  # noqa: BLE001
            mode = None
        if mode == lib.PropertyIncrementMode.VALUE_SET:
            try:
                valid = sorted(float(x) for x in prop.valid_value_set)
                v = min(valid, key=lambda x: abs(x - v))
            except Exception:  # noqa: BLE001
                pass
        elif mode == lib.PropertyIncrementMode.INCREMENT:
            try:
                inc = float(prop.increment)
                if inc > 0:
                    v = lo + round((v - lo) / inc) * inc
            except Exception:  # noqa: BLE001
                pass
        v = min(max(v, lo), hi)
        return int(round(v)) if ptype == "int" else float(v)

    def set_property(self, name: str, value: Any) -> Any:
        with self._lock:
            grabber = self._ensure_open()
            if name not in self._SPEC_BY_NAME:
                return self._set_any_prop(grabber, name, value)
            if name in self._READONLY_NAMES:
                raise CameraError(f"Property {name} is read-only", status=400)
            _, _, ptype, _label, _unit = self._SPEC_BY_NAME[name]
            lib = self._lib
            pid = self._pid(name)
            pmap = grabber.device_property_map
            try:
                prop = pmap.find(pid)
                if not prop.is_available:
                    raise CameraError(f"Property {name} is currently unavailable", status=409)
                if prop.is_readonly:
                    raise CameraError(f"Property {name} is read-only", status=400)
                if prop.is_locked:
                    raise CameraError(
                        f"Property {name} is locked. Stop acquisition or disable auto control first.",
                        status=409)
                actual_arg = self._coerce_value(prop, name, value, ptype)
                if ptype == "bool":
                    if name in ("auto_exposure", "auto_gain"):
                        # ExposureAuto/GainAuto are GenICam enums: use the exact
                        # Continuous/Off strings instead of a Python bool.
                        pmap.set_value(pid, "Continuous" if actual_arg else "Off")
                    else:
                        try:
                            pmap.set_value(pid, actual_arg)
                        except Exception:
                            # 部分相机的 bool 契约属性底层是枚举（如 TriggerMode=On/Off），
                            # 直接用 Python bool 会类型不匹配；回退到常见枚举字符串。
                            candidates = (["On", "True", "1"] if actual_arg
                                          else ["Off", "False", "0"])
                            for cand in candidates:
                                try:
                                    pmap.set_value(pid, cand)
                                    break
                                except Exception:
                                    continue
                            else:
                                raise CameraError(
                                    f"Failed to set property {name}: no matching enum value "
                                    f"for boolean {actual_arg}", status=400)
                else:
                    pmap.set_value(pid, actual_arg)
            except CameraError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise CameraError(f"Failed to set property {name}: {exc}",
                                  status=_ic4_exc_status(exc) or 400)
            try:
                return self._read_prop(pmap, name)["value"]
            except CameraError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise CameraError(f"Failed to read back property {name} after setting it: {exc}",
                                  status=_ic4_exc_status(exc) or 500)

    # ------------------------------------------- 任意属性名 / 任意类型的读写
    def _find_prop_by_name(self, pmap, name: str):
        """按名字找属性对象：先全量遍历匹配，再退回 PropId 常量。"""
        for prop in self._enumerate_all_props(pmap):
            try:
                if str(prop.name) == name:
                    return prop
            except Exception:  # noqa: BLE001
                continue
        try:
            pid = getattr(self._lib.PropId, name, None)
            if pid is not None:
                return pmap.find(pid)
        except Exception:  # noqa: BLE001
            pass
        return None

    def _get_any_prop(self, name: str) -> Any:
        pmap = self._pmap()
        prop = self._find_prop_by_name(pmap, name)
        if prop is None:
            raise CameraError(f"Unknown property: {name}", status=400)
        if not bool(getattr(prop, "is_available", True)):
            raise CameraError(f"Property {name} is currently unavailable", status=409)
        return self._read_all_value(prop, pmap, self._all_type(prop, self._lib))

    def _coerce_any_value(self, prop, name: str, value: Any, ptype: str):
        lib = self._lib
        if ptype == "bool":
            if isinstance(value, str):
                v = value.strip().lower()
                if v in ("true", "on", "1", "yes"):
                    return True
                if v in ("false", "off", "0", "no"):
                    return False
                raise CameraError(f"Property {name} requires a boolean value", status=400)
            if not isinstance(value, (bool, int, float)):
                raise CameraError(f"Property {name} requires a boolean value", status=400)
            return bool(value)
        if ptype == "string":
            return str(value)
        if ptype == "enum":
            options: list[str] = []
            try:
                options = [str(getattr(e, "name", e)) for e in prop.entries]
            except Exception:  # noqa: BLE001
                options = []
            sval = str(value)
            if options and sval not in options:
                raise CameraError(
                    f"Property {name} only supports: {', '.join(options)}", status=400)
            return sval
        if ptype in ("int", "float"):
            try:
                v = float(value)
            except (TypeError, ValueError):
                raise CameraError(f"Property {name} requires a numeric value", status=400)
            try:
                lo = float(prop.minimum)
            except Exception:  # noqa: BLE001
                lo = v
            try:
                hi = float(prop.maximum)
            except Exception:  # noqa: BLE001
                hi = v
            if math.isfinite(lo) and math.isfinite(hi) and lo < hi:
                v = min(max(v, lo), hi)
            inc_enum = getattr(lib, "PropertyIncrementMode", None)
            try:
                mode = prop.increment_mode
            except Exception:  # noqa: BLE001
                mode = None
            if inc_enum is not None and mode == inc_enum.INCREMENT:
                try:
                    inc = float(prop.increment)
                    if inc > 0 and math.isfinite(lo) and math.isfinite(hi):
                        v = lo + round((v - lo) / inc) * inc
                except Exception:  # noqa: BLE001
                    pass
            if math.isfinite(lo) and math.isfinite(hi) and lo < hi:
                v = min(max(v, lo), hi)
            return int(round(v)) if ptype == "int" else float(v)
        raise CameraError(f"Unsupported property type: {ptype}", status=400)

    def _set_any_prop(self, grabber, name: str, value: Any) -> Any:
        lib = self._lib
        pmap = grabber.device_property_map
        prop = self._find_prop_by_name(pmap, name)
        if prop is None:
            raise CameraError(f"Unknown property: {name}", status=400)
        if not bool(getattr(prop, "is_available", True)):
            raise CameraError(f"Property {name} is currently unavailable", status=409)
        if bool(getattr(prop, "is_readonly", False)):
            raise CameraError(f"Property {name} is read-only", status=400)
        if bool(getattr(prop, "is_locked", False)):
            raise CameraError(
                f"Property {name} is locked. Stop acquisition or disable auto control first.",
                status=409)
        ptype = self._all_type(prop, lib)
        if ptype == "command":
            try:
                execute = getattr(prop, "execute", None)
                if callable(execute):
                    execute()
                else:
                    pmap.set_value(name, True)
            except CameraError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise CameraError(f"Failed to execute command {name}: {exc}",
                                  status=_ic4_exc_status(exc) or 400)
            return None  # command 执行后 actual=null
        actual = self._coerce_any_value(prop, name, value, ptype)
        try:
            if ptype == "bool":
                try:
                    pmap.set_value(name, actual)
                except Exception:  # noqa: BLE001
                    candidates = (["On", "True", "1"] if actual
                                  else ["Off", "False", "0"])
                    for cand in candidates:
                        try:
                            pmap.set_value(name, cand)
                            break
                        except Exception:  # noqa: BLE001
                            continue
                    else:
                        raise CameraError(
                            f"Failed to set property {name}: no matching value "
                            f"for boolean {actual}", status=400)
            else:
                pmap.set_value(name, actual)
        except CameraError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CameraError(f"Failed to set property {name}: {exc}",
                              status=_ic4_exc_status(exc) or 400)
        return self._read_all_value(prop, pmap, ptype)


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
                raise CameraError(f"Device does not exist: {cam_id}")
            return IC4Camera(found["id"], found["name"], found["serial"])
        raise CameraError(f"Device does not exist: {cam_id}")

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
                raise CameraError("Device is not open")
            return self._device

    def stop_acquisition(self) -> None:
        with self._lock:
            if self._device is None or not self._device.opened:
                return
            self._device.stop_acquisition()

    def grab_frame(self) -> np.ndarray:
        return self.ensure_device().grab_frame()

    def get_properties(self) -> list[dict]:
        return self.ensure_device().get_properties()

    def get_all_properties(self) -> list[dict]:
        return self.ensure_device().get_all_properties()

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
