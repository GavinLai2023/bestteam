# 产品方向备忘：Human-directed, AI-operated business

**Date:** 2026-09-12
**Status:** 备忘，不是裁决。记录一次方向讨论的结论，好让以后的会话不用重走一遍。
这里没有任何一条改变当前的冻结（见 `docs/STATUS.md`）；能变成裁决的那一条，触发条件写在最后。
**来源：** 一份外部评估（LLM 生成）提出 BestTeam 应从"AI 员工"转向
"人定方向、AI 运营的业务操作层"，以 Intent / Context / Constraints / Authority /
Execution / Accountability 六个概念取代 Agent / Team / Tool；同日两轮关于澳洲
SME 需求的调研，以及 2026-09-06 关于 AI 前台的讨论（结论：不重构、不加功能，
先跑零代码的漏接来电 spike）。

> **Historical direction memo, not an API reference.** Written in Chinese
> because it records the owner's product reasoning; the file names are current
> as of the date above.

## 一句话

方向对，时间不对。六个概念里五个已经在仓库里，缺的那个是一张表；
这份评估最想让我们做的事（按它重画架构）恰好是最不该做的。

## 六个概念对到仓库

| 概念 | 仓库里对应的东西 | 状态 |
|---|---|---|
| Intent | `core/specification.py`、`core/requirements.py` — 向导把一句话意图变成 Specification → Requirements → 团队 | 有 |
| Context | 知识库（`core/knowledge_base.py` 等三种）、按用户记忆（`core/memory.py`） | 有；是"每个 agent 一份"还是"整个业务一份"未议 |
| Constraints | Requirements 的 `constraints`（含跳过澄清问题时的 `Assumed:`）；每组织的额度、急停开关 | 有一半：是自然语言约束，不是可查询的业务规则 |
| **Authority** | **没有。** 只有一档：邮件工具永远只起草（`tools/` 的 `email_draft_reply`），发不发由人 | **缺** |
| Execution | `Agent`/`Team`/`Pipeline` + `adapters/base.py` 的 `EngineAdapter` 缝 + `LangGraphAdapter` | 有；评估要的"agent 是实现细节"就是这个分层 |
| Accountability | 运行轨迹（`core/trace.py`）、客户/管理员/诊断三个 register（PR #119）、`grounding_checked`（`core/grounding.py`）、动作结果（`ui/backend/automation_results.py`，只信轨迹确认的 `draft_created`，不信模型自述） | 有 |

结论：**现有抽象没有一个会因为这个方向变成技术债。** 缺的是 Authority，它不是一层架构，是一张按组织、按动作类别的表，在动作执行前被查询。

## 三句话（候选裁决，还没到裁决的时候）

1. **授权表。** 等某个客户第一次要求 AI *发送*而不只是起草，那时候做：
   动作类别 × 允许 / 询问 / 禁止，按组织配置，动作层执行前查表；不写在 prompt 里。
   做的时候要重新推导 `docs/DECISIONS.md` 里"email 与 egress 工具按 pipeline 互斥"
   那条——它把邮件当不可信输入、把 `http_get`/`web_search` 当外泄通道；一旦 AI 可以
   *发送*邮件，邮件本身就成了外泄通道，那条规则挡住的注入路径会在邮件工具包内部重新打开。
2. **自主权分级。** 评估提出 Observe → Recommend → Draft → Act(例行) → Autonomous
   → Outcome 六级。BestTeam 今天是 **Draft 级**；`grounding_policy: observe`
   已经是 Observe 级的机制。升到 Act 级的门票是证据，不是架构：
   B1 草稿结果追踪（PR #116）→ 统计出"上月若由我处理，N% 合理"→ 授权表 → 只把异常交给人。
   顺序不能跳。
3. **"Intent in, BestTeam out" 的动态组队含义已经成立**——在向导的构建期
   （一个 intent → 一支为它组成的团队）。不需要把组队挪到每次运行时来让口号更有内涵。

## 评估里不采纳的四点，以及为什么

- **运行时动态组队**（每个 mission 临时生出一批 worker）。固定流水线的可靠性还没解决：
  两次 12 分钟的客户运行被 59 字的闲聊带到 9 万 token；向导三天静默生成同一支预制团队；
  DeepSeek 一个 400 打死所有 HIERARCHICAL 团队。HIERARCHICAL 是仓库里最接近
  "系统自己决定谁干什么"的模式，也是最脆的。组队已经在构建期做了；挪到运行时就是全部风险。
- **加权目标函数**（0.30×利润 + 0.25×增长…）。它和评估自己的价值观一节矛盾：
  "合作 15 年的客户我继续支持"是约束，不可交易；加权和的定义就是价格合适时把它换掉。
  正确模型是 Constraints，不是 objective。另外从 onboarding 对话里隐式推断老板的价值权重，
  正是 NAIC 指引和 ADM 披露规则最警惕的做法。
- **匿名 worker**（"用户不需要知道有几个 agent，像微服务一样"）。PR #119 的发现是客户看轨迹，
  而且按角色名审计。拟人的岗位名对引擎是实现细节，对 accountability register 是可读性的载体。
  评估自己的例子里 worker 立刻又有了名字。
- **目标进度条**（"MRR 涨到 $80k — 82%"）。需要读账目并把结果归因到动作；澳洲 SME
  不会把数据搬出 Xero/MYOB，唯一无需谈合作的通道是邮件。当远期图景可以，当"最重要的 UI"不行。
  "需要你决定" + "已替你处理" 两栏是对的，材料已有。

## 什么时候重开这个话题

- 有客户提出"直接发，别让我批"→ 做第 1 条，先写 `docs/superpowers/specs/` 的设计。
- B1 跑出第一个月的草稿结果统计 → 用数字决定哪一类邮件先升到 Act 级。
- 在这两件事之前，评估里的其他内容不进 `DECISIONS.md`，也不进代码。
