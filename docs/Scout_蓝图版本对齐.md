# Scout 蓝图版本对齐

> 本文档用于**对齐蓝图设计版本与运行态代码版本**，避免两者混淆。

---

## 两套独立的版本号

Scout 有两套互不相同的版本编号：

| 版本体系 | 当前值 | 含义 | 所在文件 |
|---|---|---|---|
| **蓝图设计版本** | **v1.69** | 设计迭代次数（系统蓝图.md 的内部编号） | `C:\Users\13700F\Desktop\Scout\系统蓝图.md` |
| **运行态 scout_version** | **v1.15** | 实装代码快照（PushConsumerAgent + Watchdog + suppress）| [CLAUDE.md](../CLAUDE.md) / git tags |

**务必不要混用**：蓝图说 "v1.60新增 mixed_subtype" 指的是**蓝图第 60 次修订时加入这个设计**；运行态 "v1.15 suppress" 指的是**代码在 v1.15 这个发版点上线了 SUPPRESSED_ERRORS 功能**。

---

## 确认结论（2026-04-24）

- 系统蓝图实际最新版：**v1.69**（不是 v1.60）
- 蓝图文末 changelog 明确："v1-v1.69 共 169 次变更"
- 用户原以为是 v1.60，实际已经过 9 次迭代：v1.61 / v1.62 / v1.63 / v1.64 / v1.65 / v1.66 / v1.67 / v1.68 / v1.69

---

## 蓝图 v1.60 → v1.69 主要变化（节选）

| 版本 | 核心改动 |
|---|---|
| v1.60 | mixed_subtype 三类（conflict / structural / stage_difference）、信号矛盾分类、sub_market_signals |
| v1.61 | Phase 2 重新拆分为 2A/2B/2C；决策回路整合；风控优先级统一 |
| v1.62 | Scout vs LLM 诚实定位 + 协作架构（7 种场景分工） |
| v1.63~66 | （细节迭代，详见 changelog.md） |
| v1.67 | Scout 责任边界；Scout vs 商业投资工具对照 |
| v1.68 | （细节迭代） |
| v1.69 | 独特 10% 深化路线；外部工具链接模板（Koyfin / Seeking Alpha / TradingView） |

---

## v1.15 运行态验证已追加到蓝图

蓝图末尾追加"运行态验证记录（v1.15 · 2026-04-24）"小节，包含：

- 5 天运行结果（Watchdog 救活 4 次、推荐 154→228、A 级 3 只）
- 新增关键决策（D-018 Watchdog / D-019 tool profile full / D-020 QQ 直调）
- 已知缺陷（TD-002 ~ TD-005）
- Phase 2B 进度（QQ 插件 3/8）

**注意**：蓝图文件在 `C:\Users\13700F\Desktop\Scout\`（不在 git 仓库内），对蓝图的修改不会推到 GitHub。本对齐文档是**镜像索引**，确保 Scout 仓库的开发者能从这里找到蓝图更新。

---

## 如何同步

**蓝图改了 → 运行态怎么跟进**：

1. 蓝图新增设计项（如 v1.70 提出某新 Agent）
2. 在 [Scout_开发顺序规划.md](Scout_开发顺序规划.md) 中把该项排进 Phase 2B/2C/3
3. 实装时在 [Scout_技术决策记录.md](Scout_技术决策记录.md) 加对应 D-条目
4. scout_version 递增（v1.16 / v1.17 ...）

**运行态验证了 → 蓝图如何更新**：

1. 运行态出现新事实（如 Watchdog 实战有效）
2. 在 [Scout_当前进度.md](Scout_当前进度.md) 记录
3. 追加"运行态验证记录（vX.X · YYYY-MM-DD）"小节到蓝图末尾
4. 蓝图版本号递增（v1.70 / v1.71 ...）
