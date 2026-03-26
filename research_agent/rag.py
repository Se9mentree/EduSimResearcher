from typing import Any, Dict, List

from research_agent.config import RAG_TOP_K


def retrieve_related_papers(query: str, input_paper: str, top_k: int = RAG_TOP_K) -> List[str]:
    """RAG 占位接口：后续在这里接你的向量数据库检索。"""
    _ = query, input_paper, top_k
    return []


def upsert_paper_to_vector_db(paper: Dict[str, Any]) -> None:
    """RAG 占位接口：后续在这里接你的论文入库逻辑。"""
    _ = paper
