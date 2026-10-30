"""Whether a caller may add a turn to a run, and as whom.

Shared by every run type that can be shared. The three run services are
structurally parallel but unrelated, so this arrives as a mixin over one hook
rather than as a base class - the alternative is restructuring three large
classes that work.
"""

from typing import Any, Optional, Tuple

from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_model import Participation
from src.autobots.user.user import User
from src.autobots.user.user_orm_model import UserORM


class TurnAuthorization:
    """Adds `authorize_turn` to a run service.

    The service must supply `_owned_or_shared_run`, and must construct from
    `(user, db)` - which all of them do, and which is what lets the owner's view
    be built as the same type without this knowing which type that is.
    """

    #: Named per run type so a 404 says what was not found.
    _RUN_NOT_FOUND: str = "Run not found"

    async def _owned_or_shared_run(
        self, run_id: str, ctx: Context | None = None
    ) -> Optional[Any]:
        """The run if the caller may see it at all, otherwise None."""
        raise NotImplementedError

    async def authorize_turn(
        self,
        run_id: str,
        db: AsyncIOMotorDatabase,
        ctx: Context | None = None,
    ) -> Tuple[Any, "TurnAuthorization"]:
        """Check the caller may add to this run, then hand back the owner's view of it.

        Two identities, and the order is the design. The check uses the caller,
        because "may you post here" is a question about them. Everything after
        uses the owner, because a run service carries its user into the Mongo
        filter and the S3 key prefix alike - built for a grantee it would write
        the owner's conversation into the grantee's own storage, which succeeds
        and is wrong.

        Raises 404 when the run is not theirs to see, and 403 when it is
        read-only, so the two are distinguishable to a client and neither
        reveals anything to a stranger.
        """
        run = await self._owned_or_shared_run(run_id, ctx)
        if run is None:
            raise HTTPException(status_code=404, detail=self._RUN_NOT_FOUND)

        if run.user_id == self.user_id:
            return run, self

        if run.participation == Participation.read_only:
            raise HTTPException(
                status_code=403, detail="This run is shared for reading only"
            )
        owner = await User.get(
            ctx=ctx or Context(user_id=run.user_id),
            user=UserORM(id=run.user_id),
            id=run.user_id,
            db=db,
        )
        # The stored user carries no space membership, and a find without one is
        # not unscoped - `_build_filter` injects `space_id: None`, so the owner's
        # service could only ever see personal runs. The space belongs to the
        # request rather than to the identity, and it is the one this run was
        # just found in.
        as_owner = type(self)(
            UserORM.model_validate(
                {**owner.model_dump(), "space_member": self.user.space_member}
            ),
            db,
        )
        return run, as_owner
