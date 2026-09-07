// 移动端兜底页：重功能页（回测/策略/风控配置等）提示在 PC 上完成。
import { useLocation, useNavigate } from 'react-router-dom';
import { DesktopOutlined } from '@ant-design/icons';
import { MButton } from './shared';

export default function MobilePcHint() {
  const nav = useNavigate();
  const { pathname } = useLocation();
  return (
    <div className="p-8 flex flex-col items-center justify-center text-center min-h-[60vh]">
      <DesktopOutlined style={{ fontSize: 46, color: 'rgba(255,255,255,0.22)' }} />
      <div className="mt-4 text-[16px] font-semibold">该功能建议在 PC 端操作</div>
      <div className="mt-2 text-[13px] text-white/58 leading-5">
        「{pathname}」涉及复杂配置与数据表格，在 PC 浏览器中体验更完整。<br />
        手机上可正常使用：驾驶舱状态、预测、标的切换、托管持仓。
      </div>
      <div className="mt-7 w-full space-y-2.5">
        <MButton onClick={() => nav('/')}>返回驾驶舱</MButton>
        <MButton color="default" onClick={() => nav('/market')}>查看标的持仓</MButton>
      </div>
    </div>
  );
}
