import { Tag } from 'antd';
import { CheckCircleFilled, ExclamationCircleFilled, MinusCircleFilled } from '@ant-design/icons';
import type { Env } from '../api/types';

// 顶部常驻环境标识（红线 §10）：DEMO 绿、LIVE 红、未连接 灰。
export default function EnvBadge({ env }: { env: Env }) {
  if (env === 'live') {
    return (
      <Tag color="red" icon={<ExclamationCircleFilled />} style={{ fontWeight: 600 }}>
        LIVE 实盘
      </Tag>
    );
  }
  if (env === 'demo') {
    return (
      <Tag color="green" icon={<CheckCircleFilled />} style={{ fontWeight: 600 }}>
        DEMO 模拟
      </Tag>
    );
  }
  return (
    <Tag color="default" icon={<MinusCircleFilled />}>
      未连接
    </Tag>
  );
}
