# AGENTS.md — Scout 项目记忆

Codex 在本仓库的长期记忆。优先读这份。

---

## 1. 项目概述（3 句）

Scout 是一个**多信源金融信号系统**，覆盖中国（A股）/韩国（KOSPI）/美国（NASDAQ）三地市场，为长期价值投资者（3-5 年持有）提供数据采集、信号抽取、行业监测、周报生成和外部 LLM 接入（MCP）。

**不做投资决策**；决策永远是用户的。Scout 负责让用户"更少错过、更少情绪化"。

**技术栈**：Python 3.12 + SQLite + Ollama 本地 Gemma 3 + MCP (stdio) + APScheduler。

---

## 2. 当前阶段

**Phase 2A 收尾中**（2026-04-18，v1.12 PushConsumerAgent 完成）

- 14/14 Phase 1 验收项通过（见 `scripts/phase1_acceptance.py`）
- **1424 passing tests**（Phase 1 基线 1031，v1.12 新增 42 + 9 = 51 + 其他 Phase 2A 增量）
- 真实数据：15 行业 + 5 条投资原则 + user_context 已录入 `data/knowledge.db`

**Phase 2A 已交付**：

- **v1.01** — FinancialAgent（Z''-Score 1995 公式，distress<1.81 → push）+ MasterAgent
- **v1.07** — RecommendationAgent（规则 gate + Sonnet LLM，A≥75/B 60-74/candidate 40-59/reject<40）
- **v1.08** — MotivationDriftAgent（180 天周期，reversing/drifting/stable）
- **v1.10** — DirectionBackfillAgent（历史方向回填）
- **v1.11** — V3 Playwright 激活（韩国关税厅 10대 품목 YTD）
- **v1.12** — PushConsumerAgent + `get_pending_messages`/`mark_read` MCP 工具 + 生产者接线（4 个 agent → push_outbox）

**下一步 → Phase 2B**（见 `docs/Scout操作手册_Phase1.md §12`）

**Phase 2B 目标**：event_chains 事件串、related_stocks 填充自动化、外部推送通道（Telegram / 邮件 / OpenClaw webhook consumer）、事件总线抽象。

---

## 3. 关键架构决策

| 决策 | 原因 | 文件 |
|---|---|---|
| **UTC 存储 + KST 显示** | 多市场时区需要统一；KST 是用户首要时区 | `utils/time_utils.py` |
| **Pydantic v2 + `extra='forbid'`** | 契约漂移立即可见（异常可见性原则）| `contracts/contracts.py` |
| **BaseAgent 6 类错误矩阵** | network/parse/llm/rule/data/unknown — 每类独立处理 | `agents/base.py` |
| **SQLite WAL + BEGIN IMMEDIATE** | 单机高并发；读写隔离靠 `read_snapshot` 时间戳 | `infra/db_manager.py` |
| **Gemma 3 本地（gemma4:e4b）** | 零成本；Phase 1 不用 Anthropic API | `agents/signal_collector.py` |
| **规则优先 + Gemma 辅助（v1.59）** | RESTRICTIVE_HARD 硬字面量覆盖 LLM；confidence<0.7 → null | `agents/signal_collector.py::_combine` |
| **MCP stdio 对外接口** | Codex Desktop / OpenClaw 标准协议 | `infra/mcp_server.py` |
| **推送队列独立 DB（queue.db）** | 避免 knowledge.db 锁冲突 | `knowledge/init_queue_db.py` |
| **asyncio + APScheduler (AsyncIOScheduler)** | 单进程多任务；I/O 在线程池 | `main.py::ScoutRunner` |
| **reports/ 落盘 + push_outbox 推送 preview** | 完整报告可复查；推送只带 2000 字预览避免消息过大 | `agents/direction_judge.py::_maybe_push_weekly_report` |
| **PushConsumerAgent 拉取模式 + MCP 主通道（v1.12）** | Phase 2A 不做外部推送通道；消费者过期清理（>14d）+ 同类限流（1h ≤3 条）+ 09:30 KST 每日摘要 | `agents/push_consumer_agent.py` |

---

## 4. 核心约束（违反即破坏系统不变量）

1. **时间字段永远 UTC ISO 8601 with +00:00 offset**。Pydantic 校验强制。
2. **所有 Agent 走 `run_with_error_handling`**。直接抛未分类异常会 re-raise（fail-loud）。
3. **UnitV1.id 是幂等哈希** = `hash(source+title+published_date)[:16]`。不手动生成。
4. **`mixed` 方向必须带 `mixed_subtype`**（contract validator 强制）。非 mixed 时 subtype 必须 None。
5. **SignalCollector 规则硬覆盖**：文本含 `禁止/取缔/不得/限制/整改/严禁` → direction 强制 restrictive，不论 Gemma 说什么。
6. **`SCOUT_DB_PATH` env 可覆盖 DB 路径**（测试/生产切换），优先级：`--db` > env > config.yaml。
7. **reports/ 文件名含 KST 日期**（`weekly_industry_YYYYMMDD.md`），同日重跑覆盖（幂等）。
8. **push_queue.entity_key 去重**（目前 SELECT-before-INSERT；同日同类消息只留一份）。
9. **Phase 1 不用 Anthropic API**；`config.llm.api_key_required: false`，`cloud_model` 是占位符。
10. **V3 韩国关税厅 v1.11 起由 Playwright 激活**。`infra/data_adapters/korea_customs_playwright.py` 是主路径，旧 HTTP 版（`korea_customs.py`）作 fallback。需先 `python -m playwright install chromium`。采 10대 품目 YTD 排名，过滤 HS2∈{85,84,87,89,90} → 生成 `source='V3'` 的 `info_units`，d4 维度被激活。

---

## 5. 重要技术债务

Phase 2A 建议一并处理：

- [ ] **Ollama helper 代码重复**：`SignalCollector` 和 `DirectionJudge` 各自实现了 `_call_gemma_text/_extract_content/_extract_tokens/_is_connection_error`。应抽到 `agents/_ollama.py`。不在 Phase 1 做是因为怕破坏已通过的 Step 7/9 测试。
- [ ] **缺 `__init__.py`**：`infra/`、`agents/`、`utils/`、`contracts/` 都没有。`python -m infra.mcp_server` 走不通；靠 `python infra/mcp_server.py` + sys.path 补丁。
- [ ] **`watchlist.notes` 是 composite 字符串**（`[market=CN+KR+US] [subs=...] 原始备注`）。Phase 2A 应把 `primary_market` 和 `sub_industries` 拆到专用列。
- [ ] **`queue.db` 没有 `UNIQUE(queue_name, entity_key)` 部分索引**。`PushQueue.push` 用 SELECT-before-INSERT 保证幂等，单生产者场景下 OK，多生产者有 race window。
- [ ] **`watchlist.zone` 没 CHECK 约束**。当前接受 `active/cold/cycle_bottom/observation/observe_new_direction`，但 "observation" 与 "observe_new_direction" 是同义。应统一。
- [ ] **CLI process 子命令每消息一次 `asyncio.run`**。批量处理时开销大。应改成单 event loop。
- [ ] **Windows SIGTERM 有限支持**。`main.py` 静默跳过 SIGTERM handler 注册。生产部署靠 `--max-runtime-seconds` + 外部重启。
- [ ] **Ollama 模型名约定**：`config.yaml` 和代码 default 写死 `gemma4:e4b`（Ollama 命名），而用户文档 spec 里是 `gemma-4-e4b`（dash 形式）。保持一致性要更新 cloud LLM env。

---

## 6. Phase 2A 开发 Entry Points

新功能建议的起点文件：

| 目标 | 新文件 | 依赖 |
|---|---|---|
| ~~推荐规则 + LLM 层~~ | ✅ v1.07 已完成 `agents/recommendation_agent.py` | `anthropic` SDK + watchlist |
| ~~V3 Playwright~~ | ✅ v1.11 已完成 `infra/data_adapters/korea_customs_playwright.py` | `playwright` + `chromium` |
| ~~财务一体化~~ | ✅ v1.01 已完成 `agents/financial_agent.py` | `akshare` + `stock_financials` 表 |
| ~~动机漂移检测~~ | ✅ v1.08 已完成 `agents/motivation_drift_agent.py` | `motivation_drift_log` 表 |
| ~~推送 consumer~~ | ✅ v1.12 已完成 `agents/push_consumer_agent.py`（MCP 拉取模式；外部通道 Phase 2B） | `message_queue` + PushConsumerAgent |
| 事件串 | `agents/event_chain_agent.py` | `event_chains` 表，`info_units.event_chain_id` |
| 外部推送通道 | `infra/push_channel_*.py` | Telegram / 邮件 / OpenClaw webhook |

**修改现有文件**的潜在改动：

- `main.py::ScoutRunner.schedule_all()` — 加新 Agent 的调度
- `infra/mcp_server.py::ScoutToolImpl` — 加新工具（如 `get_recommendations`）
- `contracts/contracts.py` — 加 `RecommendationV1` Pydantic 契约
- `config.yaml` — 启用 `api_key_required: true`，配 `ANTHROPIC_API_KEY`

---

## 7. 不做的事（显式否定清单）

- ❌ **自动交易**。Scout 不发单、不调用券商 API。
- ❌ **价格预测 / 点位预测**。方向判断（supportive/restrictive/mixed/null）而非 price target。
- ❌ **情绪识别 / 社交媒体**。Phase 1-3 不做 X / 雪球 / 微博抓取。
- ❌ **加密货币**。产业列表封闭在传统行业。
- ❌ **把 `asyncio.run` 嵌入热路径**。目前只在 CLI one-shot 用。
- ❌ **存明文 API key 到 DB 或 config.yaml**。走 env（`ANTHROPIC_API_KEY`）。
- ❌ **在 MCP 工具内 raise 异常**。全部降级为 `{"ok": False, "error": "..."}`。
- ❌ **改 Step 1-13 已通过的测试去迁就 Step N 的新功能**。加新测试，旧测试尽量保留。

---

## 8. 开发工作流

### 8.1 新 Agent 模板

```python
from agents.base import BaseAgent, RuleViolation, LLMError
from infra.db_manager import DatabaseManager

class MyAgent(BaseAgent):
    def __init__(self, db: DatabaseManager, ...):
        super().__init__(name="my_agent", db=db)
        # ...

    def run(self, *args, **kwargs):
        return self.run_with_error_handling(self._process, *args, **kwargs)

    def _process(self, ...) -> Any:
        # raise RuleViolation / LLMError / etc. for typed errors
        # 抛 unknown → re-raised by BaseAgent
        ...
```

### 8.2 新 MCP 工具

`infra/mcp_server.py::ScoutToolImpl` 加方法；`build_server` 加 `@app.tool(description=...)` wrapper。工具返回 `{"ok": bool, ...}`；失败降级不 raise。

### 8.3 新测试命名

- Unit：`tests/test_<module>.py::TestXxx::test_...`
- 真网络烟囱：`scripts/test_<module>_real.py`

### 8.4 Commit / PR

Phase 1 未启用 PR 流程；单人开发。如启用：每步一 commit，附 pytest 通过数 + 新增/修改文件清单。

---

## 9. 运行命令速查

```bash
# 日常
python main.py status                            # 系统状态
python main.py serve                             # 长跑调度器
python main.py collect --source D1 --days 7      # 手动采集
python main.py report --type industry            # 手动生成周报
python main.py mcp                               # MCP stdio server

# 冷启动（一次性）
python scripts/cold_start.py --yes               # 录入 15 行业 + 5 原则

# 验收
python scripts/phase1_acceptance.py              # 14 项验收
pytest tests/ -q                                 # 全量测试

# 真网络烟囱（按需）
python scripts/test_govcn_real.py --keyword 半导体
python scripts/test_direction_judge_real.py
python scripts/test_mcp_real.py
```

---

## 10. 坐标系速查

- **项目根**：`D:\13700F\Scout`
- **Python**：`C:\Users\13700F\AppData\Local\Programs\Python\Python312\python.exe`
- **Ollama host**：`http://localhost:11434`
- **Ollama 模型**：`gemma4:e4b`（Gemma 3 4B）
- **timezone**：存储 UTC，显示 KST (Asia/Seoul)
- **主 DB**：`data/knowledge.db`（20 张表）
- **队列 DB**：`data/queue.db`（独立 WAL）
- **日志**：`logs/scout.log`（按天滚动）、`logs/mcp_access.log`（MCP 调用）
- **周报**：`reports/weekly_{industry,paper}_YYYYMMDD.md`

---

**最后更新**：2026-04-18 · v1.12-push-consumer 发布  
**版本对应**：scout_version="v1.12"（Phase 2A 收尾 — PushConsumerAgent + MCP get_pending_messages/mark_read 上线）
