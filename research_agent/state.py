import operator
from typing import Annotated, Dict, List, TypedDict

from langchain_core.messages import BaseMessage


class AgentState(TypedDict):
    """State of the agent, including its memory and any relevant information."""

    # 1. User query or task description
    query: str

    # 2. Memoryflow
    messages: Annotated[list[BaseMessage], operator.add]

    # 3. Planning and current task status
    plan: List[str]
    current_step: str

    # 4. External knowledge and resources
    documents: List[str]

    # 5. Output and self-reflection
    draft: str
    revision_number: int
    critic: str
    critic_next_step: str

    # agent memory
    searched_queries: Annotated[List[str], operator.add]
    revision_history: Annotated[List[str], operator.add]
    evidence_gaps: Annotated[List[str], operator.add]
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
        "current_step": "",
        "documents": [],
        "draft": "",
        "critic": "",
        "critic_next_step": "writer",
        "revision_number": 0,
        "searched_queries": [],
        "revision_history": [],
        "evidence_gaps": [],
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
