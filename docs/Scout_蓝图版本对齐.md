# Scout 蓝图实战校验台账 (4+3 机制)

> **文档定位升级 (2026-04-26)**: 从"版本号对齐索引"升级为**蓝图实战校验台账**。

## 4+3 组合机制

- **4** = **反向 audit** (从代码偏离出发 verify 蓝图前提)
- **3** = **增量 audit** (每次实施新 TD 时 audit 那一段)
- **合起来** = 蓝图全量持续校对，不是单条 audit

## verify 状态枚举

| 标记 | 含义 |
|---|---|
| ⏳ | 待 verify |
| 🔍 | verifying (本会话进行中) |
| ✅ | 蓝图前提成立 (可按蓝图实施) |
| ❌ | 蓝图前提不成立 (蓝图待修订 / 推迟实装 / 加前提条件) |
| 🔒 | 实装条件依赖外部 (如 KR/US 财务数据 / API key) |

## 工作流

```
每次会话开场
  → 入场流程读 Layer 3 元规则 (Scout_新会话入场.md 第 7 条)
  → 翻台账看 ⏳ 待 verify 列表
  → 按当前优先级选 1-2 条 verify
  → verify 结果填入台账
  → 决策如何处理 (动代码 / 调蓝图 / 推迟)
  → commit
```

## 元规则 (来源: docs/Scout_新会话入场.md 第 7 条)

> **蓝图内容都要对比实际情况。蓝图 = 设计意图 + 参考数值，不是最终权威**。
> 实施任何蓝图规则前，**先 verify 蓝图前提在 Scout 实战中是否成立**。
> 不成立 → 调蓝图 / 加前提条件 / 推迟实装，**不要直接照抄**。

---

## 一、已 verify (4 条 — 2026-04-25 实证产出)

| 规则 ID | 蓝图位置 | 蓝图原文 (简) | verify 状态 | 偏差类型 | 决策 | 来源 commit |
|---|---|---|---|---|---|---|
| **V155-01-scout_range** | v1.55 line 4549 | 5 档 (`early_strict` / `early_qualified` / `active` / `mature` / `out_of_range`) | ✅ 蓝图前提成立 — 待完整实装 | 代码未实装 | TD-023 Step 1 已加 zone JOIN 过滤 (`f754890`)；5 档 enum 完整实装等 Phase 2C | `f754890` |
| **V158-04/05-CN-WEIGHTS** | v1.58 line 1872-1882 | 三市场差异权重表 (CN / KR / US) | 🔒 实装条件依赖外部 | 蓝图前提部分不成立 (实装 universe 缺失) | RecommendationAgent universe 仅 A 股，KR/US 在 `stock_financials` 0 行 (能力边界事实 4)；TD-020 blocked by Phase 2A KR/US 财务采集 (OpenDART + yfinance) | `59e377c` |
| **V161-02-d4-veto** | v1.61 line 1949 | 维度 4 < 6 → 总分再高也降为候选级 | ❌ 蓝图前提不成立 | 蓝图前提偏差 | d4_base=20 当前阶段大概率反映 Scout 自身覆盖不足 (V1/V3 月频 + paper_d4 TD-003 持续 429)，不是行业产出弱；TD-014a 推迟到 Phase 4-5 (v1.69 unknowns 表上线后) | `7c36fc6` |
| **V161-FREEZE-WEIGHTS-AT-80** | v1.55-v1.61 默认 | `WEIGHTS sum=80` + 归一化 `×100/80` | ✅ 蓝图前提成立 | 无偏差 (作者明确知道 + 配合归一化使用) | 任何反推权重 (如 TD-020 v1.0 sum=100) **必须同步改归一化分母**，否则 score 无声缩放；详见能力边界事实 1 | `76fd672` |

---

## 二、47 项蓝图决策 (来自 docs/Scout_完整蓝图盘点_2026-04-25.md)

> 已在 §一 verify 的 4 条 (V155-01 / V158-04 / V158-05 / V161-02) 此处不重复列出。

### 2.1 决策清单速查 (D001-D010, 10 条)

| 规则 ID | 蓝图位置 | 决策内容 (简) | verify 状态 | 备注 / 已登 TD |
|---|---|---|---|---|
| **D001** | line 6397, 6597 | watchlist 用 `industry_id INT PK`，跨表 FK 用 industry_id | ⏳ 待 verify | 已登 TD-013 (industry_dict 数据孤岛)；实施时 verify |
| **D002** | line 6414, 6598 | track_list 主键 stock TEXT + global_company_id 跨市场关联 + global_companies 表 | ⏳ 待 verify | 已登 TD-019；实施时 verify |
| **D003** | line 6436, 6599 | 所有时间字段强制 UTC ISO 8601 + KST 显示 | ⏳ 待 verify | 已实施 (`contracts/contracts.py:26-42`)；可标 ✅ |
| **D004** | line 6446, 6600 | Pydantic v2 + extra='forbid' + 版本化 | ⏳ 待 verify | 已实施 (3 处)；可标 ✅ |
| **D005** | line 6473, 6601 | 错误传播矩阵 6 类 + agent_errors 表 | ⏳ 待 verify | 已实施 (`agents/base.py:27-119`)；可标 ✅ |
| **D006** | line 6500, 6602 | 并发 BEGIN IMMEDIATE + 表-Agent 独占 + 快照时间戳 | ⏳ 待 verify | 部分 (BEGIN IMMEDIATE ✅；表-Agent 独占无代码层强制) |
| **D007** | line 6513, 6603 | 幂等性: info_units id=hash + watchlist UNIQUE + recommendations hash | ⏳ 待 verify | 已登 TD-017 (recommendations 去重失效) |
| **D008** | line 6528, 6604 | recommendations.mode: cold_start / running / diagnosis / 架构重审 | ⏳ 待 verify | 已登 TD-022 |
| **D009** | line 6544, 6605 | Prompt 版本化 + llm_invocations 表记录 | ⏳ 待 verify | 已实施；可标 ✅ |
| **D010** | line 6571, 6606 | 测试策略 (单元 60%+ + 集成 + 回归 + 契约) | ⏳ 待 verify | 已实施；可标 ✅ |

### 2.2 v1.55 决策 (V155-02 ~ V155-08, 7 条)

| 规则 ID | 蓝图位置 | 决策内容 (简) | verify 状态 | 备注 / 已登 TD |
|---|---|---|---|---|
| **V155-02** | line 4557-4564 | early_qualified 6 条严格准入 (fillability ≥4 + 动机 [1-4] / uncertainty ≤medium 等) | ⏳ 待 verify | 关联 TD-023 |
| **V155-03** | line 4566-4569 | early_qualified 月度 review + 24 月超期推送 | ⏳ 待 verify | 关联 TD-023 |
| **V155-04** | line 4595-4617 | why_different_now 强制机制 (复活方向 historical_cycles≠[] 时必填) | ⏳ 待 verify | — |
| **V155-05** | line 5675-5676 | gap_fillability INTEGER 1-5; <3 行业不进活跃区 | ⏳ 待 verify | 已登 TD-015 (4/5 未差异化) |
| **V155-06** | line 4619-4636 | supply_chain_readiness 1-5 + readiness_evidence/_bottleneck/_updated_at + 180 天过期推送 | ⏳ 待 verify | 已登 TD-013 |
| **V155-07** | line 4640-4641 | 瓶颈突破信号扫描 (知识维护 Agent 每周扫) | ⏳ 待 verify | — |
| **V155-08** | line 1817-1830 | 混合制评分 fillability 乘数 (5=1.0/4=0.9/3=0.7) + sub_industries 加权平均 | ⏳ 待 verify | 关联 TD-015 |

### 2.3 v1.58 决策 (V158-01 ~ V158-03, 3 条)

| 规则 ID | 蓝图位置 | 决策内容 (简) | verify 状态 | 备注 / 已登 TD |
|---|---|---|---|---|
| **V158-01** | line 5786 | industry_dict.sub_industries TEXT JSON `[{name,fillability,weight_in_parent}]` | ⏳ 待 verify | 已登 TD-025 (weight_in_parent 缺失) |
| **V158-02** | line 1824-1827 | 评分时 fillability = Σ(sub.fillability × weight_in_parent) / Σ(weight) | ⏳ 待 verify | 关联 TD-025 |
| **V158-03** | line 5811 | related_stocks.sub_industry TEXT JSON 数组 | ⏳ 待 verify | 字段就位但 90 行未填 |

### 2.4 v1.60 决策 (V160-01 ~ V160-06, 6 条)

| 规则 ID | 蓝图位置 | 决策内容 (简) | verify 状态 | 备注 / 已登 TD |
|---|---|---|---|---|
| **V160-01** | line 5638, 5373-5390 | info_units.mixed_subtype 必填 conflict/structural/stage_difference | ⏳ 待 verify | 已实施 (Pydantic 校验) |
| **V160-02** | line 5391-5405 | LLM 区分 conflict/structural/stage_difference | ⏳ 待 verify | 已登 TD-021 |
| **V160-03** | line 5404-5405, 5679 | structural → 写入 watchlist.sub_market_signals | ⏳ 待 verify | 已登 TD-021 |
| **V160-04** | line 3179-3182 | 信源矛盾分级处理 (conflict 暂停 / structural 披露 / stage_difference 不阻断) | ⏳ 待 verify | 已登 TD-021 |
| **V160-05** | line 5407-5414 | conflict 行业状态锁 + 暂停新推荐 + 推送 🟡 | ⏳ 待 verify | 已登 TD-021 |
| **V160-06** | line 5416-5422 | structural 类型分子领域计算 (Phase 3+) | ⏳ 待 verify | 蓝图明示 Phase 3+ |

### 2.5 v1.61 决策 (V161-01, V161-03 ~ V161-10, 9 条)

| 规则 ID | 蓝图位置 | 决策内容 (简) | verify 状态 | 备注 / 已登 TD |
|---|---|---|---|---|
| **V161-01** | line 1917-1953 | 评分计算 7 步严格顺序 (NULL 重算 + uncertainty/fillability/24m 时效三个修正项) | ⏳ 待 verify | 部分实施；3 修正项未实施 |
| **V161-03** | line 1975-2053 | 推荐→复盘数据闭环 4 张表 | ⏳ 待 verify | schema 齐但全 0 行 |
| **V161-04** | line 3127-3190 | 自动执行 vs 推送决策矩阵 (30+ 类事件) | ⏳ 待 verify | 框架在，事件触发未对齐 |
| **V161-05** | line 3192-3242 | 风控统一优先级 P0-P4 + 冲突处理规则 | ⏳ 待 verify | 推送级别对齐，状态机未实装 |
| **V161-06** | line 3621-3640 | 复验后 uncertainty 处理 (Gemma+Sonnet 一致取高 / 不一致升 high) | ⏳ 待 verify | Sonnet 复验链路未通 |
| **V161-07** | line 3658-3675 | 综合时效 (>24m 字段标 medium 置信度 + 盲点提示) | ⏳ 待 verify | 24 月扣分修正未实施 |
| **V161-08** | line 3725-3760 | 跨市场细节规则 (market 字段细化、行业 stage 跨市场判定) | ⏳ 待 verify | 蓝图叙述较散 |
| **V161-09** | line 6638-6697 | Phase 2 重新拆分 2A/2B/2C，三阶段独立验收 | ⏳ 待 verify | 已实施；可标 ✅ |
| **V161-10** | line 6701-6736 | 大师模块只读 + 独立 master_opinions 表 | ⏳ 待 verify | 命名不一致 (master_analysis vs master_opinions, C6 备忘) |

### 2.6 emoji 标记 (EM-01 ~ EM-08, 8 条)

| 规则 ID | 蓝图位置 | 决策内容 (简) | verify 状态 | 备注 / 已登 TD |
|---|---|---|---|---|
| **EM-01** | line 2613 | 估值分位数 >95% 🔴 / >80% 🟡 推送 | ⏳ 待 verify | 关联 TD-014b |
| **EM-02** | line 2670-2671 | budget 80% 🟡 / 200% 自动暂停 Sonnet | ⏳ 待 verify | Phase 1 不阻塞 |
| **EM-03** | line 2784 | freshness_alert_days=90, A 级字段超 N 天 🔴 | ⏳ 待 verify | 关联 V161-07 |
| **EM-04** | line 3398-3451 | 动机降级预警 6 条触发 + P0-P4 推送 | ⏳ 待 verify | motivation_drift_agent 部分实施 |
| **EM-05** | line 3560-3568 | 自认失败 L4 持续 6 月推送 🔴 自检报告 + 4 选项 | ⏳ 待 verify | system_meta 缺 failure_level 字段 |
| **EM-06** | line 1041-1042 | API 成本 100/150/200% 三级告警 + 自动停 Sonnet | ⏳ 待 verify | Phase 3 任务 |
| **EM-07** | line 2258 | "禁止/取缔/不得上市" → policy_risk='fatal' 自动移出活跃区 + 🔴 | ⏳ 待 verify | 已登 TD-024 |
| **EM-08** | line 4645 | readiness 180 天未更新推送 🟡 | ⏳ 待 verify | 关联 V155-06 |

---

## 三、无效代码盘点 B+C 类初始燃料 (来自 docs/Scout_无效代码盘点_2026-04-25.md, 29 条)

> 全部标 ⏳ 待 verify。备注列含"subagent 盘点 B/C 类初始燃料"。

### 3.1 类别 B — 重复实现 (9 条)

| 编号 | 重复内容 | 主要位置 | verify 状态 | 备注 |
|---|---|---|---|---|
| **B1** | Ollama helper 三件套 | `agents/signal_collector.py:228-260` vs `utils/llm_client.py:271-298` | ⏳ 待 verify | subagent 盘点 B 类初始燃料；CLAUDE.md TD list 已记录 |
| **B2** | content preview 抽取逻辑 | `infra/dashboard.py::_extract_content_preview` vs `infra/mcp_server.py::_content_preview` | ⏳ 待 verify | subagent 盘点 B 类初始燃料；作者注释明示重复 |
| **B3** | restrictive 关键词清单三套 | `signal_collector.py::RESTRICTIVE_HARD` + `recommendation_agent.py:520 restrict_kw` + `bias_checker.py:63-65` | ⏳ 待 verify | subagent 盘点 B 类初始燃料 |
| **B4** | funded 关键词清单两套 | `motivation_drift_agent.py:69` vs `recommendation_agent.py:515-516` | ⏳ 待 verify | subagent 盘点 B 类初始燃料 |
| **B5** | "GateAgent +5" 评分逻辑两套 | `agents/gate_agent.py::generate_report` vs `recommendation_agent.py:_phase3_verify` | ⏳ 待 verify | subagent 盘点 B 类初始燃料；与 A1 合并 |
| **B6** | `info_unit_id` wrapper | `utils/hash_utils.py` vs `infra/collector.py::Collector.make_info_unit_id` | ⏳ 待 verify | subagent 盘点 B 类初始燃料；建议保留 (架构选择) |
| **B7** | Pydantic InfoUnitV1 vs DB schema 字段差异 | `contracts/contracts.py` 9 字段 vs init_db.py info_units 23 列 | ⏳ 待 verify | subagent 盘点 B 类初始燃料；与 A 类合并 |
| **B8** | bias_checker vs signal_collector 关键词列表 | (同 B3) | ⏳ 待 verify | subagent 盘点 B 类初始燃料；与 B3 合并 |
| **B9** | `agents/__init__.py` 存在但其它 4 包目录缺 | infra/, utils/, contracts/, knowledge/ | ⏳ 待 verify | subagent 盘点 B 类初始燃料；CLAUDE.md TD list 已登 |

### 3.2 类别 C — 幽灵设计 (20 条)

| 编号 | 蓝图位置 | 设计内容 (简) | verify 状态 | 备注 |
|---|---|---|---|---|
| **C1** | line 6093-6103 | industry_chain 表 5 类关系 + 关系失效 + Agent 沿关系图扩散 | ⏳ 待 verify | subagent 盘点 C 类初始燃料 |
| **C2** | line 4734-4778 | 论文周报推送规则 + 不推送过滤 | ⏳ 待 verify | subagent 盘点 C 类初始燃料 |
| **C3** | line 5424-5474 | research 模式 + credibility 过滤 (默认仅权威+可靠) | ⏳ 待 verify | subagent 盘点 C 类初始燃料 |
| **C4** | line 2621-2647 | Scout 自知力检查 7 层盲区 + 周报自知力摘要 | ⏳ 待 verify | subagent 盘点 C 类初始燃料；高优先级 |
| **C5** | line 2586-2615 | config.yaml 范围校验 + 估值水位提醒 (PE/PS 历史分位数) | ⏳ 待 verify | subagent 盘点 C 类初始燃料 |
| **C6** | line 2627-2641 | 信息维度盲区 (政策/科研/宏观 ✅ / 管理层/竞品 ❌) | ⏳ 待 verify | subagent 盘点 C 类初始燃料；蓝图自标 Phase 3+ |
| **C7** | line 5335-5360 | 信号交叉验证时间窗口 (3d/7d/30d) + is_secondary_source/independent_confirmation | ⏳ 待 verify | subagent 盘点 C 类初始燃料 |
| **C8** | line 1338-1457 | Demo 模式 (`scripts/demo_mode.py`) | ⏳ 待 verify | subagent 盘点 C 类初始燃料；Scout 不引入 (元规则) |
| **C9** | line 1136-1335 | 冷启动 LLM 预填模式 (`scripts/cold_start_llm_prefill.py`) | ⏳ 待 verify | subagent 盘点 C 类初始燃料；Scout 不引入 (元规则) |
| **C10** | line 421 | `get_review_analysis_data` MCP tool (复盘归因接口) | ⏳ 待 verify | subagent 盘点 C 类初始燃料 |
| **C11** | line 437-448 | `get_industry_full_context` 应返 7 字段 (实际 3 字段) | ⏳ 待 verify | subagent 盘点 C 类初始燃料 |
| **C12** | line 451-459 | `get_decision_context` 应返 5 字段 (实际 1 字段) | ⏳ 待 verify | subagent 盘点 C 类初始燃料 |
| **C13** | line 5496-5535 | rules 表三类记忆 (情景/语义/程序) + JSON schema | ⏳ 待 verify | subagent 盘点 C 类初始燃料；当前 schema 与蓝图设计不一致 |
| **C14** | line 1041-1042, 2580-2584 | API 成本告警 100%/150%/200% + ₩4500/₩15000 | ⏳ 待 verify | subagent 盘点 C 类初始燃料；**重叠 §二 EM-06**, 本条补充更具体阈值 |
| **C15** | line 2654-2700, 2592-2599 | config.yaml `models:` 显式映射 + `config_validation:` min/max | ⏳ 待 verify | subagent 盘点 C 类初始燃料 |
| **C16** | line 3437-3461 | 动机降级预警 6 条触发完整 (动机↓/政策反转/财务恶化/信号冲突/时间衰减) | ⏳ 待 verify | subagent 盘点 C 类初始燃料；**重叠 §二 EM-04**, 本条提供 6 条具体触发条件 |
| **C17** | line 3553-3582 | Scout 自认失败 L0-L4 状态机 + system_meta 字段 | ⏳ 待 verify | subagent 盘点 C 类初始燃料；**重叠 §二 EM-05**, 本条补充 L 状态机细节 |
| **C18** | line 4645-4651 | readiness 180 天过期推送 + 用户 "重新评估" 回复 | ⏳ 待 verify | subagent 盘点 C 类初始燃料；**重叠 §二 EM-08** |
| **C19** | line 5460-5474 | 知识库卫生制度 6 类垃圾处理 (过时/低价值/未验证/错误关联/孤儿/重复) | ⏳ 待 verify | subagent 盘点 C 类初始燃料 |
| **C20** | line 4011-4085 | 数据安全 (加密备份 / 多节点 / 外部依赖容错) | ⏳ 待 verify | subagent 盘点 C 类初始燃料；蓝图自身 Phase 3+ |

---

## 四、汇总

### 总条目数

| 章节 | 条目数 |
|---|---|
| §一 已 verify | 4 |
| §二 47 项蓝图决策 (减去 §一 已 verify 的 4 条) | 43 |
| §三 无效代码 B+C | 29 |
| **合计** | **76** |

预期 80 ±5，实际 **76**，在容差内。

### 重叠处理

47 项盘点 vs 79 条盘点 (B+C) 之间检测到 **4 条重叠**（C14↔EM-06 / C16↔EM-04 / C17↔EM-05 / C18↔EM-08）：

- **处理方式**: 都保留两条记录，C 类条目"备注"列标"重叠 §二 EM-XX"+ 说明本条补充什么独立信息（C14 补阈值 / C16 补 6 条具体触发 / C17 补 L 状态机 / C18 直接重叠）
- **理由**: 47 项盘点偏蓝图章节视角，B+C 偏代码偏离视角，**两个视角都有独立审计价值**；去重会损失视角差异
- **去重时机**: verify 该条时合并为单一台账行（决策记录到 §一）

### verify 状态分布

- ✅ 蓝图前提成立: 2 (V155-01, V161-FREEZE-WEIGHTS-AT-80)
- ❌ 蓝图前提不成立: 1 (V161-02)
- 🔒 实装条件依赖外部: 1 (V158-04/05)
- ⏳ 待 verify: 72

### 下次会话建议优先级

按"高 ROI + 高置信度"排序，建议先 verify:

1. **D003 / D004 / D005 / D009 / D010** — 5 条决策清单速查项已实施，应该可以快速从 ⏳ → ✅
2. **C1 industry_chain** — 知识层差异化优势核心，蓝图明文清晰，verify 后能直接登 P0 TD
3. **C4 Scout 自知力 7 层** — 与 docs/Scout_设计意图与实现偏离.md 第八节"市场缺口识别"同主题，verify 后可与第八节合并
4. **B3 + B4 关键词清单** — 重复实现确凿，verify 即"已 verify 偏差"，可直接登 TD-NEW
5. **EM-04 + C16 动机降级** — motivation_drift_agent 已存在，verify 后可补全 6 条触发

---

## 五、历史索引 (原 Scout_蓝图版本对齐.md 内容保留)

### 5.1 两套独立的版本号

| 版本体系 | 当前值 | 含义 | 所在文件 |
|---|---|---|---|
| **蓝图设计版本** | **v1.69** | 设计迭代次数 (系统蓝图.md 内部编号) | `C:\Users\13700F\Desktop\Scout\系统蓝图.md` |
| **运行态 scout_version** | **v1.15** | 实装代码快照 | [CLAUDE.md](../CLAUDE.md) / git tags |

**务必不要混用**：蓝图说 "v1.60 新增 mixed_subtype" 指**蓝图第 60 次修订时加入这个设计**；运行态 "v1.15 suppress" 指**代码在 v1.15 这个发版点上线 SUPPRESSED_ERRORS**。

### 5.2 蓝图 v1.60 → v1.69 主要变化 (节选)

| 版本 | 核心改动 |
|---|---|
| v1.60 | mixed_subtype 三类 (conflict / structural / stage_difference)、信号矛盾分类、sub_market_signals |
| v1.61 | Phase 2 拆 2A/2B/2C；决策回路整合；风控优先级统一 |
| v1.62 | Scout vs LLM 诚实定位 + 协作架构 (7 种场景分工) |
| v1.63~66 | 细节迭代，详见 changelog.md |
| v1.67 | Scout 责任边界；Scout vs 商业投资工具对照 |
| v1.68 | 细节迭代 |
| v1.69 | 独特 10% 深化路线；外部工具链接模板 (Koyfin / Seeking Alpha / TradingView) |

### 5.3 v1.15 运行态验证

蓝图末尾追加"运行态验证记录 (v1.15 · 2026-04-24)"小节，包含 5 天运行结果 + 新增决策 (D-018/-019/-020) + 已知缺陷 (TD-002~TD-005) + Phase 2B 进度。

**注意**: 蓝图文件在 `C:\Users\13700F\Desktop\Scout\` (不在 git 仓库内)，对蓝图的修改不会推到 GitHub。本对齐文档是**镜像索引**。

### 5.4 如何同步

**蓝图改了 → 运行态怎么跟进**:

1. 蓝图新增设计项 (如 v1.70 提出某新 Agent)
2. 在 [Scout_开发顺序规划.md](Scout_开发顺序规划.md) 排进 Phase 2B/2C/3
3. 实装时在 [Scout_技术决策记录.md](Scout_技术决策记录.md) 加对应 D-条目
4. **本台账加新行 ⏳ 待 verify**
5. scout_version 递增

**运行态验证了 → 蓝图如何更新**:

1. 运行态出现新事实 (如 Watchdog 实战有效)
2. 在 [Scout_当前进度.md](Scout_当前进度.md) 记录
3. 追加"运行态验证记录 (vX.X · YYYY-MM-DD)"小节到蓝图末尾
4. **本台账对应行更新 verify 状态**
5. 蓝图版本号递增
