from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from mcp import ClientSession
from mcp.client.sse import sse_client

from research_agent.config import PDF_PARSE_TIMEOUT_SEC, PDF_READER_MCP_SSE_URL


@dataclass
class PaperDocument:
    paper_path: str
    title: str = ""
    abstract: str = ""
    full_text: str = ""
    sections: Dict[str, str] = field(default_factory=dict)
    key_sections: Dict[str, str] = field(default_factory=dict)
    citations: List[str] = field(default_factory=list)
    parse_status: str = "failed"  # success | partial | failed
    parse_notes: str = ""


@dataclass
class CapabilityResult:
    ok: bool
    capability: str
    tool_name: str = ""
    content: str = ""
    error: str = ""
    is_empty: bool = False


REQUIRED_TOOL_ALIASES: Dict[str, Tuple[str, ...]] = {
    "extract-academic-text": ("extract-academic-text", "extract_academic_text"),
    "extract-abstract": ("extract-abstract", "extract_abstract"),
    "detect-sections": ("detect-sections", "detect_sections"),
    "extract-key-sections": ("extract-key-sections", "extract_key_sections"),
    "extract-citations": ("extract-citations", "extract_citations"),
}

TITLE_TRIM_MARKERS = (
    " abstract ",
    " 摘要 ",
    " introduction ",
    " keywords ",
    " 1 introduction ",
    " code data checkpoints ",
)

TITLE_METADATA_MARKERS = (
    " language technologies institute ",
    " carnegie mellon university ",
    " university ",
    " institute ",
    " department ",
    " arxiv:",
    " doi:",
)


def _extract_tool_payload(result: Any) -> Tuple[str, Any]:
    text_chunks: List[str] = []
    structured_payload: Any = None

    if result is None:
        return "", None

    if hasattr(result, "structuredContent"):
        structured_payload = getattr(result, "structuredContent")
    elif isinstance(result, dict) and "structuredContent" in result:
        structured_payload = result.get("structuredContent")

    if hasattr(result, "content") and result.content:
        for item in result.content:
            if hasattr(item, "text") and item.text:
                text_chunks.append(item.text)
            elif isinstance(item, dict) and item.get("text"):
                text_chunks.append(item["text"])

    if isinstance(result, dict):
        if isinstance(result.get("text"), str):
            text_chunks.append(result["text"])
        if isinstance(result.get("content"), str):
            text_chunks.append(result["content"])

    if not text_chunks:
        text_chunks.append(str(result))

    return "\n".join(text_chunks).strip(), structured_payload


def _validate_paper_path(paper_path: str) -> Optional[str]:
    path = Path(paper_path)
    if not path.exists():
        return f"File not found: {paper_path}"
    if path.suffix.lower() != ".pdf":
        return "Only PDF is supported in paper_parser."
    return None


def _maybe_json_load(raw_text: str) -> Any:
    text = raw_text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _normalize_section_name(name: str) -> str:
    lowered = name.strip().lower()
    aliases = {
        "abstract": "abstract",
        "introduction": "introduction",
        "related work": "related_work",
        "method": "method",
        "methods": "method",
        "methodology": "method",
        "experiment": "experiments",
        "experiments": "experiments",
        "evaluation": "evaluation",
        "results": "results",
        "discussion": "discussion",
        "conclusion": "conclusion",
        "limitations": "limitations",
    }
    for key, value in aliases.items():
        if key in lowered:
            return value
    return lowered.replace(" ", "_")


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _looks_like_valid_title(candidate: str) -> bool:
    text = _normalize_ws(candidate)
    if len(text) < 8 or len(text) > 220:
        return False
    if text.lower().endswith(".pdf"):
        return False
    if "/" in text or "\\" in text:
        return False
    # Filter obvious filename-like stems: "Author - 2024 - Title"
    if re.search(r"\s-\s\d{4}\s-\s", text):
        return False
    words = text.split()
    if len(words) < 3:
        return False
    digit_ratio = sum(ch.isdigit() for ch in text) / max(len(text), 1)
    if digit_ratio > 0.2:
        return False
    return True


def _extract_title_from_full_text(full_text: str) -> str:
    raw = (full_text or "").strip()
    if not raw:
        return ""

    text = raw
    lowered = text.lower()
    known_prefixes = (
        "full document processed:",
        "academic text extracted:",
    )
    for prefix in known_prefixes:
        if lowered.startswith(prefix):
            text = text[len(prefix) :].strip()
            lowered = text.lower()
            break

    head = _normalize_ws(text)[:1800]
    if not head:
        return ""

    cut_idx = len(head)
    lower_head = head.lower()
    for marker in TITLE_TRIM_MARKERS:
        idx = lower_head.find(marker)
        if idx > 0:
            cut_idx = min(cut_idx, idx)
    head = head[:cut_idx]
    lower_head = head.lower()

    for marker in TITLE_METADATA_MARKERS:
        idx = lower_head.find(marker)
        if idx > 20:
            head = head[:idx]
            lower_head = head.lower()

    author_star = re.search(r"\s+[A-Z][a-z]{1,20}\s+[A-Z][a-z]{1,20}\s*[*∗]", head)
    if author_star and author_star.start() > 10:
        head = head[: author_star.start()]

    candidate = head.strip(" -–—:;|,.")
    if _looks_like_valid_title(candidate):
        return candidate

    # Fallback: check early lines and pick the first valid title-like line.
    for line in text.splitlines()[:20]:
        line_candidate = _normalize_ws(line).strip(" -–—:;|,.")
        if _looks_like_valid_title(line_candidate):
            return line_candidate
    return ""


def _to_sections(raw_text: str, structured_payload: Any = None) -> Dict[str, str]:
    sections: Dict[str, str] = {}

    if isinstance(structured_payload, dict):
        for key, value in structured_payload.items():
            if isinstance(value, str) and value.strip():
                sections[_normalize_section_name(str(key))] = value.strip()
    elif isinstance(structured_payload, list):
        for item in structured_payload:
            if isinstance(item, dict):
                name = item.get("section") or item.get("name") or item.get("title")
                text = item.get("text") or item.get("content")
                if isinstance(name, str) and isinstance(text, str) and text.strip():
                    sections[_normalize_section_name(name)] = text.strip()

    if sections:
        return sections

    parsed_json = _maybe_json_load(raw_text)
    if isinstance(parsed_json, dict):
        for key, value in parsed_json.items():
            if isinstance(value, str) and value.strip():
                sections[_normalize_section_name(str(key))] = value.strip()
        if sections:
            return sections
    elif isinstance(parsed_json, list):
        for item in parsed_json:
            if isinstance(item, dict):
                name = item.get("section") or item.get("name") or item.get("title")
                text = item.get("text") or item.get("content")
                if isinstance(name, str) and isinstance(text, str) and text.strip():
                    sections[_normalize_section_name(name)] = text.strip()
        if sections:
            return sections

    if raw_text.strip():
        sections["raw"] = raw_text.strip()
    return sections


async def _call_pdf_reader_tool(session: ClientSession, tool_name: str, args: Dict[str, Any]) -> Tuple[str, Any]:
    result = await session.call_tool(tool_name, args)
    return _extract_tool_payload(result)


def _resolve_required_tools(available_tools: Set[str]) -> Tuple[Dict[str, str], List[str]]:
    resolved: Dict[str, str] = {}
    missing: List[str] = []
    for capability, aliases in REQUIRED_TOOL_ALIASES.items():
        selected = next((alias for alias in aliases if alias in available_tools), "")
        if selected:
            resolved[capability] = selected
        else:
            missing.append(capability)
    return resolved, missing


async def _health_check(session: ClientSession) -> Tuple[Dict[str, str], str]:
    try:
        tools_response = await session.list_tools()
        tool_names = set()
        if hasattr(tools_response, "tools"):
            for tool in tools_response.tools:
                if hasattr(tool, "name"):
                    tool_names.add(tool.name)
                elif isinstance(tool, dict) and tool.get("name"):
                    tool_names.add(tool["name"])
        resolved_tools, missing = _resolve_required_tools(tool_names)
        if missing:
            missing_desc = ", ".join(
                f"{name} ({' | '.join(REQUIRED_TOOL_ALIASES[name])})" for name in missing
            )
            return {}, f"MCP connected, but required capabilities missing: {missing_desc}"
        return resolved_tools, ""
    except Exception as e:
        return {}, f"Failed to list MCP tools: {type(e).__name__}: {e}"


async def _resolve_single_capability_tool(
    session: ClientSession,
    capability: str,
) -> Tuple[str, str]:
    if capability not in REQUIRED_TOOL_ALIASES:
        available = ", ".join(sorted(REQUIRED_TOOL_ALIASES.keys()))
        return "", f"Unsupported capability: {capability}. Available: {available}"
    try:
        tools_response = await session.list_tools()
        tool_names = set()
        if hasattr(tools_response, "tools"):
            for tool in tools_response.tools:
                if hasattr(tool, "name"):
                    tool_names.add(tool.name)
                elif isinstance(tool, dict) and tool.get("name"):
                    tool_names.add(tool["name"])
        for alias in REQUIRED_TOOL_ALIASES[capability]:
            if alias in tool_names:
                return alias, ""
        alias_desc = " | ".join(REQUIRED_TOOL_ALIASES[capability])
        return "", f"Capability not available: {capability} ({alias_desc})"
    except Exception as e:
        return "", f"Failed to list MCP tools: {type(e).__name__}: {e}"


async def _parse_with_mcp(paper_path: str) -> PaperDocument:
    doc = PaperDocument(paper_path=paper_path)
    async with sse_client(PDF_READER_MCP_SSE_URL) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            resolved_tools, health_error = await _health_check(session)
            if health_error:
                doc.parse_status = "failed"
                doc.parse_notes = health_error
                return doc

            full_text, _ = await _call_pdf_reader_tool(
                session, resolved_tools["extract-academic-text"], {"file_path": paper_path}
            )
            abstract, _ = await _call_pdf_reader_tool(
                session, resolved_tools["extract-abstract"], {"file_path": paper_path}
            )
            sections_text, sections_structured = await _call_pdf_reader_tool(
                session, resolved_tools["detect-sections"], {"file_path": paper_path}
            )
            key_sections_text, key_sections_structured = await _call_pdf_reader_tool(
                session, resolved_tools["extract-key-sections"], {"file_path": paper_path}
            )
            citations_text, _ = await _call_pdf_reader_tool(
                session, resolved_tools["extract-citations"], {"file_path": paper_path}
            )

    doc.title = _extract_title_from_full_text(full_text) or Path(paper_path).stem
    doc.abstract = abstract.strip()
    doc.full_text = full_text.strip()
    doc.sections = _to_sections(sections_text, sections_structured) or _to_sections(full_text)
    doc.key_sections = _to_sections(key_sections_text, key_sections_structured)

    if citations_text.strip():
        doc.citations = [line.strip() for line in citations_text.splitlines() if line.strip()]

    if doc.full_text and doc.abstract and (doc.sections or doc.key_sections):
        doc.parse_status = "success"
        doc.parse_notes = "MCP parse complete."
    elif doc.full_text:
        doc.parse_status = "partial"
        doc.parse_notes = "Full text extracted, but abstract/sections are incomplete."
    else:
        doc.parse_status = "failed"
        doc.parse_notes = "No text extracted from PDF."

    return doc


async def _run_capability_with_mcp(
    paper_path: str,
    capability: str,
    extra_args: Optional[Dict[str, Any]] = None,
) -> CapabilityResult:
    result = CapabilityResult(ok=False, capability=capability)
    if capability not in REQUIRED_TOOL_ALIASES:
        available = ", ".join(sorted(REQUIRED_TOOL_ALIASES.keys()))
        result.error = f"Unsupported capability: {capability}. Available: {available}"
        return result

    args = {"file_path": paper_path}
    if extra_args:
        args.update(extra_args)

    async with sse_client(PDF_READER_MCP_SSE_URL) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            tool_name, resolve_error = await _resolve_single_capability_tool(session, capability)
            if resolve_error:
                result.error = resolve_error
                return result
            result.tool_name = tool_name
            text, _ = await _call_pdf_reader_tool(session, tool_name, args)
            if not text.strip():
                result.ok = True
                result.is_empty = True
                result.content = ""
                return result
            result.ok = True
            result.content = text.strip()
            return result


def load_paper(paper_path: str) -> PaperDocument:
    validation_error = _validate_paper_path(paper_path)
    if validation_error:
        return PaperDocument(
            paper_path=paper_path,
            parse_status="failed",
            parse_notes=validation_error,
        )

    path = Path(paper_path)
    try:
        return asyncio.run(asyncio.wait_for(_parse_with_mcp(str(path.resolve())), timeout=PDF_PARSE_TIMEOUT_SEC))
    except Exception as e:
        hint = ""
        error_message = str(e)
        if "ConnectError" in error_message or "Connection refused" in error_message:
            hint = " Ensure pdf-reader-mcp is running at the configured SSE URL."
        elif "socksio" in error_message.lower():
            hint = " Detected SOCKS proxy without socksio. Unset proxy vars for localhost or install socksio."
        return PaperDocument(
            paper_path=str(path.resolve()),
            parse_status="failed",
            parse_notes=f"MCP parse error: {type(e).__name__}: {e}.{hint}",
        )


def run_pdf_reader_capability(
    paper_path: str,
    capability: str,
    extra_args: Optional[Dict[str, Any]] = None,
) -> str:
    result = run_pdf_reader_capability_result(
        paper_path=paper_path,
        capability=capability,
        extra_args=extra_args,
    )
    if result.ok:
        if result.is_empty:
            return "Tool returned empty content."
        return result.content
    return f"MCP capability error: {result.error}"


def run_pdf_reader_capability_result(
    paper_path: str,
    capability: str,
    extra_args: Optional[Dict[str, Any]] = None,
) -> CapabilityResult:
    validation_error = _validate_paper_path(paper_path)
    if validation_error:
        return CapabilityResult(
            ok=False,
            capability=capability,
            error=validation_error,
        )

    path = Path(paper_path).resolve()
    try:
        return asyncio.run(
            asyncio.wait_for(
                _run_capability_with_mcp(str(path), capability, extra_args=extra_args),
                timeout=PDF_PARSE_TIMEOUT_SEC,
            )
        )
    except Exception as e:
        hint = ""
        error_message = str(e)
        if "ConnectError" in error_message or "Connection refused" in error_message:
            hint = " Ensure pdf-reader-mcp is running at the configured SSE URL."
        elif "socksio" in error_message.lower():
            hint = " Detected SOCKS proxy without socksio. Unset proxy vars for localhost or install socksio."
        return CapabilityResult(
            ok=False,
            capability=capability,
            error=f"{type(e).__name__}: {e}.{hint}",
        )
