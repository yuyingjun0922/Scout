#!/usr/bin/env python
"""
scripts/cold_start.py — Phase 1 Step 13 冷启动录入

把用户的真实输入写进 Scout：
    industries   → watchlist                          （14 active + 1 observation = 15 行）
    holdings     → track_list                         （Phase 2A 启用；Phase 1 仅录入）
    principles   → system_meta('user_principles')     （JSON 数组，string 或 dict）
    user_context → system_meta('user_context')        （JSON 对象）

用法：
    python scripts/cold_start.py                                 # 全按 YAML
    python scripts/cold_start.py --interactive                   # 交互补填 holdings / principles
    python scripts/cold_start.py --dry-run                       # 预览不写
    python scripts/cold_start.py --config my.yaml                # 自定义配置文件
    python scripts/cold_start.py --db data/test_knowledge.db     # 切测试库

幂等：
    - watchlist：industry_name 唯一 → 存在则 UPDATE，否则 INSERT
    - track_list：stock 主键 → 存在则 UPDATE，否则 INSERT
    - system_meta('user_principles' / 'user_context')：INSERT OR REPLACE（整体替换）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from infra.db_manager import DatabaseManager  # noqa: E402
from knowledge.init_db import init_database  # noqa: E402
from utils.time_utils import now_utc  # noqa: E402


DEFAULT_CONFIG = PROJECT_ROOT / "scripts" / "cold_start_config.yaml"
DEFAULT_DB = PROJECT_ROOT / "data" / "knowledge.db"

MAX_PRINCIPLES = 5
VALID_ZONES = {"active", "cold", "observation", "observe_new_direction",
               "cycle_bottom"}
VALID_MARKETS = {"A", "KR", "US"}

SYSTEM_META_PRINCIPLES_KEY = "user_principles"
SYSTEM_META_USER_CONTEXT_KEY = "user_context"

# user_context 里建议但不强制存在的键（只用于提示，非白名单）
RECOMMENDED_USER_CONTEXT_KEYS = (
    "investor_type", "capital_range", "holding_horizon",
    "markets", "tech_background", "tools_stack",
    "api_key_status", "phase",
)


# ══════════════════════ YAML 加载 & 校验 ══════════════════════


def load_config(config_path: Path) -> Dict[str, Any]:
    if not config_path.exists():
        raise FileNotFoundError(f"config not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(
            f"config must be a mapping, got {type(data).__name__}: {config_path}"
        )
    data.setdefault("industries", [])
    data.setdefault("holdings", [])
    data.setdefault("principles", [])
    data.setdefault("user_context", {})
    return data


def validate_industries(industries: List[Dict[str, Any]]) -> List[str]:
    """返回错误信息列表；空列表表示全部合法。"""
    errors: List[str] = []
    for i, ind in enumerate(industries):
        if not isinstance(ind, dict):
            errors.append(f"industry[{i}]: not a mapping")
            continue
        name = ind.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"industry[{i}]: name must be non-empty str")
            continue
        zone = ind.get("zone")
        if zone is not None and zone not in VALID_ZONES:
            errors.append(
                f"industry[{name}]: zone {zone!r} not in {sorted(VALID_ZONES)}"
            )
        subs = ind.get("sub_industries")
        if subs is not None and not isinstance(subs, list):
            errors.append(
                f"industry[{name}]: sub_industries must be list or null"
            )
        mt = ind.get("motivation_tags")
        if mt is not None and not isinstance(mt, list):
            errors.append(
                f"industry[{name}]: motivation_tags must be list or null"
            )
    return errors


def validate_holdings(holdings: List[Dict[str, Any]]) -> List[str]:
    errors: List[str] = []
    for i, h in enumerate(holdings):
        if not isinstance(h, dict):
            errors.append(f"holding[{i}]: not a mapping")
            continue
        stock = h.get("stock")
        if not isinstance(stock, str) or not stock.strip():
            errors.append(f"holding[{i}]: stock must be non-empty str")
        market = h.get("market")
        if market not in VALID_MARKETS:
            errors.append(
                f"holding[{stock}]: market {market!r} not in {sorted(VALID_MARKETS)}"
            )
        shares = h.get("shares")
        if shares is not None and not isinstance(shares, int):
            errors.append(f"holding[{stock}]: shares must be int")
        price = h.get("buy_price")
        if price is not None and not isinstance(price, (int, float)):
            errors.append(f"holding[{stock}]: buy_price must be number")
    return errors


def validate_principles(principles: List[Any]) -> List[str]:
    """接受两种形态的 principle：
        1. 非空 str（interactive 模式产物 / 简短经验）
        2. dict，必须有非空 title 键（结构化原则，如 id/title/core/application/warnings/source）
    """
    errors: List[str] = []
    if not isinstance(principles, list):
        errors.append(f"principles: must be list, got {type(principles).__name__}")
        return errors
    if len(principles) > MAX_PRINCIPLES:
        errors.append(
            f"principles: max {MAX_PRINCIPLES} items, got {len(principles)}"
        )
    for i, p in enumerate(principles):
        if isinstance(p, str):
            if not p.strip():
                errors.append(f"principle[{i}]: empty string")
        elif isinstance(p, dict):
            title = p.get("title")
            if not isinstance(title, str) or not title.strip():
                errors.append(
                    f"principle[{i}]: dict form requires non-empty 'title'"
                )
        else:
            errors.append(
                f"principle[{i}]: must be str or dict, got {type(p).__name__}"
            )
    return errors


def validate_user_context(ctx: Any) -> List[str]:
    """user_context 是自由形态 dict（Phase 1）。None / {} 视为未提供（skip）。"""
    errors: List[str] = []
    if ctx is None:
        return errors
    if not isinstance(ctx, dict):
        errors.append(f"user_context: must be mapping, got {type(ctx).__name__}")
        return errors
    # 值必须 JSON 可序列化（避免隐藏错误）
    try:
        json.dumps(ctx, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        errors.append(f"user_context: not JSON serializable: {e}")
    return errors


# ══════════════════════ 交互补填 ══════════════════════


def prompt_holdings(
    existing: List[Dict[str, Any]],
    stdin: Any = None,
    stdout: Any = None,
) -> List[Dict[str, Any]]:
    """交互式补填 holdings。空回车 / 'q' 退出。

    stdin / stdout 可注入以便测试。
    """
    s_in = stdin or sys.stdin
    s_out = stdout or sys.stdout

    def _print(msg: str = ""):
        print(msg, file=s_out)

    def _input(prompt: str) -> str:
        print(prompt, end="", file=s_out, flush=True)
        return s_in.readline().strip()

    out = list(existing)
    _print("\n─── 持仓录入（输入 'q' 或直接空行结束）───")
    _print("  格式：stock / market(A/KR/US) / shares / buy_price / buy_date(YYYY-MM-DD)")
    while True:
        stock = _input("stock code (空行跳过): ")
        if not stock or stock.lower() == "q":
            break
        market = _input(f"  market for {stock} (A/KR/US): ").upper()
        if market not in VALID_MARKETS:
            _print(f"  [!] market 必须是 A/KR/US，跳过 {stock}")
            continue
        try:
            shares_raw = _input(f"  shares for {stock}: ")
            shares = int(shares_raw) if shares_raw else 0
            price_raw = _input(f"  buy_price for {stock}: ")
            buy_price = float(price_raw) if price_raw else 0.0
        except ValueError as e:
            _print(f"  [!] 数字解析失败 ({e})，跳过 {stock}")
            continue
        company = _input(f"  company_name (可选): ")
        industry = _input(f"  industry (可选): ")
        buy_date = _input(f"  buy_date YYYY-MM-DD (可选，默认今天): ")
        if not buy_date:
            buy_date = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        out.append({
            "stock": stock,
            "market": market,
            "shares": shares,
            "buy_price": buy_price,
            "company_name": company or None,
            "industry": industry or None,
            "buy_date": buy_date,
        })
        _print(f"  ✓ added {stock}")
    return out


def prompt_principles(
    existing: List[str],
    stdin: Any = None,
    stdout: Any = None,
) -> List[str]:
    s_in = stdin or sys.stdin
    s_out = stdout or sys.stdout

    def _print(msg: str = ""):
        print(msg, file=s_out)

    def _input(prompt: str) -> str:
        print(prompt, end="", file=s_out, flush=True)
        return s_in.readline().strip()

    out = list(existing)
    _print(f"\n─── 投资经验/教训（最多 {MAX_PRINCIPLES} 条，已填 {len(out)}）───")
    _print("  一行一条，简短+可操作；空行结束")
    while len(out) < MAX_PRINCIPLES:
        line = _input(f"principle #{len(out)+1}: ")
        if not line:
            break
        out.append(line)
        _print(f"  ✓ added")
    return out


# ══════════════════════ 写入 DB ══════════════════════


def seed_industries(
    db: DatabaseManager,
    industries: List[Dict[str, Any]],
    dry_run: bool = False,
) -> Dict[str, int]:
    """UPSERT industries → watchlist。返回 {inserted, updated}."""
    inserted = 0
    updated = 0
    for ind in industries:
        name = ind["name"].strip()
        zone = ind.get("zone") or "active"
        subs = ind.get("sub_industries") or []
        tags = ind.get("motivation_tags") or []
        primary_market = ind.get("primary_market") or ""
        base_notes = (ind.get("notes") or "").strip()

        # 把 primary_market 和 sub_industries 的信息并入 notes（schema 没有专用列）
        notes_parts = []
        if primary_market:
            notes_parts.append(f"[market={primary_market}]")
        if subs:
            notes_parts.append(f"[subs={', '.join(subs)}]")
        if base_notes:
            notes_parts.append(base_notes)
        notes = " ".join(notes_parts) if notes_parts else None

        sub_json = json.dumps(subs, ensure_ascii=False) if subs else None
        tags_json = json.dumps(tags, ensure_ascii=False) if tags else None
        entered_at = now_utc()

        existing = db.query_one(
            "SELECT industry_id FROM watchlist WHERE industry_name=?", (name,)
        )
        if existing:
            if not dry_run:
                db.write(
                    """UPDATE watchlist SET
                       industry_aliases=?, zone=?, motivation_levels=?,
                       notes=?
                       WHERE industry_id=?""",
                    (sub_json, zone, tags_json, notes,
                     existing["industry_id"]),
                )
            updated += 1
        else:
            if not dry_run:
                db.write(
                    """INSERT INTO watchlist
                       (industry_name, industry_aliases, zone,
                        motivation_levels, notes, entered_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (name, sub_json, zone, tags_json, notes, entered_at),
                )
            inserted += 1
    return {"inserted": inserted, "updated": updated}


def seed_holdings(
    db: DatabaseManager,
    holdings: List[Dict[str, Any]],
    dry_run: bool = False,
) -> Dict[str, int]:
    inserted = 0
    updated = 0
    now = now_utc()
    for h in holdings:
        stock = h["stock"].strip()
        existing = db.query_one(
            "SELECT stock FROM track_list WHERE stock=?", (stock,)
        )
        market = h.get("market")
        industry = h.get("industry")
        shares = int(h.get("shares") or 0)
        buy_price = float(h.get("buy_price") or 0.0)
        buy_date = h.get("buy_date") or now
        company = h.get("company_name")
        reason = h.get("notes") or ""

        if existing:
            if not dry_run:
                db.write(
                    """UPDATE track_list SET
                       company_name=?, market=?, industry=?,
                       actual_buy_price=?, actual_buy_date=?, actual_shares=?,
                       recommend_reason=?, updated_at=?
                       WHERE stock=?""",
                    (company, market, industry,
                     buy_price, buy_date, shares,
                     reason, now, stock),
                )
            updated += 1
        else:
            if not dry_run:
                db.write(
                    """INSERT INTO track_list
                       (stock, company_name, market, industry,
                        actual_buy_price, actual_buy_date, actual_shares,
                        recommend_reason, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (stock, company, market, industry,
                     buy_price, buy_date, shares,
                     reason, now),
                )
            inserted += 1
    return {"inserted": inserted, "updated": updated}


def seed_principles(
    db: DatabaseManager,
    principles: List[Any],
    dry_run: bool = False,
) -> Dict[str, Any]:
    """整体替换 system_meta('user_principles') 的 JSON 数组值。

    空 list → skip（保留已有）。非空 → INSERT（首次）/ UPDATE（已存在）。
    """
    if not principles:
        return {
            "written": 0, "skipped_empty": True,
            "action": "skipped", "count": 0,
        }

    existing = db.query_one(
        "SELECT key FROM system_meta WHERE key=?",
        (SYSTEM_META_PRINCIPLES_KEY,),
    )
    was_existing = existing is not None

    if not dry_run:
        value = json.dumps(principles, ensure_ascii=False)
        now = now_utc()
        db.write(
            """INSERT OR REPLACE INTO system_meta (key, value, updated_at)
               VALUES (?, ?, ?)""",
            (SYSTEM_META_PRINCIPLES_KEY, value, now),
        )

    return {
        "written": len(principles),
        "skipped_empty": False,
        "action": "updated" if was_existing else "inserted",
        "count": len(principles),
    }


def seed_user_context(
    db: DatabaseManager,
    ctx: Optional[Dict[str, Any]],
    dry_run: bool = False,
) -> Dict[str, Any]:
    """整体替换 system_meta('user_context') 的 JSON 对象值。

    空 / None → skip（保留已有）。
    """
    if not ctx or not isinstance(ctx, dict):
        return {"written": 0, "skipped_empty": True, "action": "skipped"}

    existing = db.query_one(
        "SELECT key FROM system_meta WHERE key=?",
        (SYSTEM_META_USER_CONTEXT_KEY,),
    )
    was_existing = existing is not None

    if not dry_run:
        value = json.dumps(ctx, ensure_ascii=False)
        now = now_utc()
        db.write(
            """INSERT OR REPLACE INTO system_meta (key, value, updated_at)
               VALUES (?, ?, ?)""",
            (SYSTEM_META_USER_CONTEXT_KEY, value, now),
        )

    return {
        "written": 1,
        "skipped_empty": False,
        "action": "updated" if was_existing else "inserted",
        "keys": sorted(ctx.keys()),
    }


# ══════════════════════ 展示 ══════════════════════


def print_preview(seed_data: Dict[str, Any]) -> None:
    industries = seed_data["industries"]
    holdings = seed_data["holdings"]
    principles = seed_data["principles"]
    user_context = seed_data.get("user_context") or {}

    print("\n" + "=" * 64)
    print("Scout 冷启动录入预览")
    print("=" * 64)

    print(f"\n[industries] {len(industries)} 条待录入")
    by_zone: Dict[str, int] = {}
    for ind in industries:
        z = ind.get("zone") or "active"
        by_zone[z] = by_zone.get(z, 0) + 1
    for z, n in sorted(by_zone.items()):
        print(f"  {z:<25}: {n}")
    for ind in industries:
        n = ind["name"]
        z = ind.get("zone") or "active"
        subs = ind.get("sub_industries") or []
        mkt = ind.get("primary_market") or ""
        print(
            f"  - {n:<14} [{z:<12}] market={mkt:<10} "
            f"subs={len(subs)}"
        )

    print(f"\n[holdings] {len(holdings)} 条待录入")
    for h in holdings:
        print(
            f"  - {h.get('stock')} ({h.get('market')}) "
            f"×{h.get('shares', 0)} @ {h.get('buy_price', 0.0)}"
        )

    print(f"\n[principles] {len(principles)} 条待录入")
    for i, p in enumerate(principles, 1):
        if isinstance(p, dict):
            pid = p.get("id") or f"#{i}"
            title = p.get("title", "(untitled)")
            core = p.get("core", "")
            print(f"  {i}. [{pid}] {title}")
            if core:
                print(f"     核心：{core}")
        else:
            print(f"  {i}. {p}")

    print(f"\n[user_context] {len(user_context)} 个字段待录入")
    for k in sorted(user_context.keys()):
        v = user_context[k]
        if isinstance(v, list):
            v_str = ", ".join(str(x) for x in v)
        else:
            v_str = str(v)
        # 截断过长值
        if len(v_str) > 60:
            v_str = v_str[:60] + "..."
        print(f"  - {k:<22}: {v_str}")


def print_result(result: Dict[str, Any]) -> None:
    print("\n" + "=" * 64)
    print("Scout 冷启动录入结果")
    print("=" * 64)

    wl = result["industries"]
    print(
        f"\n[watchlist] +{wl['inserted']} inserted, "
        f"{wl['updated']} updated"
    )

    tl = result["holdings"]
    print(
        f"\n[track_list] +{tl['inserted']} inserted, "
        f"{tl['updated']} updated"
    )

    pr = result["principles"]
    if pr.get("skipped_empty"):
        print("\n[principles] skipped (empty list in YAML)")
    else:
        action = pr.get("action", "written")
        print(
            f"\n[principles] {pr['written']} items "
            f"(action={action}) → system_meta.user_principles"
        )

    uc = result.get("user_context") or {}
    if uc.get("skipped_empty"):
        print("\n[user_context] skipped (empty in YAML)")
    elif uc:
        action = uc.get("action", "written")
        keys = uc.get("keys") or []
        print(
            f"\n[user_context] 1 row (action={action}, {len(keys)} keys) "
            f"→ system_meta.user_context"
        )
        if keys:
            print(f"              keys: {', '.join(keys)}")

    if result.get("dry_run"):
        print("\n[note] dry-run — 实际未写入 DB")


# ══════════════════════ 入口 ══════════════════════


def run_cold_start(
    *,
    db_path: Path,
    config_path: Path,
    interactive: bool = False,
    dry_run: bool = False,
    stdin: Any = None,
    stdout: Any = None,
) -> Dict[str, Any]:
    """无 CLI 副作用的可测试入口。返回 result dict。"""
    cfg = load_config(config_path)

    errs: List[str] = []
    errs += validate_industries(cfg["industries"])
    errs += validate_holdings(cfg["holdings"])
    errs += validate_principles(cfg["principles"])
    errs += validate_user_context(cfg.get("user_context"))
    if errs:
        raise ValueError(
            "config validation failed:\n  " + "\n  ".join(errs)
        )

    if interactive:
        cfg["holdings"] = prompt_holdings(
            cfg["holdings"], stdin=stdin, stdout=stdout
        )
        cfg["principles"] = prompt_principles(
            cfg["principles"], stdin=stdin, stdout=stdout
        )
        # 再校验一次（交互可能引入问题）
        errs2 = (
            validate_holdings(cfg["holdings"])
            + validate_principles(cfg["principles"])
        )
        if errs2:
            raise ValueError(
                "interactive input validation failed:\n  "
                + "\n  ".join(errs2)
            )

    if not db_path.exists():
        init_database(db_path)

    db = DatabaseManager(db_path)
    try:
        result = {
            "industries": seed_industries(db, cfg["industries"], dry_run),
            "holdings": seed_holdings(db, cfg["holdings"], dry_run),
            "principles": seed_principles(db, cfg["principles"], dry_run),
            "user_context": seed_user_context(
                db, cfg.get("user_context"), dry_run
            ),
            "dry_run": dry_run,
        }
    finally:
        db.close()
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Scout Phase 1 Cold Start 录入")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help=f"YAML 配置（默认 {DEFAULT_CONFIG.relative_to(PROJECT_ROOT)}）")
    parser.add_argument("--db", default=str(DEFAULT_DB),
                        help=f"knowledge.db 路径（默认 {DEFAULT_DB.relative_to(PROJECT_ROOT)}）")
    parser.add_argument("--interactive", action="store_true",
                        help="交互补填 holdings / principles")
    parser.add_argument("--dry-run", action="store_true",
                        help="只预览不写 DB")
    parser.add_argument("--yes", action="store_true",
                        help="跳过确认执行提示（CI/自动化用）")
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    db_path = Path(args.db)

    try:
        cfg = load_config(config_path)
    except Exception as e:
        print(f"[fatal] config load: {e}", file=sys.stderr)
        return 2

    errs = (
        validate_industries(cfg["industries"])
        + validate_holdings(cfg["holdings"])
        + validate_principles(cfg["principles"])
        + validate_user_context(cfg.get("user_context"))
    )
    if errs:
        print("[fatal] config validation failed:", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        return 2

    # 若 interactive 在这个阶段补填（main 里做，方便 run_cold_start 不写 sys.stdin）
    if args.interactive:
        cfg["holdings"] = prompt_holdings(cfg["holdings"])
        cfg["principles"] = prompt_principles(cfg["principles"])

    # 预览
    print_preview(cfg)

    # 确认
    if not args.dry_run and not args.yes:
        print("\n确认执行？(y/N): ", end="", flush=True)
        answer = sys.stdin.readline().strip().lower()
        if answer not in ("y", "yes"):
            print("[abort] 未确认，退出。")
            return 0

    # 正式写入（复用 run_cold_start 但不再 interactive）
    result = run_cold_start(
        db_path=db_path,
        config_path=config_path,
        interactive=False,  # 已在上面补填
        dry_run=args.dry_run,
    )
    # run_cold_start 读了同一个文件 —— 但 interactive 模式的补填在此丢失
    # 故若 interactive 已补填 cfg，我们直接用 cfg 写；绕过 run_cold_start
    if args.interactive:
        db = DatabaseManager(db_path)
        try:
            result = {
                "industries": seed_industries(db, cfg["industries"], args.dry_run),
                "holdings": seed_holdings(db, cfg["holdings"], args.dry_run),
                "principles": seed_principles(db, cfg["principles"], args.dry_run),
                "user_context": seed_user_context(
                    db, cfg.get("user_context"), args.dry_run
                ),
                "dry_run": args.dry_run,
            }
        finally:
            db.close()

    print_result(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
