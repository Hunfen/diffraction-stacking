import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Form,
  Image,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Progress,
  Row,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  DeleteOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  StopOutlined,
} from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import dayjs from 'dayjs';
import { apiGet, apiPost } from '../api/client';
import { resolveUrl } from '../api/backend';
import {
  isBlockingWaiting,
  isPreparingWaiting,
  shouldAcceptCaptureState,
} from '../api/captureStateMachine';
import { useReconnectingWebSocket } from '../api/ws';
import type {
  CapturePhasePlan,
  CapturePlanResult,
  CaptureState,
  FrameInfo,
  PhaseType,
  SessionDetail,
  SessionSummary,
} from '../api/types';

const LIGHT_CONFIRM_HINT = 'Please check the live histogram to manually confirm the parameters';

const PHASE_LABELS: Record<PhaseType, string> = {
  bias: 'Bias',
  dark: 'Dark',
  flat: 'Flat',
  light: 'Light',
};

const DEFAULT_TEMPLATE: Omit<CapturePhasePlan, 'note'>[] = [
  { type: 'bias', count: 50, exposure_us: 30 },
  { type: 'dark', count: 30, exposure_us: null },
  { type: 'flat', count: 30, exposure_us: null },
  { type: 'light', count: 100, exposure_us: null },
];

interface PhaseRow {
  key: string;
  type: PhaseType;
  count: number;
  exposure_us: number | null;
  note: string;
}

let rowSeq = 0;
const nextKey = () => `row-${Date.now()}-${rowSeq++}`;

const IDLE_STATE: CaptureState = { status: 'idle', frames_done: 0, total_frames: 0 };

function newRow(): PhaseRow {
  return { key: nextKey(), type: 'light', count: 100, exposure_us: null, note: '' };
}

export default function CapturePage() {
  const navigate = useNavigate();
  const [sessionName, setSessionName] = useState('');
  const [effectiveSessionName, setEffectiveSessionName] = useState<string | null>(null);
  const [rows, setRows] = useState<PhaseRow[]>([]);
  const [starting, setStarting] = useState(false);
  const [resumeBusy, setResumeBusy] = useState(false);
  const [captureState, setCaptureState] = useState<CaptureState>(IDLE_STATE);
  const [thumbs, setThumbs] = useState<FrameInfo[]>([]);

  const active = ['running', 'waiting_user', 'capturing'].includes(captureState.status);
  const terminal = ['finished', 'aborted', 'error'].includes(captureState.status);

  /**
   * v1.1 口径（R-1 修复）：只丢弃 capturing 期间到达的 needs_user===false 陈旧 waiting_user；
   * needs_user===true（如 LIGHT 强制确认点）必须处理。用函数式 setState 取最新 prev，无需 ref。
   */
  const applyCaptureState = useCallback((next: CaptureState) => {
    setCaptureState((prev) => (shouldAcceptCaptureState(prev, next) ? next : prev));
  }, []);

  const refreshCaptureState = useCallback(async () => {
    try {
      const state = await apiGet<CaptureState>('/api/capture/state', { silent: true });
      applyCaptureState(state);
    } catch {
      /* 后端未启动/未 plan：保持 idle */
    }
  }, [applyCaptureState]);

  useEffect(() => {
    // 初始状态获取放到下一个宏任务，避免 effect 内同步 setState 造成级联渲染
    const timer = setTimeout(() => void refreshCaptureState(), 0);
    return () => clearTimeout(timer);
  }, [refreshCaptureState]);

  // 状态轮询（1s）
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => void refreshCaptureState(), 1000);
    return () => clearInterval(timer);
  }, [active, refreshCaptureState]);

  // WS /ws/events 实时同步 capture 状态
  const handleEvent = useCallback(
    (ev: MessageEvent) => {
      if (typeof ev.data !== 'string') return;
      try {
        const parsed = JSON.parse(ev.data) as { topic?: string; data?: CaptureState };
        if (parsed.topic === 'capture' && parsed.data) {
          applyCaptureState(parsed.data);
        }
      } catch {
        /* ignore */
      }
    },
    [applyCaptureState],
  );

  useReconnectingWebSocket('/ws/events', {
    enabled: true,
    onMessage: handleEvent,
  });

  const refreshThumbs = useCallback(async (sessionNameNow?: string) => {
    if (!sessionNameNow) return;
    try {
      const sessions = await apiGet<SessionSummary[]>('/api/sessions', { silent: true });
      const match =
        sessions.find((s) => s.name === sessionNameNow) ??
        (sessions.length > 0 ? sessions[0] : undefined);
      if (!match) return;
      const detail = await apiGet<SessionDetail>(`/api/sessions/${match.id}`, { silent: true });
      const orderedTypes: PhaseType[] =
        detail.phases.length > 0
          ? detail.phases.map((p) => p.type)
          : ['bias', 'dark', 'flat', 'light'];
      const all: FrameInfo[] = [];
      orderedTypes.forEach((t) => {
        (detail.frames?.[t] ?? []).forEach((f) => all.push(f));
      });
      setThumbs(all.slice(-20));
    } catch {
      /* session 尚未写入时静默忽略 */
    }
  }, []);

  // 缩略图墙轮询（2.5s）；首次刷新放到下一个宏任务，避免 effect 内同步触发级联渲染
  useEffect(() => {
    if (!captureState.session_name) return;
    const sessionNameNow = captureState.session_name;
    const timer = setInterval(() => void refreshThumbs(sessionNameNow), 2500);
    const firstTimer = setTimeout(() => void refreshThumbs(sessionNameNow), 0);
    return () => {
      clearInterval(timer);
      clearTimeout(firstTimer);
    };
  }, [captureState.session_name, refreshThumbs]);

  const applyTemplate = () => {
    setRows(
      DEFAULT_TEMPLATE.map((p, i) => ({
        ...p,
        key: nextKey(),
        note:
          i === 1
            ? 'Same exposure as light'
            : i === 2
              ? 'Defocused beam for uniform illumination'
              : '',
      })),
    );
  };

  const updateRow = (key: string, patch: Partial<PhaseRow>) => {
    setRows((prev) => prev.map((r) => (r.key === key ? { ...r, ...patch } : r)));
  };

  const moveRow = (index: number, dir: -1 | 1) => {
    setRows((prev) => {
      const target = index + dir;
      if (target < 0 || target >= prev.length) return prev;
      const next = [...prev];
      const [row] = next.splice(index, 1);
      next.splice(target, 0, row);
      return next;
    });
  };

  const startCapture = async () => {
    if (rows.length === 0) {
      message.warning('Please add at least one phase');
      return;
    }
    if (rows.some((r) => !r.type || !Number.isFinite(r.count) || r.count < 1 || r.count > 10000)) {
      message.warning('The frame count for each phase must be an integer between 1 and 10000');
      return;
    }
    const name = sessionName.trim();
    setStarting(true);
    try {
      const phases: CapturePhasePlan[] = rows.map((r) => ({
        type: r.type,
        count: Math.round(r.count),
        exposure_us: r.exposure_us === null || r.exposure_us === undefined ? null : r.exposure_us,
        note: r.note,
      }));
      const plan = await apiPost<CapturePlanResult>('/api/capture/plan', { session_name: name, phases });
      // v1.2：响应里的 session_name 是带时间戳前缀的最终名，创建计划成功后回填并以该值为准
      const effective = plan.session_name || name || `SESSION_${dayjs().format('YYYYMMDDTHHmmss')}`;
      setSessionName(effective);
      setEffectiveSessionName(effective);
      await apiPost<{ status: CaptureState['status'] }>('/api/capture/start');
      setCaptureState({
        ...IDLE_STATE,
        status: 'running',
        session_name: effective,
        total_frames: phases.reduce((a, b) => a + b.count, 0),
      });
      await refreshCaptureState();
    } finally {
      setStarting(false);
    }
  };

  const resumeCapture = async () => {
    setResumeBusy(true);
    try {
      const state = await apiPost<CaptureState>('/api/capture/resume');
      setCaptureState((prev) => ({ ...prev, ...state }));
      await refreshCaptureState();
    } finally {
      setResumeBusy(false);
    }
  };

  const abortCapture = async () => {
    try {
      const state = await apiPost<CaptureState>('/api/capture/abort');
      setCaptureState((prev) => ({ ...prev, ...state }));
      await refreshCaptureState();
    } finally {
      void refreshThumbs();
    }
  };

  const columns: ColumnsType<PhaseRow> = [
    {
      title: 'Type',
      dataIndex: 'type',
      width: 180,
      render: (_, row) => (
        <Select
          value={row.type}
          style={{ width: '100%' }}
          onChange={(v: PhaseType) => updateRow(row.key, { type: v })}
          options={(Object.keys(PHASE_LABELS) as PhaseType[]).map((t) => ({
            value: t,
            label: PHASE_LABELS[t],
          }))}
        />
      ),
    },
    {
      title: 'Frames',
      dataIndex: 'count',
      width: 120,
      render: (_, row) => (
        <InputNumber
          min={1}
          max={10000}
          precision={0}
          value={row.count}
          style={{ width: '100%' }}
          onChange={(v) => {
            if (v !== null) updateRow(row.key, { count: v });
          }}
        />
      ),
    },
    {
      title: 'Exposure Override (µs)',
      dataIndex: 'exposure_us',
      width: 160,
      render: (_, row) => (
        <InputNumber
          min={1}
          max={30000000}
          value={row.exposure_us}
          placeholder="Keep current"
          style={{ width: '100%' }}
          onChange={(v) => updateRow(row.key, { exposure_us: v })}
        />
      ),
    },
    {
      title: 'Note',
      dataIndex: 'note',
      render: (_, row) => (
        <Input
          value={row.note}
          placeholder="Phase note (optional)"
          onChange={(e) => updateRow(row.key, { note: e.target.value })}
        />
      ),
    },
    {
      title: 'Actions',
      width: 160,
      render: (_, row, index) => (
        <Space>
          <Button
            size="small"
            icon={<ArrowUpOutlined />}
            disabled={index === 0 || active}
            onClick={() => moveRow(index, -1)}
          />
          <Button
            size="small"
            icon={<ArrowDownOutlined />}
            disabled={index === rows.length - 1 || active}
            onClick={() => moveRow(index, 1)}
          />
          <Button
            size="small"
            danger
            icon={<DeleteOutlined />}
            disabled={active}
            onClick={() => setRows((prev) => prev.filter((r) => r.key !== row.key))}
          />
        </Space>
      ),
    },
  ];

  const totalFrames = rows.reduce((a, b) => a + (Number.isFinite(b.count) ? b.count : 0), 0);
  // 后续展示以 plan 响应值为准；刷新页面后没有本地 effective 时退回 WS/state 值
  const sessionDisplayName = effectiveSessionName ?? captureState.session_name ?? '—';
  const done = captureState.frames_done ?? 0;
  const total = captureState.total_frames || totalFrames;
  const progressPct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
  const currentPhaseLabel = captureState.current_phase
    ? `${PHASE_LABELS[captureState.current_phase.type]} (Phase ${captureState.current_phase.index + 1}/${captureState.current_phase.total})`
    : '—';

  const modalPrompt = useMemo(() => {
    const prompt = captureState.prompt || 'Please confirm the current capture conditions, then continue.';
    if (captureState.current_phase?.type === 'light' && !prompt.includes(LIGHT_CONFIRM_HINT)) {
      return `${prompt}\n\n${LIGHT_CONFIRM_HINT}: please verify exposure/gain/black-level against the live preview and histogram before continuing.`;
    }
    return prompt;
  }, [captureState.prompt, captureState.current_phase]);

  // v1.1 语义：waiting_user && needs_user===true 才阻塞；needs_user===false 只显示非阻塞 Banner。
  // needs_user 缺失（旧后端）按 true 处理，保持向后兼容。
  const blockingModal = isBlockingWaiting(captureState);
  const preparingWithoutUser = isPreparingWaiting(captureState);
  const preparingPhaseLabel = captureState.current_phase
    ? `${PHASE_LABELS[captureState.current_phase.type]} (Phase ${captureState.current_phase.index + 1}/${captureState.current_phase.total})`
    : 'Current phase';

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Card size="small" title="Capture Plan">
        <Form layout="vertical">
          <Row gutter={16}>
            <Col xs={24} md={8}>
              <Form.Item
                label="Session name"
                required
                extra={
                  <Typography.Text type="secondary">
                    A timestamp prefix (YYYYMMDDTHH-MM-SS_) will be added automatically
                  </Typography.Text>
                }
              >
                <Input
                  value={sessionName}
                  placeholder="Leave empty to use 'session'"
                  onChange={(e) => setSessionName(e.target.value)}
                />
              </Form.Item>
            </Col>
          </Row>
        </Form>
        <Space wrap style={{ marginBottom: 12 }}>
          <Button onClick={applyTemplate}>Default Diffraction Set (bias × 50, dark × 30, flat × 30, light × 100)</Button>
          <Button icon={<PlusOutlined />} disabled={active} onClick={() => setRows((prev) => [...prev, newRow()])}>
            Add Phase
          </Button>
        </Space>
        <Table<PhaseRow>
          rowKey="key"
          size="small"
          columns={columns}
          dataSource={rows}
          pagination={false}
          locale={{ emptyText: 'No phases yet. Click "Default Diffraction Set" or "Add Phase".' }}
          scroll={{ x: 900 }}
        />
        <Space style={{ marginTop: 12 }}>
          <Typography.Text type="secondary">
            Total frames: {totalFrames} (blank exposure_us = keep the current camera exposure; non-blank = force override before the phase starts)
          </Typography.Text>
        </Space>
        <div style={{ marginTop: 12 }}>
          <Button
            type="primary"
            size="large"
            icon={<PlayCircleOutlined />}
            loading={starting}
            disabled={active || rows.length === 0}
            onClick={() => void startCapture()}
          >
            Start Capture Sequence
          </Button>
        </div>
        {effectiveSessionName && (
          <Alert
            style={{ marginTop: 12 }}
            type="success"
            showIcon
            message={`Session name for this run: ${effectiveSessionName}`}
            description="This is the final name returned by the backend after the plan is created (with a timestamp prefix). All subsequent progress and completion messages use this name."
          />
        )}
      </Card>

      {(active || terminal) && (
        <Card
          size="small"
          title={
            <Space>
              Execution Status
              <Tag color={captureState.status === 'error' ? 'error' : 'processing'}>
                {captureState.status}
              </Tag>
            </Space>
          }
        >
          <Descriptions column={2} size="small" bordered style={{ marginBottom: 12 }}>
            <Descriptions.Item label="Session">{sessionDisplayName}</Descriptions.Item>
            <Descriptions.Item label="Current Phase">{currentPhaseLabel}</Descriptions.Item>
            <Descriptions.Item label="Current Frame">{done}</Descriptions.Item>
            <Descriptions.Item label="Total Frames">{total}</Descriptions.Item>
          </Descriptions>
          <Progress
            percent={progressPct}
            status={captureState.status === 'error' ? 'exception' : active ? 'active' : 'success'}
            format={() => `${done} / ${total}`}
          />
          {preparingWithoutUser && (
            <Alert
              style={{ marginTop: 8 }}
              type="info"
              showIcon
              banner
              message={`Preparing to capture ${preparingPhaseLabel}… (no manual confirmation required; the Continue button is not shown)`}
              description={captureState.prompt || undefined}
            />
          )}
          {captureState.message && <Alert type="info" showIcon message={captureState.message} style={{ marginTop: 8 }} />}
          {terminal && (
            <Alert
              style={{ marginTop: 8 }}
              type={captureState.status === 'finished' ? 'success' : captureState.status === 'aborted' ? 'warning' : 'error'}
              showIcon
              message={
                captureState.status === 'finished'
                  ? `Capture sequence finished (Session: ${sessionDisplayName})`
                  : captureState.status === 'aborted'
                    ? `Aborted (captured frames kept, Session: ${sessionDisplayName})`
                    : `Capture error: ${captureState.message || 'Unknown error'}`
              }
              action={
                <Button size="small" type="primary" onClick={() => navigate('/processing')}>
                  Go to Processing →
                </Button>
              }
            />
          )}
          <Popconfirm
            title="Abort capture?"
            description="Frames already captured will be kept and will not be deleted."
            okText="Abort"
            cancelText="Cancel"
            onConfirm={() => void abortCapture()}
          >
            <Button danger icon={<StopOutlined />} style={{ marginTop: 12 }} disabled={!active}>
              Abort Capture (Keep Captured Frames)
            </Button>
          </Popconfirm>
        </Card>
      )}

      {(thumbs.length > 0 || captureState.session_name) && (
        <Card size="small" title={`Latest Frame Thumbnails (last ${thumbs.length} frames, auto-refresh)`}>
          {thumbs.length === 0 ? (
            <Empty description="No thumbnails yet (they appear automatically as frames are written)" />
          ) : (
            <Image.PreviewGroup>
              <Space wrap size={4}>
                {thumbs.map((f) => (
                  <Image
                    key={f.file}
                    src={resolveUrl(f.thumb)}
                    width={96}
                    height={72}
                    alt={f.file}
                    title={f.file}
                    style={{ objectFit: 'cover', background: '#000' }}
                  />
                ))}
              </Space>
            </Image.PreviewGroup>
          )}
        </Card>
      )}

      <Modal
        title={
          captureState.current_phase
            ? `${PHASE_LABELS[captureState.current_phase.type]} — Confirmation Required (Manual Continue)`
            : 'Confirmation Required (Manual Continue)'
        }
        open={blockingModal}
        closable={false}
        maskClosable={false}
        keyboard={false}
        footer={[
          <Button key="continue" type="primary" loading={resumeBusy} onClick={() => void resumeCapture()}>
            Continue
          </Button>,
        ]}
      >
        <Typography.Paragraph style={{ whiteSpace: 'pre-line' }}>{modalPrompt}</Typography.Paragraph>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          Click "Continue" to call POST /api/capture/resume and continue the current phase.
        </Typography.Text>
      </Modal>
    </Space>
  );
}
