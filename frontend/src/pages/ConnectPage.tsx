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
      message.success('Device opened');
      await Promise.all([refreshDevices(), refreshInfo()]);
    } finally {
      setActingId(null);
    }
  };

  const closeDevice = async () => {
    setActingId('__close__');
    try {
      await apiPost('/api/device/close');
      message.success('Device closed');
      await Promise.all([refreshDevices(), refreshInfo()]);
    } finally {
      setActingId(null);
    }
  };

  const connectedDevice = devices.find((d) => d.connected) ?? null;

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card
        title="Device List"
        extra={
          <Button icon={<ReloadOutlined />} onClick={() => void refreshDevices()} loading={loading}>
            Refresh
          </Button>
        }
      >
        {devices.length === 0 && !loading ? (
          <Empty description="No devices found (is the backend service running?)" />
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
                      Close Device
                    </Button>
                  ) : (
                    <Button
                      key="open"
                      type="primary"
                      icon={<ApiOutlined />}
                      loading={actingId === d.id}
                      onClick={() => void openDevice(d.id)}
                    >
                      Open Device
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
                        text={d.connected ? 'Connected' : 'Not connected'}
                      />
                    </Space>
                  }
                  description={
                    <Space direction="vertical" size={0}>
                      <Text type="secondary">Serial: {d.serial}</Text>
                      <Text type="secondary">ID: {d.id}</Text>
                      {d.kind === 'ic4' && (
                        <Text type="warning">
                          Real ic4 device: requires imagingcontrol4 driver support; opening it on a platform without the driver will return an error.
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
        title="Current Device Info"
        extra={
          <Button icon={<ReloadOutlined />} onClick={() => void refreshInfo()} loading={infoLoading}>
            Refresh
          </Button>
        }
      >
        {!deviceInfo || !deviceInfo.connected ? (
          <Alert
            type="info"
            showIcon
            message="No Device Open"
            description={connectedDevice ? `Device "${connectedDevice.name}" is not connected` : 'Open a device above to start using Live Preview and Capture.'}
          />
        ) : (
          <>
            <Title level={5}>{deviceInfo.name}</Title>
            <Descriptions column={2} size="small" bordered style={{ marginBottom: 16 }}>
              <Descriptions.Item label="Model">{deviceInfo.name}</Descriptions.Item>
              <Descriptions.Item label="Serial">{deviceInfo.serial}</Descriptions.Item>
              <Descriptions.Item label="Device ID">{deviceInfo.id}</Descriptions.Item>
              <Descriptions.Item label="Status">
                <Badge status="success" text="Connected" />
              </Descriptions.Item>
            </Descriptions>
            <Typography.Title level={5}>Property Summary</Typography.Title>
            <Space wrap>
              {deviceInfo.properties.length === 0 ? (
                <Text type="secondary">No property info</Text>
              ) : (
                deviceInfo.properties.map((p) => (
                  <Tag key={p.name} color={p.readonly ? 'default' : 'processing'}>
                    {p.label} ({p.name}) = {String(p.value)}
                    {p.unit ? ` ${p.unit}` : ''}
                    {p.readonly ? ' · Read-only' : ''}
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
