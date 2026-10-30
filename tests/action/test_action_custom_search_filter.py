"""Tests for CRUDBase.search_text_page filter building (replaces action_custom_search)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from bson import ObjectId
from pydantic import BaseModel

from src.autobots.core.database.mongo_base_crud import CRUDBase, DocFindPage


class FakeDoc(BaseModel):
    id: str | None = None
    user_id: str | None = None
    name: str | None = None
    shared_with: str | None = None
    space_id: str | None = None


class FakeLiteDoc(BaseModel):
    id: str | None = None
    name: str | None = None


@pytest.fixture
def crud():
    collection = MagicMock()
    c = CRUDBase.__new__(CRUDBase)
    c.doc_model = FakeDoc
    c.lite_doc_model = FakeLiteDoc
    c.document = collection
    c.search_index_name = "test"
    c.search_path = ["name"]
    return c


@pytest.mark.asyncio
async def test_search_text_page_empty_query_falls_back_to_find_page(crud):
    """Empty query delegates to find_page."""
    crud.find_page = AsyncMock(
        return_value=DocFindPage(docs=[], total_count=0, limit=10, offset=0)
    )
    result = await crud.search_text_page(
        doc_find=FakeDoc(user_id="u1"), q="", limit=10, offset=0
    )
    assert result.total_count == 0
    crud.find_page.assert_called_once()


@pytest.mark.asyncio
async def test_search_text_page_builds_token_regex_clauses(crud):
    """Tokens become regex $or clauses on searchable paths."""
    crud._find_raw_page = AsyncMock(
        return_value=DocFindPage(docs=[], total_count=0, limit=10, offset=0)
    )
    await crud.search_text_page(
        doc_find=FakeDoc(user_id="u1"),
        q="gemini openai",
        searchable_paths=["name", "type"],
        limit=10,
        offset=0,
    )
    call_args = crud._find_raw_page.call_args
    find_params = call_args.kwargs["find_params"]

    assert "$and" in find_params
    # scope + 2 tokens
    assert len(find_params["$and"]) == 3

    # First clause is the scope filter (includes space_id=None for personal context)
    assert find_params["$and"][0] == {"user_id": "u1", "space_id": None}

    # Token clauses have regex on searchable paths
    token_clause = find_params["$and"][1]
    assert "$or" in token_clause
    assert any("name" in c for c in token_clause["$or"])
    assert any("type" in c for c in token_clause["$or"])


@pytest.mark.asyncio
async def test_search_text_page_with_or_queries_builds_scope(crud):
    """or_find_queries add shared_with to the scope clause."""
    crud._find_raw_page = AsyncMock(
        return_value=DocFindPage(docs=[], total_count=0, limit=10, offset=0)
    )
    await crud.search_text_page(
        doc_find=FakeDoc(user_id="u1"),
        or_find_queries=[FakeDoc(shared_with="me@example.com")],
        q="hello",
        limit=10,
        offset=0,
    )
    call_args = crud._find_raw_page.call_args
    find_params = call_args.kwargs["find_params"]

    scope = find_params["$and"][0]
    # `space_id` rides on every branch rather than wrapping them. An $and around
    # an $or stops each branch using an index that includes space_id, which cost
    # a 19s count on the shared-runs query.
    assert "$or" in scope
    assert {"user_id": "u1", "space_id": None} in scope["$or"]
    assert {"shared_with": "me@example.com", "space_id": None} in scope["$or"]


@pytest.mark.asyncio
async def test_search_text_page_objectid_token_adds_exact_match(crud):
    """24-char hex tokens add exact _id match."""
    crud._find_raw_page = AsyncMock(
        return_value=DocFindPage(docs=[], total_count=0, limit=10, offset=0)
    )
    token = "69fa" + ("0" * 20)
    await crud.search_text_page(
        doc_find=FakeDoc(user_id="u1"), q=token, limit=10, offset=0
    )
    call_args = crud._find_raw_page.call_args
    find_params = call_args.kwargs["find_params"]

    token_clause = find_params["$and"][1]
    assert {"_id": ObjectId(token)} in token_clause["$or"]


@pytest.mark.asyncio
async def test_search_text_page_short_hex_no_id_match(crud):
    """Short hex tokens don't trigger _id match."""
    crud._find_raw_page = AsyncMock(
        return_value=DocFindPage(docs=[], total_count=0, limit=10, offset=0)
    )
    await crud.search_text_page(
        doc_find=FakeDoc(user_id="u1"), q="69fa", limit=10, offset=0
    )
    call_args = crud._find_raw_page.call_args
    find_params = call_args.kwargs["find_params"]

    token_clause = find_params["$and"][1]
    assert all("_id" not in c for c in token_clause["$or"])


@pytest.mark.asyncio
async def test_search_text_page_with_space_share(crud):
    """Space share entry in or_find_queries adds to scope."""
    crud._find_raw_page = AsyncMock(
        return_value=DocFindPage(docs=[], total_count=0, limit=10, offset=0)
    )
    await crud.search_text_page(
        doc_find=FakeDoc(user_id="u1"),
        or_find_queries=[
            FakeDoc(shared_with="me@example.com"),
            FakeDoc(shared_with="space:sp_1"),
        ],
        q="test",
        limit=10,
        offset=0,
    )
    call_args = crud._find_raw_page.call_args
    find_params = call_args.kwargs["find_params"]

    scope = find_params["$and"][0]
    # Every branch carries space_id, including the space-share one — the same
    # constraint the old $and wrapper applied to all of them.
    assert "$or" in scope
    assert {"user_id": "u1", "space_id": None} in scope["$or"]
    assert {"shared_with": "me@example.com", "space_id": None} in scope["$or"]
    assert {"shared_with": "space:sp_1", "space_id": None} in scope["$or"]
