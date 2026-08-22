# 英文 UI + Static AstroPhoto + 一键安装脚本独立验收报告（en-ui-install-review）

- 评审对象：前端英文 UI + 程序名 Static AstroPhoto（t1）、后端英文用户可见字符串（t2）、一键安装脚本 `scripts/install.sh` / `scripts/install.ps1`（t3）
- 评审人：reviewer（任务 t4，attempt 1 / 01f30915-3ac2-4c55-912e-f8e139ec79b6）
- 评审日期：2026-08-22
- 评审方式：只读源码审查 + 实际运行（build / lint / backend smoke / 本机安装脚本 / uvicorn + curl / headless Chrome）；**未修改任何源码、未 commit**（本报告文件除外）
- 环境：macOS（darwin）；Python 3.12.14（`backend/.venv`）；Node v26.7.0 / npm 11.19.0 / Vite 8.2.2 / oxlint；playwright-core 1.62.1 + headless Chrome（`channel:'chrome'`，工作目录 `/tmp/pwtest`）；uvicorn `app.main:app` @ 127.0.0.1:8000（FastAPI 托管生产 dist）

## 结论摘要

| 板块 | 结果 |
| --- | --- |
| 前端 `npm run build` | **PASS**：exit 0，vite build 成功（3602 modules） |
| 前端 `npm run lint` | **PASS**：0 warnings / 0 errors |
| `src/` 与 `index.html` CJK 扫描 | **PASS**：代码 0、字符串字面量 0（注释 1034 个字符，允许） |
| headless Chrome 打开 `http://127.0.0.1:8000/` | **PASS**：DOM 全文 0 CJK；`<title>` = `Static AstroPhoto`；顶部品牌 = `Static AstroPhoto` |
| 五个页面主要文案抽查 | **PASS**：五页 UI 文案全部英文（Processing 页出现的 CJK 仅来自后端 smoke 刻意保留的中文测试会话名，属数据非文案，见 §1.5） |
| 后端 smoke | **PASS**：173/173，退出码 0 |
| 后端错误响应英文抽查 | **PASS**：未打开设备 409、非法属性 400、非法快照 folder 400 均为英文 |
| Mock 属性 label | **PASS**：14/14 英文，响应无 CJK |
| `./scripts/install.sh` 本机执行两次 | **PASS**：两次均 exit 0（各约 3 s）；`data/`、`frontend/dist`、`backend/.venv` 均存在 |
| `scripts/install.ps1` 静态审查 | **PASS**：幂等 / `-WithCamera` 分支 / node·py 检测 / 输出说明均合理（17/17 静态检查项通过，附 2 条非阻塞建议） |

**总体判定：通过（PASS）**。全部逐项证据见下，无 FAIL。

---

## 1. 前端验收

### 1.1 build：PASS

```
cd frontend && npm run build   → exit 0
tsc -b 零诊断；vite v8.2.2 构建成功：
✓ 3602 modules transformed.
dist/index.html 0.46 kB │ gzip: 0.30 kB
dist/assets/index-DVR_K-jO.css 0.21 kB │ gzip: 0.19 kB
dist/assets/index-WAGN1Uw1.js 1,629.30 kB │ gzip: 492.43 kB
✓ built in 448ms
```

证据：`/tmp/t4-frontend-build.log`（exit 0）。

### 1.2 lint：PASS

```
cd frontend && npm run lint   → exit 0
> oxlint
Found 0 warnings and 0 errors.
Finished in 40ms on 14 files with 116 rules using 8 threads.
```

证据：`/tmp/t4-frontend-lint.log`（exit 0）。

### 1.3 `src/` 与 `index.html` 用户可见 CJK 扫描：PASS

macOS 自带 grep 不支持 `-P`，改用 Python 状态机扫描（正确区分 `//`、`/* */`、单/双引号与模板字符串），扫描 14 个文件（`frontend/src/**/*.{ts,tsx}` + `frontend/index.html`）：

| 分类 | 命中 |
| --- | --- |
| 代码（注释外，含 JSX 文本） | **0** |
| 字符串字面量（含模板字符串） | **0** |
| 注释 | 1034 个字符（允许） |

抽查确认：125 个含 CJK 的源码行全部位于 `//`、`/* */`、`/** */` 注释内，无一行是渲染文案。证据：`/tmp/t4-cjk-scan.log`。

### 1.4 headless Chrome：根页面 DOM 断言 PASS

脚本 `/tmp/pwtest/en_ui_review.js`（输出 `/tmp/pwtest/en_ui_review_results.json`），`chromium.launch({channel:'chrome', headless:true})`，`goto('http://127.0.0.1:8000/')` 后 SPA 重定向到 `/live`：

| 断言 | 期望 | 实测 | 结果 |
| --- | --- | --- | --- |
| `<title>` | `Static AstroPhoto` | `Static AstroPhoto` | **PASS** |
| 顶部品牌（`.ant-layout-header .ant-typography`） | `Static AstroPhoto` | `Static AstroPhoto` | **PASS** |
| DOM 全文（`document.body.innerText`）无 CJK | 0 | `[]`（0） | **PASS** |

### 1.5 五个页面主要文案抽查：PASS

同一 headless 会话逐页 `goto` 五个路由并抓取 DOM 全文；每页 `<title>` 与顶部品牌均 = `Static AstroPhoto`，UI 文案抽查如下：

| 路由 | CJK 计数 | 抽查到的代表性英文文案 |
| --- | --- | --- |
| `/connect` | 0 | `Device List` / `Mock LEED/RHEED Camera` / `Serial: MOCK-0001` / `Close Device` / `Current Device Info` / `Property Summary` / `Exposure (exposure) = 20000 us` |
| `/live` | 0 | `The preview is an 8-bit grayscale auto-contrast image…` / `Live Stats` / `Saturated %` / `Histogram (256 bins)` / `Snapshot (Single Frame)` / `Root folder (81 files)` / `Camera Parameters` / `Range 30–30000000 us` |
| `/capture` | 0 | `Capture Plan` / `Session name` / `A timestamp prefix (YYYYMMDDTHH-MM-SS_) will be added automatically` / `Default Diffraction Set (bias × 50, dark × 30, flat × 30, light × 100)` / `No phases yet. Click "Default Diffraction Set" or "Add Phase".` |
| `/processing` | 1（`中`） | `Session Selection` / `Refresh List` / 会话名标签 `… · bias 3 / dark 3 / flat 3 / light 3 · has results` 等全部英文 UI |
| `/settings` | 0 | `Backend Address` / `Save (persisted in localStorage)` / `Restore Default (does not clear env injection)` / `Effective address: http://localhost:8000. …` |

**关于 `/processing` 的 1 个 CJK 字符**：已定位到 8 个 `.ant-tag` 会话名标签（如 `20260822T18-51-59_中文_LEED测试 · bias 2 / dark 0 / flat 0 / light 0`）。这些是 `scripts/backend_smoke.py` 为验证「非 ASCII 数据保留」而刻意创建的中文测试会话目录（见 t2 交付说明），属于**后端数据回显，不是前端文案**。页面自身的所有标题、按钮、说明、表单标签均为英文。判定：主要文案英文 **PASS**。

**补充说明（不构成 FAIL）**：第一轮逐页 `page.goto` 全页跳转时出现 2 次 `409 POST /api/live/start` console error——全页跳转不会执行 LivePage 卸载清理（`/api/live/stop`），导致下一次硬加载重复 `start`。按正常 SPA 菜单点击导航复验（`/tmp/pwtest/en_ui_spa_nav.js`：Connect→Live Preview→Capture→Processing→Settings→Live Preview→Connect），**0 个 4xx/5xx**，确认属测试导航方式产生的假阳性，产品路径无此问题。

---

## 2. 后端验收

### 2.1 smoke：PASS（173/173）

```
cd backend && .venv/bin/python ../scripts/backend_smoke.py   → exit 0
SMOKE result: 173/173 passed
All checks passed ✅
```

两次运行均全绿（第二次约 5.4 s，其自起 uvicorn 子进程于端口 53306）。证据：`/tmp/t4-backend-smoke.log`（exit 0）。

### 2.2 错误响应英文抽查：PASS

uvicorn `app.main:app` @ 127.0.0.1:8000 + curl，完整证据 `/tmp/t4-backend-errors.log`：

| 场景 | 实测 HTTP | 实测响应体 | 结果 |
| --- | --- | --- | --- |
| 未打开设备 GET `/api/properties` | **409** | `{"ok":false,"data":null,"error":"Device is not open"}` | **PASS**（英文） |
| 打开设备后 PUT `/api/properties/bogus_name` | **400** | `{"ok":false,"data":null,"error":"Unknown property: bogus_name"}` | **PASS**（英文） |
| POST `/api/snapshot` `folder="/absolute/path"` | **400** | `{"ok":false,"data":null,"error":"folder must be a relative subdirectory name (absolute paths are not allowed)"}` | **PASS**（英文） |
| 附加：PUT `/api/properties/exposure` value 非数字 | 400 | `Property exposure requires a numeric value` | **PASS**（英文） |
| 附加：`POST /api/device/open` 未知 id | 404 | `Device does not exist: mock` | **PASS**（英文） |

### 2.3 Mock 属性 label：PASS（14/14 英文）

打开 `mock-1` 后 GET `/api/properties`，14 个属性 label 全部英文，响应体无任何 CJK：

```
Exposure · Gain · Black Level · ROI Width · ROI Height · ROI X Offset · ROI Y Offset
Reverse X · Reverse Y · Trigger Mode · Diffraction Pattern · Mock Scene · Mock Slow Drift
Sensor Temperature
```

证据：`/tmp/props.json`（CJK check: NO）。

---

## 3. 安装脚本验收

### 3.1 `scripts/install.sh` 本机执行两次：PASS

| 次数 | 退出码 | 耗时 | 关键行为 |
| --- | --- | --- | --- |
| 第 1 次 | **0** | 约 3 s | `Reusing existing virtualenv`（本机 .venv 已存在）→ Node v26.7.0/npm 11.19.0 → `npm install` OK → `npm run build` OK（456 ms）→ 创建 data 目录 → 打印启动指引 |
| 第 2 次 | **0** | 约 3 s | 同上，全部步骤幂等重放成功 |

两次日志：`/tmp/install-run1.log`、`/tmp/install-run2.log`。脚本 `bash -n` 语法检查 OK；脚本全文 0 CJK。说明：本机 .venv 预存，两次均走「复用 venv」分支；全新 venv 创建分支由 t3 交付者在临时沙箱验证过（本报告另做静态核对，逻辑完整）。

### 3.2 产物检查：PASS

| 路径 | 状态 |
| --- | --- |
| `data/`、`data/captures`、`data/snapshots` | 存在（脚本 `mkdir -p` 保证） |
| `frontend/dist/`、`frontend/dist/index.html` | 存在（构建产物 0.46 kB index + assets） |
| `backend/.venv`、`backend/.venv/bin/python` | 存在（Python 3.12.14） |

### 3.3 `scripts/install.ps1` 静态审查：PASS

按任务四个维度逐项核对（17/17 静态检查项通过；PowerShell 运行环境不可用，按任务口径做静态审查）：

| 维度 | 检查项 | 结果 |
| --- | --- | --- |
| 幂等 | `.venv\Scripts\python.exe` 存在即复用，不重建 | **PASS** |
| 幂等 | `npm install` / `npm run build` 每次重跑（无副作用） | **PASS** |
| 幂等 | `data\snapshots` / `data\captures` 用 `-Force` 创建 | **PASS** |
| 幂等 | `-WithCamera` 先 `pip show imagingcontrol4`，已装则跳过 | **PASS** |
| `-WithCamera` | `[switch]$WithCamera` 参数声明 + `if ($WithCamera)` 分支 | **PASS** |
| `-WithCamera` | 未装时 `pip install imagingcontrol4` 并检查 `$LASTEXITCODE` | **PASS** |
| `-WithCamera` | 结尾按是否带参分别输出实机前置条件 / Mock 默认提示 | **PASS** |
| Python 检测 | 优先 `py -3.12`，回退 `python` | **PASS** |
| Python 检测 | `Test-Python310` 实际运行 `-c` 校验 `>= 3.10` | **PASS** |
| Python 检测 | 全不满足时给出 `winget install Python.Python.3.12` 等安装提示 | **PASS** |
| Node 检测 | `Get-Command node` / `npm`（`-ErrorAction Stop` + try/catch） | **PASS** |
| Node 检测 | `process.versions.node` 主版本 `< 20` 拒绝并给出升级命令 | **PASS** |
| 输出说明 | 统一 `[install]` 前缀 + 颜色分级（Step/Ok/Warn/Fail） | **PASS** |
| 输出说明 | npm 失败时打印日志并区分缓存/权限问题与网络问题提示 | **PASS** |
| 输出说明 | 结尾给出启动命令（`.venv\Scripts\python -m uvicorn app.main:app …`）与访问地址 | **PASS** |
| 健壮性 | `$ErrorActionPreference='Stop'`；`Push-Location`/`finally Pop-Location`；`Test-ExternalExit` 统一检查退出码 | **PASS** |
| 文案 | 脚本全文 0 CJK，输出全英文 | **PASS** |

**非阻塞建议（不扣分）**：

1. venv 复用分支只检查 `Test-Path`，不像 `install.sh` 那样复验 Python ≥ 3.10 / venv 可用性；若用户已有一个损坏的旧 venv，默认 Mock 模式下脚本仍报成功，问题会推迟到启动 uvicorn 时才暴露。建议复用时也用 `$VenvPython -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"` 校验。
2. npm 安装失败时 `$NpmLog` 临时日志文件未在 finally 中清理（`install.sh` 已清理）；失败场景会在 `%TEMP%` 累积小文件。

---

## 4. 总体判定

**通过（PASS）**。任务 t4 的全部验收项均达标：

1. 前端 build/lint 通过；`src/` + `index.html` 用户可见 CJK 为 0（注释允许）；headless Chrome 断言 `<title>`、顶部品牌均为 `Static AstroPhoto`、根页面 DOM 全文 0 CJK；五个页面主要文案英文。
2. 后端 smoke 173/173 全绿；未打开设备 409、非法属性 400、非法快照 folder 400 等错误响应均为英文；Mock 属性 label 14/14 英文。
3. `./scripts/install.sh` 本机连续执行两次均 exit 0（各约 3 s），`data/`、`frontend/dist`、`backend/.venv` 齐全；`scripts/install.ps1` 静态审查幂等、`-WithCamera`、node/py 检测、输出说明均合理。

未修改任何源码，未执行 commit；工作树在本次验收前后无新增改动（本报告文件除外）。

## 证据文件索引

- `/tmp/t4-frontend-build.log`、`/tmp/t4-frontend-lint.log`
- `/tmp/t4-cjk-scan.log`
- `/tmp/pwtest/en_ui_review.js` → `/tmp/pwtest/en_ui_review_results.json`（根页面 + 五页 DOM 断言）
- `/tmp/pwtest/en_ui_cjk_locate.js`（Processing 页 CJK 定位）、`/tmp/pwtest/en_ui_spa_nav.js`（SPA 导航 4xx 复验）
- `/tmp/t4-backend-smoke.log`、`/tmp/t4-backend-errors.log`、`/tmp/props.json`
- `/tmp/install-run1.log`、`/tmp/install-run2.log`
