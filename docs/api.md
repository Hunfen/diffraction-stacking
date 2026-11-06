# Diffraction Stacking — 前后端 API 契约 v1.4

> 本文件是后端（FastAPI）与前端（React）之间的**唯一契约**。
> 前后端必须严格按本文件实现；发现契约歧义时先报告船长，不得各自发挥。
> 配套设计见 [architecture.md](architecture.md)。

---

## 1. 通用约定

- 基础路径：`/api`（WebSocket 为 `/ws/live`、`/ws/events`）
- 所有 REST 响应为 JSON，统一包裹结构：

```json
{ "ok": true, "data": <any>, "error": null }
{ "ok": false, "data": null, "error": "<English error message>" }
```

- `ok=false` 时 HTTP 状态码按语义给出：400（参数错误）、404（不存在）、409（状态冲突）、
  500（内部错误）、503（依赖不可用，如平台无法加载 imagingcontrol4）
- **所有 `error` 与 `prompt` 文案均为英文**（数据类字段如 session/folder 名称不受此限制）
- 500 响应默认返回 `error:"Internal server error"`，不泄漏内部异常细节；**唯一例外**是拍摄 set/草稿文件损坏，
  此时 500 的 `error` 会带文件名以便定位（如 `capture_sets.json has an invalid sets object`，见 §2.6）
- 时间格式：ISO 8601（`YYYY-MM-DDTHH:MM:SS`，本地时区，不带 Z）；FITS `DATE-OBS` 精确到毫秒（`YYYY-MM-DDTHH:MM:SS.mmm`）
- 帧数据口径：相机 12-bit，内存 uint16，数值 ≤ 4095；FITS 16-bit 存储、数据在低 12 位
- 数据目录（v1.2）：所有运行时文件统一落在 `data_root` 下（`snapshots/`、`captures/`）。
  `data_root` 由 `GET/PUT /api/settings` 暴露，运行中修改立即生效；启动优先级：
  `DIFF_DATA_ROOT` 环境变量 > `backend/.data_root.json` > `<repo>/data`
- 文件下载接口 `GET /api/files/{session_id}/{rel_path}`：`rel_path` 允许 `meta.json`、`<group>/<file>`、`<group>/thumbs/<file>`、`results/<file>`；
  `session_id=snapshots` 时允许根目录文件 `<file>` 与子目录文件 `<folder>/<file>`（v1.3），
  v1.4 进一步支持任意深度 `<rel>/<file>`，分别按 `<data_root>/snapshots/<rel>`（旧口径，优先）
  与 `<data_root>/<rel>`（v1.4 绝对 folder 口径）解析。
  必须做路径穿越防护（不允许绝对路径、`..` 段、路径分隔符滥用与越出目标目录），非法路径返回 400
  - `<group>` ∈ `bias|dark|flat|light`（含 `thumbs/` 子目录）；`masters/` 与 `calibrated/` 目录下的文件同样可直接访问（如 `masters/MASTER_DARK.fits`）
  - 所有 `/api/files/**` 响应头固定带 `Cache-Control: no-store`（v1.2，保证拉伸预览等文件永远取最新版本）

---

## 2. REST 端点

### 2.1 数据目录设置（v1.2）

**GET /api/settings**

```json
{ "ok": true, "data": { "data_root": "/Users/me/diffraction_stacking/data", "writable": true }, "error": null }
```

- `data_root`：当前生效的**绝对路径**
- `writable`：当前进程对 `data_root` 是否可写（布尔值）

**PUT /api/settings** — body `{ "data_root": "/abs/path/to/data" }`

```json
{ "ok": true, "data": { "data_root": "/abs/path/to/data", "writable": true }, "error": null }
```

- 校验：`data_root` 必须是非空绝对路径，否则 **400**（英文错误）
- `data_root` 不存在时自动创建 `data_root`、`data_root/snapshots/`、`data_root/captures/`；
  创建失败返回 **400**（英文错误）
- 成功返回新的 `{data_root, writable}`，并**立即生效**：CaptureService、session 扫描、
  snapshot、处理管线每次读写都通过动态 `data_root` 访问器读取当前值（不得启动时一次性固化）
- 持久化到 `backend/.data_root.json`（内容 `{"data_root": "<绝对路径>"}`，已加入 `.gitignore`）；
  启动优先级：环境变量 `DIFF_DATA_ROOT` > `backend/.data_root.json` > 默认 `<repo>/data`

### 2.2 健康与设备

**GET /api/health**

```json
{ "ok": true, "data": { "status": "ok", "version": "1.6.0", "platform": "darwin", "camera_driver": "mock" }, "error": null }
```

`camera_driver`：`mock`（无 ic4 时）/ `ic4`（已加载 imagingcontrol4）。

**GET /api/devices**

```json
{ "ok": true, "data": [
  { "id": "mock-1", "name": "Mock LEED/RHEED Camera", "serial": "MOCK-0001",
    "kind": "mock", "connected": false }
], "error": null }
```

- mock 设备始终存在（真机 ic4 可用时追加 `kind:"ic4"` 设备，id 前缀 `ic4-`）
- `connected` 指当前是否被本服务打开

**POST /api/device/open** — body `{ "id": "mock-1" }`

```json
{ "ok": true, "data": { "id": "mock-1", "name": "Mock LEED/RHEED Camera",
  "serial": "MOCK-0001", "connected": true }, "error": null }
```

- 打开新设备前自动关闭旧设备；ic4 设备在无驱动平台返回 `ok:false, error:"Unable to load imagingcontrol4…"`

**POST /api/device/close** — 无 body

```json
{ "ok": true, "data": { "connected": false }, "error": null }
```

**GET /api/device/info**

```json
{ "ok": true, "data": { "id": "mock-1", "name": "Mock LEED/RHEED Camera",
  "serial": "MOCK-0001", "connected": true, "properties": [ <见 2.3> ] }, "error": null }
```

未打开设备时 `connected:false`、`properties:[]`。

### 2.3 属性

**GET /api/properties**

```json
{ "ok": true, "data": { "properties": [
  { "name": "exposure", "label": "Exposure", "value": 20000, "min": 30, "max": 30000000,
    "step": 1, "unit": "us", "readonly": false, "type": "int" },
  { "name": "gain", "label": "Gain", "value": 0, "min": 0, "max": 48,
    "step": 0.1, "unit": "dB", "readonly": false, "type": "float" },
  { "name": "auto_exposure", "label": "Auto Exposure", "value": true, "readonly": false, "type": "bool" },
  { "name": "auto_gain", "label": "Auto Gain", "value": true, "readonly": false, "type": "bool" },
  { "name": "black_level", "label": "Black Level", "value": 0, "min": 0, "max": 255,
    "step": 1, "unit": "ADU", "readonly": false, "type": "int" },
  { "name": "width", "label": "Width", "value": 1024, "min": 480, "max": 1920,
    "step": 16, "unit": "px", "readonly": false, "type": "int" },
  { "name": "height", "label": "Height", "value": 768, "min": 4, "max": 1200,
    "step": 4, "unit": "px", "readonly": false, "type": "int" },
  { "name": "offset_x", "label": "Offset X", "value": 448, "min": 0, "max": 896,
    "step": 4, "unit": "px", "readonly": false, "type": "int" },
  { "name": "offset_y", "label": "Offset Y", "value": 216, "min": 0, "max": 432,
    "step": 4, "unit": "px", "readonly": false, "type": "int" },
  { "name": "reverse_x", "label": "Reverse X", "value": false, "readonly": false, "type": "bool" },
  { "name": "reverse_y", "label": "Reverse Y", "value": false, "readonly": false, "type": "bool" },
  { "name": "trigger_mode", "label": "Trigger Mode", "value": false, "readonly": false, "type": "bool" },
  { "name": "device_temperature", "label": "Sensor Temperature", "value": 32.5, "min": 0, "max": 100,
    "step": 0.1, "unit": "°C", "readonly": true, "type": "float" }
], "error": null }
```

- 数值型属性必须有 `min/max/step/unit`；bool 型无 min/max；枚举型（若出现）带 `options`
- Capture region（采集区域）通过 width/height/offset_x/offset_y 表达（若相机支持）；width/height 对应 GenICam `Width`/`Height`，mock 至少支持 width/height

**PUT /api/properties/{name}** — body `{ "value": 50000 }`

```json
{ "ok": true, "data": { "name": "exposure", "value": 50000, "actual": 50000 }, "error": null }
```

- 越界值自动钳制到 min/max，`actual` 为相机实际生效值
- 未打开设备返回 409

**GET /api/properties/all**（Parameters 标签页专用，返回相机**全部**可控参数）

```json
{ "ok": true, "data": { "properties": [
  { "name": "ExposureTime", "label": "Exposure Time", "value": 20000, "type": "float",
    "min": 30, "max": 30000000, "step": 1, "unit": "us",
    "readonly": false, "locked": false, "available": true, "visibility": "BEGINNER" },
  { "name": "PixelFormat", "label": "Pixel Format", "value": "Mono16", "type": "enum",
    "options": ["Mono8", "Mono12Packed", "Mono16"],
    "readonly": false, "locked": false, "available": true, "visibility": "BEGINNER" },
  { "name": "DeviceUserID", "label": "Device User ID", "value": "", "type": "string",
    "readonly": false, "locked": false, "available": true, "visibility": "BEGINNER" },
  { "name": "TriggerSoftware", "label": "Trigger Software", "value": null, "type": "command",
    "readonly": false, "locked": false, "available": true, "visibility": "BEGINNER" }
], "error": null }
```

扩展形状（向后兼容 §2.3 的属性对象）：
- `type` 在 `int|float|bool|enum` 基础上增加 `string` 与 `command`；`value` 可为 `string|null`
  （除 number/boolean 外）
- 新增可选字段：`locked`(bool，采集/自动控制锁定)、`available`(bool，当前是否可用)、
  `visibility`(string：`BEGINNER|EXPERT|GURU|INVISIBLE`，仅 UI 建议，与可写性无关)
- 数值型 `int/float` 带 `min/max/step/unit`；`enum` 带 `options`；`string`/`command` 无 range

**PUT /api/properties/{name}** 对任意属性名与全部类型通用：
- `int/float`：数值校验 + min/max 钳制 + increment 对齐；`bool`：On/Off/true/false 兼容；
  `enum`：校验 options；`string`：直接透传；`command`：执行命令（返回 `actual:null`）
- 响应：`{ "ok": true, "data": { "name": "<name>", "value": <请求值>, "actual": <生效值> } }`
- `actual` 类型与属性类型一致（可为 string/null）；readonly→400、locked→409、unavailable→409

### 2.4 实时预览、目录浏览与抓拍

**POST /api/live/start** — body `{ "fps": 15, "scale": 1.0, "quality": 80 }`

```json
{ "ok": true, "data": { "streaming": true, "fps": 15, "scale": 1.0, "quality": 80 }, "error": null }
```

**POST /api/live/stop** — 无 body → `{ "ok": true, "data": { "streaming": false }, "error": null }`

**WS /ws/live**（live start 后开始推送，可多客户端同时订阅）

- 每条消息顺序固定：**先 1 条文本 JSON 统计，后 1 条二进制 JPEG**
- 文本 JSON：

```json
{ "min": 0, "max": 4095, "mean": 180.2, "std": 42.1, "saturated_pct": 0.31,
  "fps": 15.0, "histogram": [ <256 个 int> ],
  "histogram_min": 143, "histogram_max": 207 }
```

- `histogram` 为 **raw 12-bit 原始帧**（`grab_frame()` 输出）的 256 bin 直方图，bin 范围按当前帧的实际 ADU min–max 自适应缩放；不使用拉伸后的 preview
- `histogram_min` / `histogram_max`：本次自适应直方图对应的 ADU 上下限（即当前帧 raw min/max），前端用于标注横轴范围
- JPEG：8-bit 灰阶、自动对比度（1%~99% 分位线性映射）、质量=quality、尺寸=当前采集区域×scale

**GET /api/browse/folders**（v1.4）— 目录浏览器数据源（抓拍文件夹选择）

query：`?path=<绝对路径，可空>`

```json
{ "ok": true, "data": {
  "path": "/Users/me/diffraction_stacking/data",
  "parent": null,
  "files": [],
  "folders": [
    { "name": "captures", "path": "/Users/me/diffraction_stacking/data/captures" },
    { "name": "snapshots", "path": "/Users/me/diffraction_stacking/data/snapshots" }
  ]
}, "error": null }
```

- `path` 缺省/空 → 当前 `data_root`；否则必须是**已存在的绝对目录**，resolve 后必须
  `== data_root` 或位于其内部；任何不满足（相对路径、不存在、越界）都返回 **400**
  `error:"Folder must be inside the data root"`
- 仅列出**直接子目录**（按名称排序，不列文件）；`files` 恒为 `[]`（契约占位字段）
- `parent`：上一级绝对路径；当前目录为 `data_root` 顶层时 `parent:null`

**POST /api/snapshot** — body `{ "format": "fits", "folder": "/abs/path/to/dir", "prefix": "spot", "comment": "Si(111) 1x1, 60 eV" }`

- `format` ∈ `fits`|`tiff`|`png`（缺省 `fits`）
- `folder`（v1.4）：可选。
  - 缺省/空 → `<data_root>/snapshots/`（兼容旧行为）
  - **绝对路径**：resolve 后必须 `== data_root` 或位于其内部（越界返回 **400**
    `"Folder must be inside the data root"`）；目录不存在时自动 `mkdir(parents=True)`，
    创建失败返回 **400**（英文错误）
  - **单段相对名**（v1.3 兼容，如 `"测试文件夹"`）：映射为 `<data_root>/snapshots/<name>`；
    仍禁止 `..`/`.` 段、路径分隔符 `/` `\` 与 Windows 非法字符 `< > : " | ? *`（400 英文错误）
- `prefix`（v1.4）：可选。sanitize 口径：路径分隔符与 `< > : " | ? *` 及空白统一替换为
  `_`，允许为空；非 ASCII 字符（如中文）保留
- `comment`（可选）：用户备注，写入文件头。接受任意 UTF-8 文本（含中文），
  前后端均限制 ≤256 字符（超长后端返回 400）；缺省/空 = 不写入
  - FITS：写为 `COMMENT` 卡片（值经 ASCII 转写，多行按 `\n` 拆成多条，见 §3.2）
  - TIFF：完整原文（UTF-8）写入私有 tag 65000，并进入 ImageDescription(270) 的
    ASCII JSON（见 §3.2）
  - PNG：无头部元数据，忽略
- **文件名恒带时间戳**（v1.4）：
  - 有 prefix → `<prefix>_<YYYYMMDDTHH-MM-SS>.<ext>`
  - 无 prefix → `<YYYYMMDDTHH-MM-SS>.<ext>`

```json
{ "ok": true, "data": {
  "file": "spot_20260822T10-00-00.fits",
  "url": "/api/files/snapshots/2026_observing/spot_20260822T10-00-00.fits",
  "folder": "/Users/me/diffraction_stacking/data/2026_observing",
  "prefix": "spot"
}, "error": null }
```

- 响应 `folder` 恒为**绝对路径**；`prefix` 为 sanitize 后的值（缺省/空时为 `""`）
- `url`：folder 位于 `<data_root>/snapshots/` 内时使用旧口径
  `/api/files/snapshots/<rel-to-snapshots>/<file>`（根目录无中间段）；
  其余 data_root 内目录使用 `/api/files/snapshots/<rel-to-data_root>/<file>`
- 快照 16-bit（FITS/TIFF 保留原始 uint16，PNG 为自动对比度 8-bit）
- 快照 FITS 头同 §3.2 必填关键字（`SESSION=SNAPSHOT`、`NFRAME=1`），并写入
  全量相机参数（`HIERARCH CAM …`）与用户 `COMMENT`（见 §3.2）
- 快照 TIFF 由 tifffile 写入：ImageDescription(270) + Software(305) +
  私有 tag 65000（UTF-8 完整元数据 JSON，含中文 comment 原文，见 §3.2）
- 未打开设备返回 409

**GET /api/snapshot/folders**（v1.3 兼容保留；v1.4 前端新流程不再使用，旧调用继续可用）

```json
{ "ok": true, "data": { "folders": [
  { "name": "", "files": 3 },
  { "name": "测试文件夹", "files": 5 }
] }, "error": null }
```

- 列出 `<data_root>/snapshots/` 的**直接子目录**（不含根目录文件，按名称排序）
- 根目录本身以 `name:""` 表达，`files` 为根目录下直接文件数；
  每个子目录的 `files` 为该子目录内直接文件数

### 2.5 序列拍摄

**POST /api/capture/plan** — body：

```json
{ "session_name": "LEED_Si111_001", "phases": [
  { "type": "bias", "count": 50, "exposure_us": 30, "note": "" },
  { "type": "dark", "count": 30, "exposure_us": null, "note": "与 light 同曝光" },
  { "type": "flat", "count": 30, "exposure_us": null, "note": "束流散焦均匀照明" },
  { "type": "light", "count": 100, "exposure_us": null, "note": "" }
]}
```

- `session_name` 为用户**原始输入**；后端计算 effective session name（v1.2）：

  ```
  effective = <YYYYMMDDTHH-MM-SS>_<sanitize(input)>   # 本地时间
  ```

  `sanitize`：去除路径分隔符与 Windows 非法字符 `< > : " / \ | ? *` 及空白
  （统一替换为 `_`）；空输入（或清洗后为空）使用 `"session"`。
  非 ASCII 字符（如中文）保留于目录名/meta/URL，但 FITS 头 `SESSION` 写 ASCII 安全值。
- 响应 `data` 中的 `session_name` 为 effective（拍摄目录 id、meta.json 的 `name`、
  `/api/sessions` 与 `/api/sessions/{id}` 的 `id`/`name`、capture state 的 `session_name`
  均使用 effective，不再使用原始输入）
- `exposure_us: null` = 沿用当前相机曝光；非 null = 该相位强制覆盖（拍摄时设置、相位结束不恢复）
- `type` ∈ `bias|dark|flat|light`；`count` 1–10000；`phases` 不能为空（空数组返回 400）
- 相位 `note` 写入该相位每帧 FITS 头的 `COMMENT`（值经 ASCII 转写，多行按 `\n`
  拆分，见 §3.2）

```json
{ "ok": true, "data": {
  "plan_id": "plan-abc12345",
  "session_name": "20260822T10-00-00_LEED_Si111_001",
  "phases": [ ...校验后回显... ]
}, "error": null }
```

- 拍摄中重新 plan 返回 409

**POST /api/capture/start** — 无 body。要求已 plan 且设备已打开。

```json
{ "ok": true, "data": { "status": "running" }, "error": null }
```

**GET /api/capture/state**

```json
{ "ok": true, "data": {
  "status": "waiting_user",
  "session_name": "20260822T10-00-00_LEED_Si111_001",
  "current_phase": { "type": "light", "index": 4, "total": 4 },
  "frames_done": 110, "total_frames": 210,
  "prompt": "Restore normal diffraction conditions. Check the exposure/gain parameters against the live preview and histogram, then confirm to start the continuous Light capture",
  "message": "",
  "needs_user": true,
  "log": [ { "t": "2026-08-22T10:00:01", "msg": "BIAS done 50/50" } ]
}, "error": null }
```

- `status` ∈ `idle|running|waiting_user|capturing|finished|aborted|error`
- `session_name` 为 effective session name（v1.2，含时间戳前缀）
- `needs_user`：当前相位是否需要人工确认。**所有相位开始前都会进入 `waiting_user` 并给出该相位 prompt**；
  BIAS / DARK 相位 `needs_user=false`，展示约 0.2s 后自动进入 `capturing`（prompt 转入 `message`）；
  **FLAT 与 LIGHT 相位 `needs_user=true`，必须 POST /api/capture/resume 才进入 `capturing`**
- 未 plan 时 status=`idle`

**POST /api/capture/resume** — 无 body。仅在 `waiting_user` 且 `needs_user=true` 时有效（FLAT / LIGHT 强制人工确认，具体是哪个相位见 `current_phase`）；其他状态返回 409。确认后 `status` 切到 `capturing`，提示语按当前相位生成并写入 `log`（例如 `Parameters confirmed, starting FLAT capture`）。

```json
{ "ok": true, "data": { "status": "capturing" }, "error": null }
```

**POST /api/capture/abort** — 无 body。中止并保留已拍帧。

```json
{ "ok": true, "data": { "status": "aborted" }, "error": null }
```

进度与状态变化经 **WS /ws/events** 推送：

```json
{ "topic": "capture", "data": { "status": "...", "session_name": "20260822T10-00-00_LEED_Si111_001",
  "current_phase": { "type": "light", "index": 4, "total": 4 },
  "frames_done": 111, "total_frames": 210, "prompt": "...", "message": "...",
  "needs_user": true } }
```

### 2.6 拍摄 set（相位表草稿与命名 set）

Capture 页相位表（每个相位的 `type` / `count` / `exposure_us`，草稿另含 `note`）有两层持久化，均落在 `data_root` 下：

- `<data_root>/capture_draft.json`：当前草稿，相位表每次改动后自动覆盖写入
- `<data_root>/capture_sets.json`：用户显式保存的命名 set

两者互不影响：**写草稿绝不会改动 `capture_sets.json`**（已保存的 set 不会被未保存的编辑覆盖）。

**GET /api/capture/draft** — 读取草稿；无草稿时 `phases` 为空数组：

```json
{ "ok": true, "data": {
  "phases": [
    { "type": "bias", "count": 50, "exposure_us": 30, "note": "bias frame" },
    { "type": "light", "count": 100, "exposure_us": null, "note": null }
  ],
  "updated_at": "2026-11-03T12:00:00"
}, "error": null }
```

**PUT /api/capture/draft** — body `{ "phases": [ ... ] }`（允许空数组）：

- 校验规则与 `POST /api/capture/plan` 一致：`type` ∈ `bias|dark|flat|light`、`count` 1–10000、
  `exposure_us` 允许 `null`；非法相位返回 400
- 每项形如 `{type, count, exposure_us, note}`：`note` 为字符串或 `null`，会写进该相位每帧的
  FITS `COMMENT`，因此刷新后必须从草稿恢复；草稿不含 `session_name`
- 返回写入后的 `{phases, updated_at}`；草稿文件结构同上

**GET /api/capture/sets** — 已保存 set 的名称列表，按名称排序：

```json
{ "ok": true, "data": { "names": ["LEED_A", "LEED_B"] }, "error": null }
```

**GET /api/capture/set?name=&lt;name&gt;** — 返回 `{name, phases, updated_at}`：

- 名称走**查询参数**而非路径段，因此 `a/b`、`LEED a&b 100%` 这类名字可原样保存/读取/删除
- 不存在返回 404；`name` 去首尾空白后为空返回 400

**PUT /api/capture/set?name=&lt;name&gt;** — 新建或覆盖，body `{ "phases": [ ... ] }`：

- 校验规则同草稿（`type`/`count`/`exposure_us`），但 **set 不保存 `note`**：即使请求里带
  `note` 也会被丢弃，落盘键集严格是 `{type, count, exposure_us}`
- `name` 去首尾空白后为空返回 400
- 重名即覆盖原 set（不产生重复条目），返回 `{name, phases, updated_at}`

**DELETE /api/capture/set?name=&lt;name&gt;** — 删除；返回 `{ "name": "...", "deleted": true }`；不存在返回 404。

`<data_root>/capture_sets.json` 结构：

```json
{ "sets": {
  "LEED_A": { "phases": [ ... ], "updated_at": "2026-11-03T12:00:00" }
} }
```

- 每个 set 条目严格只有 `phases` 与 `updated_at`；每个相位严格只有 `type`/`count`/`exposure_us`
- 草稿与命名 set 都是独立的 JSON 文件，后端重启后读盘即恢复

**损坏文件不会被静默当成空状态，也不会被覆盖**：

- 文件非法 JSON、顶层不是对象、`sets` 结构非法或 `phases` 不是数组时，相关接口返回 500，
  `error` 消息里带文件名（如 `capture_sets.json has an invalid sets object`）
- 文件保持字节不变：`GET` 不会重建它，`PUT` 也会先读取校验再写，因此一次“保存空表”不会覆盖损坏的 set 或草稿
- 写草稿仍然绝不触碰 `capture_sets.json`（损坏判定也一样：草稿报错不会改动 set 文件）
- 修复方式：把文件改回合法 JSON 或删除它，接口立即恢复；文件不存在仍是正常空状态
  （`GET` 不创建文件，首次 `PUT` 才写）

Capture 页前端行为：

- 进入页面先 `GET /api/capture/draft`：`phases` 非空则填入相位表（含 `note`），否则使用内置默认模板
- 相位表任何改动后 500ms 防抖 `PUT /api/capture/draft`（草稿带 `note`）
- 草稿读取或写入失败（包括后端因文件损坏返回 500）时页面会弹出错误提示并附后端 `error` 文案，
  不会静默回退默认模板、也不会静默丢弃编辑；轮询类请求（`/api/capture/state`、`/api/sessions`）仍保持静默
- 命名 set 下拉（选中即加载到相位表；清空下拉不发请求）+ `Save As…` / `Overwrite` / `Delete` 三个操作；
  重名另存、覆写、删除各自弹一次确认
- capture state 为 `running|waiting_user|capturing` 时：set 操作、相位表四个行内编辑器
  （`type` / `count` / `exposure_us` / `note`）、`Add Phase` 与 `Default` 按钮全部禁用

### 2.7 Session 与文件

**GET /api/sessions**

```json
{ "ok": true, "data": [
  { "id": "20260822T10-00-00_LEED_Si111_001", "name": "20260822T10-00-00_LEED_Si111_001",
    "created": "2026-08-22T10:00:00",
    "frame_counts": { "bias": 50, "dark": 30, "flat": 30, "light": 100 },
    "has_results": false }
], "error": null }
```

- v1.2：`id` 与 `name` 均为 effective session name（时间戳前缀 + 清洗后的用户输入）

**GET /api/sessions/{id}**

```json
{ "ok": true, "data": {
  "id": "20260822T10-00-00_LEED_Si111_001", "name": "20260822T10-00-00_LEED_Si111_001",
  "created": "2026-08-22T10:00:00",
  "phases": [ { "type": "bias", "count": 50, "exposure_us": 30, "note": "" } ],
  "frames": {
    "bias": [ { "file": "BIAS_0001.fits", "url": "/api/files/<id>/bias/BIAS_0001.fits",
                "thumb": "/api/files/<id>/bias/thumbs/BIAS_0001.png",
                "mean": 12.3, "std": 1.4, "saturated_pct": 0.0 } ]
  },
  "results": [ { "file": "STACK_sigma3.fits",
                 "url": "/api/files/<id>/results/STACK_sigma3.fits",
                 "preview": "/api/files/<id>/results/STACK_preview.png" } ],
  "qc": { "...": "qc_report.json 内容（若存在）" }
}, "error": null }
```

- v1.2：`name` 为 effective session name（与 `id` 一致，非原始输入）

**GET /api/files/{session_id}/{rel_path}** — 文件下载/访问；`snapshots` 目录以 `session_id=snapshots` 表达。

- v1.3：`session_id=snapshots` 时支持根目录文件 `/api/files/snapshots/<file>` 与
  单级子目录文件 `/api/files/snapshots/<folder>/<file>`（folder 与 POST /api/snapshot 同口径校验，
  路径穿越/越出目录返回 400）
- v1.4：`session_id=snapshots` 的任意深度 `<rel>/<file>` 依次按
  `<data_root>/snapshots/<rel>`（旧口径，已存在的文件优先）与 `<data_root>/<rel>`
  （v1.4 绝对 folder 口径）解析，均必须在 `data_root` 内，穿越/越界返回 400
- 响应头固定带 `Cache-Control: no-store`。

### 2.8 数据处理

拍摄状态为 `running|waiting_user|capturing` 时提交任何处理任务返回 **409**。

**POST /api/process/calibrate** — body `{ "session_id": "<id>", "sigma": 3, "method": "mean" }`

可选字段 `exclude`（v1.1，按文件名排除坏帧，**仅匹配文件名、不含路径**）：

```json
{ "session_id": "<id>", "sigma": 3, "method": "mean",
  "exclude": { "bias": ["BIAS_0003.fits"], "dark": [], "flat": [], "light": [] } }
```

- `exclude` 缺省或全空 = 不排除；组名只能 ∈ `bias|dark|flat|light`；数组内重复文件名后端去重
- 排除的文件不存在时返回 **400**（英文错误）；calibrate 按组过滤后合成 master
- job done 的 `result.excluded_counts` 为 `{bias,dark,flat,light}` 各分组排除计数

```json
{ "ok": true, "data": { "job_id": "job-abc123", "type": "calibrate", "status": "pending" }, "error": null }
```

**POST /api/process/stack** — body `{ "session_id": "<id>", "sigma": 3, "method": "mean", "registration": false }`

同样支持可选 `exclude`（v1.1）；**stack 只过滤 `light`**，其余分组仅计入 `excluded_counts` 统计。job done 的 `result.excluded_counts` 与 `qc_report.json` 均含 `excluded_counts`。

**POST /api/process/stretch** — body `{ "session_id": "<id>", "result_file": "STACK_sigma3.fits",
"stretch": "asinh", "black": 0, "white": 4095 }`

```json
{ "ok": true, "data": { "job_id": "job-def456", "type": "stretch", "status": "pending" }, "error": null }
```

- `stretch` ∈ `linear|asinh|log`；输出覆盖 `results/STACK_preview.png`（8-bit PNG）；可另存 16-bit 导出
- `STACK_*.fits` 为 **float32（BITPIX=-32）**，不做 uint16 截断（v1.1）

**GET /api/process/jobs/{job_id}**

```json
{ "ok": true, "data": { "id": "job-abc123", "type": "calibrate",
  "status": "running", "progress": 40, "message": "Synthesizing master_dark (30 frames)",
  "result": null }, "error": null }
```

- `status` ∈ `pending|running|done|error`；done 时 `result` 为产物信息（文件/url/qc）

**WS /ws/events** 推送 job 变化：

```json
{ "topic": "job", "data": { "id": "job-abc123", "type": "calibrate",
  "status": "running", "progress": 40, "message": "..." } }
```

### 2.9 原生文件夹选择（v1.5）

**POST /api/pick-folder** — 在服务器本机打开原生目录选择对话框（同步 `def`，由 FastAPI 线程池执行，不阻塞事件循环）。

body（可选）：

```json
{ "title": "Select Folder" }
```

- `title` 缺省为 `"Select Folder"`

成功选择：

```json
{ "ok": true, "data": { "path": "/Users/me/data", "cancelled": false }, "error": null }
```

用户取消/关闭对话框：

```json
{ "ok": true, "data": { "path": null, "cancelled": true }, "error": null }
```

平台不可用/无法启动选择器（无 GUI、缺少 `osascript`/`powershell`/`zenity`/`kdialog` 等）：

```json
{ "ok": false, "data": null, "error": "Native folder picker is not available ..." }
```

HTTP 状态为 **501**。

平台实现：
- macOS：`osascript` `choose folder`，返回 POSIX 绝对路径
- Windows：PowerShell `System.Windows.Forms.FolderBrowserDialog`
- Linux：优先 `zenity --file-selection --directory`，其次 `kdialog --getexistingdirectory`

---

## 3. 处理管线与文件格式

### 3.1 磁盘布局

`data_root` 默认 `<repo>/data`，可用 `GET/PUT /api/settings` 或 `DIFF_DATA_ROOT` 环境变量配置（见 §2.1）：

```
<data_root>/
  snapshots/<ts>.{fits,tiff,png}                       # folder 缺省/空（v1.4 命名）
  snapshots/<folder>/<ts>.{fits,tiff,png}              # v1.3 相对名兼容 / snapshots 内绝对目录
  <任意子目录>/<prefix>_<ts>.{fits,tiff,png}            # v1.4：data_root 内绝对 folder（prefix 非空时）
  <任意子目录>/<ts>.{fits,tiff,png}                     # v1.4：无 prefix
  capture_draft.json                                  # 相位表草稿（未保存的编辑）
  capture_sets.json                                   # 命名 set（用户显式保存）
  captures/<effective_session_name>/
    meta.json            # {id, name, created, phases[], camera{...}}；name 为 effective session name
    bias/  BIAS_0001.fits ...  + thumbs/BIAS_0001.png
    dark/  DARK_0001.fits ...  + thumbs/DARK_0001.png
    flat/  FLAT_0001.fits ...  + thumbs/FLAT_0001.png
    light/ LIGHT_0001.fits ... + thumbs/LIGHT_0001.png
    masters/  MASTER_BIAS.fits  MASTER_DARK.fits  MASTER_FLAT.fits
    calibrated/ CAL_0001.fits ...
    results/  STACK_sigma3.fits  STACK_preview.png  qc_report.json
```

### 3.2 FITS 头（每帧必填）

基础关键字：

```
DATE-OBS   ISO 8601 毫秒      EXPOSURE   曝光 µs
GAIN       dB                 BLACKLEV   黑电平 ADU
TEMP       传感器温度 °C        FRAMETYP   BIAS|DARK|FLAT|LIGHT|SNAPSHOT
CAMERA     'DMK 33GX249e'     SERIAL     序列号
SESSION    effective session 名（v1.2）    NFRAME     帧序号
MEAN/STD/SATPCT   帧质量统计（快照=单帧实时计算；序列拍摄=每帧统计写入）
BITPIX=16（有效位 12）
```

全量相机参数（HIERARCH 扩展键；`camera.get_properties()` 支持时写入，
序列拍摄每帧与快照 FITS 均包含）：

```
HIERARCH CAM WIDTH      采集宽度 px      HIERARCH CAM HEIGHT     采集高度 px
HIERARCH CAM OFFSETX    采集偏移 X px    HIERARCH CAM OFFSETY    采集偏移 Y px
HIERARCH CAM REVERSX    Reverse X        HIERARCH CAM REVERSY    Reverse Y
HIERARCH CAM TRIGGER    Trigger Mode     HIERARCH CAM AUTOEXP    Auto Exposure
HIERARCH CAM AUTOGAIN   Auto Gain
HIERARCH CAM MOCKPAT / MOCKSCE / MOCKDRIFT   # Mock 专属，仅存在时写入
```

- 13 个契约属性（exposure/gain/black_level/device_temperature 用上面的基础键，
  width/height/offset_x/offset_y/reverse_x/reverse_y/trigger_mode/
  auto_exposure/auto_gain 用 HIERARCH CAM 键）；Mock 的 mock_pattern/
  mock_scene/mock_drift 以 `HIERARCH CAM MOCKPAT/MOCKSCE/MOCKDRIFT` 写入

用户 comment：

- 来源：序列拍摄 = 相位 `note`；快照 = 请求体 `comment`
- 写为 `COMMENT` 卡片；值经 `ascii_fits_value()` 转写为 printable ASCII
  （非 ASCII 字符如中文替换为 `_`，保证 FITS 头 ASCII 合规）；多行按 `\n`
  拆成多条 `COMMENT`（全空白行跳过，不生成空 COMMENT 卡）
- 中文原文不写入 FITS 头；完整保留于 TIFF 快照的私有 tag 65000（UTF-8，见下）

**TIFF 快照元数据**（仅快照；由 tifffile 写入，不再用 PIL）：

| Tag | 编号 | 内容 |
|---|---|---|
| ImageDescription | 270 | 参数 + 相机身份的 ASCII JSON（机器可读，非 ASCII 以 `\uXXXX` 转义） |
| Software | 305 | `Static AstroPhoto` |
| 私有 tag | 65000 | 完整元数据 JSON 的 UTF-8 字节（含中文 comment 原文；dtype `'B'`，writeonce=False） |

数据本身仍为 uint16 原帧（有效位 12），与 FITS 口径一致。

### 3.3 处理公式（实现口径统一）

```
master_bias = sigma_clipped_mean(bias*, σ=3, iter=3)   # bias* 为排除坏帧后的帧组
master_dark = sigma_clipped_mean(dark*, σ=3, iter=3)
flat_tmp    = mean(flat*)            # 若存在同曝光 dark 先减之
master_flat = flat_tmp / median(flat_tmp)   # 中位数归一，median 为 0 时报错

cal_i = (light_i - master_dark) / master_flat     # float32，负值截 0
[可选] registration=true：对 cal_i 与 cal_0 做整数像素 FFT 相位相关，平移对齐
stack = sigma_clipped_mean(cal*, σ=3, iter=3)     # method=mean；median 备用
STACK_*.fits = float32（BITPIX=-32，不截断）        # v1.1
preview = stretch(stack) → 8-bit PNG              # linear|asinh|log + black/white
qc_report.json = { frames, total_exposure_us, effective_bits≈log2√N,
                   rejected_pct, background_std, elapsed_s,
                   excluded_counts: {bias,dark,flat,light} }
```

---

## 4. Mock 相机行为规范（开发期真机替代）

- 合成**静态 LEED/RHEED 衍射图案**：规则格点高斯斑（多级衍射点）+ 亮 00 斑 + 荧光屏背景辉光；uint16 数值 ≤ 4095
- 噪声模型：读出噪声（高斯，~1.5 ADU）+ 泊松噪声（√signal）+ 固定热像素（约 0.05% 像素）
- `exposure` 线性放大信号并放大泊松噪声；`gain` 倍乘信号与噪声；`black_level` 为加性偏置；三者均按 12-bit 截断
- 默认采集区域 1024×768（width 480–1920/step 16；height 4–1200/step 4），帧生成时间应满足 ≥10 fps
- 温度随运行时间缓慢上升（室温 +0.05 °C/分钟，供 dark 匹配演示）
- 支持可选的慢速图案漂移（每帧亚像素级），供 registration 功能演示
- mock 帧率目标 30 fps 以上（1024×768 时），`trigger_mode=true` 时不自动出帧

---

## 5. 前端实现要点（与契约呼应）

- axios 统一封装：解包 `{ok,data,error}`，`ok=false` → antd `message.error(error)`
- WS 消息：`typeof data === 'string'` 解析 JSON；`data instanceof Blob/ArrayBuffer` 走图像路径
- 所有图片/文件 URL 使用后端返回的 `url` 字段，**不自行拼接**
- 会话展示/详情使用后端返回的 `id`/`name`（v1.2 起 `name` 为 effective session name，含时间戳前缀）
- 数据目录通过 `GET/PUT /api/settings` 查看与修改（v1.2）；不得自行拼接磁盘路径
- 后端地址：`/settings` 可配置（默认 `http://localhost:8000`，localStorage 持久化）；
  dev 模式 vite 代理 `/api`、`/api/files`、`/ws`（ws:true）
- 生产模式：FastAPI 将 `frontend/dist/` 挂载到 `/`，SPA 子路由 fallback 到 index.html
