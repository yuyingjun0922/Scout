# Scout 开发顺序规划

**最后更新**：2026-04-24
**当前状态**：Phase 2A ✅ 完成 / Phase 2B 🚧 进行中（QQ 插件 **3/8** 工具实装）

---

## 阶段全景

| 阶段 | 状态 | 里程碑 | 交付 |
|---|---|---|---|
| Phase 1 | ✅ 完成 | 2026-04-18 v1.00 | 14/14 验收项通过，1031 passing tests，基础管线打通 |
| **Phase 2A** | ✅ **完成** | 2026-04-18 v1.12 | FinancialAgent + RecommendationAgent + MotivationDriftAgent + DirectionBackfill + V3 Playwright + PushConsumerAgent |
| Phase 2A 延伸 | ✅ 完成 | 2026-04-24 v1.15 | Watchdog 实战验证 + Dashboard + QQ 直调通道 + SUPPRESSED_ERRORS（有 bug，TD-002） |
| **Phase 2B** | 🚧 **进行中** | 预计 2026-05-15 完成 | QQ 插件 8 工具 + event_chains + related_stocks 自动化 + 外部推送通道 |
| Phase 3 | 📅 Q3 2026 | — | 纠偏/纠错模块 + 学习闭环 + 大师模块 |

---

## Phase 2A（✅ 已完成）

| 版本 | 交付 | 测试增量 | 完成日期 |
|---|---|---|---|
| v1.01 | FinancialAgent（Z''-Score 1995）+ MasterAgent | +95 | 2026-04-05 |
| v1.07 | RecommendationAgent（规则 + Sonnet LLM，A/B/candidate/reject） | +72 | 2026-04-10 |
| v1.08 | MotivationDriftAgent（180 天周期） | +38 | 2026-04-12 |
| v1.10 | DirectionBackfillAgent | +21 | 2026-04-14 |
| v1.11 | V3 Playwright 激活 | +29 | 2026-04-16 |
| v1.12 | PushConsumerAgent + MCP 拉取 | +51 | 2026-04-18 |

**Phase 2A passing tests 合计**：1424

**Phase 2A 收尾延伸**（v1.13 ~ v1.15，部署+运维层面）：
- v1.13 HealthMonitorAgent + `infra/qq_channel.py`（QQ 直调）— 2026-04-19
- v1.14 Dashboard（含小猫动画迭代 → 最终回归纯文字版）— 2026-04-20 ~ 23
- v1.15 `SUPPRESSED_ERRORS` 抑制清单（有 bug → TD-002）— 2026-04-24

---

## Phase 2B（🚧 进行中）

### B.1 QQ 插件（OpenClaw `openclaw-qqbot`）— **3/8 工具已实装**

已完成 3 个：

| # | 工具 | 用途 | 状态 |
|---|---|---|---|
| 1 | `qqbot-channel.send_message` | 主动推送 C2C 文本 | ✅ 实装（`infra/qq_channel.py`） |
| 2 | `qqbot-channel.proactive` | 免 Gateway 直调的主动消息 | ✅ 实装（走 D-020 路径） |
| 3 | `qqbot-remind.schedule` | 定时提醒（cron jobs） | ✅ 实装 |

待实装 5 个（**回来后先修 TD-002，再做这 5 个**）：

| # | 工具 | 用途 | 备注 |
|---|---|---|---|
| 4 | `qqbot-channel.receive_reply` | 接收用户 QQ 回复，进入 Scout 消息队列 | 需要 webhook 回调 |
| 5 | `qqbot-media.upload_image` | 推送带图消息（如周报缩略图） | 依赖 image-server |
| 6 | `qqbot-media.upload_file` | 推送文件（如 markdown 报告） | |
| 7 | `qqbot-channel.batch_send` | 批量推送（多用户或多消息） | rate limit 集成 |
| 8 | `qqbot-channel.query_status` | 查 QQ bot 状态（在线/离线/token 过期）| Dashboard 要用 |

**预期工作量**：5 个工具 × 约 0.5~1 天 = **3~5 天**

### B.2 Phase 2B 其余目标（QQ 插件之后）

按优先级：

1. **event_chains 事件串**（3 天）
   - 新文件 `agents/event_chain_agent.py`
   - 依赖 `event_chains` 表 + `info_units.event_chain_id`（v1.59 已预留字段）
   - 目的：跨 info_unit 串联同一主题事件（例："工信部新规 → 企业合规成本 → 利空某股"）

2. **related_stocks 填充自动化**（2 天）
   - `scripts/load_v105_related_stocks.py` → Agent 化
   - 依赖 akshare + pykrx + yfinance
   - 目的：新增行业时自动拉取相关标的（三地）

3. **外部推送通道**（4 天）
   - `infra/push_channel_telegram.py` / `push_channel_email.py` / `push_channel_openclaw.py`
   - 事件总线 `infra/event_bus.py`（观察者模式）
   - 与 QQ 通道并行的备用推送

4. **suppress v2**（延伸自 TD-002，1.5 天）
   - `SUPPRESSED_ERRORS` 迁移到 DB 表 `alert_suppressions`
   - CLI：`python main.py suppress add/list/remove`
   - 热更新，不用改代码重启

### B.3 Phase 2B 总工作量

- QQ 插件 5 工具：**3~5 天**
- 核心 4 项（event_chains / related_stocks / 外部推送 / suppress v2）：**10.5 天**
- 测试 + 文档：**+2 天**
- 缓冲 20%：**+3 天**
- **合计约 18~20 工作日（3~4 周）**

### B.4 Phase 2B Entry Points

见 [CLAUDE.md §6](../CLAUDE.md) 的表格，Phase 2B 行已标出。

---

## 回来后执行顺序（2026-04-25 起）

**顺序**（严格按此执行）：

1. **Day 1（回家当天）** — 修 [TD-002 suppress 逻辑](Scout_技术债务清单.md#td-002)
   - 先定位 QQ 告警的实际推送来源（HealthMonitorAgent vs Watchdog）
   - 根据来源修对应路径 + 加参数化测试

2. **Day 1 晚** — 提交 [TD-003 SemanticScholar API Key 申请](Scout_技术债务清单.md#td-003)（只要 10 分钟，提交后等待审批）

3. **Day 2 ~ Day 5** — Phase 2B QQ 插件剩余 5 工具（按 B.1 表顺序）

4. **Day 6 ~ Day 9** — Phase 2B event_chains（B.2 第 1 项）

5. **Day 10 ~ Day 11** — Phase 2B related_stocks 自动化（B.2 第 2 项）

6. **Day 12 ~ Day 15** — Phase 2B 外部推送通道（B.2 第 3 项）

7. **Day 16 ~ Day 17** — Phase 2B suppress v2（B.2 第 4 项；此时 TD-002 已修好，做 v2 是把临时方案升级为可配置版本）

**期间持续**：TD-004 collect_V3 观察（2 周）/ TD-005 direction_backfill 偶发观察。

---

## Phase 3（📅 Q3 2026 预研）

- **纠偏/纠错模块**：对历史推荐回溯 —"哪些方向判断错了、为什么错" → 学习闭环
- **认知偏误检查**（见 `Desktop/Scout/认知偏误检查模块设计文档.md`）
- **大师模块**（见 `Desktop/Scout/大师模块设计文档.md`）：用巴菲特/林奇/Druckenmiller 思想流派做交叉验证

---

## 不改动的边界（再次重申）

❌ 自动交易
❌ 价格预测 / 点位预测
❌ 情绪识别 / 社交媒体抓取
❌ 加密货币
❌ `asyncio.run` 嵌入热路径
❌ 明文 API key 进 DB / config.yaml
❌ MCP 工具内 raise 异常

（详见 [CLAUDE.md §7](../CLAUDE.md)）
