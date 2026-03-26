import argparse
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from research_agent.state import build_initial_state
from research_agent.workflow import app


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "outputs"
LIST_APPEND_FIELDS = {"messages", "searched_queries", "revision_history", "evidence_gaps"}


def parse_args():
    parser = argparse.ArgumentParser(description="Run the Auto-Researcher workflow with a PDF paper.")
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
    return parser.parse_args()


def merge_state(current_state: Dict[str, Any], state_update: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in state_update.items():
        if key in LIST_APPEND_FIELDS and isinstance(current_state.get(key), list) and isinstance(value, list):
            current_state[key] = current_state[key] + value
        else:
            current_state[key] = value
    return current_state


def write_final_report(state: Dict[str, Any]) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_title = state.get("input_paper_title", "").strip() or "untitled_paper"
    safe_title = "".join(ch for ch in safe_title if ch.isalnum() or ch in {"_", "-", " "}).strip().replace(" ", "_")
    file_name = f"report_{timestamp}_{safe_title[:50] or 'paper'}.md"
    output_path = OUTPUT_DIR / file_name

    plan_lines = "\n".join(f"{idx}. {step}" for idx, step in enumerate(state.get("plan", []), start=1)) or "暂无"
    searched_queries = state.get("searched_queries", [])
    searched_query_lines = "\n".join(f"- {q}" for q in searched_queries) if searched_queries else "- 暂无"
    evidence_gaps = state.get("evidence_gaps", [])
    evidence_gap_lines = "\n".join(f"- {g}" for g in evidence_gaps) if evidence_gaps else "- 暂无"

    report_content = f"""# Auto Researcher 最终报告

- 生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
- 研究主题: {state.get("query", "")}
- 论文路径: {state.get("paper_path", "")}
- 论文标题: {state.get("input_paper_title", "")}
- 解析状态: {state.get("input_paper_parse_status", "")}
- 解析说明: {state.get("input_paper_parse_notes", "")}
- 修订轮次: {state.get("revision_number", 0)}
- 审稿路由: {state.get("critic_next_step", "")}

## 研究计划

{plan_lines}

## Researcher 工具调用轨迹

{searched_query_lines}

## 最终草稿

{state.get("draft", "暂无草稿")}

## Critic 总结

{state.get("critic", "暂无审稿结论")}

## 证据缺口

{evidence_gap_lines}
"""

    output_path.write_text(report_content, encoding="utf-8")
    return output_path


if __name__ == "__main__":
    args = parse_args()
    initial_state = build_initial_state(
        query=args.query,
        paper_path=args.paper_path,
    )
    final_state: Dict[str, Any] = dict(initial_state)

    print("🚀 开始执行 Agent Workflow...\n")

    for event in app.stream(initial_state):
        for node_name, state_update in event.items():
            merge_state(final_state, state_update)
            print(f"✅ [节点执行完毕]: {node_name}")
            print(f"   [状态更新]: {state_update}\n")

    report_path = write_final_report(final_state)
    print(f"📄 最终报告已写入: {report_path}")
    print("🎉 工作流执行结束！")
