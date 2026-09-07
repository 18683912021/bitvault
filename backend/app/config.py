"""BitVault 全局配置。"""
from __future__ import annotations

import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "bitvault.db")
KEY_FILE = os.path.join(DATA_DIR, "secret.key")
FRONTEND_DIST = os.path.join(os.path.dirname(BASE_DIR), "frontend", "dist")

HOST = os.environ.get("BV_HOST", "127.0.0.1")
PORT = int(os.environ.get("BV_PORT", "8000"))

# 应用层访问令牌（P0-1 鉴权）：
# 必须通过环境变量配置，禁止硬编码。未配置时 fail-closed：交易/敏感接口一律 401，
# 仅放行公开只读名单（status/market/brain/status|history/venue/instrument/health）。
API_TOKEN = os.environ.get("BV_API_TOKEN", "")

# OKX 接入点：默认主站；大陆服务器连不上 www.okx.com 时，通过环境变量切换
# 官方 AWS 接入点（deploy/env.example 有现成模板），无需改代码。
OKX_REST_BASE = os.environ.get("BV_OKX_REST_BASE", "https://www.okx.com")
OKX_WS_PUBLIC = os.environ.get("BV_OKX_WS_PUBLIC", "wss://ws.okx.com:8443/ws/v5/public")
OKX_WS_BUSINESS = os.environ.get("BV_OKX_WS_BUSINESS", "wss://ws.okx.com:8443/ws/v5/business")
OKX_WS_PRIVATE = os.environ.get("BV_OKX_WS_PRIVATE", "wss://ws.okx.com:8443/ws/v5/private")
OKX_WS_PRIVATE_DEMO = os.environ.get(
    "BV_OKX_WS_PRIVATE_DEMO", "wss://wspap.okx.com:8443/ws/v5/private"
)

# 默认交易标的：OKX 不可达/尚未同步时的兜底（同步后以 OKX 支持集为准）
DEFAULT_INST_ID = "BTC-USDT"

# 支持集筛选：计价与结算货币（OKX USDT 现货 + USDT 永续）
SUPPORTED_QUOTE_CCY = "USDT"

# 标的支持集（红线 R3：支持集之外一律拒单）。
# 运行时由 DataService.load_instruments() 从 OKX 全量同步覆盖（USDT 现货 + USDT 永续）。
class InstrumentRegistry:
    supported: set[str] = {"BTC-USDT", "BTC-USDT-SWAP"}

    @classmethod
    def reset(cls, inst_ids: set[str]) -> None:
        cls.supported = set(inst_ids) or cls.supported

    @classmethod
    def contains(cls, inst_id: str) -> bool:
        return inst_id in cls.supported

# 下单保护：限价 IOC 相对最新价的保护价偏移（红线 R2）
PROTECTIVE_PX_PCT = 0.002

# 合约杠杆上限（红线 R4 相关）——13x 是最大允许，不是默认杠杆
LEVERAGE_CAP = 13

# 回测默认成本
DEFAULT_FEE_SPOT_TAKER = 0.001
DEFAULT_FEE_SWAP_TAKER = 0.0005
DEFAULT_SLIPPAGE = 0.0005

os.makedirs(DATA_DIR, exist_ok=True)
