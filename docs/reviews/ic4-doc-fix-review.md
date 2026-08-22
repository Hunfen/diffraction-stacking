# ic4-doc-fix-review：ic4 官方文档审查修复独立验收报告

> **验收结论：PASS（通过，附 4 条 ISSUE，均为轻微/文档级，无 FAIL）。**
> 本次为独立验收（reviewer），只读审查 + 回归测试，未修改任何源码。
> 对 `docs/reviews/ic4-doc-audit.md` 的 7 项结论逐条核对，并自行用 curl 抓取
> ic4python 官方文档原文、DMK 33GX249e 官方技术手册与 TIS 官方下载/安装文档进行
> 交叉验证；回归测试独立重跑全绿。
>
> ⚠ 与上游报告相同的声明：本机为 macOS，无法 `import imagingcontrol4` 实测，
> IC4Camera 真机路径仍未经过真实硬件验证。

---

## 0. 验收方式

- 只读审查：`git diff HEAD` 核对改动（backend/app/camera.py、backend/app/main.py、
  backend/README.md、docs/reviews/ic4-doc-audit.md；未碰 frontend/ 与 docs/api.md）。
- 官方原文交叉验证：curl 抓取 9 页 ic4python 官方文档转纯文本检索 +
  DMK 33GX249e Technical Reference Manual（PDF，pdftotext 提取）+
  TIS GigE 驱动下载页 / 安装参考 PDF / GenTL producer 下载页。
- 回归独立重跑：backend_smoke.py、uvicorn + curl 全链路、WS /ws/live、
  快照 FITS 校验、t1 遗留的伪 `imagingcontrol4` 静态演练脚本。

---

## 1. 逐条核对（审计报告 §1–§8）

| 报告条目 | 引用 URL/章节是否成立 | 修复是否落盘且与描述一致 | 结论 |
| --- | --- | --- | --- |
| §1 初始化与枚举 | ✅ 成立。getting-started「A Quick Test」为 `Library.init()` → `DeviceEnum.devices()`；api-reference `device_open(dev: DeviceInfo \| str)` 明确两者都支持，字符串不唯一抛 `ErrorCode.Ambiguous`；`DeviceInfo.model_name/serial/unique_name` 均存在。 | ✅ camera.py `_IC4_LOAD_LOCK`（L431）、`_device_serial()`（L479-489）、幂等 open（L622-669）。与报告一致。 | **PASS** |
| §2 Grabber 状态机 | ✅ 成立。technical-grabber-states 状态迁移与四个状态属性与报告一致；api-reference `stream_setup(sink=None, display=None, setup_option=StreamSetupOption.ACQUISITION_START)`，全文档检索 **0 处 `stream_start`**。 | ✅ `grab_frame()` L761 直接用 `stream_setup(self._sink)`；`close()` L719-743 按 stream_stop → device_close；`_ensure_open()` L745-751。与报告一致。 | **PASS** |
| §3 属性读写 | ✅ 成立。api-reference 全文检索 `PropertyMap.get_value()`/`get_range()`/`get_visibility()` 均为 **0 处**；`find()/find_integer()/find_float()/find_boolean()` 存在；PropId 常量表中只有 `EXPOSURE_TIME='ExposureTime'`，无独立 `EXPOSURE=`；`Property.is_readonly/is_locked/is_available`、`PropInteger.minimum/maximum/increment_mode/increment/valid_value_set/unit` 均在文档中；`get_value_bool`/`set_value(bool)` 对枚举的 true/false 条目选择行为与报告描述逐字一致。 | ✅ `_SPEC` 使用 `EXPOSURE_TIME`（L593）；`_read_prop`（L871-893）、`_range_details`（L830-862）、`set_property` 钳制/对齐/只读/锁定（L957-991）。与报告一致。 | **PASS** |
| §4 实时流 | ✅ 成立。api-reference 签名 `QueueSink(listener: QueueSinkListener, accepted_pixel_formats: List[PixelFormat] = [], max_output_buffers: int = 0)`；`try_pop_output_buffer() → ImageBuffer \| None`（不抛异常）；`pop_output_buffer()` 无参数（timeout_ms 只属于 `snap_single/snap_sequence`）；numpy 支持表只列 Mono8/Mono16/BGR 等，**不含 Mono12p/Mono12Packed**；DMK 33GX249e 手册 §5.1.1.3 原文确认 Mono16 数据在 most significant bits。 | ✅ `_make_queue_sink`（L545-572）listener 内联实现 + `accepted_pixel_formats=[Mono16]` + `max_output_buffers=2`；`_pop_with_timeout`（L784-804）；`numpy_copy()+finally release`（L763-772）；`_normalize_12bit`（L806-819）。与报告一致。 | **PASS** |
| §5 错误处理 | ✅ 成立。technical-error-handling：`IC4Exception.code/message`；api-reference ErrorCode 数值 28/28 与代码映射表逐一比对**全部吻合**（3/4/5/…/109 见 §2.5）。 | ✅ camera.py `_IC4_CODE_STATUS`（L436-465）；main.py `raise_cam_err`（L259-285）优先 status，500 统一“服务器内部错误”。与报告一致。 | **PASS** |
| §6 部署 | ✅ 主体成立。technical-distributing 原文：“matching GenTL producer (IC4 driver) must be installed”；getting-started 含 `-py36-none-win_amd64.whl` 平台标签示例。Windows “Filter 绑定”一句在报告正文未给出独立引用（见 ISSUE-1）。 | ✅ backend/README.md L41-99 与报告描述一致。 | **PASS（附 ISSUE-1）** |
| §7 契约兼容 | ✅ 成立。`git diff HEAD -- docs/api.md` 为空；`git diff HEAD -- frontend` 为空；diff hunk 分析确认 `BaseCamera`/`MockCamera` 代码块未被触碰；`CameraError.__init__(message, status=None)` 为可选参数。 | ✅ 与报告一致。 | **PASS** |
| §8 修改文件清单 | ✅ 与 `git diff --numstat` 完全一致：camera.py +510/-116、main.py +14/-1、README.md +35/-9；docs/api.md/frontend 未改。注意 main.py 实际还多了 `/api/device/info` 的 try/except 包裹，清单文字未提及（见 ISSUE-3）。 | — | **PASS（附 ISSUE-3）** |

---

## 2. 官方文档独立抽查（curl 抓取原文，共 5 处）

### 2.1 QueueSink 构造签名（api-reference.html）

官方原文（HTML 转纯文本检索）：

> `class QueueSink ( listener : QueueSinkListener , accepted_pixel_formats : List [ PixelFormat ] = [] , max_output_buffers : int = 0 )`

实现（camera.py L556-572）以内联 `lib.QueueSinkListener` 子类作为第一位置参数，
`accepted_pixel_formats=[Mono16]`、`max_output_buffers=2`。**成立。**
`accepted_pixel_formats` 官方说明“can be used to force an automatic conversion from
the device’s pixel format to a pixel format usable by the sink”——报告 §4 的自动转换
论述成立。

### 2.2 pop_output_buffer 无 timeout_ms / try_pop_output_buffer 存在

官方原文：

> `pop_output_buffer ( ) → ImageBuffer` … The buffers are retrieved in order they were
> received … the oldest image is returned first.
> `try_pop_output_buffer ( ) → ImageBuffer | None` … this function does not raise an
> exception in case of an error.

api-reference 全文 `timeout_ms` 仅出现在 `snap_single(timeout_ms)` 与
`snap_sequence(count, timeout_ms)`。报告“旧代码 `pop_output_buffer(timeout_ms=5000)`
必然 TypeError”成立；新实现用 `try_pop_output_buffer()+5ms 轮询`（L784-804）符合文档。
（注：官方原文未明确写 `pop_output_buffer` “阻塞”，报告措辞略强于原文，见 ISSUE-2。）

### 2.3 PropertyMap 方法族与 PropId.EXPOSURE_TIME

官方 api-reference 全文检索：

- `PropertyMap.get_value(` / `get_range(` / `get_visibility(`：**0 处**；
- `get_value_int/float/bool/str`、`find()/find_integer()/find_float()/find_boolean()`：存在；
- PropId 常量表：`EXPOSURE_TIME = 'ExposureTime'` 存在，`EXPOSURE =` 独立常量**不存在**；
- `get_value_bool`：“For enumeration properties, a value is returned if the name of the
  currently selected entry unambiguously suggests to represent true or false”；
- `set_value(bool)`：“For enumeration properties, it selects the entry with a name that
  unambiguously suggests to represent True or False”；`set_value(int)` 对枚举仅在
  PixelFormat 上生效（与 `_try_set_native_mono16` 用 int 设置 `PIXEL_FORMAT` 一致）。

实现 `_SPEC`（L592-604）与 `_read_prop/_coerce_value`（L871-955）与原文一致。**成立。**

### 2.4 Grabber 状态机与 stream_setup 默认参数

official api-reference 原文：

> `stream_setup ( sink : Sink | None = None , display : Display | None = None ,
> setup_option : StreamSetupOption = StreamSetupOption.ACQUISITION_START ) → None`

technical-grabber-states 原文：`stream_setup → Streaming`；若 setup_option 为
`ACQUISITION_START` 则直接进入 Acquisition Active；`stream_stop → Device Opened`；
`device_close → Initial`（且自动停流/停采集）。api-reference 全文 **无 `stream_start`**。
实现 L754-762 首次抓帧直接 `stream_setup(self._sink)` 并复用 `is_streaming`，
`close()` L730-743 严格 `stream_stop → device_close`。**成立。**

### 2.5 ErrorCode 数值与 Mono16 高 12 位

- api-reference ErrorCode：代码 `_IC4_CODE_STATUS` 的 28 个常量数值逐项比对全部一致
  （如 `InvalidOperation=3`、`DeviceNotFound=16`、`Ambiguous=18`、`Timeout=27`、
  `SinkNotConnected=38`、`GenICamFeatureNotFound=101`、`GenICamAccessDenied=106`、
  `GenICamValueError=108`）。
- DMK 33GX249e Technical Reference Manual §5.1.1.3 原文：

> “The sensor of the DMK 33GX249e camera is not capable of providing 16-bit data output.
> Instead, the pixel data is transmitted in the most significant bits …”
> “GigE Vision drivers see this pixel format as Mono16.”

实现 `_normalize_12bit` 对设备原生 Mono16 恒右移 4 位（L814-816），契约
“uint16、≤4095、低 12 位”成立。**成立。**

---

## 3. 契约一致性

- `git diff HEAD -- docs/api.md` → **空**：契约文件未被修改。
- `git diff HEAD -- frontend` → **空**：前端未被修改。
- 新字段检查：改动未向 REST 响应添加任何新字段；`CameraError.status` 仅为后端内部
  异常属性，不序列化到响应。frontend 只用 `name/value/type/readonly/min/max/step/
  unit/options` 等既有契约字段（frontend/src/api/types.ts），不依赖新字段。
- 错误包裹形状：main.py 既有 `StarletteHTTPException` 处理器（L692-699）将全部
  HTTPException 包成 `{ok:false,data:null,error:...}`，`raise_cam_err` 新增分支不破坏
  包裹形状；frontend `client.ts` 按 `body.error` 读错误，任意状态码均兼容。
- `BaseCamera`（内部 CameraInterface 抽象）与 `MockCamera` 代码块在 diff 中未被触碰；
  `CameraError` 仅新增可选 `status` 参数，Mock 路径 `status=None` 走原有中文信息匹配，
  行为不变（main.py 旧匹配分支原样保留）。

---

## 4. 回归测试（独立重跑）

### 4.1 backend_smoke.py

```
cd backend && .venv/bin/python ../scripts/backend_smoke.py
SMOKE 结果：114/114 通过
全部通过 ✅
```

### 4.2 uvicorn + curl 基本路径（独立启动，端口 8123）

| 步骤 | 结果 |
| --- | --- |
| `GET /api/health` | `{"ok":true,...,"platform":"darwin","camera_driver":"mock"}` ✅ |
| `GET /api/devices` | 仅 mock-1，字段完整 ✅ |
| `POST /api/device/open {"id":"mock-1"}` | `connected:true` ✅ |
| `GET /api/device/info` | 14 个属性完整（exposure/gain/black_level/ROI/bool/温度）✅ |
| `GET /api/properties` | 与 device/info 一致 ✅ |
| `POST /api/live/start {fps:15,scale:0.5}` | `streaming:true` ✅ |
| WS `/ws/live` | 先文本统计 JSON（min/max/mean/std/saturated_pct/fps + 256 bin 直方图），后二进制 JPEG（`\xff\xd8\xff` 头）✅ |
| `PUT /api/properties/exposure {value:999999999}` | 钳制 `actual:30000000` ✅ |
| `POST /api/snapshot {format:"fits"}` | 返回文件 + URL ✅ |
| FITS 校验 | uint16 (768×1024)，max=4095，BITPIX=16、SESSION=SNAPSHOT、NFRAME=1、EXPOSURE/GAIN/DATE-OBS 等头完整 ✅ |
| `POST /api/live/stop` / `POST /api/device/close` | 正常 ✅ |

### 4.3 t1 遗留静态演练独立重跑

`/tmp/test_fake_ic4.py`（伪 imagingcontrol4 模块）重跑：**18/18 PASS，FAILURES: []**
（覆盖 枚举→open→PIXEL_FORMAT=Mono16→属性读/钳制/只读保护→stream_setup 抓帧→
Mono16 高 12 位 >>4→close(stream_stop→device_close)→设备失效 503）。

---

## 5. 分级结论

**PASS（共 7 项）**：见 §1 表，审计报告 §1–§8 全部通过。

**FAIL（0 项）**：无。

**ISSUE（4 项，均不阻塞验收）**

1. **ISSUE-1（低，文档引用缺口）**：审计报告 §6“Windows GigE 必须把 ic4 驱动
   Filter 绑定到相机网卡，否则 `DeviceEnum.devices()` 看不到设备”在报告正文没有给出
   直接官方引用，且其列举的 technical-distributing / getting-started 两页均不含该内容。
   该说法与 TIS 官方 GigE 驱动安装参考 PDF（含“Kernel-Mode filter driver”安装步骤）及
   TIS 驱动下载页方向一致，属事实性可接受，但建议在审计报告中补上安装文档/知识库
   链接，使“每条结论都有 URL 依据”完整成立。
2. **ISSUE-2（低，措辞略强于原文）**：报告 §4 称 `pop_output_buffer()`“阻塞取最旧帧”。
   api-reference 原文只说明“Retrieves … oldest image is returned first”与无参数签名，
   未明确写 blocking 语义。修复本身（改用 try_pop 轮询）不受影响。
3. **ISSUE-3（低，清单描述不完整）**：审计报告 §8 对 main.py 的描述只提
   `raise_cam_err` 的两点改动，未提实际同时新增的 `/api/device/info` 的 try/except
   `raise_cam_err` 包裹（main.py L378-383）。该改动与 §5 错误处理目标一致、行为合理，
   仅报告描述不完整。
4. **ISSUE-4（低，代码注释引用不准确）**：camera.py L433-435 注释称
   “参照契约 docs/api.md §1：…503 相机不可用…”，但 docs/api.md §1 实际只列
   400/404/409/500，没有 503（503 在旧代码“无法加载 imagingcontrol4”分支已存在，
   非本次新引入）。建议后续契约修订时在 docs/api.md §1 补记 503，并同步注释。

---

## 6. 真机联调仍存风险（静态审查无法消除的不确定性）

1. **Mono16 高 12 位 >>4 只对本型号成立**：依据已由 DMK 33GX249e 手册 §5.1.1.3 原文
   确认，但 `_always_shift_mono16` 是“设备原生 Mono16 成功即恒 >>4”的型号假设；
   换用其他机型必须重新验证（低 12 位机型会被错误缩小 16 倍）。
2. **sink 自动转换兜底分支**：设备不支持 Mono16 时走 `accepted_pixel_formats=[Mono16]`
   的 ic4 内部转换，官方文档未说明 12-bit→Mono16 的位对齐方式；`max>4095` 自适应
   右移在暗帧/低信号场景可能误判。需用饱和场景 + 暗场两轮实测。
3. **TriggerMode 枚举条目的 On/Off 名称假设**：`get_value_bool`/`set_value(bool)` 依赖
   官方文档“unambiguously suggests true/false”的条目命名，目标相机需实测确认。
4. **ExposureAuto/GainAuto 锁定**：默认自动控制开启时 exposure/gain 会 `is_locked`
   → 409，联调需先关闭自动控制（报告 §9.5 已列，验收确认其依据成立）。
5. **`max_output_buffers=2` 丢帧**：采集慢于相机帧率时丢弃中间帧，不保证严格连续；
   若后续做逐帧定量测量需评估 SnapSink/加大队列（报告 §9.3 已列）。
6. **GigE 部署**：Windows 网卡 Filter 绑定、Jumbo Frame 9000 MTU、GenTL producer 与
   `imagingcontrol4` wheel 版本匹配需在真机环境逐一验证；Linux 的接口权限/防火墙同理。
7. **本机无法 import imagingcontrol4**：IC4Camera 全部真机路径（真实驱动行为、帧率、
   内存压力下的 buffer release、断线事件时序）仍未经过真实硬件验证。建议真机首轮
   联调清单：open → get_properties 非空 → set exposure/gain 回读一致 → grab_frame
   最大 ADU≈4095（饱和）→ close → 重开 → 热插拔断线返回 503 → 长时间 live 内存稳定。

---

## 7. 验收结论

**PASS。** ic4-doc-fix-review 认定 `docs/reviews/ic4-doc-audit.md` 的 7 项审查结论
引用依据成立、修复与报告描述一致（4 条轻微 ISSUE 均为文档/注释级），契约未破坏，
回归 114/114 + HTTP/WS/FITS 全链路无回归。可以进入真机联调阶段，联调前建议先消化
§6 的 7 条风险清单。本验收未 commit，未修改源码。
