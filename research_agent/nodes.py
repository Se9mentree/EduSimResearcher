from research_agent.config import get_llm, load_agent_requirements
from research_agent.paper_parser import load_paper
from research_agent.rag import retrieve_related_papers
from research_agent.schemas import CriticOutput, PlannerOutput
from research_agent.state import AgentState
from research_agent.tools import RESEARCHER_TOOLS


AGENT_REQUIREMENTS = load_agent_requirements()
tools = RESEARCHER_TOOLS
tool_registry = {tool.name: tool for tool in tools}


def _build_paper_context(state: AgentState) -> str:
    abstract = state.get("input_paper_abstract", "").strip()
    sections = state.get("input_paper_sections", {})
    key_sections = state.get("input_paper_key_sections", {})
    fallback_text = state.get("input_paper", "").strip()

    section_blocks = []
    preferred_order = [
        "abstract",
        "introduction",
        "related_work",
        "method",
        "experiments",
        "evaluation",
        "results",
        "discussion",
        "limitations",
        "conclusion",
    ]
    seen = set()

    for section_name in preferred_order:
        if section_name in sections and sections[section_name].strip():
            section_blocks.append(f"[{section_name}]\n{sections[section_name].strip()}")
            seen.add(section_name)

    for section_name, section_text in sections.items():
        if section_name not in seen and isinstance(section_text, str) and section_text.strip():
            section_blocks.append(f"[{section_name}]\n{section_text.strip()}")

    key_blocks = []
    for section_name, section_text in key_sections.items():
        if isinstance(section_text, str) and section_text.strip():
            key_blocks.append(f"[{section_name}]\n{section_text.strip()}")

    parts = []
    if abstract:
        parts.append(f"[abstract]\n{abstract}")
    if section_blocks:
        parts.append("[sections]\n" + "\n\n".join(section_blocks))
    if key_blocks:
        parts.append("[key_sections]\n" + "\n\n".join(key_blocks))
    if fallback_text and not parts:
        parts.append("[raw_text]\n" + fallback_text)

    return "\n\n".join(parts)


def paper_ingest_node(state: AgentState):
    print("[Paper Ingest Node] Loading and parsing paper via MCP.")
    paper_path = state.get("paper_path", "").strip()
    if not paper_path:
        return {
            "input_paper_parse_status": "failed",
            "input_paper_parse_notes": "paper_path is empty.",
            "current_step": "paper_ingest",
            "working_memory": "Paper ingest failed: paper_path is empty.",
        }

    parsed_doc = load_paper(paper_path)
    return {
        "input_paper_title": parsed_doc.title,
        "input_paper": parsed_doc.full_text,
        "input_paper_abstract": parsed_doc.abstract,
        "input_paper_sections": parsed_doc.sections,
        "input_paper_key_sections": parsed_doc.key_sections,
        "input_paper_parse_status": parsed_doc.parse_status,
        "input_paper_parse_notes": parsed_doc.parse_notes,
        "current_step": "paper_ingest",
        "working_memory": f"Paper ingest status: {parsed_doc.parse_status}. {parsed_doc.parse_notes}",
    }


def paper_ingest_router(state: AgentState):
    if state.get("input_paper_parse_status") == "failed":
        return "end"
    return "planner"


def planner_node(state: AgentState):
    query = state["query"]
    input_paper_title = state.get("input_paper_title", "")
    input_paper_abstract = state.get("input_paper_abstract", "")
    paper_context = _build_paper_context(state)
    structured_planner_llm = get_llm().with_structured_output(PlannerOutput)
    prompt = f"""你是一个资深的学术研究员和任务拆解专家。
    你的任务是根据用户的研究主题，制定一个高度可执行的研究计划。
    你必须先认真阅读当前输入论文，再决定后续如何检索、比较和推进研究。
    
    用户的研究主题是: "{query}"
    当前输入论文标题是: "{input_paper_title}"
    当前输入论文摘要长度约为: "{len(input_paper_abstract)}" 字符
    
    【当前输入论文摘要】
    {input_paper_abstract if input_paper_abstract else "当前未提取到摘要。"}
    
    【当前输入论文结构化内容】
    {paper_context if paper_context else "当前未提取到可用的结构化论文内容。"}
    
    计划必须以“当前输入论文”为分析起点，优先考虑：
    1. 先识别这篇论文到底在研究什么，核心贡献、方法、实验、局限分别是什么
    2. 再决定需要去文献数据库或外部检索中补哪些参照论文
    3. 最后才形成综合判断和 future research insight
    
    请将任务拆解为 3 到 4 个具体的执行步骤。重点考虑如何：
    1. 结合当前输入论文识别它的研究定位、问题意识和方法类别
    2. 检索或调用本地论文库中的相关论文作为比较参照，而不是只分析单篇论文
    3. 围绕 education simulation、multi-agent、social behavior、interaction mechanism 提炼关键研究启发
    4. 形成可推动后续研究的 future research insight 和下一步建议
    
    计划必须服务于最终输出要求：先细读当前论文，再综合论文库中的相关论文做横向比较、研究定位、局限分析和未来研究方向判断。
    注意：在输出 description 时，请将细节写成连续的自然语言段落，绝对不要使用回车换行或项目符号！
    """
    try:
        response = structured_planner_llm.invoke(prompt)
        generated_plan = response.plan
        for i, step in enumerate(generated_plan):
            print(f"Step {i+1}: {step}")
        return {
            "plan": generated_plan,
            "revision_number": 0,
            "current_step": generated_plan[0] if generated_plan else "",
        }
    except Exception as e:
        print(f"Error in planner_node: {e}")
        return {
            "plan": [],
            "revision_number": 0,
            "current_step": "",
        }


def researcher_node(state: AgentState):
    print("[Researcher Node] Executing the research step.")
    query = state["query"]
    plan_list = state["plan"]
    paper_path = state.get("paper_path", "").strip()
    input_paper_title = state.get("input_paper_title", "")
    input_paper_abstract = state.get("input_paper_abstract", "")
    paper_context = _build_paper_context(state)
    plan_text = "\n".join(plan_list)
    researcher_llm = get_llm().bind_tools(tools)

    retrieved_papers = retrieve_related_papers(
        query=query,
        input_paper=paper_context or input_paper_abstract,
    )
    rag_context = "\n\n".join(retrieved_papers)

    prompt = f"""你是一个硬核的学术研究证据提取 Agent。
    你的任务不是联网搜索，而是必须通过 PDF-MCP 工具从当前输入论文中抽取高质量证据，服务后续写作与审稿。
    
    【研究计划】
    {plan_text}
    
    【当前输入论文标题】
    {input_paper_title}

    【当前输入论文摘要】
    {input_paper_abstract if input_paper_abstract else "当前未提取到摘要。"}
    
    【当前输入论文结构化内容】
    {paper_context if paper_context else "当前未提取到可用的结构化论文内容。"}

    【当前输入论文路径】
    {paper_path if paper_path else "当前缺失 paper_path"}
    
    【本地论文库召回结果】
    {rag_context if rag_context else "当前未接入本地论文库检索结果。"}
    
    你必须优先从当前论文中抽取以下信息：
    1. 论文研究对象和场景
    2. 方法关键词和技术路线
    3. 评估方式和实验设置
    4. 与 education simulation / multi-agent / social behavior / interaction mechanism 最相关的概念
    
    你可以使用的工具只有：
    - extract_academic_text_tool
    - extract_abstract_tool
    - detect_sections_tool
    - extract_key_sections_tool
    - extract_citations_tool

    规则：
    1. 明确禁止联网检索，不要调用任何 web/search 类能力。
    2. 必须调用 2 到 4 个工具来抽取证据。
    3. 工具参数里必须传入 paper_path（若你省略，系统会自动补齐）。
    4. 输出前先完成工具调用，不能空想或编造证据。
    """
    response = researcher_llm.invoke(prompt)
    generated_documents = []
    tool_traces = []
    tool_failures = []
    default_tools = ["extract_abstract_tool", "extract_key_sections_tool"]

    def invoke_research_tool(tool_name: str, raw_args: dict):
        tool_obj = tool_registry.get(tool_name)
        if not tool_obj:
            return {
                "ok": False,
                "reason": "unknown_tool",
                "content": f"Unknown tool: {tool_name}",
                "args": raw_args,
            }
        tool_args = dict(raw_args) if isinstance(raw_args, dict) else {}
        if not tool_args.get("paper_path"):
            tool_args["paper_path"] = paper_path
        tool_traces.append(f"{tool_name}(paper_path={tool_args.get('paper_path', '')})")
        result = str(tool_obj.invoke(tool_args))
        if result.startswith("[MCP_ERROR]"):
            return {"ok": False, "reason": "mcp_error", "content": result, "args": tool_args}
        if result.startswith("[MCP_WARN]"):
            return {"ok": False, "reason": "mcp_warn", "content": result, "args": tool_args}
        return {"ok": True, "reason": "success", "content": result, "args": tool_args}

    if response.tool_calls:
        for tool_call in response.tool_calls:
            print(f"   🤖 [大模型决定行动]: 调用工具 '{tool_call['name']}'")
            print(f"   🎯 [生成的工具参数]: {tool_call.get('args', {})}")
            invoke_result = invoke_research_tool(tool_call["name"], tool_call.get("args", {}))
            used_args = invoke_result["args"]
            if invoke_result["ok"]:
                tool_result = invoke_result["content"]
                doc_snippet = f"【工具: {tool_call['name']}】\n参数: {used_args}\n{tool_result}\n---"
                generated_documents.append(doc_snippet)
                print(f"   📄 [工具返回成功]: 截取到了 {len(tool_result)} 个字符的数据。")
            else:
                failure_note = f"{tool_call['name']}: {invoke_result['content']}"
                tool_failures.append(failure_note)
                print(f"   ⚠️ [工具结果未纳入证据]: {failure_note}")
    else:
        print("   ⚠️ [警告]: 大模型没有调用任何工具，将回退执行默认论文工具。")

    if not generated_documents and paper_path:
        for default_tool in default_tools:
            invoke_result = invoke_research_tool(default_tool, {"paper_path": paper_path})
            used_args = invoke_result["args"]
            if invoke_result["ok"]:
                tool_result = invoke_result["content"]
                doc_snippet = f"【工具: {default_tool}】\n参数: {used_args}\n{tool_result}\n---"
                generated_documents.append(doc_snippet)
                print(f"   ↻ [回退调用]: {default_tool} 返回 {len(tool_result)} 个字符。")
            else:
                failure_note = f"{default_tool}: {invoke_result['content']}"
                tool_failures.append(failure_note)
                print(f"   ⚠️ [回退工具失败]: {failure_note}")

    if rag_context:
        generated_documents.insert(0, f"【本地论文库相关论文】\n{rag_context}\n---")

    working_memory = (
        f"Researcher extracted {len(generated_documents)} evidence blocks."
        if not tool_failures
        else f"Researcher extracted {len(generated_documents)} evidence blocks with {len(tool_failures)} tool issues."
    )

    return {
        "documents": generated_documents,
        "searched_queries": tool_traces,
        "retrieved_papers": retrieved_papers,
        "rag_context": rag_context,
        "evidence_gaps": tool_failures,
        "working_memory": working_memory,
        "current_step": "researcher",
    }


def writer_node(state: AgentState):
    print("[Writer Node] Drafting the response based on the research findings.")
    query = state["query"]
    plan = state["plan"]
    documents = state["documents"]
    critic = state["critic"]
    input_paper = state.get("input_paper", "")
    input_paper_title = state.get("input_paper_title", "")
    input_paper_abstract = state.get("input_paper_abstract", "")
    paper_context = _build_paper_context(state)
    rag_context = state.get("rag_context", "")

    plan_text = "\n".join(plan)
    documents_text = "\n".join(documents)
    prompt = f"""{AGENT_REQUIREMENTS}

    现在请严格基于下面的输入，输出符合上述固定结构的研究性分析。

    【研究主题】
    {query}
    
    【当前输入论文标题】
    {input_paper_title}
    
    【当前输入论文摘要】
    {input_paper_abstract if input_paper_abstract else "当前未提取到摘要。"}
    
    【当前输入论文结构化内容】
    {paper_context if paper_context else (input_paper if input_paper else "用户当前尚未提供论文正文。请仅基于已知信息和检索结果分析，并明确不确定处。")}
    
    【研究计划】
    {plan_text}
    
    【检索到的文献资料与论文库相关内容】
    {documents_text if documents_text else "暂无检索结果。"}
    
    【论文库相关论文摘要上下文】
    {rag_context if rag_context else "当前没有来自本地论文库的额外上下文。"}
    
    【来自上一轮审稿人的反馈】
    {critic if critic else "暂无"}
    
    写作优先级要求：
    1. 先认真阅读并分析“当前输入论文内容”，不能把它当成可有可无的背景材料。
    2. 先回答“这篇当前论文到底做了什么、解决了什么、贡献和局限是什么”。
    3. 再结合“检索到的文献资料与论文库相关内容”做横向比较、文献版图定位和 future research insight 提炼。
    4. 必须明确区分：
       - 当前论文本身的内容与贡献
       - 数据库/检索到的相关论文提供的参照与比较
       - 你基于两者综合得到的研究判断与未来方向
    
    额外要求：
    1. 严格基于上述材料分析，不能编造论文数量、作者、机构、DOI、实验结果。
    2. 如果论文库信息不足，请明确写“资料不足，需要进一步研究”。
    3. 你的目标是推进研究，而不是写泛泛的论文摘要。
    4. 不能只围绕当前单篇论文展开，必须尽量结合“检索到的文献资料与论文库相关内容”做横向比较、文献版图定位和 future research insight 提炼。
    5. 请特别输出对我后续研究真正有帮助的内容：
       - 当前论文在相关文献中的位置
       - 与相近论文相比的独特价值或重复之处
       - 最值得推进的 1 到 3 个未来研究方向
       - 这些方向更适合 individual learner、classroom simulation 还是 agent campus
    6. 不能跳过当前输入论文直接写综合综述；如果当前论文信息不完整，请明确指出这会如何影响判断。
    7. 如果证据不足，不要硬编，请明确指出缺了哪些文献支撑或比较依据。
    """
    try:
        response = get_llm().invoke(prompt)
        print(f"   📝 [草稿完成]: 共生成了 {len(response.content)} 个字符。")
        return {
            "draft": response.content,
            "current_step": "writer",
        }
    except Exception as e:
        print(f"Error in writer_node: {e}")
        return {
            "draft": "writer节点执行失败，无法生成草稿。",
            "current_step": "writer",
        }


def critic_node(state: AgentState):
    print("[Critic Node] Reviewing the draft and providing feedback.")
    query = state["query"]
    draft = state["draft"]
    input_paper = state.get("input_paper", "")
    input_paper_title = state.get("input_paper_title", "")
    input_paper_abstract = state.get("input_paper_abstract", "")
    paper_context = _build_paper_context(state)
    documents = state.get("documents", [])
    rag_context = state.get("rag_context", "")
    current_rev = state.get("revision_number", 0)
    documents_text = "\n".join(documents)
    structured_critic_llm = get_llm().with_structured_output(CriticOutput)

    prompt = f"""你是一个极其严苛的学术期刊主编。
    请审查以下研究报告草稿，判断它是否真正完成了用户的研究任务。
    你必须同时认真阅读当前输入论文、相关论文材料和草稿，进行对照式审稿。

    【用户的研究目标】
    {query}
    
    【当前输入论文标题】
    {input_paper_title}
    
    【当前输入论文摘要】
    {input_paper_abstract if input_paper_abstract else "当前未提取到摘要。"}
    
    【当前输入论文结构化内容】
    {paper_context if paper_context else (input_paper if input_paper else "当前未提供论文正文。")}
    
    【相关论文与检索结果】
    {documents_text if documents_text else "暂无检索结果。"}
    
    【论文库相关论文摘要上下文】
    {rag_context if rag_context else "当前没有来自本地论文库的额外上下文。"}
    
    【待审查的草稿】
    {draft}
    
    请重点检查：
    1. 是否遵守了用户在 agent要求.md 中给定的固定结构和研究性分析目标。
    2. 是否真正围绕 education simulation、multi-agent、social behavior、interaction mechanism 展开。
    3. 是否真的认真分析了当前输入论文本身，包括问题、方法、贡献、实验、局限，而不是一笔带过。
    4. 是否区分“作者写了什么”和“我应该怎么看这篇论文”。
    5. 是否综合利用了当前输入论文与论文库/检索到的相关论文，而不是只做单篇论文摘要或只做泛泛综述。
    6. 是否真正给出了更进一步的 future research insight，而不是只停留在总结和批评。
    7. 是否严格基于当前输入论文与相关论文材料，没有凭空编造事实。
    8. 如果证据不足，是否明确指出并要求回 researcher，而不是逼 writer 硬写。

    输出规则：
    1. summary 必须简洁明确，说明通过或不通过的核心原因。
    2. missing_evidence 只写证据缺口。
    3. missing_topics 只写主题缺口，包括“没有认真分析当前论文”这类缺口。
    4. revision_actions 必须是下一轮可执行动作。
    5. 如果缺的是相关论文比较、文献版图定位、future research insight 支撑，请优先返回 researcher。
    6. next_step 只能返回 finish、writer、researcher 三选一。
    """

    try:
        response = structured_critic_llm.invoke(prompt)
        print(f"   [审查结果]: {'✅ 合格' if response.is_acceptable else '❌ 打回重做'}")
        print(f"   [审稿意见]: {response.summary}")

        critic_summary = (
            f"总评：{response.summary}；"
            f"缺失证据：{'；'.join(response.missing_evidence) if response.missing_evidence else '无'}；"
            f"缺失主题：{'；'.join(response.missing_topics) if response.missing_topics else '无'}；"
            f"修改动作：{'；'.join(response.revision_actions) if response.revision_actions else '无'}。"
        )

        next_step = response.next_step if response.next_step in {"finish", "writer", "researcher"} else "writer"
        next_revision = current_rev if response.is_acceptable else current_rev + 1

        return {
            "critic": critic_summary,
            "critic_next_step": next_step,
            "revision_number": next_revision,
            "revision_history": [response.summary],
            "evidence_gaps": response.missing_evidence + response.missing_topics,
            "working_memory": critic_summary,
            "current_step": "critic",
        }
    except Exception as e:
        print(f"Error in critic_node: {e}")
        fallback_summary = "审稿失败，默认进入 writer 节点继续修订。"
        return {
            "critic": fallback_summary,
            "critic_next_step": "writer",
            "revision_number": current_rev + 1,
            "revision_history": [fallback_summary],
            "evidence_gaps": ["critic节点执行失败，当前审稿结果不可用。"],
            "working_memory": fallback_summary,
            "current_step": "critic",
        }


def reflection_router(state: AgentState):
    if state.get("critic_next_step") == "finish":
        return "end"
    if state.get("critic_next_step") == "researcher":
        return "researcher"
    return "writer"
