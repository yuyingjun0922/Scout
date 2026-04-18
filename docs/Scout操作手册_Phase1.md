# Scout 操作手册 — Phase 1

Scout 是你的金融信号系统。Phase 1 = 数据采集 + 信号抽取 + 行业 dashboard + 周报 + 对外接口（MCP）。
**不做投资决策** —— 决策永远是你的。Scout 帮你看见、归类、记忆，让你更少错过、更少情绪化。

**当前阶段**：Phase 2A 收尾中（v1.12 PushConsumerAgent 已完成 2026-04-18）  
**下一步**：Phase 2B — DirectionJudge Sonnet 升级 + 动机漂移 180d + 事件串

---

## 1 快速开始

```bash
# 首次安装
pip install -r requirements.txt
python knowledge/init_db.py
python knowledge/init_queue_db.py

# 冷启动（录入 15 行业 + 5 条原则 + 上下文）
python scripts/cold_start.py --yes

# 查状态（应看到 watchlist active/total = 14/15）
python main.py status

# 拉一次 D1 政策（需联网）
python main.py collect --source D1 --days 7

# 生成本周行业周报（需 Ollama 运行 gemma4:e4b）
python main.py report --type industry
```

顺利 = 你已经可以把 Scout 接入 Claude Desktop（见 §8）开始用。

---

## 2 Scout 是什么（& 不是什么）

**Scout 是**：

- 多信源采集（中国国务院 D1 / arXiv+S2 D4 / 国家统计局 V1 / 韩国关税厅 V3[阻塞] / AkShare S4）
- 本地 LLM（Gemma 3）对政策文本的结构化信号抽取（policy_direction / mixed_subtype 等）
- 行业 dashboard（按信源 / 方向 / 可信度 / 周密度 聚合）
- 周度报告（行业 + 论文，Markdown 格式，Gemma 负责总结段）
- MCP 接口（让 Claude Desktop 可读写 Scout 数据）
- 推送队列（推送给外部消费者如 OpenClaw，按优先级排序）

**Scout 不是**：

- 股票推荐系统（Phase 2A 才做）
- 自动交易（永远不做）
- 价格预测（不做）
- 情绪识别 / 社交媒体抓取（Phase 1 不做）

---

## 3 命令速查

### 主入口：`python main.py <subcmd>`

| 命令 | 作用 |
|---|---|
| `python main.py serve` | 长跑模式：调度器 + 消费循环 |
| `python main.py serve --max-runtime-seconds 3600` | 跑 1 小时后自动优雅关闭 |
| `python main.py mcp` | MCP stdio server（供 Claude Desktop / OpenClaw 接入）|
| `python main.py collect --source D1 --days 7` | 手动采集 D1（或 D4/V1/V3/S4）|
| `python main.py process` | 手动消费 `collection_to_knowledge` 队列 |
| `python main.py report --type industry` | 手动生成行业周报 |
| `python main.py report --type paper` | 手动生成论文周报 |
| `python main.py report --type industry --no-gemma` | 无 AI 分析，纯数据周报 |
| `python main.py status` | 系统状态快照（数据/成本/错误/健康度） |

### 配套脚本：`python scripts/<file>.py`

| 脚本 | 作用 |
|---|---|
| `cold_start.py --yes` | 冷启动录入（行业 + 原则 + 上下文）|
| `cold_start.py --interactive` | 交互式补填 holdings / principles |
| `phase1_acceptance.py` | 14 项验收矩阵 |
| `test_mcp_real.py` | MCP stdio 真烟囱（调 10 工具）|
| `test_govcn_real.py --keyword 半导体` | gov.cn 真采集烟囱 |
| `test_direction_judge_real.py` | DirectionJudge + Gemma 真生成 |

---

## 4 每日流程

### 4.1 早（07:30 KST）— 看简报

如果 `python main.py serve` 长跑中：

- 系统自动生成 daily_briefing，推到 `push_outbox`
- 你用 Claude Desktop + MCP 问："用 scout 工具看今天的简报"
- Claude Desktop 会调 `get_latest_weekly_report` 或 `search_signals`

手动触发（不在长跑模式）：
```bash
python main.py status
python main.py collect --source D1 --days 1   # 拉昨日新政策
```

### 4.2 中（盘前/盘中）— 检查重要信号

```bash
# 查半导体近 30 天分布
python main.py status

# 在 Claude Desktop：
# "用 scout 问半导体这周的方向分布"
# → 触发 ask_industry("半导体")
```

**红色信号**（要停下来想）：

- 某行业 direction distribution 突然出现 restrictive
- `agent_errors` 连续 3 次同 source 失败（V3 阻塞除外）
- 某行业 `data_freshness.newest_signal_days_ago > 30`

### 4.3 晚（21:00 KST）— 复盘

- 检查今日 `info_units` 新增数量（应有 D1 / S4 至少）
- 若计划明日交易 → 跑 L4 决策辅助（§7）
- **硬规则**：当日决定的交易至少压一晚再执行

---

## 5 每周流程

### 5.1 周一（07:00 KST）— 行业周报

自动（长跑模式）：系统写 `reports/weekly_industry_YYYYMMDD.md`，推到 `push_outbox`。

手动：
```bash
python main.py report --type industry
# 输出在 reports/weekly_industry_<日期>.md
```

周报包含每个 active 行业的：

- 信号总数 + 按信源分布
- 政策方向分布（supportive/restrictive/neutral/mixed/null）
- 可信度分布（权威/可靠/参考/线索）
- 数据新鲜度（最老/最新/4周密度）
- Watchlist 状态（zone/dimensions/verification/gap）
- 最新 5 条信号（按"方向重要度"排序：restrictive > supportive > mixed > neutral > null）
- AI 分析段（Gemma 3 本地 token，零成本，约 3000 token/行业）

### 5.2 周日（09:00 KST）— 论文周报

```bash
python main.py report --type paper
```

本周 D4（arXiv + Semantic Scholar）Top N 论文按引用数排序，AI 总结主题 + 重点论文 + 跨主题关联。

### 5.3 周末（任选）— 原则回看

```bash
sqlite3 data/knowledge.db "SELECT value FROM system_meta WHERE key='user_principles'" | jq
```

把 P1-P5 过一遍，问自己本周有没有哪条被突破。违反 = 纪律警报。

---

## 6 必做清单

### 6.1 每次下决策前（P1 异常可见性）

- [ ] 我现在的买/卖理由是什么？一句话能说清
- [ ] 这个理由里有多少来自 Scout，多少来自直觉？
- [ ] Scout 没看到哪些我知道的信号？
- [ ] 这个行业的 `policy_direction distribution` 是什么？
- [ ] 最新信号距今多少天？超过 30 天警觉
- [ ] 这条决定的假设可以 180 天后验证吗？

### 6.2 每月回看持仓（P1 异常可见性）

- [ ] 每只持仓当时买入的假设还成立吗？
- [ ] 如果不知道成立与否，**默认假设已改变**
- [ ] 超过 3 条假设不明 → 减仓或全清
- [ ] 持仓涨了但我说不清为什么 = 危险信号

### 6.3 买入前必做（v1.68 6.1.1）

- [ ] 查该行业 MCP 全景：Claude Desktop 问 "scout.get_industry_full_context('行业名')"
- [ ] 跑 L4（§7）
- [ ] 记 decision reasoning 到笔记（不记 = 不买）
- [ ] 分批建仓计划（P5 时间纪律：3-5 次，间隔 ≥ 1 个月）
- [ ] 写下硬卖出条件（kill_conditions）

### 6.4 卖出前必做

- [ ] 触发的是哪条 kill_condition？具体引用
- [ ] 对照 P2："纪律>灵活"—— 我是在跟随纪律还是情绪？
- [ ] 如果是"浮盈想锁定" → 违反 P5（时间纪律）→ 不卖
- [ ] 如果是 fundamental 变化 → 记原因，卖

---

## 7 L4 决策辅助（v1.68 10.4）

**L4 = 买入前最深入的一次核查**。Phase 1 Scout 不自动执行，由你手动走：

### 7.1 Pre-mortem（P2 强制）

写 **3 个失败场景**：1 年后这次买入变成错误决定，是因为什么？

1. 行业动机变化（如国家安全推动消退、政策转向）
2. 竞争格局变化（新进入者、替代技术、客户迁移）
3. 个股问题（管理层、诉讼、财务）

每条失败场景指定 **leading indicator**（多早能看到）+ **该指标如何监控**（Scout 哪个 source？）。

### 7.2 跨市场交叉验证（Scout 独特价值）

- 该行业在 A 市场 direction 分布？
- 韩国同类行业 direction 分布？
- 美国同类公司最近新闻？
- 3 市场方向一致 → 强化；不一致 → 区域性风险

```bash
# Claude Desktop：
# "scout.ask_industry('半导体设备', days=30) 然后对比 HBM"
```

### 7.3 动机深度（v1.68 10.4）

- 主导动机是哪条（1_国家安全 / 2_技术主权 / 3_社会稳定 / 4_经济转型 / 窗口博弈 / 地缘受益）？
- 这条动机的半衰期估计？
- Scout 里这个行业最近 90 天有哪些信号强化/削弱了该动机？

### 7.4 容错检查

- 最多能承受多少浮亏？（%）
- 多久不回本会触发卖出？（月）
- 如果最糟糕 30% 亏损发生，整体组合影响？

**写完这 4 节 = 决策已充分思考**。写不完 = 还没准备好，晚 1 天。

---

## 8 Scout + Claude Desktop 协作

### 8.1 MCP 配置（一次性）

`%APPDATA%\Claude\claude_desktop_config.json`（Windows）：

```json
{
  "mcpServers": {
    "scout": {
      "command": "C:/Users/13700F/AppData/Local/Programs/Python/Python312/python.exe",
      "args": [
        "-u",
        "D:/13700F/Scout/infra/mcp_server.py"
      ],
      "env": {
        "PYTHONIOENCODING": "utf-8",
        "SCOUT_DB_PATH": "D:/13700F/Scout/data/knowledge.db"
      }
    }
  }
}
```

重启 Claude Desktop，在任何对话里说："用 scout 工具查半导体"。

### 8.2 10 个可用工具

| 工具 | 用途 | 何时用 |
|---|---|---|
| `get_watchlist` | 列 active 行业 | 不记得自己在看什么时 |
| `ask_industry` | 单行业 dashboard | 每日查状态 |
| `get_system_status` | 系统健康度 | 怀疑数据延迟 |
| `search_signals` | 关键词搜索 info_units | 想查某政策/论文 |
| `get_latest_weekly_report` | 读最新周报 | 没时间看长报告 |
| `add_industry` / `remove_industry` | 调整 watchlist | 新发现/失焦点 |
| `get_industry_full_context` | 行业全景（LLM 深度分析用）| 跑 L4 前 |
| `get_decision_context` | 个股上下文（Phase 1 简化）| 查某股历史 |
| `get_policy_for_motivation_analysis` | 政策原文+类似政策 | 分析某政策时 |

详细：[docs/mcp_integration.md](mcp_integration.md)

### 8.3 典型提问

- **"用 scout 查半导体行业这周的信号分布"** → `ask_industry`
- **"scout 里最近有什么涉及新能源的政策"** → `search_signals`
- **"scout.get_industry_full_context('半导体设备')，然后对比 HBM"** → 跑 L4 前的深度看
- **"用 scout 查一下系统状态"** → `get_system_status`

---

## 9 冷启动数据说明

Phase 1 已经为你在 `data/knowledge.db` 录入：

### 9.1 15 个关注行业（`watchlist` 表）

| 大类 | 行业 | Market | Zone |
|---|---|---|---|
| A 核心 | 半导体设备, HBM, 医疗器械国产替代, 工业自动化 | CN+KR+US 混合 | active |
| B AI+算力 | AI算力, AI应用软件, 数据中心配套 | US 主 | active |
| C 能源转型 | 核电, 特高压, 储能细分 | CN 主 | active |
| D 韩国特色 | 造船海工, 韩国电池 | KR | active |
| E 结构性 | 军工, 创新药, 人形机器人 | 混合 | 14 active + **人形机器人 observation** |

### 9.2 5 条投资原则（`system_meta.user_principles`）

| ID | 标题 | 核心 |
|---|---|---|
| **P1** | 异常可见性优先于执行速度 | 知道出问题比解决问题更重要 |
| **P2** | 结构>智能，纪律>灵活 | 规则能做的不依赖 LLM/直觉 |
| **P3** | 聚焦独特价值，拒绝广度 | 在真正懂的领域深耕 |
| **P4** | 工具+记忆互补，不替代判断 | 数据不等于洞察 |
| **P5** | 时间是朋友不是敌人 | 3-5 年持有期让你容错 |

完整版（含 application + warnings）：
```bash
sqlite3 data/knowledge.db "SELECT value FROM system_meta WHERE key='user_principles'" | python -m json.tool
```

### 9.3 用户上下文（`system_meta.user_context`）

Scout 和 Claude Desktop 都会读这个：

- `investor_type`: 长期价值投资者
- `capital_range`: ₩1-3 亿
- `holding_horizon`: 3-5 年
- `markets`: [A, KR, US]
- `phase`: cold_start

更新：编辑 `scripts/cold_start_config.yaml` 的 `user_context:` 节，`python scripts/cold_start.py --yes`。

---

## 10 故障排除

### 10.1 Ollama 不可达 / Gemma 推理失败

症状：周报 / SignalCollector 报错，`agent_errors.error_type='data'`
```bash
# 检查 Ollama
curl http://localhost:11434/api/tags

# 启动
ollama serve &

# 拉模型
ollama pull gemma4:e4b
```

若仍不行：`python main.py report --type industry --no-gemma` 先出无 AI 的版本，不阻塞日常流程。

### 10.2 V3 韩国关税厅一直失败

**这是预期**。V3 是 JS 渲染页，Phase 1 的 HTTP 抓取不了。Phase 2A 会加 Playwright。
忽略 `agent_errors.error_type='parse'`（V3 相关）即可。

### 10.3 cold_start 重跑重复插入？

不会。`cold_start.py` 的 UPSERT 语义：

- `watchlist`：同 industry_name → UPDATE
- `system_meta`（user_principles / user_context）：INSERT OR REPLACE
- `track_list`：同 stock → UPDATE

### 10.4 `python main.py status` 显示 RED

检查原因（status 输出的 reason 字段）：
- `any_source_collected=False` → 至少跑一次 `collect --source D1`
- `push_failed >= 5` → 查 queue.db 的 `failed` 消息
- `errors_7d >= 20` → 查 `agent_errors` 看 error_type 分布

### 10.5 数据库锁定 / WAL 问题

```bash
# 看 WAL 文件
ls data/*.db-wal data/*.db-shm

# 若某进程死锁（极罕见）
rm data/knowledge.db-wal data/knowledge.db-shm
sqlite3 data/knowledge.db "PRAGMA journal_mode=WAL; PRAGMA wal_checkpoint(TRUNCATE);"
```

---

## 11 Phase 1 当前限制

| 限制 | 描述 | 解法 |
|---|---|---|
| V3 阻塞 | 韩国关税厅 UniPass 是 JS 渲染页，HTTP 抓不到 | Phase 2A 上 Playwright / data.go.kr API |
| ~~V3 阻塞~~ | v1.11 Playwright 方案已激活（2026-04-18） | ✅ 完成 |
| ~~无推荐 Agent~~ | v1.07 RecommendationAgent 4 阶段混合制 | ✅ 完成 |
| ~~无财务一体化~~ | v1.01 FinancialAgent (Z''-1995 + PEG) 周刷新 | ✅ 完成 |
| 无动机漂移检测 | v1.08 MotivationDriftAgent 激活 4 信号检测 | ✅ 完成；Phase 2B 升级 180d 周期 |
| 无事件串关联 | `event_chain_id` 预留字段未使用 | Phase 2C 启用 |
| DirectionJudge 简化版 | 只做周报总结 + 基础投票；无深度交叉验证 | Phase 2B 升级 Sonnet 版 |
| ~~推送只到 push_outbox~~ | v1.12 PushConsumerAgent 负责清理+频控+daily digest+MCP 拉取 | ✅ 完成；Phase 2B 加真送达 consumer |

---

## 12 Phase 2A 收尾（v1.12 完成状态）

Phase 2A 在 v1.07–v1.12 期间分批落地。目前（2026-04-18, v1.12）：

**已完成**：

1. ✅ **推荐 Agent**（v1.07）：`agents/recommendation_agent.py` 四阶段混合制（硬底线+6维打分+综合验证+级别）
2. ✅ **V3 Playwright**（v1.11）：`infra/data_adapters/korea_customs_playwright.py` 激活韩国关税厅真采集
3. ✅ **股票财务一体化**（v1.01/v1.03）：`agents/financial_agent.py` 周刷新 `stock_financials`（Z''-1995 + PEG）；`agents/master_agent.py` 5 大师评分
4. ✅ **motivation_detail**（v1.08）：`agents/motivation_drift_agent.py` 四信号检测（政策/关键词/财务/冲突）
5. ✅ **推送 consumer**（v1.12）：`agents/push_consumer_agent.py` —— 框架就位（过期清理+频率压制+daily digest+MCP 拉取）；真外部通道（邮件/Telegram/OpenClaw）留给 Phase 2B

**v1.12 新增调度**（`main.py::ScoutRunner.schedule_all`）：

- `push_consumer_scan`：每小时扫 push_outbox（>14d → expired；同类型 1h 内 >3 条 → rate_limit）
- `push_consumer_digest`：09:30 KST 把 blue/white pending 汇总成一条 daily_briefing
- `job_recommend_batch`：A 级红色立即推、B 级蓝色日常、bias ≥3 条黄色 alert
- `job_motivation_drift`：reversing 红色立即、drifting 蓝色日常
- `job_financial_refresh`：Z''<1.81 红色 alert（困境区，建议复核）

**v1.12 新增 MCP 工具**（共 16 个工具）：

- `get_pending_messages(priority='all'|'urgent'|'normal', max=50)`
- `mark_read(event_id)`

**未来工作（Phase 2B+）**：

- `agents/recommend_llm_agent.py` — 当前 v1.07 是规则层，Phase 2B 加 Sonnet 深度评估层
- `infra/push_consumer_external.py` — 真邮件/Telegram/OpenClaw 送达 worker
- `motivation_drift_agent.py` → 180d 长周期 + event_chain_id 关联

**技术债清单**（Phase 2A 建议一并处理）：

- [ ] `agents/_ollama.py` 抽取（SignalCollector 和 DirectionJudge 的 Ollama helper 重复了）
- [ ] 加 `__init__.py` 让 `python -m infra.mcp_server` 可用
- [ ] `notes` 字段结构化拆 `primary_market` / `sub_industries` 到专用列
- [ ] `CHECK` 约束加 `watchlist.zone ENUM`
- [ ] `queue.db` 加 `UNIQUE(queue_name, entity_key)` 部分索引（目前靠 SELECT-before-INSERT）
- [ ] 增加 `tests/test_concurrency.py` 压测多客户端并发

---

## 附录 A：目录结构

```
Scout/
├── main.py                          # CLI 入口（serve/mcp/collect/process/report/status）
├── config.yaml                      # 全局配置
├── requirements.txt
├── data/
│   ├── knowledge.db                 # 主数据库（20 张表）
│   └── queue.db                     # 队列数据库
├── reports/                         # 周报 Markdown（按日期命名）
├── logs/                            # scout.log / mcp_access.log
├── agents/
│   ├── base.py                      # BaseAgent + 错误矩阵
│   ├── signal_collector.py          # Step 7：Gemma 信号抽取
│   └── direction_judge.py           # Step 9：方向判断 Agent（简化版）
├── infra/
│   ├── db_manager.py                # SQLite 并发控制（v1.57 决策 6）
│   ├── queue_manager.py             # 消息队列（Step 6）
│   ├── push_queue.py                # 推送队列（Step 11）
│   ├── dashboard.py                 # 行业 dashboard（Step 8）
│   ├── mcp_server.py                # MCP stdio server（Step 10）
│   └── data_adapters/
│       ├── akshare_wrapper.py       # S4
│       ├── arxiv_semantic.py        # D4
│       ├── nbs.py                   # V1
│       ├── korea_customs.py         # V3（Phase 1 阻塞）
│       └── gov_cn.py                # D1
├── knowledge/
│   ├── init_db.py                   # 20 张表 schema
│   └── init_queue_db.py             # message_queue schema
├── contracts/
│   └── contracts.py                 # Pydantic v2 契约
├── prompts/
│   ├── signal_collector_v001.md     # Step 7 prompt
│   ├── direction_judge_weekly_v001.md   # Step 9 行业周报
│   ├── direction_judge_paper_v001.md    # Step 9 论文周报
│   └── CHANGELOG.md
├── scripts/
│   ├── cold_start.py                # Step 13 录入
│   ├── cold_start_config.yaml       # 15 行业 + 5 原则 + user_context
│   ├── phase1_acceptance.py         # Step 14 验收矩阵
│   └── test_*_real.py               # 各步真网络烟囱
├── tests/                           # 1031 个测试
├── docs/
│   ├── Scout操作手册_Phase1.md       # 本文
│   └── mcp_integration.md           # MCP 接入指南
├── utils/
│   ├── time_utils.py
│   └── hash_utils.py
└── config/
    └── loader.py
```

## 附录 B：关键配置（`config.yaml`）

```yaml
llm:
  local_model: gemma4:e4b                 # Ollama 本地 Gemma 3
  cloud_model: claude-sonnet-4-6          # Phase 2A 才启用
  phase1_mode: gemma_only
  api_key_required: false                 # Phase 1 不用 Anthropic API

sources:
  D1: { name: 国务院, frequency_hours: 6,  credibility: 权威 }
  D4: { name: Semantic Scholar, frequency_hours: 24, credibility: 参考 }
  V1: { name: 国家统计局, frequency_hours: 24, credibility: 权威 }
  V3: { name: 韩国关税厅, frequency_hours: 24, credibility: 权威 }
  S4: { name: AkShare, frequency_hours: 12, credibility: 权威 }

database:
  knowledge_db: data/knowledge.db
  queue_db: data/queue.db

timezone: Asia/Seoul                       # 显示时区；存储永远 UTC
mode: cold_start                           # cold_start / running / diagnosis
```

---

**文档版本**：Phase 1 Final · 2026-04-18  
**对应代码**：1031 tests passing · 14/14 acceptance pass  
**维护者**：Scout 作者 + Claude Code
