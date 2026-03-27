import os
from pathlib import Path

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
AGENT_REQUIREMENTS_PATH = BASE_DIR / "agent要求.md"

MAX_REVISIONS = int(os.getenv("MAX_REVISIONS", "2"))
RAG_TOP_K = int(os.getenv("RAG_TOP_K", "5"))
RAG_CHUNK_SIZE = int(os.getenv("RAG_CHUNK_SIZE", "1200"))
RAG_CHUNK_OVERLAP = int(os.getenv("RAG_CHUNK_OVERLAP", "200"))
VECTOR_DB_DIR = os.getenv("VECTOR_DB_DIR", str(BASE_DIR / "data" / "vector_db"))
VECTOR_DB_COLLECTION = os.getenv("VECTOR_DB_COLLECTION", "papers")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")

PDF_READER_MCP_SSE_URL = os.getenv("PDF_READER_MCP_SSE_URL", "http://localhost:8000/sse")
PDF_PARSE_TIMEOUT_SEC = int(os.getenv("PDF_PARSE_TIMEOUT_SEC", "60"))



def get_llm() -> ChatOpenAI:
    return ChatOpenAI(
        model=os.getenv("DASHSCOPE_MODEL", "qwen-plus"),
        api_key=os.getenv("DASHSCOPE_API_KEY"),
        base_url=os.getenv("DASHSCOPE_API_BASE_URL"),
        temperature=0,
    )


def load_agent_requirements() -> str:
    if not AGENT_REQUIREMENTS_PATH.exists():
        return ""
    return AGENT_REQUIREMENTS_PATH.read_text(encoding="utf-8")
