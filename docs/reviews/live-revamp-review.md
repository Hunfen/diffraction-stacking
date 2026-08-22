# 实时预览页改造与快照文件夹独立验收报告（live-revamp-review）

- 评审对象：`backend/` v1.3 快照文件夹接口（t1） + `frontend/src/pages/LivePage.tsx` / `frontend/src/api/types.ts` 实时预览页改造（t2）
- 契约基准：`docs/api.md` v1.3、t1/t2 任务口径
- 评审人：reviewer（live-revamp-review，t3）
- 评审日期：2026-08-22
- 评审方式：只读源码审查 + 实际运行（backend smoke / 手工 API / 前端 build+lint / headless Chrome）；**未修改任何源码、未 commit**（本报告文件除外）
- 环境：macOS，Python 3.12.14（backend/.venv），Node v26.7.0 / npm 11.19.0 / Vite 8.2.2 / oxlint，playwright-core 1.62.1 + headless Chrome（channel:'chrome'，/tmp/pwtest）

## 结论摘要

| 板块 | 结果 |
| --- | --- |
| 后端 smoke | **PASS**：173/173，退出码 0 |
| 后端手工 API（v1.3 快照文件夹） | **PASS**：13/13 |
| 前端 `npm run build` + `npm run lint` | **PASS**：build exit 0；lint 0 warnings / 0 errors |
| 静态核对 5 项 | **PASS**：5/5 |
| headless Chrome /live 验收 | **1 个真实 FAIL**（见下），其余全部 PASS |

**FAIL**：页面运行中“关闭设备 → GET /api/properties 返回 409”这一警告，**不会**使已关闭的顶部 Alert 自动复现。根因：LivePage 在页面存续期间只在挂载时与 ROI 写后 refresh 时拉取 `/api/properties`（无轮询、无设备状态订阅）；且设备关闭后 `/ws/live` 连接仍保持 open（LiveService 线程空转不出帧），页面感知不到“未打开设备”警告。而页面能实际观测到的警告源（统计饱和比例 >1%、WS 断开/reconnecting）复现机制实测 **PASS**。

**总体判定：1 项 FAIL → 暂不通过**。其余全部达标；建议修复后复验放行（详见 §6）。

**更新（t5 修复复核，2026-08-22 第二轮）：上述 FAIL 已由「每 5s 静默轮询 /api/properties」修复。本轮对当前工作树重新执行静态核对、build+lint、headless Chrome 复验，**全部 PASS**（关闭设备 → 409 → Alert 在 1915ms 内复显，≤6s；重开设备 4831ms 恢复；饱和路径抽查 211ms 复显）。**总体判定更新为：通过**（详见 §9）。

---

## 1. 后端验收

### 1.1 smoke：PASS（173/173）

`cd backend && .venv/bin/python ../scripts/backend_smoke.py` → **173/173 通过，退出码 0**。
证据：`/tmp/pwtest/backend_smoke_review.log`（尾行 `SMOKE 结果：173/173 通过`、`全部通过 ✅`）。其中 `step_snapshot_folders` 覆盖：folder 缺省/空白→根目录、`测试文件夹` 创建落盘、folders 可见、url 可下载（FITS SIMPLE 魔数）、no-store、`../evil`/`/abs`/`a/b`/`a\b`/`bad:name`→400 中文错误、子目录路径穿越→400。

### 1.2 手工 API 验收：PASS（13/13）

脚本 `/tmp/pwtest/manual_api_check.py`（uvicorn 127.0.0.1:8000 + urllib，先 `POST /api/device/open {id:"mock-1"}`），结果存档 `/tmp/pwtest/manual_api_review.json`：

| # | 检查项 | 结果 | 实测证据 |
| --- | --- | --- | --- |
| 1 | GET /api/snapshot/folders 初始结构 | **PASS** | 200 + ok；`[{name:'', files:73}, {name:'测试文件夹', files:5}]`（根目录 name='' + 按名称排序子目录） |
| 2 | POST /api/snapshot {format:'fits', folder:'测试目录'} | **PASS** | 200；返回 `{file:'SNP_20260822T18-15-21.fits', url:'/api/files/snapshots/测试目录/SNP_20260822T18-15-21.fits', folder:'测试目录'}` |
| 3 | 返回 url 可下载 | **PASS** | 200，1,578,240 字节，`SIMPLE` FITS 魔数 |
| 4 | 下载响应 no-store 保留 | **PASS** | `Cache-Control: no-store`（子目录与根目录文件均验证） |
| 5 | folders 列表出现“测试目录” | **PASS** | `['', '测试文件夹', '测试目录']` |
| 6 | folder='../x' → 400 | **PASS** | 400 + 中文错误 `folder 不允许包含路径分隔符` |
| 7 | folder='/abs' → 400 | **PASS** | 400 + `folder 必须是相对子目录名（不允许绝对路径）` |
| 8 | folder='a/b' → 400 | **PASS** | 400 + 中文错误 |
| 9 | folder 含非法字符（bad:name）→ 400 | **PASS** | 400 + `folder 包含非法字符：:` |
| 10 | 前置：设备未打开时 POST snapshot → 409 | **PASS** | 首次运行实测 409 `设备未打开`（打开 mock-1 后通过），符合契约 |

实现口径与源码一致：`backend/app/main.py:471-529`（单段相对目录名校验 + POST/GET folders）、`backend/app/capture.py:401-440`（子目录落盘与 url）、`backend/app/main.py:600-630`（`snapshots/<folder>/<file>` 单级访问 + 路径穿越防护）、`backend/app/main.py:645-653`（no-store）。

---

## 2. 前端构建与 lint：PASS

| 命令 | 结果 | 证据 |
| --- | --- | --- |
| `cd frontend && npm run build` | **PASS**（exit 0） | tsc -b 零诊断；vite 3617 modules；`dist/index.html 0.47 kB`、`index-B_Nibb0r.js 1,636.61 kB（gzip 496.83 kB）`。日志 `/tmp/pwtest/frontend_build_review.log` |
| `cd frontend && npm run lint` | **PASS**（exit 0） | `Found 0 warnings and 0 errors`。日志 `/tmp/pwtest/frontend_lint_review.log` |

---

## 3. 静态核对（5 项）：PASS

| # | 核对项 | 结果 | 源码证据（frontend/src/pages/LivePage.tsx，行号按本次评审快照） |
| --- | --- | --- | --- |
| A | Alert closable + 警告自动复显逻辑 | **PASS** | L758-766：`closable` + `onClose` 置 `alertDismissed=true`（内存态，刷新恢复可接受）；L726-742：`deviceWarning`（properties 为空）/ `wsStatus closed|reconnecting`（含两态互切）/ `saturated_pct>1%` 任一从无到有 → `setAlertDismissed(false)`。**注**：逻辑存在，但“deviceWarning 从无到有”的触发依赖 properties 重拉，见 §4.3/§6 运行时 FAIL。 |
| B | 抓拍卡在统计卡正下方（同一右列竖排） | **PASS** | L806-922：第一行右侧 `Col lg={8}` 内 `Space direction="vertical"`，先“实时统计”卡（L808）后“抓拍（单帧）”卡（L852）。 |
| C | folder 下拉/新建/记忆 | **PASS** | L570-591：选项 = 根目录（name=''，带文件数）+ “新建文件夹…” + 子目录列表；L874-882：选新建出现 Input；L594-613：POST body `{format, folder}`、成功后回填后端 `folder` 并写 `localStorage('ds.snapshot.folder')`、刷新列表。类型同步 `types.ts:81-86 SnapshotResult.folder`。 |
| D | trigger_mode 不在 UI | **PASS** | L438-444：`boolProps` 仅 `reverse_x/reverse_y`；全 frontend/src grep `trigger_mode` 零命中（后端属性保留不动）。 |
| E | ROI 卡在右列 + canvas 拖选换算与钳制 + 重置全画幅 | **PASS** | L960-993：第二行右侧 `Col lg={8}` ROI 卡（四 InputNumber + 当前 ROI/全幅最大 + 重置按钮）；L101-106 `snapRoi` 按 step 吸附再 clamp min/max；L178-190 CSS→canvas 坐标换算（`getBoundingClientRect` 比例）；L641-684：`全幅坐标 = 当前 offset + 拖选坐标/scale`，offset 上限随新尺寸收紧（`max(offset.min, 全幅max−新尺寸)`），依次 PUT width→height→offset_x→offset_y 后 `refresh()` + `message.success`；L700-724：重置 = width/height=max、offset=0；L138-171：每帧 drawImage 后持续重绘 ROI 覆盖框 + 十字准星 + 半透明拖选框。 |

---

## 4. headless Chrome 验收（/live）

环境：uvicorn `app.main:app` @ 127.0.0.1:8000（FastAPI 托管生产 dist）；playwright-core 1.62.1 `chromium.launch({channel:'chrome', headless:true})`，viewport 1440×1200。主脚本 `/tmp/pwtest/live_review_check.js`（输出 `/tmp/pwtest/live_review_check.log`、`/tmp/pwtest/live_review_results.json`）。

### 4.1 卡片顺序与布局：PASS

DOM 卡片顺序：`预览画面 → 实时统计 → 抓拍（单帧）→ 相机参数 → 拍摄区域（ROI）`。
实测几何（px）：预览卡 x=16,w=933；统计卡 x=965（右列，同排 y≈199）；抓拍卡 x=965,y=646（与统计卡同列、水平重叠 459px、位于其正下方）；ROI 卡 x=965,y=861（第二行右列，相机卡 x=16 在左）。与 t2 契约 E 完全一致。
截图：`/tmp/pwtest/review_live_initial.png`。

### 4.2 Alert 可关闭 + 警告自动复显：**部分 PASS（1 项 FAIL）**

| 子项 | 结果 | 证据 |
| --- | --- | --- |
| 初始 Alert 可见且带关闭图标 | **PASS** | `.ant-alert` + `.ant-alert-close-icon` 存在 |
| 点击关闭后消失 | **PASS** | 关闭后 `document.querySelector('.ant-alert') === null`；截图 `/tmp/pwtest/review_live_after_close_alert.png` |
| 出现新警告（饱和比例>1%）→ 自动复显 | **PASS** | PUT exposure=30000000 后约 1-3s Alert 重新出现；截图 `/tmp/pwtest/review_live_alert_reappear_saturation.png` |
| WS 断开（closed/reconnecting）→ 自动复显 | **PASS** | 关闭 Alert 后 kill uvicorn（PID 75512），WS 状态变“重连中”，Alert 重新出现；脚本 `/tmp/pwtest/ws_close_warning_check.js`（3/3），截图 `/tmp/pwtest/review_live_alert_reappear_ws.png` |
| **关闭设备触发 409 警告 → 自动复显** | **FAIL** | 页面运行中 `POST /api/device/close`，等待 4.5s：`GET /api/properties` 实测 409（警告源确实发生），但 `.ant-alert` 仍为 null，WS Tag 仍为“已连接”；截图 `/tmp/pwtest/review_live_device_closed.png`。详见 §6。 |

### 4.3 ROI 输入框 + 拖选换算/钳制 + 重置：PASS（7/7）

脚本 `/tmp/pwtest/roi_drag_focus_check.js`（输出 `/tmp/pwtest/roi_drag_focus_check.log`）。为消除 Playwright 物理鼠标整数坐标舍入误差，按任务允许的方式在页面内以**精确合成 MouseEvent**（分数 clientX/clientY）模拟拖选：

- 前置：mock 默认 ROI 1024×768、offset 448/216，canvas 内部 1024×768（scale=1），四输入框 1024/768/448/216 —— PASS。
- 从画布 25%→60% 拖选（canvas 坐标 x=256,y=192,w=358.4,h=268.8）：
  - PUT 4 次，顺序 width→height→offset_x→offset_y，值 **360 / 272 / 704 / 408**，与公式 `width=snap(w/scale)`、`offset = 当前offset + 拖选坐标/scale`、offset 上限=全幅max−新尺寸、按 step/min/max 钳制的理论值**完全一致** —— PASS。
  - 拖选后四个输入框同步显示 360/272/704/408，并出现消息 `ROI 已设为 704,408 360×272` —— PASS。
- 点击“重置全画幅”：PUT 1920/1200/0/0，输入框同步 1920/1200/0/0，消息“ROI 已重置为全画幅” —— PASS。
- 截图：`/tmp/pwtest/review_live_roi_after_drag.png`、`/tmp/pwtest/review_live_roi_after_reset.png`。

说明：主脚本第三轮曾出现拖选值与理论值差 8（一个 step）的记录，定位为 Playwright 物理鼠标坐标取整导致 canvas 坐标差 ≤1px，非产品缺陷；合成事件精确验证 7/7 后，ROI 项以 7/7 为准。

### 4.4 trigger_mode 文本不存在：PASS

初始与最终两次断言 `document.body.innerText.includes('trigger_mode')` 均为 false；DOM 中无“触发模式”开关。

### 4.5 folder 下拉与记忆（附加集成抽查）：PASS

打开“抓拍（单帧）”卡 folder 下拉，选项实测为：`根目录（73 个文件）`、`新建文件夹…`、`测试文件夹（5 个文件）`、`测试目录（1 个文件）`；选择“测试目录”后 `localStorage['ds.snapshot.folder']='测试目录'`（记忆生效）。本次未做 UI 发起 POST（避免额外污染 data/，POST 链路已由 §1.2 手工 API 与 smoke 覆盖）。

### 4.6 控制台/网络错误：PASS

应用自身流程无 console.error / pageerror / requestfailed。日志中两条 `Failed to load resource ... 409` 为评审脚本主动探测 `/api/properties`（关闭设备后）产生，已在断言中排除并记录。

---

## 5. 逐项汇总

| # | 项 | 结果 | 证据路径 |
| --- | --- | --- | --- |
| 1 | backend smoke 全绿 | **PASS** | `/tmp/pwtest/backend_smoke_review.log`（173/173，exit 0） |
| 2 | 手工：folders 初始结构 | **PASS** | `/tmp/pwtest/manual_api_review.json`（13/13） |
| 3 | 手工：POST folder='测试目录' + url 可下载 + 列表出现 | **PASS** | 同上 |
| 4 | 手工：../x、/abs、a/b、非法字符 → 400 | **PASS** | 同上 |
| 5 | 手工：no-store 保留 | **PASS** | 同上 |
| 6 | frontend build + lint | **PASS** | `/tmp/pwtest/frontend_build_review.log`、`/tmp/pwtest/frontend_lint_review.log` |
| 7 | 静态：Alert closable + 复显逻辑 | **PASS** | LivePage.tsx:726-766 |
| 8 | 静态：抓拍卡在统计卡下方同右列 | **PASS** | LivePage.tsx:806-922 |
| 9 | 静态：folder 下拉/新建/记忆 | **PASS** | LivePage.tsx:570-613, 855-882 |
| 10 | 静态：trigger_mode 不在 UI | **PASS** | LivePage.tsx:438-444；grep 零命中 |
| 11 | 静态：ROI 右列 + 换算钳制 + 重置 | **PASS** | LivePage.tsx:101-171, 641-724, 960-993 |
| 12 | 浏览器：卡片顺序/右列布局 | **PASS** | `/tmp/pwtest/live_review_check.log`；`/tmp/pwtest/review_live_initial.png` |
| 13 | 浏览器：Alert 关闭后消失 | **PASS** | `/tmp/pwtest/review_live_after_close_alert.png` |
| 14 | 浏览器：饱和警告 → Alert 复显 | **PASS** | `/tmp/pwtest/review_live_alert_reappear_saturation.png` |
| 15 | 浏览器：WS 断开 → Alert 复显 | **PASS** | `/tmp/pwtest/ws_close_warning_check.log`；`/tmp/pwtest/review_live_alert_reappear_ws.png` |
| 16 | 浏览器：关闭设备 409 → Alert 复显 | **FAIL** | `/tmp/pwtest/live_review_check.log`（AFTER_DEVICE_CLOSE）；`/tmp/pwtest/review_live_device_closed.png` |
| 17 | 浏览器：ROI 输入框 + 拖选 PUT + 显示同步 | **PASS** | `/tmp/pwtest/roi_drag_focus_check.log`（7/7）；`/tmp/pwtest/review_live_roi_after_drag.png` |
| 18 | 浏览器：重置全画幅 + 显示同步 | **PASS** | 同上；`/tmp/pwtest/review_live_roi_after_reset.png` |
| 19 | 浏览器：trigger_mode 文本不存在 | **PASS** | `/tmp/pwtest/live_review_check.log` |

---

## 6. FAIL 详情与修复建议

**FAIL：关闭设备（/api/properties 409）后，已关闭的顶部 Alert 不会自动复现。**

复现步骤（均为实测）：页面加载并连接实时流 → 点击关闭顶部 Alert → `POST /api/device/close` → 等待 4.5s。
观测：`GET /api/properties` 返回 409（警告源成立）；`.ant-alert` 仍不存在；预览卡 WS Tag 仍显示“已连接”（`/ws/live` 连接未被服务端关闭，LiveService 抓帧线程抓不到帧后静默空转、不再发帧）。

根因分析：
1. `deviceWarning = !propsLoading && properties.length === 0`（LivePage.tsx:727）只依赖 `properties` 状态；而 `useProperties` 只在组件挂载时拉取、且在 ROI PUT/重置成功后 `refresh()`（LivePage.tsx:675/717）——页面存续期间没有任何定时轮询或设备状态订阅。外部关闭设备不会被感知。
2. `/ws/live` 在设备关闭后连接保持 open（服务端 LiveService 线程继续存活，仅不出帧），因此 `wsStatus` 警告路径也不会触发；且冻结的旧统计不会触发饱和警告。
3. 对比：页面能实际观测到的警告源（饱和比例 >1% 的 WS 统计变化、WS 断开/reconnecting 状态迁移）复显机制实测均 PASS，说明 dismissed 复位逻辑本身正确，缺的是 deviceWarning 的“从无到有”触发路径。

附带观察（同根因）：外部“关闭→重开”设备后不刷新页面，mock 相机 ROI 复位为 1024×768，实时帧随之变为 1024×768，而 ROI 卡仍显示旧值 1920×1200/0/0，直到下一次 properties refresh 才同步。正常使用路径（经“设备连接”页开关设备后再进入 /live 会重新挂载页面）风险较低，但建议一并修复。

建议（供船长裁决，由 frontend-dev 实施）：
- 对 `/api/properties` 增加低频率 silent 轮询（如 2–5s，复用 `useProperties.refresh`），或在 `/ws/live` 长时间无帧/收到后端设备状态事件时触发 refresh；也可由后端在设备关闭时主动断开 `/ws/live` 或广播设备事件，让前端走 WS 状态警告路径。
- 修复后复验本报告 §4.2 的 FAIL 子项即可。

---

## 7. 证据文件清单

- 日志/脚本：`/tmp/pwtest/backend_smoke_review.log`、`/tmp/pwtest/manual_api_check.py`、`/tmp/pwtest/manual_api_review.json`、`/tmp/pwtest/frontend_build_review.log`、`/tmp/pwtest/frontend_lint_review.log`、`/tmp/pwtest/live_review_check.js/.log`、`/tmp/pwtest/live_review_results.json`、`/tmp/pwtest/roi_drag_focus_check.js/.log`、`/tmp/pwtest/roi_drag_focus_results.json`、`/tmp/pwtest/ws_close_warning_check.js/.log`
- 截图：`/tmp/pwtest/review_live_initial.png`（初始布局）、`review_live_after_close_alert.png`（关闭后）、`review_live_alert_reappear_saturation.png`（饱和警告复显）、`review_live_alert_reappear_ws.png`（WS 断开复显）、`review_live_device_closed.png`（关闭设备 409 未复显，FAIL 证据）、`review_live_roi_after_drag.png` / `review_live_roi_after_reset.png`（ROI 拖选/重置）
- 全部截图已验证为 1440×1200 PNG。

## 8. 环境与收尾说明

- 验收用 uvicorn（127.0.0.1:8000）已停止；测试脚本均在 `/tmp/pwtest`，不落库。
- 任务指定的手工 POST 在 `data/snapshots/` 生成了“测试目录”子目录（含 1 个 FITS）；`data/` 已 gitignore，属运行数据。`git status` 仅含 t1/t2 的 6 个源码文件修改，**本评审未修改源码、未 commit**。

---

## 9. 修复复核（t5，2026-08-22 第二轮）

复核对象：§4.2/§6 唯一 FAIL 的修复——当前工作树 `frontend/src/pages/LivePage.tsx`（1020 行，较首轮 +23 行，仅此文件发生变化）。方式：只读静态核对 + build/lint + headless Chrome 实测。

### 9.1 静态核对：PASS（4/4）

| # | 核对项 | 结果 | 源码证据 |
| --- | --- | --- | --- |
| 1 | 5s 静默轮询存在且 silent | **PASS** | L218-230：`pollProperties` 用 `apiGet('/api/properties', { silent: true })`，失败置 `properties=[]`（409 → deviceWarning）；L219 注释明确 5s 低频、不碰 loading。请求序号 `fetchSeqRef`（L201）防止旧响应覆盖新状态。 |
| 2 | 卸载清理定时器 | **PASS** | L414-420：`setInterval(..., 5000)`，effect cleanup `return () => window.clearInterval(timer)`。 |
| 3 | deviceWarning false→true 时 dismissed 复位 | **PASS** | L750：`deviceWarning = !propsLoading && properties.length === 0`；L756-765：`deviceWarning && !prev.deviceWarning` → `setAlertDismissed(false)`。关闭设备后轮询 409 → properties 置空 → 触发复位。 |
| 4 | 饱和/WS 两条路径未回归 | **PASS** | L751-752 饱和阈值逻辑与首轮一致（`stats.saturated_pct > 1.0`）；L759-764 的 wsStatus 分支与首轮一致（closed/reconnecting 互切）；`frontend/src/api/ws.ts` 未改动（mtime 04:40，git status 无此项）。 |

### 9.2 build + lint：PASS

- `cd frontend && npm run build`：**exit 0**（`dist/assets/index-C7xHU-x6.js 1,637.02 kB │ gzip 496.91 kB`）。日志 `/tmp/pwtest/t5_build.log`。
- `npm run lint`：**exit 0，Found 0 warnings and 0 errors**。日志 `/tmp/pwtest/t5_lint.log`。

### 9.3 headless Chrome（channel:'chrome'）复验：PASS（7/7）

脚本 `/tmp/pwtest/t5_alert_fix_recheck.js`，结果 `/tmp/pwtest/t5_alert_fix_recheck.log`（viewport 1440×1200，uvicorn 127.0.0.1:8000）：

| 检查项 | 结果 | 实测 |
| --- | --- | --- |
| /live 初始：Alert 可见+closable、ROI 4 输入框、无 trigger_mode 文本 | **PASS** | `{alertVisible:true, alertClosable:true, roiInputs:4, triggerModeText:false}` |
| 点击关闭后 Alert 消失 | **PASS** | `.ant-alert` 不存在 |
| 关闭设备 → 409 → Alert ≤6s 自动重新出现 | **PASS** | **1915ms** 复显；复显时 `GET /api/properties = 409`，WS 标签“已连接”（证明走 deviceWarning 路径而非 WS 路径） |
| 重新打开设备 → 恢复 | **PASS** | **4831ms** 后 ROI 卡恢复 4 个输入框（轮询重新拉回属性） |
| 饱和路径抽查一条 | **PASS** | exposure=30000000 后 **211ms** Alert 复显（无回归），随后恢复 exposure=20000 |
| 应用自身 console/page 错误 | **PASS** | 无；仅评审探测 fetch 的 409 资源行（已排除） |

截图证据：`/tmp/pwtest/t5_initial.png`、`/tmp/pwtest/t5_after_close.png`、`/tmp/pwtest/t5_alert_reappear_409.png`、`/tmp/pwtest/t5_after_reopen.png`、`/tmp/pwtest/t5_saturation.png`（均 1440×1200 PNG）。

### 9.4 修复复核最终结论：**PASS**

- 原 FAIL 场景（页面运行中关闭设备触发 /api/properties 409）现已满足契约：≤6s 内 Alert 自动复现，且重开设备后自动恢复。
- 原警告复显机制与 ROI/布局/trigger_mode 等首轮 PASS 项未回归（build/lint 全绿 + 布局与饱和抽查）。
- 复核过程未修改源码、未 commit；uvicorn 已停止。
