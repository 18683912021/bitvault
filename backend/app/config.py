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

OKX_REST_BASE = "https://www.okx.com"
OKX_WS_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"
OKX_WS_BUSINESS = "wss://ws.okx.com:8443/ws/v5/business"
OKX_WS_PRIVATE = "wss://ws.okx.com:8443/ws/v5/private"
OKX_WS_PRIVATE_DEMO = "wss://wspap.okx.com:8443/ws/v5/private"

# 交易标的白名单（红线 R3：白名单之外一律拒单）
INSTRUMENT_WHITELIST = ["BTC-USDT", "BTC-USDT-SWAP"]

# 下单保护：限价 IOC 相对最新价的保护价偏移（红线 R2）
PROTECTIVE_PX_PCT = 0.002

# 合约杠杆上限（红线 R4 相关）
LEVERAGE_CAP = 5.0

# 回测默认成本
DEFAULT_FEE_SPOT_TAKER = 0.001
DEFAULT_FEE_SWAP_TAKER = 0.0005
DEFAULT_SLIPPAGE = 0.0005

os.makedirs(DATA_DIR, exist_ok=True)
