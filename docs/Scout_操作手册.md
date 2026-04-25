# Scout 操作手册

> **本文件**: 数据填写惯例 + 字段语义规范 (Phase-agnostic, 跨 Phase 复用)。
> Phase 1 运维操作见 [Scout操作手册_Phase1.md](Scout操作手册_Phase1.md)。

---

## gap_fillability 填写惯例 (2026-04-25 实证)

值的语义和评分映射:

```
0 = gap 不存在/已饱和         → d3=0   + Stage 1 fatal
1 = 国产可能性极低             → d3=25  + Stage 1 fatal
2 = 攻关初期/技术鸿沟大        → d3=50  (刚好通过)
3 = 中试推进                  → d3=75
4 = 加速期/部分量产            → d3=100
5 = 大规模量产+主导市场        → d3=100 (与 4 等价, TD-015 修复后会有差异)
NULL = 不确定                 → d3=50  (默认通过)
```

填写决策:

- **不确定** → `NULL`（默认 50，保守通过）
- **明确无机会** → `0` 或 `1`（Stage 1 直接淘汰）
- **`1` 是危险值**：宁愿 `NULL` 也别填 `1`（除非真的想淘汰这个行业）
- **`4` vs `5` 在 scoring 等价**（TD-015 修复后会有差异）

来源: 2026-04-25 半导体设备字段填写实战 + Claude Code 验证 mapping
（[agents/recommendation_agent.py:641](../agents/recommendation_agent.py) + 蓝图 v1.55 line 5675）
