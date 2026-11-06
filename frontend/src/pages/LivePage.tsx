import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
} from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Divider,
  Empty,
  Input,
  InputNumber,
  Row,
  Select,
  Slider,
  Space,
  Statistic,
  Switch,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  CameraOutlined,
  FolderOpenOutlined,
  LinkOutlined,
} from '@ant-design/icons';
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import dayjs from 'dayjs';
import { useAcquisition } from '../api/acquisition';
import { apiGet, apiPost, apiPut } from '../api/client';
import { resolveUrl } from '../api/backend';
import { getPreviewSettings } from '../api/settings';
import { useReconnectingWebSocket, type WsStatus } from '../api/ws';
import type {
  CameraProperty,
  DataDirSettings,
  LiveStats,
  PickFolderResult,
  PropertiesData,
  PropertyPutResult,
  SnapshotResult,
} from '../api/types';

const WS_LIVE = '/ws/live';
const SNAPSHOT_FOLDER_KEY = 'ds.snapshot.folder';
const SNAPSHOT_PREFIX_KEY = 'ds.snapshot.prefix';
const ROI_PROP_NAMES = ['width', 'height', 'offset_x', 'offset_y'] as const;

/** data_root -> default snapshot folder <data_root>/snapshots. */
function defaultSnapshotFolder(dataRoot: string): string {
  return `${dataRoot.replace(/\/+$/, '')}/snapshots`;
}

const statusMeta: Record<WsStatus, { color: string; text: string }> = {
  idle: { color: 'default', text: 'Not Started' },
  connecting: { color: 'processing', text: 'Connecting' },
  open: { color: 'success', text: 'Connected' },
  reconnecting: { color: 'warning', text: 'Reconnecting' },
  closed: { color: 'error', text: 'Disconnected' },
};

interface RoiRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

interface RoiProperty {
  value: number;
  min: number;
  max: number;
  step: number;
}

interface RoiModel {
  scale: number;
  available: boolean;
  width: RoiProperty | null;
  height: RoiProperty | null;
  offsetX: RoiProperty | null;
  offsetY: RoiProperty | null;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

/** Before submitting a numeric input: snap to the step grid, then clamp to [min, max]. */
function snapNumber(value: number, min: number, max: number, step: number): number {
  const raw = Number.isFinite(value) ? value : min;
  const snapped = min + Math.round((raw - min) / step) * step;
  return clamp(snapped, min, max);
}

/**
 * Slider display range: keep the full min..max for small ranges; for wide ones (e.g. exposure 30..30000000)
 * switch to a short window of +/-10% around the current value (at least 5 steps) so a huge span does not prevent fine control.
 */
function sliderBounds(
  value: number,
  min: number,
  max: number,
  step: number,
): { min: number; max: number } {
  const range = max - min;
  const effectiveStep = Math.max(step, 1);
  if (range <= 0) return { min, max };
  // The full range is acceptable when it has at most 200 steps or an absolute span of at most 10000.
  if (range / effectiveStep <= 200 || range <= 10000) return { min, max };
  const halfWindow = Math.max(Math.abs(value) * 0.1, effectiveStep * 5);
  return {
    min: snapNumber(value - halfWindow, min, max, effectiveStep),
    max: snapNumber(value + halfWindow, min, max, effectiveStep),
  };
}

function roiPropertyOf(prop?: CameraProperty): RoiProperty | null {
  if (!prop) return null;
  const min = prop.min ?? 0;
  const max = Math.max(min, prop.max ?? 100000);
  const step = Math.max(prop.step ?? 1, 1);
  const value = Number(prop.value);
  return {
    value: Number.isFinite(value) ? value : min,
    min,
    max,
    step,
  };
}

/** Clamp by the property's step/min/max (snap to the step grid first, then clamp to [min, max]). */
function snapRoi(value: number, prop: RoiProperty): number {
  const raw = Number.isFinite(value) ? value : prop.min;
  const snapped = prop.min + Math.round((raw - prop.min) / prop.step) * prop.step;
  return clamp(snapped, prop.min, prop.max);
}

function drawCrosshair(ctx: CanvasRenderingContext2D, w: number, h: number) {
  const cx = Math.round(w / 2);
  const cy = Math.round(h / 2);
  ctx.save();
  ctx.strokeStyle = 'rgba(0, 255, 140, 0.9)';
  ctx.lineWidth = 1;
  ctx.setLineDash([8, 5]);
  ctx.beginPath();
  ctx.moveTo(cx, 0);
  ctx.lineTo(cx, h);
  ctx.moveTo(0, cy);
  ctx.lineTo(w, cy);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.beginPath();
  ctx.arc(cx, cy, 14, 0, Math.PI * 2);
  ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(cx - 4, cy);
  ctx.lineTo(cx + 4, cy);
  ctx.moveTo(cx, cy - 4);
  ctx.lineTo(cx, cy + 4);
  ctx.stroke();
  ctx.restore();
}

/**
 * Preview overlay: crosshair + current ROI box (the preview canvas equals the current ROI x scale, so the overlay box is the canvas border)
 * + the translucent drag selection. Live frames are redrawn every frame, so the overlay is repainted after each drawImage.
 */
function drawPreviewOverlay(
  ctx: CanvasRenderingContext2D,
  w: number,
  h: number,
  selection: RoiRect | null,
) {
  if (w <= 0 || h <= 0) return;
  drawCrosshair(ctx, w, h);

  // Current ROI overlay box: dashed stroke inset by 1px so it is not clipped
  ctx.save();
  ctx.strokeStyle = 'rgba(255, 170, 40, 0.95)';
  ctx.lineWidth = 2;
  ctx.setLineDash([10, 6]);
  ctx.strokeRect(1, 1, Math.max(1, w - 2), Math.max(1, h - 2));
  ctx.setLineDash([]);
  ctx.restore();

  if (selection) {
    const x = clamp(selection.x, 0, w);
    const y = clamp(selection.y, 0, h);
    const rw = Math.min(selection.w, w - x);
    const rh = Math.min(selection.h, h - y);
    if (rw > 0 && rh > 0) {
      ctx.save();
      ctx.fillStyle = 'rgba(64, 150, 255, 0.3)';
      ctx.fillRect(x, y, rw, rh);
      ctx.strokeStyle = 'rgba(64, 150, 255, 0.95)';
      ctx.lineWidth = 1.5;
      ctx.strokeRect(x + 0.75, y + 0.75, Math.max(0, rw - 1.5), Math.max(0, rh - 1.5));
      ctx.restore();
    }
  }
}

function repaintCanvasOverlay(canvas: HTMLCanvasElement, selection: RoiRect | null): void {
  const ctx = canvas.getContext('2d');
  if (ctx) drawPreviewOverlay(ctx, canvas.width, canvas.height, selection);
}

/** Convert browser mouse coordinates to canvas pixel coordinates (canvas size = current ROI x scale). */
function canvasPointFromEvent(
  e: ReactMouseEvent<HTMLCanvasElement>,
  canvas: HTMLCanvasElement,
): { x: number; y: number } {
  const rect = canvas.getBoundingClientRect();
  const scaleX = rect.width > 0 ? canvas.width / rect.width : 0;
  const scaleY = rect.height > 0 ? canvas.height / rect.height : 0;
  return {
    x: scaleX > 0 ? clamp((e.clientX - rect.left) * scaleX, 0, canvas.width) : 0,
    y: scaleY > 0 ? clamp((e.clientY - rect.top) * scaleY, 0, canvas.height) : 0,
  };
}

function wsStatusTag(status: WsStatus) {
  const meta = statusMeta[status];
  return <Tag color={meta.color}>{meta.text}</Tag>;
}

function httpStatus(err: unknown): number | undefined {
  const response = (err as { response?: { status?: number } }).response;
  return response?.status;
}

function httpErrorMessage(err: unknown): string {
  const data = (err as { response?: { data?: { error?: string } } }).response?.data;
  if (data && typeof data === 'object' && 'error' in data && data.error) {
    return String(data.error);
  }
  if (err instanceof Error) return err.message;
  return 'Unknown error';
}

function useProperties(onActualChange?: () => void) {
  const [properties, setProperties] = useState<CameraProperty[]>([]);
  const [actuals, setActuals] = useState<Record<string, number | boolean>>({});
  const [loading, setLoading] = useState(true);
  // deviceOpen: false only after a 409 (device explicitly not open); true after a successful fetch.
  // Other errors (500/network) stay unknown, so a failed property read is not misreported as "device not open".
  const [deviceOpen, setDeviceOpen] = useState<boolean | null>(null);
  const [propertiesError, setPropertiesError] = useState<string | null>(null);
  // Request sequence: keep slow polling and manual refresh / initial load responses from overwriting newer state
  const fetchSeqRef = useRef(0);

  const refresh = useCallback(async () => {
    setLoading(true);
    const seq = ++fetchSeqRef.current;
    try {
      // Manual refresh and auto probe: a 409 for a closed device is expected, silent keeps the toast off and the page shows an inline Alert
      const data = await apiGet<PropertiesData>('/api/properties', { silent: true });
      if (seq === fetchSeqRef.current) {
        setProperties(data.properties);
        setDeviceOpen(true);
        setPropertiesError(null);
      }
    } catch (err) {
      if (seq === fetchSeqRef.current) {
        setProperties([]);
        if (httpStatus(err) === 409) {
          setDeviceOpen(false);
          setPropertiesError(null);
        } else {
          setPropertiesError(httpErrorMessage(err));
        }
      }
    } finally {
      setLoading(false);
    }
  }, []);

  // Low-frequency silent poll every 5s: leaves loading alone (so parameter cards do not flicker); 409 -> deviceOpen=false
  const pollProperties = useCallback(async () => {
    const seq = ++fetchSeqRef.current;
    try {
      const data = await apiGet<PropertiesData>('/api/properties', { silent: true });
      if (seq === fetchSeqRef.current) {
        setProperties(data.properties);
        setDeviceOpen(true);
        setPropertiesError(null);
      }
    } catch (err) {
      if (seq === fetchSeqRef.current) {
        setProperties([]);
        if (httpStatus(err) === 409) {
          setDeviceOpen(false);
          setPropertiesError(null);
        } else {
          setPropertiesError(httpErrorMessage(err));
        }
      }
    }
  }, []);

  // Initial load is an inline promise chain (loading starts true) so the effect never calls a refresh that setStates synchronously
  useEffect(() => {
    let cancelled = false;
    const seq = ++fetchSeqRef.current;
    apiGet<PropertiesData>('/api/properties', { silent: true })
      .then((data) => {
        if (!cancelled && seq === fetchSeqRef.current) {
          setProperties(data.properties);
          setDeviceOpen(true);
          setPropertiesError(null);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled && seq === fetchSeqRef.current) {
          setProperties([]);
          if (httpStatus(err) === 409) {
            setDeviceOpen(false);
            setPropertiesError(null);
          } else {
            setPropertiesError(httpErrorMessage(err));
          }
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const putProperty = useCallback(
    async (name: string, value: number | boolean): Promise<PropertyPutResult | undefined> => {
      const result = await apiPut<PropertyPutResult>(`/api/properties/${name}`, { value });
      setProperties((prev) =>
        prev.map((p) => (p.name === name ? { ...p, value: result.actual } : p)),
      );
      setActuals((prev) => ({ ...prev, [name]: result.actual }));
      onActualChange?.();
      return result;
    },
    [onActualChange],
  );

  return {
    properties,
    actuals,
    loading,
    deviceOpen,
    propertiesError,
    refresh,
    pollProperties,
    putProperty,
  };
}

function NumericPropertyRow({
  prop,
  actual,
  onPut,
}: {
  prop: CameraProperty;
  actual?: number | boolean;
  onPut: (name: string, value: number) => Promise<PropertyPutResult | undefined>;
}) {
  const [value, setValue] = useState<number>(() => Number(prop.value));
  const [editing, setEditing] = useState(false);

  const min = prop.min ?? 0;
  const max = prop.max ?? 100000;
  const step = prop.step ?? 1;
  const readOnly = Boolean(prop.readonly);
  // Exposure uses a logarithmic slider to squeeze the 30us..30s span into a finely controllable track.
  const isLogScale = prop.name === 'exposure';
  const { min: sliderMin, max: sliderMax } = sliderBounds(value, min, max, step);
  // Remember the last value synced from the prop; local edits/drags never overwrite the pending value.
  const lastPropValueRef = useRef(Number(prop.value));

  // An external prop change syncs into the local UI only when not editing, so polling or a backend write-back cannot interrupt typing.
  useEffect(() => {
    const next = Number(prop.value);
    if (!editing && Number.isFinite(next) && next !== lastPropValueRef.current) {
      setValue(next);
    }
    lastPropValueRef.current = next;
  }, [editing, prop.value]);

  const apply = useCallback(
    async (v: number) => {
      if (!Number.isFinite(v) || readOnly) return;
      // The log slider step is a UI display increment only; writes do not snap to a large UI step like 29999.97,
      // they are clamped by min/max only; the backend then applies the camera's real increment.
      const clamped = isLogScale
        ? Math.min(max, Math.max(min, v))
        : snapNumber(v, min, max, step);
      setValue(clamped);
      setEditing(false);
      // While a write is in flight the effect must not overwrite the local pending value with an old prop.
      lastPropValueRef.current = Number(prop.value);
      try {
        const result = await onPut(prop.name, clamped);
        const actualNum = result ? Number(result.actual) : NaN;
        if (Number.isFinite(actualNum)) setValue(actualNum);
      } catch {
        setValue(Number(prop.value));
      }
    },
    [min, max, step, readOnly, isLogScale, onPut, prop.name, prop.value],
  );

  const logMin = isLogScale ? Math.log10(Math.max(min, 1e-9)) : 0;
  const logMax = isLogScale ? Math.log10(max) : 1;
  const logStep = isLogScale ? (logMax - logMin) / 200 : 0.01;
  const logValue = isLogScale ? Math.log10(Math.max(value, 1e-9)) : 0;

  return (
    <Row gutter={16} align="middle" style={{ marginBottom: 8 }}>
      <Col xs={24} md={5}>
        <Space>
          <Typography.Text strong>{prop.label}</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {prop.name}
          </Typography.Text>
        </Space>
        <div>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            Range {min}–{max}
            {prop.unit ? ` ${prop.unit}` : ''}
            {actual !== undefined ? ` · Actual ${String(actual)}` : ''}
            {isLogScale
              ? ' · Log scale'
              : sliderMin !== min || sliderMax !== max
                ? ` · Slider ${sliderMin}–${sliderMax}`
                : ''}
          </Typography.Text>
        </div>
      </Col>
      <Col xs={24} md={13}>
        {isLogScale ? (
          <Slider
            min={logMin}
            max={logMax}
            step={logStep}
            value={logValue}
            disabled={readOnly}
            tooltip={{
              formatter: (v: number | undefined) =>
                v === undefined ? '' : String(Math.round(10 ** v)),
            }}
            onChange={(v) => {
              setEditing(true);
              setValue(10 ** v);
            }}
            onAfterChange={(v) => void apply(10 ** v)}
          />
        ) : (
          <Slider
            min={sliderMin}
            max={sliderMax}
            step={step}
            value={value}
            disabled={readOnly}
            onChange={(v) => {
              setEditing(true);
              setValue(v);
            }}
            onAfterChange={(v) => void apply(v)}
          />
        )}
      </Col>
      <Col xs={24} md={6}>
        <InputNumber
          min={min}
          max={max}
          step={step}
          value={value}
          addonAfter={prop.unit}
          disabled={readOnly}
          style={{ width: '100%' }}
          onChange={(v) => {
            if (v !== null && Number.isFinite(v)) {
              setEditing(true);
              setValue(v);
            }
          }}
          onFocus={() => setEditing(true)}
          onBlur={() => void apply(value)}
          onPressEnter={(e) => e.currentTarget.blur()}
        />
      </Col>
    </Row>
  );
}

function BoolPropertyRow({
  prop,
  onPut,
  compact,
}: {
  prop: CameraProperty;
  onPut: (name: string, value: boolean) => Promise<PropertyPutResult | undefined>;
  compact?: boolean;
}) {
  // External value changes remount via the parent's key={name:value}, which keeps setState out of the effect
  const [value, setValue] = useState<boolean>(() => Boolean(prop.value));

  const apply = async (v: boolean) => {
    setValue(v);
    try {
      await onPut(prop.name, v);
    } catch {
      setValue(Boolean(prop.value));
    }
  };

  if (compact) {
    return (
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 8,
          border: '1px solid rgba(128,128,128,0.35)',
          borderRadius: 6,
          padding: '4px 8px',
          height: '100%',
        }}
      >
        <div>
          <Typography.Text strong style={{ fontSize: 12 }}>
            {prop.label}
          </Typography.Text>
          <div>
            <Typography.Text type="secondary" style={{ fontSize: 11 }}>
              {prop.name}
            </Typography.Text>
          </div>
        </div>
        <Switch size="small" checked={value} onChange={(v) => void apply(v)} />
      </div>
    );
  }

  return (
    <Row justify="space-between" align="middle" style={{ marginBottom: 8 }}>
      <Col>
        <Space>
          <Typography.Text strong>{prop.label}</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {prop.name}
          </Typography.Text>
        </Space>
      </Col>
      <Col>
        <Switch checked={value} onChange={(v) => void apply(v)} />
      </Col>
    </Row>
  );
}

function RoiNumberInput({
  prop,
  disabled,
  onPut,
}: {
  prop: CameraProperty;
  disabled?: boolean;
  onPut: (name: string, value: number) => Promise<PropertyPutResult | undefined>;
}) {
  // External value changes remount via the parent's key={name:value}, which keeps setState out of the effect
  const [value, setValue] = useState<number | null>(() => Number(prop.value));
  // Bumped by blur/Enter only; the commit waits for InputNumber to re-parse and clamp the typed text first
  const [commitTick, setCommitTick] = useState(0);
  // Value already being written, so a blur followed by Enter cannot write the same number twice
  const inFlightRef = useRef<number | null>(null);

  // Typing only edits the local value: the camera is written once, when the field is committed.
  useEffect(() => {
    if (commitTick === 0) return;
    const current = Number(prop.value);
    if (value === null || !Number.isFinite(value)) {
      setValue(current);
      return;
    }
    if (disabled || value === current || value === inFlightRef.current) return;
    inFlightRef.current = value;
    void onPut(prop.name, value)
      .catch(() => {
        // Keep the old rollback semantics: a failed write shows the camera value again
        setValue(current);
      })
      .finally(() => {
        if (inFlightRef.current === value) inFlightRef.current = null;
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [commitTick]);

  return (
    <div>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {prop.label} ({prop.name})
      </Typography.Text>
      <InputNumber
        min={prop.min ?? 0}
        max={prop.max ?? 1920}
        step={prop.step ?? 1}
        value={value}
        disabled={disabled}
        style={{ width: '100%', marginTop: 4 }}
        onChange={(v) => setValue(v)}
        onBlur={() => setCommitTick((n) => n + 1)}
        onPressEnter={() => setCommitTick((n) => n + 1)}
      />
    </div>
  );
}

export default function LivePage() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [stats, setStats] = useState<LiveStats | null>(null);
  const {
    streamStarted,
    acquisitionPending,
    startAcquisition,
    stopAcquisition,
  } = useAcquisition();
  // Every mount of the Live page reads the latest preview settings (a change on the Settings page applies as soon as /live opens).
  const preview = useMemo(() => getPreviewSettings(), []);
  const livePreviewInfoShownRef = useRef(false);
  useEffect(() => {
    if (livePreviewInfoShownRef.current) return;
    livePreviewInfoShownRef.current = true;
    message.info({
      key: 'live-preview-info',
      content: (
        <>
          <div>Live Preview</div>
          <div style={{ fontSize: 12, opacity: 0.75 }}>
            The preview is an 8-bit grayscale auto-contrast image (1%–99% percentile linear stretch) and does not modify camera acquisition parameters.
          </div>
        </>
      ),
      duration: 6,
    });
  }, []);
  // Centered warning on the rising edge of streamStarted (including a mount while it is already streaming); silent on stop.
  const streamingWarningShownRef = useRef(false);
  useEffect(() => {
    if (!streamStarted) {
      streamingWarningShownRef.current = false;
      return;
    }
    if (streamingWarningShownRef.current) return;
    streamingWarningShownRef.current = true;
    message.warning({
      key: 'live-streaming-warning',
      content: 'Acquisition is streaming — stop acquisition before changing the capture region.',
    });
  }, [streamStarted]);
  // The ROI is locked while acquisition is starting or running so a write during the start transition is not rejected.
  const acquisitionLocked = streamStarted || acquisitionPending;
  const {
    properties,
    actuals,
    loading: propsLoading,
    deviceOpen,
    propertiesError,
    refresh,
    pollProperties,
    putProperty,
  } = useProperties();

  // Low-frequency silent poll of /api/properties: when the device is closed externally while the page runs (409), deviceWarning flips false->true
  useEffect(() => {
    const timer = window.setInterval(() => {
      void pollProperties();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [pollProperties]);

  const [snapshotFormat, setSnapshotFormat] = useState<'fits' | 'tiff' | 'png'>('fits');
  const [snapshot, setSnapshot] = useState<SnapshotResult | null>(null);
  const [snapshotBusy, setSnapshotBusy] = useState(false);
  const [folderPicking, setFolderPicking] = useState(false);
  // Selected snapshot folder (absolute path). localStorage remembers the last choice; without one (null/empty) use data_root + /snapshots from /api/settings.
  const [snapshotFolder, setSnapshotFolder] = useState<string | null>(
    () => localStorage.getItem(SNAPSHOT_FOLDER_KEY) || null,
  );
  // Filename prefix (may be empty). localStorage remembers the last input.
  const [snapshotPrefix, setSnapshotPrefix] = useState<string>(
    () => localStorage.getItem(SNAPSHOT_PREFIX_KEY) ?? '',
  );
  // Snapshot comment (optional): sent with POST /api/snapshot and written into the file header (FITS COMMENT / TIFF ImageDescription)
  const [snapshotComment, setSnapshotComment] = useState('');
  // ROI drag state
  const [dragging, setDragging] = useState(false);
  const [roiBusy, setRoiBusy] = useState(false);
  const roiBusyRef = useRef(false);
  const dragStartRef = useRef<{ x: number; y: number } | null>(null);
  const dragRectRef = useRef<RoiRect | null>(null);

  const setRoiBusyState = useCallback((next: boolean) => {
    roiBusyRef.current = next;
    setRoiBusy(next);
  }, []);

  const propByName = useMemo(() => {
    const map: Record<string, CameraProperty> = {};
    for (const p of properties) map[p.name] = p;
    return map;
  }, [properties]);

  const numericProps = useMemo(
    () => ['exposure', 'gain', 'black_level'].flatMap((name) => {
      const prop = propByName[name];
      return prop ? [prop] : [];
    }),
    [propByName],
  );
  // Contract v1.3: the trigger-mode switch was removed from the Live page (the backend property stays as is)
  const boolProps = useMemo(
    () => ['reverse_x', 'reverse_y', 'auto_exposure', 'auto_gain'].flatMap((name) => {
      const prop = propByName[name];
      return prop ? [prop] : [];
    }),
    [propByName],
  );
  const roiProps = useMemo(
    () => ROI_PROP_NAMES.flatMap((name) => {
      const prop = propByName[name];
      return prop ? [prop] : [];
    }),
    [propByName],
  );

  const roiModel = useMemo<RoiModel>(() => {
    const width = roiPropertyOf(propByName.width);
    const height = roiPropertyOf(propByName.height);
    const offsetX = roiPropertyOf(propByName.offset_x);
    const offsetY = roiPropertyOf(propByName.offset_y);
    return {
      scale: preview.scale > 0 ? preview.scale : 1,
      available: Boolean(width && height && offsetX && offsetY),
      width,
      height,
      offsetX,
      offsetY,
    };
  }, [propByName, preview.scale]);

  // Frame drawing and drag callbacks read the latest ROI and scale from refs, so the WebSocket callback is not rebuilt per frame
  const roiModelRef = useRef(roiModel);
  useEffect(() => {
    roiModelRef.current = roiModel;
  }, [roiModel]);

  const drawFrame = useCallback(async (blob: Blob) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    try {
      if ('createImageBitmap' in window) {
        const bitmap = await createImageBitmap(blob);
        canvas.width = bitmap.width;
        canvas.height = bitmap.height;
        const ctx = canvas.getContext('2d');
        if (ctx) {
          ctx.drawImage(bitmap, 0, 0);
          repaintCanvasOverlay(canvas, dragRectRef.current);
        }
        bitmap.close();
      } else {
        const url = URL.createObjectURL(blob);
        const img = new Image();
        img.onload = () => {
          canvas.width = img.naturalWidth;
          canvas.height = img.naturalHeight;
          const ctx = canvas.getContext('2d');
          if (ctx) {
            ctx.drawImage(img, 0, 0);
            repaintCanvasOverlay(canvas, dragRectRef.current);
          }
          URL.revokeObjectURL(url);
        };
        img.src = url;
      }
    } catch {
      /* Ignore a single frame decode error */
    }
  }, []);

  const handleMessage = useCallback(
    (ev: MessageEvent) => {
      if (typeof ev.data === 'string') {
        try {
          const parsed = JSON.parse(ev.data) as LiveStats;
          if (Array.isArray(parsed.histogram) && parsed.histogram.length === 256) {
            setStats(parsed);
          }
        } catch {
          /* Ignore non-JSON text */
        }
      } else if (ev.data instanceof Blob) {
        void drawFrame(ev.data);
      }
    },
    [drawFrame],
  );

  const wsStatus = useReconnectingWebSocket(WS_LIVE, {
    enabled: true,
    onMessage: handleMessage,
  });

  // Start the live preview on mount (unless acquisition is already running globally) and stop it on unmount.
  // start/stop now live in the global AcquisitionProvider and are still serialized by a module-level queue.
  useEffect(() => {
    if (!streamStarted && !acquisitionPending) {
      void startAcquisition();
    }
    return () => {
      void stopAcquisition();
    };
    // Runs on mount/unmount only; acquisition state changes are owned by the global Provider.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // v1.4 default snapshot folder: localStorage first; without a saved value GET /api/settings and use data_root + /snapshots.
  useEffect(() => {
    if (localStorage.getItem(SNAPSHOT_FOLDER_KEY)) return;
    let cancelled = false;
    apiGet<DataDirSettings>('/api/settings', { silent: true })
      .then((data) => {
        if (!cancelled) setSnapshotFolder(defaultSnapshotFolder(data.data_root));
      })
      .catch(() => {
        // Backend unavailable or load failed: keep it empty; when POST /api/snapshot omits folder the backend falls back to data_root/snapshots
        if (!cancelled) setSnapshotFolder('');
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const pickSnapshotFolder = async () => {
    setFolderPicking(true);
    try {
      const result = await apiPost<PickFolderResult>(
        '/api/pick-folder',
        { title: 'Select Snapshot Folder' },
        { silent: true },
      );
      if (result.path) {
        setSnapshotFolder(result.path);
        localStorage.setItem(SNAPSHOT_FOLDER_KEY, result.path);
      } else {
        message.warning('Folder selection cancelled');
      }
    } catch (err) {
      message.error(err instanceof Error ? err.message : 'Unable to open native folder picker');
    } finally {
      setFolderPicking(false);
    }
  };

  const takeSnapshot = async () => {
    setSnapshotBusy(true);
    try {
      const result = await apiPost<SnapshotResult>('/api/snapshot', {
        format: snapshotFormat,
        folder: snapshotFolder || '',
        prefix: snapshotPrefix,
        comment: snapshotComment,
      });
      setSnapshot(result);
      // v1.4 returns folder as an absolute path and prefix sanitized; both are stored in localStorage
      const savedFolder = typeof result.folder === 'string' ? result.folder : snapshotFolder || '';
      const savedPrefix = typeof result.prefix === 'string' ? result.prefix : snapshotPrefix;
      setSnapshotFolder(savedFolder);
      localStorage.setItem(SNAPSHOT_FOLDER_KEY, savedFolder);
      setSnapshotPrefix(savedPrefix);
      localStorage.setItem(SNAPSHOT_PREFIX_KEY, savedPrefix);
    } catch {
      /* The API client already showed the error */
    } finally {
      setSnapshotBusy(false);
    }
  };

  // Expected snapshot path (UI hint only): <folder>/[<prefix>_]YYYYMMDDTHH-MM-SS.<ext>, matching the backend now_stamp format.
  const expectedSnapshotPath = useMemo(() => {
    const folder =
      snapshotFolder && snapshotFolder.trim() !== '' ? snapshotFolder : 'data_root/snapshots';
    const ts = dayjs().format('YYYYMMDD[T]HH-mm-ss');
    const name = snapshotPrefix ? `${snapshotPrefix}_${ts}.${snapshotFormat}` : `${ts}.${snapshotFormat}`;
    return `${folder}/${name}`;
  }, [snapshotFolder, snapshotPrefix, snapshotFormat]);

  // ------------------------------------------------------------------ ROI drag selection
  const startCanvasDrag = useCallback((e: ReactMouseEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas || !roiModelRef.current.available || roiBusyRef.current || acquisitionLocked) return;
    const point = canvasPointFromEvent(e, canvas);
    dragStartRef.current = { x: point.x, y: point.y };
    dragRectRef.current = { x: point.x, y: point.y, w: 0, h: 0 };
    setDragging(true);
  }, [acquisitionLocked]);

  const moveCanvasDrag = useCallback((e: ReactMouseEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    const start = dragStartRef.current;
    if (!canvas || !start) return;
    const point = canvasPointFromEvent(e, canvas);
    dragRectRef.current = {
      x: Math.min(start.x, point.x),
      y: Math.min(start.y, point.y),
      w: Math.abs(point.x - start.x),
      h: Math.abs(point.y - start.y),
    };
    // Repaint the selection box between frame refreshes to keep dragging responsive
    repaintCanvasOverlay(canvas, dragRectRef.current);
  }, []);

  /** Drag release: canvas coords -> full-frame coords = current offset + drag coords / scale; all four values are clamped by step/min/max before PUT. */
  const applyDragSelection = useCallback(
    async (rect: RoiRect) => {
      const roi = roiModelRef.current;
      if (
        !roi.available
        || !roi.width
        || !roi.height
        || !roi.offsetX
        || !roi.offsetY
        || roiBusyRef.current
        || acquisitionLocked
      ) return;

      const scale = roi.scale;
      const width = snapRoi(rect.w / scale, roi.width);
      const height = snapRoi(rect.h / scale, roi.height);
      // The effective offset limit follows the new width/height: full-frame max minus the new ROI size
      const offsetXMax = Math.max(roi.offsetX.min, roi.width.max - width);
      const offsetYMax = Math.max(roi.offsetY.min, roi.height.max - height);
      const offsetX = snapRoi(roi.offsetX.value + rect.x / scale, {
        ...roi.offsetX,
        max: offsetXMax,
      });
      const offsetY = snapRoi(roi.offsetY.value + rect.y / scale, {
        ...roi.offsetY,
        max: offsetYMax,
      });

      setRoiBusyState(true);
      try {
        // Write the size before the offset: the backend clamps offset on a size write, then the correct offset is written
        await putProperty('width', width);
        await putProperty('height', height);
        await putProperty('offset_x', offsetX);
        await putProperty('offset_y', offsetY);
        await refresh();
        message.success(`Capture region set to ${offsetX},${offsetY} ${width}×${height}`);
      } catch {
        /* The api client already reported the error */
      } finally {
        setRoiBusyState(false);
      }
    },
    [putProperty, refresh, setRoiBusyState, acquisitionLocked],
  );

  const finishCanvasDrag = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const start = dragStartRef.current;
    const rect = dragRectRef.current;
    dragStartRef.current = null;
    dragRectRef.current = null;
    setDragging(false);
    repaintCanvasOverlay(canvas, null);
    if (start && rect && rect.w >= 2 && rect.h >= 2) {
      void applyDragSelection(rect);
    }
  }, [applyDragSelection]);

  const resetRoi = useCallback(async () => {
    const roi = roiModelRef.current;
    if (
      !roi.available
      || !roi.width
      || !roi.height
      || !roi.offsetX
      || !roi.offsetY
      || roiBusyRef.current
      || acquisitionLocked
    ) return;
    setRoiBusyState(true);
    try {
      // Reset to the full frame: width/height = camera max, offset = 0
      await putProperty('width', roi.width.max);
      await putProperty('height', roi.height.max);
      await putProperty('offset_x', snapRoi(0, roi.offsetX));
      await putProperty('offset_y', snapRoi(0, roi.offsetY));
      await refresh();
      message.success('Capture region reset to full frame');
    } catch {
      /* The api client already reported the error */
    } finally {
      setRoiBusyState(false);
    }
  }, [putProperty, refresh, setRoiBusyState, acquisitionLocked]);

  const histogramData = useMemo(() => {
    if (!stats) return [];
    return stats.histogram.map((count, bin) => ({ bin, count }));
  }, [stats]);

  const roiWidthText = roiModel.width
    ? `${Math.round(roiModel.width.value)} × ${Math.round(roiModel.height?.value ?? 0)} px`
    : '—';
  const roiMaxText = roiModel.width
    ? `${Math.round(roiModel.width.max)} × ${Math.round(roiModel.height?.max ?? 0)} px`
    : '—';

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Row gutter={[16, 16]}>
        <Col xs={24} lg={16}>
          <Card
            size="small"
            title={
              <Space>
                Preview
                {wsStatusTag(wsStatus)}
                {streamStarted && <Tag color="green">streaming</Tag>}
              </Space>
            }
          >
            <div style={{ position: 'relative', background: '#000', minHeight: 320, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              <canvas
                ref={canvasRef}
                style={{
                  width: '100%',
                  height: 'auto',
                  display: 'block',
                  background: '#000',
                  cursor: acquisitionLocked ? 'not-allowed' : dragging ? 'grabbing' : roiModel.available ? 'crosshair' : 'default',
                  userSelect: 'none',
                }}
                onMouseDown={startCanvasDrag}
                onMouseMove={moveCanvasDrag}
                onMouseUp={finishCanvasDrag}
              />
              {streamStarted && !stats && wsStatus !== 'open' && (
                <Empty description="Waiting for video stream…" style={{ position: 'absolute', pointerEvents: 'none' }} />
              )}
              {!streamStarted && (
                <div
                  style={{
                    position: 'absolute',
                    inset: 0,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    background: 'rgba(0, 0, 0, 0.68)',
                    color: '#fff',
                    pointerEvents: 'none',
                    zIndex: 1,
                  }}
                >
                  <Space direction="vertical" align="center">
                    <Typography.Text style={{ fontSize: 16, fontWeight: 600 }}>
                      {acquisitionPending ? 'Starting acquisition…' : 'Acquisition stopped'}
                    </Typography.Text>
                    <Typography.Text type="secondary">
                      {acquisitionPending
                        ? 'The live preview is starting…'
                        : 'Turn on Acquisition to resume the live preview.'}
                    </Typography.Text>
                  </Space>
                </div>
              )}
            </div>
            {roiModel.available && (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {streamStarted
                  ? 'Stop acquisition before editing the capture region.'
                  : acquisitionPending
                    ? 'Starting acquisition — capture region editing is temporarily locked.'
                    : 'Drag on the image to select the capture region; releasing the mouse converts the selection by the preview scale and writes it to the camera.'}
              </Typography.Text>
            )}
          </Card>
        </Col>

        <Col xs={24} lg={8}>
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <Card size="small" title="Live Stats">
              {streamStarted && stats ? (
                <>
                  <Row gutter={[12, 12]}>
                    <Col span={8}>
                      <Statistic title="min" value={stats.min} precision={0} />
                    </Col>
                    <Col span={8}>
                      <Statistic title="max" value={stats.max} precision={0} />
                    </Col>
                    <Col span={8}>
                      <Statistic title="mean" value={stats.mean} precision={1} />
                    </Col>
                    <Col span={8}>
                      <Statistic title="std" value={stats.std} precision={1} />
                    </Col>
                    <Col span={8}>
                      <Statistic title="Saturated %" value={stats.saturated_pct} precision={2} suffix="%" />
                    </Col>
                    <Col span={8}>
                      <Statistic title="FPS" value={stats.fps} precision={1} />
                    </Col>
                  </Row>
                  <Divider style={{ margin: '12px 0' }}>Histogram (raw 12-bit · 256 bins · adaptive ADU range)</Divider>
                  <div style={{ height: 180 }}>
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart data={histogramData} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
                        <CartesianGrid strokeDasharray="3 3" stroke="#333" vertical={false} />
                        <XAxis dataKey="bin" hide />
                        <YAxis hide />
                        <Tooltip
                          formatter={(value: number) => [`${value} pixels`, 'count']}
                          labelFormatter={(bin) => `bin ${bin}`}
                        />
                        <Bar dataKey="count" fill="#4dabf7" isAnimationActive={false} />
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                  {typeof stats.histogram_min === 'number' && typeof stats.histogram_max === 'number' && (
                    <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 4 }}>
                      ADU range: {stats.histogram_min}–{stats.histogram_max} (autoscaled)
                    </Typography.Text>
                  )}
                </>
              ) : (
                <Empty
                  description={
                    streamStarted
                      ? 'Waiting for live statistics…'
                      : acquisitionPending
                        ? 'Starting acquisition…'
                        : 'Acquisition stopped — no live statistics'
                  }
                />
              )}
            </Card>

            <Card size="small" title="Snapshot (Single Frame)">
              <Space direction="vertical" size="middle" style={{ width: '100%' }}>
                <div>
                  <div style={{ display: 'flex', gap: 8 }}>
                    <Button
                      icon={<FolderOpenOutlined />}
                      loading={folderPicking}
                      onClick={() => void pickSnapshotFolder()}
                    >
                      Choose Folder…
                    </Button>
                    <Input
                      value={snapshotPrefix}
                      onChange={(e) => setSnapshotPrefix(e.target.value)}
                      onBlur={() => localStorage.setItem(SNAPSHOT_PREFIX_KEY, snapshotPrefix)}
                      onPressEnter={(e) => e.currentTarget.blur()}
                      placeholder="Prefix (optional)"
                      allowClear
                      maxLength={128}
                      style={{ flex: 1, minWidth: 0 }}
                    />
                  </div>
                  <div style={{ marginTop: 4 }}>
                    <Typography.Text type="secondary" style={{ fontSize: 12, wordBreak: 'break-all' }}>
                      Will save to: {expectedSnapshotPath}
                    </Typography.Text>
                  </div>
                </div>
                <div>
                  <Input
                    value={snapshotComment}
                    onChange={(e) => setSnapshotComment(e.target.value)}
                    placeholder="Comment (optional, written into file header)"
                    allowClear
                    maxLength={256}
                  />
                  <div style={{ marginTop: 4 }}>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      Written into the file header: FITS COMMENT cards / TIFF ImageDescription (UTF-8, Chinese supported)
                    </Typography.Text>
                  </div>
                </div>
                <Space wrap>
                  <Select
                    value={snapshotFormat}
                    onChange={setSnapshotFormat}
                    options={[
                      { value: 'fits', label: 'FITS (16-bit raw)' },
                      { value: 'tiff', label: 'TIFF (12-bit)' },
                      { value: 'png', label: 'PNG (8-bit auto-contrast)' },
                    ]}
                    style={{ width: 220 }}
                  />
                  <Button
                    type="primary"
                    icon={<CameraOutlined />}
                    loading={snapshotBusy}
                    onClick={() => void takeSnapshot()}
                  >
                    Snapshot
                  </Button>
                </Space>
                {snapshot && (
                  <Descriptions size="small" column={1}>
                    <Descriptions.Item label="File Name">{snapshot.file}</Descriptions.Item>
                    <Descriptions.Item label="Folder">
                      {snapshot.folder || '—'}
                    </Descriptions.Item>
                    <Descriptions.Item label="Download">
                      <a href={resolveUrl(snapshot.url)} target="_blank" rel="noreferrer">
                        {snapshot.url} <LinkOutlined />
                      </a>
                    </Descriptions.Item>
                  </Descriptions>
                )}
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  Snapshots are saved to the selected folder; file names always include a timestamp and the URL is returned by the backend.
                </Typography.Text>
              </Space>
            </Card>
          </Space>
        </Col>
      </Row>

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={16}>
          <Card
            size="small"
            title={
              <Space>
                Camera Parameters
                <Typography.Text type="secondary" style={{ fontWeight: 'normal', fontSize: 12 }}>
                  Sliders and switches apply immediately; typed values apply on blur or Enter, and the returned actual value is shown
                </Typography.Text>
              </Space>
            }
            loading={propsLoading}
          >
            {properties.length === 0 && !propsLoading ? (
              propertiesError ? (
                <Alert type="error" showIcon message="Failed to fetch camera properties" description={propertiesError} />
              ) : deviceOpen === false ? (
                <Alert type="warning" showIcon message="No device open, cannot fetch properties" description='Please open a device on the "Connect" page first (GET /api/properties returns 409 when no device is open).' />
              ) : (
                <Alert type="info" showIcon message="Camera is open but reports no parameters" description="The camera did not expose any of the supported parameters. Check the backend log for skipped property warnings." />
              )
            ) : (
              <>
                {numericProps.map((p) => (
                  <NumericPropertyRow
                    key={p.name}
                    prop={p}
                    actual={actuals[p.name]}
                    onPut={putProperty}
                  />
                ))}
                <Divider style={{ margin: '12px 0' }} />
                <Row gutter={[12, 8]}>
                  {boolProps.map((p) => (
                    <Col xs={12} sm={8} md={6} key={`${p.name}:${String(p.value)}`}>
                      <BoolPropertyRow compact prop={p} onPut={putProperty} />
                    </Col>
                  ))}
                </Row>
              </>
            )}
          </Card>
        </Col>

        <Col xs={24} lg={8}>
          <Card
            size="small"
            title="Capture Region"
            loading={propsLoading}
            extra={
              <Tag color={streamStarted ? 'red' : acquisitionPending ? 'orange' : 'default'}>
                On Air
              </Tag>
            }
          >
            {roiProps.length === 0 && !propsLoading ? (
              propertiesError ? (
                <Typography.Text type="danger">
                  Unable to fetch camera properties: {propertiesError}
                </Typography.Text>
              ) : deviceOpen === false ? (
                <Typography.Text type="secondary">
                  Open a device on the “Connect” page first to see capture region properties.
                </Typography.Text>
              ) : (
                <Typography.Text type="secondary">
                  The current camera does not report capture region properties (the mock driver supports at least width/height).
                </Typography.Text>
              )
            ) : (
              <Space direction="vertical" size="middle" style={{ width: '100%' }}>
                <div>
                  <Typography.Text>Current region: {roiWidthText}</Typography.Text>
                  <br />
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    Full-frame maximum: {roiMaxText} · Two-way sync with preview drag
                  </Typography.Text>
                </div>
                <Row gutter={[12, 12]}>
                  {roiProps.map((p) => (
                    <Col xs={12} key={p.name}>
                      <RoiNumberInput
                        key={`${p.name}:${String(p.value)}`}
                        prop={p}
                        disabled={roiBusy || acquisitionLocked}
                        onPut={putProperty}
                      />
                    </Col>
                  ))}
                </Row>
                <Button
                  block
                  onClick={() => void resetRoi()}
                  loading={roiBusy}
                  disabled={acquisitionLocked}
                >
                  Reset to Full Frame
                </Button>
              </Space>
            )}
          </Card>
        </Col>
      </Row>
    </Space>
  );
}
