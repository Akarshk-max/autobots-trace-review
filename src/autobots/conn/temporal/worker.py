from datetime import timedelta

from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from src.autobots.conn.temporal.client import get_temporal_client
from src.autobots.conn.temporal.action.workflow import ActionWorkflow
from src.autobots.conn.temporal.action.activity import execute_action_activity
from src.autobots.conn.temporal.action_graph.workflow import ActionGraphWorkflow
from src.autobots.conn.temporal.action_graph.activity import execute_action_graph_activity
from src.autobots.conn.temporal.action_team.workflow import ActionTeamWorkflow
from src.autobots.conn.temporal.action_team.activity import execute_action_team_activity
from src.autobots.conn.temporal.tool.activity import (
    execute_tool_activity,
    finalize_tool_run_activity,
)
from src.autobots.conn.temporal.tool.workflow import ToolWorkflow
from src.autobots.core.settings import SettingsProvider


async def create_worker() -> Worker:
    settings = SettingsProvider.sget()
    client = await get_temporal_client()
    worker = Worker(
        client,
        task_queue=settings.TEMPORAL_TASK_QUEUE,
        workflows=[ActionWorkflow, ActionGraphWorkflow, ActionTeamWorkflow, ToolWorkflow],
        activities=[
            execute_action_activity,
            execute_action_graph_activity,
            execute_action_team_activity,
            execute_tool_activity,
            finalize_tool_run_activity,
        ],
        workflow_runner=UnsandboxedWorkflowRunner(),
        max_heartbeat_throttle_interval=timedelta(
            seconds=settings.TEMPORAL_HEARTBEAT_THROTTLE_SECONDS
        ),
    )
    return worker
