import argparse
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from research_agent.rag import upsert_paper_to_vector_db
from research_agent.state import build_initial_state
from research_agent.workflow import app


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "outputs"
LIST_APPEND_FIELDS = {
    "messages",
    "revision_history",
}


def parse_args():
    parser = argparse.ArgumentParser(description="Run the Auto-Researcher workflow.")
    parser.add_argument(
        "paper_path",
        nargs="?",
        default="",
        help="PDF path as positional argument.",
    )
    parser.add_argument(
        "--query",
        default="请分析这篇论文并结合我的研究方向推进研究",
        help="User research query.",
    )
    parser.add_argument(
        "--mode",
        choices=("single_paper", "topic_synthesis"),
        default="single_paper",
        help="single_paper: analyze one input PDF; topic_synthesis: summarize entry points from RAG corpus.",
    )
    parser.add_argument(
        "--add-to-rag",
        choices=("y", "n"),
        default="n",
        help="Whether to upsert the current paper into local RAG vector DB (y/n).",
    )
    return parser.parse_args()


def merge_state(current_state: Dict[str, Any], state_update: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in state_update.items():
        if key in LIST_APPEND_FIELDS and isinstance(current_state.get(key), list) and isinstance(value, list):
            current_state[key] = current_state[key] + value
        else:
            current_state[key] = value
    return current_state


def maybe_upsert_current_paper_to_rag(state: Dict[str, Any], enabled: bool) -> str:
    if not enabled:
        return "disabled"
    if str(state.get("mode", "single_paper")).strip().lower() != "single_paper":
        return "skipped: mode_not_single_paper"

    paper_path = str(state.get("paper_path", "")).strip()
    if not paper_path:
        return "skipped: empty_paper_path"

    parse_status = str(state.get("input_paper_parse_status", "")).strip().lower()
    if parse_status != "success":
        return f"skipped: parse_status={parse_status or 'unknown'}"

    payload = {
        "paper_path": paper_path,
        "source_path": paper_path,
        "title": str(state.get("input_paper_title", "")).strip(),
        "abstract": str(state.get("input_paper_abstract", "")).strip(),
        "full_text": str(state.get("input_paper", "")).strip(),
        "sections": state.get("input_paper_sections", {}) or {},
        "key_sections": state.get("input_paper_key_sections", {}) or {},
    }
    has_content = bool(payload["abstract"] or payload["full_text"] or payload["sections"] or payload["key_sections"])
    if not has_content:
        return "skipped: empty_parsed_content"

    try:
        upsert_paper_to_vector_db(payload)
    except Exception as e:
        return f"failed: {type(e).__name__}: {e}"

    return "success"


def write_final_report(state: Dict[str, Any]) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    mode = str(state.get("mode", "single_paper")).strip().lower()
    raw_title = state.get("input_paper_title", "").strip()
    if mode == "topic_synthesis":
        query_part = re.sub(r"\s+", "_", str(state.get("query", "")).strip())[:80]
        raw_title = raw_title or f"topic_synthesis_{query_part or 'agent_simulation'}"
    elif not raw_title:
        raw_title = Path(state.get("paper_path", "")).stem.strip() or "untitled_paper"

    safe_title = re.sub(r'[\\/:*?"<>|]', "_", raw_title).strip().rstrip(".")
    file_name = f"{safe_title or 'untitled_paper'}.md"
    output_path = OUTPUT_DIR / file_name
    if output_path.exists():
        output_path = OUTPUT_DIR / f"{safe_title or 'untitled_paper'}_{timestamp}.md"

    plan_lines = "\n".join(f"{idx}. {step}" for idx, step in enumerate(state.get("plan", []), start=1)) or "暂无"
    searched_queries = state.get("searched_queries", [])
    searched_query_lines = "\n".join(f"- {q}" for q in searched_queries) if searched_queries else "- 暂无"
    evidence_gaps = state.get("evidence_gaps", [])
    evidence_gap_lines = "\n".join(f"- {g}" for g in evidence_gaps) if evidence_gaps else "- 暂无"
    react_trace = state.get("react_trace", [])
    react_trace_lines = "\n".join(f"- {r}" for r in react_trace) if react_trace else "- 暂无"
    unresolved_gaps = state.get("unresolved_gaps", [])
    unresolved_lines = "\n".join(f"- {g}" for g in unresolved_gaps[:8]) if unresolved_gaps else "- 暂无"
    actions_for_researcher = state.get("critic_actions_for_researcher", [])
    actions_for_writer = state.get("critic_actions_for_writer", [])
    actions_for_researcher_lines = (
        "\n".join(f"- {x}" for x in actions_for_researcher) if actions_for_researcher else "- 暂无"
    )
    actions_for_writer_lines = (
        "\n".join(f"- {x}" for x in actions_for_writer) if actions_for_writer else "- 暂无"
    )
    critic_status = state.get("critic_status", "")
    if critic_status == "pass":
        final_judgement = "通过"
    elif critic_status == "pass_with_warnings":
        final_judgement = "通过（含警告）"
    elif critic_status == "finish_with_risks":
        final_judgement = "未通过（带风险结束）"
    else:
        final_judgement = "未通过"

    report_content = f"""# Auto Researcher 最终报告

- 生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
- 功能模式: {state.get("mode", "single_paper")}
- 研究主题: {state.get("query", "")}
- 论文路径: {state.get("paper_path", "")}
- 论文标题: {state.get("input_paper_title", "")}
- 解析状态: {state.get("input_paper_parse_status", "")}
- 解析说明: {state.get("input_paper_parse_notes", "")}
- RAG入库状态: {state.get("rag_ingest_status", "disabled")}
- 修订轮次: {state.get("revision_number", 0)}
- 最终判定: {final_judgement}
- 审稿状态: {state.get("critic_status", "")}
- 审稿路由: {state.get("critic_next_step", "")}
- 硬门槛未通过: {", ".join(state.get("hard_failed_gates", [])) if state.get("hard_failed_gates") else "无"}
- 软门槛未通过: {", ".join(state.get("soft_failed_gates", [])) if state.get("soft_failed_gates") else "无"}
- 能力阻塞项: {", ".join(state.get("blocked_by_capability", [])) if state.get("blocked_by_capability") else "无"}

## 研究计划

{plan_lines}

## Researcher 工具调用轨迹

{searched_query_lines}

## Researcher ReAct 轨迹

- 步数: {state.get("react_step_count", 0)}
- 停止原因: {state.get("react_stop_reason", "")}
{react_trace_lines}

## 未解决硬缺口

{unresolved_lines}

## 最终草稿

{state.get("draft", "暂无草稿")}

## Critic 总结

{state.get("critic", "暂无审稿结论")}

## Critic 警告

{"\n".join(f"- {w}" for w in state.get("warnings", [])) if state.get("warnings") else "- 暂无"}

## Critic 给 Researcher 的建议

{actions_for_researcher_lines}

## Critic 给 Writer 的建议

{actions_for_writer_lines}

## 证据缺口

{evidence_gap_lines}
"""

    output_path.write_text(report_content, encoding="utf-8")
    return output_path


if __name__ == "__main__":
    args = parse_args()
    if args.mode == "single_paper" and not str(args.paper_path).strip():
        raise SystemExit("single_paper 模式必须提供 paper_path。")

    initial_state = build_initial_state(
        query=args.query,
        mode=args.mode,
        paper_path=args.paper_path,
    )
    final_state: Dict[str, Any] = dict(initial_state)

    print("🚀 开始执行 Agent Workflow...\n")

    for event in app.stream(initial_state):
        for node_name, state_update in event.items():
            merge_state(final_state, state_update)
            print(f"✅ [节点执行完毕]: {node_name}")
            print(f"   [状态更新]: {state_update}\n")

    rag_ingest_status = maybe_upsert_current_paper_to_rag(final_state, enabled=(args.add_to_rag == "y"))
    final_state["rag_ingest_status"] = rag_ingest_status
    if args.add_to_rag == "y":
        print(f"🗂️ [RAG入库]: {rag_ingest_status}")
    else:
        print("🗂️ [RAG入库]: disabled")

    report_path = write_final_report(final_state)
    print(f"📄 最终报告已写入: {report_path}")
    print("🎉 工作流执行结束！")
