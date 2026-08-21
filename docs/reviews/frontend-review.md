# 前端独立评审报告（docs/api.md v1.0 契约对照 + 构建验证）

- 评审对象：`frontend/`（只读评审，未修改任何 frontend/ 源码）
- 契约基准：`docs/api.md` v1.0
- 评审人：reviewer（frontend-verify，t5）
- 评审日期：2026-08-22
- 环境：Node v26.7.0 / npm 11.19.0 / TypeScript 6.0.3 / Vite 8.2.2

## 结论摘要

- 构建：`npm install` 与 `npm run build` 均 **通过（exit 0）**。
- 契约对照 8 项：**全部 PASS**，未发现端点/字段不一致或违反契约的行为。
- 代码质量：**0 个 FAIL**；1 个功能性 ISSUE（坏帧“排除”仅前端标记、不影响后端处理），其余为低优先级工程改进项。
- 建议修复人统一为 **frontend-dev**（涉及契约变更的项需先报船长确认）。

---

## 1. 构建验证

| 检查项 | 结果 | 证据 |
| --- | --- | --- |
| `cd frontend && npm install` | **PASS**（exit 0） | “up to date in 301ms”；仅有 npm 警告：`fsevents@2.3.3` install script 未被 allowScripts 覆盖（npm 11 脚本审批策略，环境级告警，不影响 Linux/构建产物；fsevents 为 macOS 可选依赖） |
| `cd frontend && npm run build`（`tsc -b && vite build`） | **PASS**（exit 0） | `tsc -b` 零诊断输出；Vite 3616 modules transformed；产物 `dist/index.html 0.47 kB`、`index-*.js 1,622.89 kB（gzip 492.73 kB）`。唯一 warning：单 chunk 超过 500 kB |
| 附加：`npm run lint`（oxlint） | **PASS**（exit 0，0 error） | 13 条 `react` 规则 warning（详见问题 I-3），无 error |

**构建 warning 记录：**

1. `npm warn install-scripts fsevents@2.3.3 (install scripts present)` —— 环境/审批策略相关，非代码问题。
2. `(!) Some chunks are larger than 500 kB after minification` —— antd + recharts + 全站页面打进单 bundle（问题 I-2）。

---

## 2. docs/api.md 契约对照（逐项）

### 2.1 axios 统一封装对 `{ok,data,error}` 的处理 —— **PASS**

`src/api/client.ts`：
- 响应拦截器识别 `'ok' in body`；`ok=true` 解包为 `data`（`{...response, data: body.data}`），调用方拿到的直接是业务数据。
- `ok=false` 时 `message.error(body.error || '请求失败')` 并 `reject(new ApiError(errText, status))`，HTTP 状态码语义保留（400/404/409/500）。
- 网络/HTTP 错误在 rejection 分支统一弹错；优先读取后端 error 字段，409 有专门文案，超时（ECONNABORTED）与连接失败有独立文案。
- `silent` 配置用于轮询等场景避免弹窗刷屏（CapturePage 状态轮询/缩略图轮询、ProcessingPage job 轮询均已正确使用）。
- `apiGet/apiPost/apiPut` 泛型化，返回类型为解包后的 `data`。符合 api.md §5 “axios 统一封装：解包 {ok,data,error}，ok=false → antd message.error(error)”。

### 2.2 端点与请求体字段一致性 —— **PASS**

逐端点核对（字段名与 api.md §2 完全一致）：

| 端点 | 前端调用点 | 方法/请求体 |
| --- | --- | --- |
| GET /api/health | SettingsPage.tsx:37 | 无 body；per-request baseURL 健康检查 ✓ |
| GET /api/devices | ConnectPage.tsx:36 | ✓ |
| POST /api/device/open | ConnectPage.tsx:63 | `{ id }` ✓ |
| POST /api/device/close | ConnectPage.tsx:74 | 无 body ✓ |
| GET /api/device/info | ConnectPage.tsx:46 | ✓ |
| GET /api/properties | LivePage.tsx:82 | ✓ |
| PUT /api/properties/{name} | LivePage.tsx:97 | `{ value }`；回读 `actual` ✓ |
| POST /api/live/start | LivePage.tsx:282 | `{ fps, scale, quality }` ✓ |
| POST /api/live/stop | LivePage.tsx:291 | 无 body ✓ |
| POST /api/snapshot | LivePage.tsx:313 | `{ format }`（fits/tiff/png 枚举）✓ |
| POST /api/capture/plan | CapturePage.tsx:213 | `{ session_name, phases[]: { type, count, exposure_us, note } }` ✓；`exposure_us` 空值以 `null` 提交 ✓ |
| POST /api/capture/start | CapturePage.tsx:214 | 无 body ✓ |
| GET /api/capture/state | CapturePage.tsx:96 | ✓（silent 轮询） |
| POST /api/capture/resume | CapturePage.tsx:230 | 无 body，仅 waiting_user Modal 触发 ✓ |
| POST /api/capture/abort | CapturePage.tsx:240 | 无 body ✓ |
| GET /api/sessions | CapturePage.tsx:136 / ProcessingPage.tsx:90 | ✓ |
| GET /api/sessions/{id} | CapturePage.tsx:141 / ProcessingPage.tsx:107 | ✓ |
| POST /api/process/calibrate | ProcessingPage.tsx:178 | `{ session_id, sigma, method }` ✓ |
| POST /api/process/stack | ProcessingPage.tsx:197 | `{ session_id, sigma, method, registration }` ✓ |
| POST /api/process/stretch | ProcessingPage.tsx:217 | `{ session_id, result_file, stretch, black, white }` ✓ |
| GET /api/process/jobs/{job_id} | ProcessingPage.tsx:155 | 轮询兜底 ✓ |

未发现前端自行拼接 `/api/files/...` 路径；所有文件/图片地址均来自后端返回的 `url/thumb/preview` 字段。

### 2.3 WS 二进制 JPEG 与文本 JSON 区分 —— **PASS**

- `src/api/ws.ts:61`：`ws.binaryType = 'blob'`，保证 JPEG 以 Blob 到达。
- `LivePage.tsx:256-270`：`typeof ev.data === 'string'` 走 JSON.parse（解析失败静默忽略），`ev.data instanceof Blob` 走 canvas 图像路径。
- 统计 JSON 同时校验 `histogram` 为数组且长度 === 256 才写入 state，防止脏数据破坏渲染。
- 说明：实现只处理 Blob 分支、未显式处理 ArrayBuffer，但由于 `binaryType='blob'` 已固定，实际不存在 ArrayBuffer 到达路径，与 api.md §5 的“Blob/ArrayBuffer 走图像路径”语义一致（满足其一即可）。
- `/ws/events` 两个页面均先判 `typeof ev.data !== 'string'` 再 parse，不会被二进制串扰（事件通道本身只发文本）。

### 2.4 /ws/live 统计渲染与 256 bin 直方图 —— **PASS**

- `LivePage.tsx`：渲染 min / max / mean / std / saturated_pct / FPS 六个 Statistic，字段名与契约文本 JSON 一致。
- `histogramData` 将 256 个 int 映射为 256 条 Bar，无抽样/降采样，标题明确 “Histogram（256 bin）”；`isAnimationActive={false}` 适合高频更新。
- 前端仅显示后端下发的 8-bit 预览图直方图，未自行重算，口径与后端一致。

### 2.5 /ws/events 订阅 —— **PASS**

- `CapturePage.tsx:127`：订阅 `/ws/events`，处理 `topic === 'capture'`，实时更新拍摄状态；另有 1s 轮询兜底（silent）。
- `ProcessingPage.tsx:145`：订阅 `/ws/events`，同时处理 `topic === 'capture'` 与 `topic === 'job'`；job 结束（done/error）自动刷新 session 详情、stretch 完成刷新预览版本号；另有 1s job 轮询兜底。
- 后端 `main.py` 的 `/ws/events` 连接即推送当前 capture 状态快照，与前端立即渲染兼容。

### 2.6 waiting_user 确认 Modal（LIGHT 人工确认） —— **PASS**

- `CapturePage.tsx:484-504`：`open={status==='waiting_user'}`，`closable/maskClosable/keyboard=false`，唯一动作“继续”→ `POST /api/capture/resume`。
- LIGHT 强制确认：`modalPrompt`（CapturePage.tsx:345-351）在 `current_phase.type === 'light'` 且后端 prompt 未包含提示时，追加 “请对照实时预览与直方图确认曝光/增益/黑电平参数后继续。”；后端 LIGHT prompt 本身也包含人工确认文案，两端叠加后用户必见人工确认提示。
- 非 LIGHT 相位同样进入 waiting_user Modal（契约：每个相位开始前 waiting_user），行为一致。

### 2.7 缩略图与下载 URL —— **PASS**

- CapturePage 缩略图墙：`src={resolveUrl(f.thumb)}`（f.thumb 为后端返回字段）。
- ProcessingPage 结果表：下载 FITS `href={resolveUrl(record.url)}`、打开预览 `href={resolveUrl(record.preview)}`。
- LivePage 快照下载：`href={resolveUrl(snapshot.url)}`。
- 预览图缓存刷新用 `?v=` query（不是路径拼接），符合“不自行拼接 URL 路径”的要求。

### 2.8 设置页后端地址持久化 —— **PASS**

- `src/api/backend.ts`：默认 `http://localhost:8000`，localStorage key `ds.backend`；读取优先级 localStorage > `VITE_BACKEND` env > 默认值；`normalizeBackend` 去除尾部斜杠。
- SettingsPage：保存/恢复默认均有 UI 与 `message` 反馈；健康检查直接请求当前输入地址（per-request baseURL + silent）。
- `resolveUrl`/`wsUrlFor` 在 dev 使用该持久化地址（WS/图片立即生效），生产走同源（https→wss 推导正确）。行为与 api.md §5、frontend/README 完全一致。

---

## 3. 代码质量

| 检查项 | 结论 | 说明 |
| --- | --- | --- |
| 组件拆分 | **ISSUE（P3）** | 页面级拆分清晰（Connect/Live/Capture/Processing/Settings + api 模块），LivePage 的 NumericPropertyRow/BoolPropertyRow/RoiInput 已抽子组件；但 CapturePage（507 行）与 ProcessingPage（571 行）仍偏大，建议把相位表格/状态卡/任务表、处理三步卡拆成独立组件（I-1 修复时顺带）。 |
| 类型安全（TS 严格模式） | **PASS（带保留意见）** | `tsconfig.app.json` 未显式写 `"strict": true`，但 TypeScript 6.0 默认开启 strict 族检查（本机探针验证 TS7006/TS2322 在默认配置即报错），`tsc -b` 全量零错误；源码无 `any` 显式标注、无 `@ts-ignore/@ts-expect-error`，并开启 `noUnusedLocals/noUnusedParameters/verbatimModuleSyntax/erasableSyntaxOnly`。建议显式补 `"strict": true`，避免依赖 TS 版本默认值（见 I-4）。 |
| 无硬编码绝对路径 | **PASS** | `grep` 全量扫描仅发现契约允许的默认后端地址 `http://localhost:8000`（backend.ts 常量、vite.config 代理目标）；无文件系统路径（/Users、/home、C:\ 等）或写死的资源 URL。 |
| dev 代理与生产 static 托管兼容 | **PASS** | vite.config：`/api/files`（更具体）先于 `/api` 注册，`/ws` 带 `ws:true`，目标 `VITE_BACKEND` env 可覆盖；后端 `main.py`：`DIST_DIR` 存在时 `app.mount("/", StaticFiles(html=True))`，非 `/api`、`/ws` 的 404 经 SPA fallback 返回 index.html，`resolveUrl` 生产模式用同源相对路径。两端行为匹配。 |

---

## 4. 问题清单（建议修复人：frontend-dev）

| 编号 | 严重度 | 位置 | 问题 | 建议 |
| --- | --- | --- | --- | --- |
| I-1 | **P2（功能误导）** | `ProcessingPage.tsx:293`、`:344-448` | “勾选排除坏帧”只维护本地 `excluded` Set，`runCalibrate`/`runStack` 请求体没有把排除结果发给后端（api.md 契约也没有该字段）；用户会以为被排除的帧不参与叠加，实际仍全部参与。 | 二选一：(a) 与船长确认后扩展契约（calibrate/stack 请求体增加 `exclude` 列表），前端随之提交；(b) 契约不变则把 UI 文案改为“仅本地标记，不会影响后端处理”，或暂时隐藏/禁用该功能。**涉及 api.md 契约变更，必须先报船长。** |
| I-2 | P3 | 构建输出 | 单 bundle 1,622.89 kB（gzip 492.73 kB），触发 Vite chunk>500 kB warning。 | 路由级 `React.lazy` + `import()` 按页面拆 chunk；recharts/antd 按需引入，或给 antd/recharts 单独 vendor chunk。frontend/README 已声明该 warning“可容忍”，故不阻塞。 |
| I-3 | P3 | `api/ws.ts:22-23`、`CapturePage.tsx:89`；多个 effect | oxlint 13 条 warning：渲染期写 ref（React Compiler 会跳过优化）、effect 内同步 setState。 | 用 `useEffect` 同步 ref，或将 ws hook 的 `onMessage/onStatusChange` 改为 effect 依赖；setState-in-effect 类可保留但建议对照 React 官方模式收敛（如派生状态/初始化函数）。 |
| I-4 | P3 | `tsconfig.app.json` | 未显式 `"strict": true`，当前靠 TS 6.0 默认值获得严格检查；一旦降级 TS 版本严格度会静默放宽。 | 显式写入 `"strict": true`（当前代码在 TS 6 严格下已零错误，写入无风险）。 |
| I-5 | P3 | `api/settings.ts:24-29` | localStorage 预览参数只做范围钳制，未强制整数与 scale 允许步长（0.25/0.5/1.0）；被篡改或旧版本残留值（如 fps=15.5、scale=0.3）会导致 `/api/live/start` 返回 400。 | `fps/quality` 取 `Math.round` 并钳制；`scale` 收敛到最接近的 {0.25,0.5,1.0}。 |
| I-6 | P3 | `SettingsPage.tsx:100-109` | “恢复默认”直接 `setBackend(DEFAULT_BACKEND)` 写入 localStorage，会使 localStorage 优先级盖过 `VITE_BACKEND` env；在 dev 用 env 指定代理目标时，点“恢复默认”后 WS/图片指向 localhost、而 REST 代理仍指向 env，出现地址分裂。 | 恢复默认应 `clearBackend()`（清除 localStorage），让 env/默认值按 `getBackend()` 的既有优先级自然生效。 |
| I-7 | P3 | `ProcessingPage.tsx:147-169` | job 表只包含“本页提交”或“WS 事件到达”的任务；页面刷新/重新挂载后，后端正在运行的历史任务不会被拉取/轮询，直到下一条 job 事件才出现。 | 可选：提供 `GET /api/process/jobs` 列表端点并在挂载时拉取（需契约变更）；或至少把 job 轮询 id 来源记录到 sessionStorage 兜底。 |
| I-8 | P3 | `LivePage.tsx:280-293` | 进入 /live 即自动 `/api/live/start`；未打开设备时后端 409，axios 默认弹错误 toast，体验略吵（页面随后有“未打开设备”提示，语义不冲突）。 | 该 start 请求加 `silent: true`，页面用 `streamStarted=false` + 现有 Empty/Alert 呈现失败态。 |

---

## 5. 评审方式与限制

- 只读检查 `frontend/src`、`frontend/*.json`、`vite.config.ts`、`frontend/README.md`，对照 `docs/api.md`；为验证“生产 static 托管兼容”同时只读检查了 `backend/app/main.py` 的静态挂载与 SPA fallback。
- 执行了 `npm install`、`npm run build`、`npm run lint`；未运行后端集成联调。
- **未修改 frontend/ 任何源码文件**；本报告为唯一新增文件。
