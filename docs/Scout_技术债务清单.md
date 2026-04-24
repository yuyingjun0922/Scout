# Scout 技术债务清单

> 格式：**TD-编号 / 标题 / 优先级 / 发现日期 / 现象 / 根因 / 修复方案 / 预期工作量 / 状态**。
> 优先级：🔴 高（影响用户感受或数据正确性）/ 🟡 中（运营层面）/ 🟢 低（代码卫生）。

---

## TD-002 · `SUPPRESSED_ERRORS` 未真正屏蔽告警推送

- **优先级**：🔴 高
- **发现日期**：2026-04-24
- **归属模块**：`agents/health_monitor_agent.py`
- **状态**：待修

### 现象

v1.15 引入 `SUPPRESSED_ERRORS` 清单用于临时屏蔽已知非关键告警（如 `akshare_s4 × RemoteDisconnected`），until=2026-04-27（后因用户 04-24 实际回家改为 2026-04-24）。

用户离家 5 天（2026-04-19~2026-04-24），QQ 仍持续收到 akshare_s4 告警（且数量与未抑制时无明显差异），`agent_errors` 日计数：

```
04-19: akshare_s4 = 12
04-20: akshare_s4 = 6
04-21: akshare_s4 = 6
04-22: akshare_s4 = 5
04-23: akshare_s4 = 6
04-24: akshare_s4 = 6
```

### 可能根因（待排查）

1. **时区比较问题**：
   - `SUPPRESSED_ERRORS[("akshare_s4","RemoteDisconnected")]["until"] = datetime(2026,4,24,tzinfo=KST)` 代表 **KST 2026-04-24 00:00** = **UTC 2026-04-23 15:00**
   - `_is_suppressed` 里 `if until and now_utc_dt >= until: continue`（意为抑制过期跳过）
   - 所以在 **KST 2026-04-24 00:00 之后所有时刻**，抑制都已失效 — 这是**正确但不是用户期望**的行为（用户以为 04-24 当天仍抑制）
   - 但即便如此，04-19~04-23 应该是有效抑制窗口，却仍收到告警

2. **子串匹配漏过**：
   - `pattern="RemoteDisconnected"` 子串匹配，但 `agent_errors` 中也有 `pattern="ConnectionError"` 变体，清单里两条都登记了
   - 需核查是否有第 3 种错误信息变体（如 `urllib3.exceptions.*`）未覆盖

3. **调用链未接上**：
   - `_is_suppressed` 是否真的在 `_check_errors_impl` 主路径被调用？每个 `agent_name` 分支都有吗？
   - `HealthMonitorAgent.run_check_errors` 调度频次如何，是否所有路径都经过抑制判断？

4. **push_queue 重复落队**：
   - 抑制命中时 `log + queue 不落`，但如果被另一个 agent（如 MasterAgent）另起路径推送，会绕过抑制

### 修复方案（建议步骤）

1. 增加单元测试：**5 种错误消息变体 × 抑制命中/未命中** 参数化矩阵（遵循用户偏好的"参数化测试 + 防御性加严"）
2. 把 `until` 的语义从"until 这个 KST 日期的 00:00"改成"until 这个 KST 日期的 **23:59:59**"，对齐用户直觉
3. 排查 5 天内所有 akshare_s4 告警的 push_outbox 来源（是否全走 HealthMonitorAgent）
4. 日志里抑制命中后打印 `[suppress] ...`，5 天回看日志确认是否真被抑制了但推送走了别的路径

### 预期工作量

- 排查：0.5 天
- 修复 + 加测试：0.5 天
- 回归验证：离家模拟（手动制造错误，看是否被正确抑制）0.5 天
- **合计 1.5 天**

---

## TD-003 · `paper_d4` P0 修复效果不佳，S2 桶耗光

- **优先级**：🔴 高
- **发现日期**：2026-04-24
- **归属模块**：`agents/paper_d4_agent.py`（SemanticScholar 客户端）
- **状态**：P0 已落地但不够，待 P1

### 现象

Paper D4 依赖 SemanticScholar API 采集科研动向。P0 修复（指数退避 + 429 重试 + 桶分流 S1→S2→…）上线后错误数下降但**仍每日有新 429 落库**：

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
- S1 用尽后切 S2（开 Key 池）— 但**现有开 Key 本身也共享同一个 IP 匿名限额**，根本不是独立额度
- 即使 P0 加了 jittered backoff，summary-level 查询量 > 100/5min 时仍会触碰上限

### 修复方案

**P1（本周）**：申请 SemanticScholar **Partner API Key**

- URL：https://www.semanticscholar.org/product/api#api-key-form
- 审批 1~5 业务日
- 批准后：10000 req/5min，够用 100x
- **Key 放 `ANTHROPIC_API_KEY` 同级的 env**（`SEMANTIC_SCHOLAR_API_KEY`），不进 config.yaml、不进 DB

**P2（备选）**：若 key 审批被拒
- 切 Crossref + OpenAlex 作为 paper_d4 数据源（已知覆盖度比 SemanticScholar 低 ~20%）

### 预期工作量

- 申请 API Key：10 分钟（提交），等 1~5 天
- 接线（env read + header 加 `x-api-key`）：0.5 天
- 回归（看 7 天内 429 是否归零）：观测期

---

## TD-004 · `collect_V3` 04-21 偶发告警

- **优先级**：🟡 中（偶发，其余日期正常）
- **发现日期**：2026-04-21（观察到）
- **归属模块**：`infra/data_adapters/korea_customs_playwright.py`（V3 韩国关税厅 10대 품목 YTD 采集）
- **状态**：待复盘，未修

### 现象

2026-04-21 全天 `scout.log` 调度器 heartbeat 持续显示：
```
failures={'collect_V3': 2}
```

说明 collect_V3 任务在 04-21 连续 2 次失败。但 `agent_errors` 表没有对应 V3 agent 的行（说明错误发生在 scheduler 层或被 Playwright 吞了 trace 但未落 error matrix）。

04-20 / 04-22 / 04-23 / 04-24 均正常，没有 failures。

### 可能根因

1. **Playwright chromium 偶发启动失败**（Windows 权限、临时目录清理、内存压力）
2. **韩国关税厅网站 04-21 DDoS 防护临时拦截**（User-Agent 或频率）
3. **网络临时故障**（Watchdog 04-19 22:19 曾 restart gateway；可能当时上游网络有抖动）

### 修复方案

1. **增强 trace**：collect_V3 失败时把 Playwright console log 落 `agent_errors.context_data` JSON，方便事后复盘
2. **观察 2 周**：若再次复现才投入修复；单次偶发不做处理（遵循"防止过度工程"）
3. **兜底**：失败时 fallback 到 `korea_customs.py` HTTP 版（现在已是 fallback，但日志层面没确认是否真被调用）

### 预期工作量

- 加 trace 埋点：0.5 天
- 等待观察：2 周
- 复现后排查：按实际情况估

---

## TD-001（历史：Ollama helper 代码重复等）

> 见 [CLAUDE.md §5 重要技术债务](../CLAUDE.md) 原清单（Ollama helper 抽取、`__init__.py` 缺失、watchlist.notes composite 字符串、queue.db UNIQUE 缺失、watchlist.zone CHECK 约束、CLI `asyncio.run` 开销、Windows SIGTERM、Gemma 模型名约定）。
>
> 它们仍然有效，本清单只追加 **新发现** 的债务条目。
