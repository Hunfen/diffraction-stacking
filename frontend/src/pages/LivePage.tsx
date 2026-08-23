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
/** 统计饱和比例告警阈值（%）。超过后视为警告，用于顶部提示自动复显。 */
const SATURATION_WARN_THRESHOLD_PCT = 1.0;
const ROI_PROP_NAMES = ['width', 'height', 'offset_x', 'offset_y'] as const;

/**
 * 串行化 /api/live/start|stop 生命周期。
 * 快速离开/重新进入 /live 时，cleanup 的 stop 与下一次 mount 的 start 会进入同一个
 * 全局队列按序执行，避免 start 先到、迟到的 stop 再把新流停掉。
 */
let liveLifecycle: Promise<void> = Promise.resolve();
function runLiveAction(action: () => Promise<void>): Promise<void> {
  const next = liveLifecycle.then(action, action);
  liveLifecycle = next.catch(() => undefined);
  return next;
}

/** data_root → 默认快照目录 <data_root>/snapshots。 */
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

/** 按属性的 step/min/max 钳制（先吸附到 step 网格，再夹到 [min, max]）。 */
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
 * 预览覆盖层：十字准星 + 当前 ROI 框（预览画布即当前 ROI×scale，因此覆盖框为画布内边框）
 * + 拖选中的半透明选框。实时帧每帧重绘，因此每次 drawImage 后都要重画覆盖层。
 */
function drawPreviewOverlay(
  ctx: CanvasRenderingContext2D,
  w: number,
  h: number,
  selection: RoiRect | null,
) {
  if (w <= 0 || h <= 0) return;
  drawCrosshair(ctx, w, h);

  // 当前 ROI 覆盖框：虚线描边内缩 1px，避免被裁切
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

/** 将浏览器鼠标坐标换算到 canvas 内部像素坐标（canvas 内部尺寸 = 当前 ROI × scale）。 */
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

function useProperties(onActualChange?: () => void) {
  const [properties, setProperties] = useState<CameraProperty[]>([]);
  const [actuals, setActuals] = useState<Record<string, number | boolean>>({});
  const [loading, setLoading] = useState(true);
  // 请求序号：防止低频轮询与手动 refresh / 初始加载的旧响应覆盖新状态
  const fetchSeqRef = useRef(0);

  const refresh = useCallback(async () => {
    setLoading(true);
    const seq = ++fetchSeqRef.current;
    try {
      // 手动刷新/自动探测：未打开设备返回 409 属预期，silent 不弹 toast，页面用内联 Alert 提示
      const data = await apiGet<PropertiesData>('/api/properties', { silent: true });
      if (seq === fetchSeqRef.current) setProperties(data.properties);
    } catch {
      if (seq === fetchSeqRef.current) setProperties([]);
    } finally {
      setLoading(false);
    }
  }, []);

  // 每 5s 的低频静默轮询：不碰 loading（避免参数卡频繁闪烁）；409/失败 → properties 置空 → deviceWarning
  const pollProperties = useCallback(async () => {
    const seq = ++fetchSeqRef.current;
    try {
      const data = await apiGet<PropertiesData>('/api/properties', { silent: true });
      if (seq === fetchSeqRef.current) setProperties(data.properties);
    } catch {
      if (seq === fetchSeqRef.current) setProperties([]);
    }
  }, []);

  // 初始加载内联成 promise 链（loading 初值即 true），不在 effect 里同步调用会 setState 的刷新函数
  useEffect(() => {
    let cancelled = false;
    const seq = ++fetchSeqRef.current;
    apiGet<PropertiesData>('/api/properties', { silent: true })
      .then((data) => {
        if (!cancelled && seq === fetchSeqRef.current) setProperties(data.properties);
      })
      .catch(() => {
        if (!cancelled && seq === fetchSeqRef.current) setProperties([]);
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

  return { properties, actuals, loading, refresh, pollProperties, putProperty };
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
  // 外部值变化由父组件 key={name:value} 触发重挂载同步，避免 effect 内 setState
  const [value, setValue] = useState<number>(() => Number(prop.value));

  const min = prop.min ?? 0;
  const max = prop.max ?? 100000;
  const step = prop.step ?? 1;

  const apply = async (v: number) => {
    if (!Number.isFinite(v)) return;
    const clamped = Math.min(max, Math.max(min, v));
    setValue(clamped);
    try {
      await onPut(prop.name, clamped);
    } catch {
      setValue(Number(prop.value));
    }
  };

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
          </Typography.Text>
        </div>
      </Col>
      <Col xs={24} md={13}>
        <Slider
          min={min}
          max={max}
          step={step}
          value={value}
          onChange={(v) => setValue(v)}
          onAfterChange={(v) => void apply(v)}
        />
      </Col>
      <Col xs={24} md={6}>
        <InputNumber
          min={min}
          max={max}
          step={step}
          value={value}
          addonAfter={prop.unit}
          style={{ width: '100%' }}
          onChange={(v) => {
            if (v !== null && Number.isFinite(v)) void apply(v);
          }}
        />
      </Col>
    </Row>
  );
}

function BoolPropertyRow({ prop, onPut }: { prop: CameraProperty; onPut: (name: string, value: boolean) => Promise<PropertyPutResult | undefined> }) {
  // 外部值变化由父组件 key={name:value} 触发重挂载同步，避免 effect 内 setState
  const [value, setValue] = useState<boolean>(() => Boolean(prop.value));

  const apply = async (v: boolean) => {
    setValue(v);
    try {
      await onPut(prop.name, v);
    } catch {
      setValue(Boolean(prop.value));
    }
  };

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
  // 外部值变化由父组件 key={name:value} 触发重挂载同步，避免 effect 内 setState
  const [value, setValue] = useState<number>(() => Number(prop.value));

  const apply = async (v: number | null) => {
    if (v === null || !Number.isFinite(v)) return;
    setValue(v);
    try {
      await onPut(prop.name, v);
    } catch {
      setValue(Number(prop.value));
    }
  };

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
        onChange={(v) => {
          if (v !== null) void apply(v);
        }}
      />
    </div>
  );
}

export default function LivePage() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [stats, setStats] = useState<LiveStats | null>(null);
  const [streamStarted, setStreamStarted] = useState(false);
  const preview = useMemo(() => getPreviewSettings(), []);
  const { properties, actuals, loading: propsLoading, refresh, pollProperties, putProperty } = useProperties();

  // 低频静默轮询 /api/properties：页面运行中设备被外部关闭时（409），驱动 deviceWarning 从 false→true
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
  // 已选快照目录（绝对路径）。localStorage 记住上次选择；无记忆（null/空串）时用 /api/settings 的 data_root + /snapshots。
  const [snapshotFolder, setSnapshotFolder] = useState<string | null>(
    () => localStorage.getItem(SNAPSHOT_FOLDER_KEY) || null,
  );
  // 文件名前缀（可为空）。localStorage 记住上次输入。
  const [snapshotPrefix, setSnapshotPrefix] = useState<string>(
    () => localStorage.getItem(SNAPSHOT_PREFIX_KEY) ?? '',
  );
  // 顶部提示：内存态 dismissed；出现任何警告时自动复显
  const [alertDismissed, setAlertDismissed] = useState(false);

  // ROI 拖选状态
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
  // 契约 v1.3：触发模式开关从 Live 页移除（后端属性保留不动）
  const boolProps = useMemo(
    () => ['reverse_x', 'reverse_y'].flatMap((name) => {
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

  // 帧绘制/拖选回调通过 ref 读取最新 ROI 与 scale，避免每帧重建 WebSocket 回调
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
      /* 忽略单帧解码错误 */
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
          /* 非 JSON 文本忽略 */
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

  // 挂载时启动实时预览，卸载时停止；自动触发用 silent（未开设备 409 不弹 toast，由页面状态提示）。
  // start/stop 通过模块级队列串行执行，快速离开/返回时不会出现“迟到的 stop 停掉新流”。
  useEffect(() => {
    let disposed = false;
    void runLiveAction(async () => {
      // 先确保没有残留流，再启动。
      await apiPost('/api/live/stop', undefined, { silent: true }).catch(() => undefined);
      if (disposed) return;
      await apiPost('/api/live/start', preview, { silent: true });
      if (!disposed) setStreamStarted(true);
    }).catch(() => {
      if (!disposed) setStreamStarted(false);
    });
    return () => {
      disposed = true;
      void runLiveAction(async () => {
        await apiPost('/api/live/stop', undefined, { silent: true }).catch(() => undefined);
      });
    };
  }, [preview]);

  // v1.4 默认快照目录：优先 localStorage；无记忆时 GET /api/settings 取 data_root + /snapshots。
  useEffect(() => {
    if (localStorage.getItem(SNAPSHOT_FOLDER_KEY)) return;
    let cancelled = false;
    apiGet<DataDirSettings>('/api/settings', { silent: true })
      .then((data) => {
        if (!cancelled) setSnapshotFolder(defaultSnapshotFolder(data.data_root));
      })
      .catch(() => {
        // 后端不可用/加载失败：保持空值，POST /api/snapshot 缺省 folder 时后端会落到 data_root/snapshots
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
      });
      setSnapshot(result);
      // v1.4 返回 folder 为绝对路径、prefix 为 sanitize 后的值；两者都写入 localStorage
      const savedFolder = typeof result.folder === 'string' ? result.folder : snapshotFolder || '';
      const savedPrefix = typeof result.prefix === 'string' ? result.prefix : snapshotPrefix;
      setSnapshotFolder(savedFolder);
      localStorage.setItem(SNAPSHOT_FOLDER_KEY, savedFolder);
      setSnapshotPrefix(savedPrefix);
      localStorage.setItem(SNAPSHOT_PREFIX_KEY, savedPrefix);
    } catch {
      /* API client 已弹出错误提示 */
    } finally {
      setSnapshotBusy(false);
    }
  };

  // ------------------------------------------------------------------ ROI 拖选
  const startCanvasDrag = useCallback((e: ReactMouseEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas || !roiModelRef.current.available || roiBusyRef.current) return;
    const point = canvasPointFromEvent(e, canvas);
    dragStartRef.current = { x: point.x, y: point.y };
    dragRectRef.current = { x: point.x, y: point.y, w: 0, h: 0 };
    setDragging(true);
  }, []);

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
    // 帧刷新间隙立即重绘选框，保证拖动手感
    repaintCanvasOverlay(canvas, dragRectRef.current);
  }, []);

  /** 拖选松手：canvas 坐标 → 全画幅坐标 = 当前 offset + 拖选坐标 / scale，四值按 step/min/max 钳制后 PUT。 */
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
      ) return;

      const scale = roi.scale;
      const width = snapRoi(rect.w / scale, roi.width);
      const height = snapRoi(rect.h / scale, roi.height);
      // offset 有效上限随新 width/height 变化：全画幅 max − 新 ROI 尺寸
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
        // 先写尺寸再写偏移：后端写尺寸后会自动钳制 offset，再写入正确偏移
        await putProperty('width', width);
        await putProperty('height', height);
        await putProperty('offset_x', offsetX);
        await putProperty('offset_y', offsetY);
        await refresh();
        message.success(`ROI set to ${offsetX},${offsetY} ${width}×${height}`);
      } catch {
        /* 错误已由 api client 统一提示 */
      } finally {
        setRoiBusyState(false);
      }
    },
    [putProperty, refresh, setRoiBusyState],
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
    ) return;
    setRoiBusyState(true);
    try {
      // 重置全画幅：width/height = 相机 max，offset = 0
      await putProperty('width', roi.width.max);
      await putProperty('height', roi.height.max);
      await putProperty('offset_x', snapRoi(0, roi.offsetX));
      await putProperty('offset_y', snapRoi(0, roi.offsetY));
      await refresh();
      message.success('ROI reset to full frame');
    } catch {
      /* 错误已由 api client 统一提示 */
    } finally {
      setRoiBusyState(false);
    }
  }, [putProperty, refresh, setRoiBusyState]);

  // ------------------------------------------------------------------ 顶部提示复显
  const deviceWarning = !propsLoading && properties.length === 0;
  const saturationWarning =
    stats !== null && stats.saturated_pct > SATURATION_WARN_THRESHOLD_PCT;

  // 任一警告来源从「无」变「有」时，顶部提示的 dismissed 状态复位（含其他警告已存在时新增警告，
  // 以及 WS 在 closed/reconnecting 两个警告态之间切换的情况）
  const prevWarningsRef = useRef({ deviceWarning, wsStatus, saturationWarning });
  useEffect(() => {
    const prev = prevWarningsRef.current;
    const warningAppeared =
      (deviceWarning && !prev.deviceWarning)
      || ((wsStatus === 'closed' || wsStatus === 'reconnecting') && prev.wsStatus !== wsStatus)
      || (saturationWarning && !prev.saturationWarning);
    if (warningAppeared) setAlertDismissed(false);
    prevWarningsRef.current = { deviceWarning, wsStatus, saturationWarning };
  }, [deviceWarning, wsStatus, saturationWarning]);

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
      {!alertDismissed && (
        <Alert
          type="info"
          showIcon
          closable
          message="Live Preview"
          description="The preview is an 8-bit grayscale auto-contrast image (1%–99% percentile linear stretch) and does not modify camera acquisition parameters."
          onClose={() => setAlertDismissed(true)}
        />
      )}

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
            <div style={{ background: '#000', minHeight: 320, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              <canvas
                ref={canvasRef}
                style={{
                  width: '100%',
                  height: 'auto',
                  display: 'block',
                  background: '#000',
                  cursor: dragging ? 'grabbing' : roiModel.available ? 'crosshair' : 'default',
                  userSelect: 'none',
                }}
                onMouseDown={startCanvasDrag}
                onMouseMove={moveCanvasDrag}
                onMouseUp={finishCanvasDrag}
              />
              {!stats && wsStatus !== 'open' && <Empty description="Waiting for video stream…" style={{ position: 'absolute', pointerEvents: 'none' }} />}
            </div>
            {roiModel.available && (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                Drag on the image to select the capture region; releasing the mouse converts the selection by the preview scale and writes it to the camera.
              </Typography.Text>
            )}
          </Card>
        </Col>

        <Col xs={24} lg={8}>
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <Card size="small" title="Live Stats">
              {stats ? (
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
                  <Divider style={{ margin: '12px 0' }}>Histogram (256 bins)</Divider>
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
                </>
              ) : (
                <Empty description="No statistics yet" />
              )}
            </Card>

            <Card size="small" title="Snapshot (Single Frame)">
              <Space direction="vertical" size="middle" style={{ width: '100%' }}>
                <div>
                  <Button
                    icon={<FolderOpenOutlined />}
                    loading={folderPicking}
                    onClick={() => void pickSnapshotFolder()}
                  >
                    Choose Folder…
                  </Button>
                  <div style={{ marginTop: 4 }}>
                    <Typography.Text type="secondary" style={{ fontSize: 12, wordBreak: 'break-all' }}>
                      {snapshotFolder === null
                        ? 'Loading default folder…'
                        : snapshotFolder || 'Default folder (data_root/snapshots)'}
                    </Typography.Text>
                  </div>
                </div>
                <div>
                  <Input
                    value={snapshotPrefix}
                    onChange={(e) => {
                      const next = e.target.value;
                      setSnapshotPrefix(next);
                      localStorage.setItem(SNAPSHOT_PREFIX_KEY, next);
                    }}
                    placeholder="Prefix (optional)"
                    allowClear
                    maxLength={128}
                  />
                  <div style={{ marginTop: 4 }}>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      File name format: &lt;prefix&gt;_&lt;timestamp&gt;.ext (or &lt;timestamp&gt;.ext without a prefix)
                    </Typography.Text>
                  </div>
                </div>
                <Space wrap>
                  <Select
                    value={snapshotFormat}
                    onChange={setSnapshotFormat}
                    options={[
                      { value: 'fits', label: 'FITS (16-bit raw)' },
                      { value: 'tiff', label: 'TIFF (16-bit)' },
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
                  Changes are written to the camera immediately and the returned actual value is shown
                </Typography.Text>
              </Space>
            }
            loading={propsLoading}
          >
            {properties.length === 0 && !propsLoading ? (
              <Alert type="warning" showIcon message="No device open, cannot fetch properties" description='Please open a device on the "Connect" page first (GET /api/properties returns 409 when no device is open).' />
            ) : (
              <>
                {numericProps.map((p) => (
                  <NumericPropertyRow
                    key={`${p.name}:${String(p.value)}`}
                    prop={p}
                    actual={actuals[p.name]}
                    onPut={putProperty}
                  />
                ))}
                <Divider style={{ margin: '12px 0' }} />
                {boolProps.map((p) => (
                  <BoolPropertyRow key={`${p.name}:${String(p.value)}`} prop={p} onPut={putProperty} />
                ))}
              </>
            )}
          </Card>
        </Col>

        <Col xs={24} lg={8}>
          <Card size="small" title="Capture Region (ROI)" loading={propsLoading}>
            {roiProps.length === 0 && !propsLoading ? (
              <Typography.Text type="secondary">
                The current camera does not report ROI properties (the mock driver supports at least width/height).
              </Typography.Text>
            ) : (
              <Space direction="vertical" size="middle" style={{ width: '100%' }}>
                <div>
                  <Typography.Text>Current ROI: {roiWidthText}</Typography.Text>
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
                        disabled={roiBusy}
                        onPut={putProperty}
                      />
                    </Col>
                  ))}
                </Row>
                <Button block onClick={() => void resetRoi()} loading={roiBusy}>
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
