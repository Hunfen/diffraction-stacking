# 衍射采集与叠加 — 前端

Electron diffraction LEED/RHEED 采集与处理系统的 Web 前端。
严格按 `docs/api.md` 契约实现。

## 技术栈

- Vite + React 18 + TypeScript
- Ant Design 5（UI）
- react-router（SPA 路由）
- axios（统一 `{ok,data,error}` 解包）
- recharts（256-bin 实时直方图）

## 开发运行

要求：Node ≥ 20（已在 Node 26 / npm 11 验证）。

```bash
cd frontend
npm install
npm run dev
```

默认打开 `http://localhost:5173`。

### 后端地址

- 后端默认 `http://localhost:8000`。
- dev 模式：REST 请求（`/api`、`/api/files`）由 vite 代理转发到后端，
  `/ws`（`ws: true`）同样代理。代理目标可在启动时通过环境变量覆盖：

  ```bash
  VITE_BACKEND=http://192.168.1.50:8000 npm run dev
  ```

- 也可以在 **设置页** 修改后端地址（`localStorage` 持久化）。
  WS 实时流与图片/文件 URL **直接使用该地址**，修改后立即生效；
  REST 请求在 dev 下仍走 vite 代理（代理目标由启动时 `VITE_BACKEND` 决定）。
- 生产模式：FastAPI 托管 `frontend/dist/`，同源访问，SPA 子路由 fallback 到 `index.html`。

## 构建验收

```bash
cd frontend
npm run build
```

`tsc -b && vite build` 零 error 通过。antd/recharts/react 已通过 Rolldown `output.codeSplitting` 拆分为独立 vendor chunk（antd 超限时按 500 kB `maxSize` 自动切分），无 chunk 超限 warning。

## 页面

| 路由 | 功能 |
| --- | --- |
| `/connect` | 设备列表（mock / ic4 标识、连接状态），打开/关闭设备；已连接时显示型号/序列号/属性摘要 |
| `/live` | canvas 渲染 `/ws/live` 二进制 JPEG（bytes 与文本区分）+ 十字准星；min/max/mean/std/饱和%/FPS 统计、256-bin 直方图；参数面板（exposure 滑块+数字、gain、black_level、reverse_x/reverse_y/trigger_mode、ROI 四值，改动立即 PUT 并显示 actual）；snapshot（fits/tiff/png）；WS 自动重连（指数退避 1s→30s）；未开设备时自动请求静默，不弹 409 toast |
| `/capture` | Session 名称 + 相位表格（类型/张数/曝光覆盖/说明/删除/上移/下移）；「默认衍射套装」模板 bias×50, dark×30, flat×30, light×100；执行状态条（相位、已完成/总数、当前帧）；`waiting_user && needs_user===true` 弹阻塞 Modal（中文文案 + 继续按钮调 `/api/capture/resume`；LIGHT 文案注明“请对照实时直方图人工确认参数”）；`needs_user===false` 只显示非阻塞“正在准备拍摄…”Banner（无继续按钮）；capturing 期间仅丢弃迟到的 `needs_user===false` 陈旧消息，`needs_user===true` 永不丢弃；最新 20 帧缩略图墙；中止保留已拍帧；结束后引导去 processing |
| `/processing` | Session 下拉（`GET /api/sessions`）+ 帧数摘要 + 坏帧勾选（勾=纳入，默认全选；未勾选文件名进入对应组 `exclude` 数组随 calibrate/stack 提交）；步骤 1 calibrate → 步骤 2 stack（含 registration 开关）→ 步骤 3 结果列表（fits + preview png）与 stretch（linear/asinh/log + black/white 滑块）刷新预览；`/ws/events` 订阅 capture 与 job 进度；下载链接 |
| `/settings` | 后端地址（默认 `http://localhost:8000`，localStorage 持久化；恢复默认只清除本地覆盖，不覆盖 `VITE_BACKEND` env）、预览 fps/quality/scale（整数/允许步长，与后端钳制一致）、后端健康状态 |

## 代码结构

```
src/
  api/
    types.ts               # 契约类型定义
    backend.ts             # 后端地址 / URL / WS 地址解析
    client.ts              # axios 统一封装（{ok,data,error} 解包 + 错误弹窗）
    captureStateMachine.ts # capture 事件过滤与 waiting_user UI 语义（可单测）
    settings.ts            # 本地预览参数持久化
    ws.ts                  # 自动重连 WebSocket hook
  pages/
    ConnectPage.tsx
    LivePage.tsx
    CapturePage.tsx
    ProcessingPage.tsx
    SettingsPage.tsx
  App.tsx         # 顶部导航 + Layout + 路由
```
