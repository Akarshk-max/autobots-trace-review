def action_workflow_id(action_run_id: str) -> str:
    return f"action-{action_run_id}"


def action_graph_workflow_id(action_graph_result_id: str) -> str:
    return f"action-graph-{action_graph_result_id}"


def action_team_workflow_id(action_team_result_id: str) -> str:
    return f"action-team-{action_team_result_id}"


def tool_workflow_id(tool_run_id: str) -> str:
    return f"tool-{tool_run_id}"
