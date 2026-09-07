import { useEffect } from 'react';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { Layout, Menu, Badge, Space, Typography, theme } from 'antd';
import {
  DashboardOutlined, WalletOutlined,
  UnorderedListOutlined, SafetyCertificateOutlined,
  SettingOutlined, BellOutlined, WifiOutlined, DisconnectOutlined,
} from '@ant-design/icons';
import KillSwitch from '../components/KillSwitch';
import HeaderControl from '../components/HeaderControl';
import { useIsMobile } from '../useIsMobile';
import MobileLayout from '../mobile/MobileLayout';
import { useAppStore } from '../store/useAppStore';
import { useBitVaultWS } from '../ws/useBitVaultWS';
import { getStatus, getNotifications } from '../api/endpoints';
import { fmtTime } from '../utils/format';

const { Header, Sider, Content } = Layout;

const NAV = [
  { key: '/', label: '总览', icon: <DashboardOutlined /> },
  { key: '/account', label: '账户资产', icon: <WalletOutlined /> },
  { key: '/orders', label: '订单管理', icon: <UnorderedListOutlined /> },
  { key: '/risk', label: '风控中心', icon: <SafetyCertificateOutlined /> },
  { key: '/settings', label: '系统设置', icon: <SettingOutlined /> },
];

export default function MainLayout() {
  const nav = useNavigate();
  const loc = useLocation();
  const { token } = theme.useToken();
  const wsConnected = useAppStore((s) => s.wsConnected);
  const unread = useAppStore((s) => s.unread);
  const setEnv = useAppStore((s) => s.setEnv);
  const setHasKey = useAppStore((s) => s.setHasKey);
  const setRisk = useAppStore((s) => s.setRisk);
  const setVenue = useAppStore((s) => s.setVenue);
  const setInstId = useAppStore((s) => s.setInstId);
  const setNotifications = useAppStore((s) => s.setNotifications);
  const isMobile = useIsMobile();
  useBitVaultWS();

  // 初始拉一次状态 + 通知（WS 连接后会增量更新）
  // 注意：必须放在移动端分支之前执行——H5 依赖这里同步 hasKey/venue/inst_id，
  // 否则手机上"实盘"永远未连接而无法切换。
  useEffect(() => {
    const poll = async () => {
      try {
        const st = await getStatus();
        setEnv(st.env);
        setHasKey(st.has_key);
        setRisk(st.risk);
        if (st.venue) setVenue(st.venue);   // 同步后端系统级模式（paper/okx）
        if (st.inst_id) setInstId(st.inst_id);   // 同步驾驶标的（币种+合约/现货）
      } catch { /* ignore */ }
      try {
        const ns = await getNotifications(50);
        setNotifications(ns);
      } catch { /* ignore */ }
    };
    poll();
    const t = setInterval(poll, 10000);
    return () => clearInterval(t);
  }, [setEnv, setHasKey, setRisk, setVenue, setInstId, setNotifications]);

  // 移动端（H5）：独立信息架构与交互层（顶部驾驶条 + 底部 TabBar），PC 布局不受影响。
  if (isMobile) {
    return <MobileLayout />;
  }

  // 顶部保持中性简洁：自动驾驶跟随系统级模式（顶栏 Switch + 彩色徽章 + 模式切换）

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
            position: 'sticky',
            top: 0,
            zIndex: 100,
            background: token.colorBgContainer,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: '0 20px',
            height: 52,
            lineHeight: '52px',
            boxShadow: '0 2px 8px rgba(0,0,0,0.08)',
          }}
        >
          <Space size="middle">
            <Typography.Text strong>BitVault</Typography.Text>
            <Typography.Text type="secondary">
              {wsConnected ? (
                <><WifiOutlined /> 已连接</>
              ) : (
                <><DisconnectOutlined /> 连接中…</>
              )}
            </Typography.Text>
          </Space>
          <Space size="middle">
            <HeaderControl />
            <Badge
              count={unread}
              size="small"
              offset={[-4, 4]}
              style={{ display: unread ? 'inline' : 'none' }}
            >
              <BellOutlined
                style={{ fontSize: 18, color: token.colorText }}
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
          BitVault · BTC 量化交易操作系统 · {fmtTime(Date.now())}
        </Layout.Footer>
      </Layout>
    </Layout>
  );
}
