import { useState } from 'react';
import { Button, Modal, List, Typography, App } from 'antd';
import { PoweroffOutlined } from '@ant-design/icons';
import { killSwitch } from '../api/endpoints';
import { useAppStore } from '../store/useAppStore';

// 全局 Kill Switch（红线 M6-6）：二次确认 + 执行结果清单。
export default function KillSwitch() {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [report, setReport] = useState<any>(null);
  const { modal } = App.useApp();
  const risk = useAppStore((s) => s.risk);

  const confirm = () => {
    modal.confirm({
      title: '触发 Kill Switch？',
      icon: <PoweroffOutlined />,
      content: '将撤销全部挂单、市价全平所有持仓、停止所有策略。此操作不可撤销。',
      okText: '确认执行',
      okType: 'danger',
      cancelText: '取消',
      onOk: doExecute,
    });
  };

  const doExecute = async () => {
    setLoading(true);
    try {
      const r = await killSwitch();
      setReport(r.report);
      setOpen(true);
    } finally {
      setLoading(false);
    }
  };

  return (
    <>
      <Button danger type="primary" icon={<PoweroffOutlined />} loading={loading} onClick={confirm}>
        Kill Switch
      </Button>
      <Modal
        title="Kill Switch 执行结果"
        open={open}
        onOk={() => setOpen(false)}
        onCancel={() => setOpen(false)}
        cancelButtonProps={{ style: { display: 'none' } }}
      >
        {report ? (
          <>
            <Typography.Paragraph>
              撤销挂单 <b>{report.canceled ?? 0}</b> 单，平仓 <b>{report.closed ?? 0}</b> 个仓位。
              {risk?.halted ? ' 风控已熔断。' : ''}
            </Typography.Paragraph>
            {report.errors?.length > 0 && (
              <>
                <Typography.Text type="danger">执行中的错误：</Typography.Text>
                <List
                  size="small"
                  dataSource={report.errors}
                  renderItem={(e: string) => (
                    <List.Item style={{ color: '#cf1322' }}>{e}</List.Item>
                  )}
                />
              </>
            )}
          </>
        ) : (
          '无结果'
        )}
      </Modal>
    </>
  );
}
