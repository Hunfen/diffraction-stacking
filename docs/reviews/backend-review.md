# 后端独立评审报告（docs/api.md v1.0 契约对照 + 端到端测试）

- 评审对象：`backend/`（只读评审，未修改任何 backend/ 源码）
- 契约基准：`docs/api.md` v1.0
- 评审人：reviewer（backend-verify，t4）
- 评审日期：2026-08-22
- 运行环境：macOS / Python 3.12（`backend/.venv`）/ 相机驱动 mock

## 结论摘要

- 端到端自测：**78/78 通过**（`cd backend && .venv/bin/python ../scripts/backend_smoke.py`，exit 0）。
- 人工补测：**53/53 通过**（错误包裹、非法属性、未连接 409、abort 两种路径、6 组路径穿越、static mount/SPA fallback）。
- 契约端点对照：**全部 PASS**，未发现路径/方法/请求体/响应结构级 FAIL。
- 代码质量：**0 个 FAIL**；发现 1 个 P2 状态机问题（非 LIGHT 相位拍摄期间状态停留在 `waiting_user`，实测复现），其余为 P3 工程改进项。
- 建议修复人统一为 **backend-dev**；涉及契约文本澄清的项需报船长。

---

## 1. 测试验证

### 1.1 端到端自测（必测项）

命令：`cd backend && .venv/bin/python ../scripts/backend_smoke.py`

结果：**PASS，78/78，exit 0**。覆盖：health/devices、open/close、属性读写与钳制、live/start→WS 文本统计+二进制 JPEG→stop、snapshot FITS/PNG 下载、四相位 capture 全流程（LIGHT 前 `waiting_user`、resume、finished、sessions/frames/meta/thumb/FITS 头/12-bit 校验）、calibrate/stack/stretch 与 QC、收尾关闭设备后状态可查。

关键实测输出：
- `histogram 为 256 个 int`、`紧随统计的二进制消息为 JPEG（0xFFD8）` —— WS 消息顺序/字段正确。
- `FITS 头含契约要求的 10 个关键字`、`FITS 为 16-bit 且数据在低 12 位（max ≤ 4095）` —— 帧数据口径正确。
- `路径穿越（bias/../meta.json）被拒绝为 400`、`非法 session_id 被拒绝` —— 路径防护生效。
- `qc：frames=3，total_exposure_us=9000`；抽查 qc_report.json：`effective_bits=0.792`（=log2√3，公式口径一致）、`rejected_pct=0.0`、`background_std=8.214`。

### 1.2 人工补测（任务指定项）

脚本：`/tmp/manual_backend_tests.py`（用 `backend/.venv/bin/python` 执行，uvicorn 子进程 + urllib/http.client，不复用 smoke 进程）。结果：**PASS，53/53，exit 0**。

| 类别 | 结果 | 复现步骤（摘要） |
| --- | --- | --- |
| 错误结构 `{ok,data,error}` | PASS | `GET /api/nonexistent` → 404；`DELETE /api/devices` → 405；`POST /api/device/open` 发送 `{bad json` → 400；均为 `{ok:false,data:null,error:<str>}`，且 `/api` 404 不被 SPA fallback 吞掉 |
| 非法属性名 | PASS | 打开 mock-1 后 `PUT /api/properties/not_a_prop {"value":1}` → 400 `未知属性`；`PUT /api/properties/device_temperature` → 400 `只读`；`PUT /api/properties/mock_pattern {"value":"hex"}` → 400 `仅支持` |
| 未连接时调接口 | PASS | `GET /api/properties`、`PUT /api/properties/exposure`、`POST /api/snapshot`、`POST /api/live/start` → 全部 409；`capture/plan` 允许（契约未要求设备）；plan 后 `capture/start` → 409 `设备未打开` |
| abort 中途（waiting_user） | PASS | plan light×20 → start → poll 到 `waiting_user` → `POST /api/capture/abort` → 200 `{status:"aborted"}` → 状态收敛 aborted 且 0 帧；再 `resume` → 409 |
| abort 中途（拍摄中） | PASS | plan bias×2000 → start → poll 到 frames_done≥5 → abort → 200 aborted → 状态 aborted 且 frames_done 保留；`GET /api/sessions` frame_counts 与保留帧数一致。**同时复现了 I-1：拍摄进行中 status 仍为 waiting_user** |
| 路径穿越 | PASS | 原始 http.client 请求（不经 urllib 规范化）：`GET /api/files/xxx/../../etc/passwd`、`/api/files/../../etc/passwd`、`/api/files/%2e%2e/%2e%2e/etc/passwd`、`/api/files/snapshots/../../etc/passwd`、`/api/files/snapshots/%2e%2e/etc/passwd` → 全部 400；不存在 session → 404；合法 snapshot URL 仍 200 |
| static mount / SPA fallback | PASS | `GET /` 与 `GET /live` → 200 `text/html`（dist 存在时返回 index.html）；`/api` 与 `/ws` 前缀不参与 fallback |

---

## 2. docs/api.md 逐端点核对

| 端点 | 方法 | 核对结果 |
| --- | --- | --- |
| /api/health | GET | **PASS** — `ok({status,version,platform,camera_driver})`，字段与契约一致（main.py:325） |
| /api/devices | GET | **PASS** — mock-1 常驻；ic4 设备追加 `ic4-<serial>`；`connected` 实时计算（camera.py:239-248） |
| /api/device/open | POST | **PASS** — body `{id}`；打开前自动关旧设备（camera.py:265-278）；未知 id 404；返回 `{id,name,serial,connected:true}` |
| /api/device/close | POST | **PASS** — 无 body，返回 `{connected:false}` |
| /api/device/info | GET | **PASS** — 未打开时 `connected:false, properties:[]` |
| /api/properties | GET | **PASS** — 数值属性带 min/max/step/unit、bool 无 min/max、enum 带 options、温度只读；未打开 409 |
| /api/properties/{name} | PUT | **PASS** — body `{value}`；越界钳制返回 `actual`；未知/只读 400；未打开 409 |
| /api/live/start | POST | **PASS** — body `{fps,scale,quality}`；fps 1–30、scale∈{1.0,0.5,0.25}、quality int 校验（实测 31/0.3/80.5 → 400）；返回 `{streaming,fps,scale,quality}` |
| /api/live/stop | POST | **PASS** — 无 body，返回 `{streaming:false}`；线程置 stop 并 join |
| WS /ws/live | WS | **PASS** — 每客户端先 `send_text(统计)` 后 `send_bytes(JPEG)`（main.py:94-101）；统计含 min/max/mean/std/saturated_pct/fps/histogram[256]；JPEG 为 8-bit 灰阶、1%–99% 分位自动对比度、质量=quality、尺寸=ROI×scale |
| /api/snapshot | POST | **PASS** — `format` ∈ fits/tiff/png；未打开 409；FITS/TIFF 16-bit、PNG 8-bit 自动对比度；URL 形如 `/api/files/snapshots/SNP_*.fits` |
| /api/capture/plan | POST | **PASS** — `{session_name, phases[]}`；count 1–10000（Pydantic 400）、type 校验 400；null 曝光解析为当前值并回显；拍摄中 re-plan 409 |
| /api/capture/start | POST | **PASS** — 需已 plan 且设备已打开，否则 409；返回 `{status:"running"}` |
| /api/capture/state | GET | **PASS** — `{status,session_name,current_phase,frames_done,total_frames,prompt,message,log}`；未 plan 为 idle；log 每条 `{t,msg}` |
| /api/capture/resume | POST | **PASS** — 仅 waiting_user 有效，返回 `{status:"capturing"}`；其他状态 409（实测 aborted 后 409） |
| /api/capture/abort | POST | **PASS** — 仅 active 有效，返回 `{status:"aborted"}`；已拍帧保留（实测） |
| WS /ws/events | WS | **PASS** — capture 事件字段齐全（main.py:81-93、capture.py:81-93）；job 事件 `{id,type,status,progress,message}`（process.py:134-143）；另在连接时推送当前 capture 快照（超出契约的合理增强，见 I-6） |
| /api/sessions | GET | **PASS** — 新→旧排序，`{id,name,created,frame_counts,has_results}` |
| /api/sessions/{id} | GET | **PASS** — `{phases,frames{file,url,thumb,mean,std,saturated_pct},results{file,url,preview},qc}` |
| /api/files/{session_id}/{rel_path} | GET | **PASS** — 白名单路径 + resolve 校验（见 §3）；snapshots 以 `session_id=snapshots` 表达 |
| /api/process/calibrate | POST | **PASS** — `{session_id,sigma,method}`；job 返回 `{job_id,type,status:pending}`；master bias/dark/flat 与 calibrated 产物齐全 |
| /api/process/stack | POST | **PASS** — `{session_id,sigma,method,registration}`；FFT 相位相关整数像素配准；qc_report 字段齐全 |
| /api/process/stretch | POST | **PASS** — `{session_id,result_file,stretch,black,white}`；linear/asinh/log；输出覆盖 `STACK_preview.png`（8-bit） |
| /api/process/jobs/{job_id} | GET | **PASS** — pending/running 含 progress/message；done 含 result；未知 job 404 |

---

## 3. 文件路径防护与 static mount

### 3.1 路径防护 —— **PASS**

`main.py:491-524` 实现双保险：
1. `session_id` 拒绝空、`..`、`/`、`\`；
2. `rel_path` 拒绝空、前导 `/`、`..`、`\`，并按白名单拆分（`meta.json` / `<group>/<file>` / `<group>/thumbs/<file>` / `results/<file>` / snapshots 单段）；
3. `path.resolve()` 必须位于 session 目录 resolve 之内。

实测（§1.2）6 组穿越/编码变体全部 4xx，合法 URL 不受影响。

### 3.2 static mount 与 SPA fallback —— **PASS**

- `main.py:626-628`：`DIST_DIR` 存在时 `app.mount("/", StaticFiles(html=True))`；显式路由（/api、/ws、/）先注册，不受 mount 影响。
- `main.py:615-623`：非 `/api`、`/ws` 的 404 → index.html（SPA fallback）；`/api`、`/ws` 保持 `{ok:false,...}` 错误包裹。
- 实测：`GET /`、`GET /live` 返回 text/html；`GET /api/nonexistent` 返回 JSON 404 包裹。

---

## 4. 代码质量

### 4.1 线程安全（抓帧/推流/保存） —— **PASS（含 P3 改进点）**

- 相机属性/open 状态由 `RLock` 保护（camera.py:100、138-146、148-213）；`CameraService` 设备切换由 `RLock` 保护（camera.py:224-312）。
- Live 抓帧与推流分离：单生产者队列 `maxsize=1`（丢弃旧帧）+ 双 daemon 线程，推流线程只消费（main.py:104-203）；推流跨线程通过 `asyncio.run_coroutine_threadsafe` 回事件循环（main.py:87-92）。
- Capture 状态读写由 `RLock` 保护，`get_state` 深拷贝对外隔离（capture.py:66-73）。
- ProcessService 任务表由 `RLock` 保护（process.py:93-132）。
- 改进点见 I-2（MockCamera 个别计数/漂移/缓存字段在锁外更新、LiveService start/stop 无锁）。

### 4.2 状态机完整性 —— **PASS（light 强制点）/ ISSUE（非 light 相位，见 I-1）**

- idle → running → waiting_user（每个相位开始前）→ capturing → finished | aborted | error 均实现；LIGHT 前 `_wait_resume()` 强制等待人工确认（capture.py:209-213），实测复现 ✓。
- abort 在 waiting_user 与拍摄中两条路径均实测收敛到 aborted 且保留已拍帧 ✓；连续 5 帧失败 → error ✓（代码路径）。
- 发现并实测复现：**非 LIGHT 相位自动确认窗口后未切换到 capturing**，拍摄期间 status 保持 waiting_user（I-1）。

### 4.3 资源释放（stop/close） —— **PASS（含 P3 改进点）**

- `LiveService.stop` 置 stop 事件并 join 抓帧/推流线程（2s 超时后置空，daemon 兜底）（main.py:141-148）。
- lifespan 关闭时 `live_service.stop()` + `camera_service.close()`（main.py:217-225）。
- 实测：live/start→stop 多次、最终 `device/close` 后状态仍可查询，进程正常退出。
- 改进点见 I-3（capture 线程停机未显式 abort/join）。

### 4.4 FITS 头与 12-bit 数据 —— **PASS（序列帧）**

- `capture.py:101-119`：DATE-OBS/EXPOSURE/GAIN/BLACKLEV/TEMPERATURE/FRAMETYP/CAMERA/SERIAL/SESSION/NFRAME + MEAN/STD/SATPCT；BITPIX 显式 16。
- Mock 帧全链路 `np.clip(0, MAX_ADU=4095)` 后转 uint16；smoke 实测 FITS `max ≤ 4095` ✓。
- 快照 FITS 头不完整 → I-4；stack 结果 uint16 精度 → I-5。

### 4.5 饱和 / QC 计算 —— **PASS**

- `saturated_pct = count(arr>=4095)/total*100`（util.py:57-67），口径与契约一致。
- 直方图为 8-bit 预览图 256 bin（util.py:70-73），与契约“8-bit 预览图 256 bin”一致。
- `effective_bits = log2(√N)`、`total_exposure_us = Σ light EXPOSURE`、`rejected_pct`/`background_std`/`elapsed_s` 齐全（process.py:66-83）；smoke 实测 `frames=3, total_exposure_us=9000, effective_bits=0.792` 正确。
- `auto_contrast_8bit` 1%–99% 分位线性映射（util.py:45-54），与契约一致。

---

## 5. 问题清单（建议修复人：backend-dev）

| 编号 | 严重度 | 位置 | 问题 | 复现步骤 | 建议 |
| --- | --- | --- | --- | --- | --- |
| I-1 | **P2（状态机语义错误）** | capture.py:214-220 | 非 LIGHT 相位 0.2s 自动确认窗口结束后，未把 status 从 `waiting_user` 切到 `capturing` 就调用 `_capture_phase`，整个 bias/dark/flat 拍摄期间状态保持 `waiting_user`、prompt 不清空。 | plan bias×2000 → start → `GET /api/capture/state` 轮询：frames_done 已 ≥5 时 status 仍为 `waiting_user`（本评审实测复现）。影响：前端 Modal 在非 light 拍摄全程常驻并可点“继续”；`/api/capture/resume` 在拍摄中也会被接受（仅改状态、对执行无实际控制）。 | 非 light 自动窗口结束后 `_set(status="capturing", prompt=None)` 再拍摄；或与船长确认契约是否要求非 light 也进入 waiting_user，若不需要则直接 capturing（需契约澄清）。 |
| I-2 | P3 | camera.py:295-304、330-332、347；main.py:118-148；camera.py:306-312 | 少量字段在锁外读写：`_uniform` 缓存、`_drift_x/_drift_y/_frame_count`（live 抓帧线程与 capture 线程可并发 grab_frame）；`LiveService.start/stop` 无锁（并发 start 可能双线程）；`device_info` 直接读 `_device`。 | 静态代码审查（CPython 下概率性竞态，难以稳定复现）。 | 统一在 `_lock` 内更新计数/漂移/缓存；LiveService 加 start/stop 互斥；device_info 走锁。 |
| I-3 | P3 | main.py:217-225；capture.py:177-238 | capture 线程为 daemon 且 lifespan 未 abort/join；停机时 mid-capture 的 meta.json 停留 `running`，重启后内存状态 idle 与磁盘状态不一致。 | 启动 capture（如 2000 帧）后直接停 uvicorn，检查 `data/captures/<id>/meta.json` 仍为 `running`。 | lifespan 中 `capture_service.abort()` 并等待线程结束；或启动时把遗留 `running` meta 标记为 aborted。 |
| I-4 | P3 | capture.py:141-160 | 快照 FITS 只写 DATE-OBS/FRAMETYP，缺少契约 §3.2“每帧必填”的 EXPOSURE/GAIN/BLACKLEV/TEMPERATURE/CAMERA/SERIAL/SESSION/NFRAME。 | `POST /api/snapshot {"format":"fits"}` 后读取头部。 | 复用 `_save_fits` 的头写入逻辑补齐快照头。 |
| I-5 | P3 | process.py:89-94 | stack 结果以 `uint16` 落盘（`np.clip(stacked,0,65535)`），契约公式为 float32 sigma-clipped mean，均值小数被截断。 | 读取 `results/STACK_sigma3.fits`，dtype 为 uint16。 | 结果 FITS 写 float32（或船长确认 16-bit 产物口径后更新契约）。 |
| I-6 | P3（文档偏差） | main.py:507-524、583-590 | 实现允许 `masters/`、`calibrated/` 两种 rel_path 分组（job 结果返回 masters URL 依赖此行为），api.md §1 未列出；`/ws/events` 连接即推 capture 快照也未写入 api.md。 | `GET /api/files/<id>/masters/MASTER_BIAS.fits` 可 200（契约未声明）。 | 更新 docs/api.md（需船长审批）或收紧实现。 |
| I-7 | P3 | main.py:548-569；process.py:165-172 | process 三个端点不检查 capture 是否 active，拍摄进行中可对半成品 session 提交 calibrate/stack。 | 拍摄中 `POST /api/process/calibrate` 会成功创建 job 并读到部分帧。 | capture active 时返回 409，或前端禁用处理入口（契约需明确）。 |
| I-8 | P3 | main.py:299-301；capture.py:96-123 | `phases: []` 可被接受，start 后立即 finished 并创建空 session；契约未定义空计划行为。 | `POST /api/capture/plan` body `{"session_name":"X","phases":[]}` → 200。 | 空 phases 返回 400，或在 api.md 明确允许。 |
| I-9 | P3 | capture.py:105 | `DATE-OBS` 为 `YYYY-MM-DDTHH:MM:SS.mmm`（毫秒），契约示例为 `HH:MM:SS` 不带小数。ISO 8601 允许小数，影响小。 | 读取任一帧 FITS 头（实测 `2026-08-22T04:07:47.261`）。 | 与船长确认口径后统一。 |
| I-10 | P3 | main.py:249-252 | 通用异常处理器把 `str(exc)` 直接返回给客户端，存在内部细节泄漏（局域网工具，风险低）。 | 制造未捕获异常即可见堆栈文本。 | 生产环境返回通用文案，详情记日志。 |

---

## 6. 评审方式与限制

- 只读检查 `backend/app/**`、`backend/requirements.txt`、`backend/README.md`、`scripts/backend_smoke.py`，对照 `docs/api.md`；实际运行使用现有 `backend/.venv`（Python 3.12）。
- 执行了必测 smoke 与人工补测两套脚本（脚本在 /tmp，不落库）；测试产生的运行数据位于已 gitignore 的 `data/`。
- **未修改 backend/ 任何源码文件**；本报告为唯一新增文件。
