# v1.4 Snapshot Folder Picker — Acceptance Review

- **Task:** t3 — snapshot-picker-review
- **Attempt ID:** 0823602a-3701-4464-8124-0455c054c5f7
- **Reviewer:** reviewer (independent acceptance)
- **Date:** 2026-08-22
- **Repo:** `/Users/hunfen/Documents/GitHub/diffraction_stacking` (working tree, **not committed**)
- **Verdict:** ✅ **PASS** — backend, frontend build/lint, static review, and headless-Chrome E2E all green.

## Scope

Acceptance of contract v1.4 (snapshot folder selection via server-side directory browsing):
`GET /api/browse/folders`, `POST /api/snapshot` with `folder` (absolute path inside data root) + `prefix`,
filenames always carrying `<YYYYMMDDTHH-MM-SS>` timestamps, and the rewritten English snapshot card.

## 1. Backend

### 1.1 Automated smoke

```text
cd backend && .venv/bin/python ../scripts/backend_smoke.py
SMOKE result: 226/226 passed
All checks passed ✅
```

Exit code **0**. Smoke covers the new v1.4 cases: browse default/subfolder/parent/out-of-bounds,
absolute-folder snapshots, relative single-segment compatibility, prefix echo/sanitize,
`<prefix>_<ts>.fits` / `<ts>.fits` naming, traversal → 400, and snapshot URL/download behavior.

### 1.2 Manual API checks

Fresh server: `DIFF_DATA_ROOT=/tmp/t3_data_root uvicorn app.main:app` (mock camera), port 8079.

| # | Check | Expected | Observed | Result |
|---|-------|----------|----------|--------|
| 1 | `GET /api/browse/folders` (no path) | data_root, `parent: null`, direct subfolders only, sorted | `path=/private/tmp/t3_data_root`, `parent=null`, `files=[]`, folders `Alpha,Beta,EmptyDir,captures,snapshots` (root file excluded) | ✅ PASS |
| 2 | `GET /api/browse/folders?path=…/Beta` | `path=Beta`, `parent=data_root`, subfolders | `parent=/private/tmp/t3_data_root`, folders `Deep,Shallow` | ✅ PASS |
| 3 | browse outside data root (`/etc`) | 400 English | `400 {"error":"Folder must be inside the data root"}` | ✅ PASS |
| 4 | browse relative path (`Alpha`) | 400 English | same as #3 | ✅ PASS |
| 5 | browse nonexistent path | 400 English | same as #3 | ✅ PASS |
| 6 | browse `..` traversal | 400 English | same as #3 | ✅ PASS |
| 7 | `POST /api/snapshot` absolute folder + `prefix:"spot"` | `spot_<ts>.fits` | `file=spot_20260822T19-20-15.fits`, `url=/api/files/snapshots/Beta/Deep/…`, `folder` absolute, `prefix:"spot"` | ✅ PASS |
| 8 | absolute folder, no prefix | `<ts>.fits` | `file=20260822T19-20-15.fits`, `prefix:""` | ✅ PASS |
| 9 | relative single-segment `"测试目录"` | maps to `data_root/snapshots/测试目录` | `folder=/private/tmp/t3_data_root/snapshots/测试目录`, `rel_<ts>.fits` on disk | ✅ PASS |
| 10 | default folder (no `folder`) | `data_root/snapshots` | `folder=…/snapshots`, `<ts>.fits` | ✅ PASS |
| 11 | traversal `folder=…/../..` | 400 English | `Folder must be inside the data root` | ✅ PASS |
| 12 | traversal `folder=…/Beta/../../etc` | 400 English | `Folder must be inside the data root` | ✅ PASS |
| 13 | `folder=/etc` | 400 English | `Folder must be inside the data root` | ✅ PASS |
| 14 | relative with separator `../evil` | 400 English | `folder must be an absolute path inside the data root or a single-segment relative name` | ✅ PASS |
| 15 | relative `..` | 400 English | `folder must not contain . or .. segments` | ✅ PASS |
| 16 | bad format `raw` | 400 English | `format only supports fits|tiff|png` | ✅ PASS |
| 17 | illegal prefix `a b/c:d*e?f<g>h|i"j` | sanitized, no separators/whitespace | `prefix=a_b_c_d_e_f_g_h_i_j`, file `a_b_c_d_e_f_g_h_i_j_<ts>.fits` | ✅ PASS |

All error messages are English; all file names carry the timestamp; files were verified on disk.

## 2. Frontend

### 2.1 Build & lint

```text
cd frontend && npm run build   → exit 0 (tsc -b + vite build, ✓ 3602 modules, 0 errors)
cd frontend && npm run lint    → exit 0 (oxlint: Found 0 warnings and 0 errors)
```

### 2.2 Static review (`frontend/src/pages/LivePage.tsx`, `frontend/src/api/types.ts`)

| Requirement | Where | Result |
|-------------|-------|--------|
| `Choose Folder…` button | `LivePage.tsx` L923 | ✅ PASS |
| Modal opens and fetches `/api/browse/folders` with no path → data_root | `openFolderPicker` / `fetchBrowseFolders` L622–644 | ✅ PASS |
| Folder list: folder icon + name, click enters with `?path=` | modal L1103–1111 | ✅ PASS |
| Current path shown at top of modal | modal L1093–1095 (`Typography.Text code`) | ✅ PASS |
| `Up` button calls `parent`, disabled when `parent === null` | L1073–1077 | ✅ PASS |
| `Use This Folder` confirms + closes | L1088, `useCurrentFolder` L651 | ✅ PASS |
| Backend English error shown in modal Alert | `browseErrorMessage` L70–79 + L1096 | ✅ PASS |
| Selected absolute path shown under button (`Typography.Text secondary`) | L928–931 | ✅ PASS |
| Default = localStorage `ds.snapshot.folder`, else `GET /api/settings` data_root + `/snapshots` | L453–454, L605–620 | ✅ PASS |
| Prefix input, placeholder `Prefix (optional)` | L936–940 | ✅ PASS |
| Filename format hint `<prefix>_<timestamp>.ext` / `<timestamp>.ext` | L948 | ✅ PASS |
| POST body `{format, folder, prefix}` with selected folder | L665–669 | ✅ PASS |
| folder + prefix persisted to `ds.snapshot.folder` / `ds.snapshot.prefix` (input and on success) | L653, L672–677, L940 | ✅ PASS |
| Result shows `file`, folder, download link | L972–984 | ✅ PASS |
| Types v1.4: `BrowseFolderItem/BrowseFoldersData`, `SnapshotResult.folder/prefix` | `types.ts` L82–105 | ✅ PASS |
| UI copy English | entire snapshot card/modal | ✅ PASS |

## 3. Headless Chrome E2E (`channel:'chrome'`, playwright-core in `/tmp/pwtest`)

Script: `/tmp/pwtest/t3_check.js`, backend on `http://127.0.0.1:8079`, mock device opened **before** page load (real user flow: device opened on Connect page first).

Flow: `/live` → snapshot card → `Choose Folder…` → modal opens at data_root (Up disabled) →
`Beta` (Up enabled) → `Deep` → `Use This Folder` → selected path displayed →
prefix `e2e_t3` → `Snapshot` → result file `e2e_t3_20260822T19-25-20.fits`.

| # | Check | Result |
|---|-------|--------|
| 1 | Device open via API → 200 | ✅ PASS |
| 2 | Snapshot card has `Choose Folder` button | ✅ PASS |
| 3 | Default folder shows `<data_root>/snapshots` (fresh profile, no localStorage) | ✅ PASS |
| 4 | Prefix hint shows `<prefix>_<timestamp>.ext` format | ✅ PASS |
| 5 | Modal title English (`Choose Snapshot Folder`) | ✅ PASS |
| 6 | `Up` disabled at data_root top (`parent=null`) | ✅ PASS |
| 7 | `Use This Folder` + `Cancel` present | ✅ PASS |
| 8 | Modal opens at data_root path | ✅ PASS |
| 9 | `Up` enabled after entering subfolder | ✅ PASS |
| 10 | Path shows `/Beta` after click | ✅ PASS |
| 11 | Path shows `/Beta/Deep` after click | ✅ PASS |
| 12 | Confirmed path displayed under button | ✅ PASS |
| 13 | `localStorage['ds.snapshot.folder']` = chosen absolute path | ✅ PASS |
| 14 | Result file name = `e2e_t3_<ts>.fits` (prefix + timestamp) | ✅ PASS |
| 15 | `localStorage['ds.snapshot.prefix']` = `e2e_t3` | ✅ PASS |
| 16 | POST body = `{format:'fits', folder:'/private/tmp/t3_data_root/Beta/Deep', prefix:'e2e_t3'}` | ✅ PASS |
| 17 | No Chinese glyphs rendered on page | ✅ PASS |
| 18 | Console errors = 0 | ✅ PASS |
| 19 | Page errors = 0 | ✅ PASS |
| 20 | Failed requests = 0 | ✅ PASS |

**20/20 PASS, script exit 0.**

### Screenshots

- `docs/reviews/snapshot-picker-e2e-01-modal-root.png` — modal open at data_root, Up disabled
- `docs/reviews/snapshot-picker-e2e-02-beta.png` — after entering `Beta`
- `docs/reviews/snapshot-picker-e2e-03-deep.png` — after entering `Beta/Deep`
- `docs/reviews/snapshot-picker-e2e-04-result.png` — confirmed path + prefix + snapshot result card

## Observations / Notes

1. Non-blocking: if `/live` is opened **before** a device is opened, the app's silent
   `/api/properties` poll gets a 409 and the browser logs one
   `Failed to load resource … 409` console entry (handled in-app, no visible error).
   In the required flow (device opened first, as a user normally does) console is clean — **0 errors**.
2. Browse listing excludes files (only direct subfolders), matching contract v1.4.
3. Prefix sanitization replaces separators/illegal chars/whitespace with `_` and allows empty;
   empty prefix yields `<ts>.<ext>`.
4. `GET /api/snapshot/folders` legacy endpoint still works (smoke v1.3 cases pass).
5. Nothing was committed; only this report and the four E2E screenshots were added
   (untracked) in `docs/reviews/`.

## Verdict

**PASS** — all required checks green; no defects found in v1.4 snapshot folder picking.
