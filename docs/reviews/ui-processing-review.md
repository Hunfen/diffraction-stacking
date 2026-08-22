# Processing 页 6 项 UI 修改 —— 独立验收报告（t2）

> 任务：t2 — ui-processing-review（6 项 UI 修改验收）
> 评审人：reviewer（t2）· 评审日期：2026-08-22
> 评审对象：`frontend/src/pages/ProcessingPage.tsx`（未提交的工作区改动，HEAD=98cae16）
> 方法：只读审查（git diff + 源码行号核对）→ `npm run build && npm run lint` → 启动 FastAPI(uvicorn:8000，托管生产 dist) → headless Chrome `channel:'chrome'` 页面核对 + 页面内 Checkbox 交互（未点击任何运行/提交按钮，未改源码，未 commit）
> 复现脚本/证据：`/tmp/ui-processing-review-verify.js`、输出 `/tmp/ui-processing-review-verify.log`、全页截图 `/tmp/ui-processing-review.png`

## 结论摘要

| # | 验收项 | 结论 |
| --- | --- | --- |
| 0 | `cd frontend && npm run build && npm run lint` 通过 | **PASS** |
| 1 | 两处「请求体含 exclude」→「纳入：Bias X / Dark X / Flat X / Light X（共 N 帧）」，计数 = 场帧数 − excluded 数 | **PASS** |
| 2 | 每个场标题右上角「全选」Checkbox；状态 = 该场排除数为 0；切换正确增删 excluded；手动取消单帧后全选自动取消 | **PASS** |
| 3 | 无 Cache-Control / no-store / ?v= 等实现细节出现在用户可见文案 | **PASS** |
| 4 | Step 1 · Construct Bias/Dark/Flat Master & Calibrate | **PASS** |
| 5 | Step 2 · Stacking Light Field | **PASS** |
| 6 | Step 3 · Rescale & Preview + 按钮/说明同步“重标定/Rescale”（后端 stretch 字段不变） | **PASS** |

**总体结论：7/7 PASS，无 FAIL。**

---

## 0. 构建与 lint —— PASS

命令：`cd frontend && npm run build && npm run lint`（exit 0）

```text
> tsc -b && vite build
✓ 3617 modules transformed.
dist/assets/index-CSfFZUyG.js   1,630.48 kB │ gzip: 494.96 kB
✓ built in 456ms

> oxlint
Found 0 warnings and 0 errors.
Finished in 36ms on 14 files with 116 rules using 8 threads.
```

---

## 1. 纳入计数文案 —— PASS

源码证据（`ProcessingPage.tsx`）：

- 旧文案 `请求体含 exclude` 在源文件出现 **0** 次（grep 全文件），构建产物 dist JS 中亦为 0 次。
- 新文案出现 **2** 处，分别位于 Calibrate 卡（L591-593）与 Stack 卡（L630-632）：

  ```tsx
  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
    纳入：{inclusionSummary}（共 {includedCountTotal} 帧）
  </Typography.Text>
  ```

- 计数逻辑 L301-319：`includedByPhase[t] = frames.filter(f => !excluded.has(frameKey(t, f.file))).length`，即**场帧数 − 该场排除数**；`inclusionSummary` 按 Bias/Dark/Flat/Light 顺序拼接，`includedCountTotal` 为四场之和。

浏览器实测（session `20260822T17-25-23_session`，bias 100 / dark 100 / flat 0 / light 100）：

```text
纳入：Bias 100 / Dark 100 / Flat 0 / Light 100（共 300 帧）  ← 两处一致
手动取消 Bias 1 帧后 → Bias 99 …（共 299 帧）              ← 两处同时联动
取消整场 Bias 后   → Bias 0  …（共 200 帧）              ← 与排除数 100 吻合
```

---

## 2. 场级「全选」Checkbox 与联动 —— PASS

源码证据：

- L503-510：每个相位卡 `Card` 的 `extra`（右上角）渲染 `Checkbox`，label `全选`；`checked={excludedCount === 0}`（L496 `excludedCount = frames.filter(f => excluded.has(...)).length`），即**状态 = 该场排除数为 0**。
- L270-282：`togglePhaseAll(type, checked)` — 勾选时 `next.delete(key)`（移除 excluded），取消时 `next.add(key)`（整场进入 excluded），语义正确。
- L521-530：单帧 Checkbox `checked={!excluded.has(frameKey(t, f.file))}` → `toggleInclude` 增删 `excluded`；单帧取消后 `excludedCount` 由 0 变 1，场级「全选」因 `excludedCount === 0` 自动变未勾选。

浏览器实测（headless Chrome，页面内真实点击 Checkbox input，只读、无任何 POST）：

| 步骤 | 场全选状态 | 已勾帧 | 排除行 | 纳入摘要 |
| --- | --- | --- | --- | --- |
| 初始 | ✅ 勾选 | 100/100 | 未勾选（将排除）0 | 共 300 帧 |
| 手动取消 Bias 第 1 帧 | ❌ 自动取消 | 99/100 | 未勾选（将排除）1 | Bias 99 … 共 299 帧 |
| 点击 Bias「全选」（未勾→勾） | ✅ 勾选 | 100/100 | 未勾选（将排除）0 | 共 300 帧 |
| 再点「全选」（勾→未勾） | ❌ 未勾选 | 0/100 | 未勾选（将排除）100 | Bias 0 … 共 200 帧 |
| 再点「全选」恢复 | ✅ 勾选 | 100/100 | 未勾选（将排除）0 | 共 300 帧 |

Dark / Light 两场同样渲染「全选」且初始为勾选态（该 session 无 Flat 相位，故页面渲染 3 个场卡，符合数据）。页面控制台 0 error、0 失败请求。

> 遗留观察（不计入本次验收 FAIL）：`excluded` state 未在切换 Session 时清空，`frameKey` 为 `type|file` 不含 session id。若两个 session 存在同名帧文件，前一个 session 的排除选择会带到后一个 session 的显示状态上（提交仍会带进 exclude）。本次验收不涉及该行为，建议后续单独处理。

---

## 3. 无实现细节文案 —— PASS

- 源文件 grep：`请求体含 exclude`=0、`Cache-Control`=0、`no-store`=0；`?v=` 仅出现在 **代码注释**（L213/370/389）与 **img src 的 URL 构造**（L437 `${resolveUrl(...)}?v=${previewVersion}`），非用户可见文案。
- 浏览器 `document.body.innerText` 检查：`?v=`、`Cache-Control`、`no-store`、`后端返回 Cache-Control: no-store`、`请求体含 exclude` 均 **不出现**（可见文本中 `\bStretch\b` 与「拉伸」也均为 0）。
- 原先的用户可见说明「后端返回 Cache-Control: no-store，前端通过 ?v= 版本参数强制刷新」已替换为 L677：「对叠加结果重新标定显示亮度范围，生成可查看的预览图（8-bit PNG）。」

---

## 4/5/6. 步骤标题与 Rescale 文案 —— PASS

源码证据（每项在 Steps 标题 + 卡片标题出现，hint 同步）：

| 要求 | 出现位置（行号） |
| --- | --- |
| Step 1 · Construct Bias/Dark/Flat Master & Calibrate | Steps L557、卡片 L565、stackHint L425（中文“步骤 1 · …”前缀） |
| Step 2 · Stacking Light Field | Steps L558、卡片 L598、stretchHint L430（中文“步骤 2 · …”前缀） |
| Step 3 · Rescale & Preview | Steps L559、卡片 L637 |

Rescale 同步项：

- 管线标题 L552：`处理管线（Calibrate → Stack → Rescale）`；Steps 描述「重标定并刷新预览」。
- Step 3 卡片：`Rescale：`（L647）、按钮 `应用重标定并刷新预览`（L671）、hint `请先完成步骤 2 · Stacking Light Field，再运行 Rescale。` / `Rescale 任务运行中…` / `…可以应用 Rescale 并刷新预览。`（L429-433）。
- 预览区 L784：`当前预览（…，Rescale 提交 / 完成后自动刷新）`；img alt `重标定预览`（L794）。
- 任务表 JOB_TYPE_LABELS L61：`stretch: '重标定预览'`；runStretch 警告语 L367 `请选择要重标定的结果文件`。
- **后端 stretch 字段不变**：前端请求体 L375-381 仍为 `stretch: stretchMethod`（不是 rescale）；`git diff --stat -- backend` 为空（backend 无改动）；运行中后端 `/openapi.json` 的 `StretchBody` 仍要求 `{session_id, result_file, stretch, black, white}`，`POST /api/process/stretch` 路径未变。

浏览器实测：6 项文案在页面真实渲染（body 文本命中），详见 `/tmp/ui-processing-review-verify.log` 与全页截图。

---

## 5. PASS/FAIL 汇总

| 级别 | 条目 | 说明 |
| --- | --- | --- |
| PASS | 构建 + lint | exit 0，0 warning / 0 error |
| PASS | 纳入计数文案（两处） | 源文件 2 处新文案 + 计数逻辑正确 + 浏览器联动实测 |
| PASS | 场级全选 Checkbox | 3 个场卡渲染、状态/增删/单帧联动浏览器实测 |
| PASS | 无实现细节文案 | 源文件与渲染后文本均无 Cache-Control / no-store / ?v= |
| PASS | Step 1/2/3 标题 | Steps + 卡片 + hint 同步 |
| PASS | Rescale 文案 + 后端字段不变 | 按钮/说明/预览/任务表同步；请求体与后端仍为 `stretch` |
| OBSERVATION | excluded 跨 session 未重置 | 不影响本任务验收，建议后续跟进 |

未执行：任何 calibrate/stack/stretch 提交；未修改源码；未 commit（本报告文件为新增 untracked，按要求不提交）。
