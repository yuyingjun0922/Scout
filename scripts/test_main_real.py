#!/usr/bin/env python
"""
scripts/test_main_real.py — Step 12 main.py 真子进程烟囱

启动 `python main.py serve --max-runtime-seconds N`（子进程），等其自然退出，
验证：
    1. 进程返回码为 0（优雅关闭）
    2. logs/scout.log 出现 banner / 9 job registered / consumer loop started / shutdown complete
    3. 进程结束后 db 文件仍正常（WAL 未锁死）

同时：
    - 跑一次 status 看快照输出正常
    - 跑一次 collect --source D1（可选，依赖网络；--skip-collect 关闭）

DB 用 tmp 目录，不污染 data/。

用法：
    python scripts/test_main_real.py
    python scripts/test_main_real.py --runtime 10
    python scripts/test_main_real.py --skip-collect
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run_cmd(
    argv: list,
    cwd: Path,
    env: dict,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    """统一子进程入口：UTF-8 + 无 shell + capture。"""
    return subprocess.run(
        argv,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        timeout=timeout,
    )


def _decode(b: bytes) -> str:
    return b.decode("utf-8", errors="replace")


def run_subcommand_smoke(args):
    tmp = Path(tempfile.mkdtemp(prefix="scout_main_smoke_"))
    kdb = tmp / "knowledge.db"
    qdb = tmp / "queue.db"
    logs_dir = tmp / "logs"
    reports_dir = tmp / "reports"

    env = dict(os.environ)
    env.update({
        "PYTHONIOENCODING": "utf-8",
        "SCOUT_DB_PATH": str(kdb),
        "SCOUT_QUEUE_DB_PATH": str(qdb),
    })
    # 替换 logs/ 目录：通过 cwd 让 main.py 默认往 tmp/logs 写
    # main.py 中 LOGS_DIR = PROJECT_ROOT / "logs" — 我们没参数化
    # 所以用 cwd=tmp 不行（会找不到 main.py）
    # 妥协：logs 会写到 <project>/logs，测试结束后人工查

    py = sys.executable
    main_py = str(PROJECT_ROOT / "main.py")

    # ── 1. --help ──
    print("\n[1/5] main.py --help")
    r = _run_cmd([py, main_py, "--help"], PROJECT_ROOT, env)
    assert r.returncode == 0
    assert b"Scout Phase 1 unified CLI" in r.stdout

    # ── 2. status ──
    print("[2/5] main.py status (tmp db)")
    r = _run_cmd([py, main_py, "status"], PROJECT_ROOT, env)
    if r.returncode != 0:
        print("[fail] status output:\n" + _decode(r.stderr))
    assert r.returncode == 0
    out = _decode(r.stdout)
    assert "Scout 系统状态" in out
    assert "健康度" in out
    # 新 DB 空 + 没任何 source 采集过 → RED
    assert "RED" in out, f"expected RED for empty db, got:\n{out[-500:]}"

    # ── 3. serve --max-runtime-seconds=N ──
    runtime = args.runtime
    print(f"[3/5] main.py serve --max-runtime-seconds {runtime}")
    r = _run_cmd(
        [py, main_py, "serve", "--max-runtime-seconds", str(runtime)],
        PROJECT_ROOT, env,
        timeout=runtime + 30,
    )
    assert r.returncode == 0, (
        f"serve exit={r.returncode}\n"
        f"stderr:\n{_decode(r.stderr)[-1500:]}"
    )
    stderr = _decode(r.stderr)
    for expected in (
        "Scout Phase 1",
        "[schedule] 9 jobs registered",
        "[consumer] signal processor loop started",
        f"max_runtime_seconds={float(runtime)} reached",
        "[consumer] signal processor loop stopped",
        "[serve] shutdown complete",
    ):
        assert expected in stderr, (
            f"missing '{expected}' in serve stderr; full:\n{stderr[-1500:]}"
        )

    # ── 4. collect --source D1（可选）──
    if not args.skip_collect:
        print("[4/5] main.py collect --source D1 --days 3 (网络)")
        r = _run_cmd(
            [py, main_py, "collect", "--source", "D1", "--days", "3"],
            PROJECT_ROOT, env,
            timeout=120,
        )
        if r.returncode == 0:
            out = _decode(r.stdout)
            print(f"       stdout: {out.strip()}")
            assert "D1: +" in out
        else:
            # 网络波动是可能的；打警告但不 fail 整个测试
            print(f"[warn] collect failed (rc={r.returncode}): "
                  f"{_decode(r.stderr)[-500:]}")
    else:
        print("[4/5] skipped collect (--skip-collect)")

    # ── 5. status 再看一次 ──
    print("[5/5] main.py status (after serve)")
    r = _run_cmd([py, main_py, "status"], PROJECT_ROOT, env, timeout=30)
    assert r.returncode == 0
    out = _decode(r.stdout)
    print("\n" + "=" * 60)
    print("最终 status 输出（节选）:")
    print("=" * 60)
    print(out)

    print(f"\n[cleanup] tmp dir: {tmp} (保留给查日志)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="main.py real-subprocess smoke")
    parser.add_argument("--runtime", type=int, default=8,
                        help="serve 模式跑 N 秒后自动退（默认 8）")
    parser.add_argument("--skip-collect", action="store_true",
                        help="跳过联网 D1 采集")
    args = parser.parse_args()

    try:
        return run_subcommand_smoke(args)
    except AssertionError as e:
        print(f"\n[FAIL] assertion: {e}")
        return 1
    except subprocess.TimeoutExpired as e:
        print(f"\n[FAIL] timeout: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
