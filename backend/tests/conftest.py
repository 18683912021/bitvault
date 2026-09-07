# pytest 公共夹具：独立临时 SQLite（每次测试全新库，含迁移与种子）。
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app import config as cfg, db  # noqa: E402


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "DB_PATH", str(tmp_path / "test.db"))
    if getattr(db, "_conn", None) is not None:
        try:
            db._conn.close()
        except Exception:
            pass
        db._conn = None
    db.init()
    yield db
    if getattr(db, "_conn", None) is not None:
        try:
            db._conn.close()
        except Exception:
            pass
        db._conn = None


class FakeRisk:
    """测试用风控替身：默认放行并记录调用。"""

    def __init__(self):
        self.calls = []
        self.lever_cap = 13
        self.state = {"halted": False, "halt_reason": "", "reduce_hint": False}

    def check_pretrade(self, **kw):
        self.calls.append(kw)
        return None
