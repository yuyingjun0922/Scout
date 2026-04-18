"""
infra/dashboard.py — v1.66 Phase 1 Step 8 行业信号仪表盘

从 info_units + watchlist 聚合单行业信号的结构化视图。零 LLM，纯 SQL + Python。

核心 API：
    build_industry_dashboard(industry_name, db, days=30)
        → 单行业 dashboard dict

    get_all_active_industries_dashboards(db, days=30)
        → 所有 zone='active' 行业的 dashboard 列表

    format_dashboard_as_text(dashboard) → str
        → Phase 1 文本格式（推送前的简化视图）

设计选择：
    - 每个行业仪表盘 = 2 个 SQL query（info_units 批拉 + watchlist 单查）
      所有聚合在 Python 里算，因为 Phase 1 典型数据量 < 10000 行/行业/30天。
      超出时以 MIN(days) 或加 LIMIT 扩展（未启用）。
    - related_industries 是 JSON 数组字符串，用 LIKE %{industry}% 子串匹配
      （v1.66 spec 允许）。'半导体' 匹配 '半导体设备' / '半导体器件' 等，
      符合"大类视图"的预期。精确匹配留给 Phase 2A 的 json_each。
    - snapshot 语义：进入函数时取 snapshot_at = now_utc()，所有查询加
      `created_at < snapshot_at` 过滤（v1.57 决策 6，防止同循环自读）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from infra.db_manager import DatabaseManager
from utils.time_utils import now_utc


# ═══ 常量 ═══

PHASE1_SOURCES: tuple = ("D1", "D4", "V1", "V3", "S4")
POLICY_DIRECTIONS_FULL: tuple = ("supportive", "restrictive", "neutral", "mixed", "null")
MIXED_SUBTYPES_FULL: tuple = ("conflict", "structural", "stage_difference")
CREDIBILITY_LEVELS_FULL: tuple = ("权威", "可靠", "参考", "线索")

LATEST_PREVIEW_LIMIT: int = 5
DENSITY_WEEKS: int = 4
CONTENT_PREVIEW_MAX_LEN: int = 120


# ═══ 入口 ═══


def build_industry_dashboard(
    industry_name: str,
    db: DatabaseManager,
    days: int = 30,
) -> Dict[str, Any]:
    """为 industry_name 生成 dashboard。

    Args:
        industry_name: 行业名（如 '半导体'）
        db: DatabaseManager 实例
        days: 回溯窗口（默认 30 天）

    Returns:
        dashboard dict，结构见模块 docstring。

    Raises:
        ValueError: industry_name 为空或 days < 1
    """
    if not isinstance(industry_name, str) or not industry_name.strip():
        raise ValueError("industry_name must be non-empty str")
    if not isinstance(days, int) or days < 1:
        raise ValueError("days must be int >= 1")

    industry_name = industry_name.strip()
    snapshot_at = now_utc()
    cutoff = _compute_cutoff(snapshot_at, days)

    # Q1：拉取信号（窗口 + 行业匹配 + snapshot 过滤）
    rows = db.query(
        """SELECT id, source, source_credibility, timestamp, category, content,
                  policy_direction, mixed_subtype, related_industries, created_at
           FROM info_units
           WHERE created_at >= ? AND created_at < ?
             AND related_industries LIKE ?
           ORDER BY timestamp DESC""",
        (cutoff, snapshot_at, f"%{industry_name}%"),
    )

    # Q2：watchlist 查询（可能无行）
    wl_row = db.query_one(
        """SELECT industry_id, zone, dimensions, verification_status, gap_status
           FROM watchlist
           WHERE industry_name = ?""",
        (industry_name,),
    )

    return _assemble_dashboard(
        industry_name=industry_name,
        snapshot_at=snapshot_at,
        rows=rows,
        wl_row=wl_row,
    )


def get_all_active_industries_dashboards(
    db: DatabaseManager,
    days: int = 30,
) -> List[Dict[str, Any]]:
    """列出 watchlist 中所有 zone='active' 行业的 dashboard（按名字排序）。"""
    rows = db.query(
        """SELECT industry_name FROM watchlist
           WHERE zone = 'active'
           ORDER BY industry_name"""
    )
    return [
        build_industry_dashboard(r["industry_name"], db, days=days)
        for r in rows
    ]


# ═══ 文本格式化 ═══


def format_dashboard_as_text(dashboard: Dict[str, Any]) -> str:
    """Phase 1 文本格式（推送前的简化视图）。"""
    lines: List[str] = []
    sep = "=" * 72

    lines.append(sep)
    lines.append(f"[Dashboard] 行业={dashboard['industry']}")
    lines.append(f"  snapshot_at: {dashboard['snapshot_at']}")
    lines.append(sep)

    total = dashboard["recent_signals_total"]
    lines.append(f"\n● 近期信号总数: {total}")

    by_source = dashboard["recent_signals_by_source"]
    lines.append("\n● 按信源：")
    for src in PHASE1_SOURCES:
        n = by_source.get(src, 0)
        bar = "#" * min(n, 40) if n else ""
        lines.append(f"    {src}: {n:>4}  {bar}")

    direction = dashboard["policy_direction_distribution"]
    lines.append("\n● 政策方向分布：")
    for d in POLICY_DIRECTIONS_FULL:
        lines.append(f"    {d:<12}: {direction.get(d, 0)}")

    mixed = dashboard["mixed_subtype_breakdown"]
    if any(mixed.get(k, 0) for k in MIXED_SUBTYPES_FULL):
        lines.append("\n● mixed 子类型：")
        for k in MIXED_SUBTYPES_FULL:
            lines.append(f"    {k:<18}: {mixed.get(k, 0)}")

    cred = dashboard["source_credibility_weighted_count"]
    lines.append("\n● 信源可信度分布：")
    for level in CREDIBILITY_LEVELS_FULL:
        lines.append(f"    {level}: {cred.get(level, 0)}")

    fresh = dashboard["data_freshness"]
    lines.append("\n● 数据新鲜度：")
    if fresh.get("oldest_signal_days_ago") is None:
        lines.append("    （无数据）")
    else:
        lines.append(f"    最老信号: {fresh['oldest_signal_days_ago']} 天前")
        lines.append(f"    最新信号: {fresh['newest_signal_days_ago']} 天前")
        lines.append(
            f"    周密度（-4周 → 现在）: {fresh['signal_density_per_week']}"
        )

    wl = dashboard["watchlist_status"]
    if wl:
        lines.append("\n● Watchlist 状态：")
        lines.append(f"    industry_id         : {wl.get('industry_id')}")
        lines.append(f"    zone                : {wl.get('zone')}")
        lines.append(f"    dimensions          : {wl.get('dimensions')}")
        lines.append(f"    verification_status : {wl.get('verification_status')}")
        lines.append(f"    gap_status          : {wl.get('gap_status')}")
    else:
        lines.append("\n● Watchlist 状态: 未加入")

    latest = dashboard["latest_signals"]
    if latest:
        lines.append(f"\n● 最新 {len(latest)} 条信号：")
        for sig in latest:
            ts_short = (sig.get("timestamp") or "")[:19]
            d = sig.get("policy_direction") or "null"
            lines.append(
                f"    [{sig.get('source')}] {ts_short}  direction={d}"
            )
            preview = sig.get("content_preview") or ""
            lines.append(f"      {preview[:80]}")
    else:
        lines.append("\n● 最新信号：（无）")

    return "\n".join(lines)


# ═══ 内部 ═══


def _compute_cutoff(snapshot_at: str, days: int) -> str:
    """snapshot_at - days → UTC ISO 8601 字符串。"""
    dt = datetime.fromisoformat(snapshot_at)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - timedelta(days=days)).isoformat()


def _assemble_dashboard(
    industry_name: str,
    snapshot_at: str,
    rows: List[Any],
    wl_row: Optional[Any],
) -> Dict[str, Any]:
    """从已拉取的 rows + watchlist_row 拼装 dashboard 字典。"""
    by_source = {s: 0 for s in PHASE1_SOURCES}
    direction_dist = {d: 0 for d in POLICY_DIRECTIONS_FULL}
    subtype_dist = {s: 0 for s in MIXED_SUBTYPES_FULL}
    credibility_counts = {c: 0 for c in CREDIBILITY_LEVELS_FULL}

    for r in rows:
        src = r["source"]
        if src in by_source:
            by_source[src] += 1

        d = r["policy_direction"] if r["policy_direction"] else "null"
        if d in direction_dist:
            direction_dist[d] += 1

        if r["policy_direction"] == "mixed":
            st = r["mixed_subtype"]
            if st in subtype_dist:
                subtype_dist[st] += 1

        c = r["source_credibility"]
        if c in credibility_counts:
            credibility_counts[c] += 1

    latest: List[Dict[str, Any]] = []
    for r in rows[:LATEST_PREVIEW_LIMIT]:
        latest.append(
            {
                "id": r["id"],
                "source": r["source"],
                "timestamp": r["timestamp"],
                "category": r["category"],
                "policy_direction": r["policy_direction"],
                "content_preview": _extract_content_preview(r["content"]),
            }
        )

    freshness = _compute_freshness(rows, snapshot_at)

    watchlist_status: Optional[Dict[str, Any]] = None
    if wl_row is not None:
        watchlist_status = {
            "industry_id": wl_row["industry_id"],
            "zone": wl_row["zone"],
            "dimensions": wl_row["dimensions"],
            "verification_status": wl_row["verification_status"],
            "gap_status": wl_row["gap_status"],
        }

    return {
        "industry": industry_name,
        "snapshot_at": snapshot_at,
        "recent_signals_total": len(rows),
        "recent_signals_by_source": by_source,
        "policy_direction_distribution": direction_dist,
        "mixed_subtype_breakdown": subtype_dist,
        "source_credibility_weighted_count": credibility_counts,
        "latest_signals": latest,
        "data_freshness": freshness,
        "watchlist_status": watchlist_status,
    }


def _extract_content_preview(
    content_str: Optional[str],
    max_len: int = CONTENT_PREVIEW_MAX_LEN,
) -> str:
    """content 可能是 JSON（SignalCollector 输出）或纯文本。返回截断的可读预览。"""
    if not content_str:
        return ""
    try:
        obj = json.loads(content_str)
    except (ValueError, TypeError):
        return content_str[:max_len] + ("..." if len(content_str) > max_len else "")

    if isinstance(obj, dict):
        parts: List[str] = []
        title = obj.get("title")
        if title:
            parts.append(str(title))
        summary = obj.get("summary") or obj.get("description")
        if summary:
            parts.append(str(summary))
        if not parts:
            preview = json.dumps(obj, ensure_ascii=False)
        else:
            preview = " — ".join(parts)
    elif isinstance(obj, list):
        preview = json.dumps(obj, ensure_ascii=False)
    else:
        preview = str(obj)

    return preview[:max_len] + ("..." if len(preview) > max_len else "")


def _compute_freshness(rows: List[Any], snapshot_at: str) -> Dict[str, Any]:
    """计算 oldest_days_ago / newest_days_ago / 4 周密度。

    week 分桶（相对 snapshot_at）：
        density[0] = [-28d, -21d)  最早
        density[1] = [-21d, -14d)
        density[2] = [-14d, -7d)
        density[3] = [-7d, 0]       最新
    """
    if not rows:
        return {
            "oldest_signal_days_ago": None,
            "newest_signal_days_ago": None,
            "signal_density_per_week": [0] * DENSITY_WEEKS,
        }

    snap_dt = datetime.fromisoformat(snapshot_at)
    if snap_dt.tzinfo is None:
        snap_dt = snap_dt.replace(tzinfo=timezone.utc)

    def _to_dt(s: str) -> datetime:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    dts = [_to_dt(r["created_at"]) for r in rows]
    oldest_days = (snap_dt - min(dts)).days
    newest_days = (snap_dt - max(dts)).days

    density = [0] * DENSITY_WEEKS
    for dt in dts:
        days_ago = (snap_dt - dt).days
        if days_ago < 0:
            continue  # 理论不会有未来时间；防御
        bucket = days_ago // 7  # 0=last week, 1=two weeks ago, ...
        if bucket < DENSITY_WEEKS:
            # bucket=0 → density[3]（最新），bucket=3 → density[0]（最早）
            density[DENSITY_WEEKS - 1 - bucket] += 1

    return {
        "oldest_signal_days_ago": oldest_days,
        "newest_signal_days_ago": newest_days,
        "signal_density_per_week": density,
    }
