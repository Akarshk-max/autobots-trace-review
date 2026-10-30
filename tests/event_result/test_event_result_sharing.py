"""Sharing on run results: field shapes, query construction, and back-compat."""

from __future__ import annotations

import pytest

from src.autobots.core.database.mongo_base_crud import CRUDBase
from src.autobots.data_model.space_id import SpaceId
from src.autobots.event_result.event_result_model import (
    EventResultCreate,
    EventResultDoc,
    EventResultDocFind,
    EventResultFind,
    EventResultLiteDoc,
    EventResultStatus,
    EventResultUpdate,
    EventType,
)
from src.autobots.event_result.event_result_sharing import (
    build_owned_or_shared_finds,
    build_shared_finds_for_id,
)


class _ResultFind(EventResultFind):
    """Stands in for ActionResultFind / ActionGraphResultFind / ActionTeamResultFind."""


def _crud() -> CRUDBase:
    return CRUDBase.__new__(CRUDBase)


def _old_doc() -> dict:
    """A document written before sharing existed - neither field present."""
    return {
        "_id": "r1",
        "user_id": "owner-1",
        "type": "action_run",
        "status": "success",
        "result": {},
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    }


# --- field shapes --------------------------------------------------------------


def test_shared_with_round_trips_as_a_mixed_list():
    """The shape Mongo sees. If this drifts, array matching silently stops working."""
    created = EventResultCreate(
        type=EventType.action_run,
        status=EventResultStatus.success,
        shared_with=["alice@example.com", {"space_id": "sp_1"}],
    )
    dumped = created.model_dump()
    assert dumped["shared_with"] == ["alice@example.com", {"space_id": "sp_1"}]
    assert dumped["is_published"] is False

    revalidated = EventResultCreate.model_validate(dumped)
    assert revalidated.shared_with[0] == "alice@example.com"
    assert isinstance(revalidated.shared_with[1], SpaceId)


def test_lite_doc_carries_both_fields_because_it_is_the_projection():
    """`_find_raw` builds the Mongo projection from these model_fields."""
    assert "shared_with" in EventResultLiteDoc.model_fields
    assert "is_published" in EventResultLiteDoc.model_fields


def test_update_takes_a_list_but_find_takes_a_single_grantee():
    """The asymmetry is deliberate - Find is one grantee per `$or` branch."""
    update = EventResultUpdate(shared_with=["a@x.com", {"space_id": "sp_1"}])
    assert len(update.shared_with) == 2

    find = EventResultDocFind(shared_with="a@x.com")
    assert find.shared_with == "a@x.com"


def test_documents_written_before_sharing_still_validate():
    doc = EventResultDoc.model_validate(_old_doc())
    assert doc.shared_with == []
    assert doc.is_published is False


# --- query construction --------------------------------------------------------


@pytest.mark.asyncio
async def test_an_email_grant_matches_the_array_by_containment():
    built = await _crud()._build_filter(EventResultDocFind(id="r1", shared_with="me@x.com"))
    assert built["shared_with"] == "me@x.com"


@pytest.mark.asyncio
async def test_a_space_grant_flattens_to_a_dotted_path():
    """Flattening is how an array of subdocuments is matched, and it satisfies the
    space check, so no `space_id: None` is injected."""
    built = await _crud()._build_filter(
        EventResultDocFind(id="r1", shared_with=SpaceId(space_id="sp_1"))
    )
    assert built["shared_with.space_id"] == "sp_1"
    assert "space_id" not in built


@pytest.mark.asyncio
async def test_find_page_guard_no_longer_raises_attribute_error():
    """`find_page` reads both as bare attributes, so adding only `shared_with`
    would leave `get` working while `list` raised."""
    for query in (
        EventResultDocFind(user_id="u1"),
        EventResultDocFind(shared_with="me@x.com"),
        EventResultDocFind(shared_with=SpaceId(space_id="sp_1")),
    ):
        assert (
            query.user_id
            or query.is_published
            or query.shared_with
            or ("space_id" in type(query).model_fields and query.space_id)
        )


# --- the branch builders -------------------------------------------------------


def test_without_an_opt_in_the_query_stays_owner_only():
    find = _ResultFind(type=EventType.action_run)
    doc_find, branches = build_owned_or_shared_finds(
        result_find=find, user_id="u1", user_email="me@x.com", space_id=None
    )
    assert doc_find.user_id == "u1"
    assert branches is None


def test_the_owner_branch_drops_the_grant_filter():
    """Otherwise "everything I own" narrows to "mine that I shared"."""
    find = _ResultFind(type=EventType.action_run, shared_with="me@x.com")
    doc_find, branches = build_owned_or_shared_finds(
        result_find=find, user_id="u1", user_email="me@x.com", space_id=None
    )
    assert doc_find.user_id == "u1"
    assert doc_find.shared_with is None
    assert len(branches) == 1


def test_a_shared_branch_nulls_out_user_id():
    """This is how a non-owner gets in."""
    find = _ResultFind(type=EventType.action_run, shared_with="me@x.com")
    _, branches = build_owned_or_shared_finds(
        result_find=find, user_id="u1", user_email="me@x.com", space_id=None
    )
    assert branches[0].user_id is None
    assert branches[0].shared_with == "me@x.com"


def test_every_branch_carries_type_because_results_share_one_collection():
    """Without `type`, a graph-result query returns action results shared to the
    same space - all three run types share one collection."""
    find = _ResultFind(type=EventType.action_graph_run, shared_with="me@x.com")
    doc_find, branches = build_owned_or_shared_finds(
        result_find=find, user_id="u1", user_email="me@x.com", space_id="sp_1"
    )
    assert doc_find.type == EventType.action_graph_run
    assert [b.type for b in branches] == [
        EventType.action_graph_run,
        EventType.action_graph_run,
    ]

    _, id_branches = build_shared_finds_for_id(
        result_id="r1",
        event_type=EventType.action_graph_run,
        user_email="me@x.com",
        space_id="sp_1",
    )
    assert all(b.type == EventType.action_graph_run for b in id_branches)


def test_a_caller_may_only_query_their_own_grants():
    """Otherwise `?is_shared` with someone else's email would read their results."""
    find = _ResultFind(type=EventType.action_run, shared_with="someone-else@x.com")
    with pytest.raises(ValueError):
        build_owned_or_shared_finds(
            result_find=find, user_id="u1", user_email="me@x.com", space_id=None
        )


def test_a_space_branch_is_added_only_inside_a_space():
    find = _ResultFind(type=EventType.action_run, shared_with="me@x.com")

    _, without = build_owned_or_shared_finds(
        result_find=find.model_copy(), user_id="u1", user_email="me@x.com", space_id=None
    )
    assert len(without) == 1

    _, within = build_owned_or_shared_finds(
        result_find=find.model_copy(), user_id="u1", user_email="me@x.com", space_id="sp_1"
    )
    assert len(within) == 2
    assert within[1].shared_with == SpaceId(space_id="sp_1")


def test_the_single_result_lookup_scopes_to_the_id_and_the_caller():
    doc_find, branches = build_shared_finds_for_id(
        result_id="r1",
        event_type=EventType.action_run,
        user_email="me@x.com",
        space_id=None,
    )
    assert doc_find.id == "r1"
    assert doc_find.shared_with == "me@x.com"
    assert doc_find.user_id is None
    assert branches is None
