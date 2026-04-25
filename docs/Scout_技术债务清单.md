# Scout 技术债务清单

> 格式：**TD-编号 / 标题 / 优先级 / 发现日期 / 现象 / 根因 / 修复方案 / 预期工作量 / 状态**。
> 优先级：🔴 高（影响用户感受或数据正确性）/ 🟡 中（运营层面）/ 🟢 低（代码卫生）。

---

> ⚠️ **优先级警示（2026-04-25）**:
> 见 [docs/Scout_设计意图与实现偏离.md](Scout_设计意图与实现偏离.md)。
> Scout 当前评分体系结构性偏差是比所有 TD 都更严重的问题。
> 修复 TD-001 ~ TD-018 不能解决 "Scout 错过非政策驱动机会" 的根本问题。

---

## TD-002 · `SUPPRESSED_ERRORS` 未真正屏蔽告警推送

- **优先级**：🔴 高
- **发现日期**：2026-04-24
- **归属模块**：`agents/health_monitor_agent.py`
- **状态**：**待修（回来后第一件事）**

### 现象

v1.15 引入 `SUPPRESSED_ERRORS` 清单用于临时屏蔽已知非关键告警（如 `akshare_s4 × RemoteDisconnected`），until=2026-04-27（后因用户 04-24 实际回家改为 2026-04-24）。

用户离家 5 天（2026-04-19~2026-04-24），**QQ 仍持续收到 akshare_s4 告警**，`agent_errors` 日计数（以及实际推送量）：

```
04-19: akshare_s4 = 12
04-20: akshare_s4 =  6
04-21: akshare_s4 =  6
04-22: akshare_s4 =  5
04-23: akshare_s4 =  6
04-24: akshare_s4 =  6
```

错误都落了库（正常），但**应该被抑制的告警推送也到了 QQ**（不正常）。

### 可能根因（待排查）

1. **时区比较问题**
   - `SUPPRESSED_ERRORS[("akshare_s4","RemoteDisconnected")]["until"] = datetime(2026,4,24,tzinfo=KST)` 代表 **KST 2026-04-24 00:00** = **UTC 2026-04-23 15:00**
   - `_is_suppressed` 里 `if until and now_utc_dt >= until: continue`（意为抑制过期就跳过）
   - 在 **KST 2026-04-24 00:00 之后所有时刻**，抑制已失效 — 技术上正确但不符合用户直觉（用户以为 04-24 当天仍抑制）
   - 但 04-19~04-23 仍在有效抑制窗口内，理应不推 — **这段仍有告警到 QQ，说明不只是时区问题**

2. **子串匹配漏过**
   - `pattern="RemoteDisconnected"` 子串匹配，但 `agent_errors` 中也有 `ConnectionError` 变体，清单两条都登记了
   - 需核查是否有第 3 种错误信息变体（如 `urllib3.exceptions.*`）未覆盖

3. **调用链未接上**
   - `_is_suppressed` 是否真的在每条推送路径上都被调用？
   - Watchdog 的 QQ 推送走 `qq_channel.py` 直调，**完全不经过 HealthMonitorAgent 的抑制判断** — 可能是 Watchdog 在推，不是 HealthMonitorAgent
   - **需要确认：离家期间 QQ 收到的 akshare 告警是 Scout HealthMonitorAgent 推的，还是 Watchdog 5 分钟 check 探测 agent_errors 新增后主动推的**

4. **push_queue 重复落队**
   - 抑制命中时 `log + queue 不落`，但若另一个 agent（MasterAgent）另起路径推送会绕过抑制

### 修复方案（建议步骤）

1. **定位推送来源**（最优先）：在 QQ 历史消息找一条 akshare 告警，看消息格式和时间戳，反推是 HealthMonitorAgent 还是 Watchdog 推的
2. 根据定位结果：
   - 若是 Watchdog：把抑制清单移到 Watchdog PowerShell 脚本（或让 Watchdog 先 query agent_errors 带抑制过滤）
   - 若是 HealthMonitorAgent：加单元测试覆盖时区 + 子串变体，修 bug
3. 增加**参数化测试矩阵**（遵循用户协作风格）：
   - 5 种错误消息变体 × 抑制命中/未命中 × 时区边界（UTC 午夜 / KST 午夜 / until 前 1 秒 / until 后 1 秒）
4. 语义调整：`until` 从"until KST 日期的 00:00" 改成"until KST 日期的 **23:59:59**"，对齐用户直觉
5. 日志里抑制命中后 `[suppress] ...` 打印路径来源，便于下次复盘

### 预期工作量

- 定位推送来源：0.5 天
- 修复 + 参数化测试：0.5 天
- 回归（模拟离家）：0.5 天
- **合计约 1.5 天**

---

## TD-003 · `paper_d4` P0 修复不够，S2 桶耗光需 P1

- **优先级**：🔴 高
- **发现日期**：2026-04-24
- **归属模块**：`agents/paper_d4_agent.py`（SemanticScholar 客户端）
- **状态**：P0 已上线但不够，P1 未启动

### 现象

Paper D4 采集科研动向依赖 SemanticScholar API。P0 修复（指数退避 + 429 重试 + 桶分流 S1→S2）上线后错误数下降但**仍每日有新 429 落库**：

```
日期       paper_d4 错误数   首要错误类型
04-18           14           (P0 前基线)
04-19           24           SemanticScholar HTTP 429
04-20           11           SemanticScholar HTTP 429
04-21           21           SemanticScholar HTTP 429
04-22            8           SemanticScholar HTTP 429
04-23            6           SemanticScholar HTTP 429
04-24            4           SemanticScholar HTTP 429
```

### 根因

- 匿名调用限额：**100 req / 5 min / IP**（S1 桶）
- S1 用尽后切 S2（备用 key 池）— 但**现有"备用 key"本身也共享同一个 IP 匿名限额**，不是独立额度
- 即便 P0 加了 jittered backoff，summary-level 查询量 > 100/5min 时仍会碰上限
- **S2 桶已耗光**，现只剩 S1 原桶在轮询

### 修复方案

**P1（本周）**：申请 SemanticScholar **Partner API Key**

- 申请入口：`https://www.semanticscholar.org/product/api#api-key-form`
- 审批 1~5 业务日
- 批准后：10000 req/5min，够用 **100x**
- **Key 放 env**（`SEMANTIC_SCHOLAR_API_KEY`），不进 config.yaml、不进 DB（遵循核心约束第 9 条）
- 代码改动：request header 加 `x-api-key: <env>`

**P2（备选，若审批被拒）**：切 Crossref + OpenAlex 作为 paper_d4 数据源（已知覆盖度低 ~20%）

### 预期工作量

- 提交申请：10 分钟 → 等待 1~5 天
- 接线（env read + header 加 `x-api-key`）：0.5 天
- 回归（观察 7 天内 429 是否归零）：观测期

---

## TD-004 · `collect_V3` 04-21 偶发告警

- **优先级**：🟡 中（单次偶发，其余日期正常）
- **发现日期**：2026-04-21
- **归属模块**：`infra/data_adapters/korea_customs_playwright.py`
- **状态**：待复盘，未修（观察期）

### 现象

2026-04-21 全天 `scout.log` 调度器 heartbeat 持续显示：

```
failures={'collect_V3': 2}
```

说明 collect_V3 任务在 04-21 连续 2 次失败。但 `agent_errors` 表没有对应 V3 agent 的行 — 错误发生在 **scheduler 层**或被 Playwright 吞了 trace 但未落 error matrix。

04-20 / 04-22 / 04-23 / 04-24 均正常，failures=0。

### 可能根因

1. **Playwright chromium 偶发启动失败**（Windows 权限、临时目录清理、内存压力）
2. **韩国关税厅网站 04-21 DDoS 防护临时拦截**（User-Agent 或频率）
3. **网络临时故障** — 04-19 22:19 Watchdog 曾 restart gateway，当时上游网络或许有抖动，但 04-21 没有对应事件

### 修复方案

1. **增强 trace**：collect_V3 失败时把 Playwright console log 落 `agent_errors.context_data` JSON，便于事后复盘
2. **观察 2 周**：若再次复现才投入修复；单次偶发不修（遵循"防止过度工程"）
3. **兜底**：失败时 fallback 到 `korea_customs.py` HTTP 版（现在是 fallback，但日志没明说是否被调用）

### 预期工作量

- 加 trace 埋点：0.5 天
- 等待观察：2 周
- 复现后排查：按实际情况估

---

## TD-005 · `direction_backfill` 偶发 `Gemma returned non-JSON`

- **优先级**：🟡 中（单次偶发）
- **发现日期**：2026-04-19
- **归属模块**：`agents/direction_backfill_agent.py`
- **状态**：未修

### 现象

`agent_errors` 表 1 条：

```
agent_name:     direction_backfill
error_type:     parse
error_message:  Gemma returned non-JSON: ''
occurred_at:    2026-04-19T19:02:02.533407+00:00
```

Gemma 本地推理返回了**空字符串**，被 direction_backfill 的 JSON 解析失败，落错误矩阵 `parse` 类。

### 可能根因

1. Ollama 服务在该时点临时压力（Watchdog 04-19 22:12 也有 ollama restart，但时间差 3 小时）
2. Gemma `gemma4:e4b` 在某些 prompt 长度下会返回空 payload（已知 bug 模式）
3. direction_backfill 的 retry 策略太激进，第 1 次失败直接抛错不重试

### 修复方案

1. **加 retry-with-repair**：
   - Gemma 返回空 / 非 JSON → 用 `"(上次响应为空，请用 JSON 格式重新回答)"` prompt 改写一次再调用
   - 最多重试 2 次，都失败才落 `parse` 错误
2. **参考 `signal_collector`**：其 `_combine` 里已有 `confidence<0.7 → null` 的防御性加严，direction_backfill 可以复用

### 预期工作量

- 加 retry-with-repair + 测试：0.5 天

---

## TD-007 · Watchdog `state.json` schema 不一致导致 scout/gateway 限流失效 ✅

- **优先级**：~~🟡 中（限流失效不会立刻出事，但极端情况下可能分钟级硬重启风暴）~~
- **发现日期**：2026-04-25（修 TD-002 重启 scout serve 时观察到）
- **归属模块**：`C:\Tools\scout-watchdog.ps1` + `C:\Tools\scout-watchdog-state.json`
- **状态**：✅ 已修复 2026-04-25 上午

### 现象（修复前）

`C:\Tools\scout-watchdog-state.json` 实际形态：

```json
{
    "restart_history": {
        "ollama":  ["2026-04-24T19:09:04"],      ← list
        "scout":   "2026-04-24T19:04:04",         ← string (单值)
        "gateway": "2026-04-19T22:19:31"          ← string (单值)
    },
    "last_heartbeat_date": "2026-04-24"
}
```

三个 target 的 `restart_history` 值 schema **不一致**：
- `ollama` → `list[str]`（可累计，限流逻辑正常）
- `scout` → `str`（单值覆盖，限流失效）
- `gateway` → `str`（单值覆盖，限流失效）

### 根因（修复后定位）

不是 PowerShell 脚本的"两条分支"，而是 PS 5.1 `ConvertTo-Json` 在某些路径对单元素数组的扁平化语义 + Save-WDState 把 `$State` 整体喂给 `ConvertTo-Json` 时未做 array coercion。

具体触发链：
1. `Prune-History` 的 `,$kept` 操作符已经能正确返回 array（哪怕 0/1 元素）。
2. Handle-Down 内 `$hist += $now` 也保持 array 类型。
3. 但 `Save-WDState` 直接 `$State | ConvertTo-Json` —— 当某 target（如 gateway）健康从未走 Handle-Down，其 `restart_history.gateway` 保持 Load 时拿到的形态。
4. 一旦上一次保存把 gateway 写成 string，下一次 Load 读回来仍是 string，naive 写回去还是 string —— 死循环。
5. 此时 `Prune-History [array]string-cast` 路径表面工作（cast 把 string 包 array），但若该服务整个窗口都健康，没人重新触发 array coercion，json 永远是 string。

`hist=0/3` 计数本身一直是对的（`$hist.Count` on `[Object[]]`），但 string-状态下 `$hist += $now` 实际 append 到的是 cast 出的临时数组，不会反映回 `$State.restart_history.<key>`，导致下次 Load 还是 string —— **长效后果**：连续重启 N 次后 history 仍只是 1 条 string，4-5 次硬重启时限流不生效。

### 修复内容

1. **`Save-WDState` 防御性 array coercion**（`C:\Tools\scout-watchdog.ps1`）：
    ```powershell
    # 双层 @() 包装：内层把 scalar/array 都规整成 [array]，外层防 PS 5.1
    # ConvertTo-Json 单元素扁平化
    $payload = [PSCustomObject]@{
        restart_history = [PSCustomObject]@{
            ollama  = @( @($h.ollama)  )
            scout   = @( @($h.scout)   )
            gateway = @( @($h.gateway) )
        }
        last_heartbeat_date = $State.last_heartbeat_date
    }
    ```
2. **state.json 一次性迁移**：补丁后第一次自动调度（10:09 KST）即把 string 形态的 `gateway` 写回为 `["2026-04-19T22:19:31"]`，无需人工手动改。
3. **`Prune-History` 不动**：原有 `return ,$kept` 已经正确（不是 bug 源）。

### 验证

```
$ cat C:\Tools\scout-watchdog-state.json
{
    "restart_history": {
        "ollama":  ["2026-04-25T10:04:04"],      ← list ✓
        "scout":   ["2026-04-25T08:14:03"],      ← list ✓
        "gateway": ["2026-04-19T22:19:31"]       ← list ✓ (已迁移)
    },
    "last_heartbeat_date": "2026-04-25"
}
```

10:10:23 手动触发一次 watchdog 自检也 `check ollama=True scout=True gateway=True` 干净通过，state.json 三字段保持 list 形态。

按 D-018 决策 watchdog 独立维护：
- C:\Tools\ 不入仓；scout-watchdog.ps1 patch 不进 git
- 改 watchdog 不需要重启 scout serve
- 本条仅 docs commit

---

## TD-008 · user_decisions v1.69 新字段暂时全 NULL（待 QQ receive_reply 上线后接线）

- **优先级**：🟡 中（不影响现有推荐管线，阻塞复盘闭环深度）
- **发现日期**：2026-04-24
- **归属模块**：`agents/recommendation_agent.py` / `agents/push_consumer_agent.py` / 未来的 QQ receive_reply 插件
- **状态**：schema 已加，writer 待接（QQ 插件 4/8 上线后）

### 背景

2026-04-24 对 `user_decisions` 表加了 v1.69 决策字段扩展（ALTER TABLE × 5）：
- `reasoning TEXT` — 用户决策时的思考文字
- `emotion TEXT` — confident/hesitant/fomo/fear/anchoring/contrarian
- `confidence INTEGER` — 1-10 自评信心
- `time_spent_seconds INTEGER` — 决策花费秒数
- `pre_mortem TEXT` — JSON: 3 个失败场景

migration: `scripts/migrations/2026-04-24_user_decisions_v169_fields.py`
backup: `data/backups/knowledge.db.pre_v169_fields_20260424_*`

### 现状

- 5 字段在 schema 上可用，全部 NULL-able
- **没有 writer 代码会填这 5 字段**，新 row 进来时全部 NULL
- 原有字段 (recommend_id / stock / decision / decision_reason / decided_at) 也还没 INSERT 源 — user_decisions 表目前 0 行，整个"推荐→用户决策→复盘"闭环还没跑通

### 修复方案（按 QQ 插件施工顺序）

**阶段 1**（Phase 2B QQ 插件工具 4/8 = `qqbot-channel.receive_reply`）：
- 用户在 QQ 收到推荐后回复：`track 688082 confident 8 "理由..."` 格式（或自由格式 + LLM 解析）
- PushConsumerAgent 增加 `on_user_reply` handler，解析 reply → INSERT user_decisions（含 5 新字段）
- `time_spent_seconds` = 推荐推送时间到用户回复时间的差（push_queue.delivered_at − 用户 reply 时间）

**阶段 2**（Phase 3 complement）：
- `pre_mortem` JSON 格式由 master_analysis agent 在推荐当时生成 3 个失败场景，随推荐一起推给用户
- 用户回复时可选"confirm/adjust/add"pre_mortem 条目

### 预期工作量

- QQ `receive_reply` 工具本身：0.5~1 天（Phase 2B 范围内）
- INSERT writer + parser：0.5 天
- pre_mortem 生成（Phase 3）：单算

### 验证

- QQ 回复触发 INSERT user_decisions，5 新字段非 NULL
- 复盘 agent（`review_agent`，Phase 3）能跨 user_decisions + recommendations 做 emotion/confidence/pre_mortem 三维归因

---

## TD-009 · RecommendationAgent Stage 2 Sonnet 层未实装

- **优先级**：🟡 中
- **发现日期**：2026-04-24
- **归属模块**：`agents/recommendation_agent.py` + `config.yaml` + `recommendations` schema
- **状态**：延迟修（触发条件满足前不动）

### 现象

v1.07 声称"规则 + Sonnet LLM 两阶段"，实际只有规则层在运行：

- `llm_invocations` 表 **63 条全是 Gemma**，零 Sonnet
- `ANTHROPIC_API_KEY` 未设置
- `recommendations` 表缺 `llm_reasoning` / `rationale` 字段（规则层无从写入 LLM 理由）

### 影响

- 228 条推荐**全部基于纯规则层 6 维度评分**，"规则 + LLM"的宣称名不副实
- 规则层和 LLM 层对 A 级判断的差异**无法观测**（没数据）
- v1.65 成本监控**是瞎子**：没云端调用 = 永远 0 成本，看不到真实开销曲线

### 为什么不立即修

- **规则层当前准确率未知**，需 3~6 个月观察期才能判断"规则层够不够用"
- 盲目加 Sonnet stage 只会**增加成本**（每条推荐 API 调用）但**不保证质量提升**
- v1.07 原设计里"规则层 + Sonnet 判断一致时跳过 Sonnet"的逻辑可能**反而是对的**（节省成本）— 只是目前判断一致性无数据支撑

### 修复触发条件（任一满足再做）

1. 观察 3 个月，规则层 A 级**命中率 < 60%** → 需要 LLM 二审纠偏
2. 规则层边界 case（65~74 分 B 级）需要**人工介入太多** → 需要 LLM 自动判断替代
3. 决策 Agent 对比验证机制上线（蓝图 v1.12 防循环论证）→ 需要 Sonnet 独立判断

### 修复时必须同步做

1. `recommendations` 加 `llm_reasoning TEXT` 字段（新 ALTER TABLE，走 `scripts/migrations/`）
2. `config.yaml` 启用 `api_key_required: true`
3. `.env` 加 `ANTHROPIC_API_KEY`（**不进 config.yaml/DB**，遵循核心约束第 9 条）
4. `llm_invocations` 的 writer 覆盖 Sonnet path（目前只在 Gemma path 写）
5. v1.65 成本监控先把 Sonnet 调用真的记进来再谈（此前所有"成本曲线"都是空集）

### 预期工作量（真到修的那天）

- schema migration + init_db 同步：0.5 天
- recommendation_agent 接 Anthropic SDK + Stage 2 逻辑：1 天
- llm_invocations writer 覆盖 Sonnet + 成本字段：0.5 天
- 回归 + 对照测试（规则层 vs LLM 层一致性）：1 天
- **合计约 3 天**（触发条件满足后再启动）

---

## TD-011 · 判断性结论的归档惯例尚未自动化

- **优先级**：🟡 中
- **类型**：流程性 TD
- **发现日期**：2026-04-25
- **状态**：临时方案已实施（手动追加到 `docs/Scout_方法论自省.md`），自动化待做

### 现状

Scout 三角色模型（Claude Code 执行 + 外部 LLM 辅助大脑 + 用户决策）产生的判断性结论没有自动归档机制。代码改动有 git history，但"X 是 Scout 盲点"这种判断性结论没有载体，半年后会丢失。

### 临时方案（已实施）

建立 `docs/Scout_方法论自省.md`，重要判断性结论手动追加。每条结论格式：日期 / 结论 / 理由 / 适用范围 / 反决定触发条件。

### 长期方案（待做）

让 Claude Code 在每次产生重要判断性结论时自动追加（可能要在 `CLAUDE.md` 加触发规则，或建立 hook 监听特定关键词）。

### 不立即修理由

流程性 TD，不阻塞任何技术开发，但长期重要性高（没归档 = 半年后重新讨论同样的问题）。

### 详见

`docs/Scout_方法论自省.md` 第五节"归档惯例"

---

## TD-012 · 反指标硬过滤层缺失

- **优先级**：🔴 高
- **类型**：方法论 TD
- **发现日期**：2026-04-25
- **归属模块**：`agents/recommendation_agent.py`（Stage 1/Stage 2 之间）
- **状态**：未启动，Phase 2B 优先项

### 现状

Stage 1 硬底线只有 4 条（`policy_fatal` / `Z<1.81` / `gap_closed` / `risk_flag`），没有公司级负面过滤。一只股票如果 6 维度 A 级，即使大股东刚减持 10% 仍然推荐。

### 修复方案

Stage 1 后 Stage 2 评分前，加一个反指标硬过滤层：

- 过去 90 天大股东减持 > 5% → 降级到 candidate
- 商誉 / 净资产 > 30% → 降级到 candidate
- 应收账款增速 - 营收增速 > 20pp 持续 2 季度 → 降级
- 解禁未来 180 天 > 15% 流通股 → 降级

### 修复时机

Phase 2B（5 月）

### 预期工作量

1-2 天（纯规则，不需要 LLM；akshare 数据已可用）

### 详见

`docs/Scout_方法论自省.md` 第二节"改进 1: 反指标硬过滤层"

---

## TD-013 · industry_dict 表与 watchlist 数据孤岛 + 字段功能重复

- **优先级**：🔴 高
- **类型**：架构 TD
- **发现日期**：2026-04-25（半导体设备字段填写时 cross-check 发现）
- **修复时机**：Phase 2A（2026-05）
- **状态**：未启动；今天完成发现 + 决策预案，未动 schema

### 现状

`sub_industries` 字段已存在，但**不在 watchlist 表，而在 `industry_dict` 表**（v1.58 加，[knowledge/init_db.py:119](knowledge/init_db.py:119)，含结构化 JSON `[{name, fillability}, ...]`）。这导致：

1. **覆盖错位**：`industry_dict` 只 7 行，watchlist active 19 行，**只 5 行重叠**（其中 1 行 sub_industries 是空数组）；半导体设备 + 14 个其他主力行业**根本不在 industry_dict**
2. **`industry_dict.in_watchlist` 全是 0**，但其中 5 行实际在 watchlist active —— 字段从未被维护
3. **`industry_dict.supply_chain_readiness` 全 NULL**（与 `watchlist.gap_fillability` 功能重复，也都全 NULL）—— 两套相似设计互相不知道
4. **`agents/` 目录零代码读 `industry_dict`** —— 这张表对运行态零价值

### 根因

v1.58 蓝图把 `sub_industries` 放 `industry_dict` 是**架构洁癖**：理论上"行业字典"承载元信息，watchlist 只承载"被关注哪些"是干净的 normalized schema。但实际写代码的人发现 watchlist 已经有 industry_name + 大量元字段（motivation/gap/thesis），再去 industry_dict JOIN 一次是无谓 overhead，所以代码漂向 "watchlist 一站式"。

`v1.02 industry refresh` 当时只补 7 个新行业到 industry_dict，老 15 个根本没回填 —— 进一步证明 `industry_dict` 不是被当作"全行业字典"在用，而是被当作 **"新增行业 staging"** 用了。蓝图 v1.58 的 normalized 设计意图已被代码实现否决，应该承认这点让 schema 顺应事实。

### 修复方案选项评估

| 选项 | 描述 | 评估 |
|---|---|---|
| A | 补 15 行业进 industry_dict + 改 RecommendationAgent 读两表 | ❌ 为已死设计输血；6 个月后还会再失修 |
| B | watchlist 加 sub_industries 列 + 废弃 industry_dict.sub_industries | ✅ **推荐** |
| C | 建 vw_industry_full view JOIN 两表 | ❌ 回避问题；当下问题是数据没填，不是查询路径乱 |
| D | 暂停 + 等 Phase 2A 决定 | ❌ procrastination；信息已齐 |

### 推荐方案 B 实施步骤

1. **schema migration**（`scripts/migrations/2026-05-XX_watchlist_extend.py`）：
   ```sql
   ALTER TABLE watchlist ADD COLUMN sub_industries TEXT;     -- v1.58 JSON
   ALTER TABLE watchlist ADD COLUMN cyclical INTEGER;        -- 从 industry_dict 迁
   ALTER TABLE watchlist ADD COLUMN global_leaders TEXT;     -- 从 industry_dict 迁
   ALTER TABLE watchlist ADD COLUMN historical_cycles TEXT;  -- 从 industry_dict 迁
   ALTER TABLE watchlist ADD COLUMN why_different_now TEXT;  -- 从 industry_dict 迁
   ```

2. **数据迁移**：
   - `industry_dict.sub_industries`（4 行有数据）→ `watchlist.sub_industries`
   - `watchlist.notes` 的 `[subs=...]` 段 → 解析成 JSON 写到 `watchlist.sub_industries`，notes 段移除
   - `industry_dict.cyclical/scout_range` → `watchlist`（注意 scout_range vs zone 概念冲突需先决定）

3. **废弃 `industry_dict.sub_industries` / `supply_chain_readiness` / `readiness_evidence` / `readiness_bottleneck` / `in_watchlist`** —— 5 个未维护或重复字段
4. **保留 industry_dict** 作为 v1.02 cold start staging 历史记录，不再写入新行；或彻底 DROP（待决定）
5. **关掉 CLAUDE.md §5 那条 TD**（"watchlist.notes composite 字符串 → 拆 sub_industries 列"）

### 不立即修理由

1. 不阻塞当前推荐管线（agents 不读 industry_dict，所以现在没"功能 bug"）
2. 半导体设备等 5 个不依赖 sub_industries 的字段今天先填了（gap_fillability/gap_analysis/thesis/kill_conditions/motivation_detail），端到端验证显示 d3 从 50→100，002371/688012 总分 81.56→90.94 **证明补 watchlist 字段确有收益**
3. ALTER TABLE + migration + 数据迁移需要 1-2 天，应排进 Phase 2A 完整窗口

### 预期工作量

- schema migration + init_db 同步: 0.5 天
- 数据迁移脚本（含 notes 解析）: 0.5 天
- 测试 + 回归: 0.5 天
- **合计 1-2 天**

### 详见

- [docs/Scout_技术债务清单.md](docs/Scout_技术债务清单.md) 本条 + CLAUDE.md §5 watchlist.notes TD
- [scripts/update_semi_eq_5fields.py](scripts/update_semi_eq_5fields.py) 验证实验
- 验证结果：002371/688012 81.56 → 90.94 (+9.38)，d3 50→100，A 级地位巩固

---

## TD-014a · 蓝图 v1.61 d4 weighted < 6 force-downgrade 未实装

- **优先级**：🟡 中
- **类型**：蓝图-代码不一致
- **发现日期**：2026-04-25
- **修复时机**：Phase 4（6 月）
- **状态**：未启动；当前不会触发（d4 weighted=10 远超 6）

### 现状

蓝图 v1.61 line 1949 明示 "**特殊：IF 维度4分数<6 → 总分再高也降为候选级**"（数据质量 veto，针对数据稀疏行业），但 `agents/recommendation_agent.py` **没有 force-downgrade 逻辑**（grep `d4.*<.*6` / `force_downgrade` / `降为候选` 全 0 匹配）。

### 当前不紧迫

实测半导体设备 d4 weighted = 10（V1/V3/D4 16 条信号 → 100/100, weight 10），远超阈值 6。绝大多数 active 行业 d4 都不会触发这条 veto。只有 V1/V3/D4 数据严重稀疏的行业才有触发可能。

### 修复方案

`agents/recommendation_agent.py` Stage 4 级别判定后加 5-10 行：

```python
if dims["d4"].weighted < 6 and level != LEVEL_REJECT:
    level = LEVEL_CANDIDATE
    notes.append("d4_data_quality_veto: weighted<6")
```

### 工作量

1-2 小时（含单元测试覆盖 d4 weighted=5/6/7 三个 case）。

### 详见

- 蓝图 v1.61 line 1949
- TD-014b（d6 估值 veto，新设计提案，**性质不同**）
- memory: `scout_v161_veto_rules.md`

---

## TD-014b · d6 估值 veto 缺失（新设计提案）

- **优先级**：🔴 高
- **类型**：方法论改进（与 TD-012 反指标过滤层同类）
- **发现日期**：2026-04-25（半导体设备 002371/688012 实证）
- **修复时机**：Phase 2B（5 月，与 TD-012 一起做）
- **状态**：未启动

### 现状

蓝图 v1.61 **没有明文规定**估值 veto，但实证发现：**002371 (PEG=3.90) 和 688012 (PEG=4.60)** d6 score = 25（明显高估）仍获 level=A（total=90.94）。**估值过高没有 down-shift 推荐级别的机制**。

这与 TD-012 反指标硬过滤层是同类问题：6 维度加权分数高 ≠ 应该推荐，需要单维度的硬底线/veto 兜住极端 case。

### 修复方案

在 Stage 3 综合判定（`_phase3_verify` 之后、级别赋值之前）加 valuation veto：

```python
# 估值 veto: d6 < 40 且推荐级别 A → 降为 B
if level == LEVEL_A and dims["d6"].score < 40:
    level = LEVEL_B
    counter.append_note("valuation_veto_applied: d6_score<40")
```

### 阈值依据

- d6 < 40 对应 PEG > ~3 或 PE 显著偏离同行业 50%+（具体阈值待 60 条历史推荐数据回测）
- 不直接降到 candidate（避免太重），而是降一级 A→B，留给用户判断

### 工作量

2-3 小时（含 002371/688012 等 5-10 条历史 A 级推荐回测验证降级合理性）。

### 性质区分（避免混淆）

**与 TD-014a 完全不同**：TD-014a 是蓝图明文已设计但代码未实装；TD-014b 是**新设计提案**，蓝图未明示。用户曾误把这条当作蓝图原文（详见 `memory/scout_v161_veto_rules.md` 纠正记录）。

### 详见

- TD-012（反指标硬过滤层，同类问题，建议同期实装）
- memory: `scout_v161_veto_rules.md`
- docs/Scout_方法论自省.md 第二节改进 6
- 实证: 002371/688012 PEG 高估仍 A 级 (commit 6c6205b)

---

## TD-015 · gap_fillability scoring 4/5 未差异化

- **优先级**：🟡 中
- **类型**：蓝图-代码不一致
- **发现日期**：2026-04-25
- **修复时机**：Phase 4（6 月）
- **状态**：未启动

### 现状

[agents/recommendation_agent.py:641](agents/recommendation_agent.py:641) mapping `{0:0, 1:25, 2:50, 3:75, 4:100, 5:100}` 把蓝图 v1.55 设计的 "中试 vs 量产" 差异**拍平**了。今天填半导体设备时 gap_fillability=4 vs 5 对 d3 评分零差异。

蓝图 v1.55 line 5675 原意：
- 4 = 有龙头公司中试成功 + 技术路径清晰
- 5 = 有龙头公司已量产 + 技术成熟 + 成本接近平价

### 修复方案

重新设计 mapping 让 5 个档位真正区分（保持 `< 2` fatal 语义不变）：

```python
mapping = {1: 0, 2: 25, 3: 50, 4: 75, 5: 100}
# 1: fatal, score 不参与判定 (Stage 1 gate < 2)
# 2: 刚过线
# 3: 有原型量产待验证
# 4: 中试成功 (降一档反映"还没量产"的不确定)
# 5: 已量产平价
```

线性 25 步设计，5 个档位真正区分。具体阈值待历史推荐回测验证。

### 工作量

1 小时（含改 mapping + 单元测试 + 回归）。

### 详见

- 蓝图 v1.55 line 5675
- [agents/recommendation_agent.py:641](agents/recommendation_agent.py:641)
- 实证: 半导体设备 gap_fillability=4→100 (commit 6c6205b)

---

## TD-016 · CLI 单股模式 industry resolution 失效

- **优先级**：🟢 低
- **类型**：CLI bug
- **发现日期**：2026-04-25
- **修复时机**：不阻塞（有需要再做）
- **状态**：已知 workaround — 用 `SCOUT_DB_PATH` env 强制指向同一 DB

### 现状

`scout recommend --symbol XXX` 默认从 config.yaml 读 DB 路径。如果与 `SCOUT_DB_PATH` env 指向不同 DB（或 worktree 下 cwd-relative 解析错位），`_load_stock_meta(symbol)` 查不到 `related_stocks` 行，返回 `meta=None`，`industry=null` 进 `_analyze_one`。结果：d1/d2/d3/d4 全走 "缺 industry" 默认值，输出无意义评分。

实证：今天测 002371 第一次跑给出 46.25（level=candidate），加 `SCOUT_DB_PATH` env 后正常 90.94（level=A）。

### 根因（待确认）

`main.py` 的 paths 解析（`paths["kdb_path"]`）默认走 config.yaml，未优先读 `SCOUT_DB_PATH` env override；或 worktree 下 cwd-relative 路径没指向真 DB。

### 修复方案

`cmd_recommend` 调用 `agent.analyze` 之前先验证 industry 能 resolve，否则 fail-loud：

```python
meta = agent._load_stock_meta(symbol)
if not meta or not meta.get("industry"):
    logger.error(
        f"{symbol} not in related_stocks (active); resolved DB={kdb_path}; "
        f"check SCOUT_DB_PATH env vs config.yaml"
    )
    return 3
```

### 工作量

1 小时（含 fail-loud + 错误信息明确指向 DB path 不匹配）。

### 详见

- [main.py:1383](main.py:1383) `cmd_recommend`
- [agents/recommendation_agent.py:331](agents/recommendation_agent.py:331) `_load_stock_meta`
- 实证: 2026-04-25 测 002371 命中

---

## TD-017 · recommendations 表去重失效

- **优先级**：🟡 中
- **类型**：数据质量
- **发现日期**：2026-04-25
- **修复时机**：Phase 2B（5 月）
- **状态**：未启动

### 现状

`recommendations` 表 UNIQUE 约束是 `(stock, thesis_hash, recommended_at)`，但 `recommended_at` 每次 RecommendationAgent 触发都不同（UTC 微秒级 timestamp），所以 UNIQUE **实际从未生效**。

### 证据（2026-04-25 verify SQL）

230 条推荐记录 / 38 个 distinct stocks / **去重后只 95 条独立判断**（按 `stock + thesis_hash`）。即 **~41% 独立率，~59% 是重复**。

Top 重复股票（最多 8 次）：
- `002230` / `688082`：各 8 次
- `002371` (北方华创) / `688012` (中微公司) / `688256`：各 7 次（含今天 cmd_recommend 验证 commit `6c6205b` 各 +1）
- `002007` / `002013` / `002025` / `002028` / `600312` / `300144` / `002179`：各 6 次

002371 7 次级别分布：A=3 / B=2 / candidate=2

### 影响

- "230 条推荐" 实际去重后只 **95 条独立判断**（~41% 独立率）
- 用户对 Scout 产出量的心理预期被夸大约 2.4×
- 未来 Phase 4 复盘时，重复条目会**膨胀命中率统计的分母**

### 修复方案（待评估）

| 方案 | 描述 | 优势 | 风险 |
|---|---|---|---|
| **A** | 改 UNIQUE 约束为 `(stock, thesis_hash)` | 最干净 | DROP+REBUILD 表，破坏性 migration |
| **B** | 应用层去重：24h 内同 stock+thesis_hash 不 INSERT | 不动 schema | 写入逻辑变复杂，仍允许跨日重复 |
| **C** | 保留所有记录但加 `is_latest` 标志 | 历史可追溯（同股不同时段评分对比） | 多一列 + writer 维护 latest 翻转 |

### 工作量

1-2 小时（取决于选哪个方案）

### 不立即修理由

不阻塞当前运行，但影响数据质量认知和未来 Phase 4 复盘统计。Phase 2B 适合一并解决。

### 详见

- 实证 SQL：`SELECT COUNT(*) FROM recommendations` (230) vs `SELECT COUNT(DISTINCT stock||thesis_hash) FROM recommendations` (95)
- recommendations schema: [knowledge/init_db.py](knowledge/init_db.py) UNIQUE 约束行
- 也见 docs/Scout_当前进度.md 第 5 节"实战发现"

---

## TD-018a · watchlist.entered_at 5 行 NULL (migration 漏设)

- **优先级**：🟢 低
- **类型**：数据完整性
- **发现日期**：2026-04-25（调查 5 个"僵尸"行业时发现）
- **修复时机**：顺手补即可，不阻塞
- **状态**：未修

### 现状

v1.02 / v1.06 migration 把行业加进 `watchlist` 时，没设 `entered_at`。影响 5 个行业（半导体材料 / 新材料 / 生物制造 / 低空经济 / 独角兽曝光）`entered_at = NULL`。

### 影响

- "行业加入时间"信息丢失（虽然能从 `git log scripts/migrate_v102_industry_refresh.py` 反推 migration 日期 = 2026-04-18）
- 未来如果做"行业生命周期分析"（从 entered → first_signal → first_recommendation），数据缺失

### 修复方案

一次性 UPDATE 把这 5 行 `entered_at` 补上 migration 日期：

```sql
UPDATE watchlist SET entered_at = '2026-04-18T00:00:00+00:00'
WHERE industry_name IN ('半导体材料','新材料','生物制造','低空经济','独角兽曝光')
  AND entered_at IS NULL;
```

### 工作量

10 分钟（含备份 + dry-run + verify）

### 不立即修理由

不阻塞任何功能，顺手补即可。

---

## TD-018b · info_industry_map 表空，d4 用绕过逻辑

- **优先级**：🟡 中
- **类型**：架构问题（与 TD-013 同类）
- **发现日期**：2026-04-25
- **修复时机**：Phase 2A（5 月，与 TD-013 一起做架构决策）
- **状态**：未启动

### 现状

`knowledge.db` 有 `info_industry_map` 表（info_units 与 watchlist 的 FK join table），**但全表空**。`d4` 维度评分用 `info_units.related_industries LIKE '%name%'` **绕过这张表**（[agents/recommendation_agent.py:_d4_data_verification](agents/recommendation_agent.py)）。

实证（2026-04-25）：
- 半导体设备 d4 SQL 实测 90 天 16 条信号（走 LIKE 路径）
- `info_industry_map` 全表 0 行
- 5 个"僵尸"行业 + 半导体设备 + HBM 通过 `info_industry_map` 查 90 天信号都是 0

### 含义（待决定）

(a) `info_industry_map` 是设计意图被弃用 → 应明确废弃并清理代码
(b) Agents 应该维护但漏写 → 应补维护逻辑
(c) 设计冗余 → 决定保留哪个（FK join 性能更好，但 LIKE 简单直接）

**与 [TD-013](#td-013--industry_dict-表与-watchlist-数据孤岛--字段功能重复) 是同类问题**：蓝图设计了双轨，代码用脚投票选了一轨。

### 修复方案（待评估）

| 方案 | 描述 | 优势 | 劣势 |
|---|---|---|---|
| **A** | 废弃 `info_industry_map` 表（DROP） | 最干净 | 不可回退 |
| **B** | 在 info_units 写入时同步维护 `info_industry_map`（改 agents） | 保留 FK join 路径 | 写入路径变复杂 |
| **C** | 建 view 让两种查询透明 | 不动 schema | 仍是空表，治标不治本 |

### 工作量

1-2 小时（取决于选哪个方案）

### 详见

- TD-013（industry_dict 数据孤岛，同期决策）
- 实证: 2026-04-25 调查 5 僵尸行业（docs/Scout_当前进度.md §5.5/§5.6）

---

## TD-019 · global_company_id 关联数据未落地

- **优先级**：🔴 高
- **类型**：蓝图 schema 完成但数据未填 + FK 字段缺失
- **发现日期**：2026-04-25（Claude Code Phase 1 决策盘点）
- **修复时机**：Phase 2A（5 月，与 TD-013 一起做架构决策时同步推进）
- **状态**：未启动

### 现状

- **schema**: `global_companies` 表存在 ✅, `related_stocks` / `track_list` 有 `global_company_id` FK 字段 ✅
- **实际数据**: `global_companies` 表 **0 行** ❌
- **关键缺口**: `recommendations` / `stock_financials` / `price_tracking` **没加 `global_company_id` 字段** ❌
- **跨市场链路**（蓝图举例 005930.KS / SSNLF 三星共享财务）**完全空跑** ❌

### 影响

- watchlist 已加多个韩股（005290.KS / 003670.KS 等），但跨市场公司关联断链
- AI 算力即使识别成功，不能自动找出 NVDA / AVGO 对应的韩美表达
- **用户投资范围（中韩美 3 市场）与 Scout 实际能力（A 股为主）严重错配**

### 修复方案（待评估）

| 方案 | 描述 | 工作量 |
|---|---|---|
| **A** | 数据填充：用 Yahoo Finance ticker mapping / SEC EDGAR 找 ADR / 港股双地，一次性脚本写入 `global_companies` 表 | 半天到 1 天 |
| **B** | FK 字段补加：`recommendations` / `stock_financials` / `price_tracking` 加 `global_company_id` + migration + agents 同步维护逻辑 | 半天 |
| **C** | A + B 一起做，完整跨市场链路 | 1-1.5 天 |

### 不立即修理由

需要数据来源决策（Yahoo Finance vs SEC EDGAR vs 手工录入），不阻塞当前 A 股推荐链路。

### 详见

- TD-013（industry_dict 数据孤岛，同期架构决策）
- Phase 1 盘点: docs/Scout_蓝图实施盘点_2026-04-25.md #2 决策
- 蓝图设计: 三星 005930.KS + OTC SSNLF 共享财务示例

---

## TD-020 · 三市场差异权重表未实装

- **优先级**：🔴 高
- **类型**：蓝图设计未实装
- **发现日期**：2026-04-25（subagent 完整盘点 C2 / V158-05）
- **修复时机**：Phase 2B（5 月，与 TD-014b 一起做评分体系修正）
- **状态**：未启动

### 现状

蓝图 v1.58 line 1872-1882 明示三市场不同权重（CN 政策驱动 / KR 平衡 / US 财务驱动 ×2），但 `agents/recommendation_agent.py` 三市场用同一套权重（`WEIGHTS["d1"]=15` 等常数，无 market_type 分支）。

### 影响（重要）

- **US 股票走 A 股政策评分逻辑，不走美股财务驱动逻辑**
- AI 链美股（NVDA / AVGO / ANET）因此被低估
- 这是今天发现 "Scout 错过 AI 链" 的**根因之一**
- 不只是评分体系偏离设计（[Scout_设计意图与实现偏离.md](Scout_设计意图与实现偏离.md)），**也是设计本身有但代码没实装**

### 修复方案

`recommendation_agent.py` 加 market_type 判断 + 三套权重表：

```python
WEIGHTS_BY_MARKET = {
    'A':  {'d1': 15, 'd2': 15, 'd3': 15, 'd4': 10, 'd5': 15, 'd6': 10},  # 政策驱动
    'KR': {'d1': 12, 'd2': 12, 'd3': 12, 'd4': 12, 'd5': 22, 'd6': 10},  # 平衡
    'US': {'d1':  5, 'd2':  5, 'd3':  5, 'd4': 10, 'd5': 30, 'd6': 25},  # 财务驱动 (×2)
}
```

具体阈值待蓝图原文 + 历史数据验证。

### 工作量

1-2 小时（含改 weights + market_type 判断 + 单元测试 + 半导体设备/HBM 回归对比）

### 重要性

修这一条**可能让 Scout 美股推荐质量阶跃式提升**，不需要重写评分体系。

### 详见

- 蓝图 v1.58 line 1872-1882
- [docs/Scout_完整蓝图盘点_2026-04-25.md](Scout_完整蓝图盘点_2026-04-25.md) C2 / V158-05
- [docs/Scout_设计意图与实现偏离.md](Scout_设计意图与实现偏离.md) 第五A节

### 2026-04-25 dry-run 发现

**dry-run 结果摘要**:
- 230 条 recommendations **全部 market=A**
- 3 只 A 级（002371 / 688012 / 688082）新权重下**全部下降 -2 ~ -9 点**（高 PEG 被 d6 加重权重打压）
- 总体: 升级 34 / 降级 5 / 无变化 191

**阻塞链路**:
- [agents/recommendation_agent.py:347-360](agents/recommendation_agent.py) `load_universe` SQL `WHERE market='A'` 硬过滤 → KR/US 标的根本进不了推荐池
- **真正阻塞**: KR/US 财务覆盖 **0%**（48 个 KR/US 标的进 `related_stocks`，**0 个**进 `stock_financials`，has_z/peg/f 全 0）
- FinancialAgent (v1.01) 当前只覆盖 A 股，蓝图设计的"韩股 OpenDART 全自动 / 美股 yfinance"未实装

**状态变更**: 未启动 → **blocked by Phase 2A KR/US 财务采集**

**branch 留存**: [td-020-dryrun](https://github.com/yuyingjun0922/Scout/tree/td-020-dryrun) — **不合并**，作为反推权重设计的实证存档

**反推权重 v1.0 基线**（暂存，等 Phase 2A 财务采集到位后激活）:

```python
WEIGHTS_BY_MARKET = {
    'A':  {'d1': 15, 'd2': 10, 'd3': 20, 'd4': 20, 'd5': 20, 'd6': 15},
    'KR': {'d1': 12, 'd2':  8, 'd3': 15, 'd4': 20, 'd5': 30, 'd6': 15},
    'US': {'d1':  5, 'd2':  5, 'd3': 15, 'd4': 15, 'd5': 35, 'd6': 25},
}
```

详见 [docs/Scout_能力边界_2026-04-25.md](Scout_能力边界_2026-04-25.md) 4 个事实归档。

---

## TD-021 · V160 mixed_subtype 下游分级处理缺失

- **优先级**：🔴 高
- **类型**：蓝图设计未实装
- **发现日期**：2026-04-25（subagent 完整盘点 C3 / V160-02/03/05）
- **修复时机**：Phase 2C（5 月底-6 月）
- **状态**：未启动

### 现状

v1.60 设计了 `mixed_subtype`（`conflict` / `structural` / `stage_difference`）识别行业内结构性分化（例：半导体设备 消费电子-29% vs AI HBM+9%），但**只有标签没下游动作**：

- `conflict` 应该暂停该行业推荐 → **未做**
- `structural` 应该写入 `sub_market_signals` → **未做**

### 影响

v1.60 设计的精细化识别完全空跑，行业内分化信号采集到了但下游不消费。

### 修复方案

GateAgent 后加 `mixed_subtype` 路由逻辑：

```python
if signal.mixed_subtype == 'conflict':
    pause_industry_recommendations(industry, reason='conflict_signal')
elif signal.mixed_subtype == 'structural':
    write_sub_market_signals(industry, signal.sub_market_data)
```

### 工作量

半天

### 详见

- 蓝图 v1.60 line 5679（`sub_market_signals` schema）
- [docs/Scout_完整蓝图盘点_2026-04-25.md](Scout_完整蓝图盘点_2026-04-25.md) C3

---

## TD-022 · recommendations.mode 切换 + v1.56 前视偏差 ×0.8 未实装

- **优先级**：🔴 高
- **类型**：蓝图设计未实装
- **发现日期**：2026-04-25（subagent 完整盘点 C4 / D008）
- **修复时机**：Phase 2B（与 TD-014b / TD-020 一起做评分体系修正）
- **状态**：未启动

### 现状

- `recommendations.mode` 字段存在但 230 行**全是 `cold_start`**，模式切换逻辑未实装
- v1.56 设计的"冷启动期推荐分数 ×0.8 前视偏差缓解"**代码层零引用**

### 影响

当前所有 230 条推荐没做前视偏差校正，未来 Phase 4 复盘时分数会被**系统性高估**（cold_start 期不应给满分置信度）。002371/688012 当前 90.94 在 ×0.8 校正后实际应是 ~73（B 级而非 A 级）。

### 修复方案

| 步骤 | 内容 |
|---|---|
| A | RecommendationAgent 启动时检查 mode（cold_start vs running 切换条件待定，可能按 30/60/90 天阈值）|
| B | cold_start 期 `final_score *= 0.8` 应用前视偏差缓解 |
| C | 历史 230 条推荐回填校正分数（**谨慎操作**，加新列 `cold_start_adjusted_score`，不直接覆盖 total_score）|

### 工作量

半天

### 详见

- 蓝图 v1.56 前视偏差缓解
- [docs/Scout_完整蓝图盘点_2026-04-25.md](Scout_完整蓝图盘点_2026-04-25.md) C4 / D008

---

## TD-023 · scout_range 5 档退化为 2 档（v1.55 早期识别机制未实装）

- **优先级**：🔴 高（从 candidate 🟡 升级 — 是 Scout 错过早期 emerging sectors 窗口的核心机制）
- **类型**：蓝图设计未实装
- **发现日期**：2026-04-25（subagent 完整盘点 C1，今日评估升级）
- **修复时机**：Phase 2C（5 月底-6 月）
- **状态**：未启动

### 现状

蓝图 v1.55 设计了 5 档 `scout_range`：

| 档位 | 含义 | 渗透率 |
|---|---|---|
| `early_strict` | 主动观察等待 | < 5% |
| `early_qualified` | **介入窗口** | 5-15% |
| `active` | 主动推荐 | 15-30% |
| `mature` | 减仓考虑 | 30-50% |
| `out_of_range` | 退出 | > 50% |

实际：`watchlist.zone` **只用 2 档**（`active` / `observation`），v1.55 早期识别机制完全未实装。

### 影响

Scout 错过 "emerging sectors" 早期窗口的根因之一。**当前 Scout 看到的是"已经 active 的行业"，看不到"将要 active"的早期信号**。

### 修复方案

| 步骤 | 内容 |
|---|---|
| A | watchlist 加 `scout_range TEXT` 列（5 档枚举） |
| B | RecommendationAgent 加 `scout_range` 路由逻辑 |
| C | 冷启动期手工设置每个行业的 `scout_range` |
| D | 后续接入 `penetration_rate` 自动判断 |

### 工作量

1-2 天

### 关联

- TD-020（三市场权重）+ TD-023 都是 "实装已有设计" Layer 1
- 蓝图 v1.55 line 4549-4555

---

## TD-024 · policy_risk='fatal' 自动链路缺失

- **优先级**：🟡 中
- **类型**：蓝图设计未实装
- **发现日期**：2026-04-25（subagent 完整盘点 C5 / EM-07）
- **修复时机**：Phase 2B（5 月）
- **状态**：未启动

### 现状

关键词命中 fatal 风险后，标签写入 `watchlist.policy_risk`，但**无下游动作**：

- 不自动改 zone（active 行业仍 active 跑推荐）
- 不自动暂停该行业的推荐生成

### 影响

政策风险识别有但**不传导**。极端 case（如教培"双减"重演）Scout 会标记 fatal 但继续推荐。

### 修复方案

```python
# watchlist 写入 policy_risk='fatal' 时:
if policy_risk == 'fatal':
    update_watchlist_zone(industry, zone='cold')

# RecommendationAgent 全量扫描时:
if industry.zone == 'cold':
    skip_industry(reason='policy_risk_fatal')
```

### 工作量

半天

### 详见

- 蓝图 EM-07 line 2258
- [docs/Scout_完整蓝图盘点_2026-04-25.md](Scout_完整蓝图盘点_2026-04-25.md) C5 / EM-07

---

## TD-025 · industry_dict.sub_industries weight_in_parent 字段缺失

- **优先级**：🟡 中（**但 TD-013 决策后可能消失**）
- **类型**：数据缺失 + 依赖架构决策
- **发现日期**：2026-04-25（subagent 完整盘点 C7 / V158-01）
- **修复时机**：Phase 2A（与 TD-013 一起决策）
- **状态**：未启动

### 现状

v1.58 设计 `sub_industries` 加权 fillability 评分需 `weight_in_parent` 字段。`industry_dict` 4 行 `sub_industries` 数据**全部没填 weight_in_parent**。

### 依赖关系（关键）

| TD-013 选择 | 本 TD 状态 |
|---|---|
| 方案 B（废弃 industry_dict.sub_industries） | **本 TD 自动消失** |
| 方案 A（保留并扩充） | 本 TD 必须修 |

### 工作量

半天（如果走方案 A）

### 详见

- TD-013（架构决策依赖）
- 蓝图 V158-01 line 5786

---

## TD-026 · 独角兽曝光性质重新设计

- **优先级**：🔴 高（用户明确"Scout 寻找独角兽"是核心定位）
- **类型**：设计调整 + 数据源调整
- **发现日期**：2026-04-25（5 僵尸行业调查 + 用户原意确认）
- **修复时机**：Phase 2C（5 月底-6 月）或更晚（依赖外部 API key 申请）
- **状态**：未启动

### 现状

`watchlist` 有"独角兽曝光"行业（id=23），`related_stocks` 配置 MSFT/GOOGL/AMZN/Broadcom/Arista 等 **12 只全部 US mega-cap，不是真独角兽**。12 只全部 0 推荐（RecommendationAgent 不扫 US 大盘）。

### 用户原意（2026-04-25）

> "独角兽的出现可以看出是行业的爆发, scout 就是寻找独角兽"

### 设计矛盾

用户想要 Scout 通过独角兽信号识别行业爆发，**当前实现是把 mega-cap 当独角兽放在 watchlist 行业里**。

### 修复方案（推荐 C — 独角兽作为信号源，不作为 watchlist 行业）

**1. info_units 表加字段**:
- `mega_round_count`（本期大轮融资次数）
- `new_unicorn_count`（本期新晋独角兽数）

**2. 数据源接入**:
- CB Insights API（全球独角兽追踪）
- Hurun China Unicorn Report（中国侧）
- ITjuzi / Qichacha / 36Kr（中国侧补充）

**3. 创建 industry-unicorn 历史映射表**:
- 记录每个行业**首个独角兽出现年**
- 记录每个行业**首个 D1/D4/D6 信号年**
- 计算"信号→爆发"lag

**4. d5 维度（标的财务）加监控指标**:
- 该行业近 6 个月新独角兽数 → 加分
- 该行业近 6 个月 mega round 数 → 加分

### 废弃方案

- watchlist 删除"独角兽曝光"行业（zone='archived'）
- related_stocks 12 条 mega-cap 标记为"已 graduate from unicorn"，保留作为历史参考但不入推荐池

### 工作量

2-3 天（含数据源对接）

### 关联

与 TD-020（三市场权重）+ [docs/Scout_设计意图与实现偏离.md](Scout_设计意图与实现偏离.md) 第八节"市场缺口识别"是**同一主题** — 让 Scout 真正能识别新兴机会。

---

## TD-001（历史：Ollama helper 代码重复等）

> 见 [CLAUDE.md §5 重要技术债务](../CLAUDE.md) 原清单（Ollama helper 抽取、`__init__.py` 缺失、watchlist.notes composite 字符串、queue.db UNIQUE 缺失、watchlist.zone CHECK 约束、CLI `asyncio.run` 开销、Windows SIGTERM、Gemma 模型名约定）。
>
> 它们仍然有效，本清单只追加**新发现**的债务条目。
