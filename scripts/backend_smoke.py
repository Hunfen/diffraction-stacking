#!/usr/bin/env python3
"""End-to-end self test (Mock camera path).

Usage: cd backend && .venv/bin/python ../scripts/backend_smoke.py
Exit code: 0 = all passed; 1 = some checks failed.
"""
from __future__ import annotations

import asyncio
import http.client
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

import numpy as np

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
BASE = None
PORT = None
PROC = None
DATA_ROOT = None
RESULTS = []
SESSION_ID = None
EFFECTIVE_SESSION_NAME = None
# Sanitized suffix matching the raw user input used in step_capture (v1.2).
SANITIZED_SESSION_SUFFIX = "SMOKE_LEED_001_a_b_x"


def check(cond, msg):
    RESULTS.append((bool(cond), msg))
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        sys.stdout.flush()


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http_req(method, path, body=None, expect=None, timeout=30):
    url = BASE + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read()
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                payload = raw
            if expect is not None:
                check(status == expect,
                      f"{method} {path} -> HTTP {status} (expected {expect})")
            return status, payload
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            payload = raw
        if expect is not None:
            check(e.code == expect,
                  f"{method} {path} -> HTTP {e.code} (expected {expect})")
        return e.code, payload


def raw_request(path: str) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=15)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, body


def raw_response(path: str) -> tuple[int, bytes, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=15)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    headers = {k.lower(): v for k, v in resp.getheaders()}
    conn.close()
    return resp.status, body, headers


def wait_server(timeout=40):
    global PROC
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            status, payload = http_req("GET", "/api/health", expect=None, timeout=3)
            if status == 200 and payload.get("ok"):
                return
        except Exception as exc:  # noqa: BLE001
            last = exc
        if PROC is not None and PROC.poll() is not None:
            out = PROC.stdout.read().decode("utf-8", "replace") if PROC.stdout else ""
            raise RuntimeError(f"uvicorn exited early (code={PROC.returncode}):\n{out}")
        time.sleep(0.3)
    raise RuntimeError(f"Server not ready within {timeout}s: {last}")


def poll_state(cond, timeout=60, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, state = http_req("GET", "/api/capture/state")
        if state.get("ok") and cond(state["data"]):
            return state["data"]
        time.sleep(interval)
    _, state = http_req("GET", "/api/capture/state")
    return state.get("data", state)


def poll_job(job_id, timeout=60, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, payload = http_req("GET", f"/api/process/jobs/{job_id}")
        if payload.get("ok"):
            job = payload["data"]
            if job["status"] in ("done", "error"):
                return job
        time.sleep(interval)
    return {"status": "timeout"}


async def recv_live_pair(fps=10, scale=0.5, quality=70, timeout=15):
    import websockets
    uri = f"ws://127.0.0.1:{PORT}/ws/live"
    async with websockets.connect(uri, open_timeout=10, close_timeout=5) as ws:
        stats_text = await asyncio.wait_for(ws.recv(), timeout)
        jpeg = await asyncio.wait_for(ws.recv(), timeout)
        return stats_text, jpeg


def step_health_devices():
    status, h = http_req("GET", "/api/health")
    check(status == 200 and h.get("ok"), "GET /api/health returns ok envelope")
    d = h.get("data") or {}
    check(d.get("status") == "ok", "health.status == ok")
    check(d.get("camera_driver") == "mock", "health.camera_driver == mock (macOS)")

    status, payload = http_req("GET", "/api/devices")
    devs = payload.get("data") if payload.get("ok") else []
    check(status == 200 and isinstance(devs, list), "GET /api/devices data is an array")
    mock = [x for x in devs if x.get("id") == "mock-1"]
    check(bool(mock) and mock[0]["kind"] == "mock", "device list contains mock-1")

    status, payload = http_req("POST", "/api/device/open", {"id": "no-such-device"},
                               expect=404)
    check(not payload.get("ok") and isinstance(payload.get("error"), str),
          "error envelope (ok=false + error string + 404)")

    status, root_payload = http_req("GET", "/")
    root_ok = status == 200 and (
        (isinstance(root_payload, dict) and root_payload.get("ok")
         and root_payload["data"].get("frontend_built") is False)
        or isinstance(root_payload, (bytes, bytearray)))
    check(root_ok, "GET / returns frontend-not-built message (or index when built)")


def step_open_properties():
    status, payload = http_req("POST", "/api/device/open", {"id": "mock-1"})
    check(status == 200 and payload.get("ok") and payload["data"]["connected"] is True,
          "open mock-1")

    _, info = http_req("GET", "/api/device/info")
    check(info.get("ok") and info["data"]["connected"] is True
          and len(info["data"]["properties"]) > 0, "device/info includes property list")

    _, props_payload = http_req("GET", "/api/properties")
    props = {p["name"]: p for p in props_payload["data"]["properties"]}
    check("exposure" in props and props["exposure"]["min"] == 30
          and props["exposure"]["max"] == 30000000, "exposure range 30-30000000 us")
    check(props["width"]["value"] == 1024 and props["height"]["value"] == 768,
          "default ROI 1024x768")
    check("mock_pattern" in props and "leed" in props["mock_pattern"].get("options", []),
          "mock_pattern enum (leed/rheed)")

    _, r = http_req("PUT", "/api/properties/exposure", {"value": 3000})
    check(r.get("ok") and r["data"]["actual"] == 3000, "setting exposure=3000 applied")
    _, r = http_req("PUT", "/api/properties/gain", {"value": 1.5})
    check(r.get("ok") and abs(r["data"]["actual"] - 1.5) < 1e-6, "setting gain=1.5")
    _, r = http_req("PUT", "/api/properties/black_level", {"value": 5})
    check(r.get("ok") and r["data"]["actual"] == 5, "setting black_level=5")
    _, r = http_req("PUT", "/api/properties/exposure", {"value": 999999999})
    check(r.get("ok") and r["data"]["actual"] == 30000000, "out-of-range value clamped to max")
    http_req("PUT", "/api/properties/exposure", {"value": 3000})
    # Trigger an unclassified camera error (NaN -> round/int internal ValueError):
    # 500 must be the generic "Internal server error" without exception details.
    _, r500 = http_req("PUT", "/api/properties/exposure", {"value": "nan"},
                       expect=500)
    err = (r500 or {}).get("error", "")
    check(not r500.get("ok") and err == "Internal server error"
          and "nan" not in err.lower() and "ValueError" not in err
          and "Traceback" not in err,
          "unclassified camera error 500 does not leak exception details")
    _, r = http_req("PUT", "/api/properties/mock_drift", {"value": True})
    check(r.get("ok") and r["data"]["actual"] is True, "enable mock slow drift")


def step_settings():
    default_root = DATA_ROOT
    status, s = http_req("GET", "/api/settings")
    check(status == 200 and s.get("ok") and s["data"]["data_root"] == str(default_root),
          "GET /api/settings returns default data dir (absolute path)")
    check(isinstance(s["data"].get("writable"), bool)
          and s["data"]["writable"] is True,
          "settings.writable is boolean and default dir is writable")

    _, bad = http_req("PUT", "/api/settings", {"data_root": "relative/path"},
                      expect=400)
    check(not bad.get("ok") and "absolute path" in bad.get("error", ""),
          "relative data_root returns 400 (English error)")

    tmp_base = Path(tempfile.mkdtemp(prefix="ds-stack-smoke-"))
    tmp_root = (tmp_base / "backend-data").resolve()
    status, r = http_req("PUT", "/api/settings", {"data_root": str(tmp_root)})
    check(status == 200 and r.get("ok")
          and r["data"]["data_root"] == str(tmp_root)
          and r["data"].get("writable") is True,
          "PUT /api/settings creates and switches data dir")
    check((tmp_root / "snapshots").is_dir()
          and (tmp_root / "captures").is_dir(),
          "new data dir has snapshots/ and captures/ created automatically")
    _, r2 = http_req("GET", "/api/settings")
    check(r2.get("ok") and r2["data"]["data_root"] == str(tmp_root),
          "GET /api/settings reflects runtime change immediately")

    _, snap = http_req("POST", "/api/snapshot", {"format": "fits"})
    check(snap.get("ok") and snap["data"]["url"].startswith("/api/files/snapshots/"),
          "snapshot succeeds after switching data dir")
    if snap.get("ok"):
        snap_data = snap["data"]
        new_file = tmp_root / "snapshots" / snap_data["file"]
        check(new_file.is_file(), "snapshot lands in new data dir snapshots/")
        check(snap_data.get("folder") == str((tmp_root / "snapshots").resolve()),
              "v1.4 default folder returns absolute snapshots dir")
        check(snap_data.get("prefix") == "", "v1.4 default prefix is empty string")
        check(bool(re.fullmatch(r"^\d{8}T\d{2}-\d{2}-\d{2}\.fits$",
                                snap_data.get("file", ""))),
              "v1.4 snapshot filename without prefix is <timestamp>.fits")
        check(not (default_root / "snapshots" / snap_data["file"]).exists(),
              "default data dir does not get this snapshot")
        status, body, headers = raw_response(snap_data["url"])
        check(status == 200 and headers.get("cache-control") == "no-store",
              "snapshot file response has Cache-Control: no-store")

    status, r3 = http_req("PUT", "/api/settings", {"data_root": str(default_root)})
    check(status == 200 and r3.get("ok")
          and r3["data"]["data_root"] == str(default_root),
          "PUT /api/settings restores default data dir")
    shutil.rmtree(tmp_base, ignore_errors=True)


def step_browse_folders():
    """v1.4: GET /api/browse/folders — 目录浏览（默认 data_root、子目录与 parent、越界 400）。"""
    root = DATA_ROOT
    browse_root = root / "smoke_browse"
    shutil.rmtree(browse_root, ignore_errors=True)
    browse_root.mkdir(parents=True, exist_ok=True)
    for name in ("b", "a", "c"):
        (browse_root / name).mkdir(exist_ok=True)
    (browse_root / "a" / "deep").mkdir(exist_ok=True)
    (browse_root / "not_a_dir.txt").write_text("x", encoding="utf-8")

    status, payload = http_req("GET", "/api/browse/folders")
    data = payload.get("data") if payload.get("ok") else {}
    check(status == 200 and payload.get("ok"), "browse without path returns ok")
    check(data.get("path") == str(root), "browse default path == data_root")
    check(data.get("parent") is None, "browse default parent is null (data_root top level)")
    check(data.get("files") == [], "browse default files is empty array")
    names = [f.get("name") for f in data.get("folders", [])]
    check(isinstance(data.get("folders"), list) and names == sorted(names),
          "browse default folders is a list sorted by name")
    check("smoke_browse" in names, "browse default lists direct subdir smoke_browse")

    status, payload = http_req("GET", "/api/browse/folders?path="
                               + quote(str(browse_root), safe=""))
    data = payload.get("data") if payload.get("ok") else {}
    check(status == 200 and payload.get("ok") and data.get("path") == str(browse_root),
          "browse path=<absolute subdir> returns resolved path")
    check(data.get("parent") == str(root), "browse subdir parent == data_root")
    check(data.get("files") == [], "browse subdir files is empty array")
    sub_names = [f.get("name") for f in data.get("folders", [])]
    check(sub_names == ["a", "b", "c"],
          "browse lists only direct subdirs sorted by name")
    check(all(f.get("path") == str(browse_root / f.get("name"))
              for f in data.get("folders", [])),
          "browse folder entries carry absolute path")
    check("not_a_dir.txt" not in sub_names, "browse does not list files")
    check("deep" not in sub_names, "browse does not list nested subdirs")

    status, payload = http_req("GET", "/api/browse/folders?path="
                               + quote(str(browse_root / "a"), safe=""))
    data = payload.get("data") if payload.get("ok") else {}
    check(status == 200 and data.get("parent") == str(browse_root),
          "browse nested dir parent == its parent dir")
    deep_names = [f.get("name") for f in data.get("folders", [])]
    check(deep_names == ["deep"], "browse nested dir lists its direct subdir")

    for bad_path in (str(REPO), "/tmp"):
        status, r = http_req("GET", "/api/browse/folders?path="
                             + quote(bad_path, safe=""), expect=400)
        check(status == 400 and not r.get("ok")
              and (r or {}).get("error") == "Folder must be inside the data root",
              f"browse path outside data_root {bad_path!r} -> 400 English error")

    status, r = http_req("GET", "/api/browse/folders?path=relative/path",
                         expect=400)
    check(status == 400 and not r.get("ok")
          and (r or {}).get("error") == "Folder must be inside the data root",
          "browse relative path -> 400 English error")

    status, r = http_req("GET", "/api/browse/folders?path="
                         + quote(str(root / "no_such_dir"), safe=""), expect=400)
    check(status == 400 and not r.get("ok")
          and (r or {}).get("error") == "Folder must be inside the data root",
          "browse nonexistent path -> 400 English error")

    shutil.rmtree(browse_root, ignore_errors=True)


def step_chinese_session():
    """v1.2 FIX: real capture with a Chinese session; FITS SESSION header must stay ASCII-safe."""
    raw_name = "中文_LEED测试"
    phases = [{"type": "bias", "count": 2, "exposure_us": 3000,
               "note": "chinese session smoke"}]
    status, plan = http_req("POST", "/api/capture/plan",
                            {"session_name": raw_name, "phases": phases})
    check(status == 200 and plan.get("ok"), "capture/plan succeeds with Chinese session_name")
    effective = (plan.get("data") or {}).get("session_name")
    check(bool(effective) and "中文" in effective and "LEED" in effective,
          f"effective session_name keeps Chinese: {effective}")
    check(re.fullmatch(r"^\d{8}T\d{2}-\d{2}-\d{2}_中文_LEED测试$",
                       effective or "") is not None,
          "Chinese effective format is timestamp_Chinese-raw-name")

    status, r = http_req("POST", "/api/capture/start")
    check(status == 200 and r.get("ok") and r["data"]["status"] == "running",
          "Chinese session capture/start succeeds")
    state = poll_state(lambda s: s.get("status") == "finished", timeout=60)
    check(state.get("status") == "finished", "Chinese session capture finished")
    check(state.get("frames_done") == 2, "Chinese session frames_done == 2")

    _, sess = http_req("GET", "/api/sessions")
    target = next((s for s in sess.get("data", []) if s.get("id") == effective),
                  None)
    check(target is not None and target.get("name") == effective,
          "sessions list keeps Chinese effective id/name")
    if target:
        check(target.get("frame_counts", {}).get("bias") == 2,
              "Chinese session frame_counts.bias == 2")

    _, detail = http_req("GET", f"/api/sessions/{quote(effective, safe='')}")
    check(detail.get("ok") and detail["data"].get("name") == effective,
          "session detail name keeps Chinese effective")
    frames = (detail.get("data") or {}).get("frames", {}).get("bias", [])
    check(len(frames) == 2, "Chinese session detail frames.bias == 2")
    if frames:
        check(effective in frames[0].get("url", ""),
              "frame URL keeps Chinese effective")

    try:
        from astropy.io import fits
        with fits.open(DATA_ROOT / "captures" / effective
                       / "bias" / "BIAS_0001.fits") as hdul:
            session_hdr = hdul[0].header.get("SESSION")
        check(isinstance(session_hdr, str) and session_hdr.isascii()
              and session_hdr.isprintable() and bool(session_hdr.strip()),
              f"FITS SESSION header is printable ASCII: {session_hdr!r}")
        check("中文" not in session_hdr, "FITS SESSION header contains no Chinese")
    except Exception as exc:  # noqa: BLE001
        check(False, f"failed to read Chinese session FITS: {exc}")


def step_dotted_session():
    """P2 regression: session names containing '..' must stay accessible/processable."""
    status, plan = http_req(
        "POST", "/api/capture/plan",
        {"session_name": "a..b",
         "phases": [{"type": "bias", "count": 1, "exposure_us": 3000},
                    {"type": "light", "count": 1, "exposure_us": 3000}]})
    check(status == 200 and plan.get("ok"), "dotted session plan succeeds")
    effective = (plan.get("data") or {}).get("session_name") or ""
    check(effective.endswith("_a..b"),
          "dotted session keeps double dots in effective name")
    _, r = http_req("POST", "/api/capture/start")
    check(r.get("ok") and r["data"]["status"] == "running",
          "dotted session start succeeds")
    light_wait = poll_state(
        lambda s: s.get("status") == "waiting_user"
        and (s.get("current_phase") or {}).get("type") == "light", timeout=60)
    check(light_wait.get("status") == "waiting_user",
          "dotted session reaches LIGHT waiting_user")
    http_req("POST", "/api/capture/resume")
    state = poll_state(lambda s: s.get("status") == "finished", timeout=60)
    check(state.get("status") == "finished", "dotted session finished")

    _, detail = http_req("GET", f"/api/sessions/{quote(effective, safe='')}")
    check(detail.get("ok") and (detail.get("data") or {}).get("id") == effective,
          "dotted session detail is accessible")

    _, r = http_req("POST", "/api/process/calibrate",
                    {"session_id": effective, "sigma": 3, "method": "mean"})
    check(r.get("ok") and r["data"].get("job_id"),
          "dotted session calibrate accepted")
    job = poll_job(r["data"]["job_id"])
    check(job["status"] == "done", "dotted session calibrate completes")

    # Trailing-dot sanitization: Windows-invalid session names are cleaned.
    _, plan2 = http_req(
        "POST", "/api/capture/plan",
        {"session_name": "trail.",
         "phases": [{"type": "bias", "count": 1, "exposure_us": 3000}]})
    eff2 = ((plan2.get("data") or {}).get("session_name") or "")
    check(plan2.get("ok") and not eff2.endswith(".") and eff2.endswith("_trail"),
          "trailing dot is sanitized from session name")


def step_live_snapshot():
    # P2 regression: concurrent live/start requests must not create duplicate streams.
    from concurrent.futures import ThreadPoolExecutor

    def _start_live():
        return http_req("POST", "/api/live/start",
                        {"fps": 10, "scale": 0.5, "quality": 70})

    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(lambda _: _start_live(), range(2)))
    codes = sorted(r[0] for r in results)
    check(codes == [200, 409],
          "concurrent live starts only one succeeds (one 200, one 409)")
    check(any(r[0] == 200 and r[1].get("ok") for r in results),
          "concurrent live start leaves exactly one stream running")

    try:
        stats_text, jpeg = asyncio.run(recv_live_pair())
    except Exception as exc:  # noqa: BLE001
        check(False, f"/ws/live frame receive failed: {exc}")
        stats_text, jpeg = None, None
    if stats_text is not None:
        stats = json.loads(stats_text)
        keys_ok = all(k in stats for k in
                      ("min", "max", "mean", "std", "saturated_pct", "fps",
                       "histogram"))
        check(keys_ok, "live stats include min/max/mean/std/sat/fps/histogram")
        check(isinstance(stats.get("histogram"), list)
              and len(stats["histogram"]) == 256
              and all(isinstance(v, int) for v in stats["histogram"]),
              "histogram has 256 ints")
        check(isinstance(jpeg, (bytes, bytearray)) and jpeg[:2] == b"\xff\xd8",
              "binary message right after stats is JPEG (0xFFD8)")

    _, r = http_req("POST", "/api/live/stop")
    check(r.get("ok") and r["data"]["streaming"] is False, "live/stop succeeds")

    _, snap = http_req("POST", "/api/snapshot", {"format": "fits"})
    check(snap.get("ok") and snap["data"]["url"].startswith("/api/files/snapshots/"),
          "snapshot fits returns file/url")
    if snap.get("ok"):
        status, body = raw_request(snap["data"]["url"])
        check(status == 200 and body[:6] == b"SIMPLE", "snapshot FITS is downloadable")
        try:
            from astropy.io import fits
            with fits.open(DATA_ROOT / "snapshots" / snap["data"]["file"]) as hdul:
                snap_hdr = hdul[0].header
            for key in ("EXPOSURE", "GAIN", "BLACKLEV", "TEMP",
                        "FRAMETYP", "CAMERA", "SERIAL", "SESSION", "NFRAME"):
                check(key in snap_hdr, f"snapshot FITS header contains {key}")
            check(snap_hdr.get("FRAMETYP") == "SNAPSHOT",
                  "snapshot FRAMETYP == SNAPSHOT")
            check(snap_hdr.get("SESSION") == "SNAPSHOT" and snap_hdr.get("NFRAME") == 1,
                  "snapshot SESSION=SNAPSHOT, NFRAME=1")
        except Exception as exc:  # noqa: BLE001
            check(False, f"snapshot FITS header validation failed: {exc}")
    status, snap_png = http_req("POST", "/api/snapshot", {"format": "png"})
    check(status == 200 and snap_png.get("ok"),
          "snapshot PNG request succeeds")
    if snap_png.get("ok"):
        status, body = raw_request(snap_png["data"]["url"])
        check(status == 200 and body[:4] == b"\x89PNG", "snapshot PNG is downloadable")

    # Snapshot save failure path: 500 is the generic "Internal server error"
    # without leaking PermissionError details.
    snap_dir = DATA_ROOT / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    orig_mode = stat.S_IMODE(snap_dir.stat().st_mode)
    try:
        os.chmod(snap_dir, 0o555)
        _, r500 = http_req("POST", "/api/snapshot", {"format": "fits"},
                           expect=500)
        err = (r500 or {}).get("error", "")
        check(not r500.get("ok") and err == "Internal server error"
              and "PermissionError" not in err and "Traceback" not in err
              and "Permission" not in err,
              "snapshot save failure 500 does not leak exception details")
    finally:
        os.chmod(snap_dir, orig_mode)


def step_snapshot_folders():
    """v1.4: 绝对 folder + prefix + 文件名恒带时间戳；保留 v1.3 相对单段名与 folders 接口。"""
    snap_root = (DATA_ROOT / "snapshots").resolve()
    data_root = DATA_ROOT.resolve()
    ts_fits_re = r"^\d{8}T\d{2}-\d{2}-\d{2}\.fits$"

    status, snap = http_req("POST", "/api/snapshot", {"format": "fits"})
    data = snap.get("data") if snap.get("ok") else {}
    check(status == 200 and snap.get("ok"), "default folder: POST /api/snapshot succeeds")
    check(data.get("folder") == str(snap_root)
          and data.get("url") == f"/api/files/snapshots/{data.get('file')}",
          "default folder -> snapshots root (absolute folder, url has no subfolder segment)")
    check(data.get("prefix") == "", "default snapshot prefix is empty")
    check(bool(re.fullmatch(ts_fits_re, data.get("file", ""))),
          "default snapshot filename is <timestamp>.fits (v1.4)")
    if data.get("file"):
        check((snap_root / data["file"]).is_file(),
              "default-folder snapshot lands in data/snapshots/ root")

    status, snap_blank = http_req("POST", "/api/snapshot",
                                  {"format": "png", "folder": "   "})
    blank = snap_blank.get("data") if snap_blank.get("ok") else {}
    check(status == 200 and snap_blank.get("ok")
          and blank.get("folder") == str(snap_root),
          "blank folder -> snapshots root (absolute folder)")
    check(bool(re.fullmatch(r"^\d{8}T\d{2}-\d{2}-\d{2}\.png$",
                            blank.get("file", ""))),
          "blank-folder png filename is <timestamp>.png (v1.4)")

    folder_name = "测试文件夹"
    status, snap_sub = http_req("POST", "/api/snapshot",
                                {"format": "fits", "folder": folder_name})
    sub = snap_sub.get("data") if snap_sub.get("ok") else {}
    check(status == 200 and snap_sub.get("ok"), f"folder='{folder_name}': POST /api/snapshot succeeds")
    check(sub.get("folder") == str(snap_root / folder_name),
          "relative single-segment folder maps to data_root/snapshots/<name> (absolute response)")
    check(sub.get("url") == f"/api/files/snapshots/{folder_name}/{sub.get('file')}",
          "url is /api/files/snapshots/<folder>/<file>")
    check(bool(re.fullmatch(ts_fits_re, sub.get("file", ""))),
          "relative-folder snapshot filename is <timestamp>.fits (v1.4)")
    check(bool(sub.get("file"))
          and (snap_root / folder_name / sub["file"]).is_file(),
          "snapshot file is created inside the subfolder")

    if sub.get("url"):
        status, body, headers = raw_response(quote(sub["url"], safe="/"))
        check(status == 200 and body[:6] == b"SIMPLE", "subfolder snapshot url downloads (FITS)")
        check(headers.get("cache-control") == "no-store",
              "subfolder snapshot download keeps Cache-Control: no-store")

    # Windows trailing-dot sanitization for relative snapshot folder names.
    status, snap_dot = http_req("POST", "/api/snapshot",
                                {"format": "fits", "folder": "trail."})
    dot = snap_dot.get("data") if snap_dot.get("ok") else {}
    check(status == 200 and snap_dot.get("ok"),
          "trailing-dot folder snapshot succeeds")
    check(dot.get("folder") == str(snap_root / "trail"),
          "trailing dot is stripped from snapshot folder")
    check(dot.get("url") == f"/api/files/snapshots/trail/{dot.get('file')}",
          "trailing-dot folder url uses stripped name")
    if dot.get("file"):
        check((snap_root / "trail" / dot["file"]).is_file(),
              "trailing-dot folder file exists under stripped name")

    _, fp = http_req("GET", "/api/snapshot/folders")
    folders = ((fp.get("data") or {}).get("folders") or []) if fp.get("ok") else []
    check(fp.get("ok") and isinstance((fp.get("data") or {}).get("folders"), list),
          "GET /api/snapshot/folders returns ok + folders array")
    root_entry = next((f for f in folders if f.get("name") == ""), None)
    check(root_entry is not None and root_entry.get("files", 0) >= 1,
          "folders include root entry name='' with files>=1")
    names = [f.get("name") for f in folders if f.get("name") != ""]
    check(names == sorted(names), "subfolders are sorted by name")
    target = next((f for f in folders if f.get("name") == folder_name), None)
    check(target is not None and target.get("files", 0) >= 1,
          f"folder '{folder_name}' appears in folders with files>=1")

    for bad in ("../evil", "/abs", "a/b", "a\\b", 'bad:name'):
        status, r = http_req("POST", "/api/snapshot",
                             {"format": "fits", "folder": bad}, expect=400)
        err = (r or {}).get("error", "")
        check(status == 400 and not r.get("ok") and "folder" in err.lower(),
              f"invalid folder {bad!r} -> 400 English error")

    if sub.get("file"):
        status, _ = raw_request(quote(
            f"/api/files/snapshots/{folder_name}/../{sub['file']}", safe="/"))
        check(status == 400, "snapshot subfolder traversal (<folder>/../<file>) -> 400")

    # v1.4：data_root 下自选绝对目录（不存在时自动 mkdir parents）+ prefix="spot"。
    abs_dir = data_root / "v14_smoke_abs" / "nested"
    shutil.rmtree(data_root / "v14_smoke_abs", ignore_errors=True)
    status, snap_abs = http_req("POST", "/api/snapshot",
                                {"format": "fits", "folder": str(abs_dir),
                                 "prefix": "spot"})
    abs_data = snap_abs.get("data") if snap_abs.get("ok") else {}
    check(status == 200 and snap_abs.get("ok") and abs_dir.is_dir(),
          "absolute folder under data_root auto-created with parents")
    check(abs_data.get("folder") == str(abs_dir),
          "absolute folder echoed as absolute path")
    check(abs_data.get("prefix") == "spot", "prefix echoed back")
    check(bool(re.fullmatch(r"^spot_\d{8}T\d{2}-\d{2}-\d{2}\.fits$",
                            abs_data.get("file", ""))),
          "prefix='spot' filename is spot_<timestamp>.fits")
    check(abs_data.get("url") == f"/api/files/snapshots/v14_smoke_abs/nested/{abs_data.get('file')}",
          "absolute-folder url is /api/files/snapshots/<rel-to-data_root>/<file>")
    if abs_data.get("file"):
        check((abs_dir / abs_data["file"]).is_file(),
              "absolute-folder snapshot file exists on disk")
        status, body, headers = raw_response(quote(abs_data["url"], safe="/"))
        check(status == 200 and body[:6] == b"SIMPLE",
              "absolute-folder snapshot url downloads (FITS)")
        check(headers.get("cache-control") == "no-store",
              "absolute-folder snapshot download keeps Cache-Control: no-store")

    status, snap_abs2 = http_req("POST", "/api/snapshot",
                                 {"format": "fits", "folder": str(abs_dir)})
    abs2 = snap_abs2.get("data") if snap_abs2.get("ok") else {}
    check(status == 200 and snap_abs2.get("ok")
          and abs2.get("prefix") == ""
          and bool(re.fullmatch(ts_fits_re, abs2.get("file", ""))),
          "absolute folder without prefix -> <timestamp>.fits and prefix=''")
    if abs2.get("file"):
        check((abs_dir / abs2["file"]).is_file(),
              "absolute-folder snapshot without prefix exists on disk")

    raw_prefix = 'spot/<x>:"a b?*|c\\d '
    expected_prefix = re.sub(r'[<>:"/\\|?*\s]', "_", raw_prefix)
    status, snap_pfx = http_req("POST", "/api/snapshot",
                                {"format": "fits", "folder": str(abs_dir),
                                 "prefix": raw_prefix})
    pfx = snap_pfx.get("data") if snap_pfx.get("ok") else {}
    check(status == 200 and snap_pfx.get("ok"), "illegal prefix: POST /api/snapshot succeeds")
    check(pfx.get("prefix") == expected_prefix,
          f"prefix sanitized to {expected_prefix!r}")
    check(not re.search(r'[<>:"/\\|?*\s]', pfx.get("prefix", "")),
          "sanitized prefix has no separators/illegal chars/whitespace")
    pfx_name_re = "^" + re.escape(expected_prefix) + r"_\d{8}T\d{2}-\d{2}-\d{2}\.fits$"
    check(bool(re.fullmatch(pfx_name_re, pfx.get("file", ""))),
          "sanitized prefix used in filename <prefix>_<timestamp>.fits")
    if pfx.get("file"):
        check((abs_dir / pfx["file"]).is_file(),
              "sanitized-prefix snapshot exists on disk")

    for bad_abs in (str(REPO), "/tmp"):
        status, r = http_req("POST", "/api/snapshot",
                             {"format": "fits", "folder": bad_abs}, expect=400)
        check(status == 400 and not r.get("ok")
              and (r or {}).get("error") == "Folder must be inside the data root",
              f"absolute folder outside data_root {bad_abs!r} -> 400")

    # v1.4：folder == data_root 允许（文件落在 data_root 根目录）。
    status, snap_rootabs = http_req("POST", "/api/snapshot",
                                    {"format": "fits", "folder": str(data_root)})
    rootabs = snap_rootabs.get("data") if snap_rootabs.get("ok") else {}
    check(status == 200 and snap_rootabs.get("ok")
          and rootabs.get("folder") == str(data_root),
          "folder == data_root is allowed")
    if rootabs.get("file"):
        check((data_root / rootabs["file"]).is_file(),
              "folder == data_root snapshot lands in data_root root")
        check(rootabs.get("url") == f"/api/files/snapshots/{rootabs['file']}",
              "folder == data_root url has no subfolder segment")
        status, body = raw_request(rootabs["url"])
        check(status == 200 and body[:6] == b"SIMPLE",
              "folder == data_root snapshot url downloads (FITS)")
        (data_root / rootabs["file"]).unlink(missing_ok=True)

    shutil.rmtree(data_root / "v14_smoke_abs", ignore_errors=True)


def step_capture():
    global SESSION_ID, EFFECTIVE_SESSION_NAME
    phases = [
        {"type": "bias", "count": 3, "exposure_us": 30, "note": "smoke bias"},
        {"type": "dark", "count": 3, "exposure_us": None, "note": "smoke dark"},
        {"type": "flat", "count": 3, "exposure_us": None, "note": "smoke flat"},
        {"type": "light", "count": 3, "exposure_us": None, "note": "smoke light"},
    ]
    status, bad_plan = http_req("POST", "/api/capture/plan",
                                {"session_name": "EMPTY", "phases": []},
                                expect=400)
    check(status == 400 and not bad_plan.get("ok"), "empty-phases plan returns 400")

    raw_session_name = "SMOKE LEED/001:a*b?x"
    status, plan = http_req("POST", "/api/capture/plan",
                            {"session_name": raw_session_name, "phases": phases})
    check(status == 200 and plan.get("ok") and plan["data"].get("plan_id"),
          "capture/plan returns plan_id")
    echoed = plan["data"]["phases"]
    check(len(echoed) == 4 and echoed[0]["type"] == "bias"
          and echoed[1]["exposure_us"] == 3000,
          f"plan echoes and resolves null exposure to current value "
          f"(dark exposure={echoed[1].get('exposure_us')!r})")

    effective = plan["data"].get("session_name")
    EFFECTIVE_SESSION_NAME = effective
    expected_re = r"^\d{8}T\d{2}-\d{2}-\d{2}_" + SANITIZED_SESSION_SUFFIX + r"$"
    check(bool(effective) and re.fullmatch(expected_re, effective) is not None,
          f"plan returns effective session_name (timestamp_sanitized): {effective}")
    check(not re.search(r'[<>:"/\\|?*\s]', effective or ""),
          "effective session_name has no separators/Windows-illegal chars/spaces")

    status, r = http_req("POST", "/api/capture/start")
    check(status == 200 and r.get("ok") and r["data"]["status"] == "running",
          "capture/start succeeds")
    _, state0 = http_req("GET", "/api/capture/state")
    check(state0.get("ok") and state0["data"].get("session_name") == effective,
          "capture state session_name uses effective name")

    # v1.1 state machine: non-LIGHT phases waiting_user(needs_user=false) -> auto capturing
    bias_wait = poll_state(
        lambda s: s.get("status") == "waiting_user"
        and (s.get("current_phase") or {}).get("type") == "bias",
        timeout=10, interval=0.01)
    check(bias_wait.get("status") == "waiting_user"
          and bias_wait.get("needs_user") is False,
          "bias phase enters waiting_user with needs_user=false")
    _, rr = http_req("POST", "/api/capture/resume", expect=409)
    check(not rr.get("ok"), "resume during non-LIGHT waiting_user returns 409")
    bias_cap = poll_state(
        lambda s: s.get("status") == "capturing"
        and (s.get("current_phase") or {}).get("type") == "bias",
        timeout=10, interval=0.01)
    check(bias_cap.get("status") == "capturing" and bias_cap.get("needs_user") is False,
          "bias automatically enters capturing (needs_user=false)")

    state = poll_state(
        lambda s: s.get("status") == "waiting_user"
        and (s.get("current_phase") or {}).get("type") == "light", timeout=60)
    check(state.get("status") == "waiting_user"
          and state["current_phase"]["type"] == "light",
          "waiting_user before LIGHT (mandatory manual confirmation)")
    check(state.get("needs_user") is True, "LIGHT waiting_user needs_user=true")
    check("histogram" in (state.get("prompt") or ""), "LIGHT prompt contains histogram confirmation text")
    check(state["current_phase"]["index"] == 4 and state["total_frames"] == 12,
          "state: phase 4/4, 12 frames total")
    check(any("BIAS" in e["msg"] for e in state.get("log", [])), "log contains BIAS done entry")

    # Submitting a processing job during capture must 409 (session dir already created).
    _, sess_now = http_req("GET", "/api/sessions")
    cur = next((s for s in sess_now["data"]
                if s["name"] == EFFECTIVE_SESSION_NAME), None)
    if cur:
        check(cur["id"] == EFFECTIVE_SESSION_NAME,
              "sessions list id == effective session_name")
    if cur:
        _, r409 = http_req("POST", "/api/process/calibrate",
                           {"session_id": cur["id"]}, expect=409)
        check(not r409.get("ok") and "Capture in progress" in r409.get("error", ""),
              "submitting calibrate during capture returns 409")

    # P2 regression: device/settings lifecycle operations must be rejected while capture is active.
    _, r409 = http_req("POST", "/api/device/close", expect=409)
    check(not r409.get("ok") and "Capture in progress" in r409.get("error", ""),
          "device close during capture returns 409")
    _, r409 = http_req("POST", "/api/device/open", {"id": "mock-1"}, expect=409)
    check(not r409.get("ok") and "Capture in progress" in r409.get("error", ""),
          "device open during capture returns 409")
    lock_base = Path(tempfile.mkdtemp(prefix="ds-capture-lock-"))
    lock_root = lock_base / "newroot"
    _, r409 = http_req("PUT", "/api/settings", {"data_root": str(lock_root)}, expect=409)
    check(not r409.get("ok") and "Capture in progress" in r409.get("error", ""),
          "settings switch during capture returns 409")
    check(not lock_root.exists(),
          "rejected settings switch during capture creates no new data dir")
    shutil.rmtree(lock_base, ignore_errors=True)

    # Live preview remains available during waiting_user (manual histogram check scenario).
    http_req("POST", "/api/live/start", {"fps": 10, "scale": 0.25, "quality": 60})
    try:
        stats_text, _ = asyncio.run(recv_live_pair())
        check(bool(stats_text), "/ws/live has frames during waiting_user")
    except Exception as exc:  # noqa: BLE001
        check(False, f"frame receive failed during waiting_user: {exc}")
    http_req("POST", "/api/live/stop")

    # /ws/events: resume must push a capture state change.
    async def resume_and_listen():
        import websockets
        uri = f"ws://127.0.0.1:{PORT}/ws/events"
        async with websockets.connect(uri, open_timeout=10) as ws:
            snapshot = json.loads(await asyncio.wait_for(ws.recv(), 10))
            assert snapshot.get("topic") == "capture"
            # resume triggers a state push
            status, _ = http_req("POST", "/api/capture/resume")
            assert status == 200
            deadline = time.time() + 15
            while time.time() < deadline:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if msg.get("topic") == "capture" and msg["data"].get("status") in (
                        "capturing", "running"):
                    return msg
            return None

    try:
        event_msg = asyncio.run(resume_and_listen())
    except Exception as exc:  # noqa: BLE001
        check(False, f"/ws/events or resume failed: {exc}")
        event_msg = None
        http_req("POST", "/api/capture/resume")
    check(event_msg is not None and event_msg["topic"] == "capture",
          "/ws/events pushes capture state change (resume->capturing)")
    if event_msg:
        check(event_msg["data"].get("needs_user") is False,
              "/ws/events capture message has needs_user=false (after resume)")
    _, r_again = http_req("POST", "/api/capture/resume", expect=409)
    check(not r_again.get("ok"), "resume again during capturing returns 409")

    state = poll_state(lambda s: s.get("status") == "finished", timeout=90)
    check(state.get("status") == "finished", "capture finished")
    check(state.get("frames_done") == 12, "frames_done == 12")
    # Pick id from sessions list.
    _, sess = http_req("GET", "/api/sessions")
    sids = [s["id"] for s in sess["data"] if s["name"] == EFFECTIVE_SESSION_NAME]
    check(bool(sids), "sessions list contains this capture (name=effective)")
    if sids:
        SESSION_ID = sids[0]
        check(SESSION_ID == EFFECTIVE_SESSION_NAME,
              "sessions list id == effective session_name")
        target = next(s for s in sess["data"] if s["id"] == SESSION_ID)
        check(target["name"] == EFFECTIVE_SESSION_NAME,
              "sessions list name uses effective name")
        fc = target["frame_counts"]
        check(fc == {"bias": 3, "dark": 3, "flat": 3, "light": 3},
              "frame_counts are 3 per phase")


def step_capture_dir_error():
    """P2 regression: session-dir creation OSError must move capture state to error, not hang."""
    default_root = DATA_ROOT.resolve()
    tmp_base = Path(tempfile.mkdtemp(prefix="ds-capture-ro-"))
    tmp_root = tmp_base / "ro-data"
    tmp_root.mkdir()
    status, r = http_req("PUT", "/api/settings", {"data_root": str(tmp_root)})
    check(status == 200 and r.get("ok"),
          "capture-dir-error: switch to temp data root succeeds")
    captures_dir = tmp_root / "captures"
    captures_dir.mkdir(exist_ok=True)
    orig_mode = stat.S_IMODE(captures_dir.stat().st_mode)
    try:
        os.chmod(captures_dir, 0o555)
        status, plan = http_req(
            "POST", "/api/capture/plan",
            {"session_name": "RO_ERROR",
             "phases": [{"type": "bias", "count": 1, "exposure_us": 30}]})
        check(status == 200 and plan.get("ok"),
              "capture-dir-error: plan succeeds")
        _, r = http_req("POST", "/api/capture/start")
        check(r.get("ok") and r["data"]["status"] == "running",
              "capture-dir-error: start succeeds")
        state = poll_state(lambda s: s.get("status") in ("error", "finished"),
                           timeout=20)
        check(state.get("status") == "error",
              "capture-dir-error: session-dir OSError sets state to error")
        check("Failed to create session directory" in (state.get("message") or ""),
              "capture-dir-error: error message is descriptive")
    finally:
        os.chmod(captures_dir, orig_mode)
        http_req("PUT", "/api/settings", {"data_root": str(default_root)})
        shutil.rmtree(tmp_base, ignore_errors=True)

    # Leave capture state finished for the rest of the smoke run.
    _, plan = http_req(
        "POST", "/api/capture/plan",
        {"session_name": "RO_RECOVER",
         "phases": [{"type": "bias", "count": 1, "exposure_us": 30}]})
    check(plan.get("ok"), "capture-dir-error: recovery plan succeeds")
    _, r = http_req("POST", "/api/capture/start")
    check(r.get("ok") and r["data"]["status"] == "running",
          "capture-dir-error: recovery start succeeds")
    state = poll_state(lambda s: s.get("status") == "finished", timeout=30)
    check(state.get("status") == "finished",
          "capture-dir-error: recovery capture finishes")


def step_p3_regressions():
    """P3 regressions: sync abort, thumbnail failure, collision fallback naming."""
    # 1) Abort must be reflected in capture state synchronously.
    _, plan = http_req(
        "POST", "/api/capture/plan",
        {"session_name": "ABORT_SYNC",
         "phases": [{"type": "light", "count": 1, "exposure_us": 3000}]})
    check(plan.get("ok"), "abort-sync plan succeeds")
    _, r = http_req("POST", "/api/capture/start")
    check(r.get("ok") and r["data"]["status"] == "running",
          "abort-sync start succeeds")
    wait = poll_state(lambda s: s.get("status") == "waiting_user", timeout=10)
    check(wait.get("status") == "waiting_user",
          "abort-sync reaches waiting_user")
    _, rab = http_req("POST", "/api/capture/abort")
    check(rab.get("ok") and rab["data"]["status"] == "aborted",
          "abort returns aborted")
    _, st = http_req("GET", "/api/capture/state")
    check(st.get("ok") and st["data"]["status"] == "aborted",
          "capture state is aborted immediately after abort")

    # 2) Thumbnail failure must not discard a successfully written FITS frame.
    _, plan = http_req(
        "POST", "/api/capture/plan",
        {"session_name": "THUMB_FAIL",
         "phases": [{"type": "light", "count": 1, "exposure_us": 3000}]})
    check(plan.get("ok"), "thumb-fail plan succeeds")
    eff = (plan.get("data") or {}).get("session_name") or ""
    _, r = http_req("POST", "/api/capture/start")
    check(r.get("ok") and r["data"]["status"] == "running",
          "thumb-fail start succeeds")
    wait = poll_state(lambda s: s.get("status") == "waiting_user", timeout=10)
    check(wait.get("status") == "waiting_user",
          "thumb-fail reaches waiting_user")
    thumbs_dir = DATA_ROOT / "captures" / eff / "light" / "thumbs"
    check(thumbs_dir.is_dir(), "thumb-fail thumbs dir exists")
    orig_mode = stat.S_IMODE(thumbs_dir.stat().st_mode)
    try:
        os.chmod(thumbs_dir, 0o555)
        http_req("POST", "/api/capture/resume")
        state = poll_state(lambda s: s.get("status") == "finished", timeout=30)
        check(state.get("status") == "finished",
              "thumb-fail capture finishes despite thumbnail failure")
        check(state.get("frames_done") == 1,
              "thumb-fail FITS frame still counted (frames_done=1)")
    finally:
        os.chmod(thumbs_dir, orig_mode)

    # 3) Session-name collision fallback must propagate the actual suffixed id.
    _, plan = http_req(
        "POST", "/api/capture/plan",
        {"session_name": "COLLIDE",
         "phases": [{"type": "bias", "count": 1, "exposure_us": 3000}]})
    check(plan.get("ok"), "collision plan succeeds")
    eff = (plan.get("data") or {}).get("session_name") or ""
    blocker = DATA_ROOT / "captures" / eff
    blocker.mkdir(parents=True, exist_ok=True)
    try:
        _, r = http_req("POST", "/api/capture/start")
        check(r.get("ok") and r["data"]["status"] == "running",
              "collision start succeeds")
        state = poll_state(lambda s: s.get("status") == "finished", timeout=30)
        check(state.get("status") == "finished",
              "collision capture finishes")
        actual = state.get("session_name") or ""
        check(actual != eff and actual.startswith(eff + "_"),
              "collision fallback session_name includes suffix")
        _, sess = http_req("GET", "/api/sessions")
        match = next((s for s in sess.get("data", [])
                      if s.get("id") == actual), None)
        check(match is not None and match.get("name") == actual,
              "collision fallback session list id/name match")
        meta_path = DATA_ROOT / "captures" / actual / "meta.json"
        if meta_path.is_file():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            check(meta.get("id") == actual and meta.get("name") == actual,
                  "collision fallback meta id/name match")
        else:
            check(False, "collision fallback meta.json exists")
    finally:
        # The blocker directory is inside the temporary smoke data root.
        pass


def step_session_files():
    _, detail = http_req("GET", f"/api/sessions/{SESSION_ID}")
    d = detail["data"]
    check(d.get("id") == EFFECTIVE_SESSION_NAME
          and d.get("name") == EFFECTIVE_SESSION_NAME,
          "session detail id and name use effective name")
    check(len(d["phases"]) == 4, "session detail contains 4 phases")
    for g in ("bias", "dark", "flat", "light"):
        frames = d["frames"].get(g, [])
        check(len(frames) == 3, f"frames.{g} == 3")
        if frames:
            f0 = frames[0]
            check(all(k in f0 for k in ("file", "url", "thumb", "mean", "std",
                                        "saturated_pct")), f"{g} frame fields complete")
            check(isinstance(f0["mean"], (int, float)), f"{g} frame contains mean stat")
    f0 = d["frames"]["bias"][0]
    status, body, headers = raw_response(f0["url"])
    check(status == 200 and body[:6] == b"SIMPLE", "FITS file downloadable (SIMPLE header)")
    check(headers.get("cache-control") == "no-store",
          "GET /api/files response has Cache-Control: no-store")
    try:
        from astropy.io import fits
        with fits.open(DATA_ROOT / "captures" / SESSION_ID
                       / "bias" / f0["file"]) as hdul:
            hdr = hdul[0].header
            data = hdul[0].data
        required_keys = ("DATE-OBS", "EXPOSURE", "GAIN", "BLACKLEV",
                         "TEMP", "FRAMETYP", "CAMERA", "SERIAL",
                         "SESSION", "NFRAME")
        check(all(k in hdr for k in required_keys),
              "FITS header contains the 10 contract keywords")
        check(hdr.get("BITPIX") == 16 and int(data.max()) <= 4095,
              "FITS is 16-bit with data in the low 12 bits (max <= 4095)")
        check("." in str(hdr.get("DATE-OBS", "")),
              "DATE-OBS has millisecond precision (fractional seconds)")
    except Exception as exc:  # noqa: BLE001
        check(False, f"FITS header validation failed: {exc}")
    status, body = raw_request(f0["thumb"])
    check(status == 200 and body[:4] == b"\x89PNG", "thumbnail PNG downloadable")
    status, body, headers = raw_response(f"/api/files/{SESSION_ID}/meta.json")
    check(status == 200 and json.loads(body).get("name") == EFFECTIVE_SESSION_NAME,
          "meta.json downloadable and name is effective")
    check(headers.get("cache-control") == "no-store",
          "meta.json response has Cache-Control: no-store")

    status, body = raw_request(f"/api/files/{SESSION_ID}/bias/../meta.json")
    check(status == 400, "path traversal (bias/../meta.json) rejected with 400")
    status, body = raw_request(f"/api/files/../../etc/passwd")
    check(status in (400, 404), "invalid session_id rejected")


def step_processing():
    # Excluding a nonexistent file -> 400 (English error).
    status, bad = http_req("POST", "/api/process/calibrate",
                           {"session_id": SESSION_ID, "sigma": 3, "method": "mean",
                            "exclude": {"light": ["NOPE_0001.fits"]}},
                           expect=400)
    check(status == 400 and not bad.get("ok")
          and "does not exist" in bad.get("error", ""),
          "excluding a nonexistent file returns 400 (English error)")

    # P2 regression: concurrent calibrate jobs for the same session must not race.
    from concurrent.futures import ThreadPoolExecutor

    def _submit_calibrate():
        return http_req("POST", "/api/process/calibrate",
                        {"session_id": SESSION_ID, "sigma": 3, "method": "mean"})

    with ThreadPoolExecutor(max_workers=2) as ex:
        cal_results = list(ex.map(lambda _: _submit_calibrate(), range(2)))
    cal_codes = sorted(r[0] for r in cal_results)
    check(cal_codes == [200, 409],
          "concurrent calibrate jobs for same session only one accepted")
    ok_cal = next(r for r in cal_results if r[0] == 200)
    job_id = ok_cal[1]["data"]["job_id"]
    job = poll_job(job_id)
    check(job["status"] == "done", f"calibrate done ({job.get('message')})")
    check(job.get("progress") == 100, "calibrate progress == 100")
    masters = (job.get("result") or {}).get("masters", {})
    check(all(masters.get(k) for k in ("bias", "dark", "flat")),
          "master bias/dark/flat all generated")
    check((job.get("result") or {}).get("calibrated_count") == 3,
          "calibrated_count == 3")
    excl = (job.get("result") or {}).get("excluded_counts") or {}
    check(excl == {"bias": 0, "dark": 0, "flat": 0, "light": 0},
          "calibrate result contains excluded_counts (all 0)")

    # Files under masters/ are directly accessible.
    status, body = raw_request(f"/api/files/{SESSION_ID}/masters/MASTER_DARK.fits")
    check(status == 200 and body[:6] == b"SIMPLE",
          "/api/files can access masters/MASTER_DARK.fits")

    # v1.1 exclude: stack excludes one light (array with duplicates is deduped) -> frames=2
    _, r = http_req("POST", "/api/process/stack",
                    {"session_id": SESSION_ID, "sigma": 3, "method": "mean",
                     "registration": True,
                     "exclude": {"bias": [], "dark": [], "flat": [],
                                 "light": ["LIGHT_0003.fits",
                                           "LIGHT_0003.fits"]}})
    job_id = r["data"]["job_id"]
    job = poll_job(job_id)
    check(job["status"] == "done", f"stack done ({job.get('message')})")
    res = job.get("result") or {}
    check(res.get("file") == "STACK_sigma3.fits", "stack outputs STACK_sigma3.fits")
    qc = res.get("qc") or {}
    for k in ("frames", "total_exposure_us", "effective_bits", "rejected_pct",
              "background_std", "excluded_counts"):
        check(k in qc, f"qc_report contains {k}")
    check(qc.get("frames") == 2 and qc.get("total_exposure_us") == 6000,
          f"qc: frames=2 and total_exposure_us=6000 after excluding one light "
          f"(actual frames={qc.get('frames')!r}, total_exposure_us={qc.get('total_exposure_us')!r})")
    check((qc.get("excluded_counts") or {}).get("light") == 1,
          "qc.excluded_counts.light == 1")
    check((res.get("excluded_counts") or {}).get("light") == 1,
          "stack result.excluded_counts.light == 1")

    # STACK is float32 (BITPIX=-32).
    try:
        from astropy.io import fits
        with fits.open(DATA_ROOT / "captures" / SESSION_ID
                       / "results" / "STACK_sigma3.fits") as hdul:
            stack_bitpix = hdul[0].header.get("BITPIX")
            stack_data = hdul[0].data
        check(stack_bitpix == -32 and np.dtype(stack_data.dtype).kind == "f"
              and stack_data.dtype.itemsize == 4,
              "STACK_sigma3.fits is float32 (BITPIX=-32)")
    except Exception as exc:  # noqa: BLE001
        check(False, f"STACK precision validation failed: {exc}")

    _, r = http_req("POST", "/api/process/stretch",
                    {"session_id": SESSION_ID, "result_file": "STACK_sigma3.fits",
                     "stretch": "asinh", "black": 0, "white": 4095})
    job_id = r["data"]["job_id"]
    job = poll_job(job_id)
    check(job["status"] == "done", f"stretch done ({job.get('message')})")
    sres = job.get("result") or {}
    check("asinh" in sres.get("file", ""), "stretch outputs 16-bit FITS result")
    if sres.get("preview_url"):
        status, body = raw_request(sres["preview_url"])
        check(status == 200 and body[:4] == b"\x89PNG", "STACK_preview.png downloadable")
    if sres.get("url"):
        status, body = raw_request(sres["url"])
        check(status == 200 and body[:6] == b"SIMPLE", "stretched result FITS downloadable")

    # P1 regression: re-running calibrate with a different exclude must clear old
    # calibrated/*.fits so _run_stack cannot consume stale CAL files.
    _, r = http_req("POST", "/api/process/calibrate",
                    {"session_id": SESSION_ID, "sigma": 3, "method": "mean",
                     "exclude": {"bias": [], "dark": [], "flat": [],
                                 "light": ["LIGHT_0001.fits"]}})
    job_id = r["data"]["job_id"]
    job = poll_job(job_id)
    check(job["status"] == "done", "rerun calibrate with different exclude done")
    check((job.get("result") or {}).get("calibrated_count") == 2,
          "rerun calibrate writes only 2 calibrated frames (LIGHT_0001 excluded)")
    cal_dir = DATA_ROOT / "captures" / SESSION_ID / "calibrated"
    check(not (cal_dir / "CAL_0003.fits").exists(),
          "rerun calibrate removes stale CAL_0003.fits from previous run")

    _, r = http_req("POST", "/api/process/stack",
                    {"session_id": SESSION_ID, "sigma": 3, "method": "mean"})
    job_id = r["data"]["job_id"]
    job = poll_job(job_id)
    check(job["status"] == "done", "stack after rerun calibrate done")
    qc2 = ((job.get("result") or {}).get("qc") or {})
    check(qc2.get("frames") == 2,
          "stack after rerun calibrate uses 2 frames, not stale CAL_0003")

    _, detail = http_req("GET", f"/api/sessions/{SESSION_ID}")
    d = detail["data"]
    check(len(d["results"]) >= 2, "session detail results >= 2 (stack + stretched result)")
    check(isinstance(d["qc"], dict) and d["qc"].get("frames") == 2,
          "session detail returns qc_report (frames=2, includes excluded_counts)")


def step_live_error():
    """P2 regression: persistent live grab failure must stop and expose an error."""
    status, r = http_req("POST", "/api/live/start",
                         {"fps": 10, "scale": 0.5, "quality": 70})
    check(status == 200 and r.get("ok") and r["data"]["streaming"] is True,
          "live error test: start succeeds")
    http_req("POST", "/api/device/close")
    deadline = time.time() + 10
    live_state = None
    while time.time() < deadline:
        _, live_state = http_req("GET", "/api/live/state")
        data = live_state.get("data") or {}
        if live_state.get("ok") and data.get("streaming") is False \
                and data.get("error"):
            break
        time.sleep(0.1)
    check(live_state is not None and live_state.get("ok")
          and (live_state.get("data") or {}).get("streaming") is False
          and bool((live_state.get("data") or {}).get("error")),
          "live after device close stops and exposes error state")


def step_pick_folder_unit():
    """Unit-level check for /api/pick-folder non-GUI paths (no dialog opened)."""
    import sys as _sys
    from unittest.mock import patch

    if str(BACKEND) not in _sys.path:
        _sys.path.insert(0, str(BACKEND))
    import app.main as m

    if _sys.platform == "darwin":
        runner = "_run_macos_picker"
    elif _sys.platform.startswith("win"):
        runner = "_run_windows_picker"
    else:
        runner = "_run_linux_picker"

    with patch.object(m, runner, return_value="/tmp/selected"):
        check(m._pick_folder("Select") == "/tmp/selected",
              "folder picker unit: selected path is returned")
    with patch.object(m, runner, return_value=None):
        check(m._pick_folder("Select") is None,
              "folder picker unit: cancel returns None")
    with patch.object(m, runner, side_effect=RuntimeError("no GUI")):
        try:
            m._pick_folder("Select")
        except RuntimeError as exc:
            check("no GUI" in str(exc),
                  "folder picker unit: unavailable picker raises RuntimeError")
        else:
            check(False,
                  "folder picker unit: unavailable picker raises RuntimeError")

    # macOS osascript parsing: rc=0, rc!=0 with stdout path, cancel, real error.
    from types import SimpleNamespace

    with patch.object(m.subprocess, "run", return_value=SimpleNamespace(
            returncode=0, stdout="/tmp/selected\n", stderr="")):
        check(m._run_macos_picker("Select") == "/tmp/selected",
              "osascript unit: rc=0 stdout path is returned")
    with patch.object(m.subprocess, "run", return_value=SimpleNamespace(
            returncode=1, stdout="/tmp/selected\n", stderr="some warning")):
        check(m._run_macos_picker("Select") == "/tmp/selected",
              "osascript unit: rc!=0 with absolute stdout path is returned")
    with patch.object(m.subprocess, "run", return_value=SimpleNamespace(
            returncode=1, stdout="", stderr="User canceled.")):
        check(m._run_macos_picker("Select") is None,
              "osascript unit: cancel stderr returns None")
    with patch.object(m.subprocess, "run", return_value=SimpleNamespace(
            returncode=1, stdout="",
            stderr="15:64: execution error: 用户已取消。 (-128)")):
        check(m._run_macos_picker("Select") is None,
              "osascript unit: Chinese cancel stderr returns None")
    with patch.object(m.subprocess, "run", return_value=SimpleNamespace(
            returncode=-128, stdout="", stderr="")):
        check(m._run_macos_picker("Select") is None,
              "osascript unit: rc=-128 returns None")
    with patch.object(m.subprocess, "run", return_value=SimpleNamespace(
            returncode=1, stdout="", stderr="Not authorized")):
        try:
            m._run_macos_picker("Select")
        except RuntimeError as exc:
            check("Not authorized" in str(exc),
                  "osascript unit: error stderr raises RuntimeError")
        else:
            check(False,
                  "osascript unit: error stderr raises RuntimeError")


def main():
    global BASE, PORT, PROC, DATA_ROOT
    PORT = free_port()
    BASE = f"http://127.0.0.1:{PORT}"

    # Run the whole smoke suite against a temporary data root so user data and
    # backend/.data_root.json are never part of the test. The persisted settings
    # file is snapshotted and restored after uvicorn exits.
    temp_base = Path(tempfile.mkdtemp(prefix="ds-stack-smoke-"))
    DATA_ROOT = (temp_base / "data").resolve()
    settings_file = BACKEND / ".data_root.json"
    original_settings = settings_file.read_bytes() if settings_file.is_file() else None

    env = dict(os.environ)
    env["DIFF_DATA_ROOT"] = str(DATA_ROOT)
    print(f"Starting uvicorn subprocess on port {PORT} "
          f"(temporary data root: {DATA_ROOT}) ...")
    PROC = None
    try:
        step_pick_folder_unit()
        PROC = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
            cwd=BACKEND, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        wait_server()
        step_health_devices()
        step_open_properties()
        step_settings()
        step_browse_folders()
        step_chinese_session()
        step_dotted_session()
        step_live_snapshot()
        step_snapshot_folders()
        step_capture()
        step_capture_dir_error()
        step_p3_regressions()
        step_session_files()
        step_processing()
        step_live_error()

        _, close = http_req("POST", "/api/device/close")
        check(close.get("ok") and close.get("data", {}).get("connected") is False,
              "device close succeeds at end of smoke")
        _, r = http_req("GET", "/api/capture/state")
        check(r["data"]["status"] == "finished", "final: state still queryable after device close")
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check(False, f"self test exception: {exc}")
    finally:
        if PROC is not None:
            PROC.terminate()
            try:
                PROC.wait(timeout=5)
            except subprocess.TimeoutExpired:
                PROC.kill()
        # Restore the user's persisted data-root setting exactly as it was.
        try:
            if original_settings is None:
                settings_file.unlink(missing_ok=True)
            else:
                settings_file.write_bytes(original_settings)
        except OSError as exc:  # noqa: BLE001
            print(f"WARN: failed to restore {settings_file}: {exc}")
        shutil.rmtree(temp_base, ignore_errors=True)

    failed = [m for ok, m in RESULTS if not ok]
    print("\n" + "=" * 60)
    print(f"SMOKE result: {len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    if failed:
        print("Failed checks:")
        for m in failed:
            print("  - " + m)
        return 1
    print("All checks passed ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
