import { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Badge,
  Button,
  Card,
  Col,
  Descriptions,
  Input,
  InputNumber,
  Row,
  Space,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  CheckCircleOutlined,
  CloudServerOutlined,
  DatabaseOutlined,
  FolderOpenOutlined,
  ReloadOutlined,
  SaveOutlined,
} from '@ant-design/icons';
import { apiGet, apiPost, apiPut } from '../api/client';
import { clearBackend, DEFAULT_BACKEND, getBackend, normalizeBackend, setBackend } from '../api/backend';
import {
  DEFAULT_PREVIEW,
  getPreviewSettings,
  normalizePreviewSettings,
  setPreviewSettings,
  type PreviewSettings,
} from '../api/settings';
import type { DataDirSettings, HealthInfo, PickFolderResult } from '../api/types';

type HealthStatus = 'checking' | 'ok' | 'error';

export default function SettingsPage() {
  const [backendInput, setBackendInput] = useState<string>(getBackend());
  const [preview, setPreview] = useState<PreviewSettings>(getPreviewSettings());
  const [health, setHealth] = useState<HealthInfo | null>(null);
  const [healthStatus, setHealthStatus] = useState<HealthStatus>('checking');
  const [healthError, setHealthError] = useState('');
  const [dataDir, setDataDir] = useState<DataDirSettings | null>(null);
  const [dataRootInput, setDataRootInput] = useState('');
  const [dataDirLoading, setDataDirLoading] = useState(true);
  const [dataDirSaving, setDataDirSaving] = useState(false);
  const [dataDirPicking, setDataDirPicking] = useState(false);
  const [dataDirError, setDataDirError] = useState('');

  const loadDataDir = useCallback(async () => {
    setDataDirLoading(true);
    setDataDirError('');
    try {
      const settings = await apiGet<DataDirSettings>('/api/settings', { silent: true });
      setDataDir(settings);
      setDataRootInput(settings.data_root);
    } catch (err) {
      setDataDir(null);
      setDataDirError(err instanceof Error ? err.message : 'Unable to load data directory settings');
    } finally {
      setDataDirLoading(false);
    }
  }, []);

  const saveDataDir = async () => {
    const dataRoot = dataRootInput.trim();
    if (!dataRoot) {
      message.warning('Please enter the absolute path of the data directory');
      return;
    }
    setDataDirSaving(true);
    setDataDirError('');
    try {
      const next = await apiPut<DataDirSettings>('/api/settings', { data_root: dataRoot });
      setDataDir(next);
      setDataRootInput(next.data_root);
      message.success(`Data directory saved: ${next.data_root}`);
    } catch (err) {
      // 400 等失败时展示后端 error（apiPut 非 silent 已同时弹出 message.error）
      setDataDirError(err instanceof Error ? err.message : 'Failed to save. Please check the error returned by the backend');
    } finally {
      setDataDirSaving(false);
    }
  };

  const pickDataDir = async () => {
    setDataDirPicking(true);
    setDataDirError('');
    try {
      const result = await apiPost<PickFolderResult>(
        '/api/pick-folder',
        { title: 'Select Data Directory' },
        { silent: true },
      );
      if (result.path) {
        setDataRootInput(result.path);
      } else {
        message.warning('Folder selection cancelled');
      }
    } catch (err) {
      setDataDirError(err instanceof Error ? err.message : 'Unable to open native folder picker');
    } finally {
      setDataDirPicking(false);
    }
  };

  const checkHealth = async (targetBackend = backendInput) => {
    setHealthStatus('checking');
    setHealthError('');
    try {
      // 直接请求「目标后端地址」，走统一 axios 封装（silent 不弹错误提示，由本页展示状态）
      const data = await apiGet<HealthInfo>('/api/health', {
        baseURL: normalizeBackend(targetBackend),
        timeout: 5000,
        silent: true,
      });
      setHealth(data);
      setHealthStatus('ok');
    } catch (err) {
      setHealth(null);
      setHealthError(err instanceof Error ? err.message : 'Unable to connect to the backend service');
      setHealthStatus('error');
    }
  };

  useEffect(() => {
    void checkHealth();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 数据目录：加载时 GET /api/settings
  useEffect(() => {
    void loadDataDir();
  }, [loadDataDir]);

  const saveBackend = () => {
    const normalized = normalizeBackend(backendInput);
    setBackend(normalized);
    setBackendInput(normalized);
    message.success(`Backend address saved: ${normalized} (takes effect immediately for WS / image URLs; REST goes through the Vite proxy in dev)`);
    void checkHealth(normalized);
  };

  const savePreview = () => {
    const next = normalizePreviewSettings(preview);
    setPreviewSettings(next);
    setPreview(next);
    message.success('Preview settings saved (fps/quality rounded, scale snapped to 0.25/0.5/1.0). Takes effect when you re-enter /live or refresh the page');
  };

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card
        title={
          <Space>
            <DatabaseOutlined />
            Data Directory
          </Space>
        }
        loading={dataDirLoading}
      >
        <Row gutter={16} align="middle">
          <Col flex="auto">
            <Input
              size="large"
              value={dataRootInput}
              onChange={(e) => setDataRootInput(e.target.value)}
              placeholder="Enter the absolute path of the data directory, e.g. /Users/me/astro-data"
              addonBefore="PATH"
            />
          </Col>
          <Col>
            <Space>
              <Button
                icon={<FolderOpenOutlined />}
                loading={dataDirPicking}
                onClick={() => void pickDataDir()}
              >
                Choose…
              </Button>
              <Button
                type="primary"
                icon={<SaveOutlined />}
                loading={dataDirSaving}
                onClick={() => void saveDataDir()}
              >
                Save
              </Button>
              <Button icon={<ReloadOutlined />} onClick={() => void loadDataDir()}>
                Reload
              </Button>
            </Space>
          </Col>
        </Row>
        {dataDir && (
          <Descriptions bordered size="small" column={2} style={{ marginTop: 12 }}>
            <Descriptions.Item label="data_root">{dataDir.data_root}</Descriptions.Item>
            <Descriptions.Item label="writable">
              <Tag color={dataDir.writable ? 'green' : 'red'}>{dataDir.writable ? 'true (writable)' : 'false (read-only)'}</Tag>
            </Descriptions.Item>
          </Descriptions>
        )}
        {dataDirError && (
          <Alert
            style={{ marginTop: 12 }}
            type="error"
            showIcon
            message="Data directory settings operation failed"
            description={dataDirError}
          />
        )}
        <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
          After changing, new captures are written to the new directory while old data stays in the original
          directory. A successful save is confirmed by the data_root / writable values returned from PUT /api/settings;
          an invalid path or directory creation failure returns a 400 error from the backend.
        </Typography.Paragraph>
      </Card>

      <Card title="Preview Parameters (used by /live)">
        <Row gutter={24}>
          <Col xs={24} md={8}>
            <Typography.Text strong>FPS (≤30)</Typography.Text>
            <InputNumber
              min={1}
              max={30}
              step={1}
              precision={0}
              value={preview.fps}
              style={{ width: '100%' }}
              onChange={(v) => v !== null && setPreview((p) => ({ ...p, fps: v }))}
            />
          </Col>
          <Col xs={24} md={8}>
            <Typography.Text strong>JPEG Quality (10–100, integer)</Typography.Text>
            <InputNumber
              min={10}
              max={100}
              step={1}
              precision={0}
              value={preview.quality}
              style={{ width: '100%' }}
              onChange={(v) => v !== null && setPreview((p) => ({ ...p, quality: v }))}
            />
          </Col>
          <Col xs={24} md={8}>
            <Typography.Text strong>Scale (0.25 / 0.5 / 1.0)</Typography.Text>
            <InputNumber
              min={0.25}
              max={1}
              step={0.25}
              value={preview.scale}
              style={{ width: '100%' }}
              onChange={(v) => v !== null && setPreview((p) => ({ ...p, scale: v }))}
            />
          </Col>
        </Row>
        <Button type="primary" icon={<SaveOutlined />} style={{ marginTop: 16 }} onClick={savePreview}>
          Save Preview Settings
        </Button>
        <Typography.Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0 }}>
          Defaults: fps={DEFAULT_PREVIEW.fps}, quality={DEFAULT_PREVIEW.quality}, scale={DEFAULT_PREVIEW.scale}.
          These parameters are sent to the backend via POST /api/live/start when entering /live.
        </Typography.Paragraph>
      </Card>

      <Card
        title={
          <Space>
            <CloudServerOutlined />
            Backend Address
          </Space>
        }
      >
        <Row gutter={16} align="middle">
          <Col flex="auto">
            <Input
              size="large"
              value={backendInput}
              onChange={(e) => setBackendInput(e.target.value)}
              placeholder={DEFAULT_BACKEND}
              addonBefore="HTTP"
            />
          </Col>
          <Col>
            <Space>
              <Button type="primary" icon={<SaveOutlined />} onClick={saveBackend}>
                Save (persisted in localStorage)
              </Button>
              <Button
                onClick={() => {
                  // 恢复默认 = 清除 localStorage 覆盖，让 VITE_BACKEND env / 默认值按优先级自然生效
                  clearBackend();
                  const effective = getBackend();
                  setBackendInput(effective);
                  message.success(`Local backend override cleared. Effective address: ${effective}`);
                  void checkHealth(effective);
                }}
              >
                Restore Default (does not clear env injection)
              </Button>
            </Space>
          </Col>
        </Row>
        <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
          Effective address: {getBackend()}. In dev mode REST requests go through the Vite proxy (target determined
          at startup by VITE_BACKEND / the default localhost:8000); the WS live stream and image/file URLs use the
          address saved here directly, so changes take effect immediately. In production, FastAPI serves frontend/dist
          and everything is same-origin.
        </Typography.Paragraph>
      </Card>

      <Card title="Backend Health">
        <Space style={{ marginBottom: 12 }}>
          <Badge
            status={healthStatus === 'ok' ? 'success' : healthStatus === 'error' ? 'error' : 'processing'}
            text={healthStatus === 'ok' ? 'Healthy' : healthStatus === 'error' ? 'Unhealthy / Unreachable' : 'Checking…'}
          />
          <Button icon={<ReloadOutlined />} onClick={() => void checkHealth()}>
            Recheck
          </Button>
        </Space>
        {healthStatus === 'ok' && health ? (
          <Descriptions bordered size="small" column={2}>
            <Descriptions.Item label="status">
              <Tag color="green" icon={<CheckCircleOutlined />}>
                {health.status}
              </Tag>
            </Descriptions.Item>
            <Descriptions.Item label="version">{health.version}</Descriptions.Item>
            <Descriptions.Item label="platform">{health.platform}</Descriptions.Item>
            <Descriptions.Item label="camera_driver">
              <Tag color={health.camera_driver === 'ic4' ? 'blue' : 'gold'}>{health.camera_driver}</Tag>
            </Descriptions.Item>
          </Descriptions>
        ) : (
          healthStatus === 'error' && (
            <Alert type="error" showIcon message="Backend health check failed" description={healthError || 'Unable to connect'} />
          )
        )}
        <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
          The health check directly requests "backend address + /api/health". If the browser reports CORS errors in
          dev, make sure the backend allows cross-origin requests or use a backend address matching the Vite proxy
          target.
        </Typography.Paragraph>
      </Card>
    </Space>
  );
}
