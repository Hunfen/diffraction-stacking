"""ProcessService: calibrate / stack / stretch background jobs (sigma-clipped processing pipeline)."""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

import numpy as np
from PIL import Image

from .fits_io import read_fits, read_fits_data, write_fits
from .util import (ascii_fits_value, auto_contrast_8bit, get_captures_dir,
                   now_iso)

GROUP_TYPES = ("bias", "dark", "flat", "light")
JOB_TYPES = ("calibrate", "stack", "stretch")


def _normalize_exclude(exclude: dict | None) -> dict[str, list[str]]:
    """Normalise exclude into {bias:[], dark:[], flat:[], light:[]} (deduplicated, order preserved)."""
    out = {}
    for g in GROUP_TYPES:
        names = (exclude or {}).get(g) or []
        out[g] = list(dict.fromkeys(str(n) for n in names))
    return out


def _load_group(session_dir: Path, group: str,
                exclude_names: list[str] | None = None) -> list[np.ndarray]:
    group_dir = session_dir / group
    if not group_dir.is_dir():
        return []
    excluded = set(exclude_names or [])
    frames = []
    for p in sorted(group_dir.glob("*.fits")):
        if p.name in excluded:
            continue
        frames.append(np.asarray(read_fits_data(p), dtype=np.float32))
    return frames


def _load_with_headers(session_dir: Path, group: str,
                       exclude_names: list[str] | None = None):
    group_dir = session_dir / group
    out = []
    if not group_dir.is_dir():
        return out
    excluded = set(exclude_names or [])
    for p in sorted(group_dir.glob("*.fits")):
        if p.name in excluded:
            continue
        data, hdr = read_fits(p)
        out.append({"file": p.name, "data": np.asarray(data, dtype=np.float32),
                    "header": dict(hdr)})
    return out


def sigma_clipped_stack(frames: list[np.ndarray], sigma: float, iters: int,
                        method: str = "mean") -> tuple[np.ndarray, float]:
    """sigma-clipped mean/median (per-pixel mu +/- sigma clipping, iterated iters times).

    Returns (stack, rejected_pct).
    """
    if not frames:
        raise RuntimeError("No usable frames to stack")
    data = np.stack(frames, axis=0).astype(np.float64)
    n_frames, ny, nx = data.shape
    first_mean = np.mean(data, axis=0)
    work = data.copy()
    rejected_total = 0.0
    for _ in range(iters):
        center = np.mean(work, axis=0)
        spread = np.std(work, axis=0)
        spread = np.where(spread <= 0, 1e-6, spread)
        mask = np.abs(work - center) > sigma * spread
        rejected_total += float(mask.sum())
        work = np.where(mask, np.nan, work)
        if not np.isnan(work).any():
            break
    if method == "median":
        result = np.nanmedian(work, axis=0)
    else:
        result = np.nanmean(work, axis=0)
    result = np.where(np.isnan(result), first_mean, result)
    rejected_pct = rejected_total * 100.0 / max(n_frames * ny * nx, 1)
    return result.astype(np.float32), rejected_pct


def _mean_of_matching_dark(dark_items: list[dict], exposure: float):
    """sigma-clipped mean of the dark frames whose exposure matches the flat (+/-1 us); None when there is none."""
    match = [it["data"] for it in dark_items
             if abs(float(it["header"].get("EXPOSURE", -999)) - float(exposure)) <= 1.0]
    if not match:
        return None
    stacked, _ = sigma_clipped_stack(match, sigma=3.0, iters=3, method="mean")
    return stacked


def _write_fits(path: Path, arr: np.ndarray, **extra) -> None:
    """Write one float32 FITS through the orientation boundary (see ``fits_io``)."""
    arr = np.asarray(arr, dtype=np.float32)
    write_fits(path, arr, list(extra.items()))


class ProcessService:
    def __init__(self, broadcast=None):
        self.broadcast = broadcast
        self._jobs: dict[str, dict] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------ job management
    def create_job(self, jtype: str, params: dict) -> dict:
        if jtype not in JOB_TYPES:
            raise RuntimeError(f"Unknown job type: {jtype}")
        job_id = "job-" + uuid.uuid4().hex[:8]
        job = {
            "id": job_id,
            "type": jtype,
            "status": "pending",
            "progress": 0,
            "message": "Job queued",
            "result": None,
            "params": params,
            "session_id": params.get("session_id"),
            "created": now_iso(),
        }
        with self._lock:
            session_id = params.get("session_id")
            if session_id:
                for existing in self._jobs.values():
                    if existing.get("session_id") == session_id \
                            and existing.get("status") in ("pending", "running"):
                        raise RuntimeError(
                            "A processing job is already pending or running "
                            "for this session")
            self._jobs[job_id] = job
        self._push(job_id)
        return dict(job)

    def get_job(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return {k: v for k, v in job.items() if k not in ("params",)}

    def _update(self, job_id: str, **fields) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.update(fields)
        self._push(job_id)

    def _push(self, job_id: str) -> None:
        if self.broadcast is None:
            return
        job = self.get_job(job_id)
        if job is None:
            return
        self.broadcast("job", {
            "id": job["id"], "type": job["type"], "status": job["status"],
            "progress": job["progress"], "message": job["message"],
            "session_id": job.get("session_id"),
        })

    # ------------------------------------------------------------ entry point
    def run_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return
        try:
            self._update(job_id, status="running", progress=1,
                         message="Job started")
            if job["type"] == "calibrate":
                result = self._run_calibrate(job_id, job["params"])
            elif job["type"] == "stack":
                result = self._run_stack(job_id, job["params"])
            else:
                result = self._run_stretch(job_id, job["params"])
            self._update(job_id, status="done", progress=100,
                         message="Done", result=result)
        except Exception as exc:  # noqa: BLE001
            self._update(job_id, status="error", message=f"{exc}")

    def _session_dir(self, session_id: str) -> Path:
        if not session_id or session_id in (".", "..") or "/" in session_id \
                or "\\" in session_id:
            raise RuntimeError(f"Invalid session_id: {session_id}")
        d = get_captures_dir() / session_id
        if not d.is_dir():
            raise RuntimeError(f"Session does not exist: {session_id}")
        return d

    # ------------------------------------------------------------ calibrate
    def _run_calibrate(self, job_id: str, params: dict) -> dict:
        sigma = float(params.get("sigma", 3))
        method = str(params.get("method", "mean"))
        if sigma <= 0:
            raise RuntimeError("sigma must be > 0")
        if method not in ("mean", "median"):
            raise RuntimeError("method only supports mean|median")
        session_id = params["session_id"]
        d = self._session_dir(session_id)
        exclude = _normalize_exclude(params.get("exclude"))
        excluded_counts = {g: len(v) for g, v in exclude.items()}

        bias = _load_group(d, "bias", exclude["bias"])
        dark = _load_group(d, "dark", exclude["dark"])
        flat = _load_group(d, "flat", exclude["flat"])
        light_items = _load_with_headers(d, "light", exclude["light"])
        if not light_items:
            raise RuntimeError("light frames are empty, cannot calibrate")
        dark_items = _load_with_headers(d, "dark", exclude["dark"])
        flat_items = _load_with_headers(d, "flat", exclude["flat"])

        (d / "masters").mkdir(exist_ok=True)
        (d / "calibrated").mkdir(exist_ok=True)

        master_bias = master_dark = master_flat = None
        if bias:
            self._update(job_id, progress=15, message=f"Synthesizing master_bias ({len(bias)} frames)")
            master_bias, _ = sigma_clipped_stack(bias, sigma, 3, method)
            _write_fits(d / "masters" / "MASTER_BIAS.fits", master_bias)
        else:
            self._update(job_id, progress=15, message="No bias frames, skipping master_bias")

        if dark:
            self._update(job_id, progress=30, message=f"Synthesizing master_dark ({len(dark)} frames)")
            master_dark, _ = sigma_clipped_stack(dark, sigma, 3, method)
            _write_fits(d / "masters" / "MASTER_DARK.fits", master_dark)
        else:
            self._update(job_id, progress=30, message="No dark frames, skipping master_dark")

        if flat:
            self._update(job_id, progress=45, message=f"Synthesizing master_flat ({len(flat)} frames)")
            flat_mean = np.mean(np.stack(flat, axis=0), axis=0)
            flat_exp = float(flat_items[0]["header"].get("EXPOSURE", 0))
            matching_dark = _mean_of_matching_dark(dark_items, flat_exp)
            if matching_dark is not None:
                flat_mean = flat_mean - matching_dark
                self._update(job_id, progress=50,
                             message="Subtracted matching-exposure dark from flat")
            flat_mean = np.maximum(flat_mean, 0)
            med = float(np.median(flat_mean))
            if med <= 0:
                raise RuntimeError("master_flat median is 0, cannot normalize")
            master_flat = (flat_mean / med).astype(np.float32)
            _write_fits(d / "masters" / "MASTER_FLAT.fits", master_flat)
        else:
            self._update(job_id, progress=45, message="No flat frames, skipping master_flat")

        dark_ref = master_dark if master_dark is not None else 0.0
        flat_ref = master_flat if master_flat is not None else 1.0
        if master_flat is not None:
            # Prevent a zero in a normalized flat from producing a division-by-zero NaN
            flat_ref = np.where(master_flat > 0, master_flat, 1.0).astype(np.float32)

        # A calibration rerun deletes the previous calibrated/*.fits so stale CAL files cannot be
        # globbed again by _run_stack and stacked twice or misaligned (P1 regression).
        cal_dir = d / "calibrated"
        for old in cal_dir.glob("*.fits"):
            try:
                old.unlink()
            except OSError as exc:
                raise RuntimeError(f"Failed to clear stale calibrated file {old.name}: {exc}") from exc

        excluded_light = set(exclude["light"])
        all_light_items = _load_with_headers(d, "light")
        n = 0
        # Number by the original light file order: CAL NFRAME = original index, so stack can exclude by filename
        for orig_idx, item in enumerate(all_light_items):
            if item["file"] in excluded_light:
                continue
            n += 1
            cal = (item["data"] - dark_ref) / flat_ref
            cal = np.nan_to_num(cal, nan=0.0, posinf=0.0, neginf=0.0)
            cal = np.maximum(cal, 0.0).astype(np.float32)
            out = d / "calibrated" / f"CAL_{n:04d}.fits"
            _write_fits(out, cal, SESSION=ascii_fits_value(session_id),
                        NFRAME=orig_idx + 1)
            if n % max(1, len(all_light_items) // 10) == 0:
                self._update(job_id, progress=55 + int(40 * n / max(
                    len(light_items), 1)),
                    message=f"Calibrating light frame {n}/{len(light_items)}")
        return {
            "masters": {
                "bias": f"/api/files/{session_id}/masters/MASTER_BIAS.fits" if master_bias is not None else None,
                "dark": f"/api/files/{session_id}/masters/MASTER_DARK.fits" if master_dark is not None else None,
                "flat": f"/api/files/{session_id}/masters/MASTER_FLAT.fits" if master_flat is not None else None,
            },
            "calibrated_count": n,
            "excluded_counts": excluded_counts,
        }

    # ------------------------------------------------------------ stack
    def _run_stack(self, job_id: str, params: dict) -> dict:
        t0 = time.monotonic()
        sigma = float(params.get("sigma", 3))
        method = str(params.get("method", "mean"))
        registration = bool(params.get("registration", False))
        if sigma <= 0:
            raise RuntimeError("sigma must be > 0")
        if method not in ("mean", "median"):
            raise RuntimeError("method only supports mean|median")
        session_id = params["session_id"]
        d = self._session_dir(session_id)
        exclude = _normalize_exclude(params.get("exclude"))
        excluded_counts = {g: len(v) for g, v in exclude.items()}
        cal_dir = d / "calibrated"
        cal_files = sorted(cal_dir.glob("*.fits")) if cal_dir.is_dir() else []
        if not cal_files:
            raise RuntimeError("calibrated/ is empty, run calibrate first")

        # stack filters light frames only: map the original light filename -> NFRAME and skip that CAL
        excluded_light = set(exclude["light"])
        light_names = sorted(p.name for p in (d / "light").glob("*.fits")) \
            if (d / "light").is_dir() else []
        items = []
        for idx, p in enumerate(cal_files):
            data, hdr = read_fits(p)
            data = np.asarray(data, dtype=np.float32)
            nframe = int(hdr.get("NFRAME", idx + 1))
            light_name = light_names[nframe - 1] \
                if 0 < nframe <= len(light_names) else None
            if light_name is not None and light_name in excluded_light:
                continue
            items.append(data)
        if not items:
            raise RuntimeError("No usable light frames after exclusion, cannot stack")

        total = len(items)
        if registration and total > 1:
            self._update(job_id, progress=15,
                         message="FFT phase-correlation registration (integer pixels)")
            items = self._register(items, job_id, total)

        self._update(job_id, progress=55, message=f"sigma-clipped {method} stacking")
        stacked, rejected_pct = sigma_clipped_stack(items, sigma, 3, method)

        light_items = _load_with_headers(d, "light", exclude["light"])
        total_exposure_us = sum(
            float(it["header"].get("EXPOSURE", 0)) for it in light_items)
        bg = np.asarray(stacked, dtype=np.float64).ravel()
        # Background definition: standard deviation after removing the bright region (above the 75th percentile)
        thr = float(np.percentile(bg, 75))
        background_std = float(np.std(bg[bg <= thr])) if bg.size else 0.0
        n_used = total
        effective_bits = float(np.log2(max(np.sqrt(n_used), 1.0)))
        elapsed_s = time.monotonic() - t0
        qc = {
            "frames": n_used,
            "total_exposure_us": total_exposure_us,
            "effective_bits": round(effective_bits, 3),
            "rejected_pct": round(rejected_pct, 3),
            "background_std": round(background_std, 3),
            "elapsed_s": round(elapsed_s, 3),
            "excluded_counts": excluded_counts,
        }
        (d / "results").mkdir(exist_ok=True)
        sigma_label = str(int(sigma)) if float(sigma).is_integer() else str(sigma)
        stem = (f"STACK_sigma{sigma_label}" if method == "mean"
                else f"STACK_median{sigma_label}")
        out_fits = d / "results" / f"{stem}.fits"
        # v1.1: STACK keeps float32 (BITPIX=-32) without truncation or uint16 conversion
        write_fits(out_fits, np.asarray(stacked, dtype=np.float32),
                   [("SIGMA", sigma), ("METHOD", method), ("REGIS", registration)])
        json.dump(qc, (d / "results" / "qc_report.json").open(
            "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        self._update(job_id, progress=95, message="Writing STACK(float32) and qc_report")
        return {
            "file": out_fits.name,
            "url": f"/api/files/{session_id}/results/{out_fits.name}",
            "preview": None,
            "qc": qc,
            "excluded_counts": excluded_counts,
        }

    def _register(self, items: list[np.ndarray], job_id: str,
                  total: int) -> list[np.ndarray]:
        """Integer-pixel FFT phase-correlation registration (the first frame is the reference)."""
        ref = items[0]
        h, w = ref.shape
        fy = np.fft.fftfreq(h).reshape(-1, 1)
        fx = np.fft.fftfreq(w).reshape(1, -1)
        fft_ref = np.fft.fft2(ref)
        out = [ref]
        for i, img in enumerate(items[1:], start=1):
            fft_img = np.fft.fft2(img)
            cross = fft_ref * np.conj(fft_img)
            norm = np.abs(cross)
            norm = np.where(norm < 1e-9, 1.0, norm)
            corr = np.fft.ifft2(cross / norm)
            corr_abs = np.abs(corr)
            flat_idx = int(np.argmax(corr_abs))
            dy, dx = np.unravel_index(flat_idx, corr_abs.shape)
            if dy > h // 2:
                dy -= h
            if dx > w // 2:
                dx -= w
            out.append(np.roll(img, (dy, dx), axis=(0, 1)))
            if i % max(1, total // 10) == 0:
                self._update(job_id, progress=15 + int(35 * i / total),
                             message=f"Registering {i}/{total} (dy={dy}, dx={dx})")
        return out

    # ------------------------------------------------------------ stretch
    def _run_stretch(self, job_id: str, params: dict) -> dict:
        session_id = params["session_id"]
        d = self._session_dir(session_id)
        result_file = str(params.get("result_file", ""))
        if not result_file or Path(result_file).name != result_file:
            raise RuntimeError("result_file must be a file name under results/")
        src = d / "results" / result_file
        if not src.is_file():
            raise RuntimeError(f"Result file does not exist: results/{result_file}")
        stretch = str(params.get("stretch", "asinh"))
        if stretch not in ("linear", "asinh", "log"):
            raise RuntimeError("stretch only supports linear|asinh|log")
        black = float(params.get("black", 0))
        white = float(params.get("white", 4095))
        if white <= black:
            raise RuntimeError("white must be greater than black")

        self._update(job_id, progress=20, message=f"Reading {result_file}")
        data = np.asarray(read_fits_data(src), dtype=np.float32)
        self._update(job_id, progress=45, message=f"Applying {stretch} stretch")

        v = (data - black) / (white - black)
        if stretch == "linear":
            stretched = np.clip(v, 0.0, 1.0)
        elif stretch == "log":
            stretched = np.clip(
                np.log1p(np.maximum(v, 0.0)) / np.log(2.0), 0.0, 1.0)
        else:  # asinh: soft slope a = 1/3 of the range
            a = 1.0 / 3.0
            stretched = np.clip(np.arcsinh(np.maximum(v, 0.0) / a)
                                / np.arcsinh(1.0 / a), 0.0, 1.0)

        img16 = (stretched * 65535.0 + 0.5).astype(np.uint16)
        stem = Path(result_file).stem
        (d / "results").mkdir(exist_ok=True)
        out_name = f"{stem}_{stretch}.fits"
        write_fits(d / "results" / out_name, img16,
                   [("STRETCH", stretch), ("BLACK", black), ("WHITE", white)])
        # 8-bit PNG preview (take the high 8 bits of the 16-bit stretched result)
        preview8 = (img16 >> 8).astype(np.uint8)
        Image.fromarray(preview8, mode="L").save(
            d / "results" / "STACK_preview.png")
        self._update(job_id, progress=90, message="Writing preview and 16-bit FITS result")
        return {
            "file": out_name,
            "url": f"/api/files/{session_id}/results/{out_name}",
            "preview_url": f"/api/files/{session_id}/results/STACK_preview.png",
        }
