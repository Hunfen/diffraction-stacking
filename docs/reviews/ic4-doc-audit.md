# IC4 官方文档专项审查报告（ic4-doc-audit）

> **审查结论：通过（静态）。** 本次对照 ic4python 官方文档逐项审计
> `backend/app/camera.py` 的 `IC4Camera`，发现并修复 **5 处严重级 API 误用**
> （任一都会导致真机路径完全不可用），并修正错误映射与部署文档。
> 回归 `cd backend && .venv/bin/python ../scripts/backend_smoke.py` 为 **114/114 全绿**，
> Mock 行为未变。
>
> **⚠ 重要声明：本报告为“静态对照官方文档”结论，未经真机运行。**
> 本机为 macOS，无法 `import imagingcontrol4`；所有修复依据官方文档
> URL/章节 + 官方 GitHub 示例（`TheImagingSource/ic4-examples`，已 clone 至
> `/tmp/ic4-examples`）+ DMK 33GX249e 官方技术手册，真机联调前请先看文末
> “真机联调注意点”。

---

## 0. 审查依据（官方文档）

| 页面 | 用途 |
| --- | --- |
| https://www.theimagingsource.com/en-us/documentation/ic4python/guide-getting-started.html | `Library.init()` → `DeviceEnum.devices()` 调用时序 |
| https://www.theimagingsource.com/en-us/documentation/ic4python/guide-device-enumeration.html | `DeviceInfo.model_name/serial/unique_name`、枚举方式 |
| https://www.theimagingsource.com/en-us/documentation/ic4python/guide-configuring-device.html | `device_open(DeviceInfo)`、`device_property_map.set_value(PropId...)` |
| https://www.theimagingsource.com/en-us/documentation/ic4python/guide-grabbing-an-image.html | Sink 创建 → `stream_setup` → 抓帧 → `stream_stop` |
| https://www.theimagingsource.com/en-us/documentation/ic4python/technical-grabber-states.html | Grabber 状态机（Initial/Device Opened/Streaming/Acquisition Active/Device Invalid） |
| https://www.theimagingsource.com/en-us/documentation/ic4python/technical-device-properties.html | PropertyMap / PropId / PropInteger / PropFloat / PropBoolean / PropEnumeration |
| https://www.theimagingsource.com/en-us/documentation/ic4python/technical-error-handling.html | `IC4Exception.code/message`、标准异常类型 |
| https://www.theimagingsource.com/en-us/documentation/ic4python/technical-distributing.html | Python 包 + GenTL producer 部署要求 |
| https://www.theimagingsource.com/en-us/documentation/ic4python/api-reference.html | `Grabber` / `DeviceEnum` / `QueueSink` / `ImageBuffer` / `PropertyMap` / `PropId` / `ErrorCode` / `Library` / `PixelFormat` |
| https://github.com/TheImagingSource/ic4-examples | QueueSinkListener、numpy 互操作官方示例 |
| DMK 33GX249e Technical Reference Manual §5.1.1（12-Bit Packed / 16-Bit Monochrome） | Mono16 像素数据位于**高 12 位** |

---

## 1. 初始化与枚举（问题级别：高）

**官方依据**
- getting-started「A Quick Test」：`import imagingcontrol4 as ic4` →
  `ic4.Library.init()` → `ic4.DeviceEnum.devices()`，初始化必须先于枚举。
- api-reference `Library.init` / `DeviceEnum.devices()`。
- device-enumeration：`DeviceInfo.model_name`、`DeviceInfo.serial`、
  `DeviceInfo.unique_name`（serial 格式设备相关）。
- api-reference `Grabber.device_open(dev: DeviceInfo | str)`：
  **两者都支持**；字符串按 model name/unique name/serial/user id/IPv4/MAC
  匹配，不唯一时抛 `ErrorCode.Ambiguous`。

**现状**
- `_load_ic4()` 的 init→devices 时序正确，但没有并发保护：多个线程同时首次
  调用可能重复 `Library.init()`。
- `enumerate_ic4_devices()` 用 `serial or "unknown"` 兜底：无 serial 设备会
  全部碰撞为 `ic4-unknown`，且 open 时按 serial 找不到对应 DeviceInfo。
- `open()` 无幂等保护，重复调用会重复 `device_open`；打开失败时只置
  `self._grabber = None`，没有 `device_close()` 回收。
- 按 serial 枚举出 `DeviceInfo` 再 `device_open(target)` 的做法**方向正确**
  （优于传裸字符串，规避 Ambiguous），保留。

**修复**
- `_IC4_LOAD_LOCK` 保护 `Library.init()` 单次初始化。
- `_device_serial()`：serial 为空时回落到 `unique_name`，再兜底 `"unknown"`。
- `open()` 幂等（已开且 `is_device_valid` 时直接返回）；Device Invalid 时先
  回收旧 Grabber 再重开；失败路径 `stream_stop → device_close` 完整回收；
  `_grabber/_sink/_lib` 只在全部成功后发布到实例。

---

## 2. Grabber 状态机（问题级别：严重）

**官方依据**
- technical-grabber-states：
  `device_open → Device Opened`（此时才可配置）；
  `stream_setup → Streaming`，默认 `StreamSetupOption.ACQUISITION_START`
  时直接进入 Acquisition Active；`stream_stop → Device Opened`；
  `device_close → Initial`（且会自动停流）。
- api-reference `Grabber`：**没有 `stream_start()`**；`stream_setup` 默认参数
  就是 `ACQUISITION_START`；有 `is_device_open/is_device_valid/is_streaming/
  is_acquisition_active` 四个状态属性。
- guide-grabbing-an-image：`stream_setup(sink)` 后即可抓帧，结束时
  `stream_stop()`。

**现状**
- `grab_frame()` 调用了**不存在的 `Grabber.stream_start()`** → 首次抓帧必然
  `AttributeError`（真机路径完全不可用）。
- `close()` 只 `stream_stop()`，从不 `device_close()`，Grabber 停在
  Device Opened，设备资源泄漏；`_sink/_grabber` 引用被丢弃但设备未关。
- 未用任何 `is_device_open/is_streaming/is_device_valid` 状态属性做防护；
  未打开就抓帧只有自维护的 `_opened` 布尔。

**修复**
- `grab_frame()`：首次抓帧 `stream_setup(self._sink)`（默认 ACQUISITION_START，
  不再有 `stream_start()`），后续复用 `is_streaming` 判断。
- `close()`：严格按 `stream_stop → device_close` 回到 Initial；异常时也继续
  清理并重置全部状态字段。
- `_ensure_open()`：未打开→409；`is_device_valid == False`→503
  「相机连接已断开」。
- `open()` 幂等防重复 `device_open`（见 §1）。

---

## 3. 属性读写（问题级别：严重）

**官方依据**
- technical-device-properties + api-reference `PropertyMap`：
  **PropertyMap 没有 `get_value()`、`get_range()`、`get_visibility()`**。
  正确接口是 `find()/find_integer()/find_float()/find_boolean()`、
  `get_value_int/float/bool/str()`、`set_value()`、`try_set_value()`、
  `all()`；范围/步进在 **PropInteger/PropFloat** 上：`minimum/maximum/
  increment_mode/increment/valid_value_set/unit`；可写性在 **Property** 上：
  `is_readonly/is_locked/is_available`（`visibility` 只是 UI 建议值，与可写
  无关）。
- api-reference `PropId`：**没有 `EXPOSURE`，只有 `EXPOSURE_TIME`**；
  `GAIN/BLACK_LEVEL/WIDTH/HEIGHT/OFFSET_X/OFFSET_Y/REVERSE_X/REVERSE_Y/
  TRIGGER_MODE/DEVICE_TEMPERATURE/PIXEL_FORMAT` 均存在。
- `TriggerMode` 按 SFNC 通常是**枚举（On/Off）**（官方示例
  `set_value(PropId.TRIGGER_MODE, "On")`）；api-reference
  `PropertyMap.get_value_bool` 明确支持“枚举条目可唯一识别为 true/false 时
  返回布尔”，`set_value(bool)` 对枚举会选 On/Off 条目。

**现状**
- `get_properties()` 对每个属性调用 `pmap.get_value(pid)`、
  `pmap.get_range(pid)`、`pmap.get_visibility(pid)` —— 三个方法在
  PropertyMap 上**都不存在**，被 `except` 吞掉 → 真机属性列表恒为空。
- 用 `PropId.EXPOSURE`（不存在）→ 即使有 `get_value` 也拿不到曝光。
- `set_property()` 的 `pmap.get_value()` 同样不存在 → 设置后回读必炸；
  没有只读/锁定/可用性检查，没有越界钳制与 increment 对齐（契约要求
  越界自动钳制）。
- ROI 硬编码 `min=0, max=1000000, step=1`，非相机实际范围。

**修复**
- 契约属性名 → 官方 PropId 常量映射表 `_SPEC`：
  `exposure→EXPOSURE_TIME`、`gain→GAIN`、`black_level→BLACK_LEVEL`、
  `width→WIDTH`、`height→HEIGHT`、`offset_x→OFFSET_X`、`offset_y→OFFSET_Y`、
  `reverse_x→REVERSE_X`、`reverse_y→REVERSE_Y`、`trigger_mode→TRIGGER_MODE`、
  `device_temperature→DEVICE_TEMPERATURE`。
- `_read_prop()`：`pmap.find(pid)` 取 Property，按
  `PropertyType.INTEGER/FLOAT/BOOLEAN` 分派；int/float 用
  `minimum/maximum/increment_mode/increment/valid_value_set` 生成契约
  `min/max/step/unit`；`trigger_mode` 用 `get_value_bool`（枚举 On/Off
  归一化为 bool）；`is_readonly` 直接来自 `prop.is_readonly`。
- `set_property()`：未知属性/缺属性→400；`is_readonly`→400；
  `is_locked/is_available`→409；数值先钳制到 min/max，再按 INCREMENT
  对齐或 VALUE_SET 就近取值，最后 `set_value` + 回读 actual。
- `get_property()` 改为单属性直读（原 BaseCamera 会遍历整个列表，
  LiveService 每帧调用 `trigger_mode` 时对 ic4 开销过大）。

---

## 4. 实时流（问题级别：严重）

**官方依据**
- api-reference `QueueSink(listener: QueueSinkListener,
  accepted_pixel_formats: List[PixelFormat] = [], max_output_buffers: int = 0)`：
  **第一个位置参数必须是 QueueSinkListener 实例**，不是 PixelFormat。
- `QueueSink.pop_output_buffer()` **没有 `timeout_ms` 参数**（阻塞取最旧帧）；
  `try_pop_output_buffer()` 不抛异常、无帧返回 `None`。
- `sink_connected()` 返回 `True` 且不手动分配时，sink 自动分配流所需的
  最小缓冲数（官方示例做法）；`frames_queued/sink_disconnected` 在流
  线程/stream_stop 线程回调。
- `accepted_pixel_formats` 会触发 ic4 自动格式转换。
- api-reference `ImageBuffer.numpy_wrap/numpy_copy` 支持表：
  **Mono8/Mono16/BGR 等，不含 Mono12p/Mono12Packed**；
  `numpy_wrap` 数组仅在 ImageBuffer 引用存续期间有效；
  `release()` 把缓冲还给 sink 空闲队列。
- DMK 33GX249e 技术手册 §5.1.1.3：Mono16 时 12-bit 数据在**高 12 位**
  （most significant bits），须右移 4 位才满足契约“uint16、≤4095、低 12 位”。

**现状**
- `lib.QueueSink(pixel_format)`：把 PixelFormat 当 listener 传 → 构造即
  `TypeError`（真机路径完全不可用）。
- 优先请求 `Mono12p`：numpy_wrap/numpy_copy 不支持该格式，即使构造成功也
  会在 numpy 一步抛 `RuntimeError`。
- `pop_output_buffer(timeout_ms=5000)`：参数不存在 → `TypeError`。
- `numpy_wrap` 后 `np.array(copy=True)` 虽安全，但缓冲未显式 release，
  依赖 GC；12-bit 解包/高 12 位归一化完全缺失。

**修复**
- `_make_queue_sink()`：内联实现 `QueueSinkListener`（sink_connected 返回
  True，官方示例同款），`accepted_pixel_formats=[PixelFormat.Mono16]`，
  `max_output_buffers=2`（防止抓取线程跟不上时输出队列无限堆积）。
- `open()` 中 `try_set_value(PropId.PIXEL_FORMAT, Mono16)` 尽量让设备原生
  输出 Mono16（在 Device Opened 状态配置，符合状态机）。
- `_pop_with_timeout()`：用 `try_pop_output_buffer()` + 5ms 轮询实现 5000ms
  超时（官方 `pop_output_buffer` 无超时参数）；close 请求可提前打断。
- 取帧：`numpy_copy()`（拷贝语义，不依赖缓冲生命周期）→ 形状
  `(H,W,1)→(H,W)` → uint16 → `finally: buf.release()` 立即归还缓冲。
- `_normalize_12bit()`：设备原生 Mono16 时**恒右移 4 位**（依据 DMK
  33GX249e 手册 §5.1.1.3）；sink 自动转换的兜底路径按 `max>4095` 自适应
  右移（残余风险见 §9）。

---

## 5. 错误处理（问题级别：高）

**官方依据**
- technical-error-handling：所有函数错误抛异常；ic4 专属错误是
  `IC4Exception`，`code` 标识错误类别、`message` 为详情。
- api-reference `ErrorCode`：`InvalidOperation=3`、`DeviceNotFound=16`、
  `Ambiguous=18`、`Timeout=27`、`GenICamFeatureNotFound=101`、
  `GenICamAccessDenied=106`、`GenICamValueError=108` 等。
- docs/api.md §1：400 参数错误 / 404 不存在 / 409 状态冲突 / 500 内部错误，
  **500 必须返回“服务器内部错误”且不泄漏细节**。

**现状**
- ic4 错误一律被包成 `CameraError("...：{exc}")` 后靠 main.py 做中文子串
  匹配：`GenICamFeatureNotFound`、`DeviceInvalid`、`Timeout` 等都无法命中
  语义 → 多数会落到 500 且 500 会带出 ic4 原始消息，违反契约。
- “未找到序列号…”不含“设备不存在”关键字 → 会被误判 500。

**修复**
- `CameraError` 增加可选 `status`（内部字段，不改 CameraInterface 签名）：
  Mock 路径不设置，仍走原字符串匹配，行为不变。
- camera.py 内置 `ErrorCode → HTTP` 映射表 `_IC4_CODE_STATUS`：
  400：InvalidParamVal/Ambiguous/ParseError/GenICamFeatureNotFound/
  GenICamTypeMismatch/GenICamNotImplemented/GenICamValueError；
  404：DeviceNotFound；409：InvalidOperation/SinkNotConnected/
  SinkAlreadyAttached/SinkConnectAborted/SnapAborted/GenICamAccessDenied/
  GenICamDeviceError；503：LibraryNotInitialized/NoData/DeviceInvalid/
  DeviceError/Timeout；其余 500。
- main.py `raise_cam_err()`：优先用 `CameraError.status`；
  **status==500 时强制返回“服务器内部错误”并只写服务端日志**，不泄漏细节。
- 抓帧超时/设备断开 → 503；属性被锁 → 409；只读 → 400；未知属性 → 400，
  与契约语义一致。

---

## 6. 部署（问题级别：中）

**官方依据**
- technical-distributing：目标系统除了安装 `imagingcontrol4` Python 包，
  **还必须安装匹配的 GenTL producer（ic4 驱动）**；带 UI 时还需要
  `imagingcontrol4pyside6`。
- getting-started：wheel 有平台标签（如 `-py36-none-win_amd64.whl`），
  目标平台需选择匹配版本。

**现状**
- README 只有 Windows 部署段落，笼统说“安装 GigE 驱动”；未说明
  Linux 的运行时库差异、wheel 平台匹配、GenTL producer 的官方措辞；
  Jumbo Frame 只是一句话建议。

**修复**
- backend/README.md 重写为「真机部署（Windows/Linux）」：
  - 统一口径：Python 包 + 匹配 GenTL producer 缺一不可（引用
    technical-distributing）；
  - Windows：GigE 必须把 ic4 驱动 Filter 绑定到相机网卡，否则
    `DeviceEnum.devices()` 看不到设备；建议 Jumbo Frame（9000 MTU）；
  - Linux：安装匹配的 TIS ic4 GenTL producer、Linux wheel、接口权限/
    防火墙、Jumbo Frame 提示；
  - 通用验证步骤保留。

---

## 7. 契约兼容性

- **docs/api.md 未做任何修改**，契约未破坏。
- `CameraInterface`（即 `BaseCamera` 的 open/close/grab_frame/get_properties/
  get_property/set_property/info 签名）**未改**；MockCamera 代码路径一行未动。
- 唯一内部改动是 `CameraError.__init__(message, status=None)` 增加可选参数，
  属后端内部异常类，不进 API 契约；main.py 按 status 处理，Mock 路径
  status=None 走原逻辑。
- 回归：`cd backend && .venv/bin/python ../scripts/backend_smoke.py`
  → **114/114 通过**（见输出 `SMOKE 结果：114/114 通过`），
  mock open→properties→live→snapshot→capture→process 全链路行为不变。
- 附加静态演练（非真机）：以注入的伪 `imagingcontrol4` 模块跑通
  `枚举→open（PIXEL_FORMAT=Mono16）→get_properties→set_property（钳制/只读
  保护）→grab_frame（stream_setup、Mono16 高 12 位 >>4、buffer release）→
  close（stream_stop→device_close）→设备失效 503`，18/18 PASS。

---

## 8. 修改文件清单

| 文件 | 改动 |
| --- | --- |
| `backend/app/camera.py` | `CameraError` 增加可选 `status`；`_load_ic4` 加锁；`_device_serial` 兜底 unique_name；新增 ErrorCode→HTTP 映射；重写 `IC4Camera`（状态机、QueueSink listener、Mono16+右移 4 位归一化、PropertyMap 正确读写、超时取帧、缓冲释放） |
| `backend/app/main.py` | `raise_cam_err` 优先使用 `CameraError.status`；500 统一“服务器内部错误”不泄漏细节 |
| `backend/README.md` | 部署章节改为 Windows/Linux 双平台 + GenTL producer/Jumbo Frame/Filter 绑定说明；补充 Mono16→低 12 位归一化说明 |
| `docs/reviews/ic4-doc-audit.md` | 本报告（新增） |

未修改：`frontend/`、`docs/api.md`、MockCamera、CameraInterface 签名。

---

## 9. 真机联调注意点（静态审查残余不确定性）

1. **Mono16 高 12 位假设**：设备原生 Mono16 恒 `>>4` 的依据是
   DMK 33GX249e 技术手册 §5.1.1.3，请用饱和/中灰场景验证最大值≈4095
   （而非 65520）；若个别机型 Mono16 输出低 12 位，需把
   `_always_shift_mono16` 的判定改为按型号/属性表。
2. **sink 自动转换兜底分支**：设备不支持 Mono16 时走
   `accepted_pixel_formats=[Mono16]` 的 ic4 内部转换，其对齐方式官方文档
   未明说；当前按 `max>4095` 自适应右移，暗帧场景可能误判，需真机验证。
3. **QueueSink 参数**：`max_output_buffers=2` 在采集慢于相机帧率时会丢中间
   帧（每帧仍是真实帧，不保证严格连续）；如需严格逐帧，联调时再评估
   SnapSink 或加大队列。
4. **TriggerMode**：契约是 bool，按 `get_value_bool`/`set_value(bool)` 映射
   On/Off；若目标相机 TriggerMode 枚举条目不叫 On/Off，需要改字符串分支。
5. **曝光/增益自动控制**：若相机默认 `ExposureAuto/GainAuto` 开启，
   `ExposureTime/Gain` 会 `is_locked`，设置返回 409；联调时先关闭自动控制。
6. **device_property_map 的 set 前置条件**：`PIXEL_FORMAT` 在 Device Opened
   状态设置（当前实现如此）；若未来改为流中改 ROI，需先 `stream_stop`。
7. **Windows GigE**：确认 ic4 驱动 Filter 绑定到相机网卡、Jumbo Frame
   9000 MTU 生效；否则高分辨率 Mono16 可能带宽不足或枚举不到设备。
8. **wheel 版本**：`pip install imagingcontrol4` 的版本需与已安装的 ic4
   GenTL producer 匹配（technical-distributing 官方口径）。

---

## 10. 逐项结论速览

| # | 审计项 | 结论 | 原问题级别 |
| --- | --- | --- | --- |
| 1 | 初始化与枚举 | ✅ 已修复（init 加锁、serial 兜底、幂等 open、失败回收） | 高 |
| 2 | Grabber 状态机 | ✅ 已修复（删除不存在的 stream_start、补 device_close、状态属性防护） | 严重 |
| 3 | 属性读写 | ✅ 已修复（find/find_* + PropInteger/Float 范围、EXPOSURE_TIME、TriggerMode bool、锁定/只读/钳制） | 严重 |
| 4 | 实时流 | ✅ 已修复（QueueSinkListener、Mono16 转换、try_pop 超时、numpy_copy+release、高 12 位右移） | 严重 |
| 5 | 错误处理 | ✅ 已修复（ErrorCode→HTTP 映射、500 不泄漏） | 高 |
| 6 | 部署 | ✅ 已修复（Windows/Linux 双平台、GenTL producer、Filter/Jumbo Frame） | 中 |
| 7 | 契约兼容 | ✅ 未破坏（docs/api.md 未改；Mock/CameraInterface 未改；114/114） | — |
