#!/usr/bin/env python3
"""端到端自测（Mock 相机路径）。

用法：cd backend && .venv/bin/python ../scripts/backend_smoke.py
退出码：0 = 全部通过；1 = 有失败项。
"""
from __future__ import annotations

import asyncio
import http.client
import json
import os
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
BASE = None
PORT = None
PROC = None
RESULTS = []
SESSION_ID = None


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
                      f"{method} {path} -> HTTP {status}（期望 {expect}）")
            return status, payload
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            payload = raw
        if expect is not None:
            check(e.code == expect,
                  f"{method} {path} -> HTTP {e.code}（期望 {expect}）")
        return e.code, payload


def raw_request(path: str) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=15)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, body


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
            raise RuntimeError(f"uvicorn 提前退出（code={PROC.returncode}）：\n{out}")
        time.sleep(0.3)
    raise RuntimeError(f"服务未在 {timeout}s 内就绪：{last}")


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
    check(status == 200 and h.get("ok"), "GET /api/health 返回 ok 包裹")
    d = h.get("data") or {}
    check(d.get("status") == "ok", "health.status == ok")
    check(d.get("camera_driver") == "mock", "health.camera_driver == mock（macOS）")

    status, payload = http_req("GET", "/api/devices")
    devs = payload.get("data") if payload.get("ok") else []
    check(status == 200 and isinstance(devs, list), "GET /api/devices data 为数组")
    mock = [x for x in devs if x.get("id") == "mock-1"]
    check(bool(mock) and mock[0]["kind"] == "mock", "设备列表包含 mock-1")

    status, payload = http_req("POST", "/api/device/open", {"id": "no-such-device"},
                               expect=404)
    check(not payload.get("ok") and isinstance(payload.get("error"), str),
          "错误统一包裹（ok=false + error 字符串 + 404）")

    status, root_payload = http_req("GET", "/")
    root_ok = status == 200 and (
        (isinstance(root_payload, dict) and root_payload.get("ok")
         and root_payload["data"].get("frontend_built") is False)
        or isinstance(root_payload, (bytes, bytearray)))
    check(root_ok, "GET / 返回前端未构建提示（或已构建时的首页）")


def step_open_properties():
    status, payload = http_req("POST", "/api/device/open", {"id": "mock-1"})
    check(status == 200 and payload.get("ok") and payload["data"]["connected"] is True,
          "打开 mock-1")

    _, info = http_req("GET", "/api/device/info")
    check(info.get("ok") and info["data"]["connected"] is True
          and len(info["data"]["properties"]) > 0, "device/info 含属性列表")

    _, props_payload = http_req("GET", "/api/properties")
    props = {p["name"]: p for p in props_payload["data"]["properties"]}
    check("exposure" in props and props["exposure"]["min"] == 30
          and props["exposure"]["max"] == 30000000, "exposure 范围 30–30000000 us")
    check(props["width"]["value"] == 1024 and props["height"]["value"] == 768,
          "默认 ROI 1024×768")
    check("mock_pattern" in props and "leed" in props["mock_pattern"].get("options", []),
          "mock_pattern 枚举（leed/rheed）")

    _, r = http_req("PUT", "/api/properties/exposure", {"value": 3000})
    check(r.get("ok") and r["data"]["actual"] == 3000, "设置 exposure=3000 生效")
    _, r = http_req("PUT", "/api/properties/gain", {"value": 1.5})
    check(r.get("ok") and abs(r["data"]["actual"] - 1.5) < 1e-6, "设置 gain=1.5")
    _, r = http_req("PUT", "/api/properties/black_level", {"value": 5})
    check(r.get("ok") and r["data"]["actual"] == 5, "设置 black_level=5")
    _, r = http_req("PUT", "/api/properties/exposure", {"value": 999999999})
    check(r.get("ok") and r["data"]["actual"] == 30000000, "越界值自动钳制到 max")
    http_req("PUT", "/api/properties/exposure", {"value": 3000})
    # 触发未分类相机错误（NaN → round/int 内部 ValueError）：
    # 500 必须统一“服务器内部错误”，不得带异常文本
    _, r500 = http_req("PUT", "/api/properties/exposure", {"value": "nan"},
                       expect=500)
    err = (r500 or {}).get("error", "")
    check(not r500.get("ok") and err == "服务器内部错误"
          and "nan" not in err.lower() and "ValueError" not in err
          and "Traceback" not in err,
          "未分类相机错误 500 不泄漏异常细节")
    _, r = http_req("PUT", "/api/properties/mock_drift", {"value": True})
    check(r.get("ok") and r["data"]["actual"] is True, "开启 mock 慢漂移")


def step_live_snapshot():
    status, r = http_req("POST", "/api/live/start",
                         {"fps": 10, "scale": 0.5, "quality": 70})
    check(status == 200 and r.get("ok") and r["data"]["streaming"] is True,
          "live/start 成功")

    try:
        stats_text, jpeg = asyncio.run(recv_live_pair())
    except Exception as exc:  # noqa: BLE001
        check(False, f"/ws/live 收帧失败：{exc}")
        stats_text, jpeg = None, None
    if stats_text is not None:
        stats = json.loads(stats_text)
        keys_ok = all(k in stats for k in
                      ("min", "max", "mean", "std", "saturated_pct", "fps",
                       "histogram"))
        check(keys_ok, "live 统计字段齐全（min/max/mean/std/sat/fps/histogram）")
        check(isinstance(stats.get("histogram"), list)
              and len(stats["histogram"]) == 256
              and all(isinstance(v, int) for v in stats["histogram"]),
              "histogram 为 256 个 int")
        check(isinstance(jpeg, (bytes, bytearray)) and jpeg[:2] == b"\xff\xd8",
              "紧随统计的二进制消息为 JPEG（0xFFD8）")

    _, r = http_req("POST", "/api/live/stop")
    check(r.get("ok") and r["data"]["streaming"] is False, "live/stop 成功")

    _, snap = http_req("POST", "/api/snapshot", {"format": "fits"})
    check(snap.get("ok") and snap["data"]["url"].startswith("/api/files/snapshots/"),
          "snapshot fits 返回 file/url")
    if snap.get("ok"):
        status, body = raw_request(snap["data"]["url"])
        check(status == 200 and body[:6] == b"SIMPLE", "快照 FITS 可下载")
        try:
            from astropy.io import fits
            with fits.open(REPO / "data" / "snapshots" / snap["data"]["file"]) as hdul:
                snap_hdr = hdul[0].header
            for key in ("EXPOSURE", "GAIN", "BLACKLEV", "TEMP",
                        "FRAMETYP", "CAMERA", "SERIAL", "SESSION", "NFRAME"):
                check(key in snap_hdr, f"快照 FITS 头含 {key}")
            check(snap_hdr.get("FRAMETYP") == "SNAPSHOT",
                  "快照 FRAMETYP == SNAPSHOT")
            check(snap_hdr.get("SESSION") == "SNAPSHOT" and snap_hdr.get("NFRAME") == 1,
                  "快照 SESSION=SNAPSHOT、NFRAME=1")
        except Exception as exc:  # noqa: BLE001
            check(False, f"快照 FITS 头校验异常：{exc}")
    _, snap_png = http_req("POST", "/api/snapshot", {"format": "png"})
    if snap_png.get("ok"):
        status, body = raw_request(snap_png["data"]["url"])
        check(status == 200 and body[:4] == b"\x89PNG", "快照 PNG 可下载")

    # 快照保存失败路径：500 统一“服务器内部错误”，不泄漏 PermissionError 等细节
    snap_dir = REPO / "data" / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    orig_mode = stat.S_IMODE(snap_dir.stat().st_mode)
    try:
        os.chmod(snap_dir, 0o555)
        _, r500 = http_req("POST", "/api/snapshot", {"format": "fits"},
                           expect=500)
        err = (r500 or {}).get("error", "")
        check(not r500.get("ok") and err == "服务器内部错误"
              and "PermissionError" not in err and "Traceback" not in err
              and "Permission" not in err,
              "快照保存失败 500 不泄漏异常细节")
    finally:
        os.chmod(snap_dir, orig_mode)


def step_capture():
    global SESSION_ID
    phases = [
        {"type": "bias", "count": 3, "exposure_us": 30, "note": "smoke bias"},
        {"type": "dark", "count": 3, "exposure_us": None, "note": "smoke dark"},
        {"type": "flat", "count": 3, "exposure_us": None, "note": "smoke flat"},
        {"type": "light", "count": 3, "exposure_us": None, "note": "smoke light"},
    ]
    status, bad_plan = http_req("POST", "/api/capture/plan",
                                {"session_name": "EMPTY", "phases": []},
                                expect=400)
    check(status == 400 and not bad_plan.get("ok"), "空 phases 的 plan 返回 400")

    status, plan = http_req("POST", "/api/capture/plan",
                            {"session_name": "SMOKE_LEED_001", "phases": phases})
    check(status == 200 and plan.get("ok") and plan["data"].get("plan_id"),
          "capture/plan 返回 plan_id")
    echoed = plan["data"]["phases"]
    check(len(echoed) == 4 and echoed[0]["type"] == "bias"
          and echoed[1]["exposure_us"] == 3000, "plan 回显并解析 null 曝光为当前值")

    status, r = http_req("POST", "/api/capture/start")
    check(status == 200 and r.get("ok") and r["data"]["status"] == "running",
          "capture/start 成功")

    # v1.1 状态机：非 LIGHT 相位 waiting_user(needs_user=false) → 自动 capturing
    bias_wait = poll_state(
        lambda s: s.get("status") == "waiting_user"
        and (s.get("current_phase") or {}).get("type") == "bias",
        timeout=10, interval=0.01)
    check(bias_wait.get("status") == "waiting_user"
          and bias_wait.get("needs_user") is False,
          "bias 相位进入 waiting_user 且 needs_user=false")
    _, rr = http_req("POST", "/api/capture/resume", expect=409)
    check(not rr.get("ok"), "非 LIGHT 的 waiting_user 中 resume 返回 409")
    bias_cap = poll_state(
        lambda s: s.get("status") == "capturing"
        and (s.get("current_phase") or {}).get("type") == "bias",
        timeout=10, interval=0.01)
    check(bias_cap.get("status") == "capturing" and bias_cap.get("needs_user") is False,
          "bias 自动进入 capturing（needs_user=false）")

    state = poll_state(
        lambda s: s.get("status") == "waiting_user"
        and (s.get("current_phase") or {}).get("type") == "light", timeout=60)
    check(state.get("status") == "waiting_user"
          and state["current_phase"]["type"] == "light",
          "LIGHT 前进入 waiting_user（强制人工确认点）")
    check(state.get("needs_user") is True, "LIGHT 的 waiting_user needs_user=true")
    check("直方图" in (state.get("prompt") or ""), "LIGHT prompt 含直方图确认文案")
    check(state["current_phase"]["index"] == 4 and state["total_frames"] == 12,
          "状态：第 4/4 相位，共 12 帧")
    check(any("BIAS" in e["msg"] for e in state.get("log", [])), "日志含 BIAS 完成记录")

    # 拍摄进行中提交处理任务应 409（会话目录此时已创建）
    _, sess_now = http_req("GET", "/api/sessions")
    cur = next((s for s in sess_now["data"]
                if s["name"] == "SMOKE_LEED_001"), None)
    if cur:
        _, r409 = http_req("POST", "/api/process/calibrate",
                           {"session_id": cur["id"]}, expect=409)
        check(not r409.get("ok") and "拍摄进行中" in r409.get("error", ""),
              "拍摄中提交 calibrate 返回 409")

    # waiting_user 期间实时预览可用（人工对照直方图的场景）
    http_req("POST", "/api/live/start", {"fps": 10, "scale": 0.25, "quality": 60})
    try:
        stats_text, _ = asyncio.run(recv_live_pair())
        check(bool(stats_text), "waiting_user 期间 /ws/live 有帧")
    except Exception as exc:  # noqa: BLE001
        check(False, f"waiting_user 期间收帧失败：{exc}")
    http_req("POST", "/api/live/stop")

    # /ws/events：resume 后应推送 capture 状态变化
    async def resume_and_listen():
        import websockets
        uri = f"ws://127.0.0.1:{PORT}/ws/events"
        async with websockets.connect(uri, open_timeout=10) as ws:
            snapshot = json.loads(await asyncio.wait_for(ws.recv(), 10))
            assert snapshot.get("topic") == "capture"
            # resume 触发状态推送
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
        check(False, f"/ws/events 或 resume 失败：{exc}")
        event_msg = None
        http_req("POST", "/api/capture/resume")
    check(event_msg is not None and event_msg["topic"] == "capture",
          "/ws/events 推送 capture 状态变化（resume→capturing）")
    if event_msg:
        check(event_msg["data"].get("needs_user") is False,
              "/ws/events capture 消息含 needs_user=false（已 resume）")
    _, r_again = http_req("POST", "/api/capture/resume", expect=409)
    check(not r_again.get("ok"), "capturing 中再次 resume 返回 409")

    state = poll_state(lambda s: s.get("status") == "finished", timeout=90)
    check(state.get("status") == "finished", "拍摄完成（finished）")
    check(state.get("frames_done") == 12, "frames_done == 12")
    # 从 sessions 列表取 id
    _, sess = http_req("GET", "/api/sessions")
    sids = [s["id"] for s in sess["data"] if s["name"] == "SMOKE_LEED_001"]
    check(bool(sids), "sessions 列表包含本次会话")
    if sids:
        SESSION_ID = sids[0]
        target = next(s for s in sess["data"] if s["id"] == SESSION_ID)
        fc = target["frame_counts"]
        check(fc == {"bias": 3, "dark": 3, "flat": 3, "light": 3},
              "frame_counts 各相位 3 帧")


def step_session_files():
    _, detail = http_req("GET", f"/api/sessions/{SESSION_ID}")
    d = detail["data"]
    check(len(d["phases"]) == 4, "session 详情含 4 个 phase")
    for g in ("bias", "dark", "flat", "light"):
        frames = d["frames"].get(g, [])
        check(len(frames) == 3, f"frames.{g} == 3")
        if frames:
            f0 = frames[0]
            check(all(k in f0 for k in ("file", "url", "thumb", "mean", "std",
                                        "saturated_pct")), f"{g} 帧字段齐全")
            check(isinstance(f0["mean"], (int, float)), f"{g} 帧含 mean 统计")
    f0 = d["frames"]["bias"][0]
    status, body = raw_request(f0["url"])
    check(status == 200 and body[:6] == b"SIMPLE", "FITS 文件可下载（SIMPLE 头）")
    try:
        from astropy.io import fits
        with fits.open(REPO / "data" / "captures" / SESSION_ID
                       / "bias" / f0["file"]) as hdul:
            hdr = hdul[0].header
            data = hdul[0].data
        required_keys = ("DATE-OBS", "EXPOSURE", "GAIN", "BLACKLEV",
                         "TEMP", "FRAMETYP", "CAMERA", "SERIAL",
                         "SESSION", "NFRAME")
        check(all(k in hdr for k in required_keys),
              "FITS 头含契约要求的 10 个关键字")
        check(hdr.get("BITPIX") == 16 and int(data.max()) <= 4095,
              "FITS 为 16-bit 且数据在低 12 位（max ≤ 4095）")
        check("." in str(hdr.get("DATE-OBS", "")),
              "DATE-OBS 精确到毫秒（含小数秒）")
    except Exception as exc:  # noqa: BLE001
        check(False, f"FITS 头校验异常：{exc}")
    status, body = raw_request(f0["thumb"])
    check(status == 200 and body[:4] == b"\x89PNG", "缩略图 PNG 可下载")
    status, body = raw_request(f"/api/files/{SESSION_ID}/meta.json")
    check(status == 200 and json.loads(body).get("name") == "SMOKE_LEED_001",
          "meta.json 可下载")

    status, body = raw_request(f"/api/files/{SESSION_ID}/bias/../meta.json")
    check(status == 400, "路径穿越（bias/../meta.json）被拒绝为 400")
    status, body = raw_request(f"/api/files/../../etc/passwd")
    check(status in (400, 404), "非法 session_id 被拒绝")


def step_processing():
    # 排除不存在的文件 → 400（中文错误）
    status, bad = http_req("POST", "/api/process/calibrate",
                           {"session_id": SESSION_ID, "sigma": 3, "method": "mean",
                            "exclude": {"light": ["NOPE_0001.fits"]}},
                           expect=400)
    check(status == 400 and not bad.get("ok")
          and "不存在" in bad.get("error", ""),
          "exclude 不存在的文件返回 400（中文错误）")

    _, r = http_req("POST", "/api/process/calibrate",
                    {"session_id": SESSION_ID, "sigma": 3, "method": "mean"})
    job_id = r["data"]["job_id"]
    job = poll_job(job_id)
    check(job["status"] == "done", f"calibrate 完成（{job.get('message')}）")
    check(job.get("progress") == 100, "calibrate progress == 100")
    masters = (job.get("result") or {}).get("masters", {})
    check(all(masters.get(k) for k in ("bias", "dark", "flat")),
          "master bias/dark/flat 全部生成")
    check((job.get("result") or {}).get("calibrated_count") == 3,
          "calibrated_count == 3")
    excl = (job.get("result") or {}).get("excluded_counts") or {}
    check(excl == {"bias": 0, "dark": 0, "flat": 0, "light": 0},
          "calibrate result 含 excluded_counts（全 0）")

    # masters/ 目录文件可直接访问
    status, body = raw_request(f"/api/files/{SESSION_ID}/masters/MASTER_DARK.fits")
    check(status == 200 and body[:6] == b"SIMPLE",
          "/api/files 可访问 masters/MASTER_DARK.fits")

    # v1.1 exclude：stack 排除一张 light（数组含重复，后端应去重）后 frames=2
    _, r = http_req("POST", "/api/process/stack",
                    {"session_id": SESSION_ID, "sigma": 3, "method": "mean",
                     "registration": True,
                     "exclude": {"bias": [], "dark": [], "flat": [],
                                 "light": ["LIGHT_0003.fits",
                                           "LIGHT_0003.fits"]}})
    job_id = r["data"]["job_id"]
    job = poll_job(job_id)
    check(job["status"] == "done", f"stack 完成（{job.get('message')}）")
    res = job.get("result") or {}
    check(res.get("file") == "STACK_sigma3.fits", "stack 输出 STACK_sigma3.fits")
    qc = res.get("qc") or {}
    for k in ("frames", "total_exposure_us", "effective_bits", "rejected_pct",
              "background_std", "excluded_counts"):
        check(k in qc, f"qc_report 含 {k}")
    check(qc.get("frames") == 2 and qc.get("total_exposure_us") == 6000,
          "qc：排除 1 张 light 后 frames=2、total_exposure_us=6000")
    check((qc.get("excluded_counts") or {}).get("light") == 1,
          "qc.excluded_counts.light == 1")
    check((res.get("excluded_counts") or {}).get("light") == 1,
          "stack result.excluded_counts.light == 1")

    # STACK 为 float32（BITPIX=-32）
    try:
        from astropy.io import fits
        with fits.open(REPO / "data" / "captures" / SESSION_ID
                       / "results" / "STACK_sigma3.fits") as hdul:
            stack_bitpix = hdul[0].header.get("BITPIX")
            stack_data = hdul[0].data
        check(stack_bitpix == -32 and np.dtype(stack_data.dtype).kind == "f"
              and stack_data.dtype.itemsize == 4,
              "STACK_sigma3.fits 为 float32（BITPIX=-32）")
    except Exception as exc:  # noqa: BLE001
        check(False, f"STACK 精度校验异常：{exc}")

    _, r = http_req("POST", "/api/process/stretch",
                    {"session_id": SESSION_ID, "result_file": "STACK_sigma3.fits",
                     "stretch": "asinh", "black": 0, "white": 4095})
    job_id = r["data"]["job_id"]
    job = poll_job(job_id)
    check(job["status"] == "done", f"stretch 完成（{job.get('message')}）")
    sres = job.get("result") or {}
    check("asinh" in sres.get("file", ""), "stretch 输出 16-bit FITS 结果")
    if sres.get("preview_url"):
        status, body = raw_request(sres["preview_url"])
        check(status == 200 and body[:4] == b"\x89PNG", "STACK_preview.png 可下载")
    if sres.get("url"):
        status, body = raw_request(sres["url"])
        check(status == 200 and body[:6] == b"SIMPLE", "拉伸结果 FITS 可下载")

    _, detail = http_req("GET", f"/api/sessions/{SESSION_ID}")
    d = detail["data"]
    check(len(d["results"]) >= 2, "session 详情 results ≥ 2（stack + 拉伸结果）")
    check(isinstance(d["qc"], dict) and d["qc"].get("frames") == 2,
          "session 详情返回 qc_report（frames=2，含 excluded_counts）")


def main():
    global BASE, PORT, PROC
    PORT = free_port()
    BASE = f"http://127.0.0.1:{PORT}"
    print(f"启动 uvicorn 子进程（端口 {PORT}）…")
    PROC = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        cwd=BACKEND, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        wait_server()
        step_health_devices()
        step_open_properties()
        step_live_snapshot()
        step_capture()
        step_session_files()
        step_processing()

        http_req("POST", "/api/device/close")
        _, r = http_req("GET", "/api/capture/state")
        check(r["data"]["status"] == "finished", "收尾：关闭设备后状态仍可查询")
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check(False, f"自测异常：{exc}")
    finally:
        PROC.terminate()
        try:
            PROC.wait(timeout=5)
        except subprocess.TimeoutExpired:
            PROC.kill()

    failed = [m for ok, m in RESULTS if not ok]
    print("\n" + "=" * 60)
    print(f"SMOKE 结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    if failed:
        print("失败项：")
        for m in failed:
            print("  - " + m)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
