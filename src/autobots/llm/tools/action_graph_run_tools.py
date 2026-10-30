from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from src.autobots.action_graph.action_graph_result.action_graph_result_model_doc import (
    ActionGraphResultDoc,
    ActionGraphResultDocsFound,
    ActionGraphResultFind,
)
from src.autobots.action_graph.action_graph_result.user_action_graph_result import (
    UserActionGraphResult,
)
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState

class ActionGraphRunTools:
    class ActionGraphRunId(BaseModel):
        action_run_id: str

    class ListActionGraphRunConfig(ActionGraphResultFind):
        """
        List stored action-graph runs (paged) for the current user.

        Supports all ActionGraphResultFind filter fields plus pagination.

        NOTE: `offset` is treated as a page index (0-based), consistent with
        UserActionGraphResult.list_page_action_graph_result().
        """

        limit: int = Field(
            default=100,
            ge=1,
            le=500,
            description="Page size (number of runs to return).",
        )
        offset: int = Field(
            default=0,
            ge=0,
            description=(
                "Page index (0-based). 0 = first page, 1 = second page, etc. "
                "This is NOT a row offset."
            ),
        )

    @staticmethod
    async def get_action_graph_run(
        config: ActionGraphRunId,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
        space_member: SpaceMemberDoc | None = None,
    ) -> ActionGraphResultDoc | HTTPException:
        """
        Description
        -----------
        Fetches a previously executed action-graph run by its run identifier.
        The stored run document is returned with sensitive node-level config
        payloads removed (each action's `config` field inside the graph result's
        output is cleared) to avoid leaking internal configuration. If the run
        cannot be found or an error occurs, an HTTPException is returned.

        Input
        -----
        config : dict
            Expected structure:
                {
                    "action_run_id": "<unique id of the action-graph run>"
                }

            action_run_id : str
                Identifier of the action-graph run to retrieve.

        Output
        ------
        ActionGraphResultDoc | HTTPException
            On success returns the full ActionGraphResultDoc with fields such as:
                - id/run identifiers
                - status
                - timestamps
                - result: {
                    "input": <original input>,
                    "output": { "<node_id>": <ActionDoc>, ... },
                    ...
                  }
                - error_message (optional)

            Notes and guarantees:
              - Before returning, any `ActionDoc.config` values found inside
                `result.output` are replaced with empty objects to avoid
                exposing internal configuration.
              - If the run is not present or retrieval fails, returns an
                HTTPException describing the failure.
        """
        user_action_graph_run = UserActionGraphResult(user=user, db=db)
        action_run_graph_doc = await user_action_graph_run.get_action_graph_result(
            ctx=ctx, action_graph_result_id=config.action_run_id
        )
        match action_run_graph_doc:
            case None:
                return HTTPException(status_code=500, detail="Action Graph Run get failed")
            case _:
                if (
                    action_run_graph_doc
                    and action_run_graph_doc.result
                    and action_run_graph_doc.result.output
                    and isinstance(action_run_graph_doc.result.output, dict)
                ):
                    for node_id, action_doc in action_run_graph_doc.result.output.items():
                        if action_doc and action_doc.config:
                            action_doc.config = {}
                return action_run_graph_doc

    @staticmethod
    async def list_action_graph_run(
        config: ListActionGraphRunConfig,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
        space_member: SpaceMemberDoc | None = None,
    ) -> ActionGraphResultDocsFound | HTTPException:
        """
        Description
        -----------
        Returns a paginated list of action-graph run results (lite docs) for the current user.

        Pagination
        ----------
        `limit` is the page size. `offset` is a *page index* (0-based), not a row offset.
        Example: limit=5, offset=0 -> newest 5; limit=5, offset=1 -> next 5.
        """
        try:
            user_action_graph_run = UserActionGraphResult(user=user, db=db)

            find = ActionGraphResultFind.model_validate(
                config.model_dump(exclude_none=True, exclude={"limit", "offset"})
            )

            page = await user_action_graph_run.list_page_action_graph_result(
                action_graph_result_find=find,
                limit=int(config.limit),
                offset=int(config.offset),
            )

            # Best-effort sanitize any config fields if present in lite docs
            for doc in page.docs:
                if doc and doc.result and getattr(doc.result, "config", None):
                    doc.result.config = {}

            return page
        except Exception as e:
            return HTTPException(
                status_code=500, detail=f"Action Graph Run list failed: {str(e)}"
            )

    @staticmethod
    async def get_action_graph_run_status(
        config: ActionGraphRunId,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
        space_member: SpaceMemberDoc | None = None,
    ) -> str | HTTPException:
        """
        Description
        -----------
        Returns a human-readable status string for a stored action-graph run.
        The function reads the stored run and returns its status. If the run
        contains an error message, that message is appended to the status for
        quick diagnostics. If the run cannot be retrieved, the underlying
        HTTPException is returned.

        Input
        -----
        config : dict
            Expected structure:
                {
                    "action_run_id": "<unique id of the action-graph run>"
                }

            action_run_id : str
                Identifier of the action-graph run whose status should be fetched.

        Output
        ------
        str | HTTPException
            Returns a concise status string such as:
                "pending", "running", "completed", "failed"
            If an error_message exists on the run it is appended, e.g.
                "failed - <error message>"
            If retrieval fails, returns an HTTPException describing the failure.
        """
        action_graph_run_doc = await ActionGraphRunTools.get_action_graph_run(
            config=config, ctx=ctx, app_state=app_state, user=user, db=db
        )
        if isinstance(action_graph_run_doc, ActionGraphResultDoc):
            status = action_graph_run_doc.status
            if action_graph_run_doc.error_message:
                status = f"{status} - {action_graph_run_doc.error_message}"
            return status
        else:
            return action_graph_run_doc
