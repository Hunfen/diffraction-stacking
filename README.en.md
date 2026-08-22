# Static AstroPhoto

**Static AstroPhoto** is acquisition and stacking software for **static LEED / RHEED electron diffraction spots**.
It captures with a **DMK 33GX249e** industrial camera and borrows the astrophotography workflow —
**calibration (Bias / Dark / Flat) + multi-frame stacking** — to improve the SNR of faint diffraction spots.
The target is static, so no equatorial mount, guiding, or de-rotation is needed.

> 中文：[README.md](README.md)

## Features

- **Live preview & histogram**: view the live image and per-frame stats (min / max / mean / std / saturation ratio / 256-bin histogram)
- **Parameter control**: exposure, gain, black level, ROI, mirror, and trigger apply instantly, with the camera's actual values reported back
- **Four-phase capture**: Bias → Dark → Flat → Light with guided prompts; **before Light starts you manually confirm parameters against the histogram**
- **Bad-frame exclusion**: exclude frames by filename during processing; calibration / stacking both honor it
- **Calibrate / Stack / Rescale preview**: Calibrate → Stack → Rescale, with live progress and downloadable results
- **Snapshots**: choose a snapshot folder and set a prefix; files are automatically named `<prefix>_<timestamp>`
- **Mock mode**: always available without a camera, so the full workflow can be tried anywhere

## Hardware Requirements

- **Acquisition host**: Windows (real-camera acquisition); macOS / Linux / WSL can run Mock mode
- **Camera**: The Imaging Source **DMK 33GX249e** (GigE mono industrial camera); the ic4 driver is also required for the real camera
- **No camera**: no hardware needed — the built-in Mock camera covers the entire workflow

## Installation

```bash
git clone <your-repo-url> static-astrophoto
cd static-astrophoto
```

**Windows** (PowerShell):

```powershell
.\scripts\install.ps1              # Mock mode
.\scripts\install.ps1 -WithCamera  # real-camera mode (also installs the ic4 Python bindings)
```

**macOS / Linux / WSL**:

```bash
./scripts/install.sh
```

The installer creates the Python virtual environment, installs backend dependencies, builds the frontend,
and creates the data directories. It is safe to re-run.

## Running

**macOS / Linux / WSL** (Mock camera):

```bash
cd backend
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

**Windows**:

```powershell
cd backend
.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open **http://localhost:8000** in your browser; other machines on the LAN can use `http://<acquisition-host-IP>:8000`.

## Workflow

1. **Connect**: choose `mock-1` (no camera) or `ic4-<serial>` (real camera) and open the device
2. **Live**: inspect the live preview and histogram, and adjust exposure / gain / black level / ROI etc.
3. **Capture**: create a capture plan (or use the default template) and follow the prompts:
   Bias (lens cap on) → Dark (cap still on) → Flat (defocused beam / uniform illumination) → Light (confirm parameters manually, then start)
4. **Processing**: select a session → uncheck bad frames → Calibrate → Stack → Rescale preview and download results

In **Settings** you can change the **data directory** (and the backend address / preview parameters).

## Data Storage

- The default data directory is `data/` under the repository root; it can be changed in **Settings** — new captures then go to the new directory while old data stays where it was
- Snapshots are saved under `data/snapshots/` by default; on the **Live** page you can choose the snapshot folder and set a prefix, and files are named `<prefix>_<timestamp>` (e.g. `SNP_20260822T04-01-33.fits`)
- Capture data is stored under `data/captures/<timestamp>_<name>/` in bias / dark / flat / light subdirectories

## FAQ

- **White screen / broken UI**: hard-refresh the browser (Ctrl+Shift+R or Cmd+Shift+R), or reopen `http://localhost:8000`
- **Port already in use**: start on another port, e.g. `--port 8001`, then open `http://localhost:8001`
- **Real camera not found**: make sure The Imaging Source **ic4 driver** is installed and the GigE Vision filter is bound to the capture NIC; enabling **Jumbo Frames (9000 MTU)** is recommended
- **No camera**: no driver is needed — the **Mock camera is always available**, so every feature can be tried

## Documentation

- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) — development documentation (install, run, test, and deployment details)
- [docs/architecture.md](docs/architecture.md) — architecture design & key decisions
- [docs/api.md](docs/api.md) — frontend–backend API contract
