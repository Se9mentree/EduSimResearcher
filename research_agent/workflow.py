from langgraph.graph import END, StateGraph

from research_agent.config import MAX_REVISIONS
from research_agent.nodes import (
    critic_node,
    paper_ingest_node,
    paper_ingest_router,
    planner_node,
    reflection_router,
    researcher_node,
    writer_node,
)
from research_agent.state import AgentState


workflow = StateGraph(AgentState)

workflow.add_node("paper_ingest", paper_ingest_node)
workflow.add_node("planner", planner_node)
workflow.add_node("researcher", researcher_node)
workflow.add_node("writer", writer_node)
workflow.add_node("critic", critic_node)

workflow.set_entry_point("paper_ingest")
workflow.add_conditional_edges(
    "paper_ingest",
    paper_ingest_router,
    {
        "planner": "planner",
        "end": END,
    },
)
workflow.add_edge("planner", "researcher")
workflow.add_edge("researcher", "writer")
workflow.add_edge("writer", "critic")


def bounded_reflection_router(state: AgentState):
    if state.get("revision_number", 0) >= MAX_REVISIONS and state.get("critic_next_step") != "finish":
        return "end"
    return reflection_router(state)


workflow.add_conditional_edges(
    "critic",
    bounded_reflection_router,
    {
        "researcher": "researcher",
        "writer": "writer",
        "end": END,
    },
)

app = workflow.compile()
