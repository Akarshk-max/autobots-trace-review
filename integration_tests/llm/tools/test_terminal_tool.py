"""
Comprehensive tests for the terminal tool and terminal connection manager.

This test simulates a CLI agent WebSocket connection within the test process,
then calls run_terminal_command which sends commands through the connection.

Run:
    poetry run pytest tests/llm/tools/test_terminal_tool.py -v -s --timeout=300
"""

import asyncio

import pytest
import pytest_asyncio
from pydantic import ValidationError

from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.llm.tools.terminal_tool import (
    TerminalCommandConfig,
    TerminalCommandOutput,
    run_terminal_command,
)
from src.autobots.terminal.terminal_connection_manager import (
    TerminalConnectionManager,
    terminal_manager,
)

TEST_USER_ID = "test_terminal_user"


# ---------------------------------------------------------------------------
# Fake WebSocket helpers
# ---------------------------------------------------------------------------

class FakeWebSocket:
    """Mimics a FastAPI WebSocket that executes commands locally like the CLI agent."""

    def __init__(self):
        self._sent: list = []

    async def send_json(self, data: dict):
        """Server sends a command to the 'CLI agent'."""
        self._sent.append(data)
        if data.get("type") == "command":
            command = data["command"]
            request_id = data["request_id"]
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await proc.communicate()
            terminal_manager.resolve_response(
                user_id=TEST_USER_ID,
                request_id=request_id,
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
                exit_code=proc.returncode,
            )


class SlowFakeWebSocket:
    """WebSocket that delays the response to simulate a slow CLI agent."""

    def __init__(self, delay_sec: float):
        self._delay = delay_sec
        self._sent: list = []

    async def send_json(self, data: dict):
        self._sent.append(data)
        if data.get("type") == "command":
            request_id = data["request_id"]
            await asyncio.sleep(self._delay)
            terminal_manager.resolve_response(
                user_id=TEST_USER_ID,
                request_id=request_id,
                stdout="slow response",
                stderr="",
                exit_code=0,
            )


class BrokenWebSocket:
    """WebSocket that raises an exception when trying to send."""

    async def send_json(self, data: dict):
        raise ConnectionError("WebSocket connection broken")


class NeverRespondWebSocket:
    """WebSocket that accepts the command but never resolves — simulates a hung CLI agent."""

    def __init__(self):
        self._sent: list = []

    async def send_json(self, data: dict):
        self._sent.append(data)
        # Never calls resolve_response


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def terminal_connection():
    """Register a FakeWebSocket for the test user, clean up after."""
    ws = FakeWebSocket()
    await terminal_manager.connect(TEST_USER_ID, ws)
    yield ws
    await terminal_manager.disconnect(TEST_USER_ID)


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_test_user():
    """Ensure no leftover connections from previous tests."""
    yield
    # Defensive cleanup — disconnect if still connected
    if await terminal_manager.is_connected(TEST_USER_ID):
        await terminal_manager.disconnect(TEST_USER_ID)


# ===========================================================================
# 1. BASIC COMMAND EXECUTION
# ===========================================================================

@pytest.mark.asyncio
async def test_echo_command(set_test_settings, terminal_connection):
    """Simple echo command returns expected output."""
    ctx = Context(user_id=TEST_USER_ID)
    config = TerminalCommandConfig(command="echo hello from terminal tool", timeout_sec=10)

    result = await run_terminal_command(config=config, ctx=ctx, app_state=AppState())

    assert isinstance(result, TerminalCommandOutput)
    print(f"\n[echo] success={result.success} | output: {result.output}")
    assert result.success is True
    assert "hello from terminal tool" in result.output


@pytest.mark.asyncio
async def test_pwd_command(set_test_settings, terminal_connection):
    """pwd returns a non-empty directory path."""
    ctx = Context(user_id=TEST_USER_ID)
    config = TerminalCommandConfig(command="pwd", timeout_sec=10)

    result = await run_terminal_command(config=config, ctx=ctx, app_state=AppState())

    assert isinstance(result, TerminalCommandOutput)
    print(f"\n[pwd] success={result.success} | output: {result.output}")
    assert result.success is True
    assert result.output.strip().startswith("/")


@pytest.mark.asyncio
async def test_multiline_output(set_test_settings, terminal_connection):
    """Command that produces multi-line output is captured fully."""
    ctx = Context(user_id=TEST_USER_ID)
    config = TerminalCommandConfig(command="printf 'line1\nline2\nline3'", timeout_sec=10)

    result = await run_terminal_command(config=config, ctx=ctx, app_state=AppState())

    assert isinstance(result, TerminalCommandOutput)
    print(f"\n[multiline] success={result.success} | output:\n{result.output}")
    assert result.success is True
    lines = result.output.strip().split("\n")
    assert len(lines) == 3
    assert lines[0] == "line1"
    assert lines[2] == "line3"


@pytest.mark.asyncio
async def test_command_with_special_characters(set_test_settings, terminal_connection):
    """Commands with special chars (quotes, pipes, redirection) work correctly."""
    ctx = Context(user_id=TEST_USER_ID)
    config = TerminalCommandConfig(
        command="echo 'hello \"world\"' | tr '[:lower:]' '[:upper:]'", timeout_sec=10
    )

    result = await run_terminal_command(config=config, ctx=ctx, app_state=AppState())

    assert isinstance(result, TerminalCommandOutput)
    print(f"\n[special_chars] success={result.success} | output: {result.output}")
    assert result.success is True
    assert "HELLO" in result.output


@pytest.mark.asyncio
async def test_command_with_env_variable(set_test_settings, terminal_connection):
    """Shell env variable expansion works."""
    ctx = Context(user_id=TEST_USER_ID)
    config = TerminalCommandConfig(command="echo $HOME", timeout_sec=10)

    result = await run_terminal_command(config=config, ctx=ctx, app_state=AppState())

    assert isinstance(result, TerminalCommandOutput)
    print(f"\n[env_var] success={result.success} | output: {result.output}")
    assert result.success is True
    assert result.output.strip().startswith("/")


@pytest.mark.asyncio
async def test_piped_commands(set_test_settings, terminal_connection):
    """Piped commands execute end-to-end."""
    ctx = Context(user_id=TEST_USER_ID)
    config = TerminalCommandConfig(
        command="echo 'apple\nbanana\ncherry' | grep banana | wc -l", timeout_sec=10
    )

    result = await run_terminal_command(config=config, ctx=ctx, app_state=AppState())

    assert isinstance(result, TerminalCommandOutput)
    print(f"\n[piped] success={result.success} | output: {result.output}")
    assert result.success is True
    assert result.output.strip() == "1"


@pytest.mark.asyncio
async def test_empty_output_command(set_test_settings, terminal_connection):
    """Command that produces no output returns '(no output)' and success."""
    ctx = Context(user_id=TEST_USER_ID)
    config = TerminalCommandConfig(command="true", timeout_sec=10)

    result = await run_terminal_command(config=config, ctx=ctx, app_state=AppState())

    assert isinstance(result, TerminalCommandOutput)
    print(f"\n[empty_output] success={result.success} | output: {result.output}")
    assert result.success is True
    assert result.output == "(no output)"


# ===========================================================================
# 2. FAILURE & ERROR HANDLING
# ===========================================================================

@pytest.mark.asyncio
async def test_failing_command(set_test_settings, terminal_connection):
    """Non-zero exit code is reported as success=False with stderr."""
    ctx = Context(user_id=TEST_USER_ID)
    config = TerminalCommandConfig(command="ls /nonexistent_path_12345", timeout_sec=10)

    result = await run_terminal_command(config=config, ctx=ctx, app_state=AppState())

    assert isinstance(result, TerminalCommandOutput)
    print(f"\n[fail] success={result.success} | output: {result.output}")
    assert result.success is False
    assert "STDERR:" in result.output or "No such file" in result.output


@pytest.mark.asyncio
async def test_exit_code_nonzero(set_test_settings, terminal_connection):
    """Explicit exit 1 is captured as failure."""
    ctx = Context(user_id=TEST_USER_ID)
    config = TerminalCommandConfig(command="exit 1", timeout_sec=10)

    result = await run_terminal_command(config=config, ctx=ctx, app_state=AppState())

    assert isinstance(result, TerminalCommandOutput)
    print(f"\n[exit_1] success={result.success} | output: {result.output}")
    assert result.success is False


@pytest.mark.asyncio
async def test_stderr_only_command(set_test_settings, terminal_connection):
    """Command that writes only to stderr is captured."""
    ctx = Context(user_id=TEST_USER_ID)
    config = TerminalCommandConfig(command="echo error_message >&2", timeout_sec=10)

    result = await run_terminal_command(config=config, ctx=ctx, app_state=AppState())

    assert isinstance(result, TerminalCommandOutput)
    print(f"\n[stderr_only] success={result.success} | output: {result.output}")
    assert result.success is True  # exit code is still 0
    assert "error_message" in result.output


@pytest.mark.asyncio
async def test_mixed_stdout_stderr(set_test_settings, terminal_connection):
    """Both stdout and stderr are captured together."""
    ctx = Context(user_id=TEST_USER_ID)
    config = TerminalCommandConfig(
        command="echo stdout_line && echo stderr_line >&2", timeout_sec=10
    )

    result = await run_terminal_command(config=config, ctx=ctx, app_state=AppState())

    assert isinstance(result, TerminalCommandOutput)
    print(f"\n[mixed] success={result.success} | output:\n{result.output}")
    assert result.success is True
    assert "stdout_line" in result.output
    assert "stderr_line" in result.output


@pytest.mark.asyncio
async def test_not_connected_error(set_test_settings):
    """Returns a clear error when no CLI agent is connected for the user."""
    ctx = Context(user_id="nonexistent_user_id")
    config = TerminalCommandConfig(command="echo test", timeout_sec=10)

    result = await run_terminal_command(config=config, ctx=ctx, app_state=AppState())

    assert isinstance(result, TerminalCommandOutput)
    print(f"\n[not_connected] success={result.success} | output: {result.output}")
    assert result.success is False
    assert "No terminal CLI agent connected" in result.output


@pytest.mark.asyncio
async def test_broken_websocket_returns_error(set_test_settings):
    """If the WebSocket raises on send, the tool returns an error gracefully."""
    ws = BrokenWebSocket()
    await terminal_manager.connect(TEST_USER_ID, ws)
    try:
        ctx = Context(user_id=TEST_USER_ID)
        config = TerminalCommandConfig(command="echo test", timeout_sec=10)

        result = await run_terminal_command(config=config, ctx=ctx, app_state=AppState())

        assert isinstance(result, TerminalCommandOutput)
        print(f"\n[broken_ws] success={result.success} | output: {result.output}")
        assert result.success is False
        assert "broken" in result.output.lower() or "error" in result.output.lower()
    finally:
        await terminal_manager.disconnect(TEST_USER_ID)


# ===========================================================================
# 3. TIMEOUT HANDLING
# ===========================================================================

@pytest.mark.asyncio
async def test_timeout_on_hung_command(set_test_settings):
    """Command that never responds times out within the specified timeout_sec."""
    ws = NeverRespondWebSocket()
    await terminal_manager.connect(TEST_USER_ID, ws)
    try:
        ctx = Context(user_id=TEST_USER_ID)
        config = TerminalCommandConfig(command="echo should_timeout", timeout_sec=2)

        result = await run_terminal_command(config=config, ctx=ctx, app_state=AppState())

        assert isinstance(result, TerminalCommandOutput)
        print(f"\n[timeout] success={result.success} | output: {result.output}")
        assert result.success is False
        assert "timed out" in result.output.lower()
    finally:
        await terminal_manager.disconnect(TEST_USER_ID)


@pytest.mark.asyncio
async def test_slow_but_within_timeout(set_test_settings):
    """A slow response that arrives before the timeout succeeds."""
    ws = SlowFakeWebSocket(delay_sec=1.0)
    await terminal_manager.connect(TEST_USER_ID, ws)
    try:
        ctx = Context(user_id=TEST_USER_ID)
        config = TerminalCommandConfig(command="echo slow", timeout_sec=10)

        result = await run_terminal_command(config=config, ctx=ctx, app_state=AppState())

        assert isinstance(result, TerminalCommandOutput)
        print(f"\n[slow_ok] success={result.success} | output: {result.output}")
        assert result.success is True
        assert "slow response" in result.output
    finally:
        await terminal_manager.disconnect(TEST_USER_ID)


# ===========================================================================
# 4. CONNECTION MANAGER BEHAVIOR
# ===========================================================================

@pytest.mark.asyncio
async def test_is_connected_and_disconnect(set_test_settings):
    """connect/is_connected/disconnect lifecycle works correctly."""
    user = "test_lifecycle_user"
    ws = FakeWebSocket()

    assert await terminal_manager.is_connected(user) is False

    await terminal_manager.connect(user, ws)
    assert await terminal_manager.is_connected(user) is True
    assert user in await terminal_manager.get_connected_users()

    await terminal_manager.disconnect(user)
    assert await terminal_manager.is_connected(user) is False
    assert user not in await terminal_manager.get_connected_users()
    print("\n[lifecycle] connect → is_connected → disconnect: OK")


@pytest.mark.asyncio
async def test_reconnection_replaces_old_connection(set_test_settings):
    """Reconnecting replaces the old WebSocket and the new one is used."""
    user = "test_reconnect_user"
    ws_old = FakeWebSocket()
    ws_new = FakeWebSocket()

    await terminal_manager.connect(user, ws_old)
    await terminal_manager.connect(user, ws_new)

    assert await terminal_manager.is_connected(user) is True
    # New connection should be active — send a command and verify it goes to ws_new
    ctx = Context(user_id=user)
    config = TerminalCommandConfig(command="echo reconnected", timeout_sec=10)

    result = await run_terminal_command(config=config, ctx=ctx, app_state=AppState())

    assert isinstance(result, TerminalCommandOutput)
    print(f"\n[reconnect] success={result.success} | output: {result.output}")
    # Command should have been sent to ws_new, not ws_old
    assert len(ws_new._sent) == 1
    assert len(ws_old._sent) == 0

    await terminal_manager.disconnect(user)


@pytest.mark.asyncio
async def test_disconnect_cancels_pending_futures(set_test_settings):
    """Disconnecting while a command is pending raises ConnectionError."""
    ws = NeverRespondWebSocket()
    await terminal_manager.connect(TEST_USER_ID, ws)

    ctx = Context(user_id=TEST_USER_ID)

    async def send_and_wait():
        return await terminal_manager.send_command(ctx, TEST_USER_ID, "hang", timeout_sec=30)

    # Start command, then disconnect before it resolves
    task = asyncio.create_task(send_and_wait())
    await asyncio.sleep(0.1)  # let the future get registered
    await terminal_manager.disconnect(TEST_USER_ID)

    result = await task
    print(f"\n[disconnect_pending] result: {result}")
    assert "error" in result
    assert result["exit_code"] == -1


@pytest.mark.asyncio
async def test_multiple_users_isolated(set_test_settings):
    """Commands from different users don't interfere with each other."""
    user_a = "test_user_a"
    user_b = "test_user_b"

    class FakeWSForUser:
        def __init__(self, user_id):
            self._user_id = user_id
            self._sent = []

        async def send_json(self, data):
            self._sent.append(data)
            if data.get("type") == "command":
                terminal_manager.resolve_response(
                    user_id=self._user_id,
                    request_id=data["request_id"],
                    stdout=f"output_from_{self._user_id}",
                    stderr="",
                    exit_code=0,
                )

    ws_a = FakeWSForUser(user_a)
    ws_b = FakeWSForUser(user_b)
    await terminal_manager.connect(user_a, ws_a)
    await terminal_manager.connect(user_b, ws_b)

    try:
        ctx_a = Context(user_id=user_a)
        ctx_b = Context(user_id=user_b)
        config = TerminalCommandConfig(command="echo test", timeout_sec=10)

        result_a, result_b = await asyncio.gather(
            run_terminal_command(config=config, ctx=ctx_a),
            run_terminal_command(config=config, ctx=ctx_b),
        )

        assert isinstance(result_a, TerminalCommandOutput)
        assert isinstance(result_b, TerminalCommandOutput)
        print(f"\n[multi_user] A: {result_a.output} | B: {result_b.output}")
        assert "output_from_test_user_a" in result_a.output
        assert "output_from_test_user_b" in result_b.output
    finally:
        await terminal_manager.disconnect(user_a)
        await terminal_manager.disconnect(user_b)


# ===========================================================================
# 5. CONCURRENT COMMANDS
# ===========================================================================

@pytest.mark.asyncio
async def test_concurrent_commands_same_user(set_test_settings, terminal_connection):
    """Multiple commands sent concurrently for the same user all resolve correctly."""
    commands = [f"echo cmd_{i}" for i in range(5)]
    tasks = []

    for cmd in commands:
        ctx = Context(user_id=TEST_USER_ID)  # each gets a unique trace_id
        config = TerminalCommandConfig(command=cmd, timeout_sec=10)
        tasks.append(run_terminal_command(config=config, ctx=ctx, app_state=AppState()))

    results = await asyncio.gather(*tasks)

    print()
    for i, result in enumerate(results):
        assert isinstance(result, TerminalCommandOutput), f"cmd_{i}: got {type(result)}"
        print(f"[concurrent_{i}] success={result.success} | output: {result.output.strip()}")
        assert result.success is True
        assert f"cmd_{i}" in result.output


# ===========================================================================
# 6. INPUT VALIDATION (TerminalCommandConfig)
# ===========================================================================

def test_config_timeout_min_boundary():
    """timeout_sec=1 is valid (minimum)."""
    config = TerminalCommandConfig(command="echo test", timeout_sec=1)
    assert config.timeout_sec == 1


def test_config_timeout_max_boundary():
    """timeout_sec=600 is valid (maximum)."""
    config = TerminalCommandConfig(command="echo test", timeout_sec=600)
    assert config.timeout_sec == 600


def test_config_timeout_below_min_rejected():
    """timeout_sec=0 is rejected by validation."""
    with pytest.raises(ValidationError):
        TerminalCommandConfig(command="echo test", timeout_sec=0)


def test_config_timeout_above_max_rejected():
    """timeout_sec=601 is rejected by validation."""
    with pytest.raises(ValidationError):
        TerminalCommandConfig(command="echo test", timeout_sec=601)


def test_config_default_timeout():
    """Default timeout_sec is 120."""
    config = TerminalCommandConfig(command="echo test")
    assert config.timeout_sec == 120


def test_config_empty_command_allowed():
    """Empty command string is allowed at config level (will fail at execution)."""
    config = TerminalCommandConfig(command="")
    assert config.command == ""


# ===========================================================================
# 7. SINGLETON BEHAVIOR
# ===========================================================================

def test_terminal_manager_is_singleton():
    """TerminalConnectionManager always returns the same instance."""
    a = TerminalConnectionManager()
    b = TerminalConnectionManager()
    assert a is b
    assert a is terminal_manager
    print("\n[singleton] All instances are the same object: OK")


# ===========================================================================
# 8. CLAUDE CODE CLI (non-interactive -p mode)
# ===========================================================================

@pytest.mark.asyncio
async def test_claude_cli_simple_math(set_test_settings, terminal_connection):
    """Claude can answer a simple math question."""
    ctx = Context(user_id=TEST_USER_ID)
    config = TerminalCommandConfig(
        command="claude -p 'What is 2+2? Reply with just the number, nothing else.'",
        timeout_sec=60,
    )

    result = await run_terminal_command(config=config, ctx=ctx, app_state=AppState())

    assert isinstance(result, TerminalCommandOutput), f"Got {type(result)}: {result}"
    print(f"\n[claude_math] success={result.success}\n[claude_math] output:\n{result.output}")
    assert result.success is True
    assert "4" in result.output


@pytest.mark.asyncio
async def test_claude_cli_code_generation(set_test_settings, terminal_connection):
    """Claude can generate a code snippet."""
    ctx = Context(user_id=TEST_USER_ID)
    config = TerminalCommandConfig(
        command="claude -p 'Write a Python one-liner that prints hello world. Reply with just the code, no explanation.'",
        timeout_sec=60,
    )

    result = await run_terminal_command(config=config, ctx=ctx, app_state=AppState())

    assert isinstance(result, TerminalCommandOutput), f"Got {type(result)}: {result}"
    print(f"\n[claude_code] success={result.success}\n[claude_code] output:\n{result.output}")
    assert result.success is True
    assert "print" in result.output.lower()
    assert "hello" in result.output.lower()


@pytest.mark.asyncio
async def test_claude_cli_factual_question(set_test_settings, terminal_connection):
    """Claude can answer a factual question."""
    ctx = Context(user_id=TEST_USER_ID)
    config = TerminalCommandConfig(
        command="claude -p 'What is the capital of France? Reply with just the city name.'",
        timeout_sec=60,
    )

    result = await run_terminal_command(config=config, ctx=ctx, app_state=AppState())

    assert isinstance(result, TerminalCommandOutput), f"Got {type(result)}: {result}"
    print(f"\n[claude_fact] success={result.success}\n[claude_fact] output:\n{result.output}")
    assert result.success is True
    assert "paris" in result.output.lower()


@pytest.mark.asyncio
async def test_claude_cli_json_output(set_test_settings, terminal_connection):
    """Claude can return structured JSON output."""
    ctx = Context(user_id=TEST_USER_ID)
    config = TerminalCommandConfig(
        command='claude -p \'Return a JSON object with keys "name" and "age" for a 25 year old named Alice. Reply with just the JSON, nothing else.\'',
        timeout_sec=60,
    )

    result = await run_terminal_command(config=config, ctx=ctx, app_state=AppState())

    assert isinstance(result, TerminalCommandOutput), f"Got {type(result)}: {result}"
    print(f"\n[claude_json] success={result.success}\n[claude_json] output:\n{result.output}")
    assert result.success is True
    assert "alice" in result.output.lower()
    assert "25" in result.output


@pytest.mark.asyncio
async def test_claude_cli_explain_code(set_test_settings, terminal_connection):
    """Claude can explain a piece of code."""
    ctx = Context(user_id=TEST_USER_ID)
    config = TerminalCommandConfig(
        command="claude -p 'What does the Python expression [x**2 for x in range(5)] return? Reply with just the list.'",
        timeout_sec=60,
    )

    result = await run_terminal_command(config=config, ctx=ctx, app_state=AppState())

    assert isinstance(result, TerminalCommandOutput), f"Got {type(result)}: {result}"
    print(f"\n[claude_explain] success={result.success}\n[claude_explain] output:\n{result.output}")
    assert result.success is True
    assert "0" in result.output
    assert "16" in result.output
