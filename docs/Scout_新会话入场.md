# Scout 新会话入场仪式

> **用途**: 任何新的 claude.ai 会话开始时, 用户提到 Scout 项目, Claude Code 应该按本文档指引快速装载上下文。
> **创建日期**: 2026-04-25

## 触发条件

用户在新会话第一条消息里提到任一关键词:

- Scout / scout 项目
- 蓝图 / 系统蓝图
- TD-XXX (任何技术债务编号)
- 半导体设备 / 投资研究系统
- "我在做的项目" (用户上下文里有 Scout 的强暗示)

## 第 1 步: 读 5 份核心文档 (按顺序)

1. **[docs/Scout_今日认知日_2026-04-25.md](Scout_今日认知日_2026-04-25.md)** (或最新的认知日文档)
   一份就能拼出 Scout 当前完整状态: 3 层认知 + 14 commits + 5 个判断转折 + 6 个关键事实 + 未关闭循环

2. **[docs/Scout_设计意图与实现偏离.md](Scout_设计意图与实现偏离.md)**
   8 节, 含评分体系偏离 + 自省能力缺失 + Layer 1/2 修复方向 + v1.61 veto 历史澄清

3. **[docs/Scout_技术债务清单.md](Scout_技术债务清单.md)** (TD 总册)
   26 条 TD 现状, **顶部 "⚠️ 优先级警示" 必看**

4. **[docs/Scout_完整蓝图盘点_2026-04-25.md](Scout_完整蓝图盘点_2026-04-25.md)**
   47 项决策实施状态, 已登 TD 标注

5. **[CLAUDE.md](../CLAUDE.md)** (工程惯例)
   git / db / 部署 / 通信规范

## 第 2 步: 读最近 3 个 commit 看进展

```bash
git log --oneline -3
```

## 第 3 步: 读 4 份 memory 文件 (auto-memory)

- `memory/scout_methodology_flaws.md` (含 🔴 根本性问题段)
- `memory/scout_3model_triangle.md` (Gemma / Qwen / DeepSeek 时间窗口)
- `memory/scout_no_canslim.md` (定位纪律)
- `memory/scout_v161_veto_rules.md` (v1.61 veto 错误纠正记录)

## 第 4 步: 检查 Scout serve 运行状态

```powershell
Get-Process -Name python | Where-Object { $_.MainWindowTitle -like "*scout*" }
```

确认 PID 是否还在跑 (2026-04-25 部署的是 PID 2468 commit `76fd672+` 链路)

## 第 5 步: 给用户报告 "Scout 当前快照"

用如下模板回复用户:

---SNAPSHOT START---

**Scout 当前状态 (基于 docs/ 最新归档)**

## 三层根本认知 (2026-04-25 完整审视后)

1. **评分体系**: 部分偏离 4 维度并列设计, 部分是代码未实装 (Layer 1 已识别 6 条 TD)
2. **自省能力**: 完全缺失 (Phase 4-5 建 unknowns 表)
3. **跨市场链路**: 数据未填 (TD-019 Phase 2A 修)

## 进行中的工作

- 已部署观察期: TD-002 (suppress 热重载) / TD-010 (勿扰 digest)
- 已实战验证: 半导体设备 5 字段填写 (commit `6c6205b`)

## Layer 1 实装清单 (Phase 2B 优先)

6 条 🔴 TD: TD-014a / TD-020 / TD-021 / TD-022 / TD-023 / TD-024
做完后 before/after 对比验证 "是否需要 Layer 2 重写"。

## 下一步选项 (按优先级)

- TD-020 三市场差异权重 (🔴, AI 链美股识别根因之一, 1-2 小时)
- TD-013 industry_dict 数据孤岛架构决策 (🔴, 阻塞 Phase 2A)
- 4 行业补字段 (半导体材料 / 新材料 / 生物制造 / 低空经济)
- Phase B Qwen canary (5 月主力切换决策)
- TD-026 独角兽曝光重新设计 (🔴, 用户核心定位)

## 关键纪律 (避免再次反复)

- Scout 不引入: CANSLIM 技术面 / 欧奈尔体系 / 止损纪律 / 动量信号
- Scout 不做: Demo 模式 (v1.64) / LLM 预填 (v1.66) — 用户私用
- 评分体系修复: Layer 1 (实装) 先做, Layer 2 (重写) 后评估
- v1.61 veto 必须区分: 蓝图原文 (d4<6) vs 用户提案 (d6<40)

请用户给下一步指令。

---SNAPSHOT END---

## 注意事项 (装载完成后的行为约束)

- 不主动启动任何修复 / 部署
- 不主动建议 "今天结束" / "你休息一下" 类 paternalism 话术
- 严格按用户指令逐个执行, 不自动堆任务
- 如果发现新问题, 报告给用户, 不擅自登记 TD
- 用户问 "我现在该做什么", 不要给 3-5 选项菜单, 给 1-2 个高 ROI 建议
- 用户说 "逐个做", 严格逐个发指令, 不打包

## 关于 "我" (claude.ai 端)

新会话的 claude.ai 端 (我) 是 stateless 的, 没有历史记忆。

本入场机制 = Claude Code 主动喂上下文给 (我), 让 (我) 获得和今天会话同等质量的判断能力。

(我) 不需要 "记得" 今天发生了什么, (我) 只需要看到 SNAPSHOT 和用户当前问题, 就能给出和今天质量相当的判断。
