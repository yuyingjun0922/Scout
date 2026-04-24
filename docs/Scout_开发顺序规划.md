# Scout 开发顺序规划

**最后更新**：2026-04-24
**当前版本**：v1.15（Phase 2A 收尾中 → Phase 2B 启动前）

---

## 阶段全景

| 阶段 | 状态 | 里程碑 | 交付 |
|---|---|---|---|
| Phase 1 | ✅ 完成 | 2026-04-18 v1.00 | 14/14 验收项通过，1031 tests passing，基础管线打通 |
| **Phase 2A** | ✅ 收尾完成 | 2026-04-18 v1.12 | FinancialAgent + RecommendationAgent + MotivationDriftAgent + DirectionBackfill + V3 Playwright + PushConsumerAgent |
| Phase 2A 延伸 | ⚠️ 进行中 | 2026-04-24 v1.15 | Watchdog 实战验证 + Dashboard + suppress 清单（有 bug） |
| Phase 2B | 🔜 启动中 | 预计 2026-05-01 开工 | event_chains + related_stocks 自动化 + 外部推送通道 |
| Phase 3 | 📅 后续 | Q3 2026 | 纠偏/纠错模块 + 学习闭环 |

---

## Phase 2A（✅ 已交付）

| 版本 | 交付内容 | 测试增量 | 完成日期 |
|---|---|---|---|
| v1.01 | FinancialAgent（Z''-Score 1995）+ MasterAgent | +95 | 2026-04-05 |
| v1.07 | RecommendationAgent（规则 + Sonnet LLM，A/B/candidate/reject） | +72 | 2026-04-10 |
| v1.08 | MotivationDriftAgent（180 天） | +38 | 2026-04-12 |
| v1.10 | DirectionBackfillAgent | +21 | 2026-04-14 |
| v1.11 | V3 Playwright 激活 | +29 | 2026-04-16 |
| v1.12 | PushConsumerAgent + MCP 拉取 | +51 | 2026-04-18 |

**当前 passing tests**：1424

---

## Phase 2A 延伸（⚠️ 实战运维期）

2026-04-18 ~ 2026-04-24 的实战运行 + 运维增强：

| 版本 | 交付 | 日期 | 备注 |
|---|---|---|---|
| v1.13 | HealthMonitorAgent（agent_errors 告警）| 2026-04-19 | 误报多 |
| v1.14 | Scout Dashboard（emoji/盒子/小猫动画）| 2026-04-20 ~ 2026-04-23 | 闪屏 → 回归纯文字 |
| v1.15 | SUPPRESSED_ERRORS 抑制清单（有 bug，见 TD-002） | 2026-04-24 | until 日期已改 |
| v1.15.x | Watchdog 实战验证（7 次救活） | 2026-04-24 | [D-018](Scout_技术决策记录.md#d-018) |

**待清零再进 Phase 2B**：
1. TD-002 suppress 未生效（🔴 高）
2. TD-003 paper_d4 P1 API key（🔴 高，审批阻塞中）
3. TD-004 collect_V3 偶发（🟡 观察）

---

## Phase 2B（🔜 启动）

### B.1 目标（按优先级）

1. **event_chains 事件串**
   - 新文件 `agents/event_chain_agent.py`
   - 依赖 `event_chains` 表、`info_units.event_chain_id`（v1.59 已预留字段）
   - 目的：跨 info_unit 串联同一主题事件（例："工信部新规 → 企业合规成本升 → 利空某股"）
   - 预期工作量：**3 天**（含 schema、Agent、LLM prompt、5 个 fixture 回归）

2. **related_stocks 填充自动化**
   - 修改 `scripts/load_v105_related_stocks.py` → Agent 化
   - 现状：手工半自动
   - 目的：新增行业时自动拉取相关标的清单（A 股 / KOSPI / NASDAQ 三地）
   - 依赖：akshare + pykrx + yfinance
   - 预期工作量：**2 天**

3. **外部推送通道**
   - 新文件 `infra/push_channel_telegram.py` / `push_channel_email.py` / `push_channel_openclaw.py`
   - 事件总线抽象：`infra/event_bus.py`（观察者模式，每个 channel 订阅）
   - 目的：不再只靠 QQ 和 MCP pull，可主动推 Telegram / 邮件 / OpenClaw webhook
   - 预期工作量：**4 天**（含 3 个通道 + 事件总线 + 测试）

4. **suppress v2**（延伸自 TD-002）
   - 重构 `HealthMonitorAgent.SUPPRESSED_ERRORS` 为 DB 表 `alert_suppressions`
   - CLI 管理：`python main.py suppress add/list/remove`
   - 目的：告警抑制可热更新，不用改代码重启
   - 预期工作量：**1.5 天**

### B.2 Phase 2B 总工作量估算

- 核心 4 项：**10.5 天**
- 测试 + 文档：+2 天
- 缓冲 20%：+2.5 天
- **合计约 15 工作日**（约 3 周）

### B.3 Phase 2B Entry Points

> 见 [CLAUDE.md §6](../CLAUDE.md) 的表格，Phase 2B 行已标出。

---

## Phase 3（📅 Q3 2026 预研）

- **纠偏/纠错模块**：对历史推荐回溯，识别"哪些方向判断是错的、为什么错"，进入学习闭环
- **认知偏误检查模块**（见 `Desktop/Scout/认知偏误检查模块设计文档.md`）
- **大师模块**（见 `Desktop/Scout/大师模块设计文档.md`）：用巴菲特/林奇/Druckenmiller 等思想流派做交叉验证

---

## 不改动的边界（再次重申）

❌ 自动交易
❌ 价格预测 / 点位预测
❌ 情绪识别 / 社交媒体抓取
❌ 加密货币
❌ 把 `asyncio.run` 嵌入热路径
❌ 明文 API key 进 DB/config.yaml
❌ MCP 工具内 raise 异常

（详见 [CLAUDE.md §7](../CLAUDE.md)）
