import operator
from typing import Annotated, Any, Dict, List, TypedDict

from langchain_core.messages import BaseMessage


class AgentState(TypedDict):
    """State of the agent, including its memory and any relevant information."""

    # 1. User query or task description
    query: str

    # 2. Memoryflow
    messages: Annotated[list[BaseMessage], operator.add]

    # 3. Planning and current task status
    plan: List[str]
    plan_structured: List[Dict[str, str]]
    current_step: str

    # 4. External knowledge and resources
    documents: List[str]
    evidence_cards: List[Dict[str, Any]]

    # 5. Output and self-reflection
    draft: str
    writer_structured_draft: str
    writer_claims: List[Dict[str, Any]]
    revision_number: int
    critic: str
    critic_status: str
    critic_next_step: str
    critic_scores: Dict[str, int]
    critic_failed_gates: List[str]
    hard_failed_gates: List[str]
    soft_failed_gates: List[str]
    critic_actions: List[str]
    critic_actions_for_researcher: List[str]
    critic_actions_for_writer: List[str]
    warnings: List[str]
    unresolved_gaps: List[str]
    blocked_by_capability: List[str]

    # agent memory
    searched_queries: List[str]
    revision_history: Annotated[List[str], operator.add]
    evidence_gaps: List[str]
    react_trace: List[str]
    react_step_count: int
    react_stop_reason: str
    working_memory: str

    # paper / RAG context
    paper_path: str
    input_paper: str
    input_paper_title: str
    input_paper_abstract: str
    input_paper_sections: Dict[str, str]
    input_paper_key_sections: Dict[str, str]
    input_paper_parse_status: str
    input_paper_parse_notes: str
    retrieved_papers: List[str]
    rag_context: str


def build_initial_state(
    query: str,
    paper_path: str = "",
    input_paper: str = "",
    input_paper_title: str = "",
) -> AgentState:
    return {
        "query": query,
        "messages": [],
        "plan": [],
        "plan_structured": [],
        "current_step": "",
        "documents": [],
        "evidence_cards": [],
        "draft": "",
        "writer_structured_draft": "",
        "writer_claims": [],
        "critic": "",
        "critic_status": "",
        "critic_next_step": "writer",
        "critic_scores": {},
        "critic_failed_gates": [],
        "hard_failed_gates": [],
        "soft_failed_gates": [],
        "critic_actions": [],
        "critic_actions_for_researcher": [],
        "critic_actions_for_writer": [],
        "warnings": [],
        "unresolved_gaps": [],
        "blocked_by_capability": [],
        "revision_number": 0,
        "searched_queries": [],
        "revision_history": [],
        "evidence_gaps": [],
        "react_trace": [],
        "react_step_count": 0,
        "react_stop_reason": "",
        "working_memory": "",
        "paper_path": paper_path,
        "input_paper": input_paper,
        "input_paper_title": input_paper_title,
        "input_paper_abstract": "",
        "input_paper_sections": {},
        "input_paper_key_sections": {},
        "input_paper_parse_status": "",
        "input_paper_parse_notes": "",
        "retrieved_papers": [],
        "rag_context": "",
    }
