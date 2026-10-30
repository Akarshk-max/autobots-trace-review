"""Who may read a run's stream.

The endpoints took a topic straight from the path and subscribed to it, so any
authenticated user could watch any run, and `/ws/` needed no credential at all.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from bson import ObjectId
from fastapi import HTTPException


def _ensure_fake_supabase_module() -> None:
    if "supabase" in sys.modules:
        return
    fake = types.ModuleType("supabase")

    class _Client:
        pass

    fake.Client = _Client
    fake.create_client = lambda *a, **k: _Client()
    sys.modules["supabase"] = fake


_ensure_fake_supabase_module()

from src.autobots.api.v1.api_server_sent_events import topic_access  # noqa: E402
from src.autobots.api.v1.api_server_sent_events.topic_access import (  # noqa: E402
    assert_topic_readable,
    run_id_from_topic,
)

OWNER = "owner-user-id"
RUN_ID = "6a85094c553e7c8ea3df6329"


def _caller(user_id: str, email: str | None = "me@example.com"):
    return SimpleNamespace(user=SimpleNamespace(id=user_id, email=email))


class _FakeCursor:
    """The chained-call shape `CRUDBase._find_raw` drives a Motor cursor through."""

    def __init__(self, docs):
        self._docs = docs

    def allow_disk_use(self, _enabled):
        return self

    def sort(self, _spec):
        return self

    def skip(self, _n):
        return self

    def limit(self, _n):
        return self

    def __aiter__(self):
        async def gen():
            for doc in self._docs:
                yield doc

        return gen()


class _FakeDb:
    """Just enough of a Motor db for the one read the gate makes through the CRUD."""

    def __init__(self, run: dict | None):
        self._run = run
        self.queried_with: dict | None = None

    def __getitem__(self, _collection):
        outer = self

        class _Collection:
            # CRUDBase names its search indexes after the collection.
            name = "event_result"

            def find(self, find_params, _projection=None):
                outer.queried_with = find_params
                return _FakeCursor([outer._run] if outer._run is not None else [])

        return _Collection()


def _run(**overrides) -> dict:
    """A run as Mongo holds it, with the fields the lite projection validates."""
    now = datetime(2026, 9, 3, tzinfo=timezone.utc)
    doc = {
        "_id": ObjectId(RUN_ID),
        "name": "fixture run",
        "user_id": OWNER,
        "type": "action_run",
        "status": "success",
        "created_at": now,
        "updated_at": now,
        "space_id": None,
        "shared_with": [],
    }
    doc.update(overrides)
    return doc


@pytest.fixture
def no_memberships(monkeypatch):
    async def none(db, space_ids, user_id, admin_only=False):
        return False

    monkeypatch.setattr(topic_access, "_is_member_of_any", none)


@pytest.fixture
def plain_member(monkeypatch):
    """A member of every space, admin of none."""

    async def member(db, space_ids, user_id, admin_only=False):
        return bool(space_ids) and not admin_only

    monkeypatch.setattr(topic_access, "_is_member_of_any", member)


@pytest.fixture
def space_admin(monkeypatch):
    async def admin(db, space_ids, user_id, admin_only=False):
        return bool(space_ids)

    monkeypatch.setattr(topic_access, "_is_member_of_any", admin)


# --- topic grammar -------------------------------------------------------------


def test_the_three_run_topic_shapes_resolve_to_the_same_run():
    assert run_id_from_topic(RUN_ID) == RUN_ID
    assert run_id_from_topic(f"{RUN_ID}-text") == RUN_ID
    assert run_id_from_topic(f"{RUN_ID}-text-complimentary") == RUN_ID


def test_topics_that_are_not_runs_resolve_to_nothing():
    """Not every topic is a run, and this is how the gate tells."""
    for topic in (
        "autobots:heartbeat",
        "terminal:pty_out:abc",
        "terminal:cmd:agent:abc",
        "",
        "not-an-object-id",
        f"{RUN_ID}-text-somethingelse",
        f"{RUN_ID}extra",
    ):
        assert run_id_from_topic(topic) is None, topic


# --- authorisation -------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_owner_may_read(no_memberships):
    await assert_topic_readable(RUN_ID, _caller(OWNER), _FakeDb(_run()))


@pytest.mark.asyncio
async def test_the_owner_may_read_the_derived_topics_too(no_memberships):
    db = _FakeDb(_run())
    await assert_topic_readable(f"{RUN_ID}-text", _caller(OWNER), db)
    await assert_topic_readable(f"{RUN_ID}-text-complimentary", _caller(OWNER), db)


@pytest.mark.asyncio
async def test_a_stranger_may_not_read(no_memberships):
    with pytest.raises(HTTPException) as excinfo:
        await assert_topic_readable(RUN_ID, _caller("someone-else"), _FakeDb(_run()))
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_an_email_grantee_may_read(no_memberships):
    run = _run(shared_with=["me@example.com"])
    await assert_topic_readable(RUN_ID, _caller("someone-else"), _FakeDb(run))


@pytest.mark.asyncio
async def test_a_grant_to_someone_else_does_not_admit_this_caller(no_memberships):
    run = _run(shared_with=["other@example.com"])
    with pytest.raises(HTTPException):
        await assert_topic_readable(RUN_ID, _caller("someone-else"), _FakeDb(run))


@pytest.mark.asyncio
async def test_a_space_grant_admits_a_member_of_that_space(plain_member):
    run = _run(shared_with=[{"space_id": "sp_1"}])
    await assert_topic_readable(RUN_ID, _caller("someone-else"), _FakeDb(run))


@pytest.mark.asyncio
async def test_a_space_grant_does_not_admit_a_non_member(no_memberships):
    run = _run(shared_with=[{"space_id": "sp_1"}])
    with pytest.raises(HTTPException):
        await assert_topic_readable(RUN_ID, _caller("someone-else"), _FakeDb(run))


@pytest.mark.asyncio
async def test_living_in_a_space_is_not_itself_a_grant(plain_member):
    """The result endpoints let only space admins read another member's run."""
    run = _run(space_id="sp_1")
    with pytest.raises(HTTPException):
        await assert_topic_readable(RUN_ID, _caller("someone-else"), _FakeDb(run))


@pytest.mark.asyncio
async def test_a_space_admin_may_read_a_run_created_in_that_space(space_admin):
    run = _run(space_id="sp_1")
    await assert_topic_readable(RUN_ID, _caller("someone-else"), _FakeDb(run))


@pytest.mark.asyncio
async def test_a_caller_with_no_email_is_not_matched_by_a_grant(no_memberships):
    """An absent email must not compare equal to anything in shared_with."""
    run = _run(shared_with=["someone@example.com"])
    with pytest.raises(HTTPException):
        await assert_topic_readable(RUN_ID, _caller("someone-else", email=None), _FakeDb(run))


# --- lookup shape --------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unknown_run_is_rejected(no_memberships):
    with pytest.raises(HTTPException) as excinfo:
        await assert_topic_readable(RUN_ID, _caller(OWNER), _FakeDb(None))
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_a_topic_that_is_not_a_run_is_allowed_through(no_memberships):
    """These endpoints bridge pubsub generally, not runs only.

    The terminal has its own channels and more will follow. Refusing everything
    this file has not been taught about would break each of them on the day it
    ships, silently, for a reason nobody would connect back to here.
    """
    db = _FakeDb(_run())
    for topic in ("terminal:pty_out:abc", "terminal:cmd:agent:abc", "some-future-channel"):
        await assert_topic_readable(topic, _caller("someone-else"), db)
    # And it costs nothing: a topic that is not a run is never looked up.
    assert db.queried_with is None


@pytest.mark.asyncio
async def test_the_heartbeat_channel_stays_closed(no_memberships):
    """Allowing the unfamiliar is not the same as allowing the known-bad.

    `autobots:heartbeat` publishes every active run id across every tenant and
    has no client at all, so nothing breaks by keeping it shut.
    """
    from src.autobots import SettingsProvider

    db = _FakeDb(_run())
    with pytest.raises(HTTPException):
        await assert_topic_readable(
            SettingsProvider.sget().REDIS_HEARTBEAT_TOPIC, _caller(OWNER), db
        )
    assert db.queried_with is None


@pytest.mark.asyncio
async def test_the_run_is_looked_up_by_object_id(no_memberships):
    """A string _id would silently match nothing.

    The coercion lives in `EventResultCRUD.find_by_ids`; asserted from here
    because this is the caller that would break silently without it.
    """
    db = _FakeDb(_run())
    await assert_topic_readable(f"{RUN_ID}-text", _caller(OWNER), db)
    assert db.queried_with == {"_id": {"$in": [ObjectId(RUN_ID)]}}


# --- endpoint level ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_sse_endpoint_rejects_before_opening_the_stream(no_memberships):
    """The client reconnects every 2s forever if a stream opens and then closes.

    It stops on a non-2xx, so the rejection has to happen before the response head -
    i.e. `stream_sse` must raise rather than return a StreamingResponse.
    """
    from src.autobots.api.v1.api_server_sent_events import api_server_sent_events as sse

    request = SimpleNamespace(state=SimpleNamespace(context=None))
    with pytest.raises(HTTPException) as excinfo:
        await sse.stream_sse(
            request, RUN_ID, user_res=_caller("someone-else"), db=_FakeDb(_run())
        )
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_the_ndjson_endpoint_rejects_before_opening_the_stream(no_memberships):
    from src.autobots.api.v1.api_server_sent_events import api_server_sent_events as sse

    request = SimpleNamespace(state=SimpleNamespace(context=None))
    with pytest.raises(HTTPException):
        await sse.streaming_ndjson(
            request, RUN_ID, user_res=_caller("someone-else"), db=_FakeDb(_run())
        )


class _FakeWebSocket:
    """Records what the handler did to the connection, and nothing else."""

    def __init__(self, **query_params):
        self.query_params = query_params
        self.accepted = False
        self.closed_with: int | None = None

    async def accept(self):
        self.accepted = True

    async def close(self, code: int | None = None):
        self.closed_with = code


def test_http_stream_routes_require_authentication():
    from src.autobots.api.v1.api_server_sent_events import api_server_sent_events as sse

    read_routes = [
        r
        for r in sse.router.routes
        if any(seg in r.path for seg in ("/stream/", "/streaming/"))
    ]
    assert len(read_routes) == 2
    for route in read_routes:
        names = {d.call.__name__ for d in route.dependant.dependencies if d.call}
        assert "get_offline_user_from_access_token" in names, route.path


@pytest.mark.asyncio
async def test_websocket_rejects_a_client_with_no_token():
    """`/ws/{topic}` took no credential at all, which would have left the
    ownership check below unreachable there.

    Asserted through the handler rather than on the route: this one
    authenticates in its body, because a browser cannot set headers on a
    WebSocket and the token arrives as a query param instead.
    """
    from src.autobots.api.v1.api_server_sent_events import api_server_sent_events as sse

    ws = _FakeWebSocket()
    await sse.websocket_stream(ws, f"{RUN_ID}-text", db=_FakeDb(_run()))

    assert ws.closed_with == 1008
    assert not ws.accepted


@pytest.mark.asyncio
async def test_websocket_rejects_a_topic_the_caller_may_not_read(monkeypatch):
    """Authenticated is not the same as entitled, which was the whole bug."""
    from src.autobots.api.v1.api_server_sent_events import api_server_sent_events as sse

    async def authenticated(_websocket):
        return _caller("somebody-else")

    monkeypatch.setattr(sse, "authenticate_websocket", authenticated)

    ws = _FakeWebSocket(token="valid")
    await sse.websocket_stream(ws, f"{RUN_ID}-text", db=_FakeDb(_run()))

    assert ws.closed_with == 1008
    assert not ws.accepted


def test_gate_accepts_every_topic_a_publisher_can_produce():
    """The gate's vocabulary and the publishers' must not drift apart.

    Both now read `server_sent_events.topics`, so adding a suffix there extends
    this automatically — and a suffix the gate cannot parse fails here rather
    than by silently refusing every subscription to it.
    """
    from src.autobots.server_sent_events import topics

    assert run_id_from_topic(RUN_ID) == RUN_ID
    for suffix in topics.SUFFIXES:
        assert run_id_from_topic(f"{RUN_ID}{suffix}") == RUN_ID, suffix

    assert topics.text_topic(RUN_ID) == f"{RUN_ID}-text"
    assert topics.text_complimentary_topic(RUN_ID) == f"{RUN_ID}-text-complimentary"


def test_no_provider_spells_a_topic_suffix_itself():
    """Eight providers built `f"{notify.topic}-text"` by hand.

    An inline suffix is invisible to the gate, so the stream it publishes on is
    one nobody is allowed to subscribe to — a failure with no error anywhere.
    """
    from pathlib import Path

    from src.autobots.server_sent_events import topics

    conn = Path(__file__).resolve().parents[3] / "src" / "autobots" / "conn"
    offenders = [
        path.relative_to(conn).as_posix()
        for path in conn.rglob("*.py")
        for line in path.read_text().splitlines()
        if any(f'{suffix}"' in line for suffix in topics.SUFFIXES)
    ]

    assert not offenders, f"topic suffix written inline in: {sorted(set(offenders))}"
