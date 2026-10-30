"""AUT-1459: openai_vector_store_tool falls back to the system OpenAI key for
product subscribers who have no personal OpenAI credentials. Scoped to the tool.
Pure-logic tests; settings and the subscription check are mocked.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.autobots.llm.tools.openai_vector_store_tool as vs_mod
from src.autobots.conn.openai.openai_client import OpenAI
from src.autobots.llm.tools.openai_vector_store_tool import (
    _get_system_openai_for_subscriber,
)


def _ctx():
    return SimpleNamespace()


def _user():
    return SimpleNamespace(id="u", email="external@gmail.com")


def _settings(*, key: str = "sk-system"):
    return SimpleNamespace(SUBSCRIBER_OPENAI_API_KEY=key, SUBSCRIBER_OPENAI_ORG_ID="org-test")


@pytest.mark.asyncio
async def test_no_system_key_returns_none(monkeypatch):
    monkeypatch.setattr(vs_mod.SettingsProvider, "sget", staticmethod(lambda: _settings(key="")))
    conn = await _get_system_openai_for_subscriber(ctx=_ctx(), db=None, user=_user())
    assert conn is None


@pytest.mark.asyncio
async def test_non_subscriber_returns_none(monkeypatch):
    from src.autobots.integrations.app_conn_rule.user_app_conn_rule import UserAppConnRule

    monkeypatch.setattr(vs_mod.SettingsProvider, "sget", staticmethod(lambda: _settings()))
    monkeypatch.setattr(UserAppConnRule, "_has_product_access", AsyncMock(return_value=False))

    conn = await _get_system_openai_for_subscriber(ctx=_ctx(), db=None, user=_user())
    assert conn is None


@pytest.mark.asyncio
async def test_subscriber_gets_system_openai_conn(monkeypatch):
    from src.autobots.integrations.app_conn_rule.user_app_conn_rule import UserAppConnRule

    monkeypatch.setattr(vs_mod.SettingsProvider, "sget", staticmethod(lambda: _settings()))
    monkeypatch.setattr(UserAppConnRule, "_has_product_access", AsyncMock(return_value=True))

    conn = await _get_system_openai_for_subscriber(ctx=_ctx(), db=None, user=_user())
    assert isinstance(conn, OpenAI)
    assert conn.provider == "openai"
