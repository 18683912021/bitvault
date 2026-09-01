import { useEffect } from 'react';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { Layout, Menu, Badge, Space, Typography, theme } from 'antd';
import {
  DashboardOutlined, LineChartOutlined, WalletOutlined, RobotOutlined,
  ExperimentOutlined, UnorderedListOutlined, SafetyCertificateOutlined,
  BellOutlined, SettingOutlined, WifiOutlined, DisconnectOutlined,
} from '@ant-design/icons';
import EnvBadge from '../components/EnvBadge';
import KillSwitch from '../components/KillSwitch';
import { useAppStore } from '../store/useAppStore';
import { useBitVaultWS } from '../ws/useBitVaultWS';
import { getStatus, getNotifications } from '../api/endpoints';
import { fmtTime } from '../utils/format';

const { Header, Sider, Content } = Layout;

const NAV = [
  { key: '/', label: '总览', icon: <DashboardOutlined /> },
  { key: '/market', label: '行情中心', icon: <LineChartOutlined /> },
  { key: '/account', label: '账户资产', icon: <WalletOutlined /> },
  { key: '/strategies', label: '策略中心', icon: <RobotOutlined /> },
  { key: '/backtest', label: '回测中心', icon: <ExperimentOutlined /> },
  { key: '/orders', label: '订单管理', icon: <UnorderedListOutlined /> },
  { key: '/risk', label: '风控中心', icon: <SafetyCertificateOutlined /> },
  { key: '/notifications', label: '通知中心', icon: <BellOutlined /> },
  { key: '/settings', label: '系统设置', icon: <SettingOutlined /> },
];

export default function MainLayout() {
  const nav = useNavigate();
  const loc = useLocation();
  const { token } = theme.useToken();
  const env = useAppStore((s) => s.env);
  const wsConnected = useAppStore((s) => s.wsConnected);
  const unread = useAppStore((s) => s.unread);
  const setEnv = useAppStore((s) => s.setEnv);
  const setHasKey = useAppStore((s) => s.setHasKey);
  const setRisk = useAppStore((s) => s.setRisk);
  const setNotifications = useAppStore((s) => s.setNotifications);
  useBitVaultWS();

  // 初始拉一次状态 + 通知（WS 连接后会增量更新）
  useEffect(() => {
    const poll = async () => {
      try {
        const st = await getStatus();
        setEnv(st.env);
        setHasKey(st.has_key);
        setRisk(st.risk);
      } catch { /* ignore */ }
      try {
        const ns = await getNotifications(50);
        setNotifications(ns);
      } catch { /* ignore */ }
    };
    poll();
    const t = setInterval(poll, 10000);
    return () => clearInterval(t);
  }, [setEnv, setHasKey, setRisk, setNotifications]);

  // LIVE 环境顶栏红色（红线 §10）
  const isLive = env === 'live';

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider
        breakpoint="lg"
        collapsedWidth={0}
        width={208}
        style={{ background: '#001529' }}
      >
        <div
          style={{
            height: 56,
            color: '#fff',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontWeight: 700,
            fontSize: 18,
            letterSpacing: 1,
          }}
        >
          BitVault
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[loc.pathname]}
          items={NAV}
          onClick={({ key }) => nav(key)}
        />
        <div style={{ padding: '8px 16px', color: '#888', fontSize: 11 }}>
          BTC 量化交易操作系统
          <br />
          © 2026 BitVault
        </div>
      </Sider>
      <Layout>
        <Header
          style={{
            background: isLive ? '#cf1322' : token.colorBgContainer,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: '0 16px',
            boxShadow: '0 1px 4px rgba(0,0,0,0.06)',
          }}
        >
          <Space size="middle">
            {isLive ? (
              <Typography.Text strong style={{ color: '#fff' }}>
                ⚠ 实盘环境 LIVE — 操作将影响真实资金
              </Typography.Text>
            ) : (
              <Typography.Text strong>BitVault 控制台</Typography.Text>
            )}
            <EnvBadge env={env} />
            <Typography.Text type={isLive ? undefined : 'secondary'} style={{ color: isLive ? '#fff' : undefined }}>
              {wsConnected ? (
                <><WifiOutlined /> 已连接</>
              ) : (
                <><DisconnectOutlined /> 连接中…</>
              )}
            </Typography.Text>
          </Space>
          <Space size="middle">
            <Badge
              count={unread}
              size="small"
              offset={[-4, 4]}
              style={{ display: unread ? 'inline' : 'none' }}
            >
              <BellOutlined
                style={{ fontSize: 18, color: isLive ? '#fff' : token.colorText }}
                onClick={() => nav('/notifications')}
              />
            </Badge>
            <KillSwitch />
          </Space>
        </Header>
        <Content style={{ margin: 16, padding: 16, background: token.colorBgContainer, borderRadius: 8, minHeight: 'auto' }}>
          <Outlet />
        </Content>
        <Layout.Footer style={{ textAlign: 'center', color: '#999', fontSize: 12 }}>
          BitVault · 本地部署 · 默认 OKX 模拟盘 · {fmtTime(Date.now())}
        </Layout.Footer>
      </Layout>
    </Layout>
  );
}
