# Static AstroPhoto

**Static AstroPhoto** 是面向 **LEED / RHEED 静态电子衍射点** 的采集与叠加处理软件：
基于 **DMK 33GX249e** 工业相机采集，借用天文摄影的 **标定（Bias / Dark / Flat）+ 多帧叠加**
方法提升弱衍射斑点的信噪比。目标静止，因此无位置修正。

> English: [README.en.md](README.en.md)

## 功能

- **实时预览与直方图**：实时查看画面与逐帧统计（min / max / mean / std / 饱和比例 / 256-bin 直方图）
- **参数调节**：曝光、增益、黑电平、自动曝光/自动增益开关、采集区域（Width × Height）、镜像、触发等即调即生效，并回显相机实际值
- **四相位拍摄**：Bias → Dark → Flat → Light 分步引导；**Flat 与 Light 开始前需人工确认参数，其余相位自动继续**（Light 需对照直方图）
- **坏帧排除**：处理时按文件名排除坏帧，标定 / 叠加均生效
- **标定 / 叠加 / Rescale 预览**：Calibrate → Stack → Rescale，进度实时可见，结果可下载
- **快照**：选择快照文件夹并设置 prefix，文件自动按 `<prefix>_<时间戳>` 命名
- **Mock 模式**：没有相机时随时可用，可完整体验全部功能

## 硬件要求

- **采集主机**：Windows（真机采集）；macOS / Linux / WSL 可运行 Mock 模式
- **相机**：The Imaging Source **DMK 33GX249e**（GigE 单色工业相机）；真机还需安装 ic4 驱动
- **无相机**：无需任何硬件，内置 Mock 相机即可跑通全部流程

## 安装

```bash
git clone <仓库地址> static-astrophoto
cd static-astrophoto
```

**Windows**（PowerShell）：

```powershell
.\scripts\install.ps1              # Mock 模式
.\scripts\install.ps1 -WithCamera  # 真机模式（额外安装 ic4 Python 组件）
```

**macOS / Linux / WSL**：

```bash
./scripts/install.sh
```

安装脚本会自动创建 Python 虚拟环境、安装后端依赖、构建前端并创建数据目录，可安全重复执行。

已有安装需要更新到最新代码时，可使用：

- **Windows**：`.\scripts\update.ps1`；真机模式加 `-WithCamera`
- **macOS / Linux / WSL**：`./scripts/update.sh`；真机模式加 `--with-camera`

更新脚本会以 fast-forward 拉取 `main`，刷新后端依赖并重新构建前端。

## 运行

**macOS / Linux / WSL**：

```bash
./scripts/run.sh
```

或手动启动（Mock 相机）：

```bash
cd backend
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

**Windows**：

```powershell
.\scripts\run.ps1
```

或手动启动：

```powershell
cd backend
.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

浏览器打开 **http://localhost:8000**；局域网内其他机器可访问 `http://<采集主机IP>:8000`。

## 使用流程

1. **Connect**：选择 `mock-1`（无相机）或 `ic4-<serial>`（真机）并打开设备
2. **Live**：查看实时预览与直方图，调节曝光 / 增益 / 黑电平 / 采集区域 / 自动曝光 / 自动增益等参数
3. **Capture**：创建拍摄计划（可直接使用「默认衍射套装」），按提示依次完成
   Bias（盖镜头盖）→ Dark（保持盖盖）→ Flat（束流散焦均匀照明，人工确认后开始）→ Light（人工确认参数后开始）
4. **Processing**：选择 session → 取消勾选坏帧 → Calibrate → Stack → Rescale 预览并下载结果

在 **Settings** 页可修改**数据目录**（也可调整后端地址与预览参数）。

## 数据存放

- 默认数据目录为仓库根目录下的 `data/`；可在 **Settings** 页改为其他可写目录，修改后新数据写入新目录，旧数据保留在原目录
- 快照默认保存在 `data/snapshots/`；可在 **Live** 页选择快照文件夹并设置 prefix，文件按 `<prefix>_<时间戳>` 命名（例如 `SNP_20260822T04-01-33.fits`）
- 拍摄数据按 `data/captures/<时间戳>_<名称>/` 分 bias / dark / flat / light 等子目录保存

## 常见问题

- **页面白屏 / 界面异常**：强制刷新浏览器（Ctrl+Shift+R 或 Cmd+Shift+R），或重新打开 `http://localhost:8000`
- **端口被占用**：换一个端口启动，例如 `--port 8001`，再打开 `http://localhost:8001`
- **真机看不到相机**：确认已安装 The Imaging Source **ic4 驱动**并把 GigE Vision 过滤器绑定到采集网卡；建议开启 **Jumbo Frame（9000 MTU）**
- **没有相机**：无需安装任何驱动，**Mock 相机始终可用**，全部功能都可体验

## 文档

- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) — 开发文档（安装、启动、测试与部署细节）
- [docs/architecture.md](docs/architecture.md) — 架构设计与关键决策
- [docs/api.md](docs/api.md) — 前后端 API 契约
