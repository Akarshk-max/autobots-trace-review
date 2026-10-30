"""Scoped profile resolution: who resolves, who does not, and what shows instead.

The authorization tests are the point. Before this, `POST /v1/users/profiles`
had no scoping clause of any kind - any authenticated caller could post any id
and get back that person's email address. These pin the rule that replaced it.
"""

from __future__ import annotations

from src.autobots.data_model.space_id import SpaceId
from src.autobots.event_result.event_result_model import (
    EventResultLiteDoc,
    EventResultStatus,
    EventType,
)
from src.autobots.user.user_directory import (
    Resolvable,
    build_response,
    can_read_run,
    display_name_for,
    norm_email,
    placeholder_for,
    profile_for,
    resolvable_from_runs,
    self_only,
)
from src.autobots.user.user_orm_model import UserDoc

OWNER = "owner-1"
GRANTEE = "grantee-1"
STRANGER = "stranger-1"


def _run(
    run_id: str = "r1",
    *,
    user_id: str = OWNER,
    shared_with: list | None = None,
) -> EventResultLiteDoc:
    return EventResultLiteDoc.model_construct(
        id=run_id,
        user_id=user_id,
        type=EventType.action_run,
        status=EventResultStatus.success,
        shared_with=shared_with or [],
    )


def _user(user_id: str, email: str, metadata: dict | None = None) -> UserDoc:
    return UserDoc.model_construct(
        user_id=user_id, email=email, user_metadata=metadata
    )


# --- the display name chain ------------------------------------------------------


def test_display_name_prefers_a_real_name():
    assert (
        display_name_for(
            user_metadata={"full_name": "Ada Lovelace"},
            email="ada@example.com",
            user_id="u1",
        )
        == "Ada Lovelace"
    )


def test_display_name_falls_back_to_the_email_local_part():
    assert (
        display_name_for(user_metadata={}, email="ada@example.com", user_id="u1")
        == "ada"
    )


def test_display_name_falls_back_to_the_id_and_is_never_blank():
    """An unknown subject renders as something stable rather than an empty chip."""
    assert display_name_for(
        user_metadata=None, email=None, user_id="6f2a1b9c"
    ) == "User 6f2a1b"
    assert display_name_for(user_metadata={"full_name": "   "}, email="", user_id="u1")
    assert display_name_for(user_metadata=None, email=None, user_id=None)


# --- who a run scope entitles you to ---------------------------------------------


def test_a_run_you_own_resolves_its_participants():
    resolvable = resolvable_from_runs(
        [_run(shared_with=["grantee@example.com"])],
        caller_id=OWNER,
        caller_email="owner@example.com",
        caller_space_ids=set(),
    )
    assert OWNER in resolvable.user_ids
    assert "grantee@example.com" in resolvable.emails


def test_a_run_shared_to_your_email_resolves_its_owner():
    """The case a co-space-membership rule would have broken.

    Personal sharing is by email with no space involved, so a space-based rule
    leaves names unresolvable in exactly the runs this design exists to create.
    """
    resolvable = resolvable_from_runs(
        [_run(shared_with=["grantee@example.com"])],
        caller_id=GRANTEE,
        caller_email="grantee@example.com",
        caller_space_ids=set(),
    )
    assert OWNER in resolvable.user_ids


def test_a_run_shared_to_your_space_resolves_its_owner():
    resolvable = resolvable_from_runs(
        [_run(shared_with=[SpaceId(space_id="sp_1")])],
        caller_id=GRANTEE,
        caller_email="grantee@example.com",
        caller_space_ids={"sp_1"},
    )
    assert OWNER in resolvable.user_ids


def test_a_run_you_cannot_read_entitles_you_to_nothing():
    """Asking with somebody else's run id must not widen what you can resolve."""
    resolvable = resolvable_from_runs(
        [_run(shared_with=["someone-else@example.com"])],
        caller_id=STRANGER,
        caller_email="stranger@example.com",
        caller_space_ids=set(),
    )
    assert resolvable.user_ids == set()
    assert resolvable.emails == set()


def test_readability_matches_the_three_ways_a_run_is_shared():
    owned = _run(user_id=OWNER)
    by_email = _run(shared_with=["grantee@example.com"])
    by_space = _run(shared_with=[SpaceId(space_id="sp_1")])

    assert can_read_run(
        owned, caller_id=OWNER, caller_email=None, caller_space_ids=set()
    )
    assert can_read_run(
        by_email,
        caller_id=GRANTEE,
        caller_email="grantee@example.com",
        caller_space_ids=set(),
    )
    assert can_read_run(
        by_space, caller_id=GRANTEE, caller_email=None, caller_space_ids={"sp_1"}
    )
    assert not can_read_run(
        by_space, caller_id=STRANGER, caller_email=None, caller_space_ids={"sp_other"}
    )


def test_a_run_scope_never_reads_the_turns():
    """Owner-plus-grants is already a superset of who could have posted.

    Only the owner or a grantee can add a turn, so the participant set answers
    the authorization question without fetching the run's payload out of S3.
    """
    resolvable = resolvable_from_runs(
        [_run(shared_with=["a@example.com", SpaceId(space_id="sp_1")])],
        caller_id=OWNER,
        caller_email="owner@example.com",
        caller_space_ids=set(),
    )
    # Everything it needed came off the Mongo document.
    assert resolvable.user_ids == {OWNER}
    assert resolvable.emails == {"a@example.com"}


# --- space scope and the floor ---------------------------------------------------


def test_emails_compare_case_insensitively_end_to_end():
    """A grant holds what the sharer typed; a user record holds what the provider stored.

    Comparing one case-sensitively against the other silently fails to resolve a
    real participant, and it looks like a missing name rather than a bug. The
    lookup was already case-insensitive, so the authorization has to be too.
    """
    run = _run(shared_with=["alice@example.com"])
    resolvable = resolvable_from_runs(
        [run], caller_id=OWNER, caller_email="owner@example.com", caller_space_ids=set()
    )
    alice = _user("alice-1", "Alice@Example.COM", {"full_name": "Alice"})

    resolved = build_response(
        requested_user_ids=["alice-1"],
        requested_emails=[],
        users=[alice],
        resolvable=resolvable,
    )
    assert resolved["alice-1"].display_name == "Alice"

    # And the caller side: a mixed-case JWT email reads a lower-case grant.
    assert can_read_run(
        run,
        caller_id="somebody-else",
        caller_email="ALICE@example.com",
        caller_space_ids=set(),
    )


def test_norm_email_is_the_one_place_casing_is_decided():
    assert norm_email("  Alice@Example.COM ") == "alice@example.com"
    assert norm_email("") is None
    assert norm_email(None) is None


def test_a_scopeless_request_resolves_you_and_nobody_else():
    """The floor. Keeps an existing caller working without opening the directory."""
    floor = self_only(caller_id=OWNER, caller_email="owner@example.com")
    assert floor.user_ids == {OWNER}
    assert not floor.allows(user_id=STRANGER, email="stranger@example.com")


# --- what comes back -------------------------------------------------------------


def test_an_id_outside_the_scope_gets_a_placeholder_not_an_email():
    """The enumeration hole, closed.

    The stranger's user document is fetched - deciding whether an id is in scope
    needs that user's email - but nothing about them is returned.
    """
    users = [_user(STRANGER, "stranger@example.com", {"full_name": "A Stranger"})]
    resolvable = Resolvable(user_ids={OWNER})

    resolved = build_response(
        requested_user_ids=[STRANGER],
        requested_emails=[],
        users=users,
        resolvable=resolvable,
    )

    profile = resolved[STRANGER]
    assert profile.user_id is None
    assert profile.display_name == "User strang"
    assert "stranger@example.com" not in profile.model_dump_json()
    assert "A Stranger" not in profile.model_dump_json()


def test_an_in_scope_id_resolves_to_a_name_and_still_no_email():
    users = [_user(OWNER, "owner@example.com", {"full_name": "Ada Lovelace"})]
    resolved = build_response(
        requested_user_ids=[OWNER],
        requested_emails=[],
        users=users,
        resolvable=Resolvable(user_ids={OWNER}),
    )
    assert resolved[OWNER].display_name == "Ada Lovelace"
    assert resolved[OWNER].user_id == OWNER
    assert "email" not in resolved[OWNER].model_dump()


def test_an_email_in_a_grant_list_resolves_to_the_person():
    """Hydrating a run's shared_with, which holds emails rather than ids."""
    users = [_user(GRANTEE, "grantee@example.com", {"name": "Grace"})]
    resolved = build_response(
        requested_user_ids=[],
        requested_emails=["Grantee@Example.com"],
        users=users,
        resolvable=Resolvable(emails={"grantee@example.com"}),
    )
    assert resolved["Grantee@Example.com"].display_name == "Grace"


def test_an_out_of_scope_email_looks_the_same_as_one_with_no_account():
    """So the response cannot be used to test whether an address is registered."""
    registered = build_response(
        requested_user_ids=[],
        requested_emails=["someone@example.com"],
        users=[_user("u9", "someone@example.com", {"full_name": "Someone"})],
        resolvable=Resolvable(),
    )
    unregistered = build_response(
        requested_user_ids=[],
        requested_emails=["someone@example.com"],
        users=[],
        resolvable=Resolvable(),
    )
    assert (
        registered["someone@example.com"].model_dump()
        == unregistered["someone@example.com"].model_dump()
    )


def test_every_requested_subject_gets_an_entry():
    """A page never fails to render over a name it could not resolve."""
    resolved = build_response(
        requested_user_ids=["a", "b"],
        requested_emails=["c@example.com"],
        users=[],
        resolvable=Resolvable(),
    )
    assert set(resolved) == {"a", "b", "c@example.com"}
    assert all(p.display_name for p in resolved.values())


def test_a_profile_carries_an_avatar_when_the_metadata_has_one():
    user = _user(OWNER, "owner@example.com", {"avatar_url": "https://img/a.png"})
    assert profile_for(user).avatar_url == "https://img/a.png"
    assert profile_for(_user(OWNER, "owner@example.com", {})).avatar_url is None


def test_a_placeholder_never_carries_a_user_id():
    assert placeholder_for("anything").user_id is None
