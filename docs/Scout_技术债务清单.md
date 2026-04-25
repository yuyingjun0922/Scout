# Scout 技术债务清单

> 格式：**TD-编号 / 标题 / 优先级 / 发现日期 / 现象 / 根因 / 修复方案 / 预期工作量 / 状态**。
> 优先级：🔴 高（影响用户感受或数据正确性）/ 🟡 中（运营层面）/ 🟢 低（代码卫生）。

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

## TD-001（历史：Ollama helper 代码重复等）

> 见 [CLAUDE.md §5 重要技术债务](../CLAUDE.md) 原清单（Ollama helper 抽取、`__init__.py` 缺失、watchlist.notes composite 字符串、queue.db UNIQUE 缺失、watchlist.zone CHECK 约束、CLI `asyncio.run` 开销、Windows SIGTERM、Gemma 模型名约定）。
>
> 它们仍然有效，本清单只追加**新发现**的债务条目。
