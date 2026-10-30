from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from src.autobots.action.action_result.action_result_doc_model import (
    ActionResultDoc,
    ActionResultDocsFound,
    ActionResultFind,
)
from src.autobots.action.action_result.user_action_result import UserActionResult
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState

class ActionRunTools:
    class ActionRunId(BaseModel):
        action_run_id: str

    class ListActionRunConfig(ActionResultFind):
        """
        List stored action runs (paged) for the current user.

        Supports all ActionResultFind filter fields (including nested `result.*`)
        plus pagination fields `limit` and `offset`.

        NOTE: `offset` is treated as a page index (0-based), consistent with
        UserActionResult.list_action_result().
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
                "This is NOT a row offset. To skip N rows, use offset = floor(N / limit)."
            ),
        )

    @staticmethod
    async def get_action_run(
        config: ActionRunId,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
        space_member: SpaceMemberDoc | None = None,
    ) -> ActionResultDoc | HTTPException:
        """
        Description
        -----------
        Retrieves the stored result of a previously executed action. The action
        run is identified by its unique run identifier. If found, the result's
        config payload is cleared before returning to avoid leaking internal
        configuration details. If the action run does not exist or retrieval
        fails, an HTTPException is returned.

        Input
        -----
        config : dict
            Expected structure:
                {
                    "action_run_id": "<unique id of the action run>"
                }

            action_run_id : str
                Identifier of the executed action whose result should be fetched.

        Output
        ------
        dict | HTTPException
            On success returns the ActionResultDoc containing:
                - status
                - timestamps
                - result data (with config removed)
                - optional error message

            If the action run cannot be retrieved, an HTTPException with an
            appropriate status code is returned.
        """
        user_actions_run = UserActionResult(user=user, db=db)
        action_run_doc = await user_actions_run.get_action_result(action_result_id=config.action_run_id)
        match action_run_doc:
            case None:
                return HTTPException(status_code=500, detail="Action Run get failed")
            case _:
                if (
                    action_run_doc
                    and action_run_doc.result
                    and getattr(action_run_doc.result, "config", None)
                ):
                    action_run_doc.result.config = {}
                return action_run_doc

    @staticmethod
    async def list_action_run(
        config: ListActionRunConfig,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
        space_member: SpaceMemberDoc | None = None,
    ) -> ActionResultDocsFound | HTTPException:
        """
        Description
        -----------
        Returns a paginated list of action run results (lite docs) for the current user.

        Pagination
        ----------
        `limit` is the page size. `offset` is a *page index* (0-based), not a row offset.
        Example: limit=10, offset=0 -> newest 10; limit=10, offset=1 -> next 10.
        """
        try:
            user_actions_run = UserActionResult(user=user, db=db)

            # Unpack filters (everything except pagination) into an ActionResultFind
            find = ActionResultFind.model_validate(
                config.model_dump(exclude_none=True, exclude={"limit", "offset"})
            )

            page = await user_actions_run.list_action_result(
                action_result_find=find,
                limit=int(config.limit),
                offset=int(config.offset),
            )

            # Best-effort sanitize any config fields (lite docs usually omit these, but be safe)
            for doc in page.docs:
                if doc and doc.result and getattr(doc.result, "config", None):
                    doc.result.config = {}

            return page
        except Exception as e:
            return HTTPException(status_code=500, detail=f"Action Run list failed: {str(e)}")

    @staticmethod
    async def get_action_run_status(
        config: ActionRunId,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
        space_member: SpaceMemberDoc | None = None,
    ) -> str | HTTPException:
        """
        Description
        -----------
        Returns a concise status string for a previously executed action. The
        status is derived from the stored ActionResultDoc. If an error message
        exists on the run result, it is appended to the status for visibility.
        If the action run cannot be retrieved, the underlying HTTPException is
        returned instead.

        Input
        -----
        config : dict
            Expected structure:
                {
                    "action_run_id": "<unique id of the action run>"
                }

            action_run_id : str
                Identifier of the executed action whose status should be checked.

        Output
        ------
        str | HTTPException
            Returns a status string such as:
                "completed"
                "failed - <error message>"
                "running"
            If retrieval fails, returns an HTTPException describing the error.
        """
        action_run_doc = await ActionRunTools.get_action_run(
            config=config, ctx=ctx, user=user, db=db
        )
        if isinstance(action_run_doc, ActionResultDoc):
            status = action_run_doc.status
            if action_run_doc.error_message:
                status = f"{status} - {action_run_doc.error_message}"
            return status
        else:
            return action_run_doc
