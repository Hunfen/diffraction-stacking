import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Badge,
  Button,
  Card,
  Checkbox,
  Col,
  Descriptions,
  Empty,
  Form,
  InputNumber,
  List,
  Progress,
  Radio,
  Row,
  Select,
  Slider,
  Space,
  Steps,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import { DownloadOutlined, ReloadOutlined } from '@ant-design/icons';
import { apiGet, apiPost } from '../api/client';
import { resolveUrl } from '../api/backend';
import { useReconnectingWebSocket } from '../api/ws';
import type {
  CaptureState,
  ExcludeMap,
  FrameInfo,
  JobEventData,
  JobSubmitResult,
  PhaseType,
  ProcessMethod,
  SessionDetail,
  SessionSummary,
  StretchMethod,
} from '../api/types';

const PHASE_LABELS: Record<PhaseType, string> = {
  bias: 'Bias',
  dark: 'Dark',
  flat: 'Flat',
  light: 'Light',
};

const PHASE_EN_LABELS: Record<PhaseType, string> = {
  bias: 'Bias',
  dark: 'Dark',
  flat: 'Flat',
  light: 'Light',
};

const ALL_PHASE_TYPES: PhaseType[] = ['bias', 'dark', 'flat', 'light'];

const JOB_TYPE_LABELS: Record<JobEventData['type'], string> = {
  calibrate: 'Calibrate (Master Build)',
  stack: 'Stack',
  stretch: 'Rescale Preview',
};

const JOB_STATUS_META: Record<JobEventData['status'], { color: string; text: string }> = {
  pending: { color: 'default', text: 'Queued' },
  running: { color: 'processing', text: 'Running' },
  done: { color: 'success', text: 'Done' },
  error: { color: 'error', text: 'Failed' },
};

/** Poll only valid job_ids so a ghost id such as undefined never hits GET /api/process/jobs/undefined */
const isValidJobId = (id: string): boolean => typeof id === 'string' && id.length > 0 && id !== 'undefined';

/** POST /api/process/* responds with {job_id,...}; normalize it into the JobEventData (field id) used by the jobs map */
const toJobEvent = (submitted: JobSubmitResult, sessionId?: string): JobEventData => ({
  id: submitted.job_id,
  type: submitted.type,
  status: submitted.status,
  session_id: submitted.session_id ?? sessionId,
});

export default function ProcessingPage() {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(true);
  const [selectedId, setSelectedId] = useState<string | undefined>();
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(true);
  const [excluded, setExcluded] = useState<Set<string>>(new Set());

  const [calSigma, setCalSigma] = useState(3);
  const [calMethod, setCalMethod] = useState<ProcessMethod>('mean');
  const [calSubmitting, setCalSubmitting] = useState(false);

  const [stackSigma, setStackSigma] = useState(3);
  const [stackMethod, setStackMethod] = useState<ProcessMethod>('mean');
  const [registration, setRegistration] = useState(false);
  const [stackSubmitting, setStackSubmitting] = useState(false);

  const [jobs, setJobs] = useState<Record<string, JobEventData>>({});
  const [captureState, setCaptureState] = useState<CaptureState | null>(null);

  const [selectedResult, setSelectedResult] = useState<string | undefined>();
  const [stretchMethod, setStretchMethod] = useState<StretchMethod>('asinh');
  const [black, setBlack] = useState(0);
  const [white, setWhite] = useState(4095);
  const [stretchSubmitting, setStretchSubmitting] = useState(false);
  const [previewVersion, setPreviewVersion] = useState(0);

  // Bug B fix: polling closures read the latest values from refs so the polling effect keeps stable deps
  // and no longer depends on jittery derived values such as activeJobIds / selectedId.
  const selectedIdRef = useRef<string | undefined>(selectedId);
  const activeJobIdsRef = useRef<string[]>([]);
  // Bug A fix: dedupe terminal job events by `job.id|status`, so a duplicate from WS and the 1s poll
  // reloads the detail / bumps the preview version only once and previewVersion is not incremented repeatedly.
  const processedTerminalEventsRef = useRef(new Set<string>());

  const loadSessions = useCallback(async () => {
    setSessionsLoading(true);
    try {
      const list = await apiGet<SessionSummary[]>('/api/sessions');
      setSessions(list);
      setSelectedId((prev) => prev ?? list[0]?.id);
      if (!list[0]?.id && !selectedIdRef.current) setDetailLoading(false);
    } catch {
      setSessions([]);
      if (!selectedIdRef.current) setDetailLoading(false);
    } finally {
      setSessionsLoading(false);
    }
  }, []);

  // Initial load is an inline promise chain (loading starts true) so the effect never calls a refresh that setStates synchronously
  useEffect(() => {
    let cancelled = false;
    apiGet<SessionSummary[]>('/api/sessions')
      .then((list) => {
        if (cancelled) return;
        setSessions(list);
        setSelectedId((prev) => prev ?? list[0]?.id);
        if (!list[0]?.id && !selectedIdRef.current) setDetailLoading(false);
      })
      .catch(() => {
        if (!cancelled) {
          setSessions([]);
          if (!selectedIdRef.current) setDetailLoading(false);
        }
      })
      .finally(() => {
        if (!cancelled) setSessionsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const loadDetail = useCallback(async (id: string) => {
    setDetailLoading(true);
    try {
      const data = await apiGet<SessionDetail>(`/api/sessions/${id}`);
      // Keep a slow response from landing after a session switch and restoring the old session's detail/exclusion state.
      if (selectedIdRef.current !== id) return;
      setDetail(data);
      setSelectedResult((prev) =>
        prev && data.results.some((r) => r.file === prev)
          ? prev
          : data.results[0]?.file,
      );
    } catch {
      setDetail(null);
    } finally {
      setDetailLoading(false);
    }
  }, []);

  // Load the detail inline when selectedId changes; the exclusion set is cleared in the Select onChange,
  // and excluded already starts as an empty Set, so the effect needs no setState.
  useEffect(() => {
    if (!selectedId) return;
    let cancelled = false;
    apiGet<SessionDetail>(`/api/sessions/${selectedId}`)
      .then((data) => {
        if (cancelled) return;
        setDetail(data);
        setSelectedResult((prev) =>
          prev && data.results.some((r) => r.file === prev)
            ? prev
            : data.results[0]?.file,
        );
      })
      .catch(() => {
        if (!cancelled) setDetail(null);
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  // Keep refs in sync with state so polling callbacks always read the latest selectedId / activeJobIds
  useEffect(() => {
    selectedIdRef.current = selectedId;
  }, [selectedId]);

  useEffect(() => {
    activeJobIdsRef.current = Object.values(jobs)
      .filter((j) => j.status === 'pending' || j.status === 'running')
      .map((j) => j.id)
      .filter(isValidJobId);
  }, [jobs]);

  /**
   * WS /ws/events and the 1s poll share one update entry point, so both sources merge into the same setJobs.
   * It depends only on the stable loadDetail, reads everything else from refs and is not rebuilt as selectedId/jobs change.
   */
  const handleJobUpdate = useCallback(
    (job: JobEventData) => {
      if (!isValidJobId(job.id)) return;
      setJobs((prev) => ({ ...prev, [job.id]: job }));
      if (job.status !== 'done' && job.status !== 'error') return;

      const terminalKey = `${job.id}|${job.status}`;
      if (processedTerminalEventsRef.current.has(terminalKey)) return;
      processedTerminalEventsRef.current.add(terminalKey);

      const selected = selectedIdRef.current;
      // When a job payload carries session_id, only the selected session's UI is refreshed/bumped to avoid cross-session triggers.
      if (job.session_id && job.session_id !== selected) return;
      if (selected) {
        const reload = loadDetail(selected);
        if (job.type === 'stretch' && job.status === 'done') {
          // After completion reload the detail first, then bump ?v= once the latest result list is in
          void reload.then(() => setPreviewVersion((v) => v + 1));
        }
      } else if (job.type === 'stretch' && job.status === 'done') {
        setPreviewVersion((v) => v + 1);
      }
    },
    [loadDetail],
  );

  const handleEvent = useCallback(
    (ev: MessageEvent) => {
      if (typeof ev.data !== 'string') return;
      try {
        const parsed = JSON.parse(ev.data) as { topic?: string; data?: unknown };
        if (parsed.topic === 'capture' && parsed.data) {
          setCaptureState(parsed.data as CaptureState);
        } else if (parsed.topic === 'job' && parsed.data) {
          handleJobUpdate(parsed.data as JobEventData);
        }
      } catch {
        /* ignore */
      }
    },
    [handleJobUpdate],
  );

  useReconnectingWebSocket('/ws/events', { enabled: true, onMessage: handleEvent });

  // Fall back to a 1s poll while a job is still pending/running.
  // The effect depends only on the stable handleJobUpdate; each tick reads the latest ids from activeJobIdsRef,
  // and once a job is done/error the jobs update removes it from activeJobIdsRef, which ends its polling.
  useEffect(() => {
    const timer = setInterval(() => {
      const ids = activeJobIdsRef.current.filter(isValidJobId);
      if (ids.length === 0) return;
      ids.forEach((id) => {
        void apiGet<JobEventData>(`/api/process/jobs/${id}`, { silent: true })
          .then((job) => handleJobUpdate(job))
          .catch(() => undefined);
      });
    }, 1000);
    return () => clearInterval(timer);
  }, [handleJobUpdate]);

  const frameKey = (type: PhaseType, file: string) => `${type}|${file}`;

  /** Checked = included (all checked by default); unchecked frames go into the exclude array of their phase */
  const toggleInclude = (type: PhaseType, file: string, checked: boolean) => {
    setExcluded((prev) => {
      const next = new Set(prev);
      if (checked) next.delete(frameKey(type, file));
      else next.add(frameKey(type, file));
      return next;
    });
  };

  /** Frame-set select all: checked = include the whole set; unchecked = put the whole set into exclude */
  const togglePhaseAll = (type: PhaseType, checked: boolean) => {
    const frames = detail?.frames?.[type] ?? [];
    setExcluded((prev) => {
      const next = new Set(prev);
      frames.forEach((f) => {
        const key = frameKey(type, f.file);
        if (checked) next.delete(key);
        else next.add(key);
      });
      return next;
    });
  };

  /** Shared by the calibrate / stack bodies: unchecked frames grouped by phase into exclude */
  const buildExclude = useCallback((): ExcludeMap => {
    const map: ExcludeMap = { bias: [], dark: [], flat: [], light: [] };
    if (!detail) return map;
    ALL_PHASE_TYPES.forEach((t) => {
      map[t] = (detail.frames?.[t] ?? [])
        .filter((f) => excluded.has(frameKey(t, f.file)))
        .map((f) => f.file);
    });
    return map;
  }, [detail, excluded]);

  const excludedCountTotal = useMemo(
    () => ALL_PHASE_TYPES.reduce((acc, t) => acc + (buildExclude()[t]?.length ?? 0), 0),
    [buildExclude],
  );

  /** Number of currently included (checked) frames per set, displayed in ALL_PHASE_TYPES order */
  const includedByPhase = useMemo(() => {
    const counts: Record<PhaseType, number> = { bias: 0, dark: 0, flat: 0, light: 0 };
    if (!detail) return counts;
    ALL_PHASE_TYPES.forEach((t) => {
      counts[t] = (detail.frames?.[t] ?? []).filter((f) => !excluded.has(frameKey(t, f.file))).length;
    });
    return counts;
  }, [detail, excluded]);

  const includedCountTotal = useMemo(
    () => ALL_PHASE_TYPES.reduce((acc, t) => acc + includedByPhase[t], 0),
    [includedByPhase],
  );

  const inclusionSummary = useMemo(
    () => ALL_PHASE_TYPES.map((t) => `${PHASE_EN_LABELS[t]} ${includedByPhase[t]}`).join(' / '),
    [includedByPhase],
  );

  const runCalibrate = async () => {
    if (!selectedId) {
      message.warning('Please select a Session first');
      return;
    }
    setCalSubmitting(true);
    try {
      const job = await apiPost<JobSubmitResult>('/api/process/calibrate', {
        session_id: selectedId,
        sigma: calSigma,
        method: calMethod,
        exclude: buildExclude(),
      });
      // The response field is job_id; normalize it into a JobEventData right away so no ghost pending row appears
      const event = toJobEvent(job, selectedId);
      setJobs((prev) => ({ ...prev, [event.id]: event }));
      message.success(`Calibrate job ${job.job_id} submitted (${excludedCountTotal} frames excluded)`);
    } catch {
      /* The API client already showed the error */
    } finally {
      setCalSubmitting(false);
    }
  };

  const runStack = async () => {
    if (!selectedId) {
      message.warning('Please select a Session first');
      return;
    }
    setStackSubmitting(true);
    try {
      const job = await apiPost<JobSubmitResult>('/api/process/stack', {
        session_id: selectedId,
        sigma: stackSigma,
        method: stackMethod,
        registration,
        exclude: buildExclude(),
      });
      const event = toJobEvent(job, selectedId);
      setJobs((prev) => ({ ...prev, [event.id]: event }));
      message.success(`Stack job ${job.job_id} submitted (${excludedCountTotal} frames excluded)`);
    } catch {
      /* The API client already showed the error */
    } finally {
      setStackSubmitting(false);
    }
  };

  const runStretch = async () => {
    if (!selectedId || !selectedResult) {
      message.warning('Please select a result file to rescale');
      return;
    }
    // Bug A fix (a): bump ?v= right after submitting so the image refreshes once even if WS/poll events are lost;
    // after completion handleJobUpdate bumps it once more, deduped by job.id|done.
    setPreviewVersion((v) => v + 1);
    setStretchSubmitting(true);
    try {
      const job = await apiPost<JobSubmitResult>('/api/process/stretch', {
        session_id: selectedId,
        result_file: selectedResult,
        stretch: stretchMethod,
        black,
        white,
      });
      const event = toJobEvent(job, selectedId);
      setJobs((prev) => ({ ...prev, [event.id]: event }));
    } catch {
      /* The API client already showed the error */
    } finally {
      setStretchSubmitting(false);
    }
  };

  /** Manual refresh preview: bump ?v= and reload the detail, the same path used after stretch completes */
  const refreshPreview = useCallback(() => {
    setPreviewVersion((v) => v + 1);
    const selected = selectedIdRef.current;
    if (selected) void loadDetail(selected);
  }, [loadDetail]);

  const availableFrames = useMemo(() => {
    if (!detail) return 0;
    const types: PhaseType[] = detail.phases.length > 0 ? detail.phases.map((p) => p.type) : ALL_PHASE_TYPES;
    return types.reduce((acc, t) => acc + ((detail.frames?.[t] ?? []).filter((f) => !excluded.has(frameKey(t, f.file))).length), 0);
  }, [detail, excluded]);

  // Frames actually written (not planned frames), so the denominator cannot be inflated after an abort/failure.
  const totalActualFrames = useMemo(() => {
    if (!detail) return 0;
    return ALL_PHASE_TYPES.reduce(
      (acc, t) => acc + ((detail.frames?.[t] ?? []).length),
      0,
    );
  }, [detail]);

  // Keep only the current session's jobs (a payload without session_id stays global, tolerating old backends)
  const jobsForSelectedSession = useMemo(() => {
    if (!selectedId) return [];
    return Object.values(jobs).filter(
      (job) => !job.session_id || job.session_id === selectedId,
    );
  }, [jobs, selectedId]);

  // Step progress follows the latest job of each type in the current session (a rerun overrides the old state);
  // without such a job it falls back to the persisted artifact marker, so a processed session still unlocks later steps after a refresh/switch.
  const latestJobsByType = useMemo(() => {
    const latest: Partial<Record<JobEventData['type'], JobEventData>> = {};
    jobsForSelectedSession.forEach((job) => {
      latest[job.type] = job;
    });
    return latest;
  }, [jobsForSelectedSession]);

  const calibrateJob = latestJobsByType.calibrate;
  const stackJob = latestJobsByType.stack;
  const stretchJob = latestJobsByType.stretch;

  const persistedCalibrateDone = Boolean(detail?.has_calibrated || detail?.has_masters);
  const persistedStackDone = Boolean(detail && detail.results.length > 0);
  const persistedStretchDone = Boolean(detail?.results.some((r) => r.preview));

  const calibrateDone = calibrateJob ? calibrateJob.status === 'done' : persistedCalibrateDone;
  const stackDone = stackJob ? stackJob.status === 'done' : persistedStackDone;
  const stretchDone = stretchJob ? stretchJob.status === 'done' : persistedStretchDone;
  const calibrateActive =
    calibrateJob?.status === 'pending' || calibrateJob?.status === 'running';
  const stackActive = stackJob?.status === 'pending' || stackJob?.status === 'running';
  const stretchActive = stretchJob?.status === 'pending' || stretchJob?.status === 'running';

  // Step current: calibrate done moves to Stack; stack done moves to Stretch; stretch done finishes all of them
  const stepCurrent = stretchDone ? 3 : stackDone ? 2 : calibrateDone ? 1 : 0;
  const stackHint = !calibrateDone
    ? 'Complete Step 1 · Construct Bias/Dark/Flat Master & Calibrate before running Stack.'
    : stackActive
      ? 'Stack job running…'
      : 'Calibrate completed. You can now run Stack.';
  const stretchHint = !stackDone
    ? 'Complete Step 2 · Stacking Light Field before running Rescale.'
    : stretchActive
      ? 'Rescale job running…'
      : 'Stack completed. You can apply Rescale and refresh the preview.';

  const selectedResultInfo = detail?.results.find((r) => r.file === selectedResult) ?? null;
  const previewUrl = selectedResultInfo
    ? `${resolveUrl(selectedResultInfo.preview)}?v=${previewVersion}`
    : '';

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Card
        size="small"
        title="Session Selection"
        extra={
          <Button icon={<ReloadOutlined />} loading={sessionsLoading} onClick={() => void loadSessions()}>
            Refresh List
          </Button>
        }
      >
        <Space wrap>
          <Select
            style={{ minWidth: 420 }}
            placeholder="Select a Session"
            loading={sessionsLoading}
            value={selectedId}
            onChange={(v) => {
              setSelectedId(v);
              setExcluded(new Set());
              setSelectedResult(undefined);
              setDetailLoading(true);
            }}
            options={sessions.map((s) => ({
              value: s.id,
              label: `${s.id}`,
            }))}
            notFoundContent={<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No sessions yet" />}
          />
          {sessions.map((s) => (
            <Tag key={s.id} color={s.has_results ? 'green' : 'default'}>
              {s.name} · bias {s.frame_counts.bias ?? 0} / dark {s.frame_counts.dark ?? 0} / flat {s.frame_counts.flat ?? 0} / light {s.frame_counts.light ?? 0}
              {s.has_results ? ' · has results' : ''}
            </Tag>
          ))}
        </Space>
        {detail && (
          <Descriptions size="small" column={3} style={{ marginTop: 12 }}>
            <Descriptions.Item label="Name">{detail.name}</Descriptions.Item>
            <Descriptions.Item label="Created">{detail.created}</Descriptions.Item>
            <Descriptions.Item label="Frames Included in Processing (unchecked go to exclude)">
              {availableFrames} / {totalActualFrames}
            </Descriptions.Item>
          </Descriptions>
        )}
      </Card>

      <Card
        size="small"
        title="Frame List (check = include in processing; unchecked frames go into the exclude array of their phase and are submitted with calibrate/stack — files are never deleted)"
        loading={detailLoading}
      >
        {!detail ? (
          <Empty description="Select a Session to show frames for each phase" />
        ) : (
          <Row gutter={16}>
            {(detail.phases.length > 0 ? detail.phases.map((p) => p.type) : (['bias', 'dark', 'flat', 'light'] as PhaseType[])).map((t) => {
              const frames = detail.frames?.[t] ?? [];
              const excludedCount = frames.filter((f) => excluded.has(frameKey(t, f.file))).length;
              return (
                <Col xs={24} md={12} lg={6} key={t}>
                  <Card
                    size="small"
                    type="inner"
                    title={`${PHASE_LABELS[t]} (${frames.length})`}
                    extra={
                      <Checkbox
                        checked={excludedCount === 0}
                        onChange={(e) => togglePhaseAll(t, e.target.checked)}
                      >
                        Select All
                      </Checkbox>
                    }
                  >
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      Planned {detail.phases.find((p) => p.type === t)?.count ?? 0} frames · Unchecked (will be excluded) {excludedCount}
                    </Typography.Text>
                    <List
                      size="small"
                      style={{ maxHeight: 320, overflow: 'auto' }}
                      dataSource={frames}
                      locale={{ emptyText: 'No frames' }}
                      renderItem={(f: FrameInfo) => (
                        <List.Item
                          key={f.file}
                          actions={[
                            <Checkbox
                              key="include"
                              checked={!excluded.has(frameKey(t, f.file))}
                              onChange={(e) => toggleInclude(t, f.file, e.target.checked)}
                            >
                              Include
                            </Checkbox>,
                          ]}
                        >
                          <Typography.Text
                            style={{
                              textDecoration: excluded.has(frameKey(t, f.file)) ? 'line-through' : undefined,
                            }}
                            title={`mean=${f.mean ?? '—'} std=${f.std ?? '—'} saturated=${f.saturated_pct ?? '—'}%`}
                          >
                            {f.file}
                          </Typography.Text>
                        </List.Item>
                      )}
                    />
                  </Card>
                </Col>
              );
            })}
          </Row>
        )}
      </Card>

      <Card size="small" title="Processing Pipeline (Calibrate → Stack → Rescale)">
        <Steps
          size="small"
          current={stepCurrent}
          items={[
            { title: 'Step 1 · Construct Bias/Dark/Flat Master & Calibrate', description: 'Build masters and calibrate each frame' },
            { title: 'Step 2 · Stacking Light Field', description: 'Stack + optional registration' },
            { title: 'Step 3 · Rescale & Preview', description: 'Rescale and refresh the preview' },
          ]}
          style={{ marginBottom: 16 }}
        />
        <Row gutter={16}>
          <Col xs={24} lg={8}>
            <Card size="small" type="inner" title="Step 1 · Construct Bias/Dark/Flat Master & Calibrate">
              <Space direction="vertical" style={{ width: '100%' }}>
                <Form layout="inline">
                  <Form.Item label="sigma">
                    <InputNumber min={1} max={10} step={0.5} value={calSigma} onChange={(v) => v !== null && setCalSigma(v)} />
                  </Form.Item>
                  <Form.Item label="method">
                    <Radio.Group
                      value={calMethod}
                      onChange={(e) => setCalMethod(e.target.value as ProcessMethod)}
                      options={[
                        { value: 'mean', label: 'mean' },
                        { value: 'median', label: 'median' },
                      ]}
                      optionType="button"
                    />
                  </Form.Item>
                </Form>
                <Button
                  type="primary"
                  loading={calSubmitting}
                  disabled={!selectedId || calibrateActive}
                  onClick={() => void runCalibrate()}
                >
                  Run Calibrate (Exclude Unchecked Frames)
                </Button>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  Included: {inclusionSummary} ({includedCountTotal} frames total)
                </Typography.Text>
              </Space>
            </Card>
          </Col>
          <Col xs={24} lg={8}>
            <Card size="small" type="inner" title="Step 2 · Stacking Light Field">
              <Space direction="vertical" style={{ width: '100%' }}>
                <Form layout="inline">
                  <Form.Item label="sigma">
                    <InputNumber min={1} max={10} step={0.5} value={stackSigma} onChange={(v) => v !== null && setStackSigma(v)} />
                  </Form.Item>
                  <Form.Item label="method">
                    <Radio.Group
                      value={stackMethod}
                      onChange={(e) => setStackMethod(e.target.value as ProcessMethod)}
                      options={[
                        { value: 'mean', label: 'mean' },
                        { value: 'median', label: 'median' },
                      ]}
                      optionType="button"
                    />
                  </Form.Item>
                </Form>
                <Checkbox checked={registration} onChange={(e) => setRegistration(e.target.checked)}>
                  Registration (FFT phase-correlation translation alignment)
                </Checkbox>
                <Button
                  type="primary"
                  loading={stackSubmitting}
                  disabled={!selectedId || !calibrateDone || stackActive}
                  onClick={() => void runStack()}
                >
                  Run Stack (Exclude Unchecked Frames)
                </Button>
                <Typography.Text type={calibrateDone ? 'secondary' : 'warning'} style={{ fontSize: 12 }}>
                  {stackHint}
                </Typography.Text>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  Included: {inclusionSummary} ({includedCountTotal} frames total)
                </Typography.Text>
              </Space>
            </Card>
          </Col>
          <Col xs={24} lg={8}>
            <Card size="small" type="inner" title="Step 3 · Rescale & Preview">
              <Space direction="vertical" style={{ width: '100%' }}>
                <Select
                  placeholder="Select result file"
                  style={{ width: '100%' }}
                  value={selectedResult}
                  onChange={setSelectedResult}
                  options={(detail?.results ?? []).map((r) => ({ value: r.file, label: r.file }))}
                />
                <Space wrap>
                  <span>Rescale: </span>
                  <Radio.Group
                    value={stretchMethod}
                    onChange={(e) => setStretchMethod(e.target.value as StretchMethod)}
                    options={[
                      { value: 'linear', label: 'linear' },
                      { value: 'asinh', label: 'asinh' },
                      { value: 'log', label: 'log' },
                    ]}
                    optionType="button"
                  />
                </Space>
                <div>
                  <Typography.Text>black: {black}</Typography.Text>
                  <Slider min={0} max={4095} step={1} value={black} onChange={setBlack} />
                  <Typography.Text>white: {white}</Typography.Text>
                  <Slider min={0} max={4095} step={1} value={white} onChange={setWhite} />
                </div>
                <Button
                  type="primary"
                  loading={stretchSubmitting}
                  disabled={!selectedResult || !stackDone || stretchActive}
                  onClick={() => void runStretch()}
                >
                  Apply Rescale & Refresh Preview
                </Button>
                <Typography.Text type={stackDone ? 'secondary' : 'warning'} style={{ fontSize: 12 }}>
                  {stretchHint}
                </Typography.Text>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  Rescales the stacked result to a displayable brightness range and produces a viewable preview image (8-bit PNG).
                </Typography.Text>
              </Space>
            </Card>
          </Col>
        </Row>
      </Card>

      <Card size="small" title="Job Progress (WS /ws/events + 1s polling GET /api/process/jobs/{id})">
        {captureState && captureState.status !== 'idle' && (
          <div style={{ marginBottom: 12 }}>
            <Tag color="geekblue">capture</Tag>
            <Space wrap>
              <Typography.Text>{captureState.session_name ?? ''}</Typography.Text>
              <Tag color="processing">{captureState.status}</Tag>
              <Typography.Text>
                Frame {captureState.frames_done}/{captureState.total_frames}
              </Typography.Text>
              {captureState.current_phase && (
                <Typography.Text>
                  {PHASE_LABELS[captureState.current_phase.type]} ({captureState.current_phase.index}/{captureState.current_phase.total})
                </Typography.Text>
              )}
            </Space>
            <Progress
              percent={captureState.total_frames > 0 ? Math.min(100, Math.round((captureState.frames_done / captureState.total_frames) * 100)) : 0}
              size="small"
              style={{ maxWidth: 480 }}
            />
          </div>
        )}
        {Object.keys(jobs).length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No job progress yet" />
        ) : (
          <Table<JobEventData>
            rowKey="id"
            size="small"
            pagination={false}
            dataSource={Object.values(jobs)}
            columns={[
              { title: 'Job ID', dataIndex: 'id' },
              { title: 'Type', dataIndex: 'type', render: (t: JobEventData['type']) => JOB_TYPE_LABELS[t] },
              {
                title: 'Status',
                dataIndex: 'status',
                render: (s: JobEventData['status']) => <Badge color={JOB_STATUS_META[s].color} text={JOB_STATUS_META[s].text} />,
              },
              {
                title: 'Progress',
                render: (_, job) => (
                  <Progress
                    percent={job.status === 'done' ? 100 : job.progress ?? 0}
                    size="small"
                    status={job.status === 'error' ? 'exception' : job.status === 'done' ? 'success' : 'active'}
                    style={{ width: 180 }}
                  />
                ),
              },
              { title: 'Message', dataIndex: 'message' },
            ]}
          />
        )}
      </Card>

      <Card size="small" title="Result Files (results/) & Preview">
        {!detail || detail.results.length === 0 ? (
          <Empty description="No result files yet (run Calibrate → Stack to produce them)" />
        ) : (
          <>
            <Table<{ file: string; url: string; preview: string }>
              rowKey="file"
              size="small"
              pagination={false}
              dataSource={detail.results}
              columns={[
                {
                  title: 'File',
                  dataIndex: 'file',
                  render: (file: string, record) => (
                    <Space direction="vertical" size={0}>
                      <Typography.Text strong>{file}</Typography.Text>
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        FITS: {record.url} · Preview: {record.preview}
                      </Typography.Text>
                    </Space>
                  ),
                },
                {
                  title: 'Actions',
                  width: 260,
                  render: (_, record) => (
                    <Space>
                      <Button size="small" href={resolveUrl(record.url)} target="_blank" icon={<DownloadOutlined />}>
                        Download FITS
                      </Button>
                      <Button size="small" href={resolveUrl(record.preview)} target="_blank">
                        Open Preview PNG
                      </Button>
                    </Space>
                  ),
                },
              ]}
            />
            {previewUrl && (
              <div style={{ marginTop: 12, textAlign: 'center' }}>
                <Space wrap>
                  <Typography.Text type="secondary">
                    Current Preview ({selectedResult}, auto-refreshes on Rescale submit / completion)
                  </Typography.Text>
                  <Button size="small" icon={<ReloadOutlined />} onClick={refreshPreview}>
                    Refresh Preview
                  </Button>
                </Space>
                <div style={{ background: '#000', padding: 8 }}>
                  <img
                    key={previewUrl}
                    src={previewUrl}
                    alt="Rescaled preview"
                    style={{ maxWidth: '100%', maxHeight: 560 }}
                    onLoad={(e) => {
                      // Fix preview visibility: reset the display:none set by onError once loading succeeds
                      (e.target as HTMLImageElement).style.display = '';
                    }}
                    onError={(e) => {
                      (e.target as HTMLImageElement).style.display = 'none';
                    }}
                  />
                </div>
              </div>
            )}
          </>
        )}
      </Card>

      {detail?.qc && (
        <Card size="small" title="QC Report">
          <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{JSON.stringify(detail.qc, null, 2)}</pre>
        </Card>
      )}
    </Space>
  );
}
