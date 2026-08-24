import { Button, Layout, Menu, Space, Typography } from 'antd';
import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { AcquisitionProvider, useAcquisition } from './api/acquisition';
import ConnectPage from './pages/ConnectPage';
import LivePage from './pages/LivePage';
import CapturePage from './pages/CapturePage';
import ProcessingPage from './pages/ProcessingPage';
import SettingsPage from './pages/SettingsPage';

const NAV_ITEMS = [
  { key: '/connect', label: 'Connect' },
  { key: '/live', label: 'Live Preview' },
  { key: '/capture', label: 'Capture' },
  { key: '/processing', label: 'Processing' },
  { key: '/settings', label: 'Settings' },
];

function AppShell() {
  const location = useLocation();
  const navigate = useNavigate();
  const { streamStarted, acquisitionPending, toggleAcquisition } = useAcquisition();
  const selected = NAV_ITEMS.some((i) => i.key === location.pathname)
    ? location.pathname
    : '/live';

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Layout.Header style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
        <Typography.Title level={4} style={{ color: '#fff', margin: 0, whiteSpace: 'nowrap' }}>
          Static AstroPhoto
        </Typography.Title>
        <Menu
          theme="dark"
          mode="horizontal"
          selectedKeys={[selected]}
          items={NAV_ITEMS}
          onClick={({ key }) => navigate(key)}
          style={{ flex: 1, minWidth: 0 }}
        />
        <Space>
          <Button
            type={streamStarted ? 'default' : 'primary'}
            size="small"
            loading={acquisitionPending}
            onClick={() => toggleAcquisition(!streamStarted)}
          >
            {streamStarted ? 'Stop Acquisition' : 'Start Acquisition'}
          </Button>
        </Space>
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
    <AcquisitionProvider>
      <AppShell />
    </AcquisitionProvider>
  );
}
