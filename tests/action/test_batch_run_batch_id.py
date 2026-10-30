"""Every result one batch_run call creates must share a batch_id (issue #130)."""

from __future__ import annotations

import types

import pytest

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.data_model.context import Context


def _action_doc() -> ActionDoc:
    return ActionDoc(
        _id="action-1",
        name="Summarise",
        type=ActionType.text2text_llm_chat_openai,
        user_id="u1",
        created_at="2026-01-01T00:00:00Z",
    )


class _FakeUserActionResult:
    def __init__(self):
        self.creates = []
        self.counter = 0

    async def create_action_result(self, action_result_create):
        self.creates.append(action_result_create)
        self.counter += 1
        return types.SimpleNamespace(id=f"result-{self.counter}")

    async def get_action_result(self, action_result_id):
        return types.SimpleNamespace(id=action_result_id, name="Existing run")

    async def init(self, action_result_id=None, **kwargs):
        # Rerun path: the real init updates the existing run, it never creates one.
        return await self.get_action_result(action_result_id)


@pytest.fixture
def patched_action_runner(monkeypatch):
    from src.autobots.action.action import user_actions as ua_mod

    async def fake_gen_title(*args, **kwargs):
        return "Generated title"

    async def fake_run_in_background(*args, **kwargs):
        return types.SimpleNamespace(id=kwargs.get("action_result_id"), status="processing")

    # The batch document is written before the runs; these tests are about the id
    # reaching the runs, so stub the write. A fresh id per call keeps the
    # "two batches must not merge" assertion meaningful.
    counter = {"n": 0}

    async def fake_create_batch(**kwargs):
        counter["n"] += 1
        return types.SimpleNamespace(id=f"batch-{counter['n']}")

    monkeypatch.setattr(ua_mod.LlmFunc, "gen_title", staticmethod(fake_gen_title))
    monkeypatch.setattr(
        ua_mod.ActionFactory, "run_action_in_background", staticmethod(fake_run_in_background)
    )
    monkeypatch.setattr(ua_mod.BatchService, "create_batch_for_run", fake_create_batch)
    return ua_mod


async def _run_batch(ua_mod, fake_result_service, action_input, **kwargs):
    user_actions = ua_mod.UserActions.__new__(ua_mod.UserActions)
    user_actions.space_member = None
    user_actions.db = None
    user_actions.user = types.SimpleNamespace(id="u1")
    return await user_actions.batch_run_action_in_background(
        ctx=Context(user_id="u1"),
        app_state=types.SimpleNamespace(),
        action_doc=_action_doc(),
        action_input=action_input,
        user_action_result=fake_result_service,
        db=None,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_all_batches_share_one_batch_id(patched_action_runner):
    fake = _FakeUserActionResult()
    await _run_batch(
        patched_action_runner,
        fake,
        DataBlock(urls=["http://a.com", "http://b.com", "http://c.com"]),
        batch_size=1,
    )

    assert len(fake.creates) == 3, "expected one result per URL at batch_size=1"
    batch_ids = {c.batch_id for c in fake.creates}
    assert len(batch_ids) == 1, f"batches must share one id, got {batch_ids}"
    assert batch_ids.pop() is not None


@pytest.mark.asyncio
async def test_two_calls_get_different_batch_ids(patched_action_runner):
    """Two batches of one action must not merge."""
    first = _FakeUserActionResult()
    second = _FakeUserActionResult()
    urls = DataBlock(urls=["http://a.com", "http://b.com"])

    await _run_batch(patched_action_runner, first, urls, batch_size=1)
    await _run_batch(patched_action_runner, second, urls, batch_size=1)

    assert first.creates[0].batch_id != second.creates[0].batch_id


@pytest.mark.asyncio
async def test_single_run_with_no_urls_still_gets_a_batch_id(patched_action_runner):
    """A batch of one is still a batch."""
    fake = _FakeUserActionResult()
    await _run_batch(patched_action_runner, fake, DataBlock(text="just some text"))

    assert len(fake.creates) == 1
    assert fake.creates[0].batch_id is not None


@pytest.mark.asyncio
async def test_rerun_creates_no_result_so_batch_id_is_untouched(patched_action_runner):
    """A rerun updates rather than creates, so it keeps the batch_id it had."""
    fake = _FakeUserActionResult()
    await _run_batch(
        patched_action_runner,
        fake,
        DataBlock(text="just some text"),
        action_result_id="existing-1",
    )

    assert fake.creates == []


@pytest.mark.asyncio
async def test_batch_id_is_independent_of_the_n_of_n_name(patched_action_runner):
    """Renaming a run must not orphan it from its batch."""
    fake = _FakeUserActionResult()
    await _run_batch(
        patched_action_runner,
        fake,
        DataBlock(urls=["http://a.com", "http://b.com"]),
        batch_size=1,
    )

    names = [c.name for c in fake.creates]
    assert names == ["1/2 Generated title", "2/2 Generated title"]
    assert fake.creates[0].batch_id == fake.creates[1].batch_id
