from langchain_core.tools import tool

from research_agent.config import RAG_TOP_K
from research_agent.paper_parser import run_pdf_reader_capability_result
from research_agent.rag import get_rag_corpus_overview, retrieve_related_papers


def _format_capability_result(paper_path: str, capability: str) -> str:
    result = run_pdf_reader_capability_result(
        paper_path=paper_path,
        capability=capability,
    )
    if not result.ok:
        return f"[MCP_ERROR] capability={capability} error={result.error}"
    if result.is_empty:
        return f"[MCP_WARN] empty_content capability={capability}"
    return result.content


@tool
def extract_academic_text_tool(paper_path: str) -> str:
    """用于获取论文主体文本。返回按阅读顺序整理后的文本内容，适合提取问题、方法、实验细节；内容可能被上游服务截断。"""
    return _format_capability_result(paper_path, "extract-academic-text")


@tool
def extract_abstract_tool(paper_path: str) -> str:
    """用于快速获取论文摘要。返回摘要文本（若识别失败会返回错误前缀）；适合先构建论文总览，不适合替代全文证据。"""
    return _format_capability_result(paper_path, "extract-abstract")


@tool
def detect_sections_tool(paper_path: str) -> str:
    """用于识别章节结构。返回章节名称与片段预览，适合定位引言/方法/结果；章节检测是规则驱动，可能存在漏检或错分。"""
    return _format_capability_result(paper_path, "detect-sections")


@tool
def extract_key_sections_tool(paper_path: str) -> str:
    """用于抽取关键章节（如 abstract/introduction/method/results 等）。返回聚合后的关键片段，适合写作证据提炼；单节可能被截断。"""
    return _format_capability_result(paper_path, "extract-key-sections")


@tool
def extract_citations_tool(paper_path: str) -> str:
    """用于抽取文内引用与参考文献统计。返回 citation profile 和样例引用，适合判断证据链密度；不保证完整还原参考文献格式。"""
    return _format_capability_result(paper_path, "extract-citations")


@tool
def rag_search_tool(query: str, top_k: int = RAG_TOP_K, input_paper: str = "") -> str:
    """用于检索本地向量库中的相关论文片段。返回带分数和来源信息的命中列表；若检索失败会返回错误前缀。"""
    search_query = (query or "").strip()
    if not search_query:
        return "[MCP_WARN] empty_query capability=rag-search"

    hits = retrieve_related_papers(
        query=search_query,
        input_paper=(input_paper or "").strip(),
        top_k=top_k,
    )
    if not hits:
        return "[MCP_WARN] empty_content capability=rag-search"
    if len(hits) == 1 and hits[0].startswith("[RAG_ERROR]"):
        return f"[MCP_ERROR] capability=rag-search error={hits[0]}"
    return "\n\n".join(hits)


@tool
def rag_corpus_overview_tool(max_papers: int = 120, max_chars_per_paper: int = 420) -> str:
    """用于获取本地向量库的全库论文概览（按 paper_id 聚合）。返回每篇论文的标题、来源和代表片段，适合主题综述与方向切入点提炼。"""
    lines = get_rag_corpus_overview(
        max_papers=max_papers,
        max_chars_per_paper=max_chars_per_paper,
    )
    if not lines:
        return "[MCP_WARN] empty_content capability=rag-corpus-overview"
    if len(lines) == 1 and lines[0].startswith("[RAG_ERROR]"):
        return f"[MCP_ERROR] capability=rag-corpus-overview error={lines[0]}"
    return "\n\n".join(lines)


RESEARCHER_TOOLS = [
    extract_academic_text_tool,
    extract_abstract_tool,
    detect_sections_tool,
    extract_key_sections_tool,
    extract_citations_tool,
    rag_search_tool,
    rag_corpus_overview_tool,
]
