"""Who may read a run's stream.

A topic is a run id, so the check resolves the run document and authorises against
it. It deliberately does not take `space_id` from the caller: the SSE client sends no
query params, and a client-supplied hint is not authoritative anyway.

**A topic this does not recognise is allowed through.** These endpoints are a
general pubsub bridge, not a run-only one - the terminal has its own channels, and
more will follow - and a gate that refuses everything it has not been taught about
breaks each of them on the day it ships, silently, for a reason nobody will connect
to this file.

The exception is `autobots:heartbeat`, which carries every active run id across all
tenants and has no client at all. Refusing one named channel is not the same as
refusing everything unfamiliar.

The cost of that choice is that a **run** topic which stops being recognised stops
being protected, rather than stopping working. `SUFFIXES` and the test that no
provider spells one inline are what keep the recognised set honest; they are load
bearing for authorisation now, not just for tidiness.
"""

import re
from typing import List, Optional

import supabase_auth
from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots import SettingsProvider
from src.autobots.data_model.space_id import SpaceId
from src.autobots.event_result.event_result_crud import EventResultCRUD
from src.autobots.event_result.event_result_model import EventResultLiteDoc
from src.autobots.server_sent_events.topics import base_topic
from src.autobots.space.space_auth import is_space_member_a_admin
from src.autobots.space.space_crud import SpaceMemberCRUD

_RUN_ID = re.compile(r"^[0-9a-f]{24}$", re.IGNORECASE)

def _is_denied(topic: str) -> bool:
    """Channels no client may read, whatever they are.

    Read at call time rather than import time: settings are not guaranteed to be
    loaded when this module is first imported.
    """
    # Publisher-only, and it carries every active run id across every tenant.
    return topic == SettingsProvider.sget().REDIS_HEARTBEAT_TOPIC


def run_id_from_topic(topic: str) -> Optional[str]:
    """The run id a topic belongs to, or None if it is not a run topic at all.

    The suffixes come from the module the publishers use, so a new one cannot be
    introduced on the publishing side without this seeing it.
    """
    base = base_topic(topic)
    return base if base and _RUN_ID.match(base) else None


async def _is_member_of_any(
    db: AsyncIOMotorDatabase, space_ids: List[str], user_id: str, admin_only: bool = False
) -> bool:
    crud = SpaceMemberCRUD(db)
    for space_id in space_ids:
        member = await crud.find_membership(space_id=space_id, user_id=user_id)
        if member and (not admin_only or is_space_member_a_admin(member)):
            return True
    return False


async def _may_read(
    db: AsyncIOMotorDatabase, run: EventResultLiteDoc, user_id: str, user_email: Optional[str]
) -> bool:
    if str(run.user_id) == user_id:
        return True

    granted_space_ids: List[str] = []
    for grant in run.shared_with:
        if isinstance(grant, SpaceId):
            granted_space_ids.append(grant.space_id)
        elif user_email and grant == user_email:
            return True

    if granted_space_ids and await _is_member_of_any(db, granted_space_ids, user_id):
        return True

    # Merely living in a space is not a grant. The result endpoints let only space
    # admins read another member's run, so the stream matches that.
    return bool(run.space_id) and await _is_member_of_any(
        db, [run.space_id], user_id, admin_only=True
    )


async def may_read_topic(
    topic: str,
    user_res: supabase_auth.UserResponse,
    db: AsyncIOMotorDatabase,
) -> bool:
    """Whether the caller may read this topic.

    The websocket wants an answer rather than an exception: it has no response to
    write, and closes the connection with a policy-violation code instead.
    """
    if _is_denied(topic):
        return False

    run_id = run_id_from_topic(topic)
    if run_id is None:
        # Not a run, so this file has nothing to say about it. See the module
        # docstring for why that is a pass rather than a refusal.
        return True

    # `find_by_ids` is unscoped on purpose - it reads a run by id and leaves who
    # may see it to the caller, which is this function's whole job.
    runs = await EventResultCRUD(db).find_by_ids([run_id], limit=1)
    if not runs:
        return False

    return await _may_read(db, runs[0], str(user_res.user.id), user_res.user.email)


async def assert_topic_readable(
    topic: str,
    user_res: supabase_auth.UserResponse,
    db: AsyncIOMotorDatabase,
) -> None:
    """Raise 404 unless the caller may read this topic.

    Must run before the first byte is written: the client treats a stream that opens
    and then closes as a dropped connection and reconnects every 2s forever, whereas
    it stops on a non-2xx.

    404 rather than 403, matching the result endpoints - whether a run exists is
    itself not the caller's business.
    """
    if not await may_read_topic(topic, user_res, db):
        raise HTTPException(status_code=404, detail="Unknown topic")
