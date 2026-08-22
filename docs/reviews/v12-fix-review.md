# v1.2 修复独立验收报告（t3 最终版）

> 评审对象：v1.2 四项修复（backend/、frontend/、docs/api.md、scripts/）
> 评审人：reviewer（t3）· 评审日期：2026-08-22
> 方法：只读代码审查 + 运行测试（backend smoke / 手工 API / 前端 build+lint / headless Chrome E2E），未改源码、未 commit
> 评审基准：t1/t2 交付物（本轮评审发现的问题已由船长拆为 t4/t5 修复、t6 复核；本报告记录评审时状态）

## 结论摘要

| # | 验收项 | 结论 | 关键证据 |
| --- | --- | --- | --- |
| 1 | 契约核对 | **PASS** | docs/api.md 已升 v1.2，settings、session 时间戳语义、Cache-Control: no-store 与后端实现一致 |
| 2 | 后端回归 | **FAIL(1)** | smoke **135/135 exit 0**；手工 settings/plan/no-store **14/14 PASS**；但中文 session 实际拍摄时 FITS SESSION 头非 ASCII 崩溃，帧全丢且状态误报 finished |
| 3 | 前端 | **FAIL(1)** | build exit 0、lint 0 error；settings 页、capture 时间戳/回显、?v 逻辑、ref 轮询骨架均在；但 job 提交响应 `job_id` 被当 `id` 用 → 轮询 `/api/process/jobs/undefined`、幽灵 pending 行 |
| 4 | 端到端 | **FAIL(2)** | headless Chrome 全流程走通（25/28）；?v 精确 0→1→2→3、三种 job 的 WS progress 多档推进、步骤条 3 步完成；但轮询 undefined + 预览图 onError 后 display:none 不复位 |

## 1. 契约核对（docs/api.md v1.2 ↔ 后端实现）—— PASS

- 文档版本：v1.2（L1）。
- `GET /api/settings` / `PUT /api/settings`：§2.1（绝对路径校验、自动创建 `snapshots/` `captures/`、返回 `{data_root,writable}`、持久化 `backend/.data_root.json`、优先级 `DIFF_DATA_ROOT > .data_root.json > <repo>/data`）；实现于 `main.py` settings_get/settings_put + `util.py DataRootSettings`（RLock 单例，动态访问器）。
- session 命名语义：§2.5/§2.6 明确 `effective = <YYYYMMDDTHH-MM-SS>_<sanitize(input)>`，sessions 的 id/name、meta.json name、capture state 均使用 effective；实现于 `capture.py plan/_write_meta`、`sessions.py`。
- `Cache-Control: no-store`：§1 与 §2.6 要求所有 `/api/files/**` 固定该头；实现于 `main.py files()` 的 `FileResponse(..., headers={"Cache-Control":"no-store"})`。
- 结论：契约文本与实现无 FAIL 级不一致。

## 2. 后端回归

### 2.1 smoke —— PASS（135/135，exit 0）

命令：`cd backend && .venv/bin/python ../scripts/backend_smoke.py`
结果：**135/135 PASS，退出码 0**。日志：`/tmp/v12_smoke.log`。
覆盖：settings GET/PUT 切换/恢复、snapshot 落新目录、session_name 时间戳清洗、/api/files no-store、拍摄状态机、calibrate/stack/stretch 全链路。

### 2.2 手工验证 —— 14/14 PASS（脚本 `/tmp/v12_manual_final.py`）

- PASS：GET /api/settings 默认返回 `<repo>/data` 绝对路径、writable=true。
- PASS：PUT settings 切到临时目录 → 自动创建 snapshots/ 与 captures/ → 打开 mock-1 后 snapshot 落在新目录 → 快照响应头 `Cache-Control: no-store`。
- PASS：在临时目录真实拍摄 1 帧 bias（finished、frames_done=1）→ GET /api/sessions 显示该会话（id=name）→ PUT 恢复默认后 GET sessions 不再包含该会话（动态扫描生效）。
- PASS：`plan {session_name:"测试/名字:abc"}` 返回 `20260822T13-43-49_测试_名字_abc`，匹配 `YYYYMMDDTHH-MM-SS_` 前缀，非法字符 `/`、`:` 被替换为 `_`，中文保留；sessions 列表 id/name 与 plan 响应一致；meta.json name 一致。
- PASS：`/api/files/**`（snapshot、meta.json、FITS）响应头均含 `Cache-Control: no-store`。
- 备注：任务书例子写作 `测试-名字-abc`，实现为 `测试_名字_abc`（清洗符为下划线，与 smoke 及前端提示口径一致）；契约 §2.5 对替换符未做硬性规定，故记 PASS，不做 FAIL。

### 2.3 失败项（修复人：backend-dev，已建 t4）

- **FAIL**：中文 effective session 实际拍摄时，`capture.py _save_fits` 将含中文的 session_id 写入 FITS `SESSION` 头，触发 astropy 异常：`FITS header values must contain standard printable ASCII characters`。实测（`/tmp/v12_chinese_capture_repro.py`）：
  - plan/start 成功；
  - 日志出现 `BIAS 第 1 帧失败：FITS header values must contain standard printable ASCII characters…`；
  - 随后仍记录 `BIAS 完成 1/1` 并置状态 `finished`，但 `frames_done=0`，sessions 列表出现 0 帧会话。
  - 契约 §2.5 明确保留非 ASCII（中文）会话名，因此这是实现与契约冲突；应保留 meta/URL 中文名，FITS `SESSION` 头做 ASCII 安全处理；同时帧写入失败不应被吞掉后报 finished（连续失败应中止为 error，见 t4）。

## 3. 前端

- **构建**：`cd frontend && npm install && npm run build` → **exit 0**（tsc -b + vite build 成功，0 error，vite 产物 3617 modules）。
- **lint**：`cd frontend && npm run lint` → **0 warning / 0 error**。
- **Settings 数据目录**：PASS — SettingsPage 挂载时 `apiGet('/api/settings')` 回显 data_root/writable；保存按钮 `apiPut('/api/settings',{data_root})`；成功 message.success 显示新路径、失败展示后端 error；含「修改后新拍摄写入新目录，旧数据留在原目录」文案。
- **Capture 时间戳提示与响应回显**：PASS — 输入框提示 `YYYYMMDDTHH-MM-SS_`；`startCapture` 读取 `plan.session_name` 回填输入框并 Alert「本次 Session 名称：<effective>」；进度/完成提示以响应值为准。
- **ProcessingPage 预览 ?v**：代码与实测均 PASS — 提交 `setPreviewVersion(v+1)`；done 按 `job.id|status` 去重后 `loadDetail().then(() => v+1)`；手动刷新 `v+1` + reload。E2E MutationObserver 实测 src 序列 `?v=0 → 1（提交）→ 2（完成）→ 3（手动）`，三个版本重新请求均 200 + `no-store` + PNG。
- **轮询 ref 化与步骤条**：ref 化骨架存在（selectedIdRef / activeJobIdsRef / 稳定 effect 依赖）；步骤条按最新同类型 job done 推进，E2E 实测 finish 1→2→3。但存在 **FAIL（修复人：frontend-dev，已建 t5）**：
  - `POST /api/process/{calibrate|stack|stretch}` 契约响应是 `{job_id,type,status}`，页面 `runCalibrate/runStack/runStretch` 按 `job.id`（undefined）入 jobs 表；
  - 后果：jobs 表出现一条永远 pending 的幽灵行（真实 job 靠 WS 更新为另一行），`activeJobIdsRef` 含 `undefined`，每 1s 轮询 `GET /api/process/jobs/undefined` → 404，控制台报错不断。E2E 实测 calibrate 任务显示 2 行（应为 1 行）、`/api/process/jobs/undefined` 请求持续出现。

## 4. 端到端（headless Chrome，channel:'chrome'，uvicorn:8000）

脚本：`/tmp/v12_e2e_final.cjs`（playwright-core，生产 dist 由 FastAPI 托管）。流程：API 打开 mock → UI plan 4 相位×2 帧（总 8 帧）→ LIGHT 前 waiting_user 人工确认弹窗（含直方图提示）→ 点击继续 → 拍摄完成 → Processing 默认选中本会话 → calibrate → stack → stretch。

结果：**25/28 PASS**。核心验收项全部通过：

- 拍摄链路 PASS：LIGHT 前弹窗、完成提示使用 effective 名、Processing 默认选中本会话。
- **预览 ?v** PASS：src 变化日志 `[v0, v0+1, v0+2, v0+3]`（提交/完成/手动各 +1）；完成后 src 指向 `STACK_preview.png`；`?v=1/2/3` 三个 URL 重新请求均 **200 + `Cache-Control: no-store` + PNG 魔数**。
- **进度条随 job 推进** PASS：UI 观测 calibrate `0%→1%→15%→100%`（stack/stretch 过快，UI 采样只到终值）；浏览器 WS 事件级证据：calibrate `0,1,15,30,45,50,75,95,100`、stack `0,1,55,95,100`、stretch `0,1,20,45,90,100`，三类型均多档推进并以 100 结束。
- **步骤条** PASS：finish 计数 1（calibrate done）→ 2（stack done）→ 3（stretch done）。

E2E 中确认的 2 个前端问题（与 §3 同源，修复人 frontend-dev）：

- FAIL：`/api/process/jobs/undefined` 轮询（calibrate 幽灵 pending 行，任务表出现 2 行）。
- FAIL：stretch 前预览 URL 指向尚不存在的 `STACK_preview.png`（404），img `onError` 置 `display:none`；stretch 成功后无 onLoad 复位 → 图片已加载（naturalWidth=1024）但最终仍不可见。

## 5. PASS / FAIL / ISSUE 汇总

| 级别 | 条目 | 修复人 / 后续 |
| --- | --- | --- |
| PASS | 契约 v1.2 文本与后端实现一致 | — |
| PASS | backend smoke 135/135 exit 0 | — |
| PASS | 手工 settings 动态目录 + 中文 plan 命名 + no-store 14/14 | — |
| FAIL | 中文 session 拍摄：FITS SESSION 头非 ASCII 崩溃、帧丢失但误报 finished | backend-dev → t4（进行中） |
| FAIL | ProcessingPage 用 `job.id` 存提交响应 → 轮询 `/api/process/jobs/undefined`、幽灵 pending 行 | frontend-dev → t5（已完成，待 t6 复核） |
| FAIL | 预览 img onError 隐藏后不复位，stretch 成功后图片仍不可见 | frontend-dev → t5（已完成，待 t6 复核） |
| ISSUE | 任务书示例清洗符写为 `-`，实现为 `_`（与 smoke/前端口径一致，建议文档澄清） | 船长确认后改文档 |
| ISSUE | stack/stretch 任务过快，纯 UI 采样难捕捉中间 percent（WS 事件可证，不影响功能） | — |

**证据文件**：`/tmp/v12_smoke.log`、`/tmp/v12_manual_final.py`（14/14）、`/tmp/v12_chinese_capture_repro.py`、`/tmp/v12_e2e_final.cjs`（25/28）、`/tmp/v12_e2e_server.log`。
**未 commit**；本次评审仅新增本报告文件，未改任何源码。

---

# 修复复核（t6，2026-08-22）

复核对象：t4（backend：FITS 头 ASCII 安全 + 帧失败正确中止）与 t5（frontend：job_id 契约 + 预览图可见性）。

## 复核结果摘要

| 项 | 结果 | 证据 |
| --- | --- | --- |
| 后端 smoke | **PASS** | 148/148，exit 0（`/tmp/v12_recheck_smoke.log`） |
| 后端手工复核 | **PASS** | 16/16（`/tmp/v12_recheck_manual.py`） |
| 前端 build/lint | **PASS** | build exit 0；oxlint 0 warning / 0 error |
| 前端静态核对 | **PASS** | toJobEvent/isValidJobId、key={previewUrl}、onLoad 复位均在 |
| E2E 抽查 | **PASS** | 24/24（`/tmp/v12_recheck_e2e.cjs`） |

**最终结论：原 FAIL 全部修复，v1.2 修复复核 PASS（另记录 1 个 P2 改进项，见下）。**

## 1. 后端复核 —— PASS

- smoke 全绿：**148/148，exit 0**。新增中文会话用例全部通过：effective 保留中文、frames_done=2、sessions 详情 2 帧、`FITS SESSION` 头为 printable ASCII（实测 `20260822T13-46-59____LEED__`）。
- 手工复现原 FAIL（中文 2 帧 bias）：**PASS**
  - plan 返回 `20260822T13-51-08_中文_测试abc`；finished、frames_done=2；磁盘实际 2 帧 FITS；
  - sessions 列表/详情中文 id/name 正常、frame_counts.bias=2；
  - 下载 FITS：`SESSION` 头 printable ASCII（`20260822T13-51-08______abc`），不含中文。
- 写盘失败不假 finished：**PASS**
  - 临时数据目录中 plan `WRITEFAIL_TEST` bias×3，start 后等 `bias/thumbs` 目录就绪，将 `bias/` 设为 0o555；
  - 3 帧写失败（PermissionError）后状态收敛为 **error**（非 finished），frames_done=0，message/log 含「BIAS 所有帧均失败，自动中止」；sessions 中该会话 bias=0；测试后恢复权限并清理临时目录。
- P2 改进项（新增观察，非本次 FAIL 修复范围）：若在会话目录/子目录**创建过程中**（如 `bias/thumbs` 尚未建好）就注入只读，`_run` 的目录创建 PermissionError 未被捕获，线程退出后状态会停留在 running（实测复现）。建议 backend-dev 后续给 `_run` 目录初始化加兜底 `_finish("error", ...)`；不影响正常写盘失败路径的判定。

## 2. 前端复核 —— PASS

- `npm run build` exit 0；`npm run lint` 0 warning / 0 error。
- 静态核对（ProcessingPage.tsx / types.ts）：
  - `JobSubmitResult {job_id,type,status}` + `toJobEvent()` 归一为 `{id:job_id,...}`；runCalibrate/runStack/runStretch 全部走 `toJobEvent` 后 `setJobs`；
  - `isValidJobId` 过滤空/undefined，轮询只请求真实 id（activeJobIdsRef 同步处与 handleJobUpdate 入口双重过滤）；
  - 预览 `<img key={previewUrl}>` 随 URL 重挂载，`onLoad` 清空 display、`onError` 隐藏——修复原“加载成功但 display:none”问题。

## 3. E2E 抽查 —— PASS（24/24）

headless Chrome（channel:'chrome'，uvicorn:8000）：打开 mock → UI 4×2 小计划 → LIGHT waiting_user 确认 → Processing 提交 calibrate/stack/stretch。

- 无幽灵 pending 行：三种任务表内各只有 1 行（原 bug 为 calibrate 2 行）。
- 控制台/网络无错误：全程无 `/api/process/jobs/undefined` 404，无任何 `/api` 4xx，无资源加载 console error。
- 预览可见：stretch 完成后 `display=inline`、`naturalWidth=1024`（原 bug 为 display:none）。
- `?v` 自增：0 → 1（提交）→ 2（完成），`?v=1/2` 重新请求均 200 + `Cache-Control: no-store` + PNG。
- 进度推进：UI 观测 calibrate `45%→50%→100%`；WS 事件级 calibrate `0,1,15,30,45,50,75,95,100`、stack `0,1,55,95,100`、stretch `0,1,20,45,90,100`。
- 步骤条：Calibrate→Stack→Stretch 依次推进，最终 3 步全部 finish。

## 4. 最终分级汇总

| 级别 | 条目 | 状态/修复人 |
| --- | --- | --- |
| P0 | 无 | — |
| P1 | 原 FAIL：中文 session FITS 头崩溃、写盘失败假 finished | 已修复（t4），复核 PASS |
| P1 | 原 FAIL：job_id 幽灵轮询、预览图不可见 | 已修复（t5），复核 PASS |
| P2 | 目录初始化期间注入只读 → 状态停留 running | 待 backend-dev 后续兜底（不影响本次验收） |
| 最终判定 | **PASS** | 复核证据：smoke 148/148、手工 16/16、build+lint 0 error、E2E 24/24 |

