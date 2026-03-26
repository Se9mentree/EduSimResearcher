from typing import List

from pydantic import BaseModel, Field


class PlannerOutput(BaseModel):
    step: int = Field(description="当前正在执行的步骤序号。")
    name: str = Field(description="当前步骤的名称，应该简洁明了地描述该步骤的核心内容。")
    plan: List[str] = Field(
        description="为解答用户的query而拆解出来的子任务列表，每个步骤应明确具体的执行内容和目标，请确保每个步骤的所有细节合并为一个完整的长字符串，整个列表仅包含 3 到 4 个元素。"
    )


class CriticOutput(BaseModel):
    is_acceptable: bool = Field(description="该草稿是否高质量地解答了用户的研究主题，并且充分利用了提供的文献资料？")
    summary: str = Field(description="一段简洁总评，明确说明合格或不合格的核心原因。")
    missing_evidence: List[str] = Field(description="草稿中缺失的关键证据、数据、来源或时间范围问题。没有则返回空列表。")
    missing_topics: List[str] = Field(description="草稿中缺失的研究主题点或未展开的内容。没有则返回空列表。")
    revision_actions: List[str] = Field(description="下一轮修改时必须执行的具体动作。没有则返回空列表。")
    next_step: str = Field(description="下一步动作，只允许返回 finish、writer、researcher 之一。")
