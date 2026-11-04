import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
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
import { apiDelete, apiGet, apiPost, apiPut } from '../api/client';
import { resolveUrl } from '../api/backend';
import {
  isBlockingWaiting,
  isPreparingWaiting,
  shouldAcceptCaptureState,
} from '../api/captureStateMachine';
import { useReconnectingWebSocket } from '../api/ws';
import type {
  CaptureDraft,
  CaptureDraftPhase,
  CapturePhasePlan,
  CapturePhaseStored,
  CapturePlanResult,
  CaptureSet,
  CaptureSetNames,
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

const DEFAULT_TEMPLATE: CapturePhasePlan[] = [
  { type: 'bias', count: 50, exposure_us: 30, note: '' },
  { type: 'dark', count: 30, exposure_us: null, note: 'Same exposure as light' },
  { type: 'flat', count: 30, exposure_us: null, note: 'Defocused beam for uniform illumination' },
  { type: 'light', count: 100, exposure_us: null, note: '' },
];

interface PhaseRow {
  key: string;
  type: PhaseType;
  count: number;
  exposure_us: number | null;
  note: string;
}

const DEFAULT_PHASES: CaptureDraftPhase[] = DEFAULT_TEMPLATE.map((p) => ({ ...p }));

/** Named sets carry exposure and count only (no note). */
const toStoredPhases = (rows: PhaseRow[]): CapturePhaseStored[] =>
  rows.map((r) => ({
    type: r.type,
    count: Number.isFinite(r.count) ? Math.round(r.count) : 1,
    exposure_us: r.exposure_us === null || r.exposure_us === undefined ? null : r.exposure_us,
  }));

/** The draft also keeps note: it is written into every frame's FITS COMMENT and must survive a refresh. An empty note is stored as null. */
const toDraftPhases = (rows: PhaseRow[]): CaptureDraftPhase[] =>
  toStoredPhases(rows).map((p, i) => ({ ...p, note: rows[i].note === '' ? null : rows[i].note }));

const serializeDraftPhases = (phases: CaptureDraftPhase[]): string =>
  JSON.stringify(
    phases.map((p) => ({
      type: p.type,
      count: p.count,
      exposure_us: p.exposure_us ?? null,
      // An empty note and null both mean "no note", so entering the page does not write an unchanged draft
      note: p.note ? p.note : null,
    })),
  );

/** A set name travels as a query parameter, so characters like "/" are no longer cut off by a path segment. */
const setUrl = (name: string): string => `/api/capture/set?${new URLSearchParams({ name }).toString()}`;

/** Append the backend message (which names the damaged file) so a draft failure is diagnosable from the toast. */
const draftErrorDetail = (err: unknown): string => {
  const backendText = (err as { response?: { data?: { error?: string } } })?.response?.data?.error;
  if (backendText) return `: ${backendText}`;
  if (err instanceof Error && err.message) return `: ${err.message}`;
  return '';
};

let rowSeq = 0;
const nextKey = () => `row-${Date.now()}-${rowSeq++}`;

const IDLE_STATE: CaptureState = { status: 'idle', frames_done: 0, total_frames: 0 };

const draftRows = (phases: CaptureDraftPhase[]): PhaseRow[] =>
  phases.map((p) => ({
    key: nextKey(),
    type: p.type,
    count: p.count,
    exposure_us: p.exposure_us,
    note: p.note ?? '',
  }));

/** Sets keep no note, so the phase table starts with empty notes after a set is loaded. */
const storedRows = (phases: CapturePhaseStored[]): PhaseRow[] =>
  phases.map((p) => ({ key: nextKey(), type: p.type, count: p.count, exposure_us: p.exposure_us, note: '' }));

function newRow(): PhaseRow {
  return { key: nextKey(), type: 'light', count: 100, exposure_us: null, note: '' };
}

export default function CapturePage() {
  const navigate = useNavigate();
  const [sessionName, setSessionName] = useState('');
  const [effectiveSessionName, setEffectiveSessionName] = useState<string | null>(null);
  const [rows, setRows] = useState<PhaseRow[]>([]);
  const [savedSets, setSavedSets] = useState<string[]>([]);
  const [selectedSet, setSelectedSet] = useState<string | null>(null);
  const [setBusy, setSetBusy] = useState(false);
  const [saveAsOpen, setSaveAsOpen] = useState(false);
  const [saveAsName, setSaveAsName] = useState('');
  // Do not save before the draft has loaded, so the empty initial rows cannot overwrite the last phase table
  const draftLoadedRef = useRef(false);
  // Serialized phases of the last write (or of the loaded draft), used to skip no-op writes
  const lastSavedRef = useRef('');
  const [starting, setStarting] = useState(false);
  const [resumeBusy, setResumeBusy] = useState(false);
  const [captureState, setCaptureState] = useState<CaptureState>(IDLE_STATE);
  const [thumbs, setThumbs] = useState<FrameInfo[]>([]);
  // Thumbnail request sequence and owning session: switching sessions invalidates old responses and only the current session's frames are shown.
  const [thumbSession, setThumbSession] = useState<string | null>(null);
  const thumbSessionRef = useRef<string | null>(null);
  const thumbFetchSeqRef = useRef(0);

  const active = ['running', 'waiting_user', 'capturing'].includes(captureState.status);
  const terminal = ['finished', 'aborted', 'error'].includes(captureState.status);

  /**
   * v1.1 rule (R-1 fix): drop only stale needs_user===false waiting_user events that arrive during capturing;
   * needs_user===true (such as the mandatory LIGHT confirmation) must be handled. A functional setState reads the latest prev, so no ref is needed.
   */
  const applyCaptureState = useCallback((next: CaptureState) => {
    setCaptureState((prev) => (shouldAcceptCaptureState(prev, next) ? next : prev));
  }, []);

  const refreshCaptureState = useCallback(async () => {
    try {
      const state = await apiGet<CaptureState>('/api/capture/state', { silent: true });
      applyCaptureState(state);
    } catch {
      /* Backend not started or no plan yet: stay idle */
    }
  }, [applyCaptureState]);

  useEffect(() => {
    // Defer the initial fetch to the next macrotask so a synchronous setState inside the effect cannot cascade renders
    const timer = setTimeout(() => void refreshCaptureState(), 0);
    return () => clearTimeout(timer);
  }, [refreshCaptureState]);

  // State polling (1s)
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => void refreshCaptureState(), 1000);
    return () => clearInterval(timer);
  }, [active, refreshCaptureState]);

  // WS /ws/events keeps the capture state in sync
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
    // On a session switch, clear the previous session's thumbnails immediately and invalidate old requests.
    if (thumbSessionRef.current !== sessionNameNow) {
      thumbSessionRef.current = sessionNameNow;
      setThumbSession(sessionNameNow);
      setThumbs([]);
    }
    const seq = ++thumbFetchSeqRef.current;
    try {
      const sessions = await apiGet<SessionSummary[]>('/api/sessions', { silent: true });
      if (seq !== thumbFetchSeqRef.current) return;
      // Accept only an exact session match and never fall back to sessions[0], which would show another session's frames.
      const match = sessions.find((s) => s.name === sessionNameNow);
      if (!match) return;
      const detail = await apiGet<SessionDetail>(`/api/sessions/${match.id}`, { silent: true });
      if (seq !== thumbFetchSeqRef.current) return;
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
      /* Ignore silently while the session is not written yet */
    }
  }, []);

  // Thumbnail wall polling (2.5s); the first refresh is deferred to the next macrotask so the effect cannot cascade renders
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
    setRows(draftRows(DEFAULT_PHASES));
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

  const currentPhases = (): CapturePhaseStored[] => toStoredPhases(rows);

  const refreshSets = useCallback(async () => {
    try {
      const data = await apiGet<CaptureSetNames>('/api/capture/sets', { silent: true });
      setSavedSets(data.names ?? []);
    } catch {
      /* Backend not started: keep the current list */
    }
  }, []);

  // On entry, read the backend draft first and fall back to the default template only when it is empty.
  useEffect(() => {
    let cancelled = false;
    const timer = setTimeout(() => {
      void (async () => {
        try {
          const draft = await apiGet<CaptureDraft>('/api/capture/draft', { silent: true });
          if (cancelled) return;
          if (draft.phases && draft.phases.length > 0) {
            lastSavedRef.current = serializeDraftPhases(draft.phases);
            setRows(draftRows(draft.phases));
          } else {
            lastSavedRef.current = serializeDraftPhases(DEFAULT_PHASES);
            applyTemplate();
          }
        } catch (err) {
          if (!cancelled) {
            // A damaged or unreadable draft must be visible: never fall back to the template silently.
            message.error({
              content: `Failed to load the capture draft${draftErrorDetail(err)}`,
              key: 'capture-draft-load',
            });
            lastSavedRef.current = serializeDraftPhases(DEFAULT_PHASES);
            applyTemplate();
          }
        } finally {
          if (!cancelled) draftLoadedRef.current = true;
        }
      })();
    }, 0);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, []);

  useEffect(() => {
    const timer = setTimeout(() => void refreshSets(), 0);
    return () => clearTimeout(timer);
  }, [refreshSets]);

  // Any phase-table change saves the draft within 500ms (draft file only, named sets untouched).
  useEffect(() => {
    if (!draftLoadedRef.current) return;
    const phases = toDraftPhases(rows);
    const serialized = serializeDraftPhases(phases);
    if (serialized === lastSavedRef.current) return;
    const timer = setTimeout(() => {
      void apiPut<CaptureDraft>('/api/capture/draft', { phases }, { silent: true })
        .then(() => {
          lastSavedRef.current = serialized;
        })
        .catch((err) => {
          // Keep lastSavedRef unchanged so the next edit retries, but never drop the edit silently.
          message.error({
            content: `Failed to save the capture draft${draftErrorDetail(err)}`,
            key: 'capture-draft-save',
          });
        });
    }, 500);
    return () => clearTimeout(timer);
  }, [rows]);

  const loadSet = async (name: string) => {
    try {
      const set = await apiGet<CaptureSet>(setUrl(name));
      setRows(storedRows(set.phases));
      setSelectedSet(set.name);
    } catch {
      /* The API client already showed the error */
    }
  };

  const putSet = async (name: string) => {
    const saved = await apiPut<CaptureSet>(setUrl(name), {
      phases: currentPhases(),
    });
    setSavedSets((prev) => (prev.includes(saved.name) ? prev : [...prev, saved.name].sort()));
    setSelectedSet(saved.name);
    return saved;
  };

  const openSaveAs = () => {
    setSaveAsName(selectedSet ?? '');
    setSaveAsOpen(true);
  };

  // Save As: ask for overwrite once when the name exists, otherwise save directly.
  const confirmSaveAs = async () => {
    const name = saveAsName.trim();
    if (!name) {
      message.warning('Please enter a capture set name');
      return;
    }
    if (savedSets.includes(name)) {
      const okToOverwrite = await new Promise<boolean>((resolve) => {
        Modal.confirm({
          title: 'Overwrite existing set?',
          content: `A capture set named "${name}" already exists. Saving replaces its phases.`,
          okText: 'Overwrite',
          okButtonProps: { danger: true },
          cancelText: 'Cancel',
          onOk: () => resolve(true),
          onCancel: () => resolve(false),
        });
      });
      if (!okToOverwrite) return;
    }
    setSetBusy(true);
    try {
      await putSet(name);
      setSaveAsOpen(false);
      message.success(`Capture set "${name}" saved`);
    } catch {
      /* The API client already showed the error */
    } finally {
      setSetBusy(false);
    }
  };

  const overwriteSet = async () => {
    if (!selectedSet) return;
    setSetBusy(true);
    try {
      await putSet(selectedSet);
      message.success(`Capture set "${selectedSet}" overwritten`);
    } catch {
      /* The API client already showed the error */
    } finally {
      setSetBusy(false);
    }
  };

  const removeSet = async () => {
    if (!selectedSet) return;
    const name = selectedSet;
    setSetBusy(true);
    try {
      await apiDelete<{ name: string; deleted: boolean }>(setUrl(name));
      setSavedSets((prev) => prev.filter((n) => n !== name));
      setSelectedSet(null);
      message.success(`Capture set "${name}" deleted`);
    } catch {
      /* The API client already showed the error */
    } finally {
      setSetBusy(false);
    }
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
      // v1.2: the response session_name is the final name with the timestamp prefix; fill it in after planning and use it everywhere
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
    } catch {
      /* The API client already showed the error */
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
    } catch {
      /* The API client already showed the error */
    } finally {
      setResumeBusy(false);
    }
  };

  const abortCapture = async () => {
    try {
      const state = await apiPost<CaptureState>('/api/capture/abort');
      setCaptureState((prev) => ({ ...prev, ...state }));
      await refreshCaptureState();
    } catch {
      /* The API client already showed the error */
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
          disabled={active}
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
          disabled={active}
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
          disabled={active}
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
          disabled={active}
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
  // Later displays follow the plan response; after a refresh without a local effective name, fall back to the WS/state value
  const sessionDisplayName = effectiveSessionName ?? captureState.session_name ?? '—';
  const done = captureState.frames_done ?? 0;
  const total = captureState.total_frames || totalFrames;
  const progressPct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
  const currentPhaseLabel = captureState.current_phase
    ? `${PHASE_LABELS[captureState.current_phase.type]} (Phase ${captureState.current_phase.index}/${captureState.current_phase.total})`
    : '—';

  const modalPrompt = useMemo(() => {
    const prompt = captureState.prompt || 'Please confirm the current capture conditions, then continue.';
    if (captureState.current_phase?.type === 'light' && !prompt.includes(LIGHT_CONFIRM_HINT)) {
      return `${prompt}\n\n${LIGHT_CONFIRM_HINT}: please verify exposure/gain/black-level against the live preview and histogram before continuing.`;
    }
    return prompt;
  }, [captureState.prompt, captureState.current_phase]);

  // v1.1 semantics: only waiting_user && needs_user===true blocks; needs_user===false shows the non-blocking banner.
  // A missing needs_user (old backend) counts as true for backward compatibility.
  const blockingModal = isBlockingWaiting(captureState);
  const preparingWithoutUser = isPreparingWaiting(captureState);
  const preparingPhaseLabel = captureState.current_phase
    ? `${PHASE_LABELS[captureState.current_phase.type]} (Phase ${captureState.current_phase.index}/${captureState.current_phase.total})`
    : 'Current phase';

  // Show thumbnails only when their session matches captureState.session_name,
  // so the first render after a session switch cannot briefly show the previous session's frames.
  const displayedThumbs = thumbSession === captureState.session_name ? thumbs : [];

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
          <Select
            style={{ width: 240 }}
            placeholder={savedSets.length > 0 ? 'Load a saved set' : 'No saved sets yet'}
            value={selectedSet ?? undefined}
            onChange={(v?: string) => {
              // antd passes undefined when the dropdown is cleared: send no request, just clear the selection
              if (!v) {
                setSelectedSet(null);
                return;
              }
              void loadSet(v);
            }}
            onClear={() => setSelectedSet(null)}
            options={savedSets.map((n) => ({ value: n, label: n }))}
            allowClear
            disabled={active || savedSets.length === 0}
          />
          <Button disabled={active || setBusy} onClick={openSaveAs}>
            Save As…
          </Button>
          <Popconfirm
            title="Overwrite this set?"
            description={selectedSet ? `"${selectedSet}" will be replaced by the current phase table.` : undefined}
            okText="Overwrite"
            cancelText="Cancel"
            disabled={!selectedSet || active}
            onConfirm={() => void overwriteSet()}
          >
            <Button disabled={active || !selectedSet || setBusy}>Overwrite</Button>
          </Popconfirm>
          <Popconfirm
            title="Delete this set?"
            description={selectedSet ? `"${selectedSet}" will be removed permanently.` : undefined}
            okText="Delete"
            okButtonProps={{ danger: true }}
            cancelText="Cancel"
            disabled={!selectedSet || active}
            onConfirm={() => void removeSet()}
          >
            <Button danger disabled={active || !selectedSet || setBusy}>
              Delete
            </Button>
          </Popconfirm>
        </Space>
        <Space wrap style={{ marginBottom: 12 }}>
          <Button disabled={active} onClick={applyTemplate}>
            Default Diffraction Set (bias × 50, dark × 30, flat × 30, light × 100)
          </Button>
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

      {(displayedThumbs.length > 0 || captureState.session_name) && (
        <Card size="small" title={`Latest Frame Thumbnails (last ${displayedThumbs.length} frames, auto-refresh)`}>
          {displayedThumbs.length === 0 ? (
            <Empty description="No thumbnails yet (they appear automatically as frames are written)" />
          ) : (
            <Image.PreviewGroup>
              <Space wrap size={4}>
                {displayedThumbs.map((f) => (
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

      <Modal
        title="Save Capture Set"
        open={saveAsOpen}
        okText="Save"
        confirmLoading={setBusy}
        onOk={() => void confirmSaveAs()}
        onCancel={() => setSaveAsOpen(false)}
      >
        <Typography.Paragraph type="secondary">
          The set stores the exposure time and frame count of each phase.
        </Typography.Paragraph>
        <Input
          value={saveAsName}
          placeholder="Set name"
          onChange={(e) => setSaveAsName(e.target.value)}
          onPressEnter={() => void confirmSaveAs()}
        />
      </Modal>
    </Space>
  );
}
