import { useEffect, useState } from 'react';
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
import { CheckCircleOutlined, CloudServerOutlined, ReloadOutlined, SaveOutlined } from '@ant-design/icons';
import { apiGet } from '../api/client';
import { clearBackend, DEFAULT_BACKEND, getBackend, normalizeBackend, setBackend } from '../api/backend';
import {
  DEFAULT_PREVIEW,
  getPreviewSettings,
  normalizePreviewSettings,
  setPreviewSettings,
  type PreviewSettings,
} from '../api/settings';
import type { HealthInfo } from '../api/types';

type HealthStatus = 'checking' | 'ok' | 'error';

export default function SettingsPage() {
  const [backendInput, setBackendInput] = useState<string>(getBackend());
  const [preview, setPreview] = useState<PreviewSettings>(getPreviewSettings());
  const [health, setHealth] = useState<HealthInfo | null>(null);
  const [healthStatus, setHealthStatus] = useState<HealthStatus>('checking');
  const [healthError, setHealthError] = useState('');

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
      setHealthError(err instanceof Error ? err.message : '无法连接后端服务');
      setHealthStatus('error');
    }
  };

  useEffect(() => {
    void checkHealth();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const saveBackend = () => {
    const normalized = normalizeBackend(backendInput);
    setBackend(normalized);
    setBackendInput(normalized);
    message.success(`后端地址已保存：${normalized}（WS / 图片 URL 立即生效，REST 在 dev 走 vite 代理）`);
    void checkHealth(normalized);
  };

  const savePreview = () => {
    const next = normalizePreviewSettings(preview);
    setPreviewSettings(next);
    setPreview(next);
    message.success('预览参数已保存（fps/quality 取整、scale 收敛到 0.25/0.5/1.0），重新进入 /live 或刷新页面后生效');
  };

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card
        title={
          <Space>
            <CloudServerOutlined />
            后端地址
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
                保存（localStorage 持久化）
              </Button>
              <Button
                onClick={() => {
                  // 恢复默认 = 清除 localStorage 覆盖，让 VITE_BACKEND env / 默认值按优先级自然生效
                  clearBackend();
                  const effective = getBackend();
                  setBackendInput(effective);
                  message.success(`已清除本地后端覆盖，当前生效地址：${effective}`);
                  void checkHealth(effective);
                }}
              >
                恢复默认（不清除 env 注入）
              </Button>
            </Space>
          </Col>
        </Row>
        <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
          当前生效地址：{getBackend()}。dev 模式下 REST 请求经 vite 代理（目标由启动时 VITE_BACKEND / 默认
          localhost:8000 决定）；WS 实时流与图片/文件 URL 直接使用这里保存的地址，修改后立即生效。
          生产模式下 FastAPI 托管 frontend/dist，同源访问。
        </Typography.Paragraph>
      </Card>

      <Card title="后端健康状态">
        <Space style={{ marginBottom: 12 }}>
          <Badge
            status={healthStatus === 'ok' ? 'success' : healthStatus === 'error' ? 'error' : 'processing'}
            text={healthStatus === 'ok' ? '健康' : healthStatus === 'error' ? '异常 / 不可达' : '检查中…'}
          />
          <Button icon={<ReloadOutlined />} onClick={() => void checkHealth()}>
            重新检查
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
            <Alert type="error" showIcon message="后端健康检查失败" description={healthError || '无法连接'} />
          )
        )}
        <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
          健康检查直接请求「后端地址 + /api/health」。dev 下如浏览器提示 CORS，请确认后端已允许跨域或使用 vite
          代理目标一致的后端地址。
        </Typography.Paragraph>
      </Card>

      <Card title="预览参数（/live 使用）">
        <Row gutter={24}>
          <Col xs={24} md={8}>
            <Typography.Text strong>FPS（≤30）</Typography.Text>
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
            <Typography.Text strong>JPEG 质量（10–100，整数）</Typography.Text>
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
            <Typography.Text strong>缩放 scale（0.25 / 0.5 / 1.0）</Typography.Text>
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
          保存预览参数
        </Button>
        <Typography.Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0 }}>
          默认值：fps={DEFAULT_PREVIEW.fps}，quality={DEFAULT_PREVIEW.quality}，scale={DEFAULT_PREVIEW.scale}。
          这些参数在进入 /live 时通过 POST /api/live/start 发给后端。
        </Typography.Paragraph>
      </Card>
    </Space>
  );
}
