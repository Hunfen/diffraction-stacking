#!/usr/bin/env python3
"""Dump all device properties of a The Imaging Source IC4 camera to JSON.

This script is intended to run on Windows with the project's backend virtualenv
where ``imagingcontrol4`` is installed::

    backend\\.venv\\Scripts\\python.exe scripts/dump_camera_props.py --pretty

It opens the selected camera with the IC4 SDK, iterates the device property map
and writes every property (name, type, current value, range, increment, enum
entries, unit, availability/read-only/locked/visibility flags, constant flag)
to a JSON file for building a full parameter panel.

The script only uses the Python standard library plus ``imagingcontrol4``.
It never modifies any existing project file.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

# ---------------------------------------------------------------------------
# Fallback list used only when the installed imagingcontrol4 PropertyMap does
# not provide a way to enumerate all properties.  It mirrors the _SPEC table in
# backend/app/camera.py; we deliberately keep a local copy so this script does
# not have to import backend code (and therefore numpy).
# ---------------------------------------------------------------------------
_FALLBACK_SPEC: tuple[tuple[str, str, str], ...] = (
    ("exposure", "EXPOSURE_TIME", "Float"),
    ("gain", "GAIN", "Float"),
    ("auto_exposure", "EXPOSURE_AUTO", "Bool"),
    ("auto_gain", "GAIN_AUTO", "Bool"),
    ("black_level", "BLACK_LEVEL", "Integer"),
    ("width", "WIDTH", "Integer"),
    ("height", "HEIGHT", "Integer"),
    ("offset_x", "OFFSET_X", "Integer"),
    ("offset_y", "OFFSET_Y", "Integer"),
    ("reverse_x", "REVERSE_X", "Bool"),
    ("reverse_y", "REVERSE_Y", "Bool"),
    ("trigger_mode", "TRIGGER_MODE", "Bool"),
    ("device_temperature", "DEVICE_TEMPERATURE", "Float"),
)

_IMPORT_ERROR_MESSAGE = (
    "请用 backend\\.venv\\Scripts\\python.exe 运行（需要 imagingcontrol4）"
)

# Mapping from IC4 PropertyType member names / strings to the output type names
# requested by the task.
_TYPE_MAP = {
    "INTEGER": "Integer",
    "FLOAT": "Float",
    "ENUMERATION": "Enum",
    "BOOLEAN": "Bool",
    "STRING": "String",
    "COMMAND": "Command",
    "CATEGORY": "Unknown",
    "REGISTER": "Unknown",
    "ENUMENTRY": "Unknown",
    "PORT": "Unknown",
}


def _exc_text(exc: Exception) -> str:
    """Short human-readable exception text for the JSON ``error`` field."""
    return f"{exc.__class__.__name__}: {exc}"


def _is_backend_running(timeout: float = 1.5) -> bool:
    """Best-effort stdlib check for a Static AstroPhoto backend on port 8000.

    Returns True only when ``http://127.0.0.1:8000/api/health`` answers with
    HTTP 200.  Uses only :mod:`urllib.request` and never raises: any network
    error simply means "no backend detected".
    """
    try:
        import urllib.request

        with urllib.request.urlopen(
            "http://127.0.0.1:8000/api/health", timeout=timeout
        ) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 - best-effort probe, never fail the dump
        return False


def _is_open_permission_error(exc: Exception) -> bool:
    """Detect the ic4 "permission denied" device-open failure.

    Recognises the structured error (``ErrorCode.DriverError`` with value 6)
    as well as the message text ("permission denied" / "failed to open device"),
    so detection keeps working no matter how the installed imagingcontrol4
    build surfaces the error.
    """
    code = getattr(exc, "error_code", None)
    if code is None:
        code = getattr(exc, "code", None)
    if code is not None:
        name = getattr(code, "name", None)
        try:
            value = int(code)
        except (TypeError, ValueError):
            value = None
        if name == "DriverError" or value == 6:
            return True
    text = str(exc).lower()
    return "permission denied" in text or "failed to open device" in text


_OPEN_PERMISSION_HELP = (
    "无法打开相机：权限被拒绝（ic4 DriverError / permission denied）。\n"
    "可能的原因与处理步骤：\n"
    "  1. 相机已被其他程序独占打开，最常见的是本项目的 Static AstroPhoto "
    "后端仍在运行（后端启动时会独占相机）。\n"
    "     请先执行 .\\scripts\\run.ps1 -Stop（或关闭占用相机的其他程序），"
    "再重试本脚本。\n"
    "  2. ic4 驱动/GenTL producer 未安装，或 GigE 网卡未绑定（请启用巨型帧 "
    "jumbo frames，并确认网卡已绑定 ic4 GigE producer）。\n"
    "  3. 当前进程没有访问该设备的权限（相机可能被其他用户/会话占用）。\n"
    "  4. 若本机有多个相机，可用 --device-index N 选择其他设备重试。\n"
)


def _print_open_permission_help(exc: Exception) -> None:
    """Print actionable guidance for a permission-denied open, then the raw error."""
    print(_OPEN_PERMISSION_HELP, file=sys.stderr)
    print(f"原始异常: {exc.__class__.__name__}: {exc!r}", file=sys.stderr)


def _call_or_value(value: Any) -> Any:
    """Call ``value`` if it is a callable, otherwise return it unchanged."""
    return value() if callable(value) else value


def _get_attr(obj: Any, *names: str, default: Any = None) -> Any:
    """Return the first existing attribute, supporting callable attributes."""
    for name in names:
        if hasattr(obj, name):
            try:
                val = getattr(obj, name)
            except Exception:
                continue
            return _call_or_value(val)
    return default


def _finite_number(value: Any) -> float | int | None:
    """Return a JSON-safe number, or None for non-finite/invalid values."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    # Keep integers as integers when the source value is integral.
    if isinstance(value, int) or (isinstance(value, float) and f.is_integer()):
        return int(f)
    return f


def _safe_set(out: dict[str, Any], key: str, getter: Callable[[], Any],
              transform: Callable[[Any], Any] | None = None) -> None:
    """Best-effort field read: on failure leave null and record first error."""
    try:
        value = getter()
        if transform is not None:
            value = transform(value)
        if value is not None:
            out[key] = value
    except Exception as exc:  # noqa: BLE001 - individual property must not kill dump
        if out.get("error") is None:
            out["error"] = _exc_text(exc)


def _type_name(prop: Any, ic4: Any) -> str:
    """Return the requested output type string for an IC4 property object."""
    for cls_name, out_name in (
        ("PropInteger", "Integer"),
        ("PropFloat", "Float"),
        ("PropEnumeration", "Enum"),
        ("PropBoolean", "Bool"),
        ("PropString", "String"),
        ("PropCommand", "Command"),
    ):
        cls = getattr(ic4, cls_name, None)
        if cls is not None and isinstance(prop, cls):
            return out_name

    prop_type = getattr(prop, "type", None)
    if prop_type is not None:
        name = getattr(prop_type, "name", None)
        if name is None:
            name = str(prop_type).split(".")[-1]
        name = str(name).upper()
        return _TYPE_MAP.get(name, name if name in _TYPE_MAP.values() else "Unknown")
    return "Unknown"


def _visibility_text(prop: Any) -> str | None:
    """Return visibility as a simple string if the SDK exposes it."""
    value = _get_attr(prop, "visibility")
    if value is None:
        return None
    if isinstance(value, str):
        return value
    name = getattr(value, "name", None)
    return str(name if name is not None else value)


def _enum_options(prop: Any, ic4: Any) -> list[str] | None:
    """Return the list of valid enum entry names, or None for non-enums."""
    if not _type_name(prop, ic4) == "Enum":
        return None
    entries = _get_attr(prop, "entries", default=None)
    if entries is None:
        return None
    options: list[str] = []
    for entry in entries:
        name = _get_attr(entry, "name", "value", default=None)
        if name is None:
            continue
        options.append(str(name))
    return options or None


def _is_constant(prop: Any, ic4: Any, out: dict[str, Any]) -> bool:
    """Heuristic: is this parameter effectively constant/unchangeable?"""
    ptype = out.get("type")
    readonly = out.get("is_readonly") is True
    value_set = out.get("valid_value_set")

    if ptype in ("Integer", "Float"):
        lo, hi = out.get("min"), out.get("max")
        if lo is not None and hi is not None:
            try:
                return float(lo) == float(hi)
            except (TypeError, ValueError):
                pass

    if ptype in ("Bool", "String"):
        # A readonly scalar without a range/value-set is treated as constant.
        return readonly

    if ptype == "Enum":
        if value_set is not None:
            return readonly and len(value_set) == 1
        return readonly

    return False


def _extract_property(prop: Any, pmap: Any, ic4: Any) -> dict[str, Any]:
    """Extract every requested field from one IC4 property object."""
    out: dict[str, Any] = {
        "name": None,
        "display_name": None,
        "type": "Unknown",
        "value": None,
        "min": None,
        "max": None,
        "increment": None,
        "valid_value_set": None,
        "unit": None,
        "is_available": None,
        "is_readonly": None,
        "is_locked": None,
        "visibility": None,
        "is_constant": False,
        "error": None,
    }

    def set_name() -> None:
        out["name"] = str(_get_attr(prop, "name", default=""))

    def set_display_name() -> None:
        display = _get_attr(prop, "display_name", "displayName")
        out["display_name"] = str(display) if display is not None else None

    def set_type() -> None:
        out["type"] = _type_name(prop, ic4)

    def set_flags() -> None:
        out["is_available"] = _get_attr(prop, "is_available")
        out["is_readonly"] = _get_attr(prop, "is_readonly")
        out["is_locked"] = _get_attr(prop, "is_locked")
        out["visibility"] = _visibility_text(prop)

    def set_value() -> None:
        if out.get("is_available") is False:
            out["value"] = None
            return
        ptype = out.get("type")
        if ptype == "Command" or ptype == "Unknown":
            out["value"] = None
            return

        raw = _get_attr(prop, "value")
        if raw is None:
            # Some SDK builds expose values only through the PropertyMap.
            # Get the bound method without calling it, then call with prop.name.
            get_value_str = getattr(pmap, "get_value_str", None)
            if callable(get_value_str):
                raw = get_value_str(prop.name)
            else:
                out["value"] = None
                return

        if ptype in ("Integer", "Float"):
            out["value"] = _finite_number(raw)
        elif ptype == "Bool":
            out["value"] = bool(raw)
        elif ptype == "Enum":
            out["value"] = str(raw)
        else:
            out["value"] = raw if isinstance(raw, (str, int, float, bool)) else str(raw)

    def set_range() -> None:
        ptype = out.get("type")
        if ptype not in ("Integer", "Float"):
            return
        out["min"] = _finite_number(_get_attr(prop, "minimum"))
        out["max"] = _finite_number(_get_attr(prop, "maximum"))
        mode = _get_attr(prop, "increment_mode")
        if mode is not None:
            mode_name = getattr(mode, "name", None) or str(mode).split(".")[-1]
            if "INCREMENT" in str(mode_name).upper():
                out["increment"] = _finite_number(_get_attr(prop, "increment"))

    def set_enum_options() -> None:
        out["valid_value_set"] = _enum_options(prop, ic4)

    def set_unit() -> None:
        unit = _get_attr(prop, "unit")
        out["unit"] = str(unit) if unit is not None else None

    def set_constant() -> None:
        out["is_constant"] = _is_constant(prop, ic4, out)

    # Each read is independent; a failure on one field must not discard the
    # whole property.  The first error is preserved in out["error"].
    _safe_set(out, "name", set_name)
    _safe_set(out, "display_name", set_display_name)
    _safe_set(out, "type", set_type)
    _safe_set(out, "is_available", set_flags, transform=lambda _: None)
    _safe_set(out, "is_readonly", set_flags, transform=lambda _: None)
    _safe_set(out, "is_locked", set_flags, transform=lambda _: None)
    _safe_set(out, "visibility", set_flags, transform=lambda _: None)
    _safe_set(out, "value", set_value)
    _safe_set(out, "min", set_range, transform=lambda _: None)
    _safe_set(out, "max", set_range, transform=lambda _: None)
    _safe_set(out, "increment", set_range, transform=lambda _: None)
    _safe_set(out, "valid_value_set", set_enum_options)
    _safe_set(out, "unit", set_unit)
    _safe_set(out, "is_constant", set_constant)

    # Normalise null fields to JSON null (already the initial values).
    return out


def _enumerate_all_properties(pmap: Any, ic4: Any) -> list[Any] | None:
    """Try every known way to enumerate all properties of a PropertyMap.

    imagingcontrol4 exposes ``PropertyMap.all`` (the official ic4-ctrl example
    iterates ``grabber.device_property_map.all``).  We also try ``iter(pmap)``
    and index-based access as fallbacks for other 1.5.x API variants.

    Returns a list of property objects, or None when no full enumeration API is
    available (the caller then falls back to ``_FALLBACK_SPEC``).
    """
    # 1) Official documented way: pmap.all (list or method).
    all_attr = getattr(pmap, "all", None)
    if all_attr is not None:
        try:
            props = list(_call_or_value(all_attr))
            if props:
                return props
        except Exception:  # noqa: BLE001
            pass

    # 2) Iteration protocol.
    try:
        props = list(iter(pmap))
        if props:
            return props
    except Exception:  # noqa: BLE001
        pass

    # 3) Index access using count/len when available.
    try:
        count = len(pmap)
        props = [pmap[i] for i in range(count)]
        if props:
            return props
    except Exception:  # noqa: BLE001
        pass

    return None


def _fallback_property_objects(pmap: Any, ic4: Any) -> list[Any]:
    """Use the known _SPEC PropIds when full enumeration is unsupported."""
    props: list[Any] = []
    for name, prop_id_name, _ in _FALLBACK_SPEC:
        try:
            pid = getattr(ic4.PropId, prop_id_name)
            prop = pmap.find(pid)
            if prop is not None:
                props.append(prop)
        except Exception as exc:  # noqa: BLE001
            # Keep a synthetic entry so the dump still mentions the property.
            props.append(_SyntheticProperty(name, exc))
    return props


class _SyntheticProperty:
    """Minimal stand-in for a property that could not be found/read."""

    def __init__(self, name: str, error: Exception):
        self._name = name
        self._error = error

    @property
    def name(self) -> str:
        return self._name


def _enumerate_devices(ic4: Any) -> list[Any]:
    """Return device infos, compatible with both static and instance APIs."""
    try:
        return list(ic4.DeviceEnum.devices())
    except TypeError:
        return list(ic4.DeviceEnum().devices())


def _device_info(dev: Any) -> dict[str, str]:
    model = _get_attr(dev, "model_name", "name", default="")
    serial = _get_attr(dev, "serial", default="")
    unique = _get_attr(dev, "unique_name", default="")
    return {
        "name": str(model) if model else "IC4 Camera",
        "model": str(model) if model else "unknown",
        "serial": str(serial) if serial else (str(unique) if unique else "unknown"),
    }


def _range_text(p: dict[str, Any]) -> str:
    if p["type"] in ("Integer", "Float") and (p["min"] is not None or p["max"] is not None):
        return f"range=[{p['min']}..{p['max']}]"
    if p["type"] == "Enum" and p["valid_value_set"]:
        return f"options={p['valid_value_set']}"
    return ""


def _print_summary(properties: Sequence[dict[str, Any]], pretty: bool,
                   quiet: bool) -> None:
    if quiet:
        return
    print(f"属性总数: {len(properties)}")
    for p in properties:
        name = p.get("name") or "<unknown>"
        line = f"  {name}: type={p.get('type')} value={p.get('value')} {_range_text(p)}"
        if pretty:
            extra: list[str] = []
            if p.get("unit") is not None:
                extra.append(f"unit={p['unit']}")
            if p.get("valid_value_set") is not None:
                extra.append(f"valid_value_set={p['valid_value_set']}")
            if p.get("visibility") is not None:
                extra.append(f"visibility={p['visibility']}")
            extra.append(f"readonly={p.get('is_readonly')}")
            extra.append(f"locked={p.get('is_locked')}")
            extra.append(f"available={p.get('is_available')}")
            extra.append(f"constant={p.get('is_constant')}")
            if p.get("error"):
                extra.append(f"error={p['error']}")
            if extra:
                line += "  [" + ", ".join(extra) + "]"
        print(line)


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dump all IC4 camera device properties to JSON.")
    default_output = Path(__file__).resolve().parent.parent / "camera_props_dump.json"
    parser.add_argument("--output", type=Path, default=default_output,
                        help="Output JSON path (default: repo root camera_props_dump.json)")
    parser.add_argument("--device-index", type=int, default=0,
                        help="Zero-based device index to open (default: 0)")
    parser.add_argument("--pretty", action="store_true",
                        help="Print a more detailed property summary to the console")
    parser.add_argument("--quiet", action="store_true",
                        help="Do not print the per-property console summary")
    args = parser.parse_args(argv)

    # ------------------------------------------------------------------ SDK
    try:
        import imagingcontrol4 as ic4  # type: ignore
    except Exception as exc:  # noqa: BLE001
        print(_IMPORT_ERROR_MESSAGE, file=sys.stderr)
        print(f"import imagingcontrol4 failed: {exc}", file=sys.stderr)
        return 2

    try:
        init = getattr(ic4, "Library", None)
        if init is not None:
            init.init()
    except Exception as exc:  # noqa: BLE001
        print(f"imagingcontrol4 Library.init() failed: {exc}", file=sys.stderr)
        return 1

    # --------------------------------------------------------------- Devices
    try:
        devices = _enumerate_devices(ic4)
    except Exception as exc:  # noqa: BLE001
        print(f"Device enumeration failed: {exc}", file=sys.stderr)
        return 1

    if not devices:
        print("未找到 IC4 相机设备。请检查相机连接、GenTL producer/驱动绑定。",
              file=sys.stderr)
        return 1

    if args.device_index < 0 or args.device_index >= len(devices):
        print(f"--device-index {args.device_index} 越界，可用设备数: {len(devices)}",
              file=sys.stderr)
        return 1

    dev = devices[args.device_index]
    info = _device_info(dev)
    print(f"设备: {info['name']} / 型号: {info['model']} / 序列号: {info['serial']}")

    # ----------------------------------------------------------------- Open
    grabber = None
    try:
        if _is_backend_running():
            print(
                "检测到本机 8000 端口已有 Static AstroPhoto 后端运行，它会独占相机；"
                "建议先执行 .\\scripts\\run.ps1 -Stop 再重试。",
                file=sys.stderr,
            )

        grabber = ic4.Grabber()
        try:
            grabber.device_open(dev)
        except Exception as exc:  # noqa: BLE001 - turn the common failure into guidance
            if _is_open_permission_error(exc):
                _print_open_permission_help(exc)
                return 1
            raise
        pmap = grabber.device_property_map

        # ----------------------------------------------------- Enumerate
        all_props = _enumerate_all_properties(pmap, ic4)
        if all_props is not None:
            enumeration_mode = "all"
            prop_objects = all_props
        else:
            enumeration_mode = "spec-only"
            print("PropertyMap 未提供全量遍历 API，使用 backend/app/camera.py 的 _SPEC 兜底。",
                  file=sys.stderr)
            prop_objects = _fallback_property_objects(pmap, ic4)

        properties = [_extract_property(p, pmap, ic4) for p in prop_objects]

        # Remove synthetic entries that were only placeholders for missing
        # fallback properties (they still appear with error text if retained).
        # We retain them: they document that the lookup itself failed.
        result: dict[str, Any] = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "device": info,
            "enumeration": enumeration_mode,
            "properties": properties,
        }

        output_path = Path(args.output)
        if output_path.parent and not output_path.parent.exists():
            output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"JSON 已保存: {output_path.resolve()}")

        _print_summary(properties, args.pretty, args.quiet)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"抓取相机属性失败: {exc}", file=sys.stderr)
        return 1
    finally:
        if grabber is not None:
            try:
                if grabber.is_device_open:
                    grabber.device_close()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    raise SystemExit(_main())
