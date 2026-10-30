import sys
import types

import pytest


@pytest.mark.asyncio
async def test_action_graph_search_accepts_q_alias(monkeypatch):
    if "supabase" not in sys.modules:
        fake = types.ModuleType("supabase")
        fake.Client = object
        fake.create_client = lambda *args, **kwargs: object()  # noqa: E731
        sys.modules["supabase"] = fake

    from src.autobots.action_graph.action_graph_result.action_graph_result_model_doc import (
        ActionGraphResultDocsFound,
    )
    from src.autobots.api.v1.api_action_graph_result import (
        api_action_graph_results as api_mod,
    )

    class FakeUserActionGraphResult:
        def __init__(self, user, db):  # noqa: ANN001
            self.user = user
            self.db = db

        async def search(self, query: str, limit: int = 10, offset: int = 0):
            assert query == "hello"
            return ActionGraphResultDocsFound(
                docs=[], total_count=0, limit=limit, offset=offset
            )

    monkeypatch.setattr(api_mod, "UserActionGraphResult", FakeUserActionGraphResult)
    monkeypatch.setattr(api_mod, "UserORM", lambda **kwargs: types.SimpleNamespace(**kwargs))

    user_res = types.SimpleNamespace(
        user=types.SimpleNamespace(model_dump=lambda: {"id": "u1"})
    )

    fake_request = types.SimpleNamespace(state=types.SimpleNamespace(context=None))
    res = await api_mod.search_action_graph_results(
        request=fake_request, q="hello", user_res=user_res, db=None,
        space_member=None,
    )
    assert res.total_count == 0