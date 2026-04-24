# Scout 当前进度

**最后更新**：2026-04-24 (KST)
**版本**：v1.15（suppress 修复中）

---

## 1. 运行态（截至 2026-04-24 19:30 KST）

- **scout serve**：on；最近一次重启 2026-04-24 19:04 KST（Watchdog 自动拉起）
- **Ollama gemma4:e4b**：on；最近一次重启 2026-04-24 19:09 KST（Watchdog 自动拉起）
- **OpenClaw Gateway**：on
- **APScheduler heartbeat**：正常，5 分钟一跳
- **推送通道**：MCP 主通道 + QQ Watchdog 副通道（Phase 2A）

---

## 2. 5 天连续运行结果（2026-04-19 → 2026-04-24）

### 2.1 Watchdog 救活统计

| 日期 | 事件 | 目标 | 次数 |
|---|---|---|---|
| 2026-04-19 22:12 | DOWN + restart | ollama | 1 |
| 2026-04-19 22:13~22:18 | DOWN + restart | scout serve | 3（hist 0/1/2） |
| 2026-04-19 22:19 | DOWN + restart | gateway | 1 |
| 2026-04-24 19:04 | DOWN + restart | scout serve | 1 |
| 2026-04-24 19:09 | DOWN + restart | ollama | 1 |

**合计**：5 天内 Watchdog 自动救活 **7 次**；心跳（🟢 三件套全部在线）04-20~04-24 每天 09:04 KST 准点到达。Watchdog 机制**实战验证有效**，用户不在期间无需人工干预。

### 2.2 推送队列吞吐

```
日期        done   failed
2026-04-19    20      0
2026-04-20    18      0
2026-04-21    14      0
2026-04-22    11      0
2026-04-23    11      0
2026-04-24     4      0  （统计到 19:30 KST）
```

5 天累计推送完成 **78 条**，无 failed。

### 2.3 推荐池增长

| 指标 | 2026-04-19 基线 | 2026-04-24 现在 | 增量 |
|---|---|---|---|
| 总推荐数 | 154 | **228** | +74 |
| A 级 | - | 6 | - |
| B 级 | - | 24 | - |
| candidate | - | 150 | - |
| reject | - | 48 | - |

**结论**：推荐管线稳定增长 +48%；A/B 合计 30 条，占比 13%（符合规则 gate + LLM 的筛选强度预期）。

### 2.4 info_units 新增

| 日期 | 新增 units |
|---|---|
| 2026-04-19 | 2 |
| 2026-04-20 | 12 |
| 2026-04-21 | 10 |
| 2026-04-22 | 4 |
| 2026-04-23 | 2 |
| 2026-04-24 | 1（仅到 19:30） |

5 天新增 **31 条**（D1/D4/S4 分别 11/18/2）。

---

## 3. 本期问题与待办

### 3.1 suppress 告警未生效（高优先级）

**现象**：`HealthMonitorAgent.SUPPRESSED_ERRORS` 中登记的 `akshare_s4 × RemoteDisconnected/ConnectionError` 条目，until=2026-04-24 KST，但用户反馈在周中（用户离家期间）**QQ 仍收到 akshare 告警推送**，说明抑制未生效。

**可能根因**（待核）：
1. `_is_suppressed` 比较 `now_utc_dt >= until`，但 until 是 KST-naive-to-aware 的 `datetime(2026,4,24,tzinfo=KST)` = UTC 2026-04-23 15:00。如果检测时点已过 15:00 UTC（23:00 KST），抑制在 23:00 KST 开始失效但用户期望到 04-24 23:59 KST 才失效。
2. error_message 子串匹配可能被最新 akshare 异常信息变更（如 `RemoteDisconnected('Remote end closed connection')` 变成 `ConnectionError(...)`）绕过。
3. push_queue 去重未按 `entity_key` 正确聚合，同一 agent 同一小时被推多次。

**详见**：[Scout_技术债务清单.md](Scout_技术债务清单.md#td-002)

### 3.2 paper_d4 P0 修复效果不佳

P0 修复后错误数由 04-19 的 24 条降到 04-24 的 4 条，但**仍每天有新错误**（全部 SemanticScholar HTTP 429 retryable）。S2 bucket 已耗光，**需 P1（API key 注册 + rate limit 上调）才能根除**。详见 [TD-003](Scout_技术债务清单.md#td-003)。

### 3.3 collect_V3 04-21 偶发告警

04-21 全天调度器 heartbeat 持续显示 `failures={'collect_V3': 2}`。V3 韩国关税厅 Playwright 采集偶发失败（无 agent_errors 落库，只到 scheduler 层面的 failure counter）。其余日期 V3 正常。详见 [TD-004](Scout_技术债务清单.md#td-004)。

### 3.4 akshare_s4 RemoteDisconnected 持续

5 天内每天 5~12 条，原因明确（东财拒绝默认 UA），已在 suppress 清单登记（但未生效，见 3.1）。**P2**：新浪 fallback 待做。

---

## 4. 本期交付

| 项 | 状态 | 说明 |
|---|---|---|
| Watchdog 5 分钟一检 + 自动重启 + QQ 心跳 | ✅ 完成 | 实战验证 7 次救活 |
| Dashboard 纯文字版（去除小猫避免闪屏） | ✅ 完成 | commit `756f78a` |
| Suppress 清单 v1.15 | ⚠️ 部分 | until 改到 04-24，仍有漏推 |
| Phase 2A v1.12 PushConsumerAgent + MCP 拉取 | ✅ 完成 | Phase 2A 收尾 |

---

## 5. 下一步（优先级排序）

1. **TD-002** 修复 suppress 逻辑（今天/明天）
2. **TD-003** 申请 SemanticScholar API key（本周）
3. **TD-004** 复盘 collect_V3 04-21 偶发（低优先，观察 7 天再决定）
4. **Phase 2B 启动**：event_chains、related_stocks 自动化、外部推送通道 — 见 [Scout_开发顺序规划.md](Scout_开发顺序规划.md)
