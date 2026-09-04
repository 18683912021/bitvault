"""回测子进程入口：python run_backtest.py <job.json> <out.json>"""
import json
import sys
import os
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    job_path, out_path = sys.argv[1], sys.argv[2]
    with open(job_path, "r", encoding="utf-8") as f:
        job = json.load(f)

    from app import db

    db.init()

    def progress(done: int, total: int) -> None:
        with open(out_path + ".progress", "w") as f:
            json.dump({"progress": round(done / max(total, 1), 3)}, f)

    try:
        result = _run(job, progress)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"ok": True, **result}, f, ensure_ascii=False)
    except Exception as e:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"ok": False, "error": f"{e}", "trace": traceback.format_exc()[-1000:]}, f, ensure_ascii=False)


def _run(job: dict, progress) -> dict:
    from app.backtest.engine import run_backtest

    return run_backtest(job, progress_cb=progress)


if __name__ == "__main__":
    main()
