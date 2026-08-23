# Changelog

All notable changes to this project are documented in this file.

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
