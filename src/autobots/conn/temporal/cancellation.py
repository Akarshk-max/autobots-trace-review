from loguru import logger
from temporalio.service import RPCError, RPCStatusCode

from src.autobots.conn.temporal.client import get_temporal_client


async def cancel_workflow(workflow_id: str) -> bool:
    temporal_client = await get_temporal_client()
    try:
        await temporal_client.get_workflow_handle(workflow_id).cancel()
    except RPCError as e:
        if e.status == RPCStatusCode.NOT_FOUND:
            logger.bind(workflow_id=workflow_id).info("No running workflow to cancel")
            return False
        raise
    logger.bind(workflow_id=workflow_id).info("Workflow cancellation requested")
    return True
