import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  InputNumber,
  Select,
  Space,
  Switch,
  Tag,
  Typography,
} from 'antd';
import { LockOutlined, ReloadOutlined, SlidersOutlined } from '@ant-design/icons';
import { apiGet, apiPut } from '../api/client';
import type { CameraProperty, PropertiesData, PropertySetResult } from '../api/types';

/** Write function for Parameters page controls: PUT /api/properties/{name}, returning { name, value, actual }. */
type PutFn = (
  name: string,
  value: number | boolean | string,
) => Promise<PropertySetResult | undefined>;

type VisibilityLevel = 'all' | 'Beginner' | 'Expert' | 'Guru';

const VISIBILITY_OPTIONS: { value: VisibilityLevel; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'Beginner', label: 'Beginner' },
  { value: 'Expert', label: 'Expert' },
  { value: 'Guru', label: 'Guru' },
];

// ------------------------------------------------------------------ helpers

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

/** Shared disable rule: readonly / locked / available=false all disable (string is shown read-only per readonly). */
function controlDisabled(prop: CameraProperty): boolean {
  return Boolean(prop.readonly) || prop.locked === true || prop.available === false;
}

/** INVISIBLE (case-insensitive, hidden accepted too) is hidden by default. */
function isInvisible(prop: CameraProperty): boolean {
  const v = (prop.visibility ?? '').trim().toLowerCase();
  return v === 'invisible' || v === 'hidden';
}

function normVisibility(prop: CameraProperty): string {
  return (prop.visibility ?? '').trim();
}

function displayValue(v: CameraProperty['value']): string {
  if (v === null || v === undefined) return '—';
  return typeof v === 'boolean' ? (v ? 'true' : 'false') : String(v);
}

function toNumber(v: CameraProperty['value']): number | null {
  if (v === null || v === undefined) return null;
  if (typeof v === 'number') return v;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

/** Derive the displayed float precision from step (handles scientific notation, e.g. 1e-9 -> 9 decimals). */
function decimalsOf(step: number | undefined): number {
  if (step === undefined || !Number.isFinite(step) || step <= 0) return 0;
  const [mantissa, expStr] = step.toExponential().split('e');
  const frac = (mantissa.split('.')[1] ?? '').length;
  const exp = Number(expStr);
  return Math.max(0, frac - exp);
}

/** Row summary: numeric rows show range/unit/step, enum rows show options, the rest show the type. */
function propSummary(prop: CameraProperty): string {
  const parts: string[] = [];
  if (prop.type === 'int' || prop.type === 'float') {
    const range: string[] = [];
    if (prop.min !== undefined) range.push(String(prop.min));
    if (prop.max !== undefined) range.push(String(prop.max));
    if (range.length > 0) parts.push(`Range ${range.join('–')}`);
    if (prop.unit) parts.push(prop.unit);
    if (prop.step !== undefined) parts.push(`step ${prop.step}`);
  } else if (prop.type === 'enum' && prop.options && prop.options.length > 0) {
    parts.push(prop.options.join(', '));
  } else if (prop.type === 'string') {
    parts.push('text');
  } else if (prop.type === 'command') {
    parts.push('command');
  }
  return parts.join(' · ');
}

function visibilityTag(visibility?: string) {
  if (!visibility) return null;
  const v = visibility.toLowerCase();
  const color =
    v === 'beginner' ? 'green' : v === 'expert' ? 'blue' : v === 'guru' ? 'purple' : 'default';
  return <Tag color={color}>{visibility}</Tag>;
}

/** Row key: name for commands (keeps the success/failure state), name:value otherwise so an actual write-back remounts in sync. */
function rowKey(p: CameraProperty): string {
  return p.type === 'command' ? p.name : `${p.name}:${String(p.value)}`;
}

// ------------------------------------------------------------------ typed controls

function NumericControl({ prop, onPut }: { prop: CameraProperty; onPut: PutFn }) {
  const [value, setValue] = useState<number | null>(() => toNumber(prop.value));
  const disabled = controlDisabled(prop);
  const isFloat = prop.type === 'float';
  const step = prop.step;
  const precision = isFloat ? (step !== undefined ? decimalsOf(step) : undefined) : 0;

  const commit = useCallback(
    async (v: number | null) => {
      if (v === null || !Number.isFinite(v)) {
        setValue(toNumber(prop.value));
        return;
      }
      try {
        await onPut(prop.name, v);
      } catch {
        // Write failed (the api client already showed the error): revert to the backend's current value
        setValue(toNumber(prop.value));
      }
    },
    [onPut, prop.name, prop.value],
  );

  return (
    <InputNumber
      value={value}
      min={prop.min}
      max={prop.max}
      step={step}
      precision={precision}
      addonAfter={prop.unit || undefined}
      disabled={disabled}
      style={{ width: '100%' }}
      onChange={(v) => setValue(v)}
      onBlur={() => void commit(value)}
      onPressEnter={(e) => e.currentTarget.blur()}
    />
  );
}

function BoolControl({ prop, onPut }: { prop: CameraProperty; onPut: PutFn }) {
  const [checked, setChecked] = useState<boolean>(() => Boolean(prop.value));
  const disabled = controlDisabled(prop);

  const commit = useCallback(
    async (v: boolean) => {
      setChecked(v);
      try {
        await onPut(prop.name, v);
      } catch {
        setChecked(Boolean(prop.value));
      }
    },
    [onPut, prop.name, prop.value],
  );

  return <Switch checked={checked} disabled={disabled} onChange={(v) => void commit(v)} />;
}

function EnumControl({ prop, onPut }: { prop: CameraProperty; onPut: PutFn }) {
  const options = prop.options ?? [];
  const [value, setValue] = useState<string>(() => (prop.value == null ? '' : String(prop.value)));
  const disabled = controlDisabled(prop);

  const commit = useCallback(
    async (v: string) => {
      setValue(v);
      try {
        await onPut(prop.name, v);
      } catch {
        setValue(prop.value == null ? '' : String(prop.value));
      }
    },
    [onPut, prop.name, prop.value],
  );

  if (options.length === 0) {
    return <Typography.Text>{displayValue(prop.value)}</Typography.Text>;
  }

  return (
    <Select
      value={value || undefined}
      options={options.map((o) => ({ value: o, label: o }))}
      disabled={disabled}
      style={{ width: '100%' }}
      onChange={(v) => void commit(v)}
    />
  );
}

function StringControl({ prop, onPut }: { prop: CameraProperty; onPut: PutFn }) {
  const [value, setValue] = useState<string>(() => (prop.value == null ? '' : String(prop.value)));
  const disabled = controlDisabled(prop);

  const commit = useCallback(
    async (v: string) => {
      try {
        await onPut(prop.name, v);
      } catch {
        setValue(prop.value == null ? '' : String(prop.value));
      }
    },
    [onPut, prop.name, prop.value],
  );

  if (prop.readonly) {
    return <Typography.Text>{displayValue(prop.value)}</Typography.Text>;
  }

  return (
    <Input
      value={value}
      disabled={disabled}
      onChange={(e) => setValue(e.target.value)}
      onBlur={() => {
        const current = prop.value == null ? '' : String(prop.value);
        if (value !== current) void commit(value);
      }}
      onPressEnter={(e) => e.currentTarget.blur()}
    />
  );
}

function CommandControl({ prop, onPut }: { prop: CameraProperty; onPut: PutFn }) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<'ok' | 'err' | null>(null);
  const disabled = controlDisabled(prop);

  const run = useCallback(async () => {
    setBusy(true);
    setResult(null);
    try {
      await onPut(prop.name, true);
      setResult('ok');
    } catch {
      setResult('err');
    } finally {
      setBusy(false);
    }
  }, [onPut, prop.name]);

  return (
    <Space>
      <Button loading={busy} disabled={disabled} onClick={() => void run()}>
        Execute
      </Button>
      {result === 'ok' && <Typography.Text type="success">executed</Typography.Text>}
      {result === 'err' && <Typography.Text type="danger">failed</Typography.Text>}
    </Space>
  );
}

function TextOnlyControl({ prop }: { prop: CameraProperty }) {
  return <Typography.Text>{displayValue(prop.value)}</Typography.Text>;
}

function PropertyRow({ prop, onPut }: { prop: CameraProperty; onPut: PutFn }) {
  const summary = propSummary(prop);
  const statusTag = prop.locked === true ? (
    <Tag color="orange" icon={<LockOutlined />}>
      Locked
    </Tag>
  ) : prop.readonly ? (
    <Tag color="default" icon={<LockOutlined />}>
      Read-only
    </Tag>
  ) : prop.available === false ? (
    <Tag color="red">Unavailable</Tag>
  ) : null;

  let control: React.ReactNode;
  switch (prop.type) {
    case 'int':
    case 'float':
      control = <NumericControl prop={prop} onPut={onPut} />;
      break;
    case 'bool':
      control = <BoolControl prop={prop} onPut={onPut} />;
      break;
    case 'enum':
      control = <EnumControl prop={prop} onPut={onPut} />;
      break;
    case 'string':
      control = <StringControl prop={prop} onPut={onPut} />;
      break;
    case 'command':
      control = <CommandControl prop={prop} onPut={onPut} />;
      break;
    default:
      control = <TextOnlyControl prop={prop} />;
  }

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 16,
        padding: '10px 0',
        borderBottom: '1px solid rgba(128, 128, 128, 0.15)',
      }}
    >
      <div style={{ flex: '1 1 40%', minWidth: 0 }}>
        <Space size={6} wrap>
          <Typography.Text strong>{prop.label}</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {prop.name}
          </Typography.Text>
          {visibilityTag(prop.visibility)}
          {statusTag}
        </Space>
        {summary && (
          <div>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {summary}
            </Typography.Text>
          </div>
        )}
      </div>
      <div style={{ flex: '0 1 320px', minWidth: 200 }}>{control}</div>
    </div>
  );
}

// ------------------------------------------------------------------ page

export default function ParametersPage() {
  const [properties, setProperties] = useState<CameraProperty[]>([]);
  const [loading, setLoading] = useState(true);
  // deviceOpen: false only after a 409 (device explicitly not open); true after a successful fetch;
  // other errors (500/network) stay null so an open device is not misreported as closed.
  const [deviceOpen, setDeviceOpen] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [visibility, setVisibility] = useState<VisibilityLevel>('all');
  const [showHidden, setShowHidden] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const data = await apiGet<PropertiesData>('/api/properties/all', { silent: true });
      setProperties(data.properties);
      setDeviceOpen(true);
      setError(null);
    } catch (err) {
      setProperties([]);
      if (httpStatus(err) === 409) {
        setDeviceOpen(false);
        setError(null);
      } else {
        setDeviceOpen(null);
        setError(httpErrorMessage(err));
      }
    } finally {
      setLoading(false);
    }
  }, []);

  // Initial load is an inline promise chain (loading starts true) so the effect never calls a refresh that setStates synchronously
  useEffect(() => {
    let cancelled = false;
    apiGet<PropertiesData>('/api/properties/all', { silent: true })
      .then((data) => {
        if (cancelled) return;
        setProperties(data.properties);
        setDeviceOpen(true);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setProperties([]);
        if (httpStatus(err) === 409) {
          setDeviceOpen(false);
          setError(null);
        } else {
          setDeviceOpen(null);
          setError(httpErrorMessage(err));
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
    async (
      name: string,
      value: number | boolean | string,
    ): Promise<PropertySetResult | undefined> => {
      const result = await apiPut<PropertySetResult>(`/api/properties/${name}`, { value });
      setProperties((prev) =>
        prev.map((p) => (p.name === name ? { ...p, value: result.actual } : p)),
      );
      return result;
    },
    [],
  );

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return properties.filter((p) => {
      const inv = isInvisible(p);
      if (inv) return showHidden; // INVISIBLE: shown only while Show hidden is on
      if (visibility !== 'all' && normVisibility(p).toLowerCase() !== visibility.toLowerCase()) {
        return false;
      }
      if (q && !`${p.name} ${p.label}`.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [properties, search, visibility, showHidden]);

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Card
        size="small"
        title={
          <Space>
            <SlidersOutlined />
            Parameters
            <Typography.Text type="secondary" style={{ fontWeight: 'normal', fontSize: 12 }}>
              Controls all camera parameters; changes are written immediately and the returned
              actual value is shown
            </Typography.Text>
          </Space>
        }
        extra={
          <Button icon={<ReloadOutlined />} onClick={() => void refresh()} loading={loading}>
            Refresh
          </Button>
        }
      >
        <Space wrap style={{ width: '100%', justifyContent: 'space-between' }}>
          <Space wrap>
            <Input.Search
              allowClear
              placeholder="Search by name or label"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              style={{ width: 260 }}
            />
            <Select
              value={visibility}
              onChange={setVisibility}
              options={VISIBILITY_OPTIONS}
              style={{ width: 140 }}
            />
            <Space size={6}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                Show hidden
              </Typography.Text>
              <Switch size="small" checked={showHidden} onChange={setShowHidden} />
            </Space>
          </Space>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {filtered.length} / {properties.length} shown
          </Typography.Text>
        </Space>
      </Card>

      <Card size="small" loading={loading}>
        {loading ? null : deviceOpen === false ? (
          <Alert
            type="warning"
            showIcon
            message="No device open, cannot load parameters"
            description='Please open a device on the "Connect" page first (GET /api/properties/all returns 409 when no device is open).'
          />
        ) : error ? (
          <Alert type="error" showIcon message="Failed to load camera parameters" description={error} />
        ) : properties.length === 0 ? (
          <Alert
            type="info"
            showIcon
            message="Camera is open but reports no parameters"
            description="The camera did not expose any parameters. Check the backend log for skipped property warnings."
          />
        ) : filtered.length === 0 ? (
          <Empty description="No parameters match the current filter" />
        ) : (
          <div>
            {filtered.map((p) => (
              <PropertyRow key={rowKey(p)} prop={p} onPut={putProperty} />
            ))}
          </div>
        )}
      </Card>
    </Space>
  );
}
