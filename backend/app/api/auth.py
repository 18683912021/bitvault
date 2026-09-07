"""应用层鉴权（P0-1）：Bearer Token + 高风险操作二次确认。

- 未配置 BV_API_TOKEN 时 fail-closed：仅公开只读名单放行（日志提示配置）。
- 写方法（POST/PUT/PATCH/DELETE）：必须携带有效 Bearer Token；
  高风险路径还必须带 confirm=true（防误操作/CSRF 式直接调用）。
- 公开只读名单：GET /api/status、/api/market/*（GET）、/api/brain/status、
  /api/brain/history、GET /api/venue、GET /api/instrument、/api/health。
"""
from __future__ import annotations

import hmac
import logging

from fastapi import Request, Response
from starlette.responses import JSONResponse

from app import config

log = logging.getLogger("bitvault.auth")

PUBLIC_GET_PREFIXES = (
    "/api/status",
    "/api/health",
    "/api/market/",            # GET 行情/预测/支持集（只读）
    "/api/brain/status",
    "/api/brain/history",
    "/api/venue",
    "/api/instrument",
    "/api/notifications",      # 通知：运营/交易事件（不含密钥），可公开只读
)

# 高风险写操作 → 必须 confirm=true（请求体 JSON 中）
HIGH_RISK_PATHS = (
    "/api/order/place",
    "/api/order/cancel",
    "/api/account/close-position",
    "/api/risk/kill-switch",
    "/api/risk/rules",
    "/api/brain/config",
    "/api/brain/autopilot/",
    "/api/venue",
    "/api/instrument",
    "/api/strategies/instances",
    "/api/market/download-history",
)


def is_public_get(request_path: str, method: str) -> bool:
    if method != "GET":
        return False
    return any(request_path.startswith(p) for p in PUBLIC_GET_PREFIXES)


def _check_token(request: Request) -> Response | None:
    """返回 None=通过；否则返回 401 响应。"""
    auth = request.headers.get("authorization", "")
    expected = config.API_TOKEN.strip()
    if not expected:
        # fail-closed：未配置令牌时拒绝一切非公开请求，防止"默认放行"
        return JSONResponse(status_code=401,
                            content={"detail": "服务端未配置 BV_API_TOKEN，交易接口已禁用（只读接口可用，请配置后再试）"})
    if not auth.startswith("Bearer "):
        return JSONResponse(status_code=401, content={"detail": "缺少 Bearer Token"})
    token = auth.split(" ", 1)[1].strip()
    if not token or not hmac.compare_digest(token, expected):
        return JSONResponse(status_code=401, content={"detail": "访问令牌无效"})
    return None


async def check_confirm(request: Request) -> Response | None:
    """高风险 POST：请求体 JSON 中 confirm===true（布尔真值），或 query ?confirm=true
    （无 body 端点如 kill-switch），否则 400。"""
    if str(request.query_params.get("confirm", "")).lower() == "true":
        return None
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict) or body.get("confirm") is not True:
        return JSONResponse(status_code=400,
                            content={"detail": "高风险操作需要 confirm=true（防误操作）"})
    return None


async def auth_middleware(request: Request, call_next):
    if request.url.path.startswith("/api"):
        method = request.method
        res = _check_token(request) if not is_public_get(request.url.path, method) else None
        if res is None and method in ("POST", "PUT", "PATCH", "DELETE"):
            risk = any(request.url.path.startswith(p) for p in HIGH_RISK_PATHS)
            if risk and request.url.path.startswith("/api/"):
                # 跳过 health OK；对 /api/risk/resume 等低危写不需 confirm
                res = await check_confirm(request)
        if res is not None:
            return res
    return await call_next(request)
