"""One pod per user, against a real Mongo.

The unit tests pin what is sent to Mongo; these pin what Mongo does with it -
that repeated reads converge on a single record rather than accumulating one per call.
"""

import pytest

from src.autobots.event_result.event_result_model import EventType
from src.autobots.pod.pod_crud import PodCRUD
from src.autobots.pod.pod_doc_model import PodDoc, PodUpdate
from src.autobots.pod.user_pod import UserPod
from src.autobots.user.user_orm_model import UserORM


async def _raw_count(db, user_id: str) -> int:
    return await db[PodDoc.__collection__].count_documents({"user_id": user_id})


@pytest.mark.asyncio
async def test_reading_twice_creates_exactly_one_pod(ctx, db, set_test_settings):
    user = UserORM(id="test_pod_user_a", email="pod_a@runkai.ai")
    pod = UserPod(user, db)
    try:
        first = await pod.get(ctx)
        second = await pod.get(ctx)

        assert first.id == second.id, "a second read must not create a second pod"
        assert await _raw_count(db, str(user.id)) == 1
        assert first.current_runnable_id is None
        assert first.current_run_id is None
    finally:
        await db[PodDoc.__collection__].delete_many({"user_id": str(user.id)})


@pytest.mark.asyncio
async def test_an_update_persists_and_a_partial_update_does_not_blank_the_rest(
    ctx, db, set_test_settings
):
    user = UserORM(id="test_pod_user_b", email="pod_b@runkai.ai")
    pod = UserPod(user, db)
    try:
        await pod.update(ctx, PodUpdate(current_runnable_id="agent-1", current_run_id="run-1"))

        # The case the feature exists for: a new run id, same runnable.
        updated = await pod.update(ctx, PodUpdate(current_run_id="run-2"))
        assert updated.current_run_id == "run-2"
        assert updated.current_runnable_id == "agent-1", "a partial update must not blank the runnable"

        reread = await pod.get(ctx)
        assert reread.current_runnable_id == "agent-1"
        assert reread.current_run_id == "run-2"
        assert await _raw_count(db, str(user.id)) == 1
    finally:
        await db[PodDoc.__collection__].delete_many({"user_id": str(user.id)})


@pytest.mark.asyncio
async def test_pods_are_per_user(ctx, db, set_test_settings):
    a = UserORM(id="test_pod_user_c", email="pod_c@runkai.ai")
    b = UserORM(id="test_pod_user_d", email="pod_d@runkai.ai")
    try:
        await UserPod(a, db).update(ctx, PodUpdate(current_run_id="run-a"))
        b_pod = await UserPod(b, db).get(ctx)

        assert b_pod.current_run_id is None, "one user's pod must not be another's"
        assert b_pod.user_id == str(b.id)
    finally:
        for u in (a, b):
            await db[PodDoc.__collection__].delete_many({"user_id": str(u.id)})


@pytest.mark.asyncio
async def test_the_runnable_type_round_trips_as_a_string(ctx, db, set_test_settings):
    """model_dump gives an enum member, and this write path skips Pydantic
    serialization - so what Mongo stores has to be checked, not assumed."""
    user = UserORM(id="test_pod_user_g", email="pod_g@runkai.ai")
    try:
        await UserPod(user, db).update(
            ctx,
            PodUpdate(
                current_runnable_type=EventType.action_graph_run,
                current_runnable_id="graph-1",
                current_run_id="run-1",
            ),
        )
        raw = await db[PodDoc.__collection__].find_one({"user_id": str(user.id)})
        assert raw["current_runnable_type"] == "action_graph_run"
        assert isinstance(raw["current_runnable_type"], str)

        reread = await UserPod(user, db).get(ctx)
        assert reread.current_runnable_type is EventType.action_graph_run
        assert reread.current_runnable_id == "graph-1"
        assert reread.current_run_id == "run-1"
    finally:
        await db[PodDoc.__collection__].delete_many({"user_id": str(user.id)})


@pytest.mark.asyncio
async def test_the_timestamps_reach_mongo(ctx, db, set_test_settings):
    """The mixin fills these in when a model is built, and upsert never builds one -
    so without an explicit write the stored doc has no timestamps and every read
    invents a new `created_at`."""
    user = UserORM(id="test_pod_user_f", email="pod_f@runkai.ai")
    try:
        await UserPod(user, db).get(ctx)
        raw = await db[PodDoc.__collection__].find_one({"user_id": str(user.id)})

        assert raw["created_at"] is not None
        assert raw["updated_at"] is not None
    finally:
        await db[PodDoc.__collection__].delete_many({"user_id": str(user.id)})


@pytest.mark.asyncio
async def test_the_crud_layer_can_read_the_pod_the_upsert_wrote(ctx, db, set_test_settings):
    """The upsert bypasses CRUDBase, so the document it writes must still validate
    through the normal read path."""
    from src.autobots.pod.pod_doc_model import PodDocFind

    user = UserORM(id="test_pod_user_e", email="pod_e@runkai.ai")
    try:
        written = await UserPod(user, db).update(ctx, PodUpdate(current_runnable_id="agent-5"))
        found = await PodCRUD(db).find_one(PodDocFind(user_id=str(user.id)))

        assert found is not None
        assert found.id == written.id
        assert found.current_runnable_id == "agent-5"
    finally:
        await db[PodDoc.__collection__].delete_many({"user_id": str(user.id)})
