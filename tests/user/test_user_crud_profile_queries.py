"""The query layer: what UserCRUD actually asks Mongo for.

The directory's own tests cover the authorization rules against constructed
documents, which left this layer — the filters, the casing, the collation —
untested. Both bugs found in review so far lived here: the ObjectId coercion in
`EventResultCRUD.find_by_ids`, and an email query that was only half
case-insensitive while a test one layer up made it look covered.

No Mongo. A stub collection records what it was asked and replays canned rows,
which is enough to pin the filter shape and the collation.
"""

from __future__ import annotations

import pytest

from src.autobots.user.user_crud import (
    EMAIL_COLLATION,
    UserCRUD,
    find_params_for_emails,
    find_params_for_ids,
    norm_email,
)


class _Cursor:
    """Filters lazily, at iteration.

    The collation is applied to the cursor *after* `find()` returns, so matching
    eagerly would evaluate before the stub knows whether the query is
    case-insensitive — and the collation assertion below would prove nothing.
    """

    def __init__(self, rows: list[dict], calls: list[dict], params: dict):
        self._rows = rows
        self._record = {"filter": params, "collation": None, "limit": None}
        calls.append(self._record)

    def collation(self, collation):
        self._record["collation"] = collation
        return self

    def limit(self, n):
        self._record["limit"] = n
        return self

    def __aiter__(self):
        fold = self._record["collation"] is not None

        async def gen():
            for row in self._rows:
                if _matches(row, self._record["filter"], fold=fold):
                    yield row

        return gen()


class _StubCollection:
    """Answers a find() from `rows`, honouring the collation for email matching."""

    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.calls: list[dict] = []

    def find(self, find_params, projection=None):
        return _Cursor(self.rows, self.calls, find_params)


def _matches(row: dict, params: dict, *, fold: bool) -> bool:
    """Case-sensitive unless the cursor carried a collation.

    That is the whole point: a stored `Alice@Example.COM` is only reachable by a
    query for `alice@example.com` if the collation is genuinely applied.
    """

    def norm(v):
        return v.lower() if fold and isinstance(v, str) else v

    if "$or" in params:
        return any(_matches(row, branch, fold=fold) for branch in params["$or"])
    for field, cond in params.items():
        values = cond["$in"] if isinstance(cond, dict) and "$in" in cond else [cond]
        if norm(row.get(field)) in {norm(v) for v in values}:
            return True
    return False


def _crud(rows: list[dict]) -> tuple[UserCRUD, _StubCollection]:
    crud = UserCRUD.__new__(UserCRUD)
    stub = _StubCollection(rows)
    crud.document = stub
    return crud, stub


# --- the filters themselves ------------------------------------------------------


def test_email_params_are_normalised_not_as_typed():
    """The bug this replaces asked for the caller's spelling *and* the lowered one.

    That only ever matched a stored address which happened to share the caller's
    casing, so a stored `Alice@Example.COM` was unreachable.
    """
    assert find_params_for_emails(["Alice@Example.COM", "  bob@x.com "]) == {
        "email": {"$in": ["alice@example.com", "bob@x.com"]}
    }


def test_a_filter_is_none_when_there_is_nothing_to_ask():
    """No query is issued at all, rather than an `$in: []` round trip."""
    assert find_params_for_emails([]) is None
    assert find_params_for_emails(["", "   "]) is None
    assert find_params_for_ids([]) is None


def test_id_params_search_both_id_fields():
    assert find_params_for_ids(["u1"]) == {
        "$or": [{"_id": {"$in": ["u1"]}}, {"user_id": {"$in": ["u1"]}}]
    }


def test_norm_email_is_shared_with_the_directory():
    assert norm_email(" Alice@Example.COM ") == "alice@example.com"
    assert norm_email(None) is None


# --- what actually reaches Mongo -------------------------------------------------


@pytest.mark.asyncio
async def test_ids_are_never_matched_case_insensitively():
    """Two ids differing only in case are two different ids.

    So the id query must not carry the collation, even though it runs beside one
    that does.
    """
    crud, stub = _crud([{"user_id": "u1", "email": "a@x.com"}])

    await crud.find_for_profiles(user_ids=["u1"], emails=[])

    assert [c["collation"] for c in stub.calls] == [None]


@pytest.mark.asyncio
async def test_a_lowercase_stored_email_resolves_without_the_collation():
    """The common path: the auth provider lower-cases, so one indexed query does it."""
    crud, stub = _crud([{"user_id": "u1", "email": "alice@example.com"}])

    found = await crud.find_for_profiles(user_ids=[], emails=["Alice@Example.COM"])

    assert [u.user_id for u in found] == ["u1"]
    # One query, no collation: the retry never ran.
    assert [c["collation"] for c in stub.calls] == [None]


@pytest.mark.asyncio
async def test_a_mixed_case_stored_email_still_resolves():
    """The finding this fixes.

    The stub matches exactly, so this can only pass if the miss triggers a
    second query carrying a case-insensitive collation.
    """
    crud, stub = _crud([{"user_id": "u1", "email": "Alice@Example.COM"}])

    found = await crud.find_for_profiles(user_ids=[], emails=["alice@example.com"])

    assert [u.user_id for u in found] == ["u1"]
    assert stub.calls[-1]["collation"] is EMAIL_COLLATION


@pytest.mark.asyncio
async def test_an_unregistered_email_costs_one_retry_and_no_more():
    """A miss is a miss; it must not loop or fan out."""
    crud, stub = _crud([])

    found = await crud.find_for_profiles(user_ids=[], emails=["nobody@example.com"])

    assert found == []
    assert len(stub.calls) == 2  # the normalised query, then the collation retry


@pytest.mark.asyncio
async def test_nothing_requested_issues_no_query_at_all():
    crud, stub = _crud([{"user_id": "u1", "email": "a@x.com"}])

    assert await crud.find_for_profiles(user_ids=[], emails=[]) == []
    assert stub.calls == []


@pytest.mark.asyncio
async def test_a_user_found_by_both_id_and_email_appears_once():
    crud, _ = _crud([{"user_id": "u1", "email": "alice@example.com"}])

    found = await crud.find_for_profiles(
        user_ids=["u1"], emails=["alice@example.com"]
    )

    assert [u.user_id for u in found] == ["u1"]


@pytest.mark.asyncio
async def test_the_cap_is_passed_to_every_query():
    crud, stub = _crud([])

    await crud.find_for_profiles(user_ids=["u1"], emails=["a@x.com"], limit=7)

    assert {c["limit"] for c in stub.calls} == {7}
