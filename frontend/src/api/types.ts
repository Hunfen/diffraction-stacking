/** API 契约类型定义 — 与 docs/api.md v1.4 保持一致 */

export type PhaseType = 'bias' | 'dark' | 'flat' | 'light';

export interface ApiEnvelope<T> {
  ok: boolean;
  data: T;
  error: string | null;
}

export interface HealthInfo {
  status: string;
  version: string;
  platform: string;
  camera_driver: 'mock' | 'ic4';
}

export interface DeviceItem {
  id: string;
  name: string;
  serial: string;
  kind: 'mock' | 'ic4';
  connected: boolean;
}

export interface DeviceInfo {
  id: string;
  name: string;
  serial: string;
  connected: boolean;
  properties: CameraProperty[];
}

export interface CameraProperty {
  name: string;
  label: string;
  value: number | boolean;
  min?: number;
  max?: number;
  step?: number;
  unit?: string;
  readonly: boolean;
  type: 'int' | 'float' | 'bool' | 'enum';
  options?: string[];
}

export interface PropertiesData {
  properties: CameraProperty[];
}

export interface PropertyPutResult {
  name: string;
  value: number | boolean;
  actual: number | boolean;
}

export interface LiveStartParams {
  fps: number;
  scale: number;
  quality: number;
}

export interface LiveState {
  streaming: boolean;
  fps?: number;
  scale?: number;
  quality?: number;
}

/** v1.3 遗留端点 GET /api/snapshot/folders 的子目录项（v1.4 前端不再使用） */
export interface SnapshotFolder {
  /** 子目录名；根目录用空字符串表示 */
  name: string;
  /** 该文件夹内的文件数 */
  files: number;
}

export interface SnapshotFoldersData {
  folders: SnapshotFolder[];
}

/** GET /api/browse/folders 返回的单个直接子目录 */
export interface BrowseFolderItem {
  name: string;
  /** 子目录绝对路径（用于作为下一次 browse 的 path 参数） */
  path: string;
}

/** GET /api/browse/folders 返回数据：当前目录、上一级目录与直接子目录列表 */
export interface BrowseFoldersData {
  /** 当前目录绝对路径 */
  path: string;
  /** 上一级目录绝对路径；已在 data_root 顶层时为 null */
  parent: string | null;
  /** v1.4 恒为空数组（目录浏览器不列文件） */
  files: string[];
  folders: BrowseFolderItem[];
}

/** v1.4：folder 为实际保存目录的绝对路径；prefix 为后端 sanitize 后的前缀（可为空字符串） */
export interface SnapshotResult {
  file: string;
  url: string;
  folder: string;
  prefix: string;
}

export interface LiveStats {
  min: number;
  max: number;
  mean: number;
  std: number;
  saturated_pct: number;
  fps: number;
  histogram: number[];
}

export interface CapturePhasePlan {
  type: PhaseType;
  count: number;
  exposure_us: number | null;
  note: string;
}

export interface CapturePlanResult {
  plan_id: string;
  /** v1.2：带时间戳前缀的最终 session 名（后端计算并写入响应，所有后续展示以该值为准） */
  session_name: string;
  phases: CapturePhasePlan[];
}

export interface DataDirSettings {
  data_root: string;
  writable: boolean;
}

/** POST /api/pick-folder 原生目录选择器响应；path 为 null 表示用户取消 */
export interface PickFolderResult {
  path: string | null;
}

export type CaptureStatus =
  | 'idle'
  | 'running'
  | 'waiting_user'
  | 'capturing'
  | 'finished'
  | 'aborted'
  | 'error';

export interface CaptureLogEntry {
  t: string;
  msg: string;
}

export interface CurrentPhaseInfo {
  type: PhaseType;
  index: number;
  total: number;
}

export interface CaptureState {
  status: CaptureStatus;
  session_name?: string;
  current_phase?: CurrentPhaseInfo;
  frames_done: number;
  total_frames: number;
  /** v1.1：true = 必须人工确认（阻塞 Modal）；false = 自动准备中（非阻塞 Banner） */
  needs_user?: boolean;
  prompt?: string;
  message?: string;
  log?: CaptureLogEntry[];
}

/** calibrate/stack 请求体 exclude：未勾选帧文件名按相位分组 */
export type ExcludeMap = Record<PhaseType, string[]>;

export interface CaptureEvent {
  topic: 'capture';
  data: CaptureState;
}

export interface SessionSummary {
  id: string;
  name: string;
  created: string;
  frame_counts: Record<PhaseType, number>;
  has_results: boolean;
}

export interface FrameInfo {
  file: string;
  url: string;
  thumb: string;
  mean?: number;
  std?: number;
  saturated_pct?: number;
}

export interface ResultInfo {
  file: string;
  url: string;
  preview: string;
}

export interface SessionDetail {
  id: string;
  name: string;
  created: string;
  phases: CapturePhasePlan[];
  frames: Partial<Record<PhaseType, FrameInfo[]>>;
  results: ResultInfo[];
  qc?: Record<string, unknown>;
  /** v1.5：持久化产物标记，用于处理步骤门控在刷新/切换会话后仍正确 */
  has_masters?: boolean;
  has_calibrated?: boolean;
}

export interface JobResult {
  id: string;
  type: 'calibrate' | 'stack' | 'stretch';
  status: 'pending' | 'running' | 'done' | 'error';
  progress?: number;
  message?: string;
  result: unknown;
}

/** POST /api/process/{calibrate|stack|stretch} 提交响应（字段名为 job_id，与 WS/GET 的 id 不同） */
export interface JobSubmitResult {
  job_id: string;
  type: 'calibrate' | 'stack' | 'stretch';
  status: 'pending' | 'running' | 'done' | 'error';
  /** v1.5：提交所属会话；旧后端可能缺省，前端可用当前 selectedId 兜底 */
  session_id?: string;
}

/** WS /ws/events 与 GET /api/process/jobs/{id} 返回的 job 负载（字段名为 id） */
export interface JobEventData {
  id: string;
  type: 'calibrate' | 'stack' | 'stretch';
  status: 'pending' | 'running' | 'done' | 'error';
  progress?: number;
  message?: string;
  /** v1.5：任务所属会话，用于按会话门控与避免跨会话刷新 */
  session_id?: string;
}

export interface JobEvent {
  topic: 'job';
  data: JobEventData;
}

export type WsEvent = CaptureEvent | JobEvent;

export type ProcessMethod = 'mean' | 'median';
export type StretchMethod = 'linear' | 'asinh' | 'log';
