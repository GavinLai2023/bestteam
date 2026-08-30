# 面向非技术客户的"团队搭建向导"方法论与架构规划

> **Historical planning doc — methodology, not current API reference.** This
> describes the original phased plan; several specifics have since changed.
> Notably: the wizard shipped as **4 customer-facing stages, not 6**; the
> `/api/config` "advanced view" now covers **pipelines/skills/knowledge_bases**
> only — the standalone **agents/teams CRUD was removed** (PR #15, nothing
> consumed those records); and the deployment model is now **org-scoped
> multi-tenancy** (PR #14). For current state see `docs/STATUS.md`,
> `docs/DECISIONS.md`, and the per-directory `CLAUDE.md` files. The philosophy
> below (把复杂留给自己 / AI-guided wizard as the core interaction) still holds.

## Context

### 产品定位

`bestteam` 目前是一个**单用户、本地优先、YAML 驱动**的多 Agent 框架：配置
（Agent/Team/Pipeline/KnowledgeBase）是磁盘上的 YAML 文件，由 `core/loader.py`
的 `load_pipeline()` 解析为 `Pipeline` 对象；`ui/` 提供一个只读的运行监控面板，
没有持久化、登录、多用户概念。

面向客户的产品定位是：

- **目标客户是非技术人员**，对"Agent / Tool / Model / YAML"这类术语天然抗拒，
  技术感太强的产品会让他们本能地抗拒。
- 客户的输入**不是**结构化配置，而是模糊的 **Intent/Challenge**（"我们想解决
  什么问题"）和 **As-is 业务流程**（"我们现在是怎么做的"）。
- 平台需要**一步步引导客户**，把模糊的想法最终变成一个具有自主性的客制化多
  Agent 系统 —— 体验上要像"**雇了一个团队来为你工作**"，而不是"配置一个软件"。
- 需要一套**实施方法论**：**Intent → Requirements → Specification → Solution
  → Testing → Deployment**，每一步都对客户友好、可视化、可回退迭代。

因此，"图形化编辑器"不是产品的核心交互——**核心交互是一个 AI 驱动的引导式向导
（Team Builder Wizard）**；表单/CRUD 视图退居为"高级视角"（给我们团队或愿意
动手的客户做精细调整用）。

### 现有技术基础

- `core/loader.py::_build_pipeline(raw, source, extra_tools)` 接受一个与 YAML
  同构的 `dict`，产出 `Pipeline`。这个 `raw dict` 结构（agents/teams/
  knowledge_bases/pipeline.steps）天然适合作为"**Specification 的机器可读
  形式**"——向导最终要生产的就是这样一个 dict，并可直接复用现有的
  `ConfigurationError` 校验体系。
- `CollaborationMode.HIERARCHICAL`（manager + 下属）**未实现**
  （`adapters/langgraph_adapter.py:140-165` 对其抛 `ConfigurationError`）。
  "manager 带领团队"是"雇佣团队"隐喻里最直观的结构，对非技术客户来说
  "有一个负责人统筹安排"远比"sequential/parallel"更容易理解，所以这是
  **整个产品故事的核心依赖**，必须优先实现。
- `TraceEvent`（`core/trace.py`）+ `Pipeline.stream()` + WebSocket 已经能实时
  推送每个 agent 的执行过程——这正是 **Testing 阶段"看团队干活"** 所需的数据源，
  缺的只是"翻译成人话"的展示层。
- 当前没有持久化、没有登录、没有用量计量——这些基础设施仍然需要，但其设计
  应该服务于"向导会话(session)的状态机"，而不只是"配置的 CRUD"。

### 已确认的范围决策

1. **部署模式**：按客户独立部署（每客户一套实例，部署内部支持多个团队/工作流），
   不做跨客户的多租户 SaaS 隔离。
2. **Token/API Key 管理**：平台统一持有 Provider Key（环境变量/secrets），
   按 run 记录每次 LLM 调用的 token 用量，用于内部计量计费。
3. **可视化编辑器**：不做拖拽画布；也不是表单 CRUD 优先——优先级是
   "引导式向导"，表单是向导背后的数据呈现/微调方式。

## 产品理念："用 bestteam 搭建 bestteam"（Dogfooding）

向导本身的"引导能力"由**一组用 bestteam 自身的 Agent/Team 构建的"builder
agents"**驱动——这既是最快的实现路径（复用现有 SDK），也是对客户最有说服力
的演示（"连我们用来帮你建团队的，本身就是一个多 Agent 团队"）。

建议的 builder 团队（hierarchical 模式，"项目经理"为 manager）：

- **Business Analyst agent**：把 Intent + As-is 描述整理成结构化 Requirements
  （痛点、目标、成功标准、关键约束），用通俗语言向客户复述确认。
- **Solution Architect agent**：把确认后的 Requirements 转成
  **Specification**——即"团队设计草案"，**结构化输出**（见 Phase 0.5）同时
  包含：
  - 技术字段（agents/teams/knowledge_bases/pipeline.steps，匹配 loader schema）
  - 友好字段（每个 agent 的"岗位名称"、"职责描述大白话"、团队的"工作流程图
    大白话"）
- **(Testing 阶段) Narrator**：不一定是独立 agent，更可能是前端把
  `TraceEvent` 流翻译成"XX 专员正在处理…"这类活动卡片（见 Phase 5）。

## 六阶段方法论

| 阶段 | 客户看到/做什么 | 背后发生什么 | 产出（数据） | 可回退？ |
|---|---|---|---|---|
| 1. Intent | 自由文字描述"我们的挑战/想要解决的问题"+"现在怎么做的"（可选上传现有流程文档，复用 `parse_file`） | 直接存储为 builder session 的输入 | `intent_text`, `as_is_text`, 上传文件 | — |
| 2. Requirements | 看到一份用通俗语言总结的"需求摘要卡片"（痛点/目标/成功标准），可以打字补充/修正 | Business Analyst agent 总结 + （若信息不足）追问 1-2 个澄清问题，多轮对话直到客户确认 | `requirements` JSON（结构化但展示为卡片/要点列表） | 可随时回到阶段1补充信息 |
| 3. Specification | 看到"认识一下你的团队"页面：每个虚拟员工的头像/岗位名/职责一句话描述，以及"团队怎么协作"的流程示意（如"经理 -> 分配给研究员/分析师 -> 经理汇总"） | Solution Architect agent 用结构化输出生成 Spec（技术字段+友好字段），后端立刻用 `_build_pipeline()` 校验合法性 | `specification` = friendly view + 通过校验的 `raw` config dict | 可要求"重新设计"或针对某个角色"调整一下" -> 重新调用 architect（带上反馈） |
| 4. Solution | 对 Specification 的"确认/微调"视图——简单的语言化调整项（如"这个步骤改成大家一起做而不是依次做"、"这个专员还应该参考我们的XX文档"），不暴露 JSON/YAML | 用户的微调指令作为反馈再次调用 Solution Architect，或对 `raw` dict 做受限的、表单化的局部编辑（如团队模式下拉、KB 文档上传） | 更新后的 `raw` config，标记为 `status=ready_for_testing` | 可反复回到此阶段迭代 |
| 5. Testing | "试一试你的团队"沙盒：客户输入一个真实场景的请求，看到团队成员逐步"工作"的活动流（基于 TraceEvent 的友好化展示），最后看到团队产出的结果；可以给反馈"不太对，因为…" | 用 Phase 1 的 `raw` config 通过现有 `Pipeline.stream()` 真实执行；trace 经过友好化映射展示 | `runs`/`trace_events`（持久化），客户反馈文本 | 反馈可路由回阶段3/4重新设计 |
| 6. Deployment | "团队已上线"——获得一个简单的"找你的团队办事"入口（即友好版的 `/run`），可随时再次进入向导调整 | `pipeline.status = deployed`；记录上线时间 | `pipelines.status` 更新 | 上线后仍可回到向导做"团队调整" |

## 技术基础设施分期

### Phase 0：实现 HIERARCHICAL 协作模式（最高优先级，方法论的隐喻基石）

- 在 `LangGraphAdapter._build_team_graph`（`src/bestteam/adapters/langgraph_adapter.py`）
  的 `HIERARCHICAL` 分支中实现"manager 把每个下属包装成
  `delegate_to_<name>(task)` 工具"的 supervisor 模式，复用已有的 tool-calling
  循环（含最大迭代保护）。
- 新增 `tests/test_hierarchical_team.py`，参考
  `tests/test_pipeline.py::test_agent_executes_tool_calls_before_producing_final_output`
  的 `fake:` + 工具调用测试写法。
- 更新 `CLAUDE.md` "Known limitations"：HIERARCHICAL 已实现，DEBATE 仍未实现。

### Phase 0.5：结构化 Specification 生成与校验（向导的技术核心）

- 定义一组 Pydantic 模型，**镜像 loader 的 `raw dict` schema**
  （`AgentSpec`/`TeamSpec`/`KnowledgeBaseSpec`/`PipelineSpec`），并为每个
  agent/team 额外加 `display_name`、`friendly_description` 等纯展示字段
  （不传给 `_build_pipeline`）。
- Solution Architect agent 使用 langchain 的结构化输出
  （`model.with_structured_output(SpecificationSchema)`）生成 Spec。
- 后端收到 Spec 后：剥离展示字段 -> 组装成 `raw dict` -> 调用
  `_build_pipeline(raw, source=<workspace_dir>, extra_tools={})` 试编译校验，
  失败则把 `ConfigurationError` 信息**转成友好提示**反馈给 architect agent
  重新生成（自动修复循环，最多 N 次），而不是直接展示技术报错给客户。
- 这一层是整个向导"阶段3/4"的引擎，建议作为**第一个实现的、独立可验证的
  模块**（可以先写单元测试：给定一个 fake 的 Requirements，验证生成的 Spec
  能通过 `_build_pipeline` 校验）。

### Phase 1：持久化层

- SQLAlchemy + SQLite（按客户独立部署）。核心表：
  - `agents` / `teams` / `knowledge_bases` / `pipelines`：存最终生效的
    `raw` config（按 Spec 的技术字段落库）
  - **新增** `builder_sessions`：id, intent_text, as_is_text,
    requirements_json, specification_json（含友好字段）, status
    （intent/requirements/spec/solution/testing/deployed）,
    feedback_history（JSON 数组，记录每轮反馈）
  - `runs` / `trace_events`：替代内存版 `RunRegistry`（Phase 5）
  - `usage_records`：按 run 记录 token 用量与估算成本
  - `users`：每部署内简单登录
- KB 上传文档落盘到 `data/<workspace>/kb/<kb_id>/`。

### Phase 2：后端 API

- Builder session 状态机 API：
  - `POST /api/builder/sessions`（开始，提交 Intent/As-is）
  - `POST /api/builder/sessions/{id}/requirements`（生成/确认 Requirements）
  - `POST /api/builder/sessions/{id}/specification`（生成/重新生成 Spec）
  - `POST /api/builder/sessions/{id}/solution`（提交微调反馈）
  - `POST /api/builder/sessions/{id}/test-runs`（沙盒执行，复用
    `Pipeline.stream`）
  - `POST /api/builder/sessions/{id}/deploy`
- CRUD API（`agents`/`teams`/`knowledge_bases`/`pipelines`）作为"高级视角"，
  供已部署配置的精细调整，复用同一套 `_build_pipeline` 校验。
- `_get_pipeline()`（`ui/backend/main.py`）改为优先从 DB 组装 `raw` 并调用
  loader。

### Phase 3：登录 + 模型目录 + 用量计量

- 简单用户表 + session/JWT（按部署，不做跨租户隔离）。
- `model_catalog`：spec 字符串 ↔ 客户友好名称（如"高级助理（更聪明但更慢）"）
  ↔ 单价，供 Solution Architect 在生成 Spec 时按"角色复杂度"挑选模型，
  也供 Phase 5 用量计费换算。
- 在 `LangGraphAdapter` 的 agent 执行处提取 `usage_metadata`，写入
  `usage_records`。

### Phase 4：前端 —— 向导式 UI（六阶段）

- 引入 `react-router-dom`，新增页面对应六阶段（`/wizard/intent`、
  `/wizard/requirements`、`/wizard/team`（Specification "认识你的团队"）、
  `/wizard/refine`（Solution）、`/wizard/test`、`/wizard/deploy`）。
- 视觉语言：用"虚拟员工卡片"（头像占位 + 岗位名 + 一句话职责）、
  "团队协作流程图"（基于 Mermaid，复用已有 `Pipeline.visualize()`/
  `to_mermaid`，但渲染时隐藏技术细节，只标注岗位名）。
- 表单 CRUD 视图（`/agents`、`/teams`、`/knowledge-bases`、`/pipelines`）
  作为"高级设置"入口，默认折叠/隐藏。

### Phase 5：运行记录持久化 + 友好化 Trace 展示

- `RunRegistry`（`ui/backend/registry.py`）的内存存储改为落库
  （`runs`/`trace_events`），pub/sub 推送逻辑不变。
- 新增 `TraceEvent -> 友好活动卡片` 的映射层（前端或一个轻量后端转换函数）：
  例如 `agent_completed` -> "{display_name} 已完成：{摘要}"。这一层同时服务
  Testing 阶段的沙盒视图和 Deployment 后的"团队工作记录"视图。

### Phase 6（后续/可选，仅记录）

- **模板库**：从常见 Intent 模式（如"客服自动化"、"市场调研简报"）预置
  Specification 模板，加速 Solution Architect 的首次生成（few-shot/RAG）。
- 配额/预算告警、同部署内多角色权限、Spec 版本历史与回滚、多客户部署的代码
  更新分发策略、KB 上传安全校验、用量与 Provider 账单对账。

## 实施顺序建议

Phase 0 → Phase 0.5 → Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 5。
**Phase 0（HIERARCHICAL）和 Phase 0.5（结构化 Spec 生成与自校验）建议作为
最先启动的两个独立任务**：前者是"雇佣团队"隐喻的执行基石，后者是向导从
Requirements 走到 Specification 的核心引擎，且都可以在现有代码库上增量
开发、独立写单元测试验证，不依赖数据库/前端等后续基础设施。
