# Social Simulation & MAS 项目技术面试问答（基于当前 Demo）

> 适用场景：你简历上写了 **Social Simulation & Multi-Agent Systems (MAS)** 项目，面试官会重点验证“你到底做了什么、怎么做、为什么这么做、有什么结果和边界”。

---

## 1. 30 秒项目总述（开场可直接说）

我做的是一个面向论文研究场景的 agent workflow，主链路是 `paper_ingest -> planner -> researcher(ReAct) -> writer -> critic`。  
输入是一篇 PDF，先通过 MCP 解析成结构化论文内容，再由 researcher 调 MCP 工具和本地 RAG 检索证据，writer 生成结构化+可读草稿，critic 用硬/软门槛审稿并给 researcher/writer 分别下发修订动作。  
核心价值是让“证据采集-写作-审稿”形成可追溯闭环，而不是一次性生成。

---

## 2. 面试官高频问题 + 你可以怎么答

## Q1 这个项目里，哪些部分是你亲手设计和实现的？
**面试官想看**： ownership 和工程深度。  
**你可以答**：  
我主要负责 workflow 编排、状态设计、MCP 接入层、RAG 入库与检索、以及 critic 的门槛化审稿逻辑。  
具体实现上，我把 PDF 解析抽象到 `paper_parser`，把 researcher 改成了 ReAct 多步取证，并把 critic 做成硬门槛/软建议分层，支持 `finish_with_risks` 这种可解释结束语义。

## Q2 你说是 Multi-Agent，但看起来像一个工作流，怎么解释？
**面试官想看**：概念是否清楚。  
**你可以答**：  
当前 demo 本质是**单执行体的多角色工作流**，还不是“多个自治 agent 并行协商”的强多智能体系统。  
我把 planner/researcher/writer/critic 角色拆开，是为了先验证闭环和评审机制；下一步才会把 critic/researcher 拆成独立策略体并引入共享记忆与协商协议，走向真正 MAS。

## Q3 为什么要引入 MCP，而不是直接在代码里读 PDF？
**面试官想看**：架构决策能力。  
**你可以答**：  
MCP 让我把“agent 编排逻辑”和“文档解析能力”解耦。  
好处是工具能力可替换、跨进程调用标准化、错误语义可统一；代价是要做一层 client 适配与结果标准化，这部分我在 `paper_parser` 里做了。

## Q4 你如何保证 writer 不胡编？
**面试官想看**：可控生成。  
**你可以答**：  
我用了三层约束：  
1. `evidence_cards` 机制：writer 的核心 claim 必须绑定 `evidence_id`。  
2. prompt 约束：无证据必须输出“资料不足，需要进一步研究”。  
3. critic 硬门槛：`no_hallucination/evidence_alignment/paper_coverage` 不过就打回。  
此外我避免把工具错误写入 documents，减少证据池污染。

## Q5 ReAct 在你项目里具体怎么工作？
**面试官想看**：是否真的懂 ReAct，不是口号。  
**你可以答**：  
researcher 每轮执行 `Thought -> Action(单工具) -> Observation`，有固定步数预算和止损条件。  
我实现了重复签名检测、低信息增益停止、工具错误阈值停止，并记录 `react_trace` 和 `stop_reason` 用于复盘。

## Q6 你怎么做 RAG？为什么选 Chroma？
**面试官想看**：检索与存储设计。  
**你可以答**：  
我用了本地 Chroma 持久化向量库，先把论文按 section + chunk 入库，metadata 带 `paper_id/title/source_path/section/chunk_index`。  
检索时返回带 score 的证据片段，便于 writer/critic 做可追溯引用。  
选 Chroma 是因为本地化成本低、开发速度快，适合先把闭环跑通。

## Q7 你当前 RAG 的短板是什么？
**面试官想看**：你是否清楚自己系统的边界。  
**你可以答**：  
当前主要是 dense similarity，关键词精确召回和重排能力不够，容易出现“语义接近但任务不相关”的命中。  
下一步我会做 hybrid（dense+BM25）+ rerank，并加 paper-level 去重和短 chunk 过滤。

## Q8 你如何处理“critic 太严格导致系统卡死”？
**面试官想看**：系统稳定性思维。  
**你可以答**：  
我把 critic 改成硬/软门槛分级：真实性相关是硬门槛，方法深度等是软建议。  
到达修订上限时不强行判通过，而是 `finish_with_risks`，并输出 `unresolved_gaps`，避免“看起来通过但实际不可靠”。

## Q9 你做了哪些反失败设计（failure handling）？
**面试官想看**：生产可用性意识。  
**你可以答**：  
1. ingest fail-fast：PDF 解析失败直接结束，不进入写作。  
2. MCP 错误统一前缀 + 过滤，不污染证据池。  
3. critic 解析失败有 fallback 状态，不让流程崩溃。  
4. 输出里保留 stop reason、failed gates、unresolved gaps，便于诊断。

## Q10 你如何证明“不是只会 prompt”？
**面试官想看**：工程实现而非纯提示词。  
**你可以答**：  
我做了不少非 prompt 层工作：状态模型重构、路由和门槛语义、MCP client 适配、RAG 入库 CLI、证据去重、ReAct 止损策略、报告生成与可追踪字段设计。  
这些都是为了让系统可迭代、可调试、可解释。

## Q11 你怎么评估这个系统“好不好”？
**面试官想看**：指标意识。  
**你可以答**：  
我会看三组指标：  
1. 过程指标：每轮工具调用成功率、ReAct 平均步数、重复调用率。  
2. 质量指标：critic 硬门槛通过率、evidence-claim 对齐率、幻觉率。  
3. 成本指标：token 消耗、平均运行时长、单篇论文处理成本。

## Q12 你的“最难问题”是什么，怎么解的？
**面试官想看**：问题解决能力。  
**你可以答**：  
最难的是“越循环越不过审”。原因不是单点 bug，而是 planner/researcher/writer/critic 联动失衡。  
我通过三步修复：  
1. researcher 从拼接文本改为证据卡导向；  
2. critic 从单一拒绝改为硬/软门槛和角色分流；  
3. 加 ReAct 止损与报告语义统一。  
结果是系统从不可解释循环变成可诊断闭环。

---

## 3. 追问题（面试官常用）与建议回答

## 追问 A：你这个系统现在能算 production-ready 吗？
建议回答：  
还不能。当前更像 research engineering demo。  
距离 production 还差：更稳的检索与重排、并发任务调度、权限与审计、持续评测基准、以及更严格的异常恢复和监控。

## 追问 B：如果给你 2 周，你先做什么？
建议回答：  
我会优先做三件事：  
1. RAG：hybrid + rerank，提升证据质量；  
2. 评测：建立固定评测集和门槛回归测试；  
3. 架构：把 critic/researcher 拆成真正的双 agent 协同，补短期记忆与任务缓存。

## 追问 C：你如何避免“把不存在的文献当证据”？
建议回答：  
我要求 claim 必须绑定 evidence_id，evidence_id 只能来源于工具返回或 RAG 命中；  
writer 无证据只能输出“资料不足”；  
critic 再做 evidence_alignment gate 二次校验，阻断伪引用。

---

## 4. 你可以主动强调的亮点（加分）

- 我不是只做了“生成”，而是做了**证据驱动的可追溯闭环**。  
- 我把系统失败语义做了工程化表达：`pass / pass_with_warnings / fail / finish_with_risks`。  
- 我愿意明确讲边界：当前是单执行体多角色，不夸大成 fully distributed MAS。  
- 我的下一步路线清晰：检索质量、评测基线、真正多 agent 协作。

---

## 5. 面试时避免踩坑的话术

- 不要说“这个系统已经很准确”。改说“当前在可解释性和可控性上优先，准确性还在通过评测集持续优化”。  
- 不要把“角色拆分”直接等同“多智能体”。改说“这是向多智能体演进的中间架构”。  
- 不要回避不足。主动讲“已知短板 + 已实施修复 + 下一步计划”，反而更专业。

