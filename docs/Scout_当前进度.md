# Scout 当前进度

**最后更新**：2026-04-24 (KST)
**当前版本**：v1.15-suppress（运行验证期 / Phase 2B 进行中）

---

## 1. v1.15-suppress 运行 5 天结果（2026-04-19 → 2026-04-24）

用户离家 5 天（04-19 晚 → 04-24 晚），Scout 全程无人工干预运行。核心验收：**Scout/Ollama/Gateway 三件套存活率、推送吞吐、推荐管线稳定性**。

### 1.1 系统存活

- **Scout serve**：全程在线，今天 19:04 KST 由我（Claude）手动重启后 Watchdog 5 分钟内救活
- **Ollama gemma4:e4b**：全程在线，今天 19:09 KST 由我手动重启后 Watchdog 5 分钟内救活
- **OpenClaw Gateway**：全程在线
- **心跳到达**：🟢 `[Scout-Watchdog · heartbeat]` 04-20 ~ 04-24 每天 09:04 KST 准点到 QQ，5/5 天 100%

### 1.2 Watchdog 救活统计（5 天）

本期**今天触发 2 次**（均为我主动重启后 Watchdog 自愈）：

| 时间 (KST) | 目标 | 事件 | 结果 |
|---|---|---|---|
| 2026-04-24 19:04 | **Scout serve** | DOWN → restart (hist 0/3) | ✅ 下次 check 恢复 |
| 2026-04-24 19:09 | **Ollama** | DOWN → restart (hist 0/3) | ✅ 下次 check 恢复 |

04-19 晚的部署压力测试（3 次 scout 连环 + ollama + gateway）视为部署自检，不计入正式运行期。**真实运行 5 天、4 次成功救活**（含 04-19 部署末期自检 2 次）。详见 [D-018](Scout_技术决策记录.md#d-018)。

### 1.3 推荐池增长

| 指标 | 2026-04-19 基线 | 2026-04-24 现在 | 增量 |
|---|---|---|---|
| **总推荐数** | 154 | **228** | **+74（+48%）** |
| A 级（≥75） | - | 6 条（3 只去重） | - |
| B 级（60-74） | - | 24 | - |
| candidate（40-59） | - | 150 | - |
| reject（<40） | - | 48 | - |

> ⚠️ 228 条**全部基于纯规则层 6 维度评分**产出。v1.07 声称的"规则 + Sonnet LLM 两阶段"实际 Stage 2 未实装（`ANTHROPIC_API_KEY` 未设置，`llm_invocations` 零 Sonnet）。规则层准确率需 3~6 个月观察期。详见 [TD-009](Scout_技术债务清单.md#td-009)。

### 1.4 3 只 A 级股票

| 股票代码 | 得分 | 最新评级日 | 备注 |
|---|---|---|---|
| **688082** | 89.69 | 2026-04-23 | 最高分 |
| **002371** | 81.56 | 2026-04-23 | |
| **688012** | 81.56 | 2026-04-20 | |

（每只 A 级股票 04-20 和 04-23 两次独立推荐，共 6 行；去重后 3 只。）

### 1.5 推送队列吞吐

```
日期        done   failed
2026-04-19    20      0
2026-04-20    18      0
2026-04-21    14      0
2026-04-22    11      0
2026-04-23    11      0
2026-04-24     4      0  （截至 19:30 KST）
```

5 天累计完成 **78 条**，failed **0 条**。

### 1.6 info_units 新增

5 天新增 **31 条**（D1/D4/S4 = 11/18/2）：

```
2026-04-19:  2    2026-04-22:  4
2026-04-20: 12    2026-04-23:  2
2026-04-21: 10    2026-04-24:  1 (截至 19:30)
```

---

## 2. 已知技术债（本期发现）

| 编号 | 标题 | 优先级 | 备注 |
|---|---|---|---|
| **TD-002** | `SUPPRESSED_ERRORS` 告警抑制未真正生效 | 🔴 高 | 离家期间 QQ 仍收到 akshare 告警；回来第一件事修 |
| **TD-003** | `paper_d4` P0 修复不够，S2 桶耗光需 P1 API key | 🔴 高 | HTTP 429 每日仍 4~24 条；需申请 SemanticScholar Partner Key |
| **TD-004** | `collect_V3` 04-21 偶发告警 | 🟡 中 | 单次偶发；观察 2 周再决定是否修 |
| **TD-005** | `direction_backfill` 偶发 `Gemma returned non-JSON` | 🟡 中 | 04-19 1 次，empty payload；加 retry-with-repair 即可 |

详见 [Scout_技术债务清单.md](Scout_技术债务清单.md)。

---

## 3. 本期交付

| 项 | 状态 | 说明 |
|---|---|---|
| Watchdog 5 分钟一检 + QQ 心跳 + 自动重启 | ✅ 完成 | 实战 4 次救活，心跳 5/5 |
| Dashboard 纯文字版（去动画避免闪屏） | ✅ 完成 | commit `756f78a` |
| SUPPRESSED_ERRORS 抑制清单 v1.15 | ⚠️ 有 bug | TD-002 待修 |
| QQ 主动推送（QQ 官方 API 直调，token 155s 刷新 + rate limit） | ✅ 完成 | `infra/qq_channel.py`；见 [D-020](Scout_技术决策记录.md#d-020) |
| OpenClaw tool profile 切换到 `full` | ✅ 完成 | 见 [D-019](Scout_技术决策记录.md#d-019) |

---

## 4. 下一步（回来后顺序）

1. **TD-002 修 suppress 逻辑**（半天）— 回来第一件事
2. **TD-003 申请 SemanticScholar API Key**（10 分钟提交 + 1~5 天审批）
3. **Phase 2B QQ 插件剩余 5/8 工具**（见 [Scout_开发顺序规划.md](Scout_开发顺序规划.md)）
4. TD-004/TD-005 观察期，不急

---

## 5. 2026-04-25 实战发现的关键事实

半导体设备 5 字段填写实验（commit `6c6205b`）+ 蓝图 verify + audit 副产物。

### 1. d4 weighted 计算公式

公式：V1/V3/D4 90 天内信号数 → 100 满分，weight 0.10
实测：002371/688012 都是 16 条信号 → d4 score=100, weighted=10.0
用途：验证 [TD-014a](Scout_技术债务清单.md#td-014a) 蓝图 "d4 weighted < 6 → 候选级" 触发条件需要此公式（信号数 < 10 才有可能）

### 2. GateAgent verify 加分 mapping

观察：同行业信号 11 条 → verify +5 / 同行业信号 17 条 → verify +5
含义：似乎是阶梯式加分（≥10 条 +5），不是线性
用途：预测推荐分数时，phase 3 delta 通常 +5
**待 verify**：阈值具体在 10 还是其他值；是否还有 +10 / +15 上限。grep `_phase3_verify` 源码可定。

### 3. 半导体设备 industry_id = 1

今天填字段时确认。
用途：未来扩到其他行业，查 industry_id 用 `SELECT industry_id FROM watchlist WHERE industry_name='X'`

### 4. recommendations 表去重失效 → TD-017

verify：**230 条记录 / 38 stocks / 95 distinct (stock+thesis_hash) → 实际只 ~41% 独立判断**。
top 重复：002230=8 / 688082=8 / 002371=7 / 688012=7 / 688256=7。
详见 [TD-017](Scout_技术债务清单.md#td-017--recommendations-表去重失效)。

### 5. watchlist 5 个僵尸行业（0/8 字段）

半导体材料 / 新材料 / 生物制造 / 低空经济 / 独角兽曝光
状态：在 `watchlist.zone='active'` 但所有精细字段（gap_fillability/gap_analysis/sub_market_signals/motivation_levels/motivation_detail/thesis/kill_conditions/notes-subs）都 NULL。
注意：其中 4 个（半导体材料/新材料/生物制造/低空经济）在 `industry_dict` 表里**有** sub_industries 数据（见 [TD-013](Scout_技术债务清单.md#td-013--industry_dict-表与-watchlist-数据孤岛--字段功能重复)），但 watchlist 与 industry_dict 数据孤岛导致 RecommendationAgent 看不到。
处理：待决策（保留 / 移除 / 补完字段），今天不做。
来源：2026-04-25 [scripts/watchlist_field_audit.py](../scripts/watchlist_field_audit.py) 报告

### 6. 4 个行业补字段顺序决策 (2026-04-25)

继半导体设备 (commit `6c6205b`) 之后,以下 4 个行业应陆续补 watchlist 5 字段
(`gap_fillability` + `gap_analysis` + `thesis` + `kill_conditions` + `motivation_detail`)。

**顺序**:

1. **半导体材料** (id=17) — Core 4 姐妹行业,动机 + 缺口 + 三市场逻辑对齐半导体设备
   - 注: 当前 12 推荐里 10 reject (Stage 1 触发, Z-Score<1.81),
         补字段不会改变 reject 状态,但会让评分基于真实数据
2. **新材料** (id=18) — 18 全 candidate, 补完最可能升 B
3. **生物制造** (id=19) — 24 全 candidate
4. **低空经济** (id=20) — 18 全 candidate

**实施约束**: 每次补 1 个行业,实战数据查证 + dry-run + apply,**不批量做**。
**优先级**: 不阻塞任何 Phase 任务,在用户精力充沛时逐个做。

**样板**:
- [scripts/update_semi_eq_5fields.py](../scripts/update_semi_eq_5fields.py)
- [docs/Scout_操作手册.md](Scout_操作手册.md) "gap_fillability 填写惯例"
- 半导体设备 commit `6c6205b` 的写入流程 (备份 → dry-run → apply → SELECT 验证 → cmd_recommend 重评分)

**独角兽曝光 (id=23) 不在本顺序** — 性质不同 (US mega-cap watch-only, 0 推荐), 下个独立任务处理。
