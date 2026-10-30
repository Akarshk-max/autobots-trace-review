"""Tests for CRUDBase.search_text_page filter building (replaces action_graph_custom_search).

These tests validate the same behavior as the action tests but specifically
for action graph search patterns (default searchable_paths=["name"]).
"""

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
async def test_graph_search_empty_query(crud):
    crud.find_page = AsyncMock(
        return_value=DocFindPage(docs=[], total_count=0, limit=10, offset=0)
    )
    result = await crud.search_text_page(
        doc_find=FakeDoc(user_id="u1"), q="", limit=10, offset=0
    )
    assert result.total_count == 0


@pytest.mark.asyncio
async def test_graph_search_tokens_and_field_matching(crud):
    crud._find_raw_page = AsyncMock(
        return_value=DocFindPage(docs=[], total_count=0, limit=10, offset=0)
    )
    await crud.search_text_page(
        doc_find=FakeDoc(user_id="u1"),
        q="my workflow",
        searchable_paths=["name"],
        limit=10,
        offset=0,
    )
    find_params = crud._find_raw_page.call_args.kwargs["find_params"]

    assert "$and" in find_params
    assert len(find_params["$and"]) == 3  # scope + 2 tokens

    token_clause = find_params["$and"][1]
    assert "$or" in token_clause
    assert any("name" in c for c in token_clause["$or"])


@pytest.mark.asyncio
async def test_graph_search_with_shared_email(crud):
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
    find_params = crud._find_raw_page.call_args.kwargs["find_params"]
    scope = find_params["$and"][0]
    # scope is wrapped: {"$and": [{"space_id": None}, {"$or": [...]}]}
    # `space_id` rides on every branch rather than wrapping them, so each can be
    # served by an index end to end. See mongo_base_crud._build_filter.
    assert "$or" in scope
    assert {"user_id": "u1", "space_id": None} in scope["$or"]
    assert {"shared_with": "me@example.com", "space_id": None} in scope["$or"]


@pytest.mark.asyncio
async def test_graph_search_objectid_token(crud):
    crud._find_raw_page = AsyncMock(
        return_value=DocFindPage(docs=[], total_count=0, limit=10, offset=0)
    )
    token = "69fa" + ("0" * 20)
    await crud.search_text_page(
        doc_find=FakeDoc(user_id="u1"), q=token, limit=10, offset=0
    )
    find_params = crud._find_raw_page.call_args.kwargs["find_params"]
    token_clause = find_params["$and"][1]
    assert {"_id": ObjectId(token)} in token_clause["$or"]


@pytest.mark.asyncio
async def test_graph_search_short_hex_no_id(crud):
    crud._find_raw_page = AsyncMock(
        return_value=DocFindPage(docs=[], total_count=0, limit=10, offset=0)
    )
    await crud.search_text_page(
        doc_find=FakeDoc(user_id="u1"), q="69fa", limit=10, offset=0
    )
    find_params = crud._find_raw_page.call_args.kwargs["find_params"]
    token_clause = find_params["$and"][1]
    assert all("_id" not in c for c in token_clause["$or"])
