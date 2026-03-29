from typing import Dict, List

from pydantic import BaseModel, Field, conint


class PlannerStepOutput(BaseModel):
    goal: str = Field(description="该步骤要完成的研究目标。")
    required_evidence: List[str] = Field(description="该步骤所需的证据类型列表。")
    deliverable: str = Field(description="该步骤应产出的可交付结果。")


class PlannerOutput(BaseModel):
    plan: List[PlannerStepOutput] = Field(
        description="为解答用户query拆解出的步骤列表，长度必须为3到4。"
    )


class WriterClaimOutput(BaseModel):
    claim_id: str = Field(description="草稿中的声明编号，例如 C1。")
    statement: str = Field(description="具体声明内容。")
    evidence_ids: List[str] = Field(description="支撑该声明的证据ID列表，例如 E1, E3。")
    goal_ids: List[str] = Field(description="声明对应的分析目标编号，例如 Goal-4。")
    certainty: str = Field(description="确定性标签：high/medium/low/insufficient。")


class WriterStructuredOutput(BaseModel):
    structured_draft: str = Field(description="用于核验的结构化草稿。")
    claims: List[WriterClaimOutput] = Field(description="草稿中提取出的关键声明列表。")
    covered_goals: List[str] = Field(description="已覆盖的分析目标编号列表。")
    coverage_ratio: float = Field(description="分析目标覆盖率，取值0到1。")


class CriticOutput(BaseModel):
    is_acceptable: bool = Field(description="该草稿是否高质量地解答了用户的研究主题，并且充分利用了提供的文献资料？")
    summary: str = Field(description="一段简洁总评，明确说明合格或不合格的核心原因。")
    missing_evidence: List[str] = Field(default_factory=list, description="草稿中缺失的关键证据、数据、来源或时间范围问题。")
    missing_topics: List[str] = Field(default_factory=list, description="草稿中缺失的研究主题点或未展开的内容。")
    revision_actions: List[str] = Field(default_factory=list, description="下一轮修改时必须执行的具体动作。")
    failed_gates: List[str] = Field(
        default_factory=list,
        description="未通过的门槛项，推荐使用: no_hallucination, paper_coverage, core_idea_clarity, goal_coverage, evidence_alignment, comparative_quality, anti_self_report, method_mechanism_depth, method_detail_completeness, experiment_protocol_depth, result_critical_balance, motivation_overweight。",
    )
    gate_scores: Dict[str, conint(ge=0, le=5)] = Field(default_factory=dict, description="各门槛评分，0到5。")
    covered_goals: List[str] = Field(default_factory=list, description="已覆盖的分析目标编号列表。")
    goal_coverage_ratio: float = Field(default=0.0, description="分析目标覆盖率，取值0到1。")
    next_step: str = Field(description="下一步动作，只允许返回 finish、writer、researcher 之一。")


class CriticContextNeed(BaseModel):
    include_paper_context: bool = Field(
        default=False, description="是否需要注入论文结构化内容来完成审稿。"
    )
    include_rag_context: bool = Field(
        default=False, description="是否需要注入RAG召回上下文来完成对比审稿。"
    )
    include_full_evidence_cards: bool = Field(
        default=False, description="是否需要完整证据卡（而不是摘要）来完成审稿。"
    )
    reason: str = Field(
        default="", description="为什么需要额外上下文，简短说明。"
    )
