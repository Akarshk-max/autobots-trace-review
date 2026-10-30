from types import SimpleNamespace

import pytest
import pytest_asyncio
from pydantic import ValidationError

from src.autobots.action.action_type.action_text2text.action_text2text_daytona import (
    DaytonaSandboxConfig,
    _cleanup_mode,
    _derive_lifecycle_policy,
    _job_cleanup_mode,
)

pytestmark = [pytest.mark.asyncio]


@pytest_asyncio.fixture(scope="session", autouse=True)
async def start():
    """
    Override the repository session fixture. These tests require neither the
    database nor live Daytona access.
    """
    yield


def _settings():
    return SimpleNamespace(
        DAYTONA_EPHEMERAL_AUTO_STOP_GRACE_MINUTES=1,
        DAYTONA_EPHEMERAL_TTL_GRACE_MINUTES=5,
        DAYTONA_MAX_EPHEMERAL_TTL_MINUTES=1440,
        DAYTONA_RETAINED_AUTO_ARCHIVE_INTERVAL_MINUTES=60,
        DAYTONA_PERSISTENT_AUTO_ARCHIVE_INTERVAL_MINUTES=60,
    )


async def test_minimal_run_defaults_to_ephemeral():
    cfg = DaytonaSandboxConfig(
        operation="run",
        code="print('ok')",
    )

    assert cfg.lifecycle == "ephemeral"
    assert cfg.max_runtime_minutes == 10
    assert cfg.wait_for_completion_sec == 60


async def test_default_ephemeral_policy():
    cfg = DaytonaSandboxConfig(
        operation="run",
        code="print('ok')",
    )

    policy = _derive_lifecycle_policy(cfg, _settings())

    assert policy.ephemeral is True
    assert policy.auto_stop_interval == 11
    assert policy.auto_archive_interval is None
    assert policy.auto_delete_interval == 0
    assert policy.ttl_minutes == 15


async def test_seconds_long_fire_and_forget_policy():
    cfg = DaytonaSandboxConfig(
        operation="run",
        code="print('ok')",
        max_runtime_minutes=1,
        wait_for_completion_sec=0,
    )

    policy = _derive_lifecycle_policy(cfg, _settings())

    assert policy.ephemeral is True
    assert policy.auto_stop_interval == 2
    assert policy.ttl_minutes == 6


async def test_ephemeral_ttl_is_capped():
    settings = _settings()
    settings.DAYTONA_MAX_EPHEMERAL_TTL_MINUTES = 20

    cfg = DaytonaSandboxConfig(
        operation="run",
        code="print('ok')",
        max_runtime_minutes=100,
    )

    policy = _derive_lifecycle_policy(cfg, settings)

    assert policy.ttl_minutes == 20
    assert policy.auto_stop_interval == 20


async def test_retained_requires_retention_and_idle_timeout():
    with pytest.raises(ValidationError):
        DaytonaSandboxConfig(
            operation="run",
            code="print('ok')",
            lifecycle="retained",
        )


async def test_retained_policy():
    cfg = DaytonaSandboxConfig(
        operation="run",
        code="print('ok')",
        lifecycle="retained",
        retention_minutes=10080,
        idle_timeout_minutes=30,
    )

    policy = _derive_lifecycle_policy(cfg, _settings())

    assert policy.ephemeral is False
    assert policy.auto_stop_interval == 30
    assert policy.auto_archive_interval == 60
    assert policy.auto_delete_interval == 10080
    assert policy.ttl_minutes is None


async def test_persistent_requires_confirmation():
    with pytest.raises(ValidationError):
        DaytonaSandboxConfig(
            operation="run",
            code="print('ok')",
            lifecycle="persistent",
            idle_timeout_minutes=30,
        )


async def test_persistent_policy():
    cfg = DaytonaSandboxConfig(
        operation="run",
        code="print('ok')",
        lifecycle="persistent",
        idle_timeout_minutes=0,
        confirm_persistent=True,
    )

    policy = _derive_lifecycle_policy(cfg, _settings())

    assert policy.ephemeral is False
    assert policy.auto_stop_interval == 0
    assert policy.auto_archive_interval == 60
    assert policy.auto_delete_interval == -1
    assert policy.ttl_minutes is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("retention_minutes", 60),
        ("idle_timeout_minutes", 30),
        ("confirm_persistent", True),
    ],
)
async def test_reuse_rejects_lifecycle_mutation_fields(field, value):
    kwargs = {
        "operation": "run",
        "code": "print('ok')",
        "reuse_sandbox_id": "sandbox-id",
        field: value,
    }

    with pytest.raises(ValidationError):
        DaytonaSandboxConfig(**kwargs)


async def test_stop_requires_reuse_sandbox_id():
    with pytest.raises(ValidationError):
        DaytonaSandboxConfig(operation="stop")


async def test_stop_accepts_reuse_sandbox_id():
    cfg = DaytonaSandboxConfig(
        operation="stop",
        reuse_sandbox_id="sandbox-id",
    )
    assert cfg.operation == "stop"
    assert cfg.reuse_sandbox_id == "sandbox-id"


async def test_legacy_keep_sandbox_tool_argument_is_rejected():
    with pytest.raises(ValidationError):
        DaytonaSandboxConfig(
            operation="run",
            code="print('ok')",
            keep_sandbox=True,
        )


async def test_legacy_vnc_disable_autostop_argument_is_rejected():
    with pytest.raises(ValidationError):
        DaytonaSandboxConfig(
            operation="run",
            code="print('ok')",
            vnc_disable_autostop=True,
        )


async def test_cleanup_modes():
    assert _cleanup_mode(
        created_sandbox=True,
        lifecycle="ephemeral",
    ) == "delete"

    assert _cleanup_mode(
        created_sandbox=True,
        lifecycle="retained",
    ) == "stop"

    assert _cleanup_mode(
        created_sandbox=True,
        lifecycle="persistent",
    ) == "stop"

    assert _cleanup_mode(
        created_sandbox=False,
        lifecycle="existing",
    ) == "none"


async def test_reused_job_is_never_automatically_cleaned_up():
    job = SimpleNamespace(
        reused_sandbox=True,
        created_sandbox=False,
        lifecycle="existing",
        keep_sandbox=False,
    )
    assert _job_cleanup_mode(job) == "none"


async def test_legacy_keep_sandbox_job_remains_preserved():
    job = SimpleNamespace(
        reused_sandbox=False,
        created_sandbox=True,
        lifecycle="ephemeral",
        keep_sandbox=True,
    )
    assert _job_cleanup_mode(job) == "none"
