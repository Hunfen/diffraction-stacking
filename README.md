# Diffraction Stacking

面向 **LEED / RHEED 静态电子衍射点** 的采集与处理系统。
借用深空摄影的 **Bias / Dark / Flat 标定 + 多帧 Sigma 叠加** 方法提升弱衍射点的信噪比与动态范围，
目标静止，因此**无需赤道仪、导星、去旋转**。

> English version: [README.en.md](README.en.md)

---

## 系统组成

- **相机**：The Imaging Source **DMK 33GX249e**（单色 12-bit GigE 工业 CMOS，Sony IMX249，1920×1200 @ 48 fps，曝光 30 µs–30 s，增益 0–48 dB）
- **后端**：FastAPI（Python 3.10+），运行在 **Windows 采集主机**（ic4 SDK）；无真机时提供 **Mock 相机** 模式，macOS/Linux 可完整开发调试
- **前端**：React 18 + TypeScript + Vite + Ant Design 的 **Web 单页应用**（局域网浏览器访问，无需安装客户端）
- **数据链**：`12-Bit Packed 传输 → uint16 内存 → 16-bit FITS 落盘`，预览仅用 8-bit 灰阶 JPEG，不污染采集数据
- **处理管线**：master 校准帧（sigma-clipped mean）→ 逐帧标定 `(light − dark) / flat_norm` → 可选 FFT 相位相关漂移配准 → sigma-clipped 叠加（float32 结果）→ 灰度拉伸预览

## 特性

- 实时预览：WebSocket 推送 JPEG 帧 + 逐帧统计（min/max/mean/std/饱和比例/256-bin 直方图）
- 参数面板：曝光 / 增益 / 黑电平 / ROI / 镜像 / 触发等即调即生效，显示相机实际返回值
- 四相位序列拍摄：`BIAS → DARK → FLAT → LIGHT`，每相位有中文引导提示；
  **LIGHT 前强制人工对照实时直方图确认参数**（中心束过曝程度由人工判断）
- 坏帧排除：处理时按文件名 `exclude`，前后端真实生效
- 处理任务：calibrate / stack / stretch 后台 job 化，进度经 WebSocket 推送
- 数据落盘：16-bit FITS（含完整拍摄元数据）+ PNG 缩略图 + 会话 `meta.json`
- 12-bit 友好：饱和比例、温度、黑电平裁切等 QC 指标全程记录

## 架构

```mermaid
flowchart LR
    subgraph Browser[浏览器前端 React]
        Live[实时预览 + 直方图]
        Params[参数面板]
        Capture[序列拍摄]
        Proc[数据处理]
    end
    subgraph Host[采集主机 Windows / 开发机 macOS-Mock]
        BE[FastAPI 后端]
        SDK[imagingcontrol4 ic4 / MockCamera]
        Disk[(data/captures)]
    end
    Cam[DMK 33GX249e GigE] -- GigE Vision --> SDK
    Browser -- REST + WebSocket --> BE
    BE --> Disk
```

## 目录结构

```
.
├── backend/                # FastAPI 后端
│   ├── app/
│   │   ├── main.py         # REST + /ws/live + /ws/events + 前端静态托管
│   │   ├── camera.py       # MockCamera / IC4Camera + 设备服务
│   │   ├── capture.py      # 四相位拍摄状态机、FITS/缩略图落盘
│   │   ├── process.py      # calibrate / stack / stretch 后台任务
│   │   ├── sessions.py     # session 元数据
│   │   └── util.py         # 统计、自动对比度、路径工具
│   ├── requirements.txt
│   └── README.md           # 后端部署细节（Windows 真机步骤）
├── frontend/               # React SPA
│   └── src/
│       ├── api/            # axios 封装、WS 客户端、类型、状态机
│       └── pages/          # Connect / Live / Capture / Processing / Settings
├── scripts/
│   └── backend_smoke.py    # 后端端到端自测（Mock 全流程）
├── docs/
│   ├── architecture.md     # 架构设计 v0.3
│   ├── api.md              # 前后端 API 契约 v1.1
│   └── reviews/            # 独立评审与集成验收报告
└── data/                   # 运行时数据（git 忽略）
    ├── snapshots/
    └── captures/<时间戳>_<名称>/{bias,dark,flat,light,masters,calibrated,results}
```

## 快速开始

### 前置要求

| 用途 | 要求 |
|---|---|
| 后端（真机） | Windows 10/11 + Python 3.10+（推荐 3.12）+ ic4 驱动 |
| 后端（开发） | macOS / Linux + Python 3.12（自动使用 Mock 相机） |
| 前端 | Node.js ≥ 20（推荐 22+）与 npm |
| 相机 | DMK 33GX249e（可选；没有也能用 Mock 跑通全部功能） |

### 1. 克隆

```bash
git clone <your-repo-url> diffraction-stacking
cd diffraction-stacking
```

### 2. 后端

macOS / Linux（Mock 开发模式）：

```bash
cd backend
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Windows（DMK 33GX249e 真机）：

```powershell
cd backend
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install imagingcontrol4        # ic4 官方 Python 绑定
.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

> 真机前置：安装 The Imaging Source 的 ic4 驱动并绑定 GigE 网卡，建议开启 Jumbo Frame（9000 MTU）；
> 详见 [backend/README.md](backend/README.md)。

### 3. 前端

开发模式（热更新，Vite 代理 `/api`、`/ws` 到 `http://localhost:8000`）：

```bash
cd frontend
npm install
npm run dev      # http://localhost:5173
```

生产模式（构建后由 FastAPI 托管，单端口部署）：

```bash
cd frontend
npm install
npm run build    # 产物在 frontend/dist/
# 重启后端后访问 http://<主机>:8000
```

### 4. 使用流程

1. **连接**：`/connect` 页选择 `mock-1`（开发）或 `ic4-<serial>`（真机）并打开
2. **实时预览**：`/live` 页看画面与直方图，调曝光/增益/黑电平/ROI 等参数
3. **序列拍摄**：`/capture` 页创建计划（或点“默认衍射套装”），按提示依次完成
   BIAS（盖镜头盖）→ DARK（保持盖盖）→ FLAT（束流散焦均匀照明）→ LIGHT（人工确认参数后开始）
4. **数据处理**：`/processing` 页选 session → 排除坏帧 → calibrate → stack → stretch 预览并下载结果

## 12-bit 数据链说明

相机 ADC 为 12-bit（0–4095）。系统选择：

- **传输**：12-Bit Packed（1.5 B/px）——比 16-bit 省 33% GigE 带宽，全分辨率帧率上限更高，零精度损失
- **内存/计算**：解包为 uint16 后与 16-bit 计算速度完全相同；median / sigma-clip 等均以 float 计算
- **落盘**：16-bit FITS（数据在低 12 位），保证 astropy / ImageJ / DS9 直接兼容
- **叠加结果**：float32 FITS（BITPIX=-32），保留多帧平均的小数精度
- **预览**：8-bit 灰阶自动对比度（1%–99% 分位），单色相机无 debayer / 彩色 LUT

## API 与契约

- 统一响应结构：`{ "ok": bool, "data": any, "error": string|null }`
- REST：`/api/health`、`/api/devices`、`/api/device/*`、`/api/properties`、`/api/live/*`、
  `/api/snapshot`、`/api/capture/*`、`/api/sessions/*`、`/api/files/*`、`/api/process/*`
- WebSocket：`/ws/live`（文本统计 → 二进制 JPEG 成对推送）、`/ws/events`（capture / job 进度）
- Swagger UI：启动后访问 `http://<host>:8000/docs`
- 完整契约见 [docs/api.md](docs/api.md)

## 测试

```bash
# 后端端到端自测（退出码 0 = 通过）
cd backend && .venv/bin/python ../scripts/backend_smoke.py   # 114/114 通过

# 前端构建与 lint
cd frontend && npm run build && npm run lint
```

已执行的独立评审与验收（见 `docs/reviews/`）：

- 后端独立评审：smoke 114/114 + 补测（错误包裹、状态机、路径穿越、SPA fallback 等）全绿
- 前端独立评审：契约对照 8/8 PASS，构建 0 error
- 最终集成验收：62/62 PASS，P0=0 / P1=0（Mock 全流程 + 性能 + 安全回归）

## Mock 相机

无真机时后端自动提供 `mock-1`：

- 合成静态 LEED/RHEED 点阵：规则格点高斯斑 + 亮 00 斑 + 荧光屏背景辉光
- 噪声模型：读出噪声 + 泊松噪声（混合实现）+ 热像素；exposure / gain / black_level 均生效
- 默认 ROI 1024×768（最高 1920×1200）；满 ROI 衍射场景约 22–24 fps
- 温度缓慢漂移、可选亚像素漂移（用于配准功能演示）
- 特殊属性：`mock_scene`（diffraction / uniform / dark）、`mock_drift`

## 已知限制

- `IC4Camera` 真机路径尚未在 Windows 上实测，首次联调可能需微调属性枚举
- 相机无 TEC 制冷：Dark 需与 Light 在邻近时间、相近温度下拍摄（系统记录每帧温度）
- 中心束过曝不自动处理：按设计由人工在 LIGHT 前对照直方图确认参数
- macOS 只能运行 Mock 模式；真机采集必须部署在 Windows 主机

## 文档

- [docs/architecture.md](docs/architecture.md) — 架构设计与关键决策
- [docs/api.md](docs/api.md) — 前后端 API 契约 v1.1
- [docs/reviews/](docs/reviews/) — 评审与验收报告
- [backend/README.md](backend/README.md) — 后端与 Windows 真机部署细节
- [frontend/README.md](frontend/README.md) — 前端开发说明

## License

待定（TBD）
