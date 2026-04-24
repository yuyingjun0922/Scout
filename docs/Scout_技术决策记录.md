# Scout 技术决策记录（ADR-lite）

> 每条记录：**决策 / 背景 / 选项 / 决定理由 / 影响 / 状态**。
> 最新在最前。

---

## D-020 · QQ 主动推送走 QQ 官方 API 直调，不走 OpenClaw 插件（2026-04-24）

**背景**：Scout 需要在 push_consumer_agent / health_monitor_agent 里主动推 QQ 消息（C2C 单点）。
可选路径：
1. 走 OpenClaw `openclaw-qqbot` 插件（plugin tool 调用）
2. 直接 POST 到 QQ 官方开放平台 `api.sgroup.qq.com`

**选项对比**：

| 维度 | 走 OpenClaw 插件 | 直调 QQ 官方 API |
|---|---|---|
| 依赖 | Gateway 必须在线、plugin 必须加载 | 无外部依赖 |
| 失败模式 | 多一层（gateway/tool profile/plugin） | 单层（network + auth） |
| token 管理 | 由插件代管 | Scout 自己缓存 + 刷新 |
| rate limit | 插件可能不暴露 | Scout 自己控制 |
| 用户意图清晰度 | Scout 的消息 vs OpenClaw session 消息混在一起 | Scout 专属通道清晰 |

**决定**：直调 QQ 官方 API（`infra/qq_channel.py`），**不走 OpenClaw 插件**。

**实现要点**：

1. **双 endpoint 协议**
   - Step 1 `POST https://bots.qq.com/app/getAppAccessToken` → `{access_token, expires_in}`
   - Step 2 `POST https://api.sgroup.qq.com/v2/users/{openid}/messages` with `Authorization: QQBot <token>`

2. **Token 缓存**
   - 进程内存（不持久化到 DB，避免多进程共享 token 过期冲突）
   - 官方 `expires_in` 通常是 7200s，但偶尔返回 **155s**（小数值变体）— **过期前 30s 主动刷新**，容忍这种抖动
   - 线程不安全（Scout 单 event loop，无需加锁）

3. **Rate limit 保护**
   - 实例内滑动窗口，默认 **1 分钟 ≤ 10 条**
   - 超限返回 `(False, {"error": "rate_limited"})`，由调用方决定跳过或延迟
   - 保证不会因为 bug 循环推送 spam 到用户 QQ

4. **失败不抛异常**
   - 返回 `(ok: bool, detail: dict)`；PushQueue 根据 detail 决定是否重试（遵循 Scout "MCP 工具不 raise" 原则）

**影响**：
- Scout 的 QQ 推送通道与 OpenClaw 解耦 — 即使 Gateway 挂，Watchdog 仍能推告警到 QQ
- Scout 需要自己维护 QQ 应用凭据（`QQ_APP_ID`, `QQ_CLIENT_SECRET` 走 env）
- 降低调试复杂度：失败时只需排查 Scout→QQ 两点，而不是 Scout→Gateway→Plugin→QQ 四点

**状态**：已应用（`infra/qq_channel.py` v1.13）。

---

## D-019 · OpenClaw tool profile 必须用 `full`（2026-04-24）

**背景**：Scout 通过 OpenClaw Gateway 把 MCP server 暴露给外部 Agent（Claude Desktop 等）。OpenClaw 支持多种 tool profile：
- `coding` — 默认 profile，侧重代码编辑工具（read / write / bash 等）
- `full` — 全量注入，包含所有已注册工具
- 以及 `minimal` 等变体

**失败场景**（部署初期踩过）：
- 使用 `coding` profile 时，MCP plugin tools（Scout 自己注册的 `get_pending_messages` / `mark_read` / `get_policy_for_motivation_analysis` 等）**不会被注入**
- OpenClaw 的 `group:plugins` 默认为**空**，意味着即便 profile 选 `coding`，插件工具组也不生效
- 结果：外部 Agent（Claude Desktop）调 MCP 时看不到 Scout 的自定义工具，PushConsumerAgent 拉取链路完全断裂

**选项**：
1. 在 `coding` profile 下手动把 plugin tools 加入白名单 — 需要每个 MCP 工具改 Gateway 配置，维护成本高
2. 切 `full` profile — 全量注入，所有 MCP 工具自动可见
3. 自定义 profile — 过度工程

**决定**：采用 **`full`**。

**理由**：
- 只有 `full` 会把 plugin 层注册的 tools 透传给外部 Agent
- Scout 的 MCP 工具全部降级不抛异常（核心约束），暴露面扩大不引入破坏性风险
- v1.12 之后 MCP 工具新增较快，逐个改白名单成本 > 收益
- Scout 是私有部署（非 SaaS 公开接口），安全边界由 Gateway 认证保证，不靠工具裁剪

**影响**：
- OpenClaw 配置写死：`tool_profile: full`
- Scout 新增 MCP 工具无需动 Gateway
- 未来若要开放给第三方 Agent，需要重新评估是否加白名单

**状态**：已应用（2026-04-24）。**部署手册应显式写出此项**，避免后续部署时踩同一个坑。

---

## D-018 · Watchdog 实战验证通过（2026-04-24）

**背景**：2026-04-18 首次部署 `C:\Tools\scout-watchdog.ps1`（5 分钟一检 + PowerShell restart + QQ 主动推送 + 每日 09:04 KST 心跳）。用户 04-19 起离家 5 天，验证"用户不在家时 Scout 能否自愈"。

**选项**（部署前考量）：
1. 外置 Watchdog（PowerShell 定时任务） — 简单、进程隔离，能拉起 scout 本身
2. 内置 Supervisor（main.py 自监） — 无法自救 main.py 挂掉的场景
3. Windows Service 包装 — 开发成本高，Phase 1 不值

**决定**：方案 1 外置 Watchdog。

**5 天验证结果**：

| 维度 | 预期 | 实测 |
|---|---|---|
| 心跳到达率 | 每日 09:04 KST 🟢 到 QQ | **5/5 天**准点（04-20 ~ 04-24） |
| 成功救活次数 | ≥ 1 次真实故障拉起 | **4 次**（04-19 部署末期 ollama+scout；04-24 今日 scout+ollama） |
| 抖动保护 | 同小时 ≤ 3 次硬重启（hist counter） | 04-19 22:13~22:18 scout 3 次连环重启，hist 0→1→2，第 3 次后稳定 |
| QQ 告警延迟 | < 30s | 实测 2~5s |
| 救活成功率 | 100% | 100%（所有 restart 后下一次 check 恢复 True） |
| 误报（未挂却重启） | 0 | 0 |

**影响**：
- Watchdog 进入**生产就绪**状态；Phase 2B 不再加内置 Supervisor
- Watchdog 脚本独立维护于 `C:\Tools\scout-watchdog.ps1`，**不入 Scout 主仓库**（避免 watchdog 和 scout 绑定发版）
- DOWN 事件写 `C:\Tools\scout-watchdog.log`；Dashboard 从该日志 tail 2 行显示
- 每日心跳由 Watchdog 自调 QQ API（走 [D-020](#d-020) 的直调通道，**不依赖 Scout serve 本身存活**）

**状态**：已采纳为 Scout 运维基础设施。

---

## D-017 及更早（略）

> 历史决策（D-001 UTC 存储、D-002 Pydantic extra=forbid、D-003 BaseAgent 6 类错误矩阵、D-004 SQLite WAL、D-005 Gemma 本地、D-006 规则优先 + Gemma 辅助、D-007 MCP stdio、D-008 queue.db 独立、D-009 asyncio+APScheduler、D-010 reports/ 落盘、D-011 V3 Playwright、D-012 PushConsumerAgent 拉取模式…）见 [CLAUDE.md §3](../CLAUDE.md) 架构决策表，此处不重复。

本文档只记录**新增**或**需要展开讨论**的决策（通常是 CLAUDE.md 表格容纳不下的、带选项对比和验证结果的那种）。
