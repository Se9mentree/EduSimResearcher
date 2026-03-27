import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from research_agent.config import (
    EMBEDDING_MODEL,
    RAG_CHUNK_OVERLAP,
    RAG_CHUNK_SIZE,
    RAG_TOP_K,
    VECTOR_DB_COLLECTION,
    VECTOR_DB_DIR,
)

_VECTOR_STORE = None
_TEXT_SPLITTER = None


def _normalize_for_hash(text: str) -> str:
    # Conservative normalization: collapse whitespace only, avoid semantic changes.
    return re.sub(r"\s+", " ", text).strip()


def _get_text_splitter() -> RecursiveCharacterTextSplitter:
    global _TEXT_SPLITTER
    if _TEXT_SPLITTER is None:
        _TEXT_SPLITTER = RecursiveCharacterTextSplitter(
            chunk_size=RAG_CHUNK_SIZE,
            chunk_overlap=RAG_CHUNK_OVERLAP,
        )
    return _TEXT_SPLITTER


def _get_vector_store():
    global _VECTOR_STORE
    if _VECTOR_STORE is not None:
        return _VECTOR_STORE
    try:
        from langchain_chroma import Chroma
        from langchain_huggingface import HuggingFaceEmbeddings
    except Exception as e:
        raise RuntimeError(
            "RAG dependencies are missing. Run: uv add chromadb langchain-chroma "
            "langchain-text-splitters langchain-huggingface sentence-transformers"
        ) from e

    Path(VECTOR_DB_DIR).mkdir(parents=True, exist_ok=True)
    embeddings = HuggingFaceEmbeddings(
        model_name=EMBEDDING_MODEL,
        encode_kwargs={"normalize_embeddings": True},
    )
    _VECTOR_STORE = Chroma(
        collection_name=VECTOR_DB_COLLECTION,
        embedding_function=embeddings,
        persist_directory=VECTOR_DB_DIR,
    )
    return _VECTOR_STORE


def _make_paper_id(paper: Dict[str, Any]) -> str:
    if paper.get("paper_id"):
        return str(paper["paper_id"])
    stable_key = "|".join(
        [
            str(paper.get("title", "")).strip(),
            str(paper.get("source_path", "")).strip(),
            str(paper.get("paper_path", "")).strip(),
        ]
    )
    if not stable_key.strip():
        stable_key = str(paper)
    return hashlib.sha1(stable_key.encode("utf-8")).hexdigest()[:16]


def _build_blocks(paper: Dict[str, Any]) -> List[Dict[str, str]]:
    blocks: List[Dict[str, str]] = []
    abstract = str(paper.get("abstract", "")).strip()
    if abstract:
        blocks.append({"section": "abstract", "text": abstract})

    sections = paper.get("sections", {})
    if isinstance(sections, dict):
        for section_name, section_text in sections.items():
            if isinstance(section_text, str) and section_text.strip():
                blocks.append({"section": str(section_name), "text": section_text.strip()})

    key_sections = paper.get("key_sections", {})
    if isinstance(key_sections, dict):
        for section_name, section_text in key_sections.items():
            if isinstance(section_text, str) and section_text.strip():
                blocks.append({"section": f"key_{section_name}", "text": section_text.strip()})

    full_text = str(paper.get("full_text", "")).strip()
    if not blocks and full_text:
        blocks.append({"section": "full_text", "text": full_text})
    return blocks


def _build_documents_for_paper(paper: Dict[str, Any]) -> List[Document]:
    paper_id = _make_paper_id(paper)
    title = str(paper.get("title", "")).strip() or "untitled"
    source_path = str(paper.get("source_path", "")).strip() or str(paper.get("paper_path", "")).strip()
    splitter = _get_text_splitter()
    blocks = _build_blocks(paper)
    docs: List[Document] = []
    chunk_index = 0
    seen_chunk_hashes = set()

    for block in blocks:
        section_name = block["section"]
        block_text = block["text"]
        for chunk in splitter.split_text(block_text):
            cleaned_chunk = chunk.strip()
            if not cleaned_chunk:
                continue
            chunk_hash = hashlib.sha1(_normalize_for_hash(cleaned_chunk).encode("utf-8")).hexdigest()
            if chunk_hash in seen_chunk_hashes:
                continue
            seen_chunk_hashes.add(chunk_hash)
            docs.append(
                Document(
                    page_content=cleaned_chunk,
                    metadata={
                        "paper_id": paper_id,
                        "title": title,
                        "source_path": source_path,
                        "section": section_name,
                        "chunk_index": chunk_index,
                    },
                )
            )
            chunk_index += 1
    return docs


def retrieve_related_papers(query: str, input_paper: str, top_k: int = RAG_TOP_K) -> List[str]:
    search_query = (query or "").strip()
    if not search_query:
        return []
    if input_paper and input_paper.strip():
        search_query = f"{search_query}\n\nCurrent paper context:\n{input_paper[:1500]}"

    try:
        vector_store = _get_vector_store()
        results = vector_store.similarity_search_with_score(search_query, k=top_k)
    except Exception as e:
        return [f"[RAG_ERROR] {type(e).__name__}: {e}"]

    formatted: List[str] = []
    for idx, item in enumerate(results, start=1):
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        doc, score = item
        title = doc.metadata.get("title", "untitled")
        section = doc.metadata.get("section", "unknown")
        source = doc.metadata.get("source_path", "")
        snippet = (doc.page_content or "").strip()
        if len(snippet) > 800:
            snippet = snippet[:800] + " ..."
        formatted.append(
            f"[RAG#{idx}] score={score:.4f} title={title} section={section} source={source}\n{snippet}"
        )
    return formatted


def upsert_paper_to_vector_db(paper: Dict[str, Any]) -> None:
    vector_store = _get_vector_store()
    documents = _build_documents_for_paper(paper)
    if not documents:
        raise ValueError("No valid text blocks found for vector upsert.")

    paper_id = documents[0].metadata["paper_id"]
    ids = [f"{paper_id}-{doc.metadata['chunk_index']}" for doc in documents]

    try:
        vector_store.delete(where={"paper_id": paper_id})
    except Exception:
        pass

    vector_store.add_documents(documents=documents, ids=ids)
