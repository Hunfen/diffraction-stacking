# README 重组验收（t2: readme-reorg-review）

- **结论**：**PASS**
- **日期**：2026-08-22
- **范围**：`README.md`、`README.en.md`、`docs/DEVELOPMENT.md`、`docs/DEVELOPMENT.en.md`
- **性质**：独立只读验收；未 commit（工作区现状：`README*.md` modified，`docs/DEVELOPMENT*.md` untracked）

---

## 1. 文件检查 — PASS

- `docs/DEVELOPMENT.md` 与 `docs/DEVELOPMENT.en.md` 均存在（10049 / 10830 字节）。
- 与 `HEAD` 版根 `README.md` / `README.en.md` 逐行 diff：正文内容一一对应，**唯一改动是迁移说明与相对路径修正**：
  - 顶部“English/中文版”链接 → 迁移说明 + `../README.md` + 互链 `DEVELOPMENT*.md`
  - `docs/api.md` → `api.md`，`docs/reviews/` → `reviews/`，`docs/architecture.md` → `architecture.md`
  - `backend/README.md` / `frontend/README.md` → `../backend/README.md` / `../frontend/README.md`
  - 架构图与安装说明中的 `data/` 路径 → `../data/`（配合“本文件位于 docs/”提示）
  - 标题补充“（仓库根目录视角）”与命令执行位置提示
- 新 `README.md` / `README.en.md` 与旧 README 完全不同，是全新的用户向文案（重写而非搬移）。

## 2. 链接检查 — PASS

对 4 个文件全部 markdown 链接（含图片语法）逐条解析：

| 文件 | 链接 | 结果 |
|---|---|---|
| README.md | `README.en.md`、`docs/DEVELOPMENT.md`、`docs/architecture.md`、`docs/api.md` | 全部存在 |
| README.en.md | `README.md`、`docs/DEVELOPMENT.md`、`docs/architecture.md`、`docs/api.md` | 全部存在 |
| docs/DEVELOPMENT.md | `../README.md`、`DEVELOPMENT.en.md`、`../backend/README.md`、`api.md`、`architecture.md`、`reviews/`、`../frontend/README.md` | 全部可解析 |
| docs/DEVELOPMENT.en.md | `../README.md`、`DEVELOPMENT.md`、`../backend/README.md`、`api.md`、`architecture.md`、`reviews/`、`../frontend/README.md` | 全部可解析 |

- 无指向已删除路径的链接。
- 注：`docs/DEVELOPMENT*.md` 中的 `reviews/` 是**目录链接**，`test -f` 按定义不通过；但目标目录 `docs/reviews/` 存在（`test -d` / `test -e` 通过），属合法的目录链接，且继承自旧 README 的 `docs/reviews/` 写法，判定可解析。

## 3. 内容检查 — PASS

### 3.1 新 README 必需内容（中英对照齐全）

| 要求 | README.md | README.en.md |
|---|---|---|
| Static AstroPhoto 介绍 | ✓ 第 3–5 行 | ✓ 第 3–6 行 |
| 功能 | ✓ `## 功能` | ✓ `## Features` |
| 硬件要求 | ✓ `## 硬件要求` | ✓ `## Hardware Requirements` |
| 安装 + install.sh/install.ps1 与 `-WithCamera` 区别 | ✓ `## 安装`：`.\scripts\install.ps1`（Mock）/ `-WithCamera`（真机，额外装 ic4 Python 组件）；`./scripts/install.sh` | ✓ 完全对应 |
| 运行 | ✓ `## 运行`（macOS/Linux/WSL 与 Windows 两条命令） | ✓ `## Running` |
| 四步使用流程 | ✓ `## 使用流程`：Connect → Live → Capture → Processing | ✓ `## Workflow` 一致 |
| 数据存放与快照命名 | ✓ `## 数据存放`：`data/`、`data/snapshots/`、`<prefix>_<时间戳>` + `SNP_20260822T04-01-33.fits` 示例 | ✓ 一致 |
| 常见问题 | ✓ `## 常见问题`（白屏 / 端口占用 / 真机看不到相机 / 无相机） | ✓ `## FAQ` 四条对应 |
| 文档链接 | ✓ `## 文档`：DEVELOPMENT / architecture / api | ✓ `## Documentation` |

### 3.2 不含开发细节

- 正文无 API 契约内容、无处理公式（sigma/median/BITPIX/12-bit）、无评审记录、无 WebSocket/requirements 等实现细节（全文 grep 确认）。
- 仅“文档”小节链接标题中保留 `docs/api.md — 前后端 API 契约` 的指向性说明，属文档导航而非契约正文，判定符合要求。

### 3.3 中英一致

- 8 个二级标题一一对应，段落顺序、命令、步骤与示例文件名完全对应，无漏译/多译。

## 4. 命令正确性抽查 — PASS

| 文档命令 | 实际验证 |
|---|---|
| `./scripts/install.sh`（macOS/Linux/WSL） | 存在且可执行；脚本创建 `backend/.venv`、装 `backend/requirements.txt`、`npm install` + `npm run build`、建 `data/snapshots` 与 `data/captures`，幂等 ✓ |
| `.\scripts\install.ps1` / `.\scripts\install.ps1 -WithCamera` | `param([switch]$WithCamera)` 存在；真机模式额外 `pip install imagingcontrol4` ✓ |
| `cd backend && .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000` | `backend/.venv/bin/python` 存在；`backend/app/main.py` 第 231 行 `app = FastAPI(...)` 导入成功（34 条路由）；uvicorn 0.52.4 已安装。实测同命令（端口 8027，因 8000 被开发服务占用）启动成功，`GET /api/health` 返回 `{"ok":true,...}` |
| Windows `.venv\Scripts\python -m uvicorn ...` | 与 install.ps1 中 `$VenvPython = .venv\Scripts\python.exe` 及脚本尾部 summary 一致 ✓ |
| 数据存放与快照命名 | `backend/app/util.py`：默认 `data_root = <repo>/data`，`snapshots/`、`captures/` 子目录；Settings 页 `PUT /api/settings` 切换 data_root 且旧目录不迁移 ✓；`capture.py` 快照命名 `<prefix>_<YYYYMMDDTHH-MM-SS>.fits` ✓；capture 会话目录 `<timestamp>_<name>` ✓ |

---

## 观察项（非阻塞）

1. `docs/DEVELOPMENT*.md` 第 123 行“创建 `../data/snapshots` 与 `../data/captures`”是相对本文件的视角，但前文提示命令在仓库根执行；若按根目录视角读会被误读。install.sh 实际固定建在 `$ROOT_DIR/data/`，不受执行目录影响，建议后续微调措辞。
2. 架构图 mermaid 标签 `Disk[(../data/captures)]` 同理为“docs/ 视角”，与“仓库根目录视角”标题略有不一致。
3. `reviews/` 为目录链接（`test -f` 失败、`test -e` 通过），如后续机械校验脚本只接受 `-f`，建议改用 `reviews/README.md` 或保留并豁免目录链接。

以上观察项不影响本验收结论。
