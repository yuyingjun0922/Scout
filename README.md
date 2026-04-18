# Scout

**多信源金融信号系统 · Phase 2A 完成（2026-04-18）**

覆盖中国 / 韩国 / 美国三地市场，为长期价值投资者（3-5 年持有）提供：

- 多信源数据采集（国务院 D1 / arXiv + Semantic Scholar D4 / 国家统计局 V1 / 韩国关税厅 V3[Phase 1 阻塞] / AkShare S4）
- Gemma 4 本地 LLM 信号抽取（policy_direction / mixed_subtype / confidence）
- 行业 dashboard + 周报生成
- MCP stdio 接口（接入 Claude Desktop / OpenClaw）
- 推送队列 + 调度器 + 优雅关闭

**Scout 不做投资决策**；决策永远是用户的。

---

## 快速开始

```bash
# 安装
pip install -r requirements.txt

# 初始化 DB
python knowledge/init_db.py
python knowledge/init_queue_db.py

# 冷启动（录入 15 行业 + 5 原则 + user_context）
python scripts/cold_start.py --yes

# 查状态（应看到 watchlist active/total = 14/15）
python main.py status

# Phase 1 验收（14/14 应全绿）
python scripts/phase1_acceptance.py
```

---

## 命令速查

| 命令 | 作用 |
|---|---|
| `python main.py serve` | 长跑模式（调度器 + 消费循环）|
| `python main.py mcp` | MCP stdio server（Claude Desktop 接入）|
| `python main.py collect --source D1 --days 7` | 手动采集 |
| `python main.py report --type industry` | 手动生成周报 |
| `python main.py status` | 系统状态快照 |
| `pytest tests/ -q` | 全量测试（1031 passing）|

---

## 文档

| 文档 | 用途 |
|---|---|
| [docs/Scout操作手册_Phase1.md](docs/Scout操作手册_Phase1.md) | **日常使用手册**（每日流程 / 必做清单 / L4 决策辅助 / Phase 2A 预告） |
| [docs/mcp_integration.md](docs/mcp_integration.md) | Claude Desktop / OpenClaw MCP 接入配置 |
| [CLAUDE.md](CLAUDE.md) | Claude Code 项目记忆（架构决策 / 技术债 / Phase 2A 入口）|
| [scripts/cold_start_config.yaml](scripts/cold_start_config.yaml) | 冷启动录入的 15 行业 + 5 原则 |

---

## 项目结构

```
Scout/
├── main.py                          # CLI 入口（serve/mcp/collect/process/report/status）
├── config.yaml                      # 全局配置
├── agents/                          # SignalCollector / DirectionJudge
├── infra/                           # DB / 队列 / dashboard / MCP / push_queue
├── contracts/                       # Pydantic v2 契约
├── knowledge/                       # DB schema 初始化
├── prompts/                         # LLM prompt 模板
├── scripts/                         # cold_start / acceptance / real smoke
├── tests/                           # 1031 tests, 0 regressions
├── docs/                            # 用户手册 + MCP 接入
├── data/                            # knowledge.db + queue.db
├── logs/                            # scout.log + mcp_access.log
└── reports/                         # 周报 Markdown
```

---

## 状态（Phase 1）

| 指标 | 数值 |
|---|---|
| 已完成 Step | 1–14 / 14 |
| 测试通过 | **1031 / 1031** |
| 验收通过 | **14 / 14** |
| 行业已录入 | 15（14 active + 1 observation）|
| 投资原则 | 5（P1-P5，结构化 dict）|

**下一步 → Phase 2A**：推荐 Agent + V3 Playwright + 财务一体化（见 [CLAUDE.md §6](CLAUDE.md)）
