import { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Badge,
  Button,
  Card,
  Descriptions,
  Empty,
  List,
  Space,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  ApiOutlined,
  DisconnectOutlined,
  ReloadOutlined,
  UsbOutlined,
} from '@ant-design/icons';
import { apiGet, apiPost } from '../api/client';
import type { DeviceInfo, DeviceItem } from '../api/types';

const { Text, Title } = Typography;

export default function ConnectPage() {
  const [devices, setDevices] = useState<DeviceItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [deviceInfo, setDeviceInfo] = useState<DeviceInfo | null>(null);
  const [infoLoading, setInfoLoading] = useState(true);
  const [actingId, setActingId] = useState<string | null>(null);

  const refreshDevices = useCallback(async () => {
    setLoading(true);
    try {
      const list = await apiGet<DeviceItem[]>('/api/devices');
      setDevices(list);
    } finally {
      setLoading(false);
    }
  }, []);

  const refreshInfo = useCallback(async () => {
    setInfoLoading(true);
    try {
      const info = await apiGet<DeviceInfo>('/api/device/info');
      setDeviceInfo(info);
    } catch {
      setDeviceInfo(null);
    } finally {
      setInfoLoading(false);
    }
  }, []);

  // 初始加载内联成 promise 链（loading 初值即 true），不在 effect 里同步调用会 setState 的刷新函数
  useEffect(() => {
    let cancelled = false;
    apiGet<DeviceItem[]>('/api/devices')
      .then((list) => {
        if (!cancelled) setDevices(list);
      })
      .catch(() => {
        if (!cancelled) setDevices([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    apiGet<DeviceInfo>('/api/device/info')
      .then((info) => {
        if (!cancelled) setDeviceInfo(info);
      })
      .catch(() => {
        if (!cancelled) setDeviceInfo(null);
      })
      .finally(() => {
        if (!cancelled) setInfoLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const openDevice = async (id: string) => {
    setActingId(id);
    try {
      await apiPost('/api/device/open', { id });
      message.success('设备已打开');
      await Promise.all([refreshDevices(), refreshInfo()]);
    } finally {
      setActingId(null);
    }
  };

  const closeDevice = async () => {
    setActingId('__close__');
    try {
      await apiPost('/api/device/close');
      message.success('设备已关闭');
      await Promise.all([refreshDevices(), refreshInfo()]);
    } finally {
      setActingId(null);
    }
  };

  const connectedDevice = devices.find((d) => d.connected) ?? null;

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card
        title="设备列表"
        extra={
          <Button icon={<ReloadOutlined />} onClick={() => void refreshDevices()} loading={loading}>
            刷新
          </Button>
        }
      >
        {devices.length === 0 && !loading ? (
          <Empty description="未发现设备（后端服务是否已启动？）" />
        ) : (
          <List
            loading={loading}
            dataSource={devices}
            rowKey="id"
            renderItem={(d) => (
              <List.Item
                actions={[
                  d.connected ? (
                    <Button
                      key="close"
                      danger
                      icon={<DisconnectOutlined />}
                      loading={actingId === '__close__'}
                      onClick={() => void closeDevice()}
                    >
                      关闭设备
                    </Button>
                  ) : (
                    <Button
                      key="open"
                      type="primary"
                      icon={<ApiOutlined />}
                      loading={actingId === d.id}
                      onClick={() => void openDevice(d.id)}
                    >
                      打开设备
                    </Button>
                  ),
                ]}
              >
                <List.Item.Meta
                  avatar={<UsbOutlined style={{ fontSize: 24, color: d.connected ? '#52c41a' : undefined }} />}
                  title={
                    <Space>
                      <Text strong>{d.name}</Text>
                      <Tag color={d.kind === 'ic4' ? 'blue' : 'gold'}>{d.kind}</Tag>
                      <Badge
                        status={d.connected ? 'success' : 'default'}
                        text={d.connected ? '已连接' : '未连接'}
                      />
                    </Space>
                  }
                  description={
                    <Space direction="vertical" size={0}>
                      <Text type="secondary">序列号：{d.serial}</Text>
                      <Text type="secondary">ID：{d.id}</Text>
                      {d.kind === 'ic4' && (
                        <Text type="warning">
                          真机 ic4 设备：需要 imagingcontrol4 驱动支持；无驱动平台打开将返回错误。
                        </Text>
                      )}
                    </Space>
                  }
                />
              </List.Item>
            )}
          />
        )}
      </Card>

      <Card
        title="当前设备信息"
        extra={
          <Button icon={<ReloadOutlined />} onClick={() => void refreshInfo()} loading={infoLoading}>
            刷新
          </Button>
        }
      >
        {!deviceInfo || !deviceInfo.connected ? (
          <Alert
            type="info"
            showIcon
            message="尚未打开设备"
            description={connectedDevice ? `设备「${connectedDevice.name}」未连接` : '请在上方打开一个设备开始使用实时预览与序列拍摄。'}
          />
        ) : (
          <>
            <Title level={5}>{deviceInfo.name}</Title>
            <Descriptions column={2} size="small" bordered style={{ marginBottom: 16 }}>
              <Descriptions.Item label="型号">{deviceInfo.name}</Descriptions.Item>
              <Descriptions.Item label="序列号">{deviceInfo.serial}</Descriptions.Item>
              <Descriptions.Item label="设备 ID">{deviceInfo.id}</Descriptions.Item>
              <Descriptions.Item label="状态">
                <Badge status="success" text="已连接" />
              </Descriptions.Item>
            </Descriptions>
            <Typography.Title level={5}>属性摘要</Typography.Title>
            <Space wrap>
              {deviceInfo.properties.length === 0 ? (
                <Text type="secondary">无属性信息</Text>
              ) : (
                deviceInfo.properties.map((p) => (
                  <Tag key={p.name} color={p.readonly ? 'default' : 'processing'}>
                    {p.label}（{p.name}）= {String(p.value)}
                    {p.unit ? ` ${p.unit}` : ''}
                    {p.readonly ? ' · 只读' : ''}
                  </Tag>
                ))
              )}
            </Space>
          </>
        )}
      </Card>
    </Space>
  );
}
