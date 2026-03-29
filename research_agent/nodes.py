import json
import re
from difflib import SequenceMatcher
from typing import Any, Dict, List

from research_agent.config import (
    MAX_REVISIONS,
    REACT_MAX_STEPS,
    REACT_MAX_TOOL_ERRORS,
    REACT_REPEAT_ACTION_LIMIT,
    LOW_GAIN_MIN_NEW_CHARS,
    LOW_GAIN_REPEAT_LIMIT,
    RAG_GOAL9_HARD_MIN_HITS,
    EVIDENCE_GAPS_TOP_N,
    RAG_TOP_K,
    get_llm,
    load_agent_requirements,
)
from research_agent.paper_parser import load_paper
from research_agent.schemas import CriticContextNeed, CriticOutput, PlannerOutput, WriterStructuredOutput
from research_agent.state import AgentState
from research_agent.tools import RESEARCHER_TOOLS


AGENT_REQUIREMENTS = load_agent_requirements()
tools = RESEARCHER_TOOLS
tool_registry = {tool.name: tool for tool in tools}

TOP_REQUIREMENTS_CONTRACT = """
【TOP_REQUIREMENTS_CONTRACT】
研究背景：
1. 目标方向为教育 simulation，包含 individual learner / classroom / agent campus 三层级。
2. 导师关注点是 multi-agent、social behavior、interaction mechanism。
3. 输出必须区分“作者写了什么”与“我该如何判断”。

分析目标（Goal-1 到 Goal-9）：
Goal-1 论文核心问题与重要性
Goal-2 作者声称贡献 vs 真实研究价值
Goal-3 方法类别与机制归类（含agent/environment/memory/tool/interaction）
Goal-4 与用户研究方向关系与研究定位
Goal-5 在教育多智能体社交行为模拟图景中的位置
Goal-6 最可借鉴点与可迁移边界
Goal-7 局限与选题提醒
Goal-8 是否适合作为综述/组会/复现/baseline参考
Goal-9 结合相关论文提炼研究空白、趋势与future direction

分析原则：
1. 严禁编造事实；无证据必须显式写“资料不足，需要进一步研究”。
2. 结论必须可追溯到证据（当前论文或论文库相关论文）。
3. 迁移到教育场景时必须给出“可迁移/不可迁移/条件迁移”判断。
4. 目标覆盖率仅用于结构化核验，不得在可读长文里使用“✅ Goal-x”自证通过。
"""

CRITIC_HARD_GATES = {
    "no_hallucination",
    "paper_coverage",
    "core_idea_clarity",
    "goal_1_to_8_coverage",
    "evidence_alignment",
    "anti_self_report",
    "critic_json_validity",
}
CRITIC_SOFT_GATES = {
    "comparative_quality",
    "method_mechanism_depth",
    "method_detail_completeness",
    "experiment_protocol_depth",
    "result_critical_balance",
    "motivation_overweight",
    "anti_self_report_soft_warning",
}


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


def _to_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _clip_text(text: str, limit: int) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 3)] + "..."


def _dedupe_keep_order(items: List[str]) -> List[str]:
    seen = set()
    deduped = []
    for item in items:
        key = str(item).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(key)
    return deduped


def _normalize_text_key(text: str) -> str:
    normalized = str(text or "").strip().lower()
    normalized = normalized.replace("（", "(").replace("）", ")").replace("：", ":")
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = re.sub(r"^[\-\*\d\.\)\(]+\s*", "", normalized)
    return normalized.strip()


def _dedupe_by_normalized_key(items: List[str], max_per_key: int = 1) -> List[str]:
    key_counts: Dict[str, int] = {}
    deduped = []
    for item in items:
        raw = str(item).strip()
        if not raw:
            continue
        key = _normalize_text_key(raw)
        if not key:
            continue
        used = key_counts.get(key, 0)
        if used >= max_per_key:
            continue
        key_counts[key] = used + 1
        deduped.append(raw)
    return deduped


def _limit_items(items: List[str], limit: int) -> List[str]:
    if limit <= 0:
        return []
    return (items or [])[:limit]


def _dedupe_limit_items(items: List[str], limit: int, max_per_key: int = 1) -> List[str]:
    return _limit_items(_dedupe_by_normalized_key(items, max_per_key=max_per_key), limit)


def _split_critic_failed_gates(failed_gates: List[str]) -> Dict[str, List[str]]:
    hard = []
    soft = []
    for gate in _dedupe_keep_order(failed_gates):
        if gate in CRITIC_HARD_GATES:
            hard.append(gate)
        else:
            # 未注册门槛默认作为软失败，避免误伤主流程。
            soft.append(gate)
    return {"hard": hard, "soft": soft}


def _estimate_information_gain(observation: str, history: List[str]) -> Dict[str, float]:
    text = str(observation or "").strip()
    if not text:
        return {"novelty_score": 0.0, "estimated_new_chars": 0.0}
    if not history:
        return {"novelty_score": 1.0, "estimated_new_chars": float(len(text))}
    similarities = []
    for prev in history:
        prev_text = str(prev or "").strip()
        if not prev_text:
            continue
        similarities.append(SequenceMatcher(None, text, prev_text).ratio())
    max_similarity = max(similarities) if similarities else 0.0
    novelty_score = max(0.0, 1.0 - max_similarity)
    estimated_new_chars = float(len(text) * novelty_score)
    return {
        "novelty_score": round(novelty_score, 4),
        "estimated_new_chars": estimated_new_chars,
    }


def _extract_goal_ids_from_claims(writer_claims: List[Dict[str, Any]]) -> List[str]:
    found = []
    for claim in writer_claims or []:
        if not isinstance(claim, dict):
            continue
        for goal in claim.get("goal_ids", []) or []:
            g = str(goal).strip()
            if re.fullmatch(r"Goal-\d+", g):
                found.append(g)
    return _dedupe_keep_order(found)


def _split_rag_chunks(retrieved_papers: List[str], rag_context: str) -> List[str]:
    chunks = [str(x).strip() for x in (retrieved_papers or []) if str(x).strip()]
    if chunks:
        return chunks
    context = str(rag_context or "").strip()
    if not context:
        return []
    parts = [p.strip() for p in re.split(r"\n\s*\n(?=\[RAG#\d+\])", context) if p.strip()]
    if parts:
        return parts
    return [context]


def _is_education_multiagent_relevant(text: str) -> bool:
    sample = str(text or "").lower()
    if not sample:
        return False
    education_keywords = [
        "education",
        "learning",
        "classroom",
        "student",
        "teacher",
        "teaching",
        "curriculum",
        "learner",
        "school",
        "k-12",
        "k12",
        "higher education",
        "pedagog",
        "教育",
        "学习",
        "课堂",
        "学生",
        "教师",
        "教学",
        "课程",
        "校园",
    ]
    mas_keywords = [
        "multi-agent",
        "multi agent",
        "mas",
        "agent society",
        "social simulation",
        "social behavior",
        "interaction",
        "peer",
        "cooperation",
        "negotiation",
        "coordination",
        "society",
        "智能体",
        "多智能体",
        "社交",
        "交互",
        "协作",
        "仿真",
        "模拟",
    ]
    return any(k in sample for k in education_keywords) and any(k in sample for k in mas_keywords)


def _compute_rag_hit_stats(retrieved_papers: List[str], rag_context: str) -> Dict[str, int]:
    chunks = _split_rag_chunks(retrieved_papers, rag_context)
    total_hits = len(chunks)
    relevant_hits = sum(1 for chunk in chunks if _is_education_multiagent_relevant(chunk))
    return {"total_hits": total_hits, "relevant_hits": relevant_hits}


def _split_actions_for_roles(
    actions: List[str], hard_failed_gates: List[str], soft_failed_gates: List[str]
) -> Dict[str, List[str]]:
    hard_set = set(hard_failed_gates or [])
    soft_set = set(soft_failed_gates or [])
    researcher_actions = []
    writer_actions = []

    researcher_priority_gates = {
        "no_hallucination",
        "paper_coverage",
        "evidence_alignment",
        "goal_1_to_8_coverage",
        "goal_9_coverage",
        "comparative_quality",
        "method_detail_completeness",
        "experiment_protocol_depth",
    }
    writer_priority_gates = {
        "core_idea_clarity",
        "anti_self_report",
        "anti_self_report_soft_warning",
        "method_mechanism_depth",
        "result_critical_balance",
        "motivation_overweight",
    }
    all_failed = hard_set | soft_set
    researcher_required = bool(all_failed & researcher_priority_gates)
    writer_required = bool(all_failed & writer_priority_gates)

    for action in actions or []:
        text = str(action).strip()
        lower = text.lower()
        if not text:
            continue

        research_hit = any(
            k in lower
            for k in [
                "证据",
                "检索",
                "rag",
                "相关论文",
                "文献",
                "baseline",
                "对照",
                "实验协议",
                "数据",
                "补齐 goal",
                "goal-9",
            ]
        )
        writer_hit = any(
            k in lower
            for k in [
                "重写",
                "改写",
                "压缩",
                "篇幅",
                "结构",
                "表达",
                "开头新增",
                "去掉",
                "自证文本",
                "核心思路",
                "补写",
            ]
        )

        if research_hit and writer_hit:
            researcher_actions.append(text)
            writer_actions.append(text)
            continue
        if research_hit:
            researcher_actions.append(text)
            continue
        if writer_hit:
            writer_actions.append(text)
            continue

        # 阶段1：门槛优先路由（无关键词时按失败门槛分配）
        if researcher_required and not writer_required:
            researcher_actions.append(text)
            continue
        if writer_required and not researcher_required:
            writer_actions.append(text)
            continue
        if researcher_required and writer_required:
            researcher_actions.append(text)
            writer_actions.append(text)
            continue

        # 阶段2兜底：默认给 writer，避免建议无消费方。
        writer_actions.append(text)

    if not researcher_actions and researcher_required:
        researcher_actions = list(actions or [])
    if not writer_actions and writer_required:
        writer_actions = list(actions or [])

    return {
        "researcher": _dedupe_by_normalized_key(researcher_actions),
        "writer": _dedupe_by_normalized_key(writer_actions),
    }


def _contains_negated_self_report(text: str) -> bool:
    if not text:
        return False
    patterns = [
        r"不要写\s*✅?\s*goal",
        r"去掉\s*✅?\s*goal",
        r"未使用.*覆盖率",
        r"避免.*自证",
        r"不得.*自证",
        r"not\s+use\s+.*goal",
        r"do\s+not\s+write\s+.*goal",
        r"remove\s+.*coverage",
    ]
    lower_text = text.lower()
    return any(re.search(p, lower_text, flags=re.IGNORECASE) for p in patterns)


def _has_explicit_positive_self_report(text: str) -> bool:
    if not text:
        return False
    patterns = [
        r"✅\s*goal-\d+",
        r"goal-\d+\s*(全部)?覆盖",
        r"覆盖率\s*100\s*%",
        r"全部(门槛|目标).*(通过|满足)",
        r"可直接交付",
        r"无需修订",
        r"all\s+goals?\s+covered",
        r"coverage\s*[:：]?\s*1\.?0+",
        r"ready\s+to\s+deliver",
    ]
    lower_text = text.lower()
    return any(re.search(p, lower_text, flags=re.IGNORECASE) for p in patterns)


def _detect_explicit_self_report(text: str) -> bool:
    if not text:
        return False
    if _contains_negated_self_report(text):
        return False
    return _has_explicit_positive_self_report(text)


def _truncate_middle(text: str, limit: int) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    if limit <= 12:
        return cleaned[:limit]
    half = (limit - 5) // 2
    return cleaned[:half] + " ... " + cleaned[-half:]


def _compact_writer_claims(writer_claims: List[Dict], max_claims: int = 12, statement_limit: int = 200) -> List[Dict]:
    compact = []
    for claim in (writer_claims or [])[:max_claims]:
        if not isinstance(claim, dict):
            continue
        compact.append(
            {
                "claim_id": claim.get("claim_id", ""),
                "statement": _clip_text(str(claim.get("statement", "")), statement_limit),
                "evidence_ids": claim.get("evidence_ids", []),
                "goal_ids": claim.get("goal_ids", []),
                "certainty": claim.get("certainty", ""),
            }
        )
    return compact


def _compact_evidence_cards(
    evidence_cards: List[Dict], max_cards: int = 12, snippet_limit: int = 180
) -> List[Dict]:
    compact = []
    for card in (evidence_cards or [])[:max_cards]:
        if not isinstance(card, dict):
            continue
        compact.append(
            {
                "id": card.get("id", ""),
                "source_type": card.get("source_type", ""),
                "source_ref": card.get("source_ref", ""),
                "goal_ids": card.get("goal_ids", ""),
                "confidence": card.get("confidence", ""),
                "snippet": _clip_text(str(card.get("snippet", "")), snippet_limit),
            }
        )
    return compact


def _extract_between(text: str, start_tokens: List[str], end_tokens: List[str]) -> str:
    source = text or ""
    start_index = -1
    start_len = 0
    for token in start_tokens:
        idx = source.find(token)
        if idx != -1 and (start_index == -1 or idx < start_index):
            start_index = idx
            start_len = len(token)
    if start_index == -1:
        return ""
    body = source[start_index + start_len :]
    end_index = len(body)
    for token in end_tokens:
        idx = body.find(token)
        if idx != -1 and idx < end_index:
            end_index = idx
    return body[:end_index].strip()


def _critic_hard_fail_checks(draft: str) -> List[str]:
    hard_fails = []
    if "三、方法框架与技术路线" not in draft:
        hard_fails.append("core_idea_clarity")
        return hard_fails

    section_three = _extract_between(
        draft,
        ["三、方法框架与技术路线"],
        ["四、", "### 四、", "#### 四、"],
    )
    if len(section_three) < 350:
        hard_fails.append("core_idea_clarity")
    required_keywords = ["核心思路", "输入", "模块", "输出", "机制"]
    if sum(1 for k in required_keywords if k in section_three) < 3:
        hard_fails.append("core_idea_clarity")
    if "[E" not in draft:
        hard_fails.append("evidence_alignment")
    if _detect_explicit_self_report(draft):
        hard_fails.append("anti_self_report")
    return sorted(set(hard_fails))


def _method_depth_audit(draft: str) -> Dict[str, Any]:
    gate = "method_mechanism_depth"
    section = _extract_between(
        draft,
        ["三、方法框架与技术路线", "### 三、方法框架与技术路线", "#### 三、方法框架与技术路线"],
        ["四、", "### 四、", "#### 四、"],
    )
    if not section.strip():
        return {
            "gate": gate,
            "score": 0,
            "failed": True,
            "revision_actions": [
                "Goal-3: 补齐“三、方法框架与技术路线”并给出阶段化机制链（输入->处理->筛选/更新->输出），每段绑定证据ID。"
            ],
            "evidence_gaps": ["缺少方法框架与技术路线主体内容，无法判断机制链完整性。"],
        }

    lower_text = section.lower()
    stage_hits = sum(
        len(re.findall(pattern, section, flags=re.IGNORECASE))
        for pattern in [
            r"步骤\s*[1-9]",
            r"step[- ]?[1-9]",
            r"阶段\s*[1-9]",
            r"第[一二三四五六七八九]步",
            r"[一二三四五六七八九]、",
            r"（[一二三四五六七八九]）",
        ]
    )
    arrow_count = section.count("->") + section.count("→")
    stage_estimate = max(stage_hits, (arrow_count + 1) if arrow_count > 0 else 0)

    clauses = [c.strip() for c in re.split(r"[。；;\n]", section) if c.strip()]
    purpose_keywords = ["目标", "目的", "用于", "旨在", "为了解决", "to "]
    action_keywords = ["生成", "采样", "收集", "训练", "更新", "过滤", "评分", "微调", "优化", "执行", "交互"]
    output_keywords = ["输出", "产出", "结果", "得到", "保留", "模型", "轨迹", "数据", "策略", "分数"]

    purpose_action_output_segments = 0
    for clause in clauses:
        c = clause.lower()
        has_purpose = any(k in c for k in purpose_keywords)
        has_action = any(k in c for k in action_keywords)
        has_output = any(k in c for k in output_keywords)
        if sum([has_purpose, has_action, has_output]) >= 2:
            purpose_action_output_segments += 1

    training_keywords = [
        "训练",
        "更新",
        "微调",
        "损失",
        "优化器",
        "学习率",
        "adam",
        "adamw",
        "sft",
        "cross-entropy",
        "交叉熵",
    ]
    has_train_update_info = any(k in lower_text for k in training_keywords)
    has_insufficient_note = "资料不足，需要进一步研究" in section

    failed = False
    evidence_gaps = []
    revision_actions = []
    score = 5

    if stage_estimate < 3:
        failed = True
        score -= 2
        evidence_gaps.append("方法部分缺少清晰的三段及以上阶段化机制分解。")
        revision_actions.append("Goal-3: 将方法重写为至少三段机制步骤，并说明每一步的输入、动作、输出。")
    if purpose_action_output_segments < 3:
        failed = True
        score -= 2
        evidence_gaps.append("方法表述偏概念化，缺少“目的/动作/产出”结构化描述。")
        revision_actions.append("Goal-3: 每个机制步骤至少补齐“目的/动作/产出”三要素中的两项。")
    if not has_train_update_info and not has_insufficient_note:
        failed = True
        score -= 2
        evidence_gaps.append("方法部分未给出训练/更新可执行信息，也未显式声明资料不足。")
        revision_actions.append("Goal-3: 补充训练或更新细节；若原文缺失，必须写“资料不足，需要进一步研究”。")

    return {
        "gate": gate,
        "score": max(0, score),
        "failed": failed,
        "revision_actions": revision_actions,
        "evidence_gaps": evidence_gaps,
    }


def _experiment_depth_audit(draft: str) -> Dict[str, Any]:
    gate = "experiment_protocol_depth"
    section = _extract_between(
        draft,
        ["五、评估与实验分析", "五、实验与评估", "### 五、评估与实验分析", "### 五、实验与评估"],
        ["六、", "### 六、", "#### 六、"],
    )
    if not section.strip():
        section = draft

    lower_text = section.lower()
    has_env_task = any(k in lower_text for k in ["环境", "任务", "场景", "benchmark", "sotopia", "数据集", "setting"])
    has_evaluator = any(k in lower_text for k in ["人工", "human", "评估员", "gpt-4", "llm", "评审"])
    has_metric = any(
        k in lower_text
        for k in ["指标", "metric", "goal completion", "mmlu", "toxicity", "score", "准确", "召回", "f1", "auc", "ndcg"]
    )
    has_baseline = any(k in lower_text for k in ["baseline", "基线", "对照", "比较", "消融", "ablation"])
    covered = sum([has_env_task, has_evaluator, has_metric, has_baseline])

    strong_claim = any(
        k in lower_text
        for k in ["显著", "接近", "sota", "state-of-the-art", "大幅", "明显", "提升", "优于"]
    )
    has_quant = bool(re.search(r"(\d+(\.\d+)?%|n\s*=\s*\d+|\d+\.\d+|\d+/\d+)", lower_text))
    has_insufficient_note = "资料不足，需要进一步研究" in section

    failed = False
    evidence_gaps = []
    revision_actions = []
    score = 5

    if covered < 3:
        failed = True
        score -= 3
        evidence_gaps.append("实验协议覆盖不足，环境/评估者/指标/对照四类要素不足三类。")
        revision_actions.append("Goal-7/Goal-8: 补齐实验协议三要素以上（环境与任务、评估者设置、指标、基线/对照）。")
    if strong_claim and not (has_quant or has_insufficient_note):
        failed = True
        score -= 2
        evidence_gaps.append("存在强效果结论，但未给出可核验量化片段或资料不足声明。")
        revision_actions.append("Goal-8: 对“显著/接近SOTA”等表述补量化证据；若原文缺失，显式声明资料不足。")

    return {
        "gate": gate,
        "score": max(0, score),
        "failed": failed,
        "revision_actions": revision_actions,
        "evidence_gaps": evidence_gaps,
    }


def _result_balance_audit(draft: str) -> Dict[str, Any]:
    gate = "result_critical_balance"
    result_section = _extract_between(
        draft,
        ["五、评估与实验分析", "五、实验与评估", "### 五、评估与实验分析", "### 五、实验与评估"],
        ["六、", "### 六、", "#### 六、"],
    )
    limit_section = _extract_between(
        draft,
        ["六、局限性与批判性思考", "### 六、局限性与批判性思考", "六、局限与批判性思考"],
        ["七、", "### 七、", "#### 七、"],
    )
    analysis_text = f"{result_section}\n{limit_section}".strip() or draft
    lower_text = analysis_text.lower()

    has_positive = any(
        k in lower_text
        for k in ["提升", "改进", "有效", "优于", "接近", "改善", "降低", "保持", "成功", "gain"]
    )
    has_risk = any(
        k in lower_text
        for k in ["局限", "风险", "偏差", "高估", "不足", "挑战", "外推", "泄漏", "不可靠", "资料不足"]
    )

    failed = not (has_positive and has_risk)
    score = 5 if not failed else 1
    evidence_gaps = []
    revision_actions = []
    if failed:
        evidence_gaps.append("结果分析未形成“收益+风险/偏差”并存的平衡论证。")
        revision_actions.append(
            "Goal-7/Goal-9: 补写与主结论同等级的风险约束（评估偏差/外推边界/数据局限）并绑定证据。"
        )

    return {
        "gate": gate,
        "score": score,
        "failed": failed,
        "revision_actions": revision_actions,
        "evidence_gaps": evidence_gaps,
    }


def _method_detail_completeness_audit(draft: str) -> Dict[str, Any]:
    gate = "method_detail_completeness"
    section = _extract_between(
        draft,
        ["三、方法框架与技术路线", "### 三、方法框架与技术路线", "#### 三、方法框架与技术路线"],
        ["四、", "### 四、", "#### 四、"],
    )
    if not section.strip():
        return {
            "gate": gate,
            "score": 0,
            "failed": True,
            "revision_actions": ["Goal-3: 补齐方法细节（任务输入、数据筛选、训练目标、优化设置、评估协议）。"],
            "evidence_gaps": ["方法细节缺失，无法判断可复现性。"],
        }

    lower_text = section.lower()
    has_insufficient_note = "资料不足，需要进一步研究" in section
    detail_checks = {
        "task_input": any(k in lower_text for k in ["输入", "任务", "scenario", "profile", "goal"]),
        "data_curation": any(k in lower_text for k in ["采样", "筛选", "过滤", "正样本", "阈值", "ratio"]),
        "training_objective": any(k in lower_text for k in ["训练目标", "目标函数", "最大化", "最小化", "kl", "交叉熵", "loss"]),
        "optimizer_setting": any(k in lower_text for k in ["optimizer", "adam", "adamw", "学习率", "batch", "epoch", "微调", "sft"]),
        "evaluation_protocol": any(k in lower_text for k in ["评估", "指标", "baseline", "对照", "人工", "human", "mmlu", "toxicity"]),
    }
    hit_count = sum(1 for v in detail_checks.values() if v)

    failed = False
    score = 5
    evidence_gaps = []
    revision_actions = []
    if hit_count < 4 and not has_insufficient_note:
        failed = True
        score = 1 if hit_count <= 2 else 2
        missed = [k for k, v in detail_checks.items() if not v]
        evidence_gaps.append(f"方法细节覆盖不足，缺失要素: {', '.join(missed)}。")
        revision_actions.append("Goal-3: 按“任务输入/数据筛选/训练目标/优化设置/评估协议”五项补齐方法细节并绑定证据ID。")

    return {
        "gate": gate,
        "score": score,
        "failed": failed,
        "revision_actions": revision_actions,
        "evidence_gaps": evidence_gaps,
    }


def _motivation_overweight_audit(draft: str) -> Dict[str, Any]:
    gate = "motivation_overweight"
    sec2 = _extract_between(
        draft,
        ["二、研究动机与核心问题", "### 二、研究动机与核心问题", "#### 二、研究动机与核心问题"],
        ["三、", "### 三、", "#### 三、"],
    )
    sec3 = _extract_between(
        draft,
        ["三、方法框架与技术路线", "### 三、方法框架与技术路线", "#### 三、方法框架与技术路线"],
        ["四、", "### 四、", "#### 四、"],
    )

    len2 = len((sec2 or "").strip())
    len3 = len((sec3 or "").strip())
    if len2 == 0 and len3 == 0:
        return {
            "gate": gate,
            "score": 0,
            "failed": True,
            "revision_actions": ["补齐第二、三部分内容，尤其是方法细节。"],
            "evidence_gaps": ["缺少研究动机与方法章节内容。"],
        }

    # Penalize drafts where motivation dominates method detail.
    overweight = len2 > max(1200, int(1.15 * max(1, len3)))
    failed = bool(overweight)
    score = 5 if not failed else 2
    evidence_gaps = []
    revision_actions = []
    if failed:
        evidence_gaps.append("研究动机篇幅明显大于方法细节，叙述重心失衡。")
        revision_actions.append("压缩‘二、研究动机与核心问题’，将更多篇幅用于‘三、方法框架与技术路线’的可复现细节。")

    return {
        "gate": gate,
        "score": score,
        "failed": failed,
        "revision_actions": revision_actions,
        "evidence_gaps": evidence_gaps,
    }


def _goal_ids_from_tool(tool_name: str) -> List[str]:
    if tool_name in {"extract_abstract_tool"}:
        return ["Goal-1", "Goal-2"]
    if tool_name in {"extract_academic_text_tool", "detect_sections_tool", "extract_key_sections_tool"}:
        return ["Goal-3", "Goal-4", "Goal-5", "Goal-6", "Goal-7"]
    if tool_name in {"extract_citations_tool"}:
        return ["Goal-8", "Goal-9"]
    if tool_name in {"rag_search_tool"}:
        return ["Goal-4", "Goal-8", "Goal-9"]
    return ["Goal-4", "Goal-6", "Goal-9"]


def _rewrite_rag_query(
    query: str,
    input_paper_title: str,
    input_paper_abstract: str,
    paper_context: str,
    plan_structured: List[Dict[str, str]],
) -> str:
    base_query = (query or "").strip()
    if not base_query:
        return ""

    prompt = f"""你是学术检索 query 重写器。请将用户研究意图改写成更适合向量数据库检索的一条查询语句。
要求：
1. 只基于给定输入，不得编造作者、年份、数据集、实验结果。
2. 保留核心主题词（尤其方法、机制、场景、任务词）。
3. 不要写解释，不要分点，只输出一行 query 文本。
4. query 控制在 220 字符以内。
5. 优先覆盖：研究对象 + 关键机制 + 教育场景。

原始研究主题：
{base_query}

当前论文标题：
{input_paper_title}

当前论文摘要（截断）：
{(input_paper_abstract or "")[:1200]}

当前论文结构化内容（截断）：
{(paper_context or "")[:1600]}

计划结构化任务：
{_to_json(plan_structured[:3])}
"""

    try:
        response = get_llm().invoke(prompt)
        rewritten = " ".join(str(response.content or "").split()).strip()
        if not rewritten:
            return base_query
        for prefix in ("query:", "Query:", "检索词:", "检索查询:"):
            if rewritten.startswith(prefix):
                rewritten = rewritten[len(prefix) :].strip()
        if "\n" in rewritten:
            rewritten = rewritten.splitlines()[0].strip()
        if len(rewritten) > 220:
            rewritten = rewritten[:220].rstrip()
        return rewritten or base_query
    except Exception:
        return base_query


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

    prompt = f"""你是学术研究任务拆解专家。你需要输出可执行计划而不是宏观综述。
{TOP_REQUIREMENTS_CONTRACT}

用户研究主题: {query}
当前论文标题: {input_paper_title}
当前论文摘要:
{input_paper_abstract if input_paper_abstract else "当前未提取到摘要。"}

当前论文结构化内容:
{paper_context if paper_context else "当前未提取到可用结构化内容。"}

输出要求：
1. 仅输出 3 到 4 个步骤。
2. 每个步骤必须包含 goal、required_evidence、deliverable。
3. required_evidence 必须是 researcher 可以直接采集的证据类型。
4. 禁止预设未检索到的具体论文结论、实验数值、作者单位或DOI。
5. 步骤必须覆盖 Goal-1 到 Goal-9 的核心问题。
6. 每个 goal/deliverable 必须简洁可执行（1-2句），不得要求用户额外手工提供数据才能执行。
"""

    try:
        response = structured_planner_llm.invoke(prompt)
        structured_plan = []
        readable_plan = []
        for idx, step in enumerate(response.plan, start=1):
            clipped_goal = _clip_text(step.goal, 140)
            clipped_required = [_clip_text(item, 90) for item in step.required_evidence[:4]]
            clipped_deliverable = _clip_text(step.deliverable, 160)
            step_dict = {
                "goal": clipped_goal,
                "required_evidence": "; ".join(clipped_required),
                "deliverable": clipped_deliverable,
            }
            structured_plan.append(step_dict)
            readable_plan.append(
                f"Step-{idx} 目标: {clipped_goal} | 证据需求: {'; '.join(clipped_required)} | 产出: {clipped_deliverable}"
            )
        return {
            "plan": readable_plan,
            "plan_structured": structured_plan,
            "revision_number": 0,
            "current_step": readable_plan[0] if readable_plan else "",
            "working_memory": f"Planner generated {len(readable_plan)} executable steps.",
        }
    except Exception as e:
        print(f"Error in planner_node: {e}")
        return {
            "plan": [],
            "plan_structured": [],
            "revision_number": 0,
            "current_step": "",
            "working_memory": "Planner failed to generate structured plan.",
        }


def researcher_node(state: AgentState):
    print("[Researcher Node] Executing the research step.")
    query = state["query"]
    plan_text = "\n".join(state.get("plan", []))
    plan_structured = state.get("plan_structured", [])
    critic = state.get("critic", "")
    critic_actions_for_researcher = state.get("critic_actions_for_researcher", []) or []
    paper_path = state.get("paper_path", "").strip()
    input_paper_title = state.get("input_paper_title", "")
    input_paper_abstract = state.get("input_paper_abstract", "")
    paper_context = _build_paper_context(state)
    researcher_llm = get_llm().bind_tools(tools)
    rag_query = _rewrite_rag_query(
        query=query,
        input_paper_title=input_paper_title,
        input_paper_abstract=input_paper_abstract,
        paper_context=paper_context,
        plan_structured=plan_structured,
    )

    trigger_text = " ".join(
        [
            query,
            plan_text,
            _to_json(plan_structured),
            " ".join(state.get("critic_failed_gates", []) or []),
            critic,
            " ".join(critic_actions_for_researcher),
        ]
    ).lower()
    rag_trigger = any(
        k in trigger_text
        for k in [
            "goal-9",
            "future direction",
            "相关论文",
            "文献版图",
            "对比",
            "comparative",
            "cross-paper",
            "baseline",
        ]
    )
    priority_queue = [
        "detect_sections_tool",
        "extract_key_sections_tool",
        "extract_academic_text_tool",
        "extract_citations_tool",
    ]
    if rag_trigger:
        priority_queue.append("rag_search_tool")
    tool_traces = [
        f"rag_query_original: {_clip_text(query, 200)}",
        f"rag_query_rewritten: {_clip_text(rag_query, 220)}",
        f"react_budget: steps={REACT_MAX_STEPS}, max_errors={REACT_MAX_TOOL_ERRORS}, repeat_limit={REACT_REPEAT_ACTION_LIMIT}",
        f"react_priority_queue: {' -> '.join(priority_queue)}",
        f"rag_trigger: {rag_trigger}",
    ]
    react_trace = []
    tool_failures = []
    evidence_cards = []
    default_tools = ["extract_abstract_tool", "extract_key_sections_tool"]
    retrieved_papers = []
    evidence_idx = 1
    step_count = 0
    stop_reason = ""
    tool_error_count = 0
    low_gain_streak = 0
    action_key_counts: Dict[str, int] = {}
    seen_evidence_signatures = set()
    tools_used = set()
    observations: List[str] = []
    observation_history: List[str] = []

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
        if tool_name != "rag_search_tool" and not tool_args.get("paper_path"):
            tool_args["paper_path"] = paper_path
        if tool_name == "rag_search_tool":
            if not rag_trigger:
                return {
                    "ok": False,
                    "reason": "rag_not_triggered",
                    "content": "[MCP_WARN] rag_search skipped: Goal-9/comparative trigger not active",
                    "args": tool_args,
                }
            if not tool_args.get("query"):
                tool_args["query"] = rag_query
            top_k = tool_args.get("top_k", RAG_TOP_K)
            try:
                top_k = int(top_k)
            except Exception:
                top_k = RAG_TOP_K
            if top_k <= 0:
                top_k = RAG_TOP_K
            tool_args["top_k"] = top_k
            if not tool_args.get("input_paper"):
                tool_args["input_paper"] = paper_context or input_paper_abstract
        if tool_name == "rag_search_tool":
            trace_query = _clip_text(str(tool_args.get("query", "")), 140)
            tool_traces.append(
                f"{tool_name}(query={trace_query}, top_k={tool_args.get('top_k', RAG_TOP_K)})"
            )
        else:
            tool_traces.append(f"{tool_name}(paper_path={tool_args.get('paper_path', '')})")
        result = str(tool_obj.invoke(tool_args))
        if result.startswith("[MCP_ERROR]"):
            return {"ok": False, "reason": "mcp_error", "content": result, "args": tool_args}
        if result.startswith("[MCP_WARN]"):
            return {"ok": False, "reason": "mcp_warn", "content": result, "args": tool_args}
        return {"ok": True, "reason": "success", "content": result, "args": tool_args}

    def infer_card_hints(tool_name: str) -> Dict[str, str]:
        if tool_name == "detect_sections_tool":
            return {"topic": "structure", "section_hint": "sections"}
        if tool_name == "extract_key_sections_tool":
            return {"topic": "method_evidence", "section_hint": "key_sections"}
        if tool_name == "extract_academic_text_tool":
            return {"topic": "full_text_evidence", "section_hint": "full_text"}
        if tool_name == "extract_citations_tool":
            return {"topic": "comparative_evidence", "section_hint": "references"}
        if tool_name == "rag_search_tool":
            return {"topic": "related_work", "section_hint": "rag"}
        return {"topic": "general_evidence", "section_hint": ""}

    def add_evidence_card(
        source_type: str,
        source_ref: str,
        claim: str,
        snippet: str,
        goal_ids: List[str],
        confidence: str,
        locator_hint: str = "",
        novelty_score: float = 0.0,
    ):
        nonlocal evidence_idx
        hints = infer_card_hints(source_ref)
        card = {
            "id": f"E{evidence_idx}",
            "source_type": source_type,
            "source_ref": source_ref,
            "claim": claim,
            "snippet": snippet[:1200],
            "goal_ids": ", ".join(goal_ids),
            "confidence": confidence,
            "topic": hints["topic"],
            "section_hint": hints["section_hint"],
            "locator_hint": locator_hint,
            "novelty_score": round(float(novelty_score), 4),
        }
        evidence_cards.append(card)
        evidence_idx += 1

    for step in range(1, REACT_MAX_STEPS + 1):
        step_count = step
        next_priority_tool = next((t for t in priority_queue if t not in tools_used), "")
        react_prompt = f"""你是 ReAct 取证研究员。请在本轮先思考，再决定是否调用一个工具。
{TOP_REQUIREMENTS_CONTRACT}

当前轮次: {step}/{REACT_MAX_STEPS}
研究主题: {query}
当前论文标题: {input_paper_title}
当前论文路径: {paper_path if paper_path else "缺失"}
计划文本:
{plan_text}
计划结构化任务:
{_to_json(plan_structured)}
当前论文摘要:
{input_paper_abstract if input_paper_abstract else "当前未提取到摘要。"}
当前论文结构化内容:
{_truncate_middle(paper_context if paper_context else "当前未提取到可用结构化内容。", 2800)}
上一轮审稿摘要:
{_clip_text(critic, 500) if critic else "无"}
上一轮给 researcher 的动作建议:
{_to_json(critic_actions_for_researcher) if critic_actions_for_researcher else "[]"}

已观测到的结果:
{_to_json(observations[-8:]) if observations else "[]"}

当前已收集证据卡摘要:
{_to_json(_compact_evidence_cards(evidence_cards, max_cards=10, snippet_limit=120))}

优先取证队列:
{' -> '.join(priority_queue)}
本轮建议优先工具:
{next_priority_tool if next_priority_tool else "无（核心队列已覆盖）"}

可用工具:
- extract_academic_text_tool(paper_path)
- extract_abstract_tool(paper_path)
- detect_sections_tool(paper_path)
- extract_key_sections_tool(paper_path)
- extract_citations_tool(paper_path)
- rag_search_tool(query, top_k, input_paper)

规则:
1. 每轮最多调用一个工具，参数必须具体。
2. 若证据已足够，请直接输出以 `FINAL:` 开头的停止说明，并且不要调用工具。
3. 先覆盖优先队列：结构定位 -> 方法/实验细节 -> 引用证据；仅在 Goal-9 / comparative 触发时调用 rag_search_tool。
4. 严禁编造工具结果。
5. 若某项信息无法获取，明确写“资料不足，需要进一步研究”。
"""
        response = researcher_llm.invoke(react_prompt)
        thought = _clip_text(str(response.content or ""), 420)
        react_trace.append(f"step-{step} thought: {thought if thought else '(empty)'}")
        tool_calls = list(response.tool_calls or [])

        if not tool_calls:
            if thought and (
                thought.lower().startswith("final:")
                or "完成取证" in thought
                or "证据已足够" in thought
                or "停止" in thought
            ):
                stop_reason = "model_declared_final"
                react_trace.append(f"step-{step} stop: {stop_reason}")
            elif next_priority_tool:
                tool_calls = [{"name": next_priority_tool, "args": {}}]
                react_trace.append(f"step-{step} note: fallback_to_priority_tool={next_priority_tool}")
            else:
                stop_reason = "model_no_action"
                react_trace.append(f"step-{step} stop: {stop_reason}")
            if stop_reason:
                break

        tool_call = tool_calls[0]
        if len(tool_calls) > 1:
            react_trace.append(f"step-{step} note: multiple_tools={len(tool_calls)} only_first_used")

        tool_name = tool_call["name"]
        raw_args = tool_call.get("args", {})
        print(f"   🤖 [ReAct Step {step}] 调用工具 '{tool_name}'")
        print(f"   🎯 [生成的工具参数]: {raw_args}")
        invoke_result = invoke_research_tool(tool_name, raw_args)
        used_args = invoke_result["args"]
        action_key = f"{tool_name}:{json.dumps(used_args, ensure_ascii=False, sort_keys=True)}"

        react_trace.append(f"step-{step} action: {tool_name} args={_clip_text(_to_json(used_args), 260)}")
        seen_count = int(action_key_counts.get(action_key, 0)) + 1
        action_key_counts[action_key] = seen_count
        if seen_count > REACT_REPEAT_ACTION_LIMIT:
            stop_reason = "repeated_signature"
            react_trace.append(f"step-{step} stop: {stop_reason}")
            break

        if invoke_result["ok"]:
            tools_used.add(tool_name)
            tool_result = invoke_result["content"]
            observation = _clip_text(tool_result, 900)
            gain = _estimate_information_gain(observation, observation_history)
            novelty_score = float(gain["novelty_score"])
            estimated_new_chars = int(gain["estimated_new_chars"])
            observations.append(f"[{tool_name}] {observation}")
            observation_history.append(observation)
            react_trace.append(
                f"step-{step} observation: {observation} | novelty={novelty_score:.4f}, est_new_chars={estimated_new_chars}"
            )
            is_low_gain = estimated_new_chars < LOW_GAIN_MIN_NEW_CHARS
            evidence_signature = f"{tool_name}:{_normalize_text_key(observation[:320])}"
            duplicate_evidence = evidence_signature in seen_evidence_signatures

            if tool_name == "rag_search_tool":
                source_type = "rag"
                source_ref = "vector_db"
                rag_chunks = [
                    item.strip()
                    for item in re.split(r"\n\s*\n(?=\[RAG#\d+\])", tool_result)
                    if item.strip()
                ]
                if not rag_chunks:
                    rag_chunks = [tool_result.strip()]
                retrieved_papers.extend(rag_chunks)
            else:
                source_type = "current_paper"
                source_ref = tool_name

            if is_low_gain and duplicate_evidence:
                react_trace.append(f"step-{step} note: skip_duplicate_low_gain_evidence")
            else:
                add_evidence_card(
                    source_type=source_type,
                    source_ref=source_ref,
                    claim=f"{tool_name} 提供了与研究目标相关的原始证据。",
                    snippet=tool_result,
                    goal_ids=_goal_ids_from_tool(tool_name),
                    confidence="medium",
                    locator_hint=f"{tool_name}:{_clip_text(_to_json(used_args), 120)}",
                    novelty_score=novelty_score,
                )
                seen_evidence_signatures.add(evidence_signature)

            if is_low_gain:
                low_gain_streak += 1
            else:
                low_gain_streak = 0
            print(f"   📄 [工具返回成功]: 截取到了 {len(tool_result)} 个字符的数据。")
        else:
            tool_error_count += 1
            failure_note = f"{tool_name}: {invoke_result['content']}"
            tool_failures.append(failure_note)
            observations.append(f"[{tool_name}] ERROR: {invoke_result['content']}")
            react_trace.append(f"step-{step} observation_error: {_clip_text(invoke_result['content'], 300)}")
            print(f"   ⚠️ [工具结果未纳入证据]: {failure_note}")
            if tool_error_count >= REACT_MAX_TOOL_ERRORS:
                stop_reason = "too_many_tool_errors"
                react_trace.append(f"step-{step} stop: {stop_reason}")
                break

        if low_gain_streak >= LOW_GAIN_REPEAT_LIMIT:
            stop_reason = "low_information_gain"
            react_trace.append(f"step-{step} stop: {stop_reason}")
            break
        remaining_priority_tools = [t for t in priority_queue if t not in tools_used]
        if not remaining_priority_tools and low_gain_streak > 0:
            stop_reason = "priority_queue_exhausted"
            react_trace.append(f"step-{step} stop: {stop_reason}")
            break

    if not stop_reason:
        stop_reason = "max_steps_reached"
        react_trace.append(f"step-{step_count} stop: {stop_reason}")

    if not evidence_cards and paper_path:
        react_trace.append("fallback: no evidence collected in react loop, running default tools")
        for default_tool in default_tools:
            invoke_result = invoke_research_tool(default_tool, {"paper_path": paper_path})
            if invoke_result["ok"]:
                tool_result = invoke_result["content"]
                add_evidence_card(
                    source_type="current_paper",
                    source_ref=default_tool,
                    claim=f"{default_tool} 回退调用成功，提取到可用证据。",
                    snippet=tool_result,
                    goal_ids=_goal_ids_from_tool(default_tool),
                    confidence="medium",
                )
                print(f"   ↻ [回退调用]: {default_tool} 返回 {len(tool_result)} 个字符。")
            else:
                failure_note = f"{default_tool}: {invoke_result['content']}"
                tool_failures.append(failure_note)
                print(f"   ⚠️ [回退工具失败]: {failure_note}")
    if not evidence_cards:
        stop_reason = f"{stop_reason}|no_evidence_after_fallback"
        react_trace.append("stop: no evidence after fallback")

    tool_failures = _dedupe_limit_items(tool_failures, EVIDENCE_GAPS_TOP_N)
    react_trace = _limit_items(react_trace, 120)

    documents_from_cards = [
        f"【证据 {card['id']}】[{card['source_type']}] {card['source_ref']} | goals={card['goal_ids']}\n{card['snippet'][:600]}\n---"
        for card in evidence_cards
    ]
    rag_context = "\n\n".join(retrieved_papers)

    working_memory = (
        f"Researcher ReAct finished: steps={step_count}, stop={stop_reason}, evidence_cards={len(evidence_cards)}, tool_errors={len(tool_failures)}."
    )

    return {
        "documents": documents_from_cards,
        "evidence_cards": evidence_cards,
        "searched_queries": tool_traces,
        "retrieved_papers": retrieved_papers,
        "rag_context": rag_context,
        "evidence_gaps": tool_failures,
        "react_step_count": step_count,
        "react_stop_reason": stop_reason,
        "react_trace": react_trace,
        "working_memory": working_memory,
        "current_step": "researcher",
    }


def writer_node(state: AgentState):
    print("[Writer Node] Drafting the response based on the research findings.")
    query = state["query"]
    plan_structured = state.get("plan_structured", [])
    critic = state.get("critic", "")
    critic_actions_for_writer = state.get("critic_actions_for_writer", []) or []
    input_paper = state.get("input_paper", "")
    input_paper_title = state.get("input_paper_title", "")
    input_paper_abstract = state.get("input_paper_abstract", "")
    paper_context = _build_paper_context(state)
    evidence_cards = state.get("evidence_cards", [])
    evidence_text = _to_json(evidence_cards)

    structured_prompt = f"""{AGENT_REQUIREMENTS}
{TOP_REQUIREMENTS_CONTRACT}

你先输出“结构化核验稿”，用于防幻觉验证。
输入如下：
研究主题: {query}
当前论文标题: {input_paper_title}
当前论文摘要: {input_paper_abstract if input_paper_abstract else "无"}
当前论文结构化内容: {paper_context if paper_context else input_paper}
计划结构化任务:
{_to_json(plan_structured)}
证据卡:
{evidence_text if evidence_cards else "[]"}
上一轮审稿反馈:
{critic if critic else "无"}
上一轮给 writer 的动作建议:
{_to_json(critic_actions_for_writer) if critic_actions_for_writer else "[]"}

输出约束：
1. 生成 claims，必须给 evidence_ids 和 goal_ids。
2. 没有证据支持的结论，certainty 必须是 insufficient，statement 必须写“资料不足，需要进一步研究”。
3. 严禁输出无证据支撑的作者单位、会议归属、实验数值、DOI。
4. 覆盖 Goal-1 到 Goal-9，coverage_ratio 是0到1。
5. 必须包含 claim_id='C-core'：用3-5句讲清“本文核心思路链路（输入->关键模块->交互机制->输出->主要发现）”。
6. 必须包含 C-M1 到 C-M5 五类方法细节声明（可与其他claim合并，但语义必须覆盖）：
   - C-M1: 任务与输入定义
   - C-M2: 数据收集与筛选规则（阈值/比例/保留条件）
   - C-M3: 训练目标与损失函数
   - C-M4: 优化设置（优化器/学习率/训练策略）
   - C-M5: 评估协议（评估者/指标/对照）
   若原文缺失，必须在对应声明中写“资料不足，需要进一步研究”，certainty=insufficient。
"""

    writer_structured_draft = "资料不足，需要进一步研究。"
    writer_claims = []
    covered_goals = []
    coverage_ratio = 0.0

    try:
        writer_structured_llm = get_llm().with_structured_output(WriterStructuredOutput)
        structured_response = writer_structured_llm.invoke(structured_prompt)
        writer_structured_draft = structured_response.structured_draft
        writer_claims = [claim.model_dump() for claim in structured_response.claims]
        covered_goals = structured_response.covered_goals
        coverage_ratio = structured_response.coverage_ratio
    except Exception as e:
        print(f"Error in writer structured draft stage: {e}")
        writer_claims = [
            {
                "claim_id": "C-fallback",
                "statement": "资料不足，需要进一步研究。",
                "evidence_ids": [],
                "goal_ids": [],
                "certainty": "insufficient",
            }
        ]

    render_prompt = f"""{AGENT_REQUIREMENTS}
{TOP_REQUIREMENTS_CONTRACT}

请基于结构化核验稿生成最终可读长文版本，必须保持九段固定结构。
你必须做到：
1. 每个关键结论在文本中标注证据ID（如 [E1]）。
2. 明确区分“作者主张”与“你的研究判断”。
3. 对教育迁移给出“可迁移/不可迁移/条件迁移”判断。
4. 证据不足处必须写“资料不足，需要进一步研究”。
5. 在“三、方法框架与技术路线”的开头新增“核心思路（3-5句）”，必须清晰回答：
   - 系统输入是什么
   - 关键模块如何协同
   - 交互机制如何触发行为变化
   - 输出是什么
   - 实验主要发现是什么
6. 禁止输出“✅ Goal-x”“覆盖率100%”“全部通过”这类自证文本。
7. 控制篇幅重心：
   - “二、研究动机与核心问题”要精炼，不超过4个要点，避免长段铺陈。
   - “三、方法框架与技术路线”必须是全文最细致部分。
8. 在“三、方法框架与技术路线”中必须明确包含这五块信息（可用小标题或编号）：
   - 任务与输入定义
   - 数据收集与筛选（含阈值/比例/保留规则）
   - 训练目标与优化设置（loss/optimizer/训练策略）
   - 评估协议与对照设置（评估者/指标/baseline）
   - 关键发现与偏差（收益+风险并列）
   对无法确认的细节，必须显式写“资料不足，需要进一步研究”。

结构化核验稿:
{writer_structured_draft}

给 writer 的动作建议:
{_to_json(critic_actions_for_writer) if critic_actions_for_writer else "[]"}

关键声明:
{_to_json(writer_claims)}

证据卡:
{evidence_text if evidence_cards else "[]"}

覆盖目标:
{', '.join(covered_goals) if covered_goals else '无'}
覆盖率:
{coverage_ratio}
"""

    try:
        response = get_llm().invoke(render_prompt)
        print(f"   📝 [草稿完成]: 共生成了 {len(response.content)} 个字符。")
        return {
            "draft": response.content,
            "writer_structured_draft": writer_structured_draft,
            "writer_claims": writer_claims,
            "working_memory": f"Writer produced dual outputs with {len(writer_claims)} claims.",
            "current_step": "writer",
        }
    except Exception as e:
        print(f"Error in writer_node: {e}")
        fallback = "writer节点执行失败，无法生成草稿。资料不足，需要进一步研究。"
        return {
            "draft": fallback,
            "writer_structured_draft": writer_structured_draft,
            "writer_claims": writer_claims,
            "working_memory": fallback,
            "current_step": "writer",
        }


def critic_node(state: AgentState):
    print("[Critic Node] Reviewing the draft and providing feedback.")
    query = state["query"]
    draft = state.get("draft", "")
    writer_structured_draft = state.get("writer_structured_draft", "")
    writer_claims = state.get("writer_claims", [])
    input_paper = state.get("input_paper", "")
    input_paper_title = state.get("input_paper_title", "")
    input_paper_abstract = state.get("input_paper_abstract", "")
    paper_context_full = _build_paper_context(state)
    evidence_cards = state.get("evidence_cards", [])
    rag_context_full = state.get("rag_context", "")
    current_rev = state.get("revision_number", 0)
    structured_critic_llm = get_llm().with_structured_output(CriticOutput)
    context_probe_llm = get_llm().with_structured_output(CriticContextNeed)

    draft_preview = _truncate_middle(draft, 9000)
    writer_structured_preview = _truncate_middle(writer_structured_draft, 3500)
    writer_claims_compact = _compact_writer_claims(writer_claims, max_claims=14, statement_limit=220)
    evidence_summary = _compact_evidence_cards(evidence_cards, max_cards=12, snippet_limit=180)
    evidence_expanded = _compact_evidence_cards(evidence_cards, max_cards=22, snippet_limit=420)
    paper_context_preview = _truncate_middle(paper_context_full if paper_context_full else input_paper, 4800)
    rag_context_preview = _truncate_middle(rag_context_full, 3200)

    probe_prompt = f"""你是审稿上下文裁剪助手。目标是在保证审稿质量的同时尽量减少上下文长度。
你只需要判断当前审稿是否必须额外注入以下上下文：
- paper_context（论文结构化正文）
- rag_context（相关论文召回）
- full_evidence_cards（完整证据卡，而不是摘要）

输入：
研究目标：{query}
当前论文标题：{input_paper_title}
当前论文摘要：{_clip_text(input_paper_abstract, 800)}
Writer结构化核验稿（摘要）：{writer_structured_preview}
Writer关键声明（摘要）：
{_to_json(writer_claims_compact)}
Writer可读草稿（摘要）：
{draft_preview}
证据卡摘要：
{_to_json(evidence_summary)}

判定规则：
1. 只在“确实缺少对应证据才能判断”时返回 true。
2. 如果草稿已包含足够证据链，尽量返回 false。
3. 输出必须简洁。
"""

    try:
        context_need = context_probe_llm.invoke(probe_prompt)
        include_paper_context = bool(context_need.include_paper_context)
        include_rag_context = bool(context_need.include_rag_context)
        include_full_evidence_cards = bool(context_need.include_full_evidence_cards)
        context_reason = _clip_text(context_need.reason, 240)
    except Exception as e:
        include_paper_context = False
        include_rag_context = False
        include_full_evidence_cards = False
        context_reason = f"context_probe_failed: {type(e).__name__}"

    selected_paper_context = paper_context_preview if include_paper_context else "（未注入，按需省略）"
    selected_rag_context = rag_context_preview if include_rag_context else "（未注入，按需省略）"
    selected_evidence = evidence_expanded if include_full_evidence_cards else evidence_summary

    prompt = f"""你是门槛化审稿人，需要按评分门槛而非主观长评审稿。
{TOP_REQUIREMENTS_CONTRACT}

用户研究目标:
{query}
当前论文标题:
{input_paper_title}
当前论文摘要:
{input_paper_abstract if input_paper_abstract else "无"}
当前论文结构化内容:
{selected_paper_context}
本地论文库上下文:
{selected_rag_context if selected_rag_context else "无"}
上下文裁剪说明:
{context_reason}

证据卡:
{_to_json(selected_evidence)}

Writer结构化核验稿:
{writer_structured_preview}

Writer关键声明:
{_to_json(writer_claims_compact)}

Writer可读草稿:
{draft_preview}

请按门槛评分：
- no_hallucination
- paper_coverage
- core_idea_clarity
- goal_1_to_8_coverage
- goal_9_coverage
- evidence_alignment
- comparative_quality
- anti_self_report
- method_mechanism_depth
- method_detail_completeness
- experiment_protocol_depth
- result_critical_balance
- motivation_overweight

输出要求：
1. gate_scores 0-5。
2. failed_gates 只列未通过门槛。
3. covered_goals 与 goal_coverage_ratio 必须填写。
4. revision_actions 必须可执行，尽量写成“Goal-x: 动作”。
5. next_step 只能是 finish/writer/researcher。
6. 不得因为“writer自报覆盖率高”而直接通过，必须以正文实质内容为准。
"""

    try:
        try:
            response = structured_critic_llm.invoke(prompt)
        except Exception as first_error:
            print(f"   [Critic Retry] first parse failed: {type(first_error).__name__}: {first_error}")
            retry_prompt = f"""你是门槛化审稿人。请在更短上下文下输出严格结构化结果。
{TOP_REQUIREMENTS_CONTRACT}

用户研究目标: {query}
当前论文标题: {input_paper_title}
当前论文摘要: {_clip_text(input_paper_abstract, 500)}

证据卡(摘要):
{_to_json(evidence_summary)}

Writer关键声明(摘要):
{_to_json(writer_claims_compact)}

Writer可读草稿(摘要):
{_truncate_middle(draft_preview, 5000)}

评分门槛:
no_hallucination, paper_coverage, core_idea_clarity, goal_1_to_8_coverage, goal_9_coverage, evidence_alignment, comparative_quality, anti_self_report, method_mechanism_depth, method_detail_completeness, experiment_protocol_depth, result_critical_balance, motivation_overweight

输出要求:
1. gate_scores 0-5
2. failed_gates 列未通过项
3. covered_goals 与 goal_coverage_ratio 必填
4. revision_actions 必须可执行
5. next_step 只能是 finish/writer/researcher
"""
            response = structured_critic_llm.invoke(retry_prompt)
        gate_scores = {k: int(v) for k, v in (response.gate_scores or {}).items()}
        llm_failed_gates = response.failed_gates or []
        llm_marked_anti_self_report = "anti_self_report" in llm_failed_gates
        anti_self_report_deterministic = _detect_explicit_self_report(draft)
        anti_self_report_soft_warning = False
        if llm_marked_anti_self_report and not anti_self_report_deterministic:
            llm_failed_gates = [g for g in llm_failed_gates if g != "anti_self_report"]
            anti_self_report_soft_warning = True
        missing_evidence = response.missing_evidence or []
        missing_topics = response.missing_topics or []
        revision_actions = response.revision_actions or []
        covered_goals = response.covered_goals or []
        goal_coverage_ratio = response.goal_coverage_ratio or 0.0
        goal_ids_from_writer = _extract_goal_ids_from_claims(writer_claims)
        goal_ids_from_draft = re.findall(r"Goal-\d+", draft)
        covered_goal_set = set(_dedupe_keep_order(covered_goals + goal_ids_from_writer + goal_ids_from_draft))
        missing_goal_1_to_8 = [f"Goal-{i}" for i in range(1, 9) if f"Goal-{i}" not in covered_goal_set]
        goal_9_covered = "Goal-9" in covered_goal_set
        goal_coverage_ratio = len([g for g in covered_goal_set if re.fullmatch(r"Goal-[1-9]", g)]) / 9.0

        retrieved_papers = state.get("retrieved_papers", []) or []
        rag_stats = _compute_rag_hit_stats(retrieved_papers, rag_context_full)
        rag_total_hits = int(rag_stats.get("total_hits", 0))
        rag_relevant_hits = int(rag_stats.get("relevant_hits", 0))
        blocked_by_capability = []

        deep_audits = [
            _method_depth_audit(draft),
            _method_detail_completeness_audit(draft),
            _experiment_depth_audit(draft),
            _result_balance_audit(draft),
            _motivation_overweight_audit(draft),
        ]
        deep_failed_gates = []
        for audit in deep_audits:
            gate = str(audit.get("gate", "")).strip()
            score = int(audit.get("score", 0))
            failed = bool(audit.get("failed", False))
            if gate:
                if gate in gate_scores:
                    gate_scores[gate] = min(gate_scores[gate], score)
                else:
                    gate_scores[gate] = score
            if failed and gate:
                deep_failed_gates.append(gate)
            revision_actions.extend(audit.get("revision_actions", []))
            missing_evidence.extend(audit.get("evidence_gaps", []))

        hard_fail_gates = _critic_hard_fail_checks(draft)
        if hard_fail_gates:
            gate_scores.setdefault("core_idea_clarity", 0)
            if "anti_self_report" in hard_fail_gates:
                gate_scores["anti_self_report"] = 0
            if "core_idea_clarity" in hard_fail_gates:
                revision_actions.append(
                    "Goal-3: 在“三、方法框架与技术路线”开头新增“核心思路（输入->模块->交互->输出->发现）”3-5句，并绑定证据ID。"
                )
            if "anti_self_report" in hard_fail_gates:
                revision_actions.append("去掉“✅ Goal-x/覆盖率100%”等自证文本，改为正文实质论证。")
            missing_topics.append("当前草稿未满足论文核心思路清晰阐释门槛。")
        elif anti_self_report_soft_warning:
            revision_actions.append(
                "存在疑似自证话术（非显式），建议改为客观证据论证并删除元评价表达。"
            )

        failed_gates = _dedupe_keep_order(llm_failed_gates + deep_failed_gates + hard_fail_gates)
        gate_split = _split_critic_failed_gates(failed_gates)
        hard_failed_gates = gate_split["hard"]
        soft_failed_gates = gate_split["soft"]
        if anti_self_report_soft_warning and "anti_self_report" not in hard_failed_gates:
            soft_failed_gates = _dedupe_keep_order(soft_failed_gates + ["anti_self_report_soft_warning"])
            gate_scores["anti_self_report_soft_warning"] = min(int(gate_scores.get("anti_self_report", 4)), 3)

        if missing_goal_1_to_8:
            hard_failed_gates = _dedupe_keep_order(hard_failed_gates + ["goal_1_to_8_coverage"])
            gate_scores["goal_1_to_8_coverage"] = 1
            missing_topics.append(f"Goal-1~Goal-8 覆盖不足，缺失: {', '.join(missing_goal_1_to_8)}")
            revision_actions.append(
                f"补齐 Goal-1~Goal-8 缺失项（{', '.join(missing_goal_1_to_8)}），每项至少绑定1条当前论文证据ID。"
            )
        else:
            gate_scores["goal_1_to_8_coverage"] = max(int(gate_scores.get("goal_1_to_8_coverage", 4)), 4)

        if rag_relevant_hits >= RAG_GOAL9_HARD_MIN_HITS:
            if not goal_9_covered:
                hard_failed_gates = _dedupe_keep_order(hard_failed_gates + ["goal_9_coverage"])
                gate_scores["goal_9_coverage"] = 1
                missing_topics.append("Goal-9 未覆盖：已有RAG证据但未形成研究空白与future direction结论。")
                revision_actions.append("基于RAG证据补齐 Goal-9：给出文献版图对比与可执行 future direction。")
            else:
                gate_scores["goal_9_coverage"] = max(int(gate_scores.get("goal_9_coverage", 4)), 4)
        else:
            blocked_msg = (
                "Goal-9降级为软门槛："
                f"教育+多智能体相关命中={rag_relevant_hits}/{rag_total_hits} "
                f"< {RAG_GOAL9_HARD_MIN_HITS}（能力受限）"
            )
            blocked_by_capability.append(blocked_msg)
            if not goal_9_covered:
                soft_failed_gates = _dedupe_keep_order(soft_failed_gates + ["goal_9_coverage"])
                gate_scores["goal_9_coverage"] = 3
                revision_actions.append("当前轮RAG证据不足，Goal-9按软门槛处理；后续优先补充相关论文检索。")

        if not hard_failed_gates and not soft_failed_gates and not response.is_acceptable:
            soft_failed_gates = _dedupe_keep_order(soft_failed_gates + ["llm_overall_reject"])
            failed_gates = _dedupe_keep_order(failed_gates + ["llm_overall_reject"])
            gate_scores.setdefault("llm_overall_reject", 2)
            revision_actions.append("结合审稿总评补齐关键缺口，并保持每条核心结论都绑定证据ID。")

        revision_actions = _dedupe_limit_items(revision_actions, EVIDENCE_GAPS_TOP_N)
        missing_evidence = _dedupe_limit_items(missing_evidence, EVIDENCE_GAPS_TOP_N)
        missing_topics = _dedupe_limit_items(missing_topics, EVIDENCE_GAPS_TOP_N)
        blocked_by_capability = _dedupe_limit_items(blocked_by_capability, EVIDENCE_GAPS_TOP_N)

        for gate in hard_failed_gates:
            gate_scores[gate] = min(int(gate_scores.get(gate, 2)), 2)
        for gate in soft_failed_gates:
            gate_scores[gate] = min(int(gate_scores.get(gate, 3)), 3)

        at_revision_limit = current_rev + 1 >= MAX_REVISIONS
        hard_blocked = bool(hard_failed_gates)
        has_soft_warnings = bool(soft_failed_gates)

        if not hard_blocked and not has_soft_warnings:
            critic_status = "pass"
            next_step = "finish"
        else:
            if hard_blocked:
                critic_status = "finish_with_risks" if at_revision_limit else "fail"
                next_step = "finish" if at_revision_limit else "researcher"
            else:
                critic_status = "pass_with_warnings"
                next_step = "finish" if at_revision_limit else "writer"

        strict_finish_eligible = critic_status == "pass"
        response.is_acceptable = critic_status in {"pass", "pass_with_warnings"}
        next_revision = current_rev if strict_finish_eligible else current_rev + 1

        unresolved = []
        if critic_status == "finish_with_risks":
            unresolved = _dedupe_limit_items(
                missing_evidence + missing_topics + revision_actions,
                EVIDENCE_GAPS_TOP_N,
            )

        warnings = []
        if has_soft_warnings:
            warnings.extend(revision_actions)
        warnings.extend(blocked_by_capability)
        warnings = _dedupe_limit_items(warnings, EVIDENCE_GAPS_TOP_N)

        all_failed_gates = _dedupe_keep_order(hard_failed_gates + soft_failed_gates)
        new_failed_gates = _dedupe_limit_items(all_failed_gates, EVIDENCE_GAPS_TOP_N)
        new_revision_actions = _dedupe_limit_items(revision_actions, EVIDENCE_GAPS_TOP_N)
        new_evidence_gaps = _dedupe_limit_items(
            missing_evidence + missing_topics,
            EVIDENCE_GAPS_TOP_N,
        )
        new_unresolved = _dedupe_limit_items(unresolved, EVIDENCE_GAPS_TOP_N)
        split_actions = _split_actions_for_roles(new_revision_actions, hard_failed_gates, soft_failed_gates)
        actions_for_researcher = _limit_items(split_actions["researcher"], EVIDENCE_GAPS_TOP_N)
        actions_for_writer = _limit_items(split_actions["writer"], EVIDENCE_GAPS_TOP_N)

        deep_audit_summary = "; ".join(
            f"{a['gate']}={int(gate_scores.get(a['gate'], a.get('score', 0)))}"
            f"{'✗' if a['gate'] in all_failed_gates else '✓'}"
            for a in deep_audits
        )
        critic_summary = (
            f"总评：{response.summary}；"
            f"审稿状态：{critic_status}；"
            f"深度审计：{deep_audit_summary if deep_audit_summary else '无'}；"
            f"硬门槛未通过：{', '.join(hard_failed_gates) if hard_failed_gates else '无'}；"
            f"软门槛未通过：{', '.join(soft_failed_gates) if soft_failed_gates else '无'}；"
            f"能力阻塞：{'; '.join(blocked_by_capability) if blocked_by_capability else '无'}；"
            f"目标覆盖率：{goal_coverage_ratio:.2f}；"
            f"下一步：{next_step}。"
        )

        if critic_status in {"pass", "pass_with_warnings"}:
            print("   [审查结果]: ✅ 通过")
        elif critic_status == "finish_with_risks":
            print("   [审查结果]: ⚠️ 带风险结束")
        else:
            print("   [审查结果]: ❌ 打回重做")
        print(f"   [审稿意见]: {response.summary}")

        return {
            "critic": critic_summary,
            "critic_status": critic_status,
            "critic_next_step": next_step,
            "critic_scores": gate_scores,
            "critic_failed_gates": new_failed_gates,
            "hard_failed_gates": hard_failed_gates,
            "soft_failed_gates": soft_failed_gates,
            "critic_actions": new_revision_actions,
            "critic_actions_for_researcher": actions_for_researcher,
            "critic_actions_for_writer": actions_for_writer,
            "warnings": warnings,
            "blocked_by_capability": blocked_by_capability,
            "revision_number": next_revision,
            "revision_history": [response.summary],
            "evidence_gaps": new_evidence_gaps,
            "unresolved_gaps": new_unresolved,
            "working_memory": critic_summary,
            "current_step": "critic",
        }
    except Exception as e:
        print(f"Error in critic_node: {e}")
        fallback_summary = "审稿失败（系统错误），默认进入 writer 节点继续修订。"
        at_revision_limit = current_rev + 1 >= MAX_REVISIONS
        next_step = "finish" if at_revision_limit else "writer"
        critic_status = "finish_with_risks" if at_revision_limit else "fail"
        unresolved = ["critic节点执行失败，当前审稿结果不可用。"] if at_revision_limit else []
        return {
            "critic": fallback_summary,
            "critic_status": critic_status,
            "critic_next_step": next_step,
            "critic_scores": {},
            "critic_failed_gates": ["critic_runtime_error"],
            "hard_failed_gates": ["critic_json_validity"],
            "soft_failed_gates": [],
            "critic_actions": ["修复critic节点并重试审稿。"],
            "critic_actions_for_researcher": ["修复critic节点后，重新执行证据收集流程。"],
            "critic_actions_for_writer": ["等待critic恢复后再依据审稿动作修订草稿。"],
            "warnings": [],
            "blocked_by_capability": [],
            "revision_number": current_rev + 1,
            "revision_history": [fallback_summary],
            "evidence_gaps": ["critic节点执行失败，当前审稿结果不可用。"],
            "unresolved_gaps": unresolved,
            "working_memory": fallback_summary,
            "current_step": "critic",
        }


def reflection_router(state: AgentState):
    if state.get("critic_next_step") == "finish":
        return "end"
    if state.get("critic_next_step") == "researcher":
        return "researcher"
    return "writer"
