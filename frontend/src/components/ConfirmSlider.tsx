import { useState } from 'react';
import { Slider, Button, Typography, Space } from 'antd';
import { CheckOutlined } from '@ant-design/icons';

// LIVE 环境下单滑动确认（红线 §4.3）。滑到底触发 onConfirm。
export default function ConfirmSlider({
  label = '滑动以确认下单',
  onConfirm,
  loading,
}: {
  label?: string;
  onConfirm: () => void;
  loading?: boolean;
}) {
  const [v, setV] = useState(0);

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="small">
      <Typography.Text type="secondary">{label}</Typography.Text>
      <Slider
        value={v}
        min={0}
        max={100}
        step={1}
        tooltip={{ open: false }}
        onChange={(val) => {
          setV(val);
          if (val >= 100) onConfirm();
        }}
      />
      <Button
        type="primary"
        block
        loading={loading}
        disabled={v < 100}
        icon={<CheckOutlined />}
        onClick={onConfirm}
      >
        {v >= 100 ? '已确认，提交' : '请滑到底'}
      </Button>
    </Space>
  );
}
