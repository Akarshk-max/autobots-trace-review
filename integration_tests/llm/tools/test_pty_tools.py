"""
Tests for the PTY session tools.

Uses a FakePTYWebSocket that simulates the CLI agent's PTY behavior in-process,
so no real PTY or CLI agent is needed. Also includes RealPTYWebSocket-based tests
that spawn actual PTY processes (including Claude CLI interactive sessions).

Run:
    poetry run pytest tests/llm/tools/test_pty_tools.py -v -s --timeout=300
"""

import asyncio
import fcntl
import os
import pty
import struct
import termios

import pytest
import pytest_asyncio

from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.llm.tools.pty_tools import (
    ClosePTYSessionConfig,
    ClosePTYSessionOutput,
    ReadPTYOutputConfig,
    ReadPTYOutputOutput,
    SendPTYInputConfig,
    SendPTYInputOutput,
    StartPTYSessionConfig,
    StartPTYSessionOutput,
    close_pty_session,
    read_pty_output,
    send_pty_input,
    start_pty_session,
)
from src.autobots.terminal.pty_session_manager import (
    PTYSessionManager,
    PTYSessionStatus,
    pty_session_manager,
)
from src.autobots.terminal.terminal_connection_manager import terminal_manager

TEST_USER_ID = "test_pty_user"


# ---------------------------------------------------------------------------
# Fake WebSocket that simulates CLI agent PTY behavior
# ---------------------------------------------------------------------------

class FakePTYWebSocket:
    """
    Simulates the CLI agent side of PTY communication. When it receives a
    pty_start message it immediately confirms with pty_started, then enqueues
    some fake output. When it receives pty_input it echoes it back as output.
    """

    def __init__(self, user_id: str = TEST_USER_ID):
        self._user_id = user_id
        self._sent: list[dict] = []
        self._sessions: dict[str, bool] = {}  # session_id → alive

    async def send_json(self, data: dict):
        self._sent.append(data)
        msg_type = data.get("type")

        if msg_type == "pty_start":
            session_id = data["session_id"]
            self._sessions[session_id] = True
            # Simulate async confirmation from CLI agent
            await asyncio.sleep(0.05)
            pty_session_manager.mark_started(session_id)
            # Send initial prompt output
            await asyncio.sleep(0.05)
            pty_session_manager.enqueue_output(session_id, ">>> ")

        elif msg_type == "pty_input":
            session_id = data["session_id"]
            input_data = data.get("data", "")
            if self._sessions.get(session_id):
                # Echo input back as output (simulating a REPL)
                await asyncio.sleep(0.05)
                pty_session_manager.enqueue_output(session_id, input_data)
                # Simulate command result
                if input_data.strip().endswith("\\n") or input_data.strip() == "":
                    await asyncio.sleep(0.05)
                    pty_session_manager.enqueue_output(session_id, "output_result\n>>> ")

        elif msg_type == "pty_close":
            session_id = data["session_id"]
            self._sessions.pop(session_id, None)


class ErrorPTYWebSocket:
    """WebSocket that returns pty_error when trying to start a PTY."""

    async def send_json(self, data: dict):
        if data.get("type") == "pty_start":
            session_id = data["session_id"]
            await asyncio.sleep(0.05)
            pty_session_manager.mark_error(session_id, "Permission denied")


class NeverRespondPTYWebSocket:
    """WebSocket that never confirms PTY start (simulates timeout)."""

    def __init__(self):
        self._sent: list[dict] = []

    async def send_json(self, data: dict):
        self._sent.append(data)
        # Never calls mark_started — will cause timeout


class RealPTYWebSocket:
    """
    In-process fake WebSocket that actually spawns real PTY processes,
    mimicking the CLI agent's PTYProcess behavior. Used for integration
    tests that need real command execution (e.g. Claude CLI).
    """

    def __init__(self, user_id: str = TEST_USER_ID):
        self._user_id = user_id
        self._sent: list[dict] = []
        self._sessions: dict[str, dict] = {}  # session_id → {master_fd, proc, read_task}

    async def send_json(self, data: dict):
        self._sent.append(data)
        msg_type = data.get("type")

        if msg_type == "pty_start":
            session_id = data["session_id"]
            command = data.get("command", "bash")
            cols = data.get("cols", 120)
            rows = data.get("rows", 40)
            try:
                master_fd, slave_fd = pty.openpty()
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

                proc = await asyncio.create_subprocess_exec(
                    "/bin/sh", "-c", command,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    preexec_fn=os.setsid,
                )
                os.close(slave_fd)

                flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
                fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

                self._sessions[session_id] = {"master_fd": master_fd, "proc": proc}

                pty_session_manager.mark_started(session_id)

                # Start background read loop
                read_task = asyncio.create_task(self._read_loop(session_id, master_fd, proc))
                self._sessions[session_id]["read_task"] = read_task

            except Exception as e:
                pty_session_manager.mark_error(session_id, str(e))

        elif msg_type == "pty_input":
            session_id = data["session_id"]
            input_data = data.get("data", "")
            sess = self._sessions.get(session_id)
            if sess:
                os.write(sess["master_fd"], input_data.encode("utf-8"))

        elif msg_type == "pty_close":
            session_id = data["session_id"]
            await self._close_session(session_id)

    async def _read_loop(self, session_id: str, master_fd: int, proc):
        """Read from PTY master fd and enqueue output to the session manager."""
        import select
        try:
            while True:
                ready, _, _ = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: select.select([master_fd], [], [], 0.5)
                )
                if ready:
                    try:
                        data = os.read(master_fd, 4096)
                        if not data:
                            break
                        pty_session_manager.enqueue_output(
                            session_id, data.decode("utf-8", errors="replace")
                        )
                    except OSError:
                        break
                elif proc.returncode is not None:
                    # One last read attempt
                    try:
                        data = os.read(master_fd, 4096)
                        if data:
                            pty_session_manager.enqueue_output(
                                session_id, data.decode("utf-8", errors="replace")
                            )
                    except OSError:
                        pass
                    break

            if proc.returncode is None:
                try:
                    exit_code = await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    exit_code = -1
            else:
                exit_code = proc.returncode

            pty_session_manager.mark_exited(session_id, exit_code)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _close_session(self, session_id: str):
        sess = self._sessions.pop(session_id, None)
        if not sess:
            return
        read_task = sess.get("read_task")
        if read_task and not read_task.done():
            read_task.cancel()
            try:
                await read_task
            except asyncio.CancelledError:
                pass
        proc = sess.get("proc")
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        master_fd = sess.get("master_fd")
        if master_fd is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass

    async def cleanup_all(self):
        """Close all active PTY sessions."""
        for session_id in list(self._sessions.keys()):
            await self._close_session(session_id)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def pty_connection():
    """Register a FakePTYWebSocket for the test user."""
    ws = FakePTYWebSocket()
    await terminal_manager.connect(TEST_USER_ID, ws)
    yield ws
    await terminal_manager.disconnect(TEST_USER_ID)
    pty_session_manager.cleanup_user(TEST_USER_ID)


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_pty_user():
    """Ensure no leftover sessions or connections."""
    yield
    pty_session_manager.cleanup_user(TEST_USER_ID)
    if await terminal_manager.is_connected(TEST_USER_ID):
        await terminal_manager.disconnect(TEST_USER_ID)


# ===========================================================================
# 1. START PTY SESSION
# ===========================================================================

@pytest.mark.asyncio
async def test_start_pty_session_success(set_test_settings, pty_connection):
    """Starting a PTY session returns session_id and running status."""
    ctx = Context(user_id=TEST_USER_ID)
    config = StartPTYSessionConfig(command="python")

    result = await start_pty_session(config=config, ctx=ctx, app_state=AppState())

    assert isinstance(result, StartPTYSessionOutput)
    print(f"\n[start] success={result.success} | session_id={result.session_id[:8]}")
    assert result.success is True
    assert result.status == "running"
    assert result.session_id != ""

    # Cleanup
    pty_session_manager.remove_session(result.session_id)


@pytest.mark.asyncio
async def test_start_pty_session_no_connection(set_test_settings):
    """Returns failure when no CLI agent is connected."""
    ctx = Context(user_id="nonexistent_user")
    config = StartPTYSessionConfig(command="bash")

    result = await start_pty_session(config=config, ctx=ctx, app_state=AppState())

    assert isinstance(result, StartPTYSessionOutput)
    print(f"\n[start_no_conn] success={result.success}")
    assert result.success is False


@pytest.mark.asyncio
async def test_start_pty_session_error_from_cli(set_test_settings):
    """Returns failure when CLI agent reports an error."""
    ws = ErrorPTYWebSocket()
    await terminal_manager.connect(TEST_USER_ID, ws)
    try:
        ctx = Context(user_id=TEST_USER_ID)
        config = StartPTYSessionConfig(command="restricted_command")

        result = await start_pty_session(config=config, ctx=ctx, app_state=AppState())

        assert isinstance(result, StartPTYSessionOutput)
        print(f"\n[start_error] success={result.success} | status={result.status}")
        assert result.success is False
        assert result.status == "error"
    finally:
        await terminal_manager.disconnect(TEST_USER_ID)


@pytest.mark.asyncio
async def test_start_pty_session_timeout(set_test_settings):
    """Returns failure when CLI agent never confirms start (within timeout)."""
    ws = NeverRespondPTYWebSocket()
    await terminal_manager.connect(TEST_USER_ID, ws)
    try:
        ctx = Context(user_id=TEST_USER_ID)
        config = StartPTYSessionConfig(command="slow_start")

        result = await start_pty_session(config=config, ctx=ctx, app_state=AppState())

        assert isinstance(result, StartPTYSessionOutput)
        print(f"\n[start_timeout] success={result.success} | status={result.status}")
        assert result.success is False
        assert result.status == "timeout"
    finally:
        await terminal_manager.disconnect(TEST_USER_ID)


# ===========================================================================
# 2. SEND PTY INPUT
# ===========================================================================

@pytest.mark.asyncio
async def test_send_pty_input_success(set_test_settings, pty_connection):
    """Sending input to a running session succeeds."""
    ctx = Context(user_id=TEST_USER_ID)

    # Start a session first
    start_config = StartPTYSessionConfig(command="python")
    start_result = await start_pty_session(config=start_config, ctx=ctx)
    assert start_result.success is True
    session_id = start_result.session_id

    # Send input
    input_config = SendPTYInputConfig(session_id=session_id, input_data="print('hello')\n")
    result = await send_pty_input(config=input_config, ctx=ctx)

    assert isinstance(result, SendPTYInputOutput)
    print(f"\n[send_input] success={result.success} | message={result.message}")
    assert result.success is True

    pty_session_manager.remove_session(session_id)


@pytest.mark.asyncio
async def test_send_pty_input_session_not_found(set_test_settings, pty_connection):
    """Sending input to a non-existent session returns failure."""
    ctx = Context(user_id=TEST_USER_ID)
    config = SendPTYInputConfig(session_id="nonexistent-session-id", input_data="test\n")

    result = await send_pty_input(config=config, ctx=ctx)

    assert isinstance(result, SendPTYInputOutput)
    print(f"\n[send_not_found] success={result.success} | message={result.message}")
    assert result.success is False
    assert "not found" in result.message.lower()


@pytest.mark.asyncio
async def test_send_pty_input_to_exited_session(set_test_settings, pty_connection):
    """Sending input to an exited session returns failure."""
    ctx = Context(user_id=TEST_USER_ID)

    # Create and immediately mark as exited
    session_id = pty_session_manager.create_session(TEST_USER_ID, "python")
    pty_session_manager.mark_started(session_id)
    pty_session_manager.mark_exited(session_id, 0)

    config = SendPTYInputConfig(session_id=session_id, input_data="test\n")
    result = await send_pty_input(config=config, ctx=ctx)

    assert isinstance(result, SendPTYInputOutput)
    print(f"\n[send_exited] success={result.success} | message={result.message}")
    assert result.success is False
    assert "exited" in result.message.lower()

    pty_session_manager.remove_session(session_id)


# ===========================================================================
# 3. READ PTY OUTPUT
# ===========================================================================

@pytest.mark.asyncio
async def test_read_pty_output_success(set_test_settings, pty_connection):
    """Reading output from a session with buffered data works."""
    ctx = Context(user_id=TEST_USER_ID)

    # Start session
    start_config = StartPTYSessionConfig(command="python")
    start_result = await start_pty_session(config=start_config, ctx=ctx)
    assert start_result.success is True
    session_id = start_result.session_id

    # Read the initial prompt output
    read_config = ReadPTYOutputConfig(session_id=session_id, timeout_sec=2.0)
    result = await read_pty_output(config=read_config, ctx=ctx)

    assert isinstance(result, ReadPTYOutputOutput)
    print(f"\n[read] success={result.success} | output={repr(result.output)}")
    assert result.success is True
    assert result.session_status == "running"
    assert ">>>" in result.output

    pty_session_manager.remove_session(session_id)


@pytest.mark.asyncio
async def test_read_pty_output_session_not_found(set_test_settings, pty_connection):
    """Reading from a non-existent session returns failure."""
    ctx = Context(user_id=TEST_USER_ID)
    config = ReadPTYOutputConfig(session_id="nonexistent-session-id", timeout_sec=1.0)

    result = await read_pty_output(config=config, ctx=ctx)

    assert isinstance(result, ReadPTYOutputOutput)
    print(f"\n[read_not_found] success={result.success} | status={result.session_status}")
    assert result.success is False
    assert result.session_status == "not_found"


@pytest.mark.asyncio
async def test_read_pty_output_timeout_empty(set_test_settings, pty_connection):
    """Reading from a session with no output times out and returns (no output)."""
    ctx = Context(user_id=TEST_USER_ID)

    # Create session manually without output
    session_id = pty_session_manager.create_session(TEST_USER_ID, "python")
    pty_session_manager.mark_started(session_id)

    config = ReadPTYOutputConfig(session_id=session_id, timeout_sec=0.5)
    result = await read_pty_output(config=config, ctx=ctx)

    assert isinstance(result, ReadPTYOutputOutput)
    print(f"\n[read_timeout] success={result.success} | output={repr(result.output)}")
    assert result.success is True
    assert result.output == "(no output)"

    pty_session_manager.remove_session(session_id)


@pytest.mark.asyncio
async def test_read_pty_output_includes_exit_code(set_test_settings, pty_connection):
    """After session exits, read_pty_output reports exit code."""
    ctx = Context(user_id=TEST_USER_ID)

    session_id = pty_session_manager.create_session(TEST_USER_ID, "python")
    pty_session_manager.mark_started(session_id)
    pty_session_manager.enqueue_output(session_id, "final output\n")
    pty_session_manager.mark_exited(session_id, 42)

    config = ReadPTYOutputConfig(session_id=session_id, timeout_sec=1.0)
    result = await read_pty_output(config=config, ctx=ctx)

    assert isinstance(result, ReadPTYOutputOutput)
    print(
        f"\n[read_exited] success={result.success} | status={result.session_status}"
        f" | exit_code={result.exit_code} | output={repr(result.output)}"
    )
    assert result.success is True
    assert result.session_status == "exited"
    assert result.exit_code == 42
    assert "final output" in result.output

    pty_session_manager.remove_session(session_id)


# ===========================================================================
# 4. CLOSE PTY SESSION
# ===========================================================================

@pytest.mark.asyncio
async def test_close_pty_session_success(set_test_settings, pty_connection):
    """Closing a running session succeeds."""
    ctx = Context(user_id=TEST_USER_ID)

    # Start session
    start_config = StartPTYSessionConfig(command="python")
    start_result = await start_pty_session(config=start_config, ctx=ctx)
    assert start_result.success is True
    session_id = start_result.session_id

    # Close session
    close_config = ClosePTYSessionConfig(session_id=session_id)
    result = await close_pty_session(config=close_config, ctx=ctx)

    assert isinstance(result, ClosePTYSessionOutput)
    print(f"\n[close] success={result.success} | message={result.message}")
    assert result.success is True

    # Verify session is removed
    assert pty_session_manager.get_session(TEST_USER_ID, session_id) is None


@pytest.mark.asyncio
async def test_close_pty_session_not_found(set_test_settings, pty_connection):
    """Closing a non-existent session returns failure."""
    ctx = Context(user_id=TEST_USER_ID)
    config = ClosePTYSessionConfig(session_id="nonexistent-session-id")

    result = await close_pty_session(config=config, ctx=ctx)

    assert isinstance(result, ClosePTYSessionOutput)
    print(f"\n[close_not_found] success={result.success} | message={result.message}")
    assert result.success is False
    assert "not found" in result.message.lower()


# ===========================================================================
# 5. SESSION MANAGER UNIT TESTS
# ===========================================================================

def test_pty_session_manager_is_singleton():
    """PTYSessionManager always returns the same instance."""
    a = PTYSessionManager()
    b = PTYSessionManager()
    assert a is b
    assert a is pty_session_manager
    print("\n[singleton] All instances are the same object: OK")


def test_create_and_list_sessions():
    """Creating sessions and listing them works correctly."""
    user = "test_list_user"
    try:
        s1 = pty_session_manager.create_session(user, "python")
        s2 = pty_session_manager.create_session(user, "bash")

        sessions = pty_session_manager.list_sessions(user)
        assert len(sessions) == 2
        session_ids = {s.session_id for s in sessions}
        assert s1 in session_ids
        assert s2 in session_ids
        print(f"\n[list] Found {len(sessions)} sessions: OK")
    finally:
        pty_session_manager.cleanup_user(user)


def test_session_status_transitions():
    """Session status transitions STARTING → RUNNING → EXITED work."""
    user = "test_status_user"
    try:
        sid = pty_session_manager.create_session(user, "python")
        info = pty_session_manager.get_session(user, sid)
        assert info.status == PTYSessionStatus.STARTING

        pty_session_manager.mark_started(sid)
        info = pty_session_manager.get_session(user, sid)
        assert info.status == PTYSessionStatus.RUNNING

        pty_session_manager.mark_exited(sid, 0)
        info = pty_session_manager.get_session(user, sid)
        assert info.status == PTYSessionStatus.EXITED
        assert info.exit_code == 0

        print("\n[status_transitions] STARTING → RUNNING → EXITED: OK")
    finally:
        pty_session_manager.cleanup_user(user)


def test_session_error_status():
    """Session can transition to ERROR status."""
    user = "test_error_user"
    try:
        sid = pty_session_manager.create_session(user, "python")
        pty_session_manager.mark_error(sid, "Something went wrong")

        info = pty_session_manager.get_session(user, sid)
        assert info.status == PTYSessionStatus.ERROR
        assert info.error == "Something went wrong"
        print("\n[error_status] ERROR status set: OK")
    finally:
        pty_session_manager.cleanup_user(user)


def test_cleanup_user_removes_all():
    """cleanup_user removes all sessions and queues for a user."""
    user = "test_cleanup_user"
    pty_session_manager.create_session(user, "python")
    pty_session_manager.create_session(user, "bash")

    removed = pty_session_manager.cleanup_user(user)
    assert len(removed) == 2
    assert pty_session_manager.list_sessions(user) == []
    print(f"\n[cleanup] Removed {len(removed)} sessions: OK")


@pytest.mark.asyncio
async def test_drain_output_with_data():
    """drain_output returns buffered data."""
    user = "test_drain_user"
    try:
        sid = pty_session_manager.create_session(user, "python")
        pty_session_manager.enqueue_output(sid, "chunk1")
        pty_session_manager.enqueue_output(sid, "chunk2")

        result = await pty_session_manager.drain_output(sid, max_chars=1000, timeout_sec=1.0)
        assert result == "chunk1chunk2"
        print(f"\n[drain] output={repr(result)}: OK")
    finally:
        pty_session_manager.cleanup_user(user)


@pytest.mark.asyncio
async def test_drain_output_respects_max_chars():
    """drain_output truncates at max_chars."""
    user = "test_drain_max_user"
    try:
        sid = pty_session_manager.create_session(user, "python")
        pty_session_manager.enqueue_output(sid, "a" * 100)

        result = await pty_session_manager.drain_output(sid, max_chars=10, timeout_sec=1.0)
        assert len(result) == 10
        print(f"\n[drain_max] truncated to {len(result)} chars: OK")
    finally:
        pty_session_manager.cleanup_user(user)


@pytest.mark.asyncio
async def test_drain_output_timeout_on_empty():
    """drain_output returns empty string when queue is empty and times out."""
    user = "test_drain_empty_user"
    try:
        sid = pty_session_manager.create_session(user, "python")

        result = await pty_session_manager.drain_output(sid, max_chars=1000, timeout_sec=0.3)
        assert result == ""
        print("\n[drain_empty] Timed out with empty result: OK")
    finally:
        pty_session_manager.cleanup_user(user)


# ===========================================================================
# 6. FULL WORKFLOW (start → input → read → close)
# ===========================================================================

@pytest.mark.asyncio
async def test_full_pty_workflow(set_test_settings, pty_connection):
    """Full workflow: start → send input → read output → close."""
    ctx = Context(user_id=TEST_USER_ID)

    # 1. Start
    start_config = StartPTYSessionConfig(command="python")
    start_result = await start_pty_session(config=start_config, ctx=ctx)
    assert start_result.success is True
    session_id = start_result.session_id
    print(f"\n[workflow] Started session: {session_id[:8]}")

    # 2. Read initial prompt
    read_config = ReadPTYOutputConfig(session_id=session_id, timeout_sec=2.0)
    read_result = await read_pty_output(config=read_config, ctx=ctx)
    assert read_result.success is True
    print(f"[workflow] Initial output: {repr(read_result.output)}")

    # 3. Send input
    input_config = SendPTYInputConfig(session_id=session_id, input_data="print('hello')\n")
    input_result = await send_pty_input(config=input_config, ctx=ctx)
    assert input_result.success is True
    print("[workflow] Input sent")

    # 4. Read response
    await asyncio.sleep(0.2)  # Let fake WS process
    read_result2 = await read_pty_output(config=read_config, ctx=ctx)
    assert read_result2.success is True
    print(f"[workflow] Response output: {repr(read_result2.output)}")

    # 5. Close
    close_config = ClosePTYSessionConfig(session_id=session_id)
    close_result = await close_pty_session(config=close_config, ctx=ctx)
    assert close_result.success is True
    print("[workflow] Session closed")

    # 6. Verify closed
    assert pty_session_manager.get_session(TEST_USER_ID, session_id) is None
    print("[workflow] Full workflow completed successfully")


# ===========================================================================
# 7. MULTI-USER ISOLATION
# ===========================================================================

@pytest.mark.asyncio
async def test_multi_user_session_isolation(set_test_settings):
    """Sessions from different users are isolated."""
    user_a = "pty_user_a"
    user_b = "pty_user_b"

    ws_a = FakePTYWebSocket(user_a)
    ws_b = FakePTYWebSocket(user_b)
    await terminal_manager.connect(user_a, ws_a)
    await terminal_manager.connect(user_b, ws_b)

    try:
        ctx_a = Context(user_id=user_a)
        ctx_b = Context(user_id=user_b)

        # Start sessions for both users
        start_a = await start_pty_session(
            config=StartPTYSessionConfig(command="python"), ctx=ctx_a
        )
        start_b = await start_pty_session(
            config=StartPTYSessionConfig(command="bash"), ctx=ctx_b
        )

        assert start_a.success is True
        assert start_b.success is True

        # User A can't see user B's session
        assert pty_session_manager.get_session(user_a, start_b.session_id) is None
        assert pty_session_manager.get_session(user_b, start_a.session_id) is None

        # Each user sees only their own sessions
        assert len(pty_session_manager.list_sessions(user_a)) == 1
        assert len(pty_session_manager.list_sessions(user_b)) == 1

        print("\n[multi_user] Sessions are properly isolated: OK")
    finally:
        pty_session_manager.cleanup_user(user_a)
        pty_session_manager.cleanup_user(user_b)
        await terminal_manager.disconnect(user_a)
        await terminal_manager.disconnect(user_b)


# ===========================================================================
# 8. CONFIG VALIDATION
# ===========================================================================

def test_start_config_defaults():
    """StartPTYSessionConfig has correct defaults."""
    config = StartPTYSessionConfig(command="python")
    assert config.cols == 120
    assert config.rows == 40


def test_start_config_custom_size():
    """Custom cols/rows are accepted."""
    config = StartPTYSessionConfig(command="python", cols=200, rows=50)
    assert config.cols == 200
    assert config.rows == 50


def test_read_config_defaults():
    """ReadPTYOutputConfig has correct defaults."""
    config = ReadPTYOutputConfig(session_id="test")
    assert config.max_chars == 10000
    assert config.timeout_sec == 5.0


def test_read_config_bounds():
    """ReadPTYOutputConfig enforces bounds."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ReadPTYOutputConfig(session_id="test", max_chars=0)

    with pytest.raises(ValidationError):
        ReadPTYOutputConfig(session_id="test", timeout_sec=0.0)

    with pytest.raises(ValidationError):
        ReadPTYOutputConfig(session_id="test", timeout_sec=31.0)


# ===========================================================================
# 9. REAL PTY TESTS (actual process execution)
# ===========================================================================

REAL_PTY_USER = "test_real_pty_user"


@pytest_asyncio.fixture
async def real_pty_connection():
    """Register a RealPTYWebSocket that spawns actual PTY processes."""
    ws = RealPTYWebSocket(REAL_PTY_USER)
    await terminal_manager.connect(REAL_PTY_USER, ws)
    yield ws
    await ws.cleanup_all()
    pty_session_manager.cleanup_user(REAL_PTY_USER)
    await terminal_manager.disconnect(REAL_PTY_USER)


@pytest.mark.asyncio
async def test_real_pty_bash_echo(set_test_settings, real_pty_connection):
    """Start a real bash PTY, run echo, read output."""
    ctx = Context(user_id=REAL_PTY_USER)

    # Start bash
    start_result = await start_pty_session(
        config=StartPTYSessionConfig(command="bash --norc --noprofile"), ctx=ctx
    )
    assert start_result.success is True
    session_id = start_result.session_id
    print(f"\n[real_bash] Started: {session_id[:8]}")

    # Wait for bash prompt
    await asyncio.sleep(0.5)
    read_config = ReadPTYOutputConfig(session_id=session_id, timeout_sec=3.0)
    prompt = await read_pty_output(config=read_config, ctx=ctx)
    print(f"[real_bash] Initial: {repr(prompt.output[:100])}")

    # Send echo command
    await send_pty_input(
        config=SendPTYInputConfig(session_id=session_id, input_data="echo hello_from_pty\n"),
        ctx=ctx,
    )
    await asyncio.sleep(0.5)
    result = await read_pty_output(config=read_config, ctx=ctx)
    print(f"[real_bash] Echo output: {repr(result.output[:200])}")
    assert result.success is True
    assert "hello_from_pty" in result.output

    # Close
    await close_pty_session(config=ClosePTYSessionConfig(session_id=session_id), ctx=ctx)
    print("[real_bash] Done")


@pytest.mark.asyncio
async def test_real_pty_python_repl(set_test_settings, real_pty_connection):
    """Start a real Python REPL, execute two expressions, read output."""
    ctx = Context(user_id=REAL_PTY_USER)

    start_result = await start_pty_session(
        config=StartPTYSessionConfig(command="python3 -u"), ctx=ctx
    )
    assert start_result.success is True
    session_id = start_result.session_id
    print(f"\n[real_python] Started: {session_id[:8]}")

    await asyncio.sleep(1.0)
    read_config = ReadPTYOutputConfig(session_id=session_id, timeout_sec=3.0)

    # Drain initial prompt
    await read_pty_output(config=read_config, ctx=ctx)

    # Task 1: arithmetic
    await send_pty_input(
        config=SendPTYInputConfig(session_id=session_id, input_data="print(2 + 3)\n"),
        ctx=ctx,
    )
    await asyncio.sleep(0.5)
    r1 = await read_pty_output(config=read_config, ctx=ctx)
    print(f"[real_python] Task 1 (2+3): {repr(r1.output[:200])}")
    assert r1.success is True
    assert "5" in r1.output

    # Task 2: string manipulation
    await send_pty_input(
        config=SendPTYInputConfig(
            session_id=session_id, input_data="print('hello world'.upper())\n"
        ),
        ctx=ctx,
    )
    await asyncio.sleep(0.5)
    r2 = await read_pty_output(config=read_config, ctx=ctx)
    print(f"[real_python] Task 2 (upper): {repr(r2.output[:200])}")
    assert r2.success is True
    assert "HELLO WORLD" in r2.output

    await close_pty_session(config=ClosePTYSessionConfig(session_id=session_id), ctx=ctx)
    print("[real_python] Done")


# ===========================================================================
# 10. CLAUDE CLI PTY TESTS
#
# Claude CLI's interactive TUI mode is fundamentally incompatible with
# programmatic PTY control for two reasons:
#   1. Its tokio runtime panics on "Bad file descriptor" during PTY fork/exec
#   2. Its TUI output is ANSI escape sequences, not parseable plain text
#
# Instead we start a bash PTY and run `claude -p` (pipe mode) commands
# through it. This tests the full PTY plumbing (start → input → read →
# input → read → close) with real Claude responses.
# ===========================================================================

# Sentinel echoed before each `claude -p` call so we can detect when the
# command has finished and bash has returned a prompt.
_DONE_SENTINEL = "___CLAUDE_TASK_DONE___"


async def _send_claude_cmd(
    ctx: Context,
    session_id: str,
    prompt: str,
    timeout_total: float = 60.0,
) -> str:
    """
    Helper: send a `claude -p '<prompt>'` command inside the bash PTY,
    wait for the sentinel output, and return accumulated output.

    The sentinel appears TWICE in the PTY stream:
      1. In the command echo (bash showing what was typed)
      2. As actual output of the `echo` command (after claude -p finishes)
    We wait for the 2nd occurrence so Claude's full response is captured.
    """
    escaped = prompt.replace("'", "'\\''")
    cmd = f"claude -p '{escaped}'; echo {_DONE_SENTINEL}\n"

    await send_pty_input(
        config=SendPTYInputConfig(session_id=session_id, input_data=cmd),
        ctx=ctx,
    )

    accumulated = ""
    elapsed = 0.0
    poll_sec = 3.0
    while elapsed < timeout_total:
        result = await read_pty_output(
            config=ReadPTYOutputConfig(
                session_id=session_id, max_chars=100000, timeout_sec=poll_sec,
            ),
            ctx=ctx,
        )
        if result.output and result.output != "(no output)":
            accumulated += result.output
        # Wait for 2nd occurrence: 1st is command echo, 2nd is actual output
        if accumulated.count(_DONE_SENTINEL) >= 2:
            break
        if result.session_status in ("exited", "error"):
            break
        elapsed += poll_sec

    return accumulated


@pytest.mark.asyncio
async def test_claude_cli_pty_multi_task_math_and_facts(set_test_settings, real_pty_connection):
    """
    Open a bash PTY, run 2 separate claude -p tasks (math + factual),
    verify each response.
    """
    ctx = Context(user_id=REAL_PTY_USER)

    # Start bash
    start_result = await start_pty_session(
        config=StartPTYSessionConfig(command="bash --norc --noprofile"), ctx=ctx
    )
    assert start_result.success is True, f"Failed to start bash: {start_result}"
    session_id = start_result.session_id
    print(f"\n[claude_multi] Started bash PTY: {session_id[:8]}")

    # Wait for bash prompt
    await asyncio.sleep(0.5)
    await read_pty_output(
        config=ReadPTYOutputConfig(session_id=session_id, timeout_sec=2.0), ctx=ctx,
    )

    # --- Task 1: Simple math ---
    print("[claude_multi] Task 1: What is 15 * 7?")
    t1 = await _send_claude_cmd(
        ctx, session_id,
        "What is 15 * 7? Reply with just the number, nothing else.",
    )
    print(f"[claude_multi] Task 1 output:\n{t1[:500]}")
    assert "105" in t1, f"Expected '105' in output, got: {t1[:500]}"

    # --- Task 2: Factual question ---
    print("[claude_multi] Task 2: Capital of Japan?")
    t2 = await _send_claude_cmd(
        ctx, session_id,
        "What is the capital of Japan? Reply with just the city name.",
    )
    print(f"[claude_multi] Task 2 output:\n{t2[:500]}")
    assert "tokyo" in t2.lower(), f"Expected 'tokyo' in output, got: {t2[:500]}"

    await close_pty_session(config=ClosePTYSessionConfig(session_id=session_id), ctx=ctx)
    await real_pty_connection.cleanup_all()
    print("[claude_multi] Both tasks passed!")


@pytest.mark.asyncio
async def test_claude_cli_pty_code_and_explanation(set_test_settings, real_pty_connection):
    """
    Open a bash PTY, run 2 claude -p tasks:
    1. Write a Python function
    2. Explain what a regex matches
    """
    ctx = Context(user_id=REAL_PTY_USER)

    start_result = await start_pty_session(
        config=StartPTYSessionConfig(command="bash --norc --noprofile"), ctx=ctx
    )
    assert start_result.success is True
    session_id = start_result.session_id
    print(f"\n[claude_code] Started bash PTY: {session_id[:8]}")

    await asyncio.sleep(0.5)
    await read_pty_output(
        config=ReadPTYOutputConfig(session_id=session_id, timeout_sec=2.0), ctx=ctx,
    )

    # --- Task 1: Code generation ---
    print("[claude_code] Task 1: write is_palindrome")
    t1 = await _send_claude_cmd(
        ctx, session_id,
        "Write a Python function called is_palindrome that checks if a string "
        "is a palindrome. Just the function, no explanation.",
    )
    print(f"[claude_code] Task 1 output:\n{t1[:600]}")
    assert "is_palindrome" in t1.lower(), (
        f"Expected 'is_palindrome' in output, got: {t1[:600]}"
    )
    assert "return" in t1.lower(), f"Expected 'return' in output, got: {t1[:600]}"

    # --- Task 2: Regex explanation ---
    print("[claude_code] Task 2: explain email regex")
    t2 = await _send_claude_cmd(
        ctx, session_id,
        "What does the regex pattern ^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\\.[a-zA-Z]{2,}$ "
        "match? Answer in one sentence.",
    )
    print(f"[claude_code] Task 2 output:\n{t2[:600]}")
    assert "email" in t2.lower(), f"Expected 'email' in output, got: {t2[:600]}"

    await close_pty_session(config=ClosePTYSessionConfig(session_id=session_id), ctx=ctx)
    await real_pty_connection.cleanup_all()
    print("[claude_code] Both tasks passed!")


@pytest.mark.asyncio
async def test_claude_cli_pty_three_rapid_questions(set_test_settings, real_pty_connection):
    """
    Open a bash PTY, run 3 claude -p tasks back to back:
    1. List the inner planets
    2. Translate to French
    3. Logic puzzle
    """
    ctx = Context(user_id=REAL_PTY_USER)

    start_result = await start_pty_session(
        config=StartPTYSessionConfig(command="bash --norc --noprofile"), ctx=ctx
    )
    assert start_result.success is True
    session_id = start_result.session_id
    print(f"\n[claude_3q] Started bash PTY: {session_id[:8]}")

    await asyncio.sleep(0.5)
    await read_pty_output(
        config=ReadPTYOutputConfig(session_id=session_id, timeout_sec=2.0), ctx=ctx,
    )

    # --- Q1: List ---
    print("[claude_3q] Q1: inner planets")
    q1 = await _send_claude_cmd(
        ctx, session_id,
        "List the 4 inner planets of our solar system, one per line.",
    )
    print(f"[claude_3q] Q1 output:\n{q1[:400]}")
    q1_lower = q1.lower()
    assert "mercury" in q1_lower, f"Expected mercury, got: {q1[:400]}"
    assert "venus" in q1_lower, f"Expected venus, got: {q1[:400]}"
    assert "earth" in q1_lower, f"Expected earth, got: {q1[:400]}"
    assert "mars" in q1_lower, f"Expected mars, got: {q1[:400]}"

    # --- Q2: Translation ---
    print("[claude_3q] Q2: translate to French")
    q2 = await _send_claude_cmd(
        ctx, session_id,
        "Translate 'good morning' to French. Reply with just the translation.",
    )
    print(f"[claude_3q] Q2 output:\n{q2[:400]}")
    assert "bonjour" in q2.lower(), f"Expected 'bonjour', got: {q2[:400]}"

    # --- Q3: Logic ---
    print("[claude_3q] Q3: logic puzzle")
    q3 = await _send_claude_cmd(
        ctx, session_id,
        "If all roses are flowers, and some flowers fade quickly, "
        "can we conclude all roses fade quickly? Answer yes or no with "
        "a one sentence explanation.",
    )
    print(f"[claude_3q] Q3 output:\n{q3[:400]}")
    assert "no" in q3.lower(), f"Expected 'no', got: {q3[:400]}"

    await close_pty_session(config=ClosePTYSessionConfig(session_id=session_id), ctx=ctx)
    await real_pty_connection.cleanup_all()
    print("[claude_3q] All 3 questions passed!")
