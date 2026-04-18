# Prompts Changelog

Scout prompt templates — history of changes. Each prompt file is immutable once released (`_v{NNN}`). Breaking changes bump the version and create a new file; agents pin to a specific version for reproducibility.

---

## signal_collector

### v001 — 2026-04-18

**Initial release**. SignalCollectorAgent prompt for Gemma 3 (`gemma4:e4b` via Ollama).

**Scope**:
- Task: 从原始文本抽取 `policy_direction` / `confidence` / `category` / `related_industries` / `summary` / `reasoning`。
- Output: strict JSON via Ollama `format='json'`.

**Rules encoded**:
- v1.59：政策方向四值 + `null`（保守优先）；多解读 / 低置信度 → `null`。
- v1.60：`mixed` 必须带 `mixed_subtype` ∈ {conflict, structural, stage_difference}；非 mixed 时 `mixed_subtype` 必须为 `null`。

**Few-shot examples** (4):
1. supportive (高置信，新能源汽车政策)
2. restrictive (工信部钢铁限产)
3. mixed/conflict (发改委光伏规)
4. null (海关进出口数据，多解读保守)

**Caller-side hard override (documented in prompt)**:
- RESTRICTIVE_HARD keywords (`禁止/取缔/不得/限制/整改/严禁`) → direction 强制 restrictive。
- MULTI_INTERPRETATION keywords (`进口/出口/价格/产能/库存`) + 无明确表态 → null。
- `confidence < 0.7` → null。

**Known limits**:
- Phase 1：mixed_subtype 默认填 `conflict`；structural / stage_difference 待 Phase 2A 细化示例。
- 产业列表 19 项封闭，新产业需显式加入 prompt 才能输出。

---

## direction_judge_weekly

### v001 — 2026-04-18

**Initial release**. DirectionJudgeAgent weekly-industry 周报 prompt。

**Scope**: 输入 dashboard JSON → 输出中文 3 段周报（≤ 300 字）：本周重点 + 趋势 + 风险。

**Features**:
- 非 JSON 输出（自由文本），非判断性（只总结 + 指出可关注方向）。
- 引用 JSON 里的具体数字（`recent_signals_total`、`signal_density_per_week`）。
- 明确不做股价 / 投资建议。
- 若 `data_freshness.newest_signal_days_ago > 30` 提示数据滞后。

---

## direction_judge_paper

### v001 — 2026-04-18

**Initial release**. DirectionJudgeAgent weekly-paper 周报 prompt。

**Scope**: 输入 Top-N 论文 JSON → 输出中文 ≤ 200 字周总结。

**Features**:
- 主题概览 + 重点论文 + 跨主题关联。
- 不杜撰引用数 / 标题；英文标题可保留。
