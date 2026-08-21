# v1.1 修复回归验证报告（docs/api.md v1.1）

- 评审对象：全仓 v1.1 修复回归（只读源码 + 运行测试，未修改源码、未 commit）
- 评审人：reviewer（regression-verify，t9）
- 评审日期：2026-08-22
- 环境：macOS / Python 3.12（backend/.venv）/ Node 26 / 相机驱动 mock

## 0. 结论摘要

- **契约升级检查**：docs/api.md 已升级 v1.1；`exclude`、`needs_user`、`STACK float32(BITPIX=-32)` 三项契约与后端实现**一致**（逐条见 §1）。
- **后端回归**：`backend_smoke.py` **107/107 通过（exit 0）**；手工验证 a–g **50/50 通过（exit 0）**。后端无 FAIL。
- **前端回归**：`npm install && npm run build` **通过（exit 0）**；exclude 提交、Modal/Banner 条件、`strict:true` 三项代码检查通过。但发现 **1 个 FAIL/P1**：CapturePage 的 `applyCaptureState` 会丢弃「非 LIGHT 相位拍摄结束后、LIGHT 相位开始前」那条合法的 `waiting_user(needs_user=true)` 事件（用真实后端 WS 事件序列 + 前端逻辑模拟复现），导致标准 4 相位流程中 LIGHT 确认 Modal 不弹出、拍摄在 LIGHT 处无限等待。
- ISSUE 分级：**P0=0，P1=1，P2=1，P3=3**（见 §5）。
- 建议修复人：P1 → **frontend-dev**（修复任务见 §5）；P2/P3 → backend-dev。

---

## 1. docs/api.md v1.1 与实现一致性

| 契约项 | 契约要求 | 实现核对 | 结论 |
| --- | --- | --- | --- |
| exclude 字段 | calibrate/stack 可选 `exclude`；仅文件名、组名 ∈ bias/dark/flat/light；不存在 → 400；calibrate 按组过滤；stack 只过滤 light；`result.excluded_counts` 与 qc 都含计数 | main.py:571-599 `_validate_exclude`（未知组/路径名/不存在 400）；process.py:20-26 归一化、:201-211 calibrate 过滤、:293-343 stack 只过滤 light 且计入 excluded_counts | **PASS**（运行验证见 §2-c） |
| needs_user 字段 | state 与 WS 事件含 `needs_user`；非 LIGHT waiting_user 约 0.2s 自动 capturing（prompt→message）；LIGHT `needs_user=true` 阻塞至 resume；capturing 中 resume 409 | capture.py:48/94/150/164-172/240-242/272-277/295 | **PASS**（运行验证见 §2-a/b） |
| STACK float32 | `STACK_*.fits` float32、BITPIX=-32、不截断 | process.py:350-351 直接写 float32 HDU | **PASS**（运行验证见 §2-f） |
| 附：500 统一文案 | 500 返回 `error:"服务器内部错误"` | main.py:249-257 通用异常处理器已改；但 main.py:272（`raise_cam_err` 兜底）与 main.py:441（snapshot 保存失败）仍以原始消息 `raise HTTPException(500, ...)` → ISSUE P3-2 | **ISSUE** |

## 2. 后端回归验证

### 2.1 必测：backend_smoke.py —— **PASS 107/107（exit 0）**

v1.1 新增断言全部通过，包括：空 phases plan 400、bias 相位 `waiting_user(needs_user=false)` 且 resume 409、自动进入 capturing、LIGHT `needs_user=true`、拍摄中 calibrate 409、capturing 中 resume 409、exclude 不存在 400、exclude 排除 light 后 `frames=2/total_exposure_us=6000`、`qc.excluded_counts`、STACK `BITPIX=-32`、snapshot FITS 头（EXPOSURE/GAIN/BLACKLEV/TEMPERATURE/FRAMETYP/CAMERA/SERIAL）、DATE-OBS 毫秒、masters/ 路径可访问。

### 2.2 手工验证 —— **PASS 50/50（exit 0）**

独立脚本 `/tmp/t9_manual.py`（自起 uvicorn，不复用 smoke 进程）：

| 项 | 要求 | 实测 | 结论 |
| --- | --- | --- | --- |
| a | 非 LIGHT：waiting_user→~0.2s 自动 capturing；拍摄中多次采样 status=capturing、needs_user=false | bias 相位 `waiting_user(needs_user=false, prompt 存在)` → resume 409 → **221ms** 后 `capturing(needs_user=false, prompt=null, message=prompt)`；capturing 期间 **40/40 次采样**均为 capturing | **PASS** |
| b | LIGHT 阻塞 waiting_user、needs_user=true；capturing 中 resume 409；resume 后 capturing | LIGHT `waiting_user(needs_user=true)` 保持 0.6s 不动 → resume 200 → capturing(needs_user=false) → 再次 resume 409 → finished(2 帧) | **PASS** |
| c | calibrate/stack 带 exclude 计数正确；排除不存在文件 400 | 不存在/带路径/未知分组 → 400；calibrate exclude `{bias:["BIAS_0001.fits"],light:["LIGHT_0002.fits"]}` → `excluded_counts={bias:1,dark:0,flat:0,light:1}`、`calibrated_count=2`；stack 同 exclude → `qc.frames=2、total_exposure_us=4000.0`，qc 与 result 均含 excluded_counts（bias 仅计数） | **PASS** |
| d | 拍摄中提交处理任务 409 | bias 拍摄中 `POST /api/process/calibrate` → 409 `拍摄进行中，不能提交处理任务` | **PASS** |
| e | 空 phases plan 400 | `phases:[]` → 400 统一错误包裹 | **PASS** |
| f | STACK 结果 FITS float32 | 下载后 astropy 读取：`BITPIX=-32`、dtype=float32 | **PASS** |
| g | 路径穿越回归抽查 | `xxx/../../etc/passwd`、`%2e%2e/%2e%2e/etc/passwd`、`snapshots/../../etc/passwd` → 全部 4xx | **PASS** |

## 3. 前端回归验证

### 3.1 构建 —— **PASS**

- `cd frontend && npm install`：exit 0（仅 fsevents install-script 环境警告）。
- `npm run build`（`tsc -b && vite build`）：exit 0，3616 modules；产物已按 antd/charts/react-vendor 拆 chunk（旧「单 chunk 1.62MB」问题已修复，无 >500kB warning）。
- `npm run lint`：0 error / 12 warnings（较 t5 减少 1 条）。

### 3.2 代码检查 —— 3 项通过，1 项 FAIL

| 项 | 结论 | 证据 |
| --- | --- | --- |
| ProcessingPage 提交 calibrate/stack 携带 exclude；未勾选帧进对应组 | **PASS** | `buildExclude()`（ProcessingPage.tsx:186-195）把 `excluded` Set 按相位映射为 `{bias,dark,flat,light}` 文件名数组；`runCalibrate`/`runStack` 请求体均带 `exclude: buildExclude()`（:214/:235）；UI 文案与 Checkbox 语义已改为「勾选 = 纳入」 |
| Modal 仅 `needs_user=true` 阻塞；非 light 只显示 Banner | **PASS（条件逻辑）** | CapturePage.tsx:366-367：`blockingModal = status==='waiting_user' && needs_user !== false`；`preparingWithoutUser = status==='waiting_user' && needs_user === false`；:445-454 渲染非阻塞 Banner；Modal `open={blockingModal}` |
| tsconfig strict true | **PASS** | tsconfig.app.json:10 `"strict": true` |
| 前端实际能收到并展示 LIGHT 确认（端到端事件流） | **FAIL（P1）** | CapturePage.tsx:95-100 `applyCaptureState`：`next.status==='waiting_user' && captureStateRef.current.status==='capturing'` 时**直接丢弃事件**。用真实后端 WS 序列复现：bias→light 计划的事件为 `running → waiting_user(bias,false) → capturing×3 → waiting_user(light,true)`；按前端逻辑模拟，最后一条 light 事件被丢弃，**可渲染的 light Modal 事件数 = 0**。轮询路径（:105）同样被丢弃。结果：标准 4 相位流程拍摄到 LIGHT 时 Modal 永不弹出、无法 resume，后端无限阻塞在 waiting_user（只能 abort） |

### 3.3 FAIL 复现步骤（P1，修复人：frontend-dev）

1. 打开 mock 设备；plan `[{bias×2},{light×1}]` → start。
2. 订阅 `/ws/events`，实际事件序列（已采集）：
   `running` → `waiting_user(needs_user=false, bias)` → `capturing×3` → `waiting_user(needs_user=true, light)`。
3. 前端 `applyCaptureState` 在收到最后一条时，因 `captureStateRef.current.status === 'capturing'` 直接 return；1s 轮询 `refreshCaptureState` 走同一函数，同样丢弃。
4. 表现：LIGHT 人工确认 Modal 不出现，用户无「继续」按钮，拍摄在 LIGHT 处无限等待（后端状态仍为 waiting_user，只能点击中止）。

**建议修复**（frontend-dev）：`applyCaptureState` 只丢弃「capturing 期间到达的 `waiting_user` 且 `needs_user === false`」的陈旧消息；`needs_user === true` 的 waiting_user **永不丢弃**。最稳妥做法是直接删除该 guard（v1.1 后端已修复状态机，capturing 期间不会再发陈旧 waiting_user）。修复后需在浏览器中重跑一次默认 4 相位流程验收。

## 4. 其余观察

- 后端 v1.1 修复点（空 phases 400、拍摄中处理 409、500 文案、snapshot 头、DATE-OBS 毫秒、masters 路径、shutdown 优雅中止）均已在 smoke/手工中验证通过。
- 本次测试生成的 session 位于已 gitignore 的 `data/`；测试脚本在 `/tmp`；未修改源码、未 commit。

## 5. ISSUE 分级汇总

| 级别 | 编号 | 位置 | 问题 | 建议修复人 |
| --- | --- | --- | --- | --- |
| P0 阻断 | — | — | 无 | — |
| P1 重要 | R-1 | frontend/src/pages/CapturePage.tsx:95-100 | `applyCaptureState` 丢弃 capturing→waiting_user(needs_user=true) 的合法 LIGHT 事件，标准 4 相位流程 LIGHT Modal 不弹出、拍摄阻塞（§3.3 已复现） | **frontend-dev**：只丢弃 `needs_user===false` 的陈旧 waiting_user（或删 guard），修复后浏览器重跑默认模板验收 |
| P2 建议 | R-2 | backend/app/main.py:272、441 | v1.1 契约「500 统一返回 服务器内部错误」仍有两条旁路：`raise_cam_err` 兜底 `HTTPException(500, msg)`、snapshot 保存失败 `HTTPException(500, str(exc))`，会经 Starlette 处理器把原始错误细节返回客户端 | backend-dev：改为 `HTTPException(500, "服务器内部错误")` 并 log 原始异常 |
| P3 建议 | R-3 | backend/app/capture.py:396-407 | snapshot FITS 头已补 7 个关键字，但仍缺 §3.2 的 SESSION/NFRAME（若契约认为快照也属于「每帧」）；api.md 未对 snapshot 头单独声明 | backend-dev / 船长确认口径 |
| P3 建议 | R-4 | backend/app/main.py:581-595 | exclude 数组允许重复文件名，`excluded_counts` 按数组长度计数（如 `["LIGHT_0002.fits","LIGHT_0002.fits"]` 计 2），与「排除文件数」语义可能不一致 | backend-dev：校验时去重 |
| P3 建议 | R-5 | frontend（lint） | oxlint 仍 12 条 warning（渲染期写 ref / effect 内 setState），构建与功能不受影响 | frontend-dev：随 R-1 一并收敛 |

## 6. 结论

- 后端 v1.1 修复**全部回归通过**（smoke 107/107 + 手工 50/50），契约 exclude/needs_user/float32 与实现一致。
- 前端构建与静态条件逻辑通过，但 **R-1（P1）阻断前端主流程**：默认 4 相位拍摄无法在浏览器中完成 LIGHT 确认。**不 commit、不改源码；等 frontend-dev 修复 R-1 后应重跑本节 §3.3 浏览器验收。**
