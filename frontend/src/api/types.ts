/** API contract types — kept in sync with docs/api.md v1.4 */

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
  value: number | boolean | string | null;
  min?: number;
  max?: number;
  step?: number;
  unit?: string;
  readonly: boolean;
  type: 'int' | 'float' | 'bool' | 'enum' | 'string' | 'command';
  options?: string[];
  /** True when acquisition or automatic control locks the camera property; locked means not writable */
  locked?: boolean;
  /** Whether the property is currently available (controls are disabled on false); missing means available */
  available?: boolean;
  /** IC4/GenICam visibility (a UI hint such as Beginner/Expert/Guru/Invisible, unrelated to writability) */
  visibility?: string;
}

export interface PropertiesData {
  properties: CameraProperty[];
}

export interface PropertyPutResult {
  name: string;
  value: number | boolean;
  actual: number | boolean;
}

/** Extended response of PUT /api/properties/{name} (the Parameters page uses it for enum/string/command) */
export interface PropertySetResult {
  name: string;
  value: number | boolean | string | null;
  actual: number | boolean | string | null;
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

/** Subdirectory item of the legacy v1.3 GET /api/snapshot/folders (unused by the v1.4 frontend) */
export interface SnapshotFolder {
  /** Subdirectory name; the root directory is an empty string */
  name: string;
  /** Number of files in that folder */
  files: number;
}

export interface SnapshotFoldersData {
  folders: SnapshotFolder[];
}

/** One direct subdirectory returned by GET /api/browse/folders */
export interface BrowseFolderItem {
  name: string;
  /** Absolute path of the subdirectory (used as the path argument of the next browse call) */
  path: string;
}

/** GET /api/browse/folders payload: current directory, parent directory and direct subdirectories */
export interface BrowseFoldersData {
  /** Absolute path of the current directory */
  path: string;
  /** Absolute path of the parent directory; null at the data_root top level */
  parent: string | null;
  /** Always an empty array in v1.4 (the directory browser lists no files) */
  files: string[];
  folders: BrowseFolderItem[];
}

/** v1.4: folder is the absolute path actually saved to; prefix is the backend-sanitized prefix (may be empty) */
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
  histogram_min: number;
  histogram_max: number;
}

export interface CapturePhasePlan {
  type: PhaseType;
  count: number;
  exposure_us: number | null;
  note: string;
}

export interface CapturePlanResult {
  plan_id: string;
  /** v1.2: final session name with the timestamp prefix (computed by the backend; every later display uses it) */
  session_name: string;
  phases: CapturePhasePlan[];
}

/** Phase stored in a named set (exposure and count only, no note) */
export interface CapturePhaseStored {
  type: PhaseType;
  count: number;
  exposure_us: number | null;
}

/** Draft phase additionally keeps note: it goes into every frame's FITS COMMENT and must survive a refresh */
export interface CaptureDraftPhase extends CapturePhaseStored {
  note: string | null;
}

export interface CaptureDraft {
  phases: CaptureDraftPhase[];
  updated_at: string | null;
}

export interface CaptureSetNames {
  names: string[];
}

export interface CaptureSet {
  name: string;
  phases: CapturePhaseStored[];
  updated_at: string | null;
}

export interface DataDirSettings {
  data_root: string;
  writable: boolean;
}

/** Response of the POST /api/pick-folder native picker; path null means the user cancelled */
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
  /** v1.1: true = manual confirmation required (blocking Modal); false = preparing automatically (non-blocking banner) */
  needs_user?: boolean;
  prompt?: string;
  message?: string;
  log?: CaptureLogEntry[];
}

/** calibrate/stack request exclude: unchecked frame filenames grouped by phase */
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
  /** v1.5: persisted artifact marker so step gating stays correct after a refresh or session switch */
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

/** Response of POST /api/process/{calibrate|stack|stretch} (field is job_id, unlike the id used by WS/GET) */
export interface JobSubmitResult {
  job_id: string;
  type: 'calibrate' | 'stack' | 'stretch';
  status: 'pending' | 'running' | 'done' | 'error';
  /** v1.5: session that submitted the job; old backends may omit it and the frontend can fall back to selectedId */
  session_id?: string;
}

/** Job payload from WS /ws/events and GET /api/process/jobs/{id} (field is id) */
export interface JobEventData {
  id: string;
  type: 'calibrate' | 'stack' | 'stretch';
  status: 'pending' | 'running' | 'done' | 'error';
  progress?: number;
  message?: string;
  /** v1.5: session owning the job, used for per-session gating and to avoid cross-session refreshes */
  session_id?: string;
}

export interface JobEvent {
  topic: 'job';
  data: JobEventData;
}

export type WsEvent = CaptureEvent | JobEvent;

export type ProcessMethod = 'mean' | 'median';
export type StretchMethod = 'linear' | 'asinh' | 'log';
