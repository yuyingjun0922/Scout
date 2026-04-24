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

## TD-007 · Watchdog `state.json` schema 不一致导致 scout/gateway 限流失效

- **优先级**：🟡 中（限流失效不会立刻出事，但极端情况下可能分钟级硬重启风暴）
- **发现日期**：2026-04-25（修 TD-002 重启 scout serve 时观察到）
- **归属模块**：`C:\Tools\scout-watchdog.ps1` + `C:\Tools\scout-watchdog-state.json`
- **状态**：待修

### 现象

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

### 根因

Watchdog PowerShell 脚本在写 state 时对 `ollama` 走 list append 分支，对 `scout`/`gateway` 走 string overwrite 分支。`DOWN ... restart attempt (hist=N/3)` 日志里，`hist` 计数靠读 list 长度 → list-分支有 N；string-分支读不出 list 长度，可能恒为 0。

**实战影响**：2026-04-19 22:13~22:18 scout 连续 3 次 restart 时 log 显示 `hist=0/3, 1/3, 2/3` — **那次限流其实生效了**（可能是该次特殊 path 或内存态 counter 托底）。但按 state.json 当前形态，**scout 每次重启后只记录最近一次**，下次重启时读不到前次 → 限流失效 → 可能陷入分钟级重启风暴。

### 修复方案

1. **统一 schema**：`restart_history.*` 全部为 `list[str]`，写入时 `+= [$timestamp]`，读取时按时间窗口过滤前 N 个。
2. **迁移**：现有 state.json 手动改一次，把 `scout` 和 `gateway` 的 string 包成 single-element list。
3. **hist 计算对齐**：scout-watchdog.ps1 的 hist 变量从 list 长度派生，三个 target 走同一分支。
4. **加 pester 单测**（可选）：mock state.json，多次连环 DOWN，断言 hist 递增且到 3/3 后跳过重启直到下一小时窗口。

### 预期工作量

- 脚本改动：0.5 天
- 手动迁移 state.json：5 分钟
- 单测（可选）：0.5 天

### 验证

- 修后手动连续 `Stop-Process scout` 4 次，观察第 4 次应该被限流（log 不触发 restart）
- 观察 1 周，确认 state.json 的三个 target 都保持 list schema

---

## TD-001（历史：Ollama helper 代码重复等）

> 见 [CLAUDE.md §5 重要技术债务](../CLAUDE.md) 原清单（Ollama helper 抽取、`__init__.py` 缺失、watchlist.notes composite 字符串、queue.db UNIQUE 缺失、watchlist.zone CHECK 约束、CLI `asyncio.run` 开销、Windows SIGTERM、Gemma 模型名约定）。
>
> 它们仍然有效，本清单只追加**新发现**的债务条目。
