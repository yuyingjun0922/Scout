"""告警抑制模块 — v1.16 SUPPRESSED_ERRORS 外化。

解决 TD-002：模块级常量固化在进程启动时，代码更新不重启进程不生效。

设计原则（对齐 P1 异常可见性）：
- 配置外化到 config/suppressions.yaml（不重启可刷新）
- 30s TTL cache 避免每次调用都 IO
- 启动时打印加载内容（让"实际生效规则"可见）
- 文件找不到或 parse 失败时返回 empty dict + WARN log，不 raise
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger("scout.suppress")  # 挂 scout tree, 继承 scout.log 的 file handler

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "suppressions.yaml"
_CACHE_TTL_SECONDS = 30

_lock = threading.Lock()
_cache: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None
_cache_at: Optional[datetime] = None


def _parse_suppressions_yaml(path: Path) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """从 yaml 载入 → {(agent_name, pattern): {reason, until, tracking}} 结构。"""
    if not path.exists():
        logger.warning(f"[suppress] config not found: {path}, returning empty")
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError) as e:
        logger.warning(f"[suppress] failed to parse {path}: {e}, returning empty")
        return {}

    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for entry in raw.get("suppressions", []) or []:
        try:
            agent = entry["agent_name"]
            pattern = entry["pattern"]
            until_raw = entry.get("until")
            # until 支持 ISO 8601 with tz (e.g. "2026-05-01T23:59:59+09:00")
            until = None
            if until_raw:
                until = datetime.fromisoformat(until_raw)
                if until.tzinfo is None:
                    # 裸日期视为 KST 23:59:59 对齐用户直觉
                    until = until.replace(hour=23, minute=59, second=59)
            out[(agent, pattern)] = {
                "reason": entry.get("reason", ""),
                "until": until,
                "tracking": entry.get("tracking", ""),
            }
        except KeyError as e:
            logger.warning(f"[suppress] entry missing field {e}: {entry}")
    return out


def get_suppressions(force_reload: bool = False) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """取得当前生效的抑制规则，带 30s TTL cache。"""
    global _cache, _cache_at
    with _lock:
        now = datetime.now(tz=timezone.utc)
        if (
            force_reload
            or _cache is None
            or _cache_at is None
            or (now - _cache_at).total_seconds() > _CACHE_TTL_SECONDS
        ):
            _cache = _parse_suppressions_yaml(_CONFIG_PATH)
            _cache_at = now
        return dict(_cache)  # 返回副本防外部修改


def is_suppressed(
    agent_name: str,
    errors: List[Any],
    suppression_map: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
    now_utc_dt: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """若 (agent_name, 错误消息) 命中抑制清单则返回 cfg(附 pattern 字段)，否则 None。

    errors: iterable of dict-like {error_message: str} 或 sqlite3.Row。
    参数注入（suppression_map / now_utc_dt）仅用于测试，生产调用不传。
    """
    if suppression_map is None:
        suppression_map = get_suppressions()
    if now_utc_dt is None:
        now_utc_dt = datetime.now(tz=timezone.utc)

    for (supp_agent, supp_pattern), cfg in suppression_map.items():
        if supp_agent != agent_name:
            continue
        until = cfg.get("until")
        if until and now_utc_dt >= until:
            continue
        if any(supp_pattern in (e["error_message"] or "") for e in errors):
            return {**cfg, "pattern": supp_pattern}
    return None


def log_loaded_rules() -> None:
    """启动时调用：打印当前生效规则到日志，让"实际配置"可见。"""
    rules = get_suppressions(force_reload=True)
    if not rules:
        logger.info("[suppress] no rules loaded (config missing or empty)")
        return
    logger.info(f"[suppress] loaded {len(rules)} rule(s):")
    for (agent, pattern), cfg in rules.items():
        until_str = cfg["until"].isoformat() if cfg.get("until") else "no-expire"
        logger.info(f"  - ({agent!r}, {pattern!r}) until={until_str} reason={cfg.get('reason','')[:60]}")
