import { Layout, Menu, Typography, ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import dayjs from 'dayjs';
import 'dayjs/locale/zh-cn';
import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import ConnectPage from './pages/ConnectPage';
import LivePage from './pages/LivePage';
import CapturePage from './pages/CapturePage';
import ProcessingPage from './pages/ProcessingPage';
import SettingsPage from './pages/SettingsPage';

dayjs.locale('zh-cn');

const NAV_ITEMS = [
  { key: '/connect', label: '设备连接' },
  { key: '/live', label: '实时预览' },
  { key: '/capture', label: '序列拍摄' },
  { key: '/processing', label: '数据处理' },
  { key: '/settings', label: '设置' },
];

function AppShell() {
  const location = useLocation();
  const navigate = useNavigate();
  const selected = NAV_ITEMS.some((i) => i.key === location.pathname)
    ? location.pathname
    : '/live';

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Layout.Header style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
        <Typography.Title level={4} style={{ color: '#fff', margin: 0, whiteSpace: 'nowrap' }}>
          衍射采集与叠加
        </Typography.Title>
        <Menu
          theme="dark"
          mode="horizontal"
          selectedKeys={[selected]}
          items={NAV_ITEMS}
          onClick={({ key }) => navigate(key)}
          style={{ flex: 1, minWidth: 0 }}
        />
      </Layout.Header>
      <Layout.Content style={{ padding: 16 }}>
        <Routes>
          <Route path="/" element={<Navigate to="/live" replace />} />
          <Route path="/connect" element={<ConnectPage />} />
          <Route path="/live" element={<LivePage />} />
          <Route path="/capture" element={<CapturePage />} />
          <Route path="/processing" element={<ProcessingPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/live" replace />} />
        </Routes>
      </Layout.Content>
    </Layout>
  );
}

export default function App() {
  return (
    <ConfigProvider locale={zhCN}>
      <AppShell />
    </ConfigProvider>
  );
}
