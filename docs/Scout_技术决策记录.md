# Scout 技术决策记录（ADR-lite）

> 每条记录：**决策 / 背景 / 选项 / 决定理由 / 影响 / 状态**。
> 最新在最前。

---

## D-019 · OpenClaw tool profile 必须用 `full`（2026-04-24）

**背景**：Scout MCP server 通过 OpenClaw Gateway 对外暴露。OpenClaw 支持两种 tool profile：
- `minimal`：只暴露核心若干工具（读类为主）
- `full`：暴露全部注册工具

上线后使用 `minimal` profile 时，`get_pending_messages` / `mark_read` / `get_policy_for_motivation_analysis` 等 v1.12 之后新增的工具在外部 Agent（Claude Desktop）不可见，导致 PushConsumerAgent 拉取链路断裂。

**选项**：
1. 留 `minimal`，手动在 OpenClaw 白名单里逐个添加 — 要求每加一个 MCP 工具就改 gateway 配置，易漏
2. 改 `full`，允许所有已注册工具透传 — 暴露面略大，但都是 Scout 自有只读/写安全的工具

**决定**：采用 `full`。

**理由**：
- Scout 的 MCP 工具全部降级不抛异常（见核心约束 第 4 条），暴露面扩大不会引入破坏性风险
- v1.12 之后工具增长较快，逐个白名单成本 > 收益
- Scout 是私有部署，不是 SaaS 公开接口，安全边界由 Gateway 认证而非工具裁剪保证

**影响**：
- OpenClaw 配置：`tool_profile: full`
- MCP 工具新增时无需改 Gateway
- 若将来暴露给第三方，需要重新评估

**状态**：已应用（2026-04-24）。

---

## D-018 · Watchdog 实战验证通过（2026-04-24）

**背景**：2026-04-18 首次部署 `C:\Tools\scout-watchdog.ps1`（5 分钟一检 + PowerShell restart + QQ Push + 每日 09:04 KST 心跳）。用户 04-19 起离家 5 天，需检验"用户不在家时 Scout 能否自愈"。

**选项**（回顾部署前考量）：
1. 外置 Watchdog（PowerShell 定时任务）— 简单、进程隔离、能拉起 scout 本身
2. 内置 Supervisor（main.py 自监）— 无法自救 main.py 进程挂掉的情况
3. Windows Service 包装 — 开发成本高，Phase 1 不值

**决定**：采用方案 1 外置 Watchdog。

**验证结果**（5 天）：

| 维度 | 预期 | 实测 |
|---|---|---|
| 心跳到达率 | 每日 09:04 KST | 5/5 天准点（04-20 ~ 04-24） |
| 救活次数 | ≥ 1 次 | **7 次**（ollama×2, scout serve×4, gateway×1） |
| 抖动次数 | 同小时 ≤ 3 次硬重启 | 04-19 22:13~22:18 scout 三次连环重启，第 3 次后稳定（hist 计数器工作正常） |
| QQ 告警延迟 | < 30s | 实测 2~5s |
| 救活成功率 | 100% | 100%（所有 restart 后下一次 check 恢复 True） |

**影响**：
- Watchdog 机制进入**生产就绪**状态，后续 Phase 2B 不再需要单独加内置 Supervisor
- Watchdog 脚本由 `C:\Tools\scout-watchdog.ps1` 独立维护，不进入 Scout 主仓库（避免循环依赖）
- 每次 DOWN 事件写 `C:\Tools\scout-watchdog.log`；Dashboard 从该日志 tail 显示

**状态**：已采纳为 Scout 运维基础设施的一部分。

---

## D-017 及更早（略）

> 历史决策（D-001 UTC 存储、D-002 Pydantic extra=forbid、D-003 BaseAgent 6 类错误矩阵、D-004 SQLite WAL、D-005 Gemma 本地、D-006 规则优先 + Gemma 辅助、D-007 MCP stdio、D-008 queue.db 独立、D-009 asyncio+APScheduler、D-010 reports/ 落盘、D-011 V3 Playwright、D-012 PushConsumerAgent 拉取模式…）见 [CLAUDE.md §3](../CLAUDE.md) 架构决策表，此处不重复。

本文档仅记录 **新增** 或 **需要展开讨论** 的决策（通常是 CLAUDE.md 表格容纳不下的、带选项对比和验证结果的那种）。
