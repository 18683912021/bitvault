"""1H dev 窗口 Walk-Forward：require_setup=True 固定，扫 score_min 稳定区。

目的（Phase 10-12）：确认 1H +require_setup 的 +5.69U/12 trades 不是单窗口偶然，
       且 score_min 参数在邻域稳定（不追单点最优）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 构造 sys.argv 后调用 run_walk_forward.main()
sys.argv = [
    "run_walk_forward.py",
    "--period", "1H",
    "--start", "2022-12-29",
    "--train-months", "9",
    "--test-months", "3",
    "--grid", '{"score_min": [65, 70, 75]}',
    "--fixed", '{"require_setup": true}',
    "--min-trades", "3",      # 1H dev 交易稀疏，放宽到 3 笔/Train 段
    "--no-short",
]

from scripts.run_walk_forward import main  # noqa: E402

if __name__ == "__main__":
    main()
