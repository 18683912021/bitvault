"""API Key 本地加密存储（AES-256-GCM）。红线 R8：密钥永不落明文。"""
from __future__ import annotations

import base64
import os
import threading

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app import config

_lock = threading.Lock()
_key_cache: bytes | None = None


def _load_master_key() -> bytes:
    global _key_cache
    if _key_cache is not None:
        return _key_cache
    with _lock:
        if _key_cache is not None:
            return _key_cache
        if os.path.exists(config.KEY_FILE):
            with open(config.KEY_FILE, "rb") as f:
                _key_cache = f.read()
        else:
            _key_cache = AESGCM.generate_key(bit_length=256)
            with open(config.KEY_FILE, "wb") as f:
                f.write(_key_cache)
        return _key_cache


def encrypt(plaintext: str) -> str:
    key = _load_master_key()
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt(token: str) -> str:
    key = _load_master_key()
    raw = base64.b64decode(token.encode("ascii"))
    nonce, ct = raw[:12], raw[12:]
    return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")


def mask(secret: str, visible: int = 4) -> str:
    if len(secret) <= visible:
        return "****"
    return secret[:visible] + "****" + secret[-visible:] if len(secret) > visible * 2 else "****"
