# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

### Added

- **Camera parameter snapshot (`camera_props.json` + `CAMPARS`)**: when a capture session starts, the backend reads *every* camera property once (`camera.get_all_properties()`, 164 entries on the DMK 33GX249e) and stores the result as `<session>/camera_props.json` — `{generated_at, device{id,name,serial,kind}, session, props{name: value, …}}` with a flat `props` mapping. A failed read never blocks the acquisition (the file is still written with `props: null` and a warning is logged). Snapshot FITS files point at `<snapshots>/camera_props.json`, created on demand. The per-frame capture path does *not* call `get_all_properties()`; frames only carry a `CAMPARS` pointer card, so a whole session costs one extra property read. `CAMPARS` is relative to its own root (the session directory for raw/calibrated/masters/results, the `snapshots` directory for snapshots) and is `none` for sessions captured before this change. New module: `backend/app/camera_props.py`.
- **Derived products repeat the acquisition settings**: calibrated frames carry `EXPOSURE/GAIN/BLACKLEV/TEMP`, the six ROI cards (`WIDTH/HEIGHT/OFFSETX/OFFSETY/REVERSX/REVERSY`), `CAMERA/SERIAL` and `CAMPARS`; masters only carry `TEMP_MED/TEMP_MIN/TEMP_MAX`, `CAMERA/SERIAL` and `CAMPARS`; `STACK_*.fits` (and the stretched products copied from it) add `NCOMBINE` (frames used) and `NREJECT` (raw frames not used). Continuous settings are aggregated as median plus temperature min/max; when a discrete setting differs between the contributing frames the header raises `MIXED=T` and `results/qc_report.json` gets a `mixed` section listing the frame numbers per distinct value (for example `{"GAIN": {"10.0": [1], "20.0": [2]}}`), appended across re-runs. The `HIERARCH CAM …` cards already present on raw frames are unchanged — 12 cards with the mock camera, 9 on a real ic4 camera (the three `mock_*` properties do not exist there); the "13 acquisition settings" figure counts those 9 camera cards plus the four top-level `EXPOSURE/GAIN/BLACKLEV/TEMP` cards.

- **Capture set persistence**: the Capture page phase table (each phase's `exposure_us` + frame count) is now stored as a backend draft at `<data_root>/capture_draft.json`, so it survives page switches, page refreshes and backend restarts (`GET/PUT /api/capture/draft`). The draft also keeps each phase's `note` (it is written into every frame's FITS `COMMENT`). Named capture sets are stored at `<data_root>/capture_sets.json` with `GET /api/capture/sets` and `GET/PUT/DELETE /api/capture/set?name=<name>`; a set holds only `type`/`count`/`exposure_us` (never `note`). The Capture page gets a saved-set dropdown plus Save As / Overwrite / Delete, and all set actions are disabled while a capture sequence is active. Saving the draft never modifies the saved sets (they are separate files). Contract: `docs/api.md` §2.6.

### Changed

- **FLAT is now a mandatory manual confirmation point**: the `waiting_user` before the FLAT phase is blocking (`needs_user=true`) exactly like LIGHT, so capturing starts only after `POST /api/capture/resume`; BIAS and DARK keep the non-blocking ~0.2s prompt and continue automatically. The resume confirmation message now names the phase being started (`Parameters confirmed, starting FLAT capture` / `... starting LIGHT capture`) instead of always saying LIGHT. `scripts/backend_smoke.py` asserts that FLAT and LIGHT stay blocked (still `waiting_user` 0.6 s after arriving) while BIAS and DARK continue automatically.
- **Capture set endpoints take the name as a query parameter**: `GET/PUT/DELETE /api/capture/sets/{name}` became `GET/PUT/DELETE /api/capture/set?name=<name>`, so names containing `/` (and other URL-special characters) can be saved, read back and deleted unchanged. The list endpoint stays `GET /api/capture/sets`.
- **Capture page locks the phase table while a sequence is active**: the in-row `type` / `count` / `exposure_us` / `note` editors and the `Default` button are now disabled while the capture state is `running`, `waiting_user` or `capturing`, matching the existing lock on Add Phase and row actions. Clearing the saved-set dropdown no longer issues a `GET /api/capture/set` request (which produced a spurious 404 toast).
- **Code comments and Python docstrings are English-only**: comments in `backend/app/`, `frontend/src/` and `scripts/backend_smoke.py` plus all 81 module/class/function docstrings in `backend/app/*.py` and `scripts/*.py` were translated to English (documented parameters, return values, side effects and history are preserved). User-facing strings (UI copy, error messages) and `docs/*.md` remain Chinese.
- **Capture page shows draft read/write failures**: `GET /api/capture/draft` and the debounced `PUT /api/capture/draft` are no longer silent. A corrupted draft file (HTTP 500 naming the file) or any other failure now raises an error toast with the backend message instead of silently falling back to the default template or silently dropping edits. Polling requests (`/api/capture/state`, `/api/sessions`, …) stay silent, so no toast spam is introduced.
- **Popups are centered messages only, and Capture Region reports streaming with an `On Air` tag**: every transient popup is now an antd centered `message`; the Live Preview notice no longer uses the top-right `notification` variant, and no `notification.*` call remains in the frontend. The Live page card is titled `Capture Region` (the `(Width × Height)` suffix is gone) and its title row carries an `On Air` tag whose antd colour encodes the acquisition state — `red` while streaming, `orange` while the acquisition is pending, `default` (grey) when stopped. The in-card alert and the two status lines it used to show were removed, so the card keeps exactly the same height in all three states. Starting the stream shows one centered `warning` to stop the acquisition before changing the capture region (once per start, including when the page opens on an already running stream); stopping shows nothing. On the Capture Plan toolbar the two adjacent `Space` groups were merged into a single wrapping `Space` (uniform 8 px gaps, so Delete and Default are no longer flush against each other) and the default template button is now labelled `Default`.

### Fixed

- **FITS files open upright in DS9 / GIMP / astropy**: every FITS written by the backend (raw frames, snapshots, masters, calibrated frames, `STACK_*.fits`, stretched 16-bit results) is now stored bottom-up, which is the FITS display convention, and carries `ORIENT='FITS-BOTTOM-UP'`. The reader in `backend/app/fits_io.py` flips the data back only when that marker is present, so the in-memory arrays (and therefore the live JPEG preview, snapshot TIFF/PNG and `STACK_preview.png`) keep the previous orientation and the processing results are numerically unchanged. Existing FITS files without the marker are read exactly as before; historical data is not migrated.

- **Damaged capture set/draft files now fail loudly**: a corrupt `<data_root>/capture_sets.json` or `<data_root>/capture_draft.json` (invalid JSON, non-object top level, malformed `sets`/`phases`) makes the related endpoints return 500 with the file name in the message instead of silently serving an empty state. Neither file is overwritten while it is damaged: `GET` never rewrites it and `PUT` validates the existing bytes before writing, so a single "save empty table" cannot destroy saved sets. Restoring valid JSON (or removing the file) recovers the endpoints immediately; a missing file is still a normal empty state that only the first `PUT` creates.

## [1.6.0] - 2026-08-24

### Added

- **Auto Exposure / Auto Gain switches**: `auto_exposure` and `auto_gain` are now first-class bool camera properties. The Live page shows them as compact status switches next to Reverse X / Reverse Y in Camera Parameters, so the current state is visible and both functions can be toggled directly.
- **Sensor readout area renamed**: the Live page card is now titled “Capture Region (Width × Height)” and the backend property labels are `Width`, `Height`, `Offset X`, `Offset Y` instead of ROI-prefixed labels.

### Changed

- **Mock camera sensor ranges**: `width` min/step is now 480/16 px and `height` min/step is now 4/4 px, matching the DMK 33GX249e technical manual; offsets now use a step of 4 px.
- **Histogram**: the live histogram is now computed from the **raw 12-bit frame** returned by `grab_frame()`, using 256 bins adapted to the current frame's actual ADU min/max. It no longer uses the auto-contrast/JPEG preview, so short-exposure images do not collapse into a few misleading bars while still following the visible contrast range. Live stats now also include `histogram_min`/`histogram_max` so the frontend can label the autoscaled ADU range.
- **Live Preview notice**: the dismissible inline alert on the Live page was removed and replaced with an antd popup notification.
- **Manual exposure/gain overrides auto**: on the mock camera, setting `exposure`/`gain` while the corresponding auto function is enabled disables that auto function, mirroring real camera behavior.
- Changelog moved to `docs/CHANGELOG.md`.
- **Run launcher**: `scripts/run.ps1` / `scripts/run.sh` now detect an already-running backend on the chosen port and reuse it instead of exiting with EADDRINUSE/WinError 10048; they also print the actual access URLs including the machine's LAN IPv4 addresses, and warn when the running backend is still serving an older version than the repository. Added `-Stop`/`-Restart` (PowerShell) and `--stop`/`--restart` (bash) to safely stop a confirmed backend and optionally restart it; non-backend port occupants are never killed.
- **Frontend dist is now committed**: `frontend/dist` is no longer git-ignored, so pulling the repository also updates the served frontend without requiring a manual `npm run build`.

### Fixed

- **IC4 auto enum mapping**: `ExposureAuto`/`GainAuto` enum values (`Continuous`/`Off`) are read and written correctly through the bool contract properties.

## [1.5.0] - 2026-08-24

### Added

- **Native OS folder picker**: new backend endpoint `POST /api/pick-folder` opens the server machine's native folder-selection dialog (macOS `osascript`, Windows PowerShell `FolderBrowserDialog`, Linux `zenity`/`kdialog`).
- **Settings → Data Directory**: added a `Choose…` button next to the manual path input that uses the native folder picker.
- **Live → Snapshot (Single Frame)**: `Choose Folder…` now uses the native folder picker; the old in-app `Server Browse…` modal/button was removed.
- Documented the new endpoint in `docs/api.md`.

### Changed

- Settings page card order is now: Data Directory → Preview Parameters (used by /live) → Backend Address → Backend Health.
- Folder-picker cancellation is shown as a warning (`Folder selection cancelled`) instead of an error.
- macOS `osascript` cancellation is now recognized for localized messages (`用户已取消`, `-128`, `cancel`/`cancelled`) and for non-zero exits that still emit a selected path on stdout.

## [1.4.1] - 2026-08-24

### Fixed

#### Backend
- **Calibration stale files**: re-running Calibrate no longer leaves stale `calibrated/*.fits`; Stack now only uses the current calibration run.
- **IC4 reconnect**: opening a disconnected IC4 camera now detects the invalid grabber and actually reopens instead of returning a dead device.
- **Capture error handling**: session directory/meta creation failures now transition the capture state to `error` instead of hanging in `running`.
- **Capture lifecycle**: changing the data root, opening another device, or closing the device during an active capture is now rejected with `409`.
- **Live stream reliability**: `LiveService.start/stop` is serialized to prevent duplicate threads; persistent grab failures stop the stream and expose an error via `/api/live/state` and WebSocket events.
- **IC4 enum-backed booleans**: bool properties backed by enum values (e.g. trigger mode) can now be set correctly.
- **Session names**: dotted names such as `a..b` are now accessible/processable; Windows-invalid trailing dots/spaces are sanitized.
- **Job concurrency**: new processing jobs for a session that already has a pending/running job are rejected with `409`, preventing concurrent writes to the same session.
- **Abort behavior**: `abort()` now transitions state synchronously and prevents an extra frame from being saved after abort.
- **Thumbnail failures**: a failed thumbnail write no longer discards a successfully written FITS frame.
- **Session-name collision**: collision fallback suffix is now reflected consistently in capture state, session list, and `meta.json`.

#### Frontend
- **Processing session isolation**: excluded frames are reset when switching sessions; step gating is scoped per session and falls back to persisted artifacts.
- **Phase display**: `current_phase.index` is rendered as the 1-based phase number, fixing "Phase 2/N" on the first phase.
- **Capture state machine**: stale `waiting_user` events can no longer replace the LIGHT confirmation modal or reopen it after resume.
- **Thumbnails**: no longer falls back to another session's frames; stale in-flight responses are invalidated.
- **Live remount race**: `/api/live/start` and `/api/live/stop` calls are serialized so quick route changes don't leave the page without a stream.
- **Stale processing state**: `selectedResult` is validated/reset on session switch; slow `loadDetail` responses cannot overwrite a newer session selection.
- **Empty session list**: Frame List no longer spins forever when there are no sessions.
- **Unhandled rejections**: user-triggered async handlers now catch API failures instead of producing unhandled promise rejections.
- **Frame count**: processing progress/counts use actual captured frame counts instead of planned counts.

#### Scripts & Docs
- **Installer recovery**: installers now use a `.deps-installed` sentinel and only reuse a virtualenv after dependencies are known-good.
- **PowerShell Python detection**: `install.ps1` now tries Python 3.12 / 3.11 / 3.10 / `py` / `python` / `python3`.
- **Smoke test side effects**: `backend_smoke.py` runs against a temporary data root, restores `backend/.data_root.json`, and leaves no test data behind.
- **Smoke assertions**: PNG snapshot and final device-close are now actually asserted.
- **npm log cleanup**: `install.ps1` removes its temporary npm log after success.
- **Documentation**: stale smoke counts, API contract version references, Chinese-vs-English error wording, architecture details, default capture template counts, and platform-specific README commands were aligned with the implementation.

### Changed
- API job payloads now optionally include `session_id`; session detail includes persisted-artifact flags (`has_masters`, `has_calibrated`).
- Added `GET /api/live/state` for live-stream error/state visibility.
- Backend smoke suite expanded from 226 to 282 checks.
