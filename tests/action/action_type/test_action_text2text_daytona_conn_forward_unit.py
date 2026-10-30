from types import SimpleNamespace

import pytest
import pytest_asyncio

from src.autobots.action.action_type.action_text2text.action_text2text_daytona import (
    DaytonaSandboxConfig,
    _job_has_forwarded_connections,
    _normalize_pid,
)


@pytest_asyncio.fixture(scope="session", autouse=True)
async def start():
    """
    Override the repository session fixture. These tests require neither the
    database nor live Daytona access.
    """
    yield


# --- _normalize_pid ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("12345", "12345"),
        ("  12345  ", "12345"),
        ("12345\n", "12345"),
        # Transport layers can prepend noise; the last clean digit line wins.
        ("warning: something\n12345", "12345"),
        ("12345\ntrailing noise\n", "12345"),
        # The double-echo bug this normalization defends against: two PIDs printed.
        ("12345\n12345", "12345"),
    ],
)
def test_normalize_pid_extracts_numeric_pid(raw: str, expected: str):
    assert _normalize_pid(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "\n\n",
        "not-a-pid",
        "12345 67890",  # not a clean single token
        "pid=12345",
        "-1",
    ],
)
def test_normalize_pid_rejects_non_numeric(raw):
    assert _normalize_pid(raw) is None


def test_normalize_pid_result_is_int_parseable():
    # The cancel handler calls int() on the stored value, so whatever survives
    # normalization must always parse.
    assert int(_normalize_pid("noise\n4242")) == 4242


# --- _job_has_forwarded_connections -----------------------------------------


def test_job_without_forwarding_is_false():
    job = SimpleNamespace(sandbox_owner_user_id=None, forward_databases=[])
    assert _job_has_forwarded_connections(job) is False


def test_job_with_sandbox_owner_is_true():
    job = SimpleNamespace(sandbox_owner_user_id="user-1", forward_databases=[])
    assert _job_has_forwarded_connections(job) is True


def test_job_with_forward_databases_is_true():
    job = SimpleNamespace(sandbox_owner_user_id=None, forward_databases=["warehouse"])
    assert _job_has_forwarded_connections(job) is True


def test_legacy_job_doc_without_the_fields_is_false():
    # Job documents persisted before AUT-1593 carry neither attribute.
    assert _job_has_forwarded_connections(SimpleNamespace()) is False


# --- config fields ----------------------------------------------------------


def test_forwarding_fields_default_to_empty():
    cfg = DaytonaSandboxConfig(operation="run", code="print('ok')")

    assert cfg.forward_connections == []
    assert cfg.forward_databases == []
    assert cfg.forward_databases_direct == []


def test_forward_connections_takes_connection_names():
    # This repo scopes app connections by name (several per app type), so the
    # field is a list of saved connection names, not of app types.
    cfg = DaytonaSandboxConfig(
        operation="run",
        code="print('ok')",
        forward_connections=["workday-prod", "aws-analytics"],
        forward_databases=["warehouse"],
        forward_databases_direct=["reporting"],
    )

    assert cfg.forward_connections == ["workday-prod", "aws-analytics"]
    assert cfg.forward_databases == ["warehouse"]
    assert cfg.forward_databases_direct == ["reporting"]
