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
  bias: '偏置场 Bias',
  dark: '暗场 Dark',
  flat: '平场 Flat',
  light: '亮场 Light',
};

const ALL_PHASE_TYPES: PhaseType[] = ['bias', 'dark', 'flat', 'light'];

const JOB_TYPE_LABELS: Record<JobEventData['type'], string> = {
  calibrate: '标定（Master 合成）',
  stack: '叠加',
  stretch: '拉伸预览',
};

const JOB_STATUS_META: Record<JobEventData['status'], { color: string; text: string }> = {
  pending: { color: 'default', text: '排队中' },
  running: { color: 'processing', text: '运行中' },
  done: { color: 'success', text: '完成' },
  error: { color: 'error', text: '失败' },
};

/** 只轮询合法 job_id，避免 undefined 之类的幽灵 id 打 GET /api/process/jobs/undefined */
const isValidJobId = (id: string): boolean => typeof id === 'string' && id.length > 0 && id !== 'undefined';

/** POST /api/process/* 响应是 {job_id,...}，统一归一成 jobs 表使用的 JobEventData（字段名 id） */
const toJobEvent = (submitted: JobSubmitResult): JobEventData => ({
  id: submitted.job_id,
  type: submitted.type,
  status: submitted.status,
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

  // Bug B 修复：轮询闭包一律通过 ref 读取最新值，轮询 effect 的依赖保持稳定，
  // 不再依赖 activeJobIds / selectedId 等会抖动的派生值。
  const selectedIdRef = useRef<string | undefined>(selectedId);
  const activeJobIdsRef = useRef<string[]>([]);
  // Bug A 修复：对 terminal job 事件按 `job.id|status` 去重，WS 与 1s 轮询重复送达时
  // 只 reload 详情 / 自增预览版本一次，防止 previewVersion 被反复累加。
  const processedTerminalEventsRef = useRef(new Set<string>());

  const loadSessions = useCallback(async () => {
    setSessionsLoading(true);
    try {
      const list = await apiGet<SessionSummary[]>('/api/sessions');
      setSessions(list);
      setSelectedId((prev) => prev ?? list[0]?.id);
    } catch {
      setSessions([]);
    } finally {
      setSessionsLoading(false);
    }
  }, []);

  // 初始加载内联成 promise 链（loading 初值即 true），不在 effect 里同步调用会 setState 的刷新函数
  useEffect(() => {
    let cancelled = false;
    apiGet<SessionSummary[]>('/api/sessions')
      .then((list) => {
        if (cancelled) return;
        setSessions(list);
        setSelectedId((prev) => prev ?? list[0]?.id);
      })
      .catch(() => {
        if (!cancelled) setSessions([]);
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
      setDetail(data);
      setSelectedResult((prev) => prev ?? data.results[0]?.file);
    } catch {
      setDetail(null);
    } finally {
      setDetailLoading(false);
    }
  }, []);

  // selectedId 变化时内联加载详情；手动切换时由 Select onChange 置 detailLoading=true
  useEffect(() => {
    if (!selectedId) return;
    let cancelled = false;
    apiGet<SessionDetail>(`/api/sessions/${selectedId}`)
      .then((data) => {
        if (cancelled) return;
        setDetail(data);
        setSelectedResult((prev) => prev ?? data.results[0]?.file);
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

  // ref 与 state 同步：轮询回调始终读到最新的 selectedId / activeJobIds
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
   * WS /ws/events 与 1s 轮询共用同一个更新入口，保证两种来源合并进同一个 setJobs。
   * 只依赖稳定的 loadDetail，闭包内全部读 ref，不随 selectedId/jobs 变化重建。
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
      if (selected) {
        const reload = loadDetail(selected);
        if (job.type === 'stretch' && job.status === 'done') {
          // 完成后先重新加载详情，拿到最新结果列表后再自增 ?v= 刷新预览
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

  // 对仍处于 pending/running 的 job 每 1s 轮询兜底。
  // effect 依赖只有稳定的 handleJobUpdate；每次 tick 从 activeJobIdsRef 读最新 id，
  // job 变为 done/error 后 jobs 更新 → activeJobIdsRef 同步剔除 → 自然停止该 job 的轮询。
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

  /** 勾选 = 纳入（默认全选）；取消勾选的帧进入对应相位的 exclude 数组 */
  const toggleInclude = (type: PhaseType, file: string, checked: boolean) => {
    setExcluded((prev) => {
      const next = new Set(prev);
      if (checked) next.delete(frameKey(type, file));
      else next.add(frameKey(type, file));
      return next;
    });
  };

  /** calibrate / stack 请求体统一携带：未勾选帧按相位分组进 exclude */
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

  const runCalibrate = async () => {
    if (!selectedId) {
      message.warning('请先选择 Session');
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
      // 提交响应字段是 job_id，立即归一成 JobEventData 写入 jobs，避免幽灵 pending 行
      const event = toJobEvent(job);
      setJobs((prev) => ({ ...prev, [event.id]: event }));
      message.success(`已提交标定任务 ${job.job_id}（exclude 共 ${excludedCountTotal} 帧）`);
    } finally {
      setCalSubmitting(false);
    }
  };

  const runStack = async () => {
    if (!selectedId) {
      message.warning('请先选择 Session');
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
      const event = toJobEvent(job);
      setJobs((prev) => ({ ...prev, [event.id]: event }));
      message.success(`已提交叠加任务 ${job.job_id}（exclude 共 ${excludedCountTotal} 帧）`);
    } finally {
      setStackSubmitting(false);
    }
  };

  const runStretch = async () => {
    if (!selectedId || !selectedResult) {
      message.warning('请选择要拉伸的结果文件');
      return;
    }
    // Bug A 修复 (a)：提交时立即自增 ?v=，即使 WS/轮询事件丢失，图片也会先强制刷新一次；
    // 任务完成后 handleJobUpdate 会再按 job.id|done 去重自增一次。
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
      const event = toJobEvent(job);
      setJobs((prev) => ({ ...prev, [event.id]: event }));
    } finally {
      setStretchSubmitting(false);
    }
  };

  /** 手动「刷新预览」：自增 ?v= 并重新加载详情，与 stretch 完成后的刷新路径一致 */
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

  // 步骤条推进：以「最新一次对应类型 job」的完成状态为准（rerun 后以新任务覆盖旧状态）
  const latestJobsByType = useMemo(() => {
    const latest: Partial<Record<JobEventData['type'], JobEventData>> = {};
    Object.values(jobs).forEach((job) => {
      latest[job.type] = job;
    });
    return latest;
  }, [jobs]);

  const calibrateJob = latestJobsByType.calibrate;
  const stackJob = latestJobsByType.stack;
  const stretchJob = latestJobsByType.stretch;
  const calibrateDone = calibrateJob?.status === 'done';
  const stackDone = stackJob?.status === 'done';
  const stretchDone = stretchJob?.status === 'done';
  const calibrateActive =
    calibrateJob?.status === 'pending' || calibrateJob?.status === 'running';
  const stackActive = stackJob?.status === 'pending' || stackJob?.status === 'running';
  const stretchActive = stretchJob?.status === 'pending' || stretchJob?.status === 'running';

  // 步骤条 current：calibrate 完成后进入 Stack 步；stack 完成后进入 Stretch 步；stretch 完成则全部完成
  const stepCurrent = stretchDone ? 3 : stackDone ? 2 : calibrateDone ? 1 : 0;
  const stackHint = !calibrateDone
    ? '请先完成步骤 1 · Calibrate，再运行 Stack。'
    : stackActive
      ? 'Stack 任务运行中…'
      : 'Calibrate 已完成，建议运行 Stack。';
  const stretchHint = !stackDone
    ? '请先完成步骤 2 · Stack，再运行 Stretch。'
    : stretchActive
      ? 'Stretch 任务运行中…'
      : 'Stack 已完成，可以应用 Stretch 并刷新预览。';

  const selectedResultInfo = detail?.results.find((r) => r.file === selectedResult) ?? null;
  const previewUrl = selectedResultInfo
    ? `${resolveUrl(selectedResultInfo.preview)}?v=${previewVersion}`
    : '';

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Card
        size="small"
        title="Session 选择"
        extra={
          <Button icon={<ReloadOutlined />} loading={sessionsLoading} onClick={() => void loadSessions()}>
            刷新列表
          </Button>
        }
      >
        <Space wrap>
          <Select
            style={{ minWidth: 420 }}
            placeholder="选择 Session"
            loading={sessionsLoading}
            value={selectedId}
            onChange={(v) => {
              setSelectedId(v);
              setDetailLoading(true);
            }}
            options={sessions.map((s) => ({
              value: s.id,
              label: `${s.id}`,
            }))}
            notFoundContent={<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无 Session" />}
          />
          {sessions.map((s) => (
            <Tag key={s.id} color={s.has_results ? 'green' : 'default'}>
              {s.name} · bias {s.frame_counts.bias ?? 0} / dark {s.frame_counts.dark ?? 0} / flat {s.frame_counts.flat ?? 0} / light {s.frame_counts.light ?? 0}
              {s.has_results ? ' · 有结果' : ''}
            </Tag>
          ))}
        </Space>
        {detail && (
          <Descriptions size="small" column={3} style={{ marginTop: 12 }}>
            <Descriptions.Item label="名称">{detail.name}</Descriptions.Item>
            <Descriptions.Item label="创建时间">{detail.created}</Descriptions.Item>
            <Descriptions.Item label="纳入处理帧数（未勾选将进 exclude）">
              {availableFrames} / {detail.phases.reduce((a, p) => a + p.count, 0)}
            </Descriptions.Item>
          </Descriptions>
        )}
      </Card>

      <Card
        size="small"
        title="帧列表（勾选 = 纳入处理；未勾选帧将进入对应相位的 exclude 数组随 calibrate/stack 提交，不会删除文件）"
        loading={detailLoading}
      >
        {!detail ? (
          <Empty description="选择 Session 后显示各相位帧" />
        ) : (
          <Row gutter={16}>
            {(detail.phases.length > 0 ? detail.phases.map((p) => p.type) : (['bias', 'dark', 'flat', 'light'] as PhaseType[])).map((t) => {
              const frames = detail.frames?.[t] ?? [];
              const excludedCount = frames.filter((f) => excluded.has(frameKey(t, f.file))).length;
              return (
                <Col xs={24} md={12} lg={6} key={t}>
                  <Card size="small" type="inner" title={`${PHASE_LABELS[t]}（${frames.length}）`}>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      计划 {detail.phases.find((p) => p.type === t)?.count ?? 0} 张 · 未勾选（将排除）{excludedCount}
                    </Typography.Text>
                    <List
                      size="small"
                      style={{ maxHeight: 320, overflow: 'auto' }}
                      dataSource={frames}
                      locale={{ emptyText: '暂无帧' }}
                      renderItem={(f: FrameInfo) => (
                        <List.Item
                          key={f.file}
                          actions={[
                            <Checkbox
                              key="include"
                              checked={!excluded.has(frameKey(t, f.file))}
                              onChange={(e) => toggleInclude(t, f.file, e.target.checked)}
                            >
                              纳入
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

      <Card size="small" title="处理管线（Calibrate → Stack → Stretch）">
        <Steps
          size="small"
          current={stepCurrent}
          items={[
            { title: '步骤 1：Calibrate', description: '合成 Master 并逐帧标定' },
            { title: '步骤 2：Stack', description: '叠加 + 可选配准' },
            { title: '步骤 3：Stretch', description: '拉伸并刷新预览' },
          ]}
          style={{ marginBottom: 16 }}
        />
        <Row gutter={16}>
          <Col xs={24} lg={8}>
            <Card size="small" type="inner" title="步骤 1 · Calibrate（sigma-clipped master）">
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
                  运行 Calibrate（排除未勾选帧）
                </Button>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  请求体含 exclude：bias {buildExclude().bias.length} / dark {buildExclude().dark.length} / flat {buildExclude().flat.length} / light {buildExclude().light.length}
                </Typography.Text>
              </Space>
            </Card>
          </Col>
          <Col xs={24} lg={8}>
            <Card size="small" type="inner" title="步骤 2 · Stack">
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
                  配准（registration，FFT 相位相关平移对齐）
                </Checkbox>
                <Button
                  type="primary"
                  loading={stackSubmitting}
                  disabled={!selectedId || !calibrateDone || stackActive}
                  onClick={() => void runStack()}
                >
                  运行 Stack（排除未勾选帧）
                </Button>
                <Typography.Text type={calibrateDone ? 'secondary' : 'warning'} style={{ fontSize: 12 }}>
                  {stackHint}
                </Typography.Text>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  请求体含 exclude：bias {buildExclude().bias.length} / dark {buildExclude().dark.length} / flat {buildExclude().flat.length} / light {buildExclude().light.length}
                </Typography.Text>
              </Space>
            </Card>
          </Col>
          <Col xs={24} lg={8}>
            <Card size="small" type="inner" title="步骤 3 · Stretch">
              <Space direction="vertical" style={{ width: '100%' }}>
                <Select
                  placeholder="选择结果文件"
                  style={{ width: '100%' }}
                  value={selectedResult}
                  onChange={setSelectedResult}
                  options={(detail?.results ?? []).map((r) => ({ value: r.file, label: r.file }))}
                />
                <Space wrap>
                  <span>stretch：</span>
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
                  <Typography.Text>black：{black}</Typography.Text>
                  <Slider min={0} max={4095} step={1} value={black} onChange={setBlack} />
                  <Typography.Text>white：{white}</Typography.Text>
                  <Slider min={0} max={4095} step={1} value={white} onChange={setWhite} />
                </div>
                <Button
                  type="primary"
                  loading={stretchSubmitting}
                  disabled={!selectedResult || !stackDone || stretchActive}
                  onClick={() => void runStretch()}
                >
                  应用拉伸并刷新预览
                </Button>
                <Typography.Text type={stackDone ? 'secondary' : 'warning'} style={{ fontSize: 12 }}>
                  {stretchHint}
                </Typography.Text>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  拉伸输出覆盖 results/STACK_preview.png（8-bit PNG）。后端返回 Cache-Control: no-store，前端通过 ?v= 版本参数强制刷新。
                </Typography.Text>
              </Space>
            </Card>
          </Col>
        </Row>
      </Card>

      <Card size="small" title="任务进度（WS /ws/events + 1s 轮询 GET /api/process/jobs/{id}）">
        {captureState && captureState.status !== 'idle' && (
          <div style={{ marginBottom: 12 }}>
            <Tag color="geekblue">capture</Tag>
            <Space wrap>
              <Typography.Text>{captureState.session_name ?? ''}</Typography.Text>
              <Tag color="processing">{captureState.status}</Tag>
              <Typography.Text>
                帧 {captureState.frames_done}/{captureState.total_frames}
              </Typography.Text>
              {captureState.current_phase && (
                <Typography.Text>
                  {PHASE_LABELS[captureState.current_phase.type]}（{captureState.current_phase.index + 1}/{captureState.current_phase.total}）
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
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无任务进度" />
        ) : (
          <Table<JobEventData>
            rowKey="id"
            size="small"
            pagination={false}
            dataSource={Object.values(jobs)}
            columns={[
              { title: 'Job ID', dataIndex: 'id' },
              { title: '类型', dataIndex: 'type', render: (t: JobEventData['type']) => JOB_TYPE_LABELS[t] },
              {
                title: '状态',
                dataIndex: 'status',
                render: (s: JobEventData['status']) => <Badge color={JOB_STATUS_META[s].color} text={JOB_STATUS_META[s].text} />,
              },
              {
                title: '进度',
                render: (_, job) => (
                  <Progress
                    percent={job.status === 'done' ? 100 : job.progress ?? 0}
                    size="small"
                    status={job.status === 'error' ? 'exception' : job.status === 'done' ? 'success' : 'active'}
                    style={{ width: 180 }}
                  />
                ),
              },
              { title: '消息', dataIndex: 'message' },
            ]}
          />
        )}
      </Card>

      <Card size="small" title="结果文件（results/）与预览">
        {!detail || detail.results.length === 0 ? (
          <Empty description="暂无结果文件（运行 Calibrate → Stack 后出现）" />
        ) : (
          <>
            <Table<{ file: string; url: string; preview: string }>
              rowKey="file"
              size="small"
              pagination={false}
              dataSource={detail.results}
              columns={[
                {
                  title: '文件',
                  dataIndex: 'file',
                  render: (file: string, record) => (
                    <Space direction="vertical" size={0}>
                      <Typography.Text strong>{file}</Typography.Text>
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        FITS：{record.url} · 预览：{record.preview}
                      </Typography.Text>
                    </Space>
                  ),
                },
                {
                  title: '操作',
                  width: 260,
                  render: (_, record) => (
                    <Space>
                      <Button size="small" href={resolveUrl(record.url)} target="_blank" icon={<DownloadOutlined />}>
                        下载 FITS
                      </Button>
                      <Button size="small" href={resolveUrl(record.preview)} target="_blank">
                        打开预览 PNG
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
                    当前预览（{selectedResult}，stretch 提交 / 完成后自动刷新，?v={previewVersion}）
                  </Typography.Text>
                  <Button size="small" icon={<ReloadOutlined />} onClick={refreshPreview}>
                    刷新预览
                  </Button>
                </Space>
                <div style={{ background: '#000', padding: 8 }}>
                  <img
                    key={previewUrl}
                    src={previewUrl}
                    alt="stretch preview"
                    style={{ maxWidth: '100%', maxHeight: 560 }}
                    onLoad={(e) => {
                      // 修复预览可见性：成功后复位 onError 设置过的 display:none
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
        <Card size="small" title="QC 报告">
          <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{JSON.stringify(detail.qc, null, 2)}</pre>
        </Card>
      )}
    </Space>
  );
}
