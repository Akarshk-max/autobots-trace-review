from typing import List

from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.action.action.common_action_models import DataBlock, RunSource
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.user.user_directory import keep_mentionable
from src.autobots.user.user_orm_model import UserORM


async def accept_blocks(
    db: AsyncIOMotorDatabase,
    blocks: List[DataBlock],
    *,
    ctx: Context,
    app_state: AppState,
    sender: UserORM,
    run_id: str | None,
    source: RunSource = RunSource.api,
    keep_existing_author: bool = False,
) -> List[DataBlock]:
    """Blocks as the server will store them, for every path that takes one.

    One directory call for the whole list, so a queue of fifty costs one lookup.

    `keep_existing_author` is for a list being rewritten rather than added to:
    re-stamping there would re-attribute everybody else's message to whoever
    reordered it.
    """
    stamped = [
        block
        if keep_existing_author and block.user_id
        else block.stamped_by(str(sender.id), source=source)
        for block in blocks
    ]

    mentioned = [user_id for block in stamped for user_id in block.mentions]
    if not mentioned:
        return stamped

    allowed = await keep_mentionable(
        db,
        ctx=ctx,
        app_state=app_state,
        sender=sender,
        run_id=run_id,
        user_ids=mentioned,
    )
    return [
        block.model_copy(
            update={"mentions": [u for u in block.mentions if u in allowed]}
        )
        if block.mentions
        else block
        for block in stamped
    ]
