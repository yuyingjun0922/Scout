# Scout MCP Integration Guide

Scout 对外通过 MCP (Model Context Protocol) 暴露 10 个工具给外部 LLM 客户端（Claude Desktop、OpenClaw、任何兼容 MCP 的 IDE）。本文档说明如何配置接入。

**协议**：MCP 1.0（JSON-RPC over stdio）
**Server 名**：`scout`
**传输**：stdio（子进程管道，无网络端口）
**实现**：[`infra/mcp_server.py`](../infra/mcp_server.py)

---

## 快速验证

先确认 Scout 端可以独立启动：

```bash
cd D:/13700F/Scout
python infra/mcp_server.py --help
```

应看到 CLI help。

跑 stdio 烟囱（启动服务、列工具、调全部工具）：

```bash
python scripts/test_mcp_real.py
```

应看到 10 个工具全部返回 `ok=true`。

---

## 工具清单（10 个）

### 只读查询

| 工具 | 入参 | 用途 |
|---|---|---|
| `get_watchlist` | （无） | 列 active 行业 + 状态 |
| `ask_industry` | `industry: str, days: int = 30` | 单行业 dashboard |
| `get_system_status` | （无） | DB/信源/成本/错误健康快照 |
| `search_signals` | `query, source?, days=30, limit=20` | 关键词 + 源 + 时间搜 info_units |
| `get_latest_weekly_report` | `type: "industry" \| "paper"` | 读 reports/ 最新周报 |

### 写入

| 工具 | 入参 | 用途 |
|---|---|---|
| `add_industry` | `industry, reason=""` | 加入 watchlist（active）|
| `remove_industry` | `industry, reason` | 软删：zone='cold' |

### LLM 深度分析专用

| 工具 | 入参 | 用途 |
|---|---|---|
| `get_industry_full_context` | `industry` | 行业全景（watchlist + 180 天 info_units）|
| `get_decision_context` | `stock` | 个股决策上下文（Phase 1 简化）|
| `get_policy_for_motivation_analysis` | `info_unit_id` | 政策原文 + 近 1 年类似政策 |

所有工具返回 `{"ok": bool, ...}` 的 JSON 字典。失败不抛异常，`ok=false, error="..."`。

---

## Claude Desktop 接入

### 1. 找配置文件

| OS | 路径 |
|---|---|
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |

### 2. 加一个 `mcpServers` 条目

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

关键点：

- `command` 必须是**绝对路径**，否则 Claude Desktop 可能找不到 Python。
- `-u` 关闭 stdout 缓冲，让 MCP 消息及时送达。
- `PYTHONIOENCODING=utf-8` 防止 Windows cp949 乱码（中文字段会爆）。
- `SCOUT_DB_PATH` 可切 `test_knowledge.db` 做验证、切 `knowledge.db` 做生产。

### 3. 重启 Claude Desktop

应用菜单 → Quit（不是关闭窗口），再打开。

在对话里试一句："**用 scout 工具查一下 watchlist 里有哪些行业。**"

Claude Desktop 会调 `scout.get_watchlist()`，把返回 JSON 纳入回答上下文。

---

## OpenClaw 接入

OpenClaw 的 MCP 配置在 `settings.json`（或 workspace 的 `.vscode/settings.json`）：

```json
{
  "claude.mcp.servers": {
    "scout": {
      "command": "python",
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

如果 OpenClaw 能继承 shell PATH，可以直接用 `"python"`；否则换成绝对路径。

重启 OpenClaw，在任何对话里可通过 `@scout` 触发工具调用。

---

## 环境变量

| 变量 | 含义 | 默认 |
|---|---|---|
| `SCOUT_DB_PATH` | info_units DB 路径（覆盖 config.yaml）| config.yaml 的 `database.knowledge_db` |
| `PYTHONIOENCODING` | Python stdio 编码 | 建议 `utf-8`（Windows 必需）|

---

## 调试

### 访问日志

每次工具调用写一行 JSON 到 `logs/mcp_access.log`：

```
2026-04-18 12:00:00,001 {"tool": "get_watchlist", "params": {}, "ok": true, "rows": 3, "ms": 12}
```

字段：
- `tool`：工具名
- `params`：入参
- `ok`：成功 / 失败
- `rows`：返回的行数（估算）
- `ms`：耗时毫秒
- `error`：失败时的错误串（最多 300 字）

### stderr

Server 启动时打一行到 stderr：

```
[scout-mcp] db=D:/13700F/Scout/data/knowledge.db reports=... logs=...
```

Claude Desktop 在主机文件里记录 stderr（Windows：`%APPDATA%\Claude\logs`）。

### 手动测

用 [`scripts/test_mcp_real.py`](../scripts/test_mcp_real.py) 独立跑：

```bash
python scripts/test_mcp_real.py --tool get_watchlist      # 只跑一个
python scripts/test_mcp_real.py --prod-db                 # 用主库
```

---

## 常见问题

**Q: Claude Desktop 说 "MCP server failed to start"。**
- 99% 是 `command` 路径错。把 Python 绝对路径写死。
- 看 `%APPDATA%\Claude\logs\mcp-server-scout.log`。

**Q: 返回中文乱码。**
- `env.PYTHONIOENCODING=utf-8` 必须加，Windows 默认 cp949。

**Q: 工具返回 `{"ok": false, "error": "..."}`。**
- 这是正常。工具永不抛异常，全部降级返回。LLM 看到 ok=false 会知道如何 fallback。

**Q: `search_signals` 想看最近所有信号，query 能传空吗？**
- 不行。`query` 必须非空。查全量请用 `get_industry_full_context` 或直接 SQL。

**Q: `add_industry` / `remove_industry` 失败了数据会回滚吗？**
- 每个工具单独事务；`DatabaseManager.write` 失败会 rollback。成功 commit，失败返 `{"ok": false, "error": "..."}`。

**Q: 可以并发调吗？**
- Phase 1 验证了"50 次顺序调用 + 单连接"。SQLite WAL + `BEGIN IMMEDIATE` 支持多个 MCP 客户端并发读、读写互斥。生产建议：Claude Desktop + OpenClaw 同时接一个 server 实例没问题；2+ server 实例同时开着都写同一个 DB 理论可行但未压测。

---

## Phase 1 / Phase 2 差异

| 工具 | Phase 1 | Phase 2A+ |
|---|---|---|
| `get_industry_full_context` | watchlist + 180 天 info_units；related_stocks/chain 留空 | 填入财务 / 产业链 / 未覆盖维度 |
| `get_decision_context` | 只返 related_stocks 基础表 | 加基本面 / 持仓 / thesis |
| `get_policy_for_motivation_analysis` | 同 source + 同 category + 标题子串匹配 | 语义检索 + 事件串关联 |

其它 7 个工具 Phase 1 已稳定。

---

## 扩展

加新工具，两步：

1. 在 [`infra/mcp_server.py`](../infra/mcp_server.py) 的 `ScoutToolImpl` 加方法，`self._call(tool_name, params, impl_fn)` 包住，返 `{"ok": ..., ...}`。
2. 在 `build_server()` 里加 `@app.tool(description=...)` 装饰的薄 wrapper。

测试：
- `tests/test_mcp_server.py` 加一个测试类覆盖新工具。
- `scripts/test_mcp_real.py` 的 `DEFAULT_CALL_SEQUENCE` 加一条。

不要忘记更新本文档的"工具清单"。
