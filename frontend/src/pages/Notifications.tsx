import { useEffect, useState, useCallback } from 'react';
import { Card, Table, Tag, Button, Empty, Typography, Tabs } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { useAppStore } from '../store/useAppStore';
import { getNotifications, getAuditLogs } from '../api/endpoints';
import { fmtTime } from '../utils/format';
import type { Notification, AuditLog } from '../api/types';

export default function Notifications() {
  const setNotifications = useAppStore((s) => s.setNotifications);
  const [list, setList] = useState<Notification[]>([]);
  const [audit, setAudit] = useState<AuditLog[]>([]);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [n, a] = await Promise.all([getNotifications(200), getAuditLogs(200)]);
      setList(n);
      setAudit(a);
      setNotifications(n);
    } finally {
      setLoading(false);
    }
  }, [setNotifications]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 8000);
    return () => clearInterval(t);
  }, [refresh]);

  return (
    <Card
      size="small"
      styles={{ body: { padding: 0 } }}
      title="通知中心"
      extra={<Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={refresh}>刷新</Button>}
    >
      <Tabs
        items={[
          { key: 'notif', label: `通知 (${list.length})`, children: (
            <Table
              size="small" rowKey={(n) => n.id} dataSource={list} pagination={{ pageSize: 20, size: 'small' }}
              locale={{ emptyText: <Empty description="暂无通知" style={{ padding: 40 }} /> }}
              columns={[
                { title: '时间', dataIndex: 'ts', width: 160, render: fmtTime },
                { title: '级别', dataIndex: 'level', width: 80, render: (v: string) => <Tag color={v === 'error' ? 'red' : v === 'warning' ? 'orange' : 'blue'}>{v}</Tag> },
                { title: '标题', dataIndex: 'title', width: 180 },
                { title: '内容', dataIndex: 'body', render: (v: string) => <Typography.Text style={{ fontSize: 12 }}>{v}</Typography.Text> },
                { title: '已读', dataIndex: 'read', width: 60, render: (v: number) => v ? '是' : '否' },
              ]}
            />
          ) },
          { key: 'audit', label: `审计日志 (${audit.length})`, children: (
            <Table
              size="small" rowKey={(a) => a.id} dataSource={audit} pagination={{ pageSize: 20, size: 'small' }}
              locale={{ emptyText: <Empty description="暂无审计日志" style={{ padding: 40 }} /> }}
              columns={[
                { title: '时间', dataIndex: 'ts', width: 160, render: fmtTime },
                { title: '来源', dataIndex: 'actor', width: 120, render: (v: string) => v?.startsWith('strategy') ? `策略${v}` : v },
                { title: '动作', dataIndex: 'action', width: 160 },
                { title: '参数', dataIndex: 'payload_json', render: (v: string) => <Typography.Text style={{ fontSize: 12 }}>{v}</Typography.Text> },
                { title: '结果', dataIndex: 'result', width: 120, render: (v: string) => <Tag color={v === 'ok' ? 'green' : 'orange'}>{v}</Tag> },
              ]}
            />
          ) },
        ]}
      />
    </Card>
  );
}
