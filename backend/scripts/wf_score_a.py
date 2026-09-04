"""Walk Forward OOS 验证 A（强制形态）：扫描 score_min 稳定区。

15m 24 个月 → 8 窗口（9 train + 3 test），每窗口 Train 选 score_min → Test OOS。
符合验收标准 #1（OOS 有效）#6（参数非单点最优）。

用法：python scripts/wf_score_a.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # backend/
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))                     # backend/scripts/

# 复用 run_walk_forward 的 main，但用 sys.argv 模拟参数（绕过 PowerShell JSON 引号）
if __name__ == "__main__":
    sys.argv = [
        "run_walk_forward.py",
        "--period", "15m",
        "--start", "2024-09-01",
        "--end", "2026-09-01",
        "--train-months", "9",
        "--test-months", "3",
        "--min-trades", "8",
        # score_min 稳定区扫描：65/70/75/80（70 是 V2 生产值，看邻域是否都站得住）
        "--grid", '{"score_min": [65, 70, 75, 80]}',
        "--fixed", '{"require_setup": true, "setup_filter": "breakout_retest"}',
    ]
    from run_walk_forward import main
    main()
