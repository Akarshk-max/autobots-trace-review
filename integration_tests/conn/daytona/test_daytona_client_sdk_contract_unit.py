import importlib
import sys
import types
from dataclasses import dataclass
from typing import Any, Dict, Optional, cast
from urllib.parse import urlparse

import pytest
import pytest_asyncio

pytestmark = [pytest.mark.asyncio]

@pytest_asyncio.fixture(scope="session", autouse=True)
async def start():
    """
    Override tests/conftest.py session autouse start() so we do NOT:
      - setup_test_db()
      - sleep(15)
    These are pure unit tests with a fake SDK module.
    """
    yield

def _patch_settings(monkeypatch, dc, **overrides):
    """
    Patch SettingsProvider.sget() inside
    src.autobots.conn.daytona.daytona_client so network, resource, and
    Toolbox settings are deterministic in unit tests.

    Lifecycle values are now passed explicitly to create_sandbox() and are
    intentionally not read from SettingsProvider by DaytonaClient.
    """
    class _S:
        DAYTONA_TOOLBOX_BASE_URL = "https://proxy.app.daytona.io/toolbox"
        DAYTONA_NETWORK_BLOCK_ALL = False
        DAYTONA_NETWORK_ALLOW_LIST = ""
        DAYTONA_MAX_CPU = 4
        DAYTONA_MAX_MEMORY_GIB = 8
        DAYTONA_DEFAULT_IMAGE = "daytonaio/sandbox:latest"

    for k, v in overrides.items():
        setattr(_S, k, v)

    monkeypatch.setattr(dc.SettingsProvider, "sget", staticmethod(lambda: _S()))


async def _create_sandbox(
    client,
    *,
    ctx=None,
    language: str = "python",
    env_vars: Optional[Dict[str, str]] = None,
    snapshot: Optional[str] = None,
    cpu: Optional[int] = None,
    memory_gib: Optional[int] = None,
    ephemeral: bool = True,
    ttl_minutes: Optional[int] = 15,
    auto_stop_interval: int = 11,
    auto_archive_interval: Optional[int] = None,
    auto_delete_interval: int = 0,
):
    """
    Create a sandbox with the lifecycle contract required by DaytonaClient.

    Individual tests can override lifecycle fields when verifying retained or
    persistent behavior.
    """
    return await client.create_sandbox(
        ctx=ctx,
        language=language,
        env_vars=env_vars or {},
        snapshot=snapshot,
        cpu=cpu,
        memory_gib=memory_gib,
        ephemeral=ephemeral,
        ttl_minutes=ttl_minutes,
        auto_stop_interval=auto_stop_interval,
        auto_archive_interval=auto_archive_interval,
        auto_delete_interval=auto_delete_interval,
    )


def _as_dict(x: Any) -> dict[str, Any]:
    """
    Type-narrow helper for linters (pylint/pyright) when dealing with Optional/Any.
    """
    assert isinstance(x, dict)
    return x

def _as_list(x: Any) -> list[Any]:
    """
    Type-narrow helper for linters (pylint/pyright) when dealing with Optional/Any.
    """
    assert isinstance(x, list)
    return x

# -----------------------------
# Helpers: fake Daytona SDK
# -----------------------------

@dataclass
class _ExecRes:
    exit_code: int = 0
    result: str = "ok"


class FakeSessionExecuteRequest:
    def __init__(self, command: str, run_async: bool):
        self.command = command
        self.run_async = run_async


class FakeSessionExecResponse:
    def __init__(self, cmd_id: str = "cmd_123"):
        self.cmd_id = cmd_id


class FakeSessionLogs:
    def __init__(self, stdout: str = "stdout", stderr: str = "stderr"):
        self.stdout = stdout
        self.stderr = stderr


@dataclass
class FakeResources:
    cpu: int | None = None
    memory: int | None = None
    disk: int | None = None
    gpu: int | None = None


class FakeProcess:
    def __init__(self):
        self.last_exec: Optional[dict[str, Any]] = None
        self.exec_history: list[dict[str, Any]] = []
        self.vnc_probe_calls: int = 0
        self.created_sessions: list[str] = []
        self.last_session_exec: Optional[dict[str, Any]] = None
        self.last_logs_call: Optional[dict[str, Any]] = None
        self.deleted_sessions: list[str] = []

    # Must accept kwargs per docs: cwd, env, timeout
    def exec(self, command: str, cwd=None, env=None, timeout=None):
        rec = {
            "command": command,
            "cwd": cwd,
            "env": env,
            "timeout": timeout,
        }
        self.last_exec = rec
        self.exec_history.append(rec)

        # DaytonaClient._probe_localhost_port() uses a python3 -c with base64 decode + exec.
        # Simulate: first probe fails (port closed), subsequent probes succeed (port open).
        if "exec(base64.b64decode(" in command:
            self.vnc_probe_calls += 1
            if self.vnc_probe_calls == 1:
                return _ExecRes(exit_code=1, result="NO")
            return _ExecRes(exit_code=0, result="OK")

        return _ExecRes(exit_code=0, result="exec_ok")

    def create_session(self, session_id: str) -> None:
        self.created_sessions.append(session_id)

    def execute_session_command(self, session_id: str, req: Any, timeout: Optional[int] = None):
        self.last_session_exec = {
            "session_id": session_id,
            "req": req,
            "timeout": timeout,
        }
        return FakeSessionExecResponse(cmd_id="cmd_abc")

    def get_session_command_logs(self, session_id: str, command_id: str):
        self.last_logs_call = {"session_id": session_id, "command_id": command_id}
        return FakeSessionLogs(stdout="log_out", stderr="log_err")

    # Async-name variant (some SDK surfaces / future variants)
    def get_session_command_logs_async(self, session_id: str, command_id: str):
        self.last_logs_call = {"session_id": session_id, "command_id": command_id, "mode": "async"}
        return FakeSessionLogs(stdout="log_out_async", stderr="log_err_async")

    def delete_session(self, session_id: str):
        self.deleted_sessions.append(session_id)


class FakeFS:
    def __init__(self, *, typeerror_on_bytes: bool = False):
        self.typeerror_on_bytes = typeerror_on_bytes
        self.calls: list[tuple[Any, str]] = []

    def upload_file(self, arg1: Any, path: str):
        # Two possible signatures we want to support:
        #   upload_file(bytes, remote_path)
        #   upload_file(local_path_str, remote_path)
        if self.typeerror_on_bytes and isinstance(arg1, (bytes, bytearray)):
            raise TypeError("expected local_path not bytes")
        self.calls.append((arg1, path))
        return None


class FakeSignedPreviewUrl:
    def __init__(self, url: str):
        self.url = url


class FakeComputerUse:
    def __init__(self):
        self.start_called = 0
        self.started = False

    def start(self):
        self.start_called += 1
        self.started = True
        return {"ok": True}

    def get_status(self):
        return {"started": self.started}


class FakeSandbox:
    def __init__(self, sandbox_id: str = "sb_1", fs: Optional[FakeFS] = None):
        self.id = sandbox_id
        self.sandbox_id = sandbox_id
        # Provide a realistic default SDK-like string form that previously broke normalization.
        self.state = "SandboxState.STARTED"
        self.start_called = 0
        self.stop_called = 0
        self.autostop_interval_set: Optional[int] = None
        self.refresh_activity_called = 0
        self.cpu = 1
        self.memory = 1
        self.disk = 3
        self.computer_use = FakeComputerUse()
        self.process = FakeProcess()
        self.fs = fs or FakeFS()
        self.last_resize: Optional[FakeResources] = None
        self.refresh_called = 0
        self.wait_called_with: Optional[int] = None

    def refresh_data(self):
        self.refresh_called += 1

    def start(self, timeout: Optional[float] = None):
        # emulate successful start
        self.start_called += 1
        self.state = "SandboxState.STARTED"

    def stop(self, timeout: Optional[float] = None):
        self.stop_called += 1
        self.state = "SandboxState.STOPPED"

    def set_autostop_interval(self, interval: int):
        # Kept only so tests can prove that reuse and VNC do not call it.
        self.autostop_interval_set = int(interval)

    def refresh_activity(self):
        self.refresh_activity_called += 1

    def create_signed_preview_url(self, port: int, expires_in_seconds: int):
        # Return a URL that initially points to "/" (directory listing) so tests can verify
        # the action layer rewrites to /vnc.html (if you assert that at E2E level).
        return FakeSignedPreviewUrl(f"https://preview.test/{self.sandbox_id}:{port}?ttl={expires_in_seconds}")

    def wait_for_resize_complete(self, timeout: int = 60):
        self.wait_called_with = timeout

    def resize(self, resources: FakeResources):
        self.last_resize = resources
        if resources.cpu is not None:
            self.cpu = resources.cpu
        if resources.memory is not None:
            self.memory = resources.memory


class FakeDaytonaConfig:
    def __init__(self, api_key: str, api_url: str):
        self.api_key = api_key
        self.api_url = api_url


class FakeCreateSandboxFromSnapshotParams:
    def __init__(
        self,
        *,
        language: str,
        env_vars: Dict[str, str],
        snapshot: Optional[str] = None,
        ephemeral: Optional[bool] = None,
        ttl_minutes: Optional[int] = None,
        auto_stop_interval: Optional[int] = None,
        auto_archive_interval: Optional[int] = None,
        auto_delete_interval: Optional[int] = None,
    ):
        self.language = language
        self.env_vars = env_vars
        self.snapshot = snapshot
        self.ephemeral = ephemeral
        self.ttl_minutes = ttl_minutes
        self.auto_stop_interval = auto_stop_interval
        self.auto_archive_interval = auto_archive_interval
        self.auto_delete_interval = auto_delete_interval
        self.network_block_all = False
        self.network_allow_list = ""


class FakeCreateSandboxParams(FakeCreateSandboxFromSnapshotParams):
    pass


class FakeCreateSandboxFromImageParams:
    def __init__(
        self,
        *,
        language: str,
        image: str,
        env_vars: Dict[str, str],
        resources: FakeResources,
        ephemeral: Optional[bool] = None,
        ttl_minutes: Optional[int] = None,
        auto_stop_interval: Optional[int] = None,
        auto_archive_interval: Optional[int] = None,
        auto_delete_interval: Optional[int] = None,
    ):
        self.language = language
        self.image = image
        self.env_vars = env_vars
        self.resources = resources
        self.ephemeral = ephemeral
        self.ttl_minutes = ttl_minutes
        self.auto_stop_interval = auto_stop_interval
        self.auto_archive_interval = auto_archive_interval
        self.auto_delete_interval = auto_delete_interval
        self.network_block_all = False
        self.network_allow_list = ""


class FakeDaytona:
    def __init__(self, cfg: FakeDaytonaConfig):
        self.cfg = cfg
        self.created_params: list[Any] = []
        self.deleted: list[Any] = []
        self._sandbox = FakeSandbox("sb_1")

    def create(self, params: Any):
        self.created_params.append(params)

        # Simulate create-from-image resources applying at creation time
        res = getattr(params, "resources", None)
        if res is not None:
            if getattr(res, "cpu", None) is not None:
                self._sandbox.cpu = res.cpu
            if getattr(res, "memory", None) is not None:
                self._sandbox.memory = res.memory

        # Simulate tier snapshots affecting baseline resources
        snap = getattr(params, "snapshot", None)
        if snap == "daytona-medium":
            self._sandbox.cpu = 2
            self._sandbox.memory = 4
            self._sandbox.disk = 8
        elif snap == "daytona-large":
            self._sandbox.cpu = 4
            self._sandbox.memory = 8
            self._sandbox.disk = 10
        return self._sandbox

    def delete(self, sandbox_obj: Any):
        self.deleted.append(sandbox_obj)


def _install_fake_daytona(monkeypatch, *, fs_typeerror_on_bytes: bool = False, include_image_params: bool = True):
    """
    Installs a fake 'daytona' module into sys.modules so that
    src.autobots.conn.daytona.daytona_client imports it.
    """
    mod = types.ModuleType("daytona")
    # Use setattr to avoid pylint E1101(no-member) on dynamic module attributes.
    mod.DaytonaConfig = FakeDaytonaConfig
    mod.Resources = FakeResources
    mod.CreateSandboxFromSnapshotParams = FakeCreateSandboxFromSnapshotParams
    if include_image_params:
        mod.CreateSandboxFromImageParams = FakeCreateSandboxFromImageParams
    mod.CreateSandboxParams = FakeCreateSandboxParams
    mod.SessionExecuteRequest = FakeSessionExecuteRequest
    mod.SessionCommandLogsResponse = FakeSessionLogs  # not used directly; ok

    # Control FS behavior by swapping the sandbox instance
    # We do this by wrapping FakeDaytona.create() sandbox fs.
    class DaytonaWithFS(FakeDaytona):
        def __init__(self, cfg):
            super().__init__(cfg)
            self._sandbox = FakeSandbox("sb_1", fs=FakeFS(typeerror_on_bytes=fs_typeerror_on_bytes))

    mod.Daytona = DaytonaWithFS

    monkeypatch.setitem(sys.modules, "daytona", mod)
    return mod


async def _patch_to_thread(monkeypatch, target_module):
    """
    Make asyncio.to_thread run synchronously for deterministic unit tests.
    """
    async def _to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(target_module.asyncio, "to_thread", _to_thread)


# -----------------------------
# Tests
# -----------------------------

async def test_create_sandbox_passes_explicit_lifecycle_to_snapshot_params(monkeypatch):
    _install_fake_daytona(monkeypatch)
    # reload module so _DAYTONA_MOD and cached classes are picked up
    import src.autobots.conn.daytona.daytona_client as dc
    importlib.reload(dc)
    await _patch_to_thread(monkeypatch, dc)
    _patch_settings(monkeypatch, dc)

    client = dc.DaytonaClient(api_key="k", api_url="https://x")
    info = await _create_sandbox(
        client,
        ctx=None,
        language="python",
        env_vars={"A": "B"},
        ephemeral=True,
        ttl_minutes=35,
        auto_stop_interval=31,
        auto_archive_interval=None,
        auto_delete_interval=0,
    )
    assert info.sandbox_id == "sb_1"

    # Verify the complete lifecycle contract was passed to the constructor.
    created_params_list = _as_list(getattr(cast(Any, client._client), "created_params", None))
    assert created_params_list
    created_params = created_params_list[0]
    assert isinstance(created_params, FakeCreateSandboxFromSnapshotParams)
    assert created_params.language == "python"
    assert created_params.env_vars == {"A": "B"}
    assert created_params.ephemeral is True
    assert created_params.ttl_minutes == 35
    assert created_params.auto_stop_interval == 31
    assert created_params.auto_archive_interval is None
    assert created_params.auto_delete_interval == 0


async def test_create_sandbox_passes_retained_lifecycle_to_snapshot_params(monkeypatch):
    _install_fake_daytona(monkeypatch)
    import src.autobots.conn.daytona.daytona_client as dc
    importlib.reload(dc)
    await _patch_to_thread(monkeypatch, dc)
    _patch_settings(monkeypatch, dc)

    client = dc.DaytonaClient(api_key="k", api_url="https://x")
    info = await _create_sandbox(
        client,
        ephemeral=False,
        ttl_minutes=None,
        auto_stop_interval=30,
        auto_archive_interval=60,
        auto_delete_interval=10080,
    )
    assert info.sandbox_id == "sb_1"

    created_params_list = _as_list(
        getattr(cast(Any, client._client), "created_params", None)
    )
    created_params = created_params_list[0]
    assert isinstance(created_params, FakeCreateSandboxFromSnapshotParams)
    assert created_params.ephemeral is False
    assert created_params.ttl_minutes is None
    assert created_params.auto_stop_interval == 30
    assert created_params.auto_archive_interval == 60
    assert created_params.auto_delete_interval == 10080


async def test_create_sandbox_passes_snapshot_and_skips_resize_when_not_needed(monkeypatch):
    _install_fake_daytona(monkeypatch)
    import src.autobots.conn.daytona.daytona_client as dc
    importlib.reload(dc)
    await _patch_to_thread(monkeypatch, dc)
    _patch_settings(monkeypatch, dc)

    client = dc.DaytonaClient(api_key="k", api_url="https://x")
    info = await _create_sandbox(
        client,
        ctx=None,
        language="python",
        env_vars={},
        snapshot="daytona-medium",
    )
    sb = client._sandbox_cache[info.sandbox_id]
    created_params_list = _as_list(getattr(cast(Any, client._client), "created_params", None))
    created_params = created_params_list[0]
    assert getattr(created_params, "snapshot", None) == "daytona-medium"
    # FakeDaytona simulates medium tier baseline
    assert sb.cpu == 2
    assert sb.memory == 4
    assert sb.last_resize is None
    assert sb.wait_called_with is None


async def test_create_sandbox_custom_cpu_memory_prefers_image_resources(monkeypatch):
    _install_fake_daytona(monkeypatch)
    import src.autobots.conn.daytona.daytona_client as dc
    importlib.reload(dc)
    await _patch_to_thread(monkeypatch, dc)
    _patch_settings(monkeypatch, dc)

    client = dc.DaytonaClient(api_key="k", api_url="https://x")
    info = await _create_sandbox(
        client,
        ctx=None,
        language="python",
        env_vars={},
        snapshot="daytona-small",
        cpu=3,
        memory_gib=6,
    )
    sb = client._sandbox_cache[info.sandbox_id]
    # Preferred behavior: image+resources at creation time; no resize call.
    assert sb.cpu == 3
    assert sb.memory == 6
    assert sb.last_resize is None
    assert sb.wait_called_with is None

    created_params_list = _as_list(getattr(cast(Any, client._client), "created_params", None))
    assert created_params_list
    created_params = created_params_list[0]
    assert isinstance(created_params, FakeCreateSandboxFromImageParams)
    assert created_params.resources.cpu == 3
    assert created_params.resources.memory == 6
    assert created_params.ephemeral is True
    assert created_params.ttl_minutes == 15
    assert created_params.auto_stop_interval == 11
    assert created_params.auto_archive_interval is None
    assert created_params.auto_delete_interval == 0


async def test_wait_for_resize_complete_keyword_fallbacks_to_positional(monkeypatch):
    """
    Ensure our DaytonaClient is robust if wait_for_resize_complete does not accept keyword args.
    (We simulate a positional-only signature using Python's '/' positional-only syntax.)
    """
    _install_fake_daytona(monkeypatch, include_image_params=False)
    import src.autobots.conn.daytona.daytona_client as dc
    importlib.reload(dc)
    await _patch_to_thread(monkeypatch, dc)
    _patch_settings(monkeypatch, dc)

    client = dc.DaytonaClient(api_key="k", api_url="https://x")
    info = await _create_sandbox(
        client,
        ctx=None,
        language="python",
        env_vars={},
        snapshot="daytona-small",
    )
    sb = client._sandbox_cache[info.sandbox_id]

    # Override with a positional-only wait method to force TypeError on keyword call.
    def _pos_only_wait(self, timeout, /):
        self.wait_called_with = timeout

    sb.wait_for_resize_complete = _pos_only_wait.__get__(sb, type(sb))  # type: ignore[attr-defined]

    # Trigger resize path
    info2 = await _create_sandbox(
        client,
        ctx=None,
        language="python",
        env_vars={},
        snapshot="daytona-small",
        cpu=3,
        memory_gib=6,
    )
    sb2 = client._sandbox_cache[info2.sandbox_id]
    assert sb2.last_resize is not None
    assert sb2.wait_called_with == 120


async def test_exec_calls_process_exec_with_kwargs(monkeypatch):
    _install_fake_daytona(monkeypatch)
    import src.autobots.conn.daytona.daytona_client as dc
    importlib.reload(dc)
    await _patch_to_thread(monkeypatch, dc)

    client = dc.DaytonaClient(api_key="k", api_url="https://x")
    info = await _create_sandbox(
        client,
        ctx=None,
        language="python",
        env_vars={},
    )
    code, out = await client.exec(
        ctx=None,
        sandbox_id=info.sandbox_id,
        command="echo hi",
        timeout_sec=12,
        cwd="/tmp",
        env={"X": "1"},
    )

    assert code == 0
    assert out == "exec_ok"

    sb = client._sandbox_cache[info.sandbox_id]
    proc = sb.process
    assert proc.last_exec is not None
    assert proc.last_exec["command"] == "echo hi"
    assert proc.last_exec["cwd"] == "/tmp"
    assert proc.last_exec["env"] == {"X": "1"}
    assert proc.last_exec["timeout"] == 12


async def test_session_execute_uses_SessionExecuteRequest_and_run_async_true(monkeypatch):
    _install_fake_daytona(monkeypatch)
    import src.autobots.conn.daytona.daytona_client as dc
    importlib.reload(dc)
    await _patch_to_thread(monkeypatch, dc)

    client = dc.DaytonaClient(api_key="k", api_url="https://x")
    info = await _create_sandbox(
        client,
        ctx=None,
        language="python",
        env_vars={},
    )

    session_id = await client.create_session(ctx=None, sandbox_id=info.sandbox_id, session_id="sess_1")
    assert session_id == "sess_1"

    cmd_id = await client.execute_session_command(
        ctx=None,
        sandbox_id=info.sandbox_id,
        session_id=session_id,
        command="python3 runner.py",
        timeout_sec=99,
    )
    assert cmd_id == "cmd_abc"

    sb = client._sandbox_cache[info.sandbox_id]
    proc = sb.process
    assert proc.last_session_exec is not None
    last_session_exec = _as_dict(proc.last_session_exec)
    req = last_session_exec["req"]
    assert isinstance(req, FakeSessionExecuteRequest)
    assert req.command == "python3 runner.py"
    assert req.run_async is True
    assert last_session_exec["timeout"] == 99


async def test_get_session_command_logs_calls_sdk_method(monkeypatch):
    _install_fake_daytona(monkeypatch)
    import src.autobots.conn.daytona.daytona_client as dc
    importlib.reload(dc)
    await _patch_to_thread(monkeypatch, dc)

    client = dc.DaytonaClient(api_key="k", api_url="https://x")
    info = await _create_sandbox(
        client,
        ctx=None,
        language="python",
        env_vars={},
    )

    sid = await client.create_session(ctx=None, sandbox_id=info.sandbox_id, session_id="sess_1")
    _ = await client.execute_session_command(ctx=None, sandbox_id=info.sandbox_id, session_id=sid, command="cmd")

    stdout, stderr = await client.get_session_command_logs(
        ctx=None,
        sandbox_id=info.sandbox_id,
        session_id=sid,
        command_id="cmd_abc",
    )
    assert stdout == "log_out"
    assert stderr == "log_err"


async def test_get_session_command_logs_falls_back_to_async_name(monkeypatch):
    """
    Verify our fallback probe works:
      get_session_command_logs OR get_session_command_logs_async
    """
    _install_fake_daytona(monkeypatch)
    import src.autobots.conn.daytona.daytona_client as dc
    importlib.reload(dc)
    await _patch_to_thread(monkeypatch, dc)
    _patch_settings(monkeypatch, dc)

    client = dc.DaytonaClient(api_key="k", api_url="https://x")
    info = await _create_sandbox(
        client,
        ctx=None,
        language="python",
        env_vars={},
    )

    # Force fallback:
    #   - sb.process.get_session_command_logs is a class method in FakeProcess, so delattr(instance, ...) fails.
    #   - Shadow it on the instance with None so getattr(...) returns non-callable and code falls back.
    sb = client._sandbox_cache[info.sandbox_id]
    assert hasattr(sb.process, "get_session_command_logs")
    sb.process.get_session_command_logs = None

    sid = await client.create_session(ctx=None, sandbox_id=info.sandbox_id, session_id="sess_1")
    _ = await client.execute_session_command(ctx=None, sandbox_id=info.sandbox_id, session_id=sid, command="cmd")

    stdout, stderr = await client.get_session_command_logs(ctx=None, sandbox_id=info.sandbox_id, session_id=sid, command_id="cmd_abc")
    assert stdout == "log_out_async"
    assert stderr == "log_err_async"


async def test_write_file_prefers_fs_upload_bytes_first(monkeypatch):
    _install_fake_daytona(monkeypatch, fs_typeerror_on_bytes=False)
    import src.autobots.conn.daytona.daytona_client as dc
    importlib.reload(dc)
    await _patch_to_thread(monkeypatch, dc)

    client = dc.DaytonaClient(api_key="k", api_url="https://x")
    info = await _create_sandbox(
        client,
        ctx=None,
        language="python",
        env_vars={},
    )

    content = b"print('hi')"
    await client.write_file(ctx=None, sandbox_id=info.sandbox_id, path="runner.py", content=content)

    sb = client._sandbox_cache[info.sandbox_id]
    fs = sb.fs
    assert fs.calls, "Expected upload_file to be called"
    arg1, path = fs.calls[0]
    assert isinstance(arg1, (bytes, bytearray))
    assert arg1 == content
    assert path == "runner.py"


async def test_write_file_typeerror_falls_back_to_local_path_upload(monkeypatch):
    """
    This unit test verifies your *fallback signature handling*.
    It will FAIL if daytona_client.py is missing `import tempfile` and `import os`.
    That’s good: it catches that exact bug.
    """
    _install_fake_daytona(monkeypatch, fs_typeerror_on_bytes=True)
    import src.autobots.conn.daytona.daytona_client as dc
    importlib.reload(dc)
    await _patch_to_thread(monkeypatch, dc)

    client = dc.DaytonaClient(api_key="k", api_url="https://x")
    info = await _create_sandbox(
        client,
        ctx=None,
        language="python",
        env_vars={},
    )

    content = b"x" * 10
    await client.write_file(ctx=None, sandbox_id=info.sandbox_id, path="runner.py", content=content)

    sb = client._sandbox_cache[info.sandbox_id]
    fs = sb.fs
    assert len(fs.calls) >= 1
    # After TypeError, we expect a call where arg1 is a local file path (str)
    assert any(isinstance(call[0], str) and call[1] == "runner.py" for call in fs.calls)


 
async def test_write_file_b64_large_payload_uses_chunking(monkeypatch):
    _install_fake_daytona(monkeypatch)
    import src.autobots.conn.daytona.daytona_client as dc
    importlib.reload(dc)
    await _patch_to_thread(monkeypatch, dc)

    client = dc.DaytonaClient(api_key="k", api_url="https://x")
    info = await _create_sandbox(
        client,
        ctx=None,
        language="python",
        env_vars={},
    )
    sb = client._sandbox_cache[info.sandbox_id]

    # Should NOT raise anymore; large payload should be written via chunked exec calls.
    await client.write_file_b64(ctx=None, sandbox_id=info.sandbox_id, path="big.bin", content=b"x" * 200_000)

    # We expect multiple exec calls: one init + multiple chunk appends.
    assert len(sb.process.exec_history) >= 2
    cmds = [c.get("command", "") for c in sb.process.exec_history]
    assert any("open('big.bin','wb')" in cmd or "open(\"big.bin\",\"wb\")" in cmd for cmd in cmds), "Expected init write"
    assert any("'ab'" in cmd or "\"ab\"" in cmd for cmd in cmds), "Expected append-binary chunk writes"


async def test_start_sandbox_does_not_restart_when_already_started(monkeypatch):
    _install_fake_daytona(monkeypatch)
    import src.autobots.conn.daytona.daytona_client as dc
    importlib.reload(dc)
    await _patch_to_thread(monkeypatch, dc)
    _patch_settings(monkeypatch, dc)

    client = dc.DaytonaClient(api_key="k", api_url="https://x")
    info = await _create_sandbox(
        client,
        ctx=None,
        language="python",
        env_vars={},
    )
    sb = client._sandbox_cache[info.sandbox_id]

    # Simulate SDK-like state string form that previously broke normalization.
    sb.state = "SandboxState.STARTED"
    sb.start_called = 0

    await client.start_sandbox(ctx=None, sandbox_id=info.sandbox_id, timeout_sec=5)

    # Should not call start() when already started.
    assert sb.start_called == 0
    # Reuse must never alter lifecycle settings.
    assert sb.autostop_interval_set is None
    # The already-started fast path currently returns without refreshing activity.
    assert sb.refresh_activity_called == 0


async def test_start_sandbox_starts_when_stopped(monkeypatch):
    _install_fake_daytona(monkeypatch)
    import src.autobots.conn.daytona.daytona_client as dc
    importlib.reload(dc)
    await _patch_to_thread(monkeypatch, dc)
    _patch_settings(monkeypatch, dc)

    client = dc.DaytonaClient(api_key="k", api_url="https://x")
    info = await _create_sandbox(
        client,
        ctx=None,
        language="python",
        env_vars={},
    )
    sb = client._sandbox_cache[info.sandbox_id]

    sb.state = "SandboxState.STOPPED"
    sb.start_called = 0

    await client.start_sandbox(ctx=None, sandbox_id=info.sandbox_id, timeout_sec=5)

    assert sb.start_called == 1
    assert sb.state == "SandboxState.STARTED"
    # Starting a reused sandbox must not rewrite its lifecycle policy.
    assert sb.autostop_interval_set is None
    assert sb.refresh_activity_called >= 1


async def test_stop_sandbox_calls_sdk_stop(monkeypatch):
    _install_fake_daytona(monkeypatch)
    import src.autobots.conn.daytona.daytona_client as dc
    importlib.reload(dc)
    await _patch_to_thread(monkeypatch, dc)
    _patch_settings(monkeypatch, dc)

    client = dc.DaytonaClient(api_key="k", api_url="https://x")
    info = await _create_sandbox(
        client,
        ctx=None,
        language="python",
        env_vars={},
    )
    sb = client._sandbox_cache[info.sandbox_id]
    sb.state = "SandboxState.STARTED"

    await client.stop_sandbox(
        ctx=None,
        sandbox_id=info.sandbox_id,
        timeout_sec=5,
    )

    assert sb.stop_called == 1
    assert sb.state == "SandboxState.STOPPED"
    assert sb.autostop_interval_set is None


async def test_ensure_vnc_calls_start_once_and_then_skips_when_port_is_up(monkeypatch):
    _install_fake_daytona(monkeypatch)
    import src.autobots.conn.daytona.daytona_client as dc
    importlib.reload(dc)
    await _patch_to_thread(monkeypatch, dc)
    _patch_settings(monkeypatch, dc)

    client = dc.DaytonaClient(api_key="k", api_url="https://x")
    info = await _create_sandbox(
        client,
        ctx=None,
        language="python",
        env_vars={},
    )
    sb = client._sandbox_cache[info.sandbox_id]

    # First call:
    # - pre-probe fails (vnc_probe_calls=1 => NO)
    # - then computer_use.start() is called
    # - readiness probe succeeds (vnc_probe_calls>=2 => OK)
    out1 = await client.ensure_vnc(
        ctx=None,
        sandbox_id=info.sandbox_id,
        novnc_port=6080,
        wait_ready_sec=2,
    )
    assert out1.get("novnc_port") == 6080
    assert sb.computer_use.start_called == 1
    assert sb.autostop_interval_set is None

    # Second call:
    # - pre-probe succeeds => must NOT call start() again
    out2 = await client.ensure_vnc(
        ctx=None,
        sandbox_id=info.sandbox_id,
        novnc_port=6080,
        wait_ready_sec=2,
    )
    assert out2.get("novnc_port") == 6080
    assert sb.computer_use.start_called == 1
    assert sb.autostop_interval_set is None


async def test_get_signed_preview_url_returns_url(monkeypatch):
    _install_fake_daytona(monkeypatch)
    import src.autobots.conn.daytona.daytona_client as dc
    importlib.reload(dc)
    await _patch_to_thread(monkeypatch, dc)

    client = dc.DaytonaClient(api_key="k", api_url="https://x")
    info = await _create_sandbox(
        client,
        ctx=None,
        language="python",
        env_vars={},
    )

    url = await client.get_signed_preview_url(ctx=None, sandbox_id=info.sandbox_id, port=6080, expires_in_seconds=900)
    assert url.startswith("https://preview.test/")
    p = urlparse(url)
    assert p.scheme in {"http", "https"}
    assert "ttl=900" in (p.query or "")


async def test_exec_falls_back_to_toolbox_http_when_sdk_exec_fails(monkeypatch):
    _install_fake_daytona(monkeypatch)
    import src.autobots.conn.daytona.daytona_client as dc
    importlib.reload(dc)
    await _patch_to_thread(monkeypatch, dc)
    _patch_settings(monkeypatch, dc)

    # Patch _get_sandbox_sync to force toolbox fallback
    def _boom(_sandbox_id: str):
        raise RuntimeError("no sdk sandbox")

    client = dc.DaytonaClient(api_key="k", api_url="https://x")
    monkeypatch.setattr(client, "_get_sandbox_sync", _boom)

    class FakeResp:
        status_code = 200
        text = "OK"
        def json(self):
            return {"exitCode": 0, "result": "toolbox_ok"}

    class FakeAsyncClient:
        # store last request class-wide for assertions
        last_request = None
        def __init__(self, timeout=None):
            self.timeout = timeout
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc, tb):
            return False
        async def post(self, url, headers=None, json=None):
            FakeAsyncClient.last_request = {"url": url, "headers": headers, "json": json}
            return FakeResp()

    monkeypatch.setattr(dc.httpx, "AsyncClient", FakeAsyncClient)

    code, out = await client.exec(ctx=None, sandbox_id="sb_x", command="echo hi", timeout_sec=3)
    assert code == 0
    assert out == "toolbox_ok"

    # Verify SDK->Toolbox syntax: correct URL + headers + payload keys
    req = _as_dict(FakeAsyncClient.last_request)
    assert req["url"].endswith("/sb_x/process/execute")
    assert req["headers"]["Authorization"].startswith("Bearer ")
    assert "command" in req["json"]
    assert "timeout" in req["json"]


async def test_start_sandbox_permission_error_is_not_swallowed(monkeypatch):
    _install_fake_daytona(monkeypatch)
    import src.autobots.conn.daytona.daytona_client as dc
    importlib.reload(dc)
    await _patch_to_thread(monkeypatch, dc)
    _patch_settings(monkeypatch, dc)

    client = dc.DaytonaClient(api_key="k", api_url="https://x")
    info = await _create_sandbox(client)
    sb = client._sandbox_cache[info.sandbox_id]
    sb.state = "SandboxState.STOPPED"

    class ForbiddenError(RuntimeError):
        status_code = 403

    def _forbidden_start(*args, **kwargs):
        raise ForbiddenError("403 Access denied")

    sb.start = _forbidden_start

    with pytest.raises(dc.DaytonaPermissionError) as exc:
        await client.start_sandbox(
            ctx=None,
            sandbox_id=info.sandbox_id,
            timeout_sec=5,
        )

    assert exc.value.operation == "start"
    assert exc.value.status_code == 403


async def test_stop_sandbox_permission_error_is_not_swallowed(monkeypatch):
    _install_fake_daytona(monkeypatch)
    import src.autobots.conn.daytona.daytona_client as dc
    importlib.reload(dc)
    await _patch_to_thread(monkeypatch, dc)
    _patch_settings(monkeypatch, dc)

    client = dc.DaytonaClient(api_key="k", api_url="https://x")
    info = await _create_sandbox(client)
    sb = client._sandbox_cache[info.sandbox_id]

    class ForbiddenError(RuntimeError):
        status_code = 403

    def _forbidden_stop(*args, **kwargs):
        raise ForbiddenError("403 Access denied")

    sb.stop = _forbidden_stop

    with pytest.raises(dc.DaytonaPermissionError) as exc:
        await client.stop_sandbox(
            ctx=None,
            sandbox_id=info.sandbox_id,
            timeout_sec=5,
        )

    assert exc.value.operation == "stop"
    assert exc.value.status_code == 403


async def test_delete_sandbox_calls_sdk_delete_with_object(monkeypatch):
    _install_fake_daytona(monkeypatch)
    import src.autobots.conn.daytona.daytona_client as dc
    importlib.reload(dc)
    await _patch_to_thread(monkeypatch, dc)

    client = dc.DaytonaClient(api_key="k", api_url="https://x")
    info = await _create_sandbox(
        client,
        ctx=None,
        language="python",
        env_vars={},
    )
    sb = client._sandbox_cache[info.sandbox_id]

    await client.delete_sandbox(ctx=None, sandbox_id=info.sandbox_id)

    deleted_list = _as_list(getattr(cast(Any, client._client), "deleted", None))
    assert deleted_list, "Expected FakeDaytona.delete to be called"
    assert deleted_list[-1] is sb, "Expected Daytona.delete called with sandbox OBJECT"


async def test_delete_sandbox_permission_error_is_not_swallowed(monkeypatch):
    _install_fake_daytona(monkeypatch)
    import src.autobots.conn.daytona.daytona_client as dc
    importlib.reload(dc)
    await _patch_to_thread(monkeypatch, dc)
    _patch_settings(monkeypatch, dc)

    client = dc.DaytonaClient(api_key="k", api_url="https://x")
    info = await _create_sandbox(client)

    class ForbiddenError(RuntimeError):
        status_code = 403

    def _forbidden_delete(_sandbox):
        raise ForbiddenError("403 Access denied")

    client._client.delete = _forbidden_delete

    with pytest.raises(dc.DaytonaPermissionError) as exc:
        await client.delete_sandbox(
            ctx=None,
            sandbox_id=info.sandbox_id,
        )

    assert exc.value.operation == "delete"
    assert exc.value.status_code == 403


async def test_delete_session_uses_sdk_when_available(monkeypatch):
    _install_fake_daytona(monkeypatch)
    import src.autobots.conn.daytona.daytona_client as dc
    importlib.reload(dc)
    await _patch_to_thread(monkeypatch, dc)

    client = dc.DaytonaClient(api_key="k", api_url="https://x")
    info = await _create_sandbox(
        client,
        ctx=None,
        language="python",
        env_vars={},
    )
    sid = await client.create_session(ctx=None, sandbox_id=info.sandbox_id, session_id="sess_1")

    await client.delete_session(ctx=None, sandbox_id=info.sandbox_id, session_id=sid)
    sb = client._sandbox_cache[info.sandbox_id]
    assert "sess_1" in sb.process.deleted_sessions


async def test_delete_session_falls_back_to_toolbox_when_sdk_missing(monkeypatch):
    _install_fake_daytona(monkeypatch)
    import src.autobots.conn.daytona.daytona_client as dc
    importlib.reload(dc)
    await _patch_to_thread(monkeypatch, dc)
    _patch_settings(monkeypatch, dc)

    client = dc.DaytonaClient(api_key="k", api_url="https://x")
    info = await _create_sandbox(
        client,
        ctx=None,
        language="python",
        env_vars={},
    )

    # Force toolbox fallback:
    # delete_session is a class method in FakeProcess, so delattr(instance, ...) fails.
    # Shadow it on the instance with None so the code path skips SDK method.
    sb = client._sandbox_cache[info.sandbox_id]
    sb.process.delete_session = None

    class FakeDelResp:
        status_code = 204
        text = ""

    class FakeAsyncClient:
        last_request = None
        def __init__(self, timeout=None):
            self.timeout = timeout
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc, tb):
            return False
        async def delete(self, url, headers=None):
            FakeAsyncClient.last_request = {"url": url, "headers": headers}
            return FakeDelResp()

    monkeypatch.setattr(dc.httpx, "AsyncClient", FakeAsyncClient)

    await client.delete_session(ctx=None, sandbox_id=info.sandbox_id, session_id="sess_x")

    req = _as_dict(FakeAsyncClient.last_request)
    assert req["url"].endswith(f"/{info.sandbox_id}/process/session/sess_x")
    assert req["headers"]["Authorization"].startswith("Bearer ")
 
 
async def test_delete_session_treats_toolbox_404_as_success(monkeypatch):
    _install_fake_daytona(monkeypatch)
    import src.autobots.conn.daytona.daytona_client as dc
    importlib.reload(dc)
    await _patch_to_thread(monkeypatch, dc)
    _patch_settings(monkeypatch, dc)

    client = dc.DaytonaClient(api_key="k", api_url="https://x")
    info = await _create_sandbox(client)

    sb = client._sandbox_cache[info.sandbox_id]
    sb.process.delete_session = None

    class FakeDelResp:
        status_code = 404
        text = '{"message":"not found"}'

    class FakeAsyncClient:
        def __init__(self, timeout=None):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def delete(self, url, headers=None):
            return FakeDelResp()

    monkeypatch.setattr(dc.httpx, "AsyncClient", FakeAsyncClient)

    # Must not raise or log a cleanup failure.
    await client.delete_session(
        ctx=None,
        sandbox_id=info.sandbox_id,
        session_id="already_missing",
    )
