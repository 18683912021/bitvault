# BitVault

**BTC 量化交易操作系统** —— 本地部署、带 Web UI 的开箱即用量化交易终端，通过欧易 OKX API v5（REST + WebSocket）接入行情与交易，覆盖「行情监控 → 策略编写 → 历史回测 → 模拟盘验证 → 实盘执行 → 风控监控」完整闭环。

> ⚠️ 本项目为个人/小团队自用交易系统，可全自动下单。使用前请务必阅读文末「安全与合规」章节。

---

## 功能特性

- **行情数据中心**：OKX 全量标的同步（USDT 现货 + USDT 永续），实时价格 K 线、盘口、成交、资金费率，纯 WebSocket 推送
- **自动驾驶（Brain）**：市场状态因子引擎 + 信号引擎，实时决策信号流（DecisionFeed）；forecast24 滚动预测推送
- **策略中心**：模板策略 + 多实例生命周期管理（启动/停止/暂停/恢复/日志）
- **回测系统**：历史回测 + 绩效归因（含独立样本外检查脚本）
- **模拟盘（Paper）**：独立虚拟资金环境，与实盘完全隔离，支持一键重置
- **实盘执行引擎（OMS）**：订单全生命周期管理，限价 IOC 保护价偏移，防穿单
- **风控中心**：事前/事中/事后三层风控，风险规则热重载、杠杆上限、熔断（kill-switch）、撤销/重试
- **账户与资产**：OKX 账户总览、资产曲线、收益归因、回合统计（roundtrips）
- **应用层鉴权（P0-1）**：`BV_API_TOKEN` Bearer Token，未配置时 fail-closed（交易/敏感接口全部 401，仅公开只读名单放行）
- **多端 UI**：桌面 Web（Ant Design 5）+ 移动端 H5（Tailwind CSS v4，响应式）
- **行情数据本地落库**：SQLite（`backend/data/`），可导出历史 K 线下游分析
- **API Key 本地加密存储**：AES-256-GCM 加密（`secret.key` 落盘），密钥永不落明文、绝不入库

## 技术栈

| 层 | 技术 |
| --- | --- |
| 后端 | Python 3.12 · FastAPI · Uvicorn · WebSockets · SQLite |
| 前端 | React 18 · Vite 5 · Ant Design 5 · Tailwind CSS v4 · lightweight-charts · Zustand |
| 交易所 | OKX API v5（REST + WebSocket），主站与官方 AWS 接入点可切换 |
| 测试 | pytest + pytest-asyncio（`backend/tests/`，47 个用例，全量通过） |

## 目录结构

```
BitVault/
├── backend/
│   ├── app/
│   │   ├── main.py            # 服务装配入口（FastAPI app + 后台任务 + 前端静态托管）
│   │   ├── config.py          # 全局配置（全部环境变量，无硬编码密钥）
│   │   ├── api/               # REST 路由 + WS 路由 + 鉴权中间件
│   │   ├── exchange/          # OKX REST 客户端 + WebSocket 客户端
│   │   ├── market/            # 行情数据服务、标的支持集同步
│   │   ├── brain/             # 自动驾驶：因子引擎 / 信号引擎 / forecast24 / 健康检查
│   │   ├── strategy/          # 策略模板与实例管理
│   │   ├── backtest/          # 历史回测
│   │   ├── paper/             # 模拟盘引擎
│   │   ├── trading/           # OMS 实盘执行引擎、回合统计
│   │   ├── risk/              # 风控引擎（规则 / 熔断 / 保护价）
│   │   ├── account/           # 账户与资产服务
│   │   └── core/              # 事件总线、密钥加密存储
│   ├── tests/                 # pytest 测试套件
│   └── run.py                 # 本地启动入口
├── frontend/
│   ├── src/
│   │   ├── pages/             # 总览/市场/策略/回测/设置
│   │   ├── mobile/            # 移动端 H5 页面
│   │   ├── components/        # 组件（决策流、预测卡、标的切换等）
│   │   └── store/             # Zustand 状态
│   └── vite.config.ts
├── deploy/                    # 服务器部署：env.example / start.sh / nginx / supervisor
└── DEPLOY.md                  # 宝塔面板部署指南
```

## 快速开始（本地开发）

### 1. 后端

```bash
cd backend
python -m venv .venv && .venv/Scripts/activate    # Windows
python -m pip install -r requirements.txt

# 可选：配置应用层访问令牌（不配置时交易接口 401，仅公开只读接口可用）
# export BV_API_TOKEN=your-token

python run.py
# UI: http://127.0.0.1:8000   （默认环境为 OKX 模拟盘，配置 API Key 前可查看行情）
```

### 2. 前端开发（热更新）

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173，/api 与 /ws 代理到后端 8000
```

生产构建：`npm run build` → `frontend/dist/`，由后端 `main.py` 同源静态托管。

### 3. 运行测试

```bash
cd backend
PYTHONPATH=. python -m pytest tests/ -q
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BV_HOST` | `127.0.0.1` | 后端监听地址（生产仅内网，由 Nginx 反代） |
| `BV_PORT` | `8000` | 后端监听端口 |
| `BV_API_TOKEN` | 空 | 应用层鉴权令牌。**必须配置**，否则交易/敏感接口一律 401 |
| `BV_OKX_REST_BASE` | `https://www.okx.com` | 大陆服务器连不上主站时切 `https://aws.okx.com` |
| `BV_OKX_WS_PUBLIC` / `BV_OKX_WS_BUSINESS` / `BV_OKX_WS_PRIVATE` | 主站 WS | 对应 AWS 接入点模板见 `deploy/env.example` |

OKX API Key **不在环境变量或仓库中**：登录后在 设置 页面配置，前端提交后 AES-256-GCM 加密落库；提交前运行 `/api/keys/test` 校验。OKX 后台创建 Key 时建议只勾「交易」权限、禁「提币」、绑定本机 IP。

## API 概览（REST `/api` + WS）

常用端点：`/health` `/status` `/instrument` `/venue` `/market/*` `/account/*` `/paper/*` `/brain/*` `/order/*` `/strategies/*` `/backtest/*` `/risk/*` `/roundtrips` `/audit-logs`。

- 所有写方法（POST/PUT/DELETE）必须携带 `Authorization: Bearer <BV_API_TOKEN>`
- 无令牌时白名单：`health/status/instrument/venue/…/market 公开只读`
- 前端高风险下单操作要求 `confirm: true` 字段，后端二次校验

## 部署

服务器部署见 [DEPLOY.md](DEPLOY.md)（宝塔面板：Nginx 反代 + Supervisor 守护 + 一键构建脚本 `deploy/start.sh`）。核心原则：

1. 后端只监听 `127.0.0.1:8000`，公网仅 80/443，必须上 HTTPS
2. 环境变量走 `backend/.env`（gitignore，不入库）
3. `backend/data/`（bitvault.db + secret.key）不入库，升级代码不会覆盖，**务必定期备份**

## 安全与合规

- 仓库不包含任何密钥、令牌、日志或私人数据；敏感配置一律环境变量 / 本地加密文件
- 风控红线（默认 10x 杠杆、支持集白名单之外拒单、限价保护偏移等）实现于 `risk_engine.py`，涉及真实资金前请阅读并调优
- 加密货币合约交易风险极高，可能导致全部本金损失；本项目不构成投资建议，使用者自担风险

---

更多产品与设计细节见 [PRODUCT.md](PRODUCT.md) 与 [BITVAULT_STRATEGY_RESEARCH.md](BITVAULT_STRATEGY_RESEARCH.md)。
