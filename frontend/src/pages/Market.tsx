import { useEffect, useMemo, useState } from 'react';
import {
  Card, Col, Row, Segmented, Select, Table, Tag, Typography, Space, InputNumber,
  Radio, Button, Form, Slider, Modal, Divider, Empty, Tooltip,
} from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import LightweightChart from '../components/LightweightChart';
import ConfirmSlider from '../components/ConfirmSlider';
import { useAppStore } from '../store/useAppStore';
import { useMarketStore } from '../store/useMarketStore';
import { useAccountStore } from '../store/useAccountStore';
import { getCandles, placeOrder, cancelOrder, closePosition, getOrders } from '../api/endpoints';
import { PERIODS, fmtPx, fmtTimeShort, pnlColor } from '../utils/format';
import type { Candle } from '../api/types';

const INSTRUMENTS = ['BTC-USDT', 'BTC-USDT-SWAP'];

export default function Market() {
  const env = useAppStore((s) => s.env);
  const hasKey = useAppStore((s) => s.hasKey);
  const instId = useMarketStore((s) => s.instId);
  const period = useMarketStore((s) => s.period);
  const setInstId = useMarketStore((s) => s.setInstId);
  const setPeriod = useMarketStore((s) => s.setPeriod);
  const candles = useMarketStore((s) => s.candles[`${instId}:${period}`]);
  const setCandles = useMarketStore((s) => s.setCandles);
  const depth = useMarketStore((s) => s.depth[instId]);
  const trades = useMarketStore((s) => s.trades[instId]);
  const positions = useAccountStore((s) => s.positions);

  const [loading, setLoading] = useState(false);
  const [form] = Form.useForm();
  const isSwap = instId.endsWith('-SWAP');
  const curPos = positions.find((p) => p.instId === instId);

  // 切换标的/周期时拉历史 K 线
  useEffect(() => {
    setLoading(true);
    getCandles(instId, period, 300)
      .then((rows) => setCandles(instId, period, rows as Candle[]))
      .catch(() => {})
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instId, period]);

  const bestBid = depth?.bids?.[0]?.[0] ?? 0;
  const bestAsk = depth?.asks?.[0]?.[0] ?? 0;
  const spread = bestAsk && bestBid ? bestAsk - bestBid : 0;

  const doSubmit = async (vals: any) => {
    const body = {
      inst_id: instId,
      side: vals.side,
      ord_type: vals.ord_type,
      px: vals.ord_type === 'market' ? null : vals.px,
      sz_base: Number(vals.sz_base),
      reduce_only: !!vals.reduce_only,
      venue: vals.venue || (hasKey ? 'okx' : 'paper'),
    };
    await placeOrder(body);
    form.resetFields(['sz_base']);
  };

  const confirmSubmit = (vals: any) => {
    if (env === 'live' && vals.venue === 'okx') {
      // LIVE 滑动确认（红线 §4.3）
      Modal.confirm({
        title: '实盘下单确认',
        content: (
          <ConfirmSlider
            onConfirm={async () => { await doSubmit(vals); Modal.destroyAll(); }}
            loading={loading}
          />
        ),
        icon: null,
        okButtonProps: { style: { display: 'none' } },
        cancelButtonProps: { style: { display: 'none' } },
      });
    } else {
      Modal.confirm({
        title: '确认下单？',
        content: `${vals.side === 'buy' ? '买入' : '卖出'} ${vals.sz_base} BTC @ ${vals.ord_type === 'market' ? '市价(保护价)' : vals.px}`,
        okText: '确认',
        cancelText: '取消',
        onOk: () => doSubmit(vals),
      });
    }
  };

  const cancelAll = () => {
    Modal.confirm({
      title: '撤销全部挂单？',
      okText: '确认撤销',
      okType: 'danger',
      cancelText: '取消',
      onOk: async () => {
        const list = await getOrders('open', 500);
        await Promise.all(
          list.filter((o) => o.inst_id === instId).map((o) => cancelOrder(o.cl_ord_id).catch(() => {}))
        );
      },
    });
  };

  const closePos = () => {
    if (!curPos) return;
    Modal.confirm({
      title: '一键平仓？',
      content: `${curPos.instId} 持仓 ${curPos.pos}，未实现 ${curPos.upl?.toFixed(2)}`,
      okText: '确认平仓',
      okType: 'danger',
      cancelText: '取消',
      onOk: () => closePosition(curPos.instId),
    });
  };

  return (
    <div>
      <Card
        size="small"
        style={{ marginBottom: 16 }}
        styles={{ body: { padding: '8px 16px' } }}
      >
        <Space wrap>
          <Select
            value={instId}
            onChange={setInstId}
            options={INSTRUMENTS.map((i) => ({ value: i, label: i }))}
            style={{ width: 160 }}
          />
          <Segmented value={period} onChange={(v) => setPeriod(v as string)} options={[...PERIODS]} />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            买一 {fmtPx(bestBid)} / 卖一 {fmtPx(bestAsk)} / 价差 {fmtPx(spread)}
          </Typography.Text>
          <Button size="small" icon={<ReloadOutlined />} onClick={() => setCandles(instId, period, [])}>
            重载
          </Button>
        </Space>
      </Card>

      <Row gutter={16}>
        <Col xs={24} lg={16}>
          <Card size="small" styles={{ body: { padding: 8 } }} loading={loading && !candles?.length}>
            {candles && candles.length > 5 ? (
              <LightweightChart candles={candles} height={480} showMA maPeriods={[5, 20]} title={`${instId} · ${period}`} />
            ) : (
              <Empty description="K 线加载中" style={{ padding: 80 }} />
            )}
          </Card>
        </Col>

        <Col xs={24} lg={8}>
          <Card size="small" title="盘口 5 档" styles={{ body: { padding: 8 } }}>
            <BookRows asks={depth?.asks?.slice(0, 5) || []} bids={depth?.bids?.slice(0, 5) || []} />
          </Card>
          <Card size="small" title="最近成交" style={{ marginTop: 12 }} styles={{ body: { padding: 4 } }}>
            <Table
              size="small"
              rowKey={(r) => String(r.ts)}
              dataSource={(trades ? [...trades] : []).slice(-30).reverse()}
              pagination={false}
              scroll={{ y: 200 }}
              columns={[
                { title: '方向', dataIndex: 'side', width: 50, render: (s: string) => <Tag color={s === 'buy' ? 'red' : 'green'}>{s === 'buy' ? '买' : '卖'}</Tag> },
                { title: '价格', dataIndex: 'px', render: fmtPx },
                { title: '数量', dataIndex: 'sz', render: (v: number) => v?.toFixed(5) },
                { title: '时间', dataIndex: 'ts', width: 70, render: fmtTimeShort },
              ]}
            />
          </Card>
        </Col>
      </Row>

      <Row gutter={16} style={{ marginTop: 16 }}>
        <Col xs={24} lg={12}>
          <Card size="small" title="手动下单" styles={{ body: { padding: 16 } }}>
            <Form
              form={form}
              layout="vertical"
              initialValues={{ ord_type: 'market', side: 'buy', reduce_only: false, sz_base: 0.001, venue: hasKey ? 'okx' : 'paper' }}
              onFinish={confirmSubmit}
            >
              <Form.Item name="venue" label="下单通道">
                <Radio.Group>
                  <Radio.Button value="paper">本地模拟（无需 Key）</Radio.Button>
                  <Radio.Button value="okx" disabled={!hasKey}>OKX {env === 'live' ? '实盘' : env === 'demo' ? '模拟盘' : '（未连接）'}</Radio.Button>
                </Radio.Group>
              </Form.Item>
              <Form.Item name="side" label="方向">
                <Radio.Group>
                  <Radio.Button value="buy">买入 / 开多</Radio.Button>
                  <Radio.Button value="sell">卖出 / 开空</Radio.Button>
                </Radio.Group>
              </Form.Item>
                <Row gutter={8}>
                  <Col span={10}>
                    <Form.Item name="ord_type" label="类型">
                      <Select
                        options={[
                          { value: 'market', label: '市价(限价IOC保护)' },
                          { value: 'post_only', label: '限价(post_only)' },
                        ]}
                      />
                    </Form.Item>
                  </Col>
                  <Col span={14}>
                    <Form.Item
                      noStyle
                      shouldUpdate={(a, b) => a.ord_type !== b.ord_type}
                    >
                      {({ getFieldValue }) =>
                        getFieldValue('ord_type') !== 'market' ? (
                          <Form.Item name="px" label="价格 (USDT)">
                            <InputNumber style={{ width: '100%' }} step={0.1} min={0} />
                          </Form.Item>
                        ) : (
                          <Form.Item label="价格 (USDT)">
                            <InputNumber disabled placeholder="市价按保护价" style={{ width: '100%' }} />
                          </Form.Item>
                        )
                      }
                    </Form.Item>
                  </Col>
                </Row>
                <Form.Item name="sz_base" label="数量 (BTC)">
                  <InputNumber style={{ width: '100%' }} step={0.001} min={0} />
                </Form.Item>
                <Form.Item name="szSlider" label="">
                  <Slider min={0} max={isSwap ? 1 : 1} step={0.001} tooltip={{ formatter: (v) => `${v} BTC` }}
                    onChange={(v) => form.setFieldValue('sz_base', Number(v))}
                  />
                </Form.Item>
                <Form.Item name="reduce_only" valuePropName="checked">
                  <Radio.Group>
                    <Radio value={false}>开仓</Radio>
                    <Radio value={true}>仅减仓 (reduceOnly)</Radio>
                  </Radio.Group>
                </Form.Item>
                {env === 'live' && (
                  <Typography.Text type="danger" style={{ display: 'block', marginBottom: 8 }}>
                    ⚠ 实盘环境：将使用滑动确认。
                  </Typography.Text>
                )}
                <Form.Item>
                  <Space>
                    <Button type="primary" htmlType="submit">下单</Button>
                    <Button danger onClick={cancelAll}>撤销全部挂单</Button>
                  </Space>
                </Form.Item>
              </Form>
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card size="small" title={`当前持仓 · ${instId}`} styles={{ body: { padding: 16 } }}>
            {curPos ? (
              <Space direction="vertical" style={{ width: '100%' }}>
                <Row>
                  <Col span={8}><Typography.Text type="secondary">方向/数量</Typography.Text></Col>
                  <Col span={16}>
                    <Tag color={curPos.pos > 0 ? 'red' : 'green'}>{curPos.pos > 0 ? '多' : '空'}</Tag>
                    {curPos.pos} {isSwap ? '张' : 'BTC'}
                  </Col>
                </Row>
                <Row>
                  <Col span={8}><Typography.Text type="secondary">开仓均价</Typography.Text></Col>
                  <Col span={16}>{fmtPx(curPos.avgPx)}</Col>
                </Row>
                <Row>
                  <Col span={8}><Typography.Text type="secondary">标记价</Typography.Text></Col>
                  <Col span={16}>{fmtPx(curPos.markPx)}</Col>
                </Row>
                <Row>
                  <Col span={8}><Typography.Text type="secondary">未实现盈亏</Typography.Text></Col>
                  <Col span={16} style={{ color: pnlColor(curPos.upl) || undefined }}>{curPos.upl?.toFixed(2)} USDT ({(curPos.uplRatio * 100).toFixed(2)}%)</Col>
                </Row>
                {curPos.liqPx != null && (
                  <Row>
                    <Col span={8}><Typography.Text type="secondary">强平价</Typography.Text></Col>
                    <Col span={16} style={{ color: '#cf1322' }}>{fmtPx(curPos.liqPx)}</Col>
                  </Row>
                )}
                <Button danger block onClick={closePos}>一键平仓</Button>
              </Space>
            ) : (
              <Empty description="当前无持仓" style={{ padding: 30 }} />
            )}
          </Card>
        </Col>
      </Row>
    </div>
  );
}

function BookRows({ asks, bids }: { asks: [number, number][]; bids: [number, number][] }) {
  const max = Math.max(
    ...asks.map((a) => a[1]), ...bids.map((b) => b[1]), 1
  );
  return (
    <div style={{ fontSize: 12, fontFamily: 'monospace' }}>
      {asks.slice().reverse().map((a, i) => (
        <BookRow key={'a' + i} px={a[0]} sz={a[1]} side="ask" max={max} />
      ))}
      <Divider style={{ margin: '4px 0' }} />
      {bids.map((b, i) => (
        <BookRow key={'b' + i} px={b[0]} sz={b[1]} side="bid" max={max} />
      ))}
    </div>
  );
}

function BookRow({ px, sz, side, max }: { px: number; sz: number; side: 'ask' | 'bid'; max: number }) {
  const pct = Math.min(100, (sz / max) * 100);
  const color = side === 'ask' ? 'rgba(63,134,0,0.12)' : 'rgba(207,19,34,0.12)';
  return (
    <Tooltip title={`${px} × ${sz}`}>
      <div style={{ display: 'flex', justifyContent: 'space-between', padding: '2px 4px', position: 'relative' }}>
        <div style={{ position: 'absolute', right: 0, top: 0, bottom: 0, width: `${pct}%`, background: color }} />
        <span style={{ color: side === 'ask' ? '#3f8600' : '#cf1322', position: 'relative' }}>{fmtPx(px)}</span>
        <span style={{ position: 'relative' }}>{sz.toFixed(4)}</span>
      </div>
    </Tooltip>
  );
}
