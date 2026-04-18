#!/usr/bin/env python
"""
scripts/test_mcp_real.py — Step 10 MCP Server 真 stdio 烟囱

启动 `python -m infra.mcp_server` 子进程，通过 stdio 连接，调用全部 10 个工具，
验证：
  - initialize 握手
  - list_tools 返 10 个
  - call_tool 各自返 structured dict

DB 选择：
  - 默认 data/test_knowledge.db（通过 SCOUT_DB_PATH env 传给子进程）
  - --prod-db 切 data/knowledge.db

用法：
  python scripts/test_mcp_real.py
  python scripts/test_mcp_real.py --prod-db
  python scripts/test_mcp_real.py --tool get_watchlist
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


def _parse_tool_result(result: Any) -> Dict[str, Any]:
    """把 CallToolResult → dict（解 JSON 或直接字典）."""
    content = getattr(result, "content", None)
    if not content:
        return {}
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"_raw_text": text}
    return {}


def _pp(title: str, payload: Any, max_chars: int = 800) -> None:
    print(f"\n--- {title} ---")
    if isinstance(payload, (dict, list)):
        s = json.dumps(payload, ensure_ascii=False, indent=2)
    else:
        s = str(payload)
    if len(s) > max_chars:
        s = s[:max_chars] + f"\n... (truncated, total {len(s)} chars)"
    print(s)


# ══ call 顺序：依赖链——先 query，后 add/remove，再 re-query ══

DEFAULT_CALL_SEQUENCE = [
    ("get_system_status", {}),
    ("get_watchlist", {}),
    ("ask_industry", {"industry": "半导体", "days": 30}),
    ("search_signals", {"query": "半导体", "days": 30, "limit": 5}),
    ("get_latest_weekly_report", {"type": "industry"}),
    ("add_industry", {"industry": "MCP测试_光伏", "reason": "scripts/test_mcp_real.py"}),
    ("add_industry", {"industry": "MCP测试_光伏", "reason": "verify idempotent"}),  # 第二次
    ("remove_industry", {"industry": "MCP测试_光伏", "reason": "烟囱完成"}),
    ("get_industry_full_context", {"industry": "半导体"}),
    ("get_decision_context", {"stock": "600519"}),
]


async def _probe_first_info_unit_id(session: ClientSession) -> Optional[str]:
    """search_signals → 取第一条 id，喂给 get_policy_for_motivation_analysis。"""
    for query in ("通知", "政策", "半导体", "arxiv", "paper"):
        r = await session.call_tool(
            "search_signals", {"query": query, "days": 365, "limit": 1}
        )
        parsed = _parse_tool_result(r)
        if parsed.get("ok") and parsed.get("signals"):
            return parsed["signals"][0]["id"]
    return None


async def run(
    db_path: str,
    single_tool: Optional[str] = None,
    server_script: Optional[Path] = None,
) -> int:
    script = server_script or (PROJECT_ROOT / "infra" / "mcp_server.py")

    env = dict(os.environ)
    env["SCOUT_DB_PATH"] = db_path
    env["PYTHONIOENCODING"] = "utf-8"

    params = StdioServerParameters(
        command=sys.executable,
        args=["-u", str(script)],  # -u 关闭 stdout 缓冲
        env=env,
        cwd=str(PROJECT_ROOT),
    )

    print(f"[setup] spawning: {sys.executable} -u {script}")
    print(f"[setup] SCOUT_DB_PATH={db_path}")

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            # 握手
            init_result = await session.initialize()
            srv_info = init_result.serverInfo
            print(f"\n[init] server={srv_info.name} v={srv_info.version}")

            # 列工具
            tools_result = await session.list_tools()
            tool_names = [t.name for t in tools_result.tools]
            print(f"[tools] {len(tool_names)}: {tool_names}")
            if len(tool_names) != 10:
                print(f"[fail] expected 10 tools, got {len(tool_names)}")
                return 1

            # ── 执行调用矩阵 ──
            sequence = DEFAULT_CALL_SEQUENCE[:]
            if single_tool:
                sequence = [(name, args) for name, args in sequence if name == single_tool]
                if not sequence:
                    print(f"[warn] --tool {single_tool} 不在默认序列，尝试空参调用")
                    sequence = [(single_tool, {})]

            for tool_name, args in sequence:
                print(f"\n{'=' * 70}")
                print(f"[call] {tool_name}({args})")
                try:
                    result = await session.call_tool(tool_name, args)
                except Exception as e:
                    print(f"[error] {type(e).__name__}: {e}")
                    continue
                parsed = _parse_tool_result(result)
                ok = parsed.get("ok", True)
                badge = "OK" if ok else "FAIL"
                print(f"[{badge}] keys: {sorted(parsed.keys())[:10]}")
                _pp(tool_name, parsed, max_chars=600)

            # ── 动态探测一个真实 info_unit_id 喂 get_policy ──
            print(f"\n{'=' * 70}")
            print("[probe] 取一个真实 info_unit_id 喂 get_policy_for_motivation_analysis")
            uid = await _probe_first_info_unit_id(session)
            if uid:
                print(f"[probe] 使用 info_unit_id={uid}")
                result = await session.call_tool(
                    "get_policy_for_motivation_analysis",
                    {"info_unit_id": uid},
                )
                _pp("get_policy_for_motivation_analysis", _parse_tool_result(result), max_chars=800)
            else:
                print("[probe] 没有可用的 info_unit，跳过 get_policy 调用")

    print("\n[done] MCP 会话优雅关闭")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Scout MCP Server stdio 真烟囱")
    parser.add_argument("--prod-db", action="store_true", help="用 data/knowledge.db")
    parser.add_argument("--tool", default=None, help="只调某个工具")
    args = parser.parse_args()

    db_name = "knowledge.db" if args.prod_db else "test_knowledge.db"
    db_path = PROJECT_ROOT / "data" / db_name
    if not db_path.exists():
        print(f"[error] DB 不存在：{db_path}。先跑采集 / init_db。")
        return 1

    try:
        return asyncio.run(run(str(db_path), single_tool=args.tool))
    except KeyboardInterrupt:
        print("\n[abort] 用户中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
