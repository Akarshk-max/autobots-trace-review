"""A shared run's payload is read from the owner's S3 prefix, not the caller's.

Every run type keeps a lite document in Mongo and writes the full one to S3, so
returning the Mongo record to a grantee hands them a run with its conversation
missing. That is what it did, and it rendered as an empty run rather than as an
error, on a run that plainly had content.

The subtle half is *whose* prefix. `UserFiles` builds its key prefix from the id
of the user it was constructed with, so the reader on the service points at the
caller. A shared payload lives under the owner. These tests assert the identity
the reader is built for, because that is the part a refactor can quietly undo
while every other assertion still passes.

Action runs are covered by the split-storage suite; this covers graph and team,
which have the same shape and the same fix.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

import pytest

import src.autobots.action_graph.action_graph_result.user_action_graph_result as graph_mod
import src.autobots.action_team.action_team_result.user_action_team_result as team_mod
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM

OWNER = "owner-1"
GRANTEE = "grantee-2"
RESULT_ID = "run-abc"
AT = datetime(2026, 1, 2, 3, 4, 5)


def _doc(doc_cls, *, event_type: str, name: str) -> Dict[str, Any]:
    """A valid result document. Built as a dict because the payload has to
    survive `model_validate_json` on the way back out — `model_construct` skips
    required fields, and the read would then fail validation rather than test
    anything."""
    return doc_cls.model_validate(
        {
            "_id": RESULT_ID,
            "user_id": OWNER,
            "type": event_type,
            "status": "success",
            "name": name,
            "created_at": AT,
            "updated_at": AT,
            "result": {
                "_id": "runnable-1",
                "name": "a runnable",
                "user_id": OWNER,
                "created_at": AT,
                "updated_at": AT,
            },
        }
    )


class _RecordingUserFiles:
    """Records which user each reader was built for, and serves one payload."""

    built_for: List[str] = []
    payload_for_owner: Optional[bytes] = None

    def __init__(self, user: UserORM, s3: Any = None, top_level_folder: str = ""):
        self._user_id = str(user.id)
        _RecordingUserFiles.built_for.append(self._user_id)

    async def get_bytes(self, ctx: Context, filename: str) -> Optional[bytes]:
        # Only the owner's prefix holds it. A reader built for anybody else
        # finds nothing, which is exactly the bug when the caller is used.
        if self._user_id == OWNER:
            return _RecordingUserFiles.payload_for_owner
        return None


def _install(monkeypatch: pytest.MonkeyPatch, module) -> None:
    monkeypatch.setattr(module, "UserFiles", _RecordingUserFiles)
    monkeypatch.setattr(module, "get_s3", lambda: None)
    monkeypatch.setattr(module, "EventResultCRUD", lambda db: None)
    _RecordingUserFiles.built_for = []


@pytest.mark.asyncio
async def test_graph_shared_read_uses_the_owners_prefix(monkeypatch: pytest.MonkeyPatch):
    _install(monkeypatch, graph_mod)
    doc_cls = graph_mod.ActionGraphResultDoc
    # Asserted on `name`, a plain field: `result` is a typed sub-document and a
    # stand-in for it would not survive the round trip.
    full = _doc(doc_cls, event_type="action_graph_run", name="from-s3")
    _RecordingUserFiles.payload_for_owner = full.model_dump_json().encode()

    service = graph_mod.UserActionGraphResult(
        user=UserORM(id=GRANTEE, email="g@example.com"), db=None
    )
    lite = _doc(doc_cls, event_type="action_graph_run", name="from-mongo")

    out = await service._read_shared_payload(
        lite, result_id=RESULT_ID, ctx=Context(user_id=GRANTEE)
    )

    assert out.name == "from-s3", "should be the S3 copy, not the lite one"
    assert OWNER in _RecordingUserFiles.built_for, "never read the owner's prefix"


@pytest.mark.asyncio
async def test_team_shared_read_uses_the_owners_prefix(monkeypatch: pytest.MonkeyPatch):
    _install(monkeypatch, team_mod)
    doc_cls = team_mod.ActionTeamResultDoc
    full = _doc(doc_cls, event_type="action_team_run", name="from-s3")
    _RecordingUserFiles.payload_for_owner = full.model_dump_json().encode()

    service = team_mod.UserActionTeamResult(
        user=UserORM(id=GRANTEE, email="g@example.com"), db=None
    )
    lite = _doc(doc_cls, event_type="action_team_run", name="from-mongo")

    out = await service._read_shared_payload(
        lite, result_id=RESULT_ID, ctx=Context(user_id=GRANTEE)
    )

    assert out.name == "from-s3"
    assert OWNER in _RecordingUserFiles.built_for


@pytest.mark.asyncio
async def test_the_lite_doc_is_kept_when_s3_has_nothing(monkeypatch: pytest.MonkeyPatch):
    """Matches what the owned path does: fall back rather than fail the read."""
    _install(monkeypatch, team_mod)
    _RecordingUserFiles.payload_for_owner = None
    doc_cls = team_mod.ActionTeamResultDoc

    service = team_mod.UserActionTeamResult(
        user=UserORM(id=GRANTEE, email="g@example.com"), db=None
    )
    lite = _doc(doc_cls, event_type="action_team_run", name="from-mongo")

    out = await service._read_shared_payload(
        lite, result_id=RESULT_ID, ctx=Context(user_id=GRANTEE)
    )

    assert out.name == "from-mongo"
