# 前后端集成验收报告（uvicorn:8000 + frontend/dist 静态托管）

- 评审对象：全仓库集成验收（只读 + 运行；未修改任何源码、未 commit）
- 前置评审：`docs/reviews/backend-review.md`（t4）、`docs/reviews/frontend-review.md`（t5）
- 评审人：reviewer（integration-review，t6）
- 评审日期：2026-08-22
- 环境：macOS / Python 3.12（backend/.venv）/ Node 26 / 相机驱动 mock / 前端 dist 已由 t5 构建

## 0. 前置检查

- backend-review.md：**0 FAIL**（smoke 78/78、补测 53/53 全过；1 个 P2、9 个 P3）。
- frontend-review.md：**0 FAIL**（构建通过；8 项契约对照全 PASS；1 个 P2、7 个 P3）。
- 结论：**无阻塞项，按计划继续集成验收**。两份报告的遗留问题在本报告中重新分级汇总（§5）。

## 1. 结论摘要

- 集成测试脚本 **48/48 PASS**（uvicorn 127.0.0.1:8000 + mock 设备全流程）。
- 静态托管与 SPA fallback **PASS**：`GET /`、`/live`、`/capture` 均 200 text/html；JS 资产 200；`/api` 404 不被 fallback 吞掉。
- WS 实时流 **PASS**：连续 6 对消息全部为「文本统计 → 二进制 JPEG」；统计含 256-bin 直方图；JPEG 0xFFD8。
- 全流程 **PASS**：open → 属性调整 → live → plan(bias×2/dark×2/flat×2/light×2) → LIGHT waiting_user → resume → finished(8 帧) → calibrate → stack(registration=true) → stretch → 8 FITS + 8 缩略图 + 3 个结果文件全部下载成功。
- 遗留 ISSUE：**P0=0，P1=2，P2=10**（详见 §5）。

## 2. 启动与环境

```bash
cd backend && .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

启动日志正常（Startup complete，无 error）。后端为单进程共享服务实例；dist 存在时挂载 `frontend/dist/`。

### 2.1 基础接口（curl 实测）

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| `GET /api/health` | **PASS** | `{"ok":true,"data":{"status":"ok","version":"0.1.0","platform":"darwin","camera_driver":"mock"},"error":null}` |
| `GET /openapi.json` | **PASS** | title=Diffraction Stacking 后端, version=0.1.0, paths=23（REST 全量；WS 不在 OpenAPI 中属正常） |
| `GET /` | **PASS** | 200, `text/html; charset=utf-8`, 474B（前端 dist/index.html，含 `/assets/index-C-2JpAMZ.js` 引用） |
| `GET /live`（子路由直刷） | **PASS** | 200 text/html（SPA fallback 生效，不是 404） |
| `GET /capture`（子路由直刷） | **PASS** | 200 text/html |
| `GET /assets/index-C-2JpAMZ.js` | **PASS** | 200 text/javascript |
| `GET /api/not-exist` | **PASS** | 404 `{"ok":false,"data":null,"error":"Not Found"}`——API 404 保持统一包裹，未被 SPA fallback 吞掉 |

## 3. 集成测试（mock 全流程，48/48 PASS）

脚本：`/tmp/integration_test.py`（backend/.venv 执行；urllib + websockets；逐项 PASS/FAIL）。关键项：

### 3.1 设备与属性

- `POST /api/device/open {"id":"mock-1"}` → 200 connected=true。
- `PUT /api/properties/exposure 2000`、`gain 0.5`、`black_level 5` → actual 与请求一致。

### 3.2 WS /ws/live（≥3 帧，实际收 6 对）

- `POST /api/live/start {"fps":30,"scale":1.0,"quality":80}` → 200。
- 6 对消息**全部**先 `str`（JSON 统计：min/max/mean/std/saturated_pct/fps/histogram[256]）后 `bytes`（JPEG，`FF D8` 开头，>1KB）。
- 收帧后 `POST /api/live/stop` → streaming=false。

### 3.3 拍摄（bias×2/dark×2/flat×2/light×2）

- plan 回显 4 相位；start → running。
- LIGHT 前进入 `waiting_user`，`current_phase.type=light`，prompt 含“直方图”人工确认文案。
- `POST /api/capture/resume` → capturing；最终 finished，`frames_done=8`。
- sessions 列表 `frame_counts == {bias:2,dark:2,flat:2,light:2}`。

### 3.4 处理管线

- calibrate → done，`calibrated_count=2`，master bias/dark/flat 齐全。
- stack（registration=true）→ done，`STACK_sigma3.fits`；qc `frames=2, total_exposure_us=4000.0`（=2×2000µs，口径正确）。
- stretch（asinh, black=0, white=4095）→ done，返回 `url` 与 `preview_url`。

### 3.5 产物下载

- 8 个 FITS（`SIMPLE` 魔数）+ 8 个缩略图（PNG 魔数）→ **16/16 可下载**。
- stack FITS、stretch FITS、`STACK_preview.png`、`meta.json` 均可下载且内容合法。
- session detail：`results≥2`、`qc` 为 dict。

## 4. 性能观测（本机 mock，粗测）

| 项目 | 数值 | 说明 |
| --- | --- | --- |
| 流首帧延迟 | **124.1 ms** | 从 `POST /api/live/start` 发出到 WS 首条统计到达（含 HTTP 往返 + 首帧生成 + JPEG 编码） |
| 流帧间隔（scale=1.0, fps=30） | mean **37.7 ms**（min 28.3 / max 40.4），约 **26.5 fps** | 受 mock 帧生成 ~32ms + JPEG 编码限制，达不到标称 30fps 属预期 |
| Mock 抓帧耗时 1024×768（默认 ROI） | mean **32.1 ms**（min 31.2 / max 34.7），约 31 fps | warmup 后 50 帧 |
| Mock 抓帧耗时 1920×1200（满 ROI） | mean **98.4 ms**（min 93.1 / max 135.4），约 10 fps | 契约“帧生成时间应满足 ≥10 fps”在满 ROI 下为临界达标（10fps 上下），默认 ROI 远高于 30fps |
| 拍摄/处理耗时（2×4 计划） | 秒级完成 | 8 帧 capture + calibrate + stack + stretch 均 <60s（脚本内 poll 上限） |

结论：默认 ROI 实时预览流畅；满 ROI 只有 ~10fps，若要求满 ROI 30fps 需优化 mock 帧生成（真机路径由 SDK/带宽决定，不受此影响）。未发现与契约直接冲突（契约仅要求 ≥10 fps 与 mock 默认 30fps+）。

## 5. 遗留 ISSUE 分级（含前置评审汇总与本次新增）

| 级别 | 编号 | 来源 | 问题 | 处理建议（修复人） |
| --- | --- | --- | --- | --- |
| P0 阻断 | — | — | 无 | — |
| P1 重要 | B-I1 | backend-review I-1 | 非 LIGHT 相位自动确认窗口后不切 `capturing`，bias/dark/flat 拍摄全程 status 为 `waiting_user`（本集成再次通过状态轮询确认行为存在）；前端 Modal 会全程常驻、resume 可被误点 | 自动窗口结束即 `_set(status="capturing", prompt=None)`；非 light 是否保留 waiting_user 窗口请船长定夺契约（backend-dev） |
| P1 重要 | F-I1 | frontend-review I-1 | “排除坏帧”只是前端本地标记，calibrate/stack 请求未携带，用户会误以为坏帧不参与叠加（涉及科学数据口径） | 契约增加 exclude 字段（需船长）或隐藏/禁用该 UI，至少改文案明示“仅本地标记”（frontend-dev） |
| P2 建议 | B-I2 | backend-review I-2 | MockCamera `_drift/_frame_count/_uniform` 锁外读写；LiveService start/stop 无锁；device_info 无锁读 | RLock 覆盖；LiveService 互斥（backend-dev） |
| P2 建议 | B-I3 | backend-review I-3 | capture 线程 daemon、lifespan 不 abort；停机 mid-capture meta 停留 running，重启后状态不一致 | lifespan abort+join，或启动扫描修正遗留 running（backend-dev） |
| P2 建议 | B-I4 | backend-review I-4 | 快照 FITS 头缺契约 §3.2 必填关键字 | 复用 `_save_fits` 头逻辑补齐（backend-dev） |
| P2 建议 | B-I5 | backend-review I-5 | stack 结果 uint16 落盘，均值小数截断（契约公式为 float32） | 结果写 float32 或船长确认口径（backend-dev） |
| P2 建议 | B-I6 | backend-review I-6 | api.md 未列出 masters/calibrated 文件路径与 WS 连接快照（实现存在） | 更新契约文档（船长审批） |
| P2 建议 | B-I7~I10 | backend-review I-7~I10 | 拍摄中可提交处理任务；空 phases 可 plan；DATE-OBS 毫秒；500 返回内部异常文本 | 按原报告建议处理（backend-dev） |
| P2 建议 | F-I2 | frontend-review I-2 | 单 chunk 1.62MB（>500kB warning） | 路由级 lazy + vendor 拆分（frontend-dev） |
| P2 建议 | F-I3~I8 | frontend-review I-3~I8 | 13 条 oxlint warning；strict 未显式；localStorage 参数未按步长收敛；“恢复默认”覆盖 env 语义；job 刷新不可见；live 未开设备弹 409 toast | 按原报告建议处理（frontend-dev） |
| P2 建议 | NEW-1 | 本次实测（uvicorn 日志） | 写 FITS 时 `TEMPERATURE` 关键字 >8 字符，astropy 报 `VerifyWarning: Keyword name 'TEMPERATURE' ... a HIERARCH card will be created`。astropy 读取兼容（smoke 头校验通过），但第三方 FITS 工具可能不识别 | 契约口径确认：改为 `HIERARCH TEMPERATURE` 显式写 / 或改用 `TEMP`；文档写明兼容性（backend-dev + 船长） |
| P2 建议 | NEW-2 | 本次实测（§4） | 满 ROI 1920×1200 的 mock 帧生成 ~98ms（≈10fps），临界；若后续在满 ROI 上做 30fps 演示会不够 | 优化 mock 图案生成/缓存或明确满 ROI 帧率预期（backend-dev） |

## 6. 端到端清单

| 环节 | 结果 |
| --- | --- |
| uvicorn:8000 启动 + /api/health + /openapi.json | PASS |
| FastAPI 挂载 frontend/dist，GET / 返回前端页面 | PASS |
| SPA 子路由 /live、/capture 刷新不 404 | PASS |
| mock：open → 属性调整 → live（WS ≥3 帧，文本→JPEG 顺序） | PASS（6/6 对） |
| plan bias×2/dark×2/flat×2/light×2 → LIGHT waiting_user → resume → finished | PASS |
| calibrate → stack → stretch | PASS |
| 下载结果与缩略图（8 FITS + 8 PNG + 3 结果文件 + meta.json） | PASS |
| 关闭设备、服务正常退出 | PASS |

## 7. 评审方式与限制

- 只读源码 + 实际运行；测试脚本位于 /tmp，运行数据在已 gitignore 的 `data/`；未修改任何源码、未 commit。
- 本次未做真实浏览器自动化（前端交互层由 t5 静态审查 + 构建验证覆盖），集成验证覆盖前端产物静态服务与其消费的全部后端接口/WS 协议。
