from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException

from src.autobots.api.v1.api_run_metrics import api_run_metrics
from src.autobots.run_metrics.run_metrics_models import TimeRange

SAMPLE = {
    "scope": "personal", "time_range": "last_7_days", "timezone": "Asia/Kolkata",
    "start": "2026-09-01T00:00:00", "end": "2026-09-08T00:00:00",
    "totals": {}, "previous": {}, "buckets": [],
    "top_subjects": [], "by_trigger": [], "by_model": [], "top_errors": [],
}


def _request():
    return SimpleNamespace(state=SimpleNamespace(context=None))


def _response(status_code=200, json_body=None, text=""):
    return SimpleNamespace(
        status_code=status_code, json=lambda: json_body or SAMPLE, text=text
    )


def _client_returning(response=None, error=None):
    client = AsyncMock()
    client.get = AsyncMock(return_value=response, side_effect=error)
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    return context, client


async def _call(**overrides):
    kwargs = dict(
        request=_request(), time_range=TimeRange.last_7_days, granularity="daily",
        timezone="Asia/Kolkata", space_id=None,
        user_res=SimpleNamespace(user=SimpleNamespace(id="u-1")), token="tok-123",
    )
    kwargs.update(overrides)
    return await api_run_metrics.get_run_metrics(**kwargs)


@pytest.mark.asyncio
async def test_it_forwards_the_query_and_the_callers_token():
    context, client = _client_returning(_response())
    with patch.object(httpx, "AsyncClient", return_value=context):
        await _call()

    sent = client.get.await_args.kwargs
    assert sent["params"] == {
        "time_range": "last_7_days", "granularity": "daily", "timezone": "Asia/Kolkata"
    }
    assert sent["headers"] == {"authorization": "Bearer tok-123"}
    assert sent["url"].endswith("/v1/run_metrics/")


@pytest.mark.asyncio
async def test_space_id_is_forwarded_only_when_given():
    context, client = _client_returning(_response())
    with patch.object(httpx, "AsyncClient", return_value=context):
        await _call(space_id="sp-42")
    assert client.get.await_args.kwargs["params"]["space_id"] == "sp-42"


@pytest.mark.asyncio
async def test_the_response_comes_back_typed():
    context, _ = _client_returning(_response())
    with patch.object(httpx, "AsyncClient", return_value=context):
        result = await _call()
    assert result.scope == "personal"
    assert result.timezone == "Asia/Kolkata"


@pytest.mark.asyncio
async def test_a_refusal_downstream_keeps_its_status():
    """A 403 from the space check must not reach the caller as a 500."""
    context, _ = _client_returning(_response(status_code=403, text="Not a member of this space"))
    with patch.object(httpx, "AsyncClient", return_value=context):
        with pytest.raises(HTTPException) as raised:
            await _call(space_id="sp-not-mine")
    assert raised.value.status_code == 403


@pytest.mark.asyncio
async def test_an_unreachable_dashboard_reads_as_unavailable_not_broken():
    context, _ = _client_returning(error=httpx.ConnectError("no route to host"))
    with patch.object(httpx, "AsyncClient", return_value=context):
        with pytest.raises(HTTPException) as raised:
            await _call()
    assert raised.value.status_code == 503


def test_the_url_is_built_from_the_configured_dashboard_host():
    url = api_run_metrics.dashboard_service_url()
    assert url.startswith("http://") and url.endswith("/v1/run_metrics/")
