# 产品方向备忘：Human-directed, AI-operated business

**Date:** 2026-09-12（2026-09-25 补记一节：通用需求值不值得提前做）
**Status:** 备忘，不是裁决。记录方向讨论的结论，好让以后的会话不用重走一遍。
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

## 补记 2026-09-25：通用需求（AI 前台一类）值不值得提前做

问题：还没收到具体需求，但中小生意"必然会出现"的需求——AI 前台除了回邮件还能接电话
之类——能不能提前做？这一轮是桌面研究，没写代码。

**一句话：需求是真的，但"必然"是最差的选品标准。必然 = 人人看得见 = 已经有人做完了。**
该用的尺子是「必然 + 现成软件做不了 + 做错了代价大」，三条都满足才轮到我们。

### 逐条对表：这些需求已经在客户付费的软件里

| 必然需求 | 谁已经做了 |
|---|---|
| 漏接电话 / 下班后来电 | 澳洲 AI 前台厂商一大批，A$99–1,299/月，setup 最高约 $990；人工接听服务更早 |
| 接电话本身 | 微软 2026 年已上线 Teams Phone Agent（AI 话务员，60+ 语言，可接 Copilot Studio 自定义语音 agent）和 Copilot 代接来电。我们两个客户里有一个是 M365 |
| 预约提醒、报价跟进、发票催收、完工回访 | ServiceM8 的 Automation 插件 + 双向短信，四条全有 |
| 逾期账款催收 | Xero 自动提醒；JAX agent 也管这块 |
| 物业报修的下班后接听与分级 | 澳洲已有专做 rent roll 的接听服务，AI 与人工两种，含紧急分级和升级到 tradie |
| 报价发出去没下文的跟进 | **缝还开着**：Xero 没有，用户提案仍在 discovery；第三方 Paidnice 在填 |

这是本备忘"真正的对手是嵌入式 AI"那条结论的实测复现，而且更进一层：平台方已经把
AI 做到电话上了。

### 数字：厂商的不能用，独立的讲另一个故事

"漏接电话让澳洲小生意一年损失 80 亿""平均每家 12.6 万""tradie 漏接 40% 来电"——
查到的**每一个**来源都是卖漏接回拨产品的公司自己的博客，没有 ABS、行业协会或学术来源。
有独立来源的是：ABS（约 7,000 家企业，2025-10 至 2026-02）**小企业 11%** 在用 AI（中型
22%、大型 35%）；国家 AI 中心 SME tracker 44%，但"老板偶尔用一次聊天机器人"也算；
Weel 交易数据 2026-06 有 **30.8%** 的 SMB 在为 AI 付费。合起来是一个很陡的漏斗：
44% 试过 → 31% 付费 → 12% 真进工作流 → 不到 10% 说影响显著。用途集中在后台行政。
不采用的第一原因是信任（未采用者的 65%），第二是没人会（CEDA：67%）。

### 提前做的三项代价

- **另一套运行时。** 通话要 1 秒内回话，流水线是秒到分钟级（share-chat 流式已撞过
  LangGraph 的节点边界）。实时环必须外购，不是给现有引擎加通道。
- **授权/动作层。** 电话必须当场答复、当场约时间，没有人在旁边审草稿——"只起草不发送"
  这条安全边界在这里整体失效。等于把本备忘第 1 条候选裁决提前变成前置条件。
- **合规面。** 各州录音告知是 Surveillance / Listening Devices Acts 的拼盘（开场全员告知
  是唯一安全做法，同时满足 APP 5）；联邦没有强制披露 AI 的规定，但 ACL 第 18 条管得着
  "被问是不是 AI 却含糊其辞"；DNC 登记册对 AI 语音与真人一视同仁；ACMA 到 2026-05
  仍无 AI 语音专门指引。

另外，这个市场的差评很集中：配了约 30 小时仍不可靠、机器人音、报不出公司名、退订退不掉。
钱花在调优和售后，不在功能——对单人兼职是最贵的一种成本。

### 如果哪天真要碰前台，位置在哪

不在"那通电话"。电话是 commodity：底层每分钟 $0.07–0.31，中间全是 reseller，微软还免费送。
站得住的是**电话被别人接完之后交过来的那一段**——"这次报修归房东还是租客出钱""上次是哪个
供应商来修的""合同条款怎么写的"。答案不在通话里，在文件、邮件和历史记录里，通用前台厂商
做不了，而这正是知识库 + 流水线的强项。要做就做交接之后那段，让别人去接电话。

### 所以"提前做"什么

不是功能，是准备度，而且就是本备忘第 1 条：授权表。它不依赖通道，光邮件自己就会撞上。
即便如此也等触发条件，不提前动手。

**来源：** ABS 媒体稿 `business-adoption-artificial-intelligence-accelerates-2024-25`；
国家 AI 中心 SME tracker（2026-02）；Weel 付费数据经 scalesuite 口径拆解；
Microsoft Teams Phone Agent 公告（techcommunity，2026）；ServiceM8 Automation 帮助中心；
Xero Central 发票提醒 + Xero Product Ideas 的报价提醒提案；telnyx 澳洲录音法指南；
justcall AI 语音披露与 DNC 汇总。

## 什么时候重开这个话题

- 有客户提出"直接发，别让我批"→ 做第 1 条，先写 `docs/superpowers/specs/` 的设计。
- B1 跑出第一个月的草稿结果统计 → 用数字决定哪一类邮件先升到 Act 级。
- 有客户说"电话进来没人接"比"邮件处理不过来"更痛 → 先问他现在怎么处理、愿不愿意让
  别人接完转过来；仍然不从建通道开始。
- 在这几件事之前，评估里的其他内容不进 `DECISIONS.md`，也不进代码。
