"""The directory against a real MongoDB.

Everything else about this feature is tested against constructed documents, which
leaves the layer where both bugs so far actually lived: `_id` being an ObjectId
in one collection and a string in another, and whether a collation genuinely
makes an email query case-insensitive. Neither is decidable without a server.

Isolation: a throwaway database named per run, emptied in teardown. The CRUD
classes take a database and derive their own collection names, so pointing them
at a temp database keeps every write inside it — this never reads or writes the
configured one. Skips when no server is reachable, so it is safe in a sandbox.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
import pytest_asyncio
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

from src.autobots.core.settings import SettingsProvider
from src.autobots.event_result.event_result_crud import EventResultCRUD
from src.autobots.space.space_crud import SpaceMemberCRUD
from src.autobots.user.user_crud import UserCRUD
from src.autobots.user.user_directory import ProfileScope, resolve_profiles

OWNER_ID = "owner-user-1"
GRANTEE_ID = "grantee-user-1"
STRANGER_ID = "stranger-user-1"

OWNER_EMAIL = "owner@example.com"
GRANTEE_EMAIL = "grantee@example.com"
# Deliberately not lower-case: the row the normalised query cannot reach.
MIXED_CASE_EMAIL = "Mixed.Case@Example.COM"
MIXED_CASE_ID = "mixed-case-user-1"


@pytest_asyncio.fixture(scope="function")
async def mongo_db():
    """A database of its own, emptied afterwards.

    Named per run so parallel workers cannot collide, per the repo's xdist rule.
    """
    conn = SettingsProvider.sget().MONGO_CONN
    client = AsyncIOMotorClient(
        f"{conn}?retryWrites=true&w=majority", serverSelectionTimeoutMS=5000
    )
    try:
        await client.admin.command("ping")
    except Exception as e:  # noqa: BLE001 - any connection failure means skip
        client.close()
        pytest.skip(f"No MongoDB reachable: {e}")

    name = f"autobots_test_directory_{uuid.uuid4().hex[:12]}"
    assert name != SettingsProvider.sget().MONGO_DATABASE, "must not be the real database"
    db = client[name]
    try:
        yield db
    finally:
        # Collections, not the database: the Atlas role can create databases but
        # is not authorised for `dropDatabase`, and a database with no
        # collections stops existing anyway. Dropping the database instead
        # leaves an orphan behind on every run.
        for collection in await db.list_collection_names():
            await db.drop_collection(collection)
        client.close()


@pytest_asyncio.fixture(scope="function")
async def seeded(mongo_db):
    """Two users, a space, and three runs covering each way a run is readable."""
    now = datetime.now()

    await mongo_db["Users"].insert_many(
        [
            {
                "_id": OWNER_ID,
                "user_id": OWNER_ID,
                "email": OWNER_EMAIL,
                "user_metadata": {"full_name": "Olive Owner"},
            },
            {
                "_id": GRANTEE_ID,
                "user_id": GRANTEE_ID,
                "email": GRANTEE_EMAIL,
                "user_metadata": {},
            },
            {
                "_id": STRANGER_ID,
                "user_id": STRANGER_ID,
                "email": "stranger@example.com",
                "user_metadata": {"full_name": "Sam Stranger"},
            },
            {
                "_id": MIXED_CASE_ID,
                "user_id": MIXED_CASE_ID,
                "email": MIXED_CASE_EMAIL,
                "user_metadata": {"full_name": "Mixed Case"},
            },
        ]
    )

    await mongo_db["SpaceMembers"].insert_many(
        [
            {"space_id": "sp_1", "user_id": OWNER_ID, "role": "owner"},
            {"space_id": "sp_1", "user_id": GRANTEE_ID, "role": "member"},
            {"space_id": "sp_other", "user_id": STRANGER_ID, "role": "owner"},
        ]
    )

    # ObjectId ids, as EventResults really stores them. A string-id query
    # against these matches nothing, which is the bug this file exists for.
    run_email = ObjectId()
    run_space = ObjectId()
    run_private = ObjectId()
    base = {
        "name": "",
        "user_id": OWNER_ID,
        "type": "action_run",
        "status": "success",
        "created_at": now,
        "updated_at": now,
        "result": {},
    }
    await mongo_db["EventResults"].insert_many(
        [
            {**base, "_id": run_email, "shared_with": [GRANTEE_EMAIL]},
            {**base, "_id": run_space, "shared_with": [{"space_id": "sp_1"}]},
            {**base, "_id": run_private, "shared_with": []},
        ]
    )

    return {
        "db": mongo_db,
        "run_email": str(run_email),
        "run_space": str(run_space),
        "run_private": str(run_private),
    }


# --- the CRUD methods, against the real thing ------------------------------------


@pytest.mark.asyncio
async def test_find_by_ids_matches_objectid_ids_from_strings(seeded):
    """The coercion. Without it this returns [] and every scope authorizes nobody.

    That failure is silent — no error, just names that never resolve — so it is
    the one thing here that most needs a server to catch.
    """
    runs = await EventResultCRUD(seeded["db"]).find_by_ids([seeded["run_email"]])

    assert [r.id for r in runs] == [seeded["run_email"]]
    assert runs[0].user_id == OWNER_ID
    assert runs[0].shared_with == [GRANTEE_EMAIL]


@pytest.mark.asyncio
async def test_find_by_ids_is_empty_for_an_unknown_id(seeded):
    assert await EventResultCRUD(seeded["db"]).find_by_ids([str(ObjectId())]) == []


@pytest.mark.asyncio
async def test_space_membership_queries(seeded):
    crud = SpaceMemberCRUD(seeded["db"])

    assert await crud.distinct_space_ids_for_user(GRANTEE_ID) == ["sp_1"]
    assert sorted(await crud.distinct_user_ids_for_spaces(["sp_1"])) == sorted(
        [OWNER_ID, GRANTEE_ID]
    )
    assert await crud.distinct_user_ids_for_spaces([]) == []


@pytest.mark.asyncio
async def test_a_lowercase_email_resolves_on_the_indexed_path(seeded):
    found = await UserCRUD(seeded["db"]).find_for_profiles(
        user_ids=[], emails=["Owner@Example.COM"]
    )
    assert [u.user_id for u in found] == [OWNER_ID]


@pytest.mark.asyncio
async def test_a_mixed_case_stored_email_resolves_via_the_collation(seeded):
    """Proves the server honours `strength=2`, which no stub can establish.

    The stored address is `Mixed.Case@Example.COM`; the normalised query asks for
    the lower-cased form and misses, so this only passes if the retry's collation
    is real.
    """
    found = await UserCRUD(seeded["db"]).find_for_profiles(
        user_ids=[], emails=["mixed.case@example.com"]
    )
    assert [u.user_id for u in found] == [MIXED_CASE_ID]


@pytest.mark.asyncio
async def test_ids_are_matched_case_sensitively(seeded):
    """The collation must not leak onto the id query."""
    found = await UserCRUD(seeded["db"]).find_for_profiles(
        user_ids=[OWNER_ID.upper()], emails=[]
    )
    assert found == []


# --- the whole feature, end to end -----------------------------------------------


@pytest.mark.asyncio
async def test_a_grantee_resolves_the_owner_of_a_run_shared_to_their_email(seeded):
    """The case a co-space-membership rule would have broken, now for real."""
    profiles = await resolve_profiles(
        seeded["db"],
        caller_id=GRANTEE_ID,
        caller_email=GRANTEE_EMAIL,
        user_ids=[OWNER_ID],
        emails=[],
        scope=ProfileScope(type="runs", ids=[seeded["run_email"]]),
    )

    assert profiles[OWNER_ID].user_id == OWNER_ID
    assert profiles[OWNER_ID].display_name == "Olive Owner"


@pytest.mark.asyncio
async def test_a_space_grant_resolves_the_owner(seeded):
    profiles = await resolve_profiles(
        seeded["db"],
        caller_id=GRANTEE_ID,
        caller_email=GRANTEE_EMAIL,
        user_ids=[OWNER_ID],
        emails=[],
        scope=ProfileScope(type="runs", ids=[seeded["run_space"]]),
    )
    assert profiles[OWNER_ID].display_name == "Olive Owner"


@pytest.mark.asyncio
async def test_a_run_the_caller_cannot_read_entitles_them_to_nothing(seeded):
    """The enumeration hole. Naming somebody else's run must not widen the scope."""
    profiles = await resolve_profiles(
        seeded["db"],
        caller_id=STRANGER_ID,
        caller_email="stranger@example.com",
        user_ids=[OWNER_ID],
        emails=[],
        scope=ProfileScope(type="runs", ids=[seeded["run_private"]]),
    )

    assert profiles[OWNER_ID].user_id is None
    assert "Olive Owner" not in profiles[OWNER_ID].model_dump_json()
    assert OWNER_EMAIL not in profiles[OWNER_ID].model_dump_json()


@pytest.mark.asyncio
async def test_a_scopeless_request_resolves_only_the_caller(seeded):
    profiles = await resolve_profiles(
        seeded["db"],
        caller_id=GRANTEE_ID,
        caller_email=GRANTEE_EMAIL,
        user_ids=[GRANTEE_ID, OWNER_ID],
        emails=[],
        scope=None,
    )

    assert profiles[GRANTEE_ID].user_id == GRANTEE_ID
    assert profiles[OWNER_ID].user_id is None


@pytest.mark.asyncio
async def test_a_space_scope_resolves_members_and_nobody_else(seeded):
    profiles = await resolve_profiles(
        seeded["db"],
        caller_id=GRANTEE_ID,
        caller_email=GRANTEE_EMAIL,
        user_ids=[OWNER_ID, STRANGER_ID],
        emails=[],
        scope=ProfileScope(type="spaces", ids=["sp_1", "sp_other"]),
    )

    # sp_1 the caller is in; sp_other they are not, so its member stays hidden.
    assert profiles[OWNER_ID].user_id == OWNER_ID
    assert profiles[STRANGER_ID].user_id is None


@pytest.mark.asyncio
async def test_a_grant_list_email_hydrates_to_a_person(seeded):
    """Hydrating `shared_with`, which holds addresses rather than ids."""
    profiles = await resolve_profiles(
        seeded["db"],
        caller_id=OWNER_ID,
        caller_email=OWNER_EMAIL,
        user_ids=[],
        emails=[GRANTEE_EMAIL],
        scope=ProfileScope(type="runs", ids=[seeded["run_email"]]),
    )

    # No display name set, so it falls back to the address's local part.
    assert profiles[GRANTEE_EMAIL].user_id == GRANTEE_ID
    assert profiles[GRANTEE_EMAIL].display_name == "grantee"


@pytest.mark.asyncio
async def test_no_profile_ever_carries_an_email(seeded):
    """The disclosure this endpoint stopped making."""
    profiles = await resolve_profiles(
        seeded["db"],
        caller_id=OWNER_ID,
        caller_email=OWNER_EMAIL,
        user_ids=[OWNER_ID, GRANTEE_ID],
        emails=[],
        scope=ProfileScope(type="runs", ids=[seeded["run_email"]]),
    )

    for profile in profiles.values():
        assert "email" not in profile.model_dump()
