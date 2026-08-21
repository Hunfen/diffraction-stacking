/** API 契约类型定义 — 与 docs/api.md v1.0 保持一致 */

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

export interface SnapshotResult {
  file: string;
  url: string;
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
  phases: CapturePhasePlan[];
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
}

export interface JobResult {
  id: string;
  type: 'calibrate' | 'stack' | 'stretch';
  status: 'pending' | 'running' | 'done' | 'error';
  progress?: number;
  message?: string;
  result: unknown;
}

export interface JobEventData {
  id: string;
  type: 'calibrate' | 'stack' | 'stretch';
  status: 'pending' | 'running' | 'done' | 'error';
  progress?: number;
  message?: string;
}

export interface JobEvent {
  topic: 'job';
  data: JobEventData;
}

export type WsEvent = CaptureEvent | JobEvent;

export type ProcessMethod = 'mean' | 'median';
export type StretchMethod = 'linear' | 'asinh' | 'log';
