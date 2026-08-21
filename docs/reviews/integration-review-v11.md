# v1.1 修复后最终集成验收 —— 阻塞报告（未执行）

- 任务：t10 — integration-v11
- 评审人：reviewer
- 评审日期：2026-08-22
- 状态：**BLOCKED（前置条件不满足，未执行集成用例）**

## 1. 前置检查结果：FAIL

按任务要求，先读 `docs/reviews/regression-review.md` 确认 0 FAIL。

**结果：该报告仍含 1 个 FAIL/P1（R-1），且源码未发生任何修复变更。**

证据：
1. `docs/reviews/regression-review.md` §3.2 明确标记：
   - FAIL（P1）：`frontend/src/pages/CapturePage.tsx:95-100` 的 `applyCaptureState` 会丢弃「capturing 期间到达的 `waiting_user(needs_user=true)`」合法事件。
   - 后果：标准 4 相位流程中 LIGHT 确认 Modal 不弹出、拍摄在 LIGHT 处无限等待。
2. 现场复核源码（只读）：
   ```ts
   // frontend/src/pages/CapturePage.tsx:95-100（未变）
   const applyCaptureState = useCallback((next: CaptureState) => {
     if (next.status === 'waiting_user' && captureStateRef.current.status === 'capturing') {
       return;
     }
     setCaptureState(next);
   }, []);
   ```
   该 guard 仍会无条件丢弃 `capturing → waiting_user(needs_user=true)` 的合法 LIGHT 事件。
3. 文件时间戳：`frontend/src/pages/CapturePage.tsx`、`backend/app/main.py` 均未晚于 regression-review.md，确认无新修复。

## 2. 阻塞项与解除条件

| 阻塞项 | 建议修复人 | 修复要求 | 解除后动作 |
| --- | --- | --- | --- |
| R-1：CapturePage 丢弃合法 LIGHT `waiting_user(needs_user=true)` 事件 | frontend-dev | 只丢弃 `needs_user===false` 的陈旧 waiting_user（或直接删除该 guard，v1.1 后端已不会再发陈旧 waiting_user） | 修复后重新执行 t10 全部用例（本报告 §4）并重跑浏览器默认 4 相位验收 |

## 3. 本次已执行/未执行

- 已执行：只读复核 regression-review.md 与相关源码，确认 FAIL 仍存在。
- 未执行（按任务规则，有 FAIL 则停止）：
  - uvicorn:8000 集成全流程
  - t6 P1/P2 关闭复查
  - 路径穿越 / SPA fallback / 409 / 400 抽查
  - 性能对比测量

## 4. 解除阻塞后的验收清单（供重跑参考）

1. `uvicorn app.main:app --port 8000`；`GET /api/health`、`GET /openapi.json`。
2. mock 全流程：open → 属性 → live WS（≥3 对文本统计→JPEG）→ plan bias×2/dark×2/flat×2/light×2：
   - 非 LIGHT 相位自动 `capturing`（needs_user=false）；
   - LIGHT 阻塞 `waiting_user`（needs_user=true）→ resume → capturing；
   - finished 后 calibrate（带 exclude，核对 excluded_counts）→ stack（float32/BITPIX=-32）→ stretch → 全部产物下载。
3. 复查 t6 P1/P2：①非 LIGHT 拍摄期间 status=capturing；②前端 exclude 真实生效（请求体 + 后端计数）；③FITS TEMP 无 HIERARCH warning；④满 ROI mock ≥15fps 或 README 记录取舍。
4. 抽查：SPA fallback、路径穿越 4xx、capturing 中 resume 409、拍摄中处理任务 409、空 phases 400。
5. 输出 PASS/FAIL 清单 + 性能前后对比 + 遗留 ISSUE 分级。

---

# FINAL 节（t13 复验 + v1.1 完整集成验收，追加于阻塞报告之后）

- 评审人：reviewer（final-verify，t13）
- 评审日期：2026-08-22
- 前置确认：t11（R-1 修复）、t12（500 泄漏/快照头/exclude 去重）均 completed；本轮重新执行 smoke 与前端 build 均为全绿。

## 1. R-1 复验 —— PASS

- 代码检查：`frontend/src/api/captureStateMachine.ts` 的 `shouldAcceptCaptureState` **只丢弃** `current.status==='capturing'` 期间到达的 `needs_user===false` 陈旧 waiting_user；`needs_user===true`（及 undefined 兼容路径）一律接受。CapturePage.tsx:95-105 改为函数式 `setState((prev)=>(shouldAcceptCaptureState(prev,next)?next:prev))`，不再依赖 render 期 ref。Modal/Banner 由纯函数 `isBlockingWaiting/isPreparingWaiting` 推导。
- 真实后端 WS 事件序列（bias×2 → light×1，本机采集 6 条）：`running → waiting_user(bias,false) → capturing×3 → waiting_user(light,true)`。
- 用 **实际打包前源码模块**（Node 26 直接 import `captureStateMachine.ts`）对上述序列执行：6/6 事件被接受，末条 light waiting_user 后 `isBlockingWaiting === true`（Modal 打开）、`isPreparingWaiting === false`；合成陈旧事件（capturing 中收到 needs_user=false 的 waiting_user）被正确拒绝。**PASS。**

## 2. 后端复验 —— PASS

- `cd backend && .venv/bin/python ../scripts/backend_smoke.py`：**114/114 通过，exit 0**（含新增 SESSION/NFRAME 断言）。
- 500 两处旁路（运行级 monkeypatch 触发，不改源码）：
  - 未分类相机错误 → `raise_cam_err` 记日志后抛 `HTTPException(500,"服务器内部错误")`，响应不含 `SECRET_*` 细节 —— PASS；
  - snapshot 保存抛错 → 记日志后同样统一 `服务器内部错误` —— PASS。
- 快照 FITS 头：实测 `SESSION='SNAPSHOT'`、`NFRAME=1`（另有 EXPOSURE/GAIN/BLACKLEV/TEMP/FRAMETYP/CAMERA/SERIAL）—— PASS。
- FITS 温度关键字：api.md v1.1 §3.2 已改为标准短名 `TEMP`；读取本轮拍摄帧 header keys 为 `.../TEMP/...`，astropy 读取 **0 条 VerifyWarning**（旧 HIERARCH warning 关闭）—— PASS。

## 3. 完整集成验收（uvicorn:8000）—— **62/62 PASS**

脚本 `/tmp/t13_integration.py`（自写独立用例，不复用 smoke 进程）：
- `GET /api/health`（mock）、`GET /openapi.json`（paths=23）—— PASS。
- 静态托管：`GET /`、`/live`、`/capture` 全部 200 text/html；`/api/not-exist` 404 保持 `{ok:false,data:null,error}` —— PASS。
- mock 全流程：open → exposure/gain/black_level → live WS 6 对（全部 文本统计(256-bin)→JPEG(0xFFD8)，~391KB/帧）→ plan bias×2/dark×2/flat×2/light×2：
  - bias 前 `waiting_user(needs_user=false)`，resume 409，随后自动 `capturing(needs_user=false, prompt=null)` —— PASS；
  - 拍摄中提交 calibrate 对已存在 session → 409 —— PASS；
  - LIGHT `waiting_user(needs_user=true)`、prompt 含直方图、等待 0.3s 仍阻塞 → resume 200 → capturing → 再次 resume 409 → finished（frames_done=8，frame_counts 各 2）—— PASS；
- 空 phases plan → 400 —— PASS。
- calibrate 带 exclude（light 排除 LIGHT_0002）：`excluded_counts.light=1`、`calibrated_count=1` —— PASS。
- stack 带 exclude：`qc.frames=1、total_exposure_us=2000.0、excluded_counts.light=1`；下载 FITS `BITPIX=-32` 且 dtype=float32 —— PASS。
- stretch → done；`STACK_preview.png` 下载成功；8 FITS + 8 缩略图 + meta.json 全部可下载 —— PASS。
- 路径穿越回归：`xxx/../../etc/passwd`、`%2e%2e/%2e%2e/etc/passwd`、`snapshots/../../etc/passwd` → 全部 4xx —— PASS。

## 4. t6 P1/P2 关闭复查

| t6/t9 遗留 | 状态 | 证据 |
| --- | --- | --- |
| ①非 LIGHT 拍摄期间 status=capturing | **已关闭** | 集成用例中 bias 自动 capturing 后连续采样；t9 已有 40/40 采样 |
| ②前端 exclude 真实生效 | **已关闭** | 前端 `buildExclude()` 随 calibrate/stack 提交（源码复查）+ 本轮后端 `excluded_counts`/`qc.frames` 结果与排除一致 |
| ③FITS TEMP 无 HIERARCH warning | **已关闭** | header 用短名 TEMP；本轮 uvicorn 日志与 astropy 读取均 0 warning |
| ④满 ROI mock ≥15fps 或 README 记录取舍 | **基本关闭（1 个 P2 文档口径）** | 实测满 ROI 1920×1200：diffraction 场景 **24.4 fps**（≥15 ✓）；README 已记录“17–24 fps（暗场）”与优化口径。但暗场帧率与曝光强相关：默认 exposure=20000µs 时实测 **6.1 fps**（exposure=30µs 时 17.6 fps），README 未注明该曝光条件，见遗留 P2-F2 |

## 5. 性能对比（修复前 t6 vs 本轮 t13）

| 指标 | t6（修复前） | t13（本轮） | 变化 |
| --- | --- | --- | --- |
| live 首帧延迟（POST /api/live/start → 首条统计） | 124.1 ms | **89.9 ms** | -28% |
| 流帧间隔（请求 30fps，默认 ROI 1024×768） | mean 37.7 ms（~26.5 fps） | **mean 30.2 ms（~33.1 fps）** | 达到标称 30fps |
| 满 ROI 1920×1200 帧生成（diffraction） | ~98.4 ms（~10 fps） | **~41.0 ms（~24.4 fps）** | mock 优化生效 |

## 6. 遗留 ISSUE 分级（最终）

| 级别 | 编号 | 问题 | 建议 |
| --- | --- | --- | --- |
| P0 阻断 | — | 无 | — |
| P1 重要 | — | 无 | — |
| P2 建议 | F1 | backend/README.md 的 mock 性能表述未注明曝光条件：满 ROI 暗场“17–24 fps”仅在低曝光（如 30µs）成立，默认曝光 20000µs 实测 ~6.1 fps（默认 ROI 暗场 ~17.6 fps，仍满足 api.md“≥10 fps”，但不满足 README 的“满 ROI ≥15”表述） | backend-dev：README 补一句曝光依赖（如“暗场在典型 bias 曝光 30µs 下 17+ fps；高曝光下泊松精确路径变慢”），或对 λ≥0.5 的暗场继续优化 |
| P2 建议 | F2 | 前端状态机已用纯函数并通过真实序列验证，但本轮未跑真实浏览器点击流（无浏览器环境）；R-1 修复后建议在浏览器中人工点击一遍默认 4 相位模板作为最终签字动作 | 船长/frontend-dev 在真机上跑一次 UI 冒烟 |

## 7. 最终结论

- 前置 t11/t12 完成；R-1 修复有效（真实事件序列 + 实际 TS 模块验证）。
- 后端 smoke 114/114、前端 build 绿（tsc 严格 + vite 成功、oxlint 0 warning）、500 无泄漏、快照头完整。
- 完整集成验收 **62/62 PASS**，性能较 t6 全面改善。
- **P0=0、P1=0 → v1.1 验收通过**；遗留 2 个 P2 不阻塞。未修改源码、未 commit。
