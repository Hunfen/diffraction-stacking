import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Divider,
  Empty,
  InputNumber,
  Row,
  Select,
  Slider,
  Space,
  Statistic,
  Switch,
  Tag,
  Typography,
} from 'antd';
import { CameraOutlined, LinkOutlined } from '@ant-design/icons';
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { apiGet, apiPost, apiPut } from '../api/client';
import { resolveUrl } from '../api/backend';
import { getPreviewSettings } from '../api/settings';
import { useReconnectingWebSocket, type WsStatus } from '../api/ws';
import type {
  CameraProperty,
  LiveStats,
  PropertiesData,
  PropertyPutResult,
  SnapshotResult,
} from '../api/types';

const WS_LIVE = '/ws/live';

const statusMeta: Record<WsStatus, { color: string; text: string }> = {
  idle: { color: 'default', text: '未启动' },
  connecting: { color: 'processing', text: '连接中' },
  open: { color: 'success', text: '已连接' },
  reconnecting: { color: 'warning', text: '重连中' },
  closed: { color: 'error', text: '已断开' },
};

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

function wsStatusTag(status: WsStatus) {
  const meta = statusMeta[status];
  return <Tag color={meta.color}>{meta.text}</Tag>;
}

function useProperties(onActualChange?: () => void) {
  const [properties, setProperties] = useState<CameraProperty[]>([]);
  const [actuals, setActuals] = useState<Record<string, number | boolean>>({});
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      // 手动刷新/自动探测：未打开设备返回 409 属预期，silent 不弹 toast，页面用内联 Alert 提示
      const data = await apiGet<PropertiesData>('/api/properties', { silent: true });
      setProperties(data.properties);
    } catch {
      setProperties([]);
    } finally {
      setLoading(false);
    }
  }, []);

  // 初始加载内联成 promise 链（loading 初值即 true），不在 effect 里同步调用会 setState 的刷新函数
  useEffect(() => {
    let cancelled = false;
    apiGet<PropertiesData>('/api/properties', { silent: true })
      .then((data) => {
        if (!cancelled) setProperties(data.properties);
      })
      .catch(() => {
        if (!cancelled) setProperties([]);
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

  return { properties, actuals, loading, refresh, putProperty };
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
            范围 {min}–{max}
            {prop.unit ? ` ${prop.unit}` : ''}
            {actual !== undefined ? ` · 实际值 ${String(actual)}` : ''}
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

export default function LivePage() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [stats, setStats] = useState<LiveStats | null>(null);
  const [streamStarted, setStreamStarted] = useState(false);
  const preview = useMemo(() => getPreviewSettings(), []);
  const { properties, actuals, loading: propsLoading, putProperty } = useProperties();
  const [snapshotFormat, setSnapshotFormat] = useState<'fits' | 'tiff' | 'png'>('fits');
  const [snapshot, setSnapshot] = useState<SnapshotResult | null>(null);
  const [snapshotBusy, setSnapshotBusy] = useState(false);

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
          drawCrosshair(ctx, canvas.width, canvas.height);
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
            drawCrosshair(ctx, canvas.width, canvas.height);
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

  // 挂载时启动实时预览，卸载时停止；自动触发用 silent（未开设备 409 不弹 toast，由页面状态提示）
  useEffect(() => {
    let disposed = false;
    void apiPost('/api/live/start', preview, { silent: true })
      .then(() => {
        if (!disposed) setStreamStarted(true);
      })
      .catch(() => {
        if (!disposed) setStreamStarted(false);
      });
    return () => {
      disposed = true;
      void apiPost('/api/live/stop', undefined, { silent: true }).catch(() => undefined);
    };
  }, [preview]);

  const histogramData = useMemo(() => {
    if (!stats) return [];
    return stats.histogram.map((count, bin) => ({ bin, count }));
  }, [stats]);

  const numericProps = properties.filter((p) =>
    ['exposure', 'gain', 'black_level'].includes(p.name),
  );
  const boolProps = properties.filter((p) =>
    ['reverse_x', 'reverse_y', 'trigger_mode'].includes(p.name),
  );
  const roiProps = properties.filter((p) =>
    ['width', 'height', 'offset_x', 'offset_y'].includes(p.name),
  );

  const takeSnapshot = async () => {
    setSnapshotBusy(true);
    try {
      const result = await apiPost<SnapshotResult>('/api/snapshot', { format: snapshotFormat });
      setSnapshot(result);
    } finally {
      setSnapshotBusy(false);
    }
  };

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        message="实时预览"
        description="预览为 8-bit 灰阶自动对比度（1%~99% 分位线性映射），不修改相机采集参数。"
      />

      <Row gutter={16}>
        <Col xs={24} lg={16}>
          <Card
            size="small"
            title={
              <Space>
                预览画面
                {wsStatusTag(wsStatus)}
                {streamStarted && <Tag color="green">streaming</Tag>}
              </Space>
            }
          >
            <div style={{ background: '#000', minHeight: 320, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              <canvas
                ref={canvasRef}
                style={{ width: '100%', height: 'auto', display: 'block', background: '#000' }}
              />
              {!stats && wsStatus !== 'open' && <Empty description="等待视频流…" style={{ position: 'absolute' }} />}
            </div>
          </Card>
        </Col>

        <Col xs={24} lg={8}>
          <Card size="small" title="实时统计">
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
                    <Statistic title="饱和 %" value={stats.saturated_pct} precision={2} suffix="%" />
                  </Col>
                  <Col span={8}>
                    <Statistic title="FPS" value={stats.fps} precision={1} />
                  </Col>
                </Row>
                <Divider style={{ margin: '12px 0' }}>Histogram（256 bin）</Divider>
                <div style={{ height: 180 }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={histogramData} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#333" vertical={false} />
                      <XAxis dataKey="bin" hide />
                      <YAxis hide />
                      <Tooltip
                        formatter={(value: number) => [`${value} 像素`, 'count']}
                        labelFormatter={(bin) => `bin ${bin}`}
                      />
                      <Bar dataKey="count" fill="#4dabf7" isAnimationActive={false} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </>
            ) : (
              <Empty description="暂无统计数据" />
            )}
          </Card>
        </Col>
      </Row>

      <Row gutter={16}>
        <Col xs={24} lg={16}>
          <Card
            size="small"
            title={
              <Space>
                相机参数
                <Typography.Text type="secondary" style={{ fontWeight: 'normal', fontSize: 12 }}>
                  改动立即写入相机并显示返回 actual
                </Typography.Text>
              </Space>
            }
            loading={propsLoading}
          >
            {properties.length === 0 && !propsLoading ? (
              <Alert type="warning" showIcon message="未打开设备，无法获取属性" description="请先到「设备连接」页打开设备（GET /api/properties 未打开设备时返回 409）。" />
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
                <Divider style={{ margin: '12px 0' }}>ROI（宽度 / 高度 / X 偏移 / Y 偏移）</Divider>
                {roiProps.length === 0 ? (
                  <Typography.Text type="secondary">当前相机未报告 ROI 属性（mock 至少支持 width/height）。</Typography.Text>
                ) : (
                  <Space wrap>
                    {roiProps.map((p) => (
                      <RoiInput key={`${p.name}:${String(p.value)}`} prop={p} onPut={putProperty} />
                    ))}
                  </Space>
                )}
              </>
            )}
          </Card>
        </Col>

        <Col xs={24} lg={8}>
          <Card size="small" title="抓拍（单帧）">
            <Space direction="vertical" style={{ width: '100%' }}>
              <Space wrap>
                <Select
                  value={snapshotFormat}
                  onChange={setSnapshotFormat}
                  options={[
                    { value: 'fits', label: 'FITS（16-bit 原始）' },
                    { value: 'tiff', label: 'TIFF（16-bit）' },
                    { value: 'png', label: 'PNG（8-bit 自动对比度）' },
                  ]}
                  style={{ width: 220 }}
                />
                <Button
                  type="primary"
                  icon={<CameraOutlined />}
                  loading={snapshotBusy}
                  onClick={() => void takeSnapshot()}
                >
                  抓拍
                </Button>
              </Space>
              {snapshot && (
                <Descriptions size="small" column={1}>
                  <Descriptions.Item label="文件名">{snapshot.file}</Descriptions.Item>
                  <Descriptions.Item label="下载">
                    <a href={resolveUrl(snapshot.url)} target="_blank" rel="noreferrer">
                      {snapshot.url} <LinkOutlined />
                    </a>
                  </Descriptions.Item>
                </Descriptions>
              )}
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                快照保存到 data/snapshots/，URL 由后端返回。
              </Typography.Text>
            </Space>
          </Card>
        </Col>
      </Row>
    </Space>
  );
}

function RoiInput({ prop, onPut }: { prop: CameraProperty; onPut: (name: string, value: number) => Promise<PropertyPutResult | undefined> }) {
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
    <Space direction="vertical" size={2}>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {prop.label}（{prop.name}）
      </Typography.Text>
      <InputNumber
        min={prop.min ?? 0}
        max={prop.max ?? 1920}
        step={prop.step ?? 1}
        value={value}
        style={{ width: 130 }}
        onChange={(v) => {
          if (v !== null) void apply(v);
        }}
      />
    </Space>
  );
}
