"""Who a message may be addressed to, and what happens to the rest.

A run's grant list holds addresses, not ids, so a check written against
`shared_with` alone would pass the owner and nobody else.
"""

from __future__ import annotations

from typing import Any

import pytest

import src.autobots.user.user_directory as directory
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.user.user_directory import Profile, keep_mentionable
from src.autobots.user.user_orm_model import UserORM

OWNER = "owner-1"
GRANTEE = "grantee-2"
STRANGER = "stranger-3"
RUN = "run-1"

owner_user = UserORM(id=OWNER, email="owner@example.com")
grantee_user = UserORM(id=GRANTEE, email="grantee@example.com")


def _profiles(**resolved: bool) -> dict:
    """`user_id` is set only when the subject resolved; that is the whole signal."""
    return {
        uid: Profile(display_name=f"name-{uid}", user_id=uid if ok else None)
        for uid, ok in resolved.items()
    }


@pytest.mark.asyncio
async def test_only_people_the_sender_can_see_here_survive(monkeypatch: pytest.MonkeyPatch):
    seen: dict = {}

    async def _resolve(db: Any, **kwargs):
        seen.update(kwargs)
        return _profiles(**{OWNER: True, GRANTEE: True, STRANGER: False})

    monkeypatch.setattr(directory, "resolve_profiles", _resolve)

    kept = await keep_mentionable(
        None,
        ctx=Context(),
        app_state=AppState(),
        sender=grantee_user,
        run_id=RUN,
        user_ids=[OWNER, GRANTEE, STRANGER],
    )

    assert kept == {OWNER, GRANTEE}
    assert seen["caller_id"] == GRANTEE
    assert seen["scope"].ids == [RUN]


@pytest.mark.asyncio
async def test_a_run_that_does_not_exist_yet_has_only_you(monkeypatch: pytest.MonkeyPatch):
    async def _never(*a, **k):
        raise AssertionError("no run to ask about")

    monkeypatch.setattr(directory, "resolve_profiles", _never)

    kept = await keep_mentionable(
        None,
        ctx=Context(),
        app_state=AppState(),
        sender=owner_user,
        run_id=None,
        user_ids=[OWNER, GRANTEE],
    )

    assert kept == {OWNER}


@pytest.mark.asyncio
async def test_nothing_asked_when_there_is_nothing_to_check(monkeypatch: pytest.MonkeyPatch):
    async def _never(*a, **k):
        raise AssertionError("resolved with no mentions")

    monkeypatch.setattr(directory, "resolve_profiles", _never)

    assert await keep_mentionable(
        None,
        ctx=Context(),
        app_state=AppState(),
        sender=owner_user,
        run_id=RUN,
        user_ids=[],
    ) == set()

def test_a_block_says_who_it_is_for_and_nothing_more():
    """The whole shape: ids, and an empty list when it is for nobody."""
    block = DataBlock(text="hi @someone can you look", mentions=[GRANTEE])

    assert block.mentions == [GRANTEE]
    assert DataBlock(text="no mentions").mentions == []


@pytest.mark.asyncio
async def test_accept_blocks_attributes_and_filters_in_one_pass(
    monkeypatch: pytest.MonkeyPatch,
):
    """The whole of what the server decides about an inbound block."""
    import src.autobots.action.action.inbound_blocks as inbound

    async def _allow_owner_only(db: Any, **kwargs) -> set:
        return {OWNER}

    monkeypatch.setattr(inbound, "keep_mentionable", _allow_owner_only)

    out = await inbound.accept_blocks(
        None,
        [DataBlock(
            text="ask @a and @b",
            user_id="forged-by-the-client",
            mentions=[OWNER, STRANGER],
        )],
        ctx=Context(),
        app_state=AppState(),
        sender=grantee_user,
        run_id=RUN,
    )

    assert out[0].user_id == GRANTEE, "attribution must not be taken on trust"
    assert out[0].mentions == [OWNER]


@pytest.mark.asyncio
async def test_a_rewritten_list_keeps_the_authors_it_already_had(
    monkeypatch: pytest.MonkeyPatch,
):
    """Reordering must not re-attribute somebody else's queued message."""
    import src.autobots.action.action.inbound_blocks as inbound

    async def _none(db: Any, **kwargs) -> set:
        return set()

    monkeypatch.setattr(inbound, "keep_mentionable", _none)

    out = await inbound.accept_blocks(
        None,
        [DataBlock(text="theirs", user_id=OWNER), DataBlock(text="new")],
        ctx=Context(),
        app_state=AppState(),
        sender=grantee_user,
        run_id=RUN,
        keep_existing_author=True,
    )

    assert [b.user_id for b in out] == [OWNER, GRANTEE]


@pytest.mark.asyncio
async def test_no_mentions_means_no_directory_call(monkeypatch: pytest.MonkeyPatch):
    import src.autobots.action.action.inbound_blocks as inbound

    async def _never(*a, **k):
        raise AssertionError("looked up with nothing to look up")

    monkeypatch.setattr(inbound, "keep_mentionable", _never)

    out = await inbound.accept_blocks(
        None,
        [DataBlock(text="plain")],
        ctx=Context(),
        app_state=AppState(),
        sender=owner_user,
        run_id=RUN,
    )
    assert out[0].user_id == OWNER
