import asyncio

from loguru import logger
from pydantic import BaseModel, Field

from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.terminal.pty_session_manager import (
    PTYSessionStatus,
    pty_session_manager,
)
from src.autobots.terminal.terminal_connection_manager import (
    RemoteWebSocketProxy,
    terminal_manager,
)
from src.autobots.data_model.app_state import AppState

# ---------------------------------------------------------------------------
# Config / Output models
# ---------------------------------------------------------------------------

class StartPTYSessionConfig(BaseModel):
    command: str = Field(..., description="The command to launch in the PTY (e.g. 'python', 'bash')")
    cols: int = Field(120, ge=10, le=500, description="Terminal width in columns")
    rows: int = Field(40, ge=5, le=200, description="Terminal height in rows")


class StartPTYSessionOutput(BaseModel):
    session_id: str = ""
    status: str = ""
    success: bool


class SendPTYInputConfig(BaseModel):
    session_id: str = Field(..., description="The PTY session ID to send input to")
    input_data: str = Field(..., description="The keystrokes/text to send (use \\n for Enter)")


class SendPTYInputOutput(BaseModel):
    success: bool
    message: str = ""


class ReadPTYOutputConfig(BaseModel):
    session_id: str = Field(..., description="The PTY session ID to read output from")
    max_chars: int = Field(10000, ge=1, le=100000, description="Maximum characters to read")
    timeout_sec: float = Field(5.0, ge=0.1, le=30.0, description="Seconds to wait for output")


class ReadPTYOutputOutput(BaseModel):
    output: str = ""
    session_status: str = ""
    exit_code: int | None = None
    success: bool


class ClosePTYSessionConfig(BaseModel):
    session_id: str = Field(..., description="The PTY session ID to close")


class ClosePTYSessionOutput(BaseModel):
    success: bool
    message: str = ""


# ---------------------------------------------------------------------------
# Tool functions
# ---------------------------------------------------------------------------

async def start_pty_session(
    config: StartPTYSessionConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
) -> StartPTYSessionOutput | Exception:
    """
    Description
    -----------
    IMPORTANT: For persistent shell sessions (cd, env vars, running multiple
    shell commands in sequence), prefer run_terminal_command with
    session_id="new" instead. It is simpler and handles everything in one call.

    Only use start_pty_session for fully interactive programs that require
    real-time keystroke input, such as: interactive Python REPL, debuggers
    (pdb, gdb), text editors (vim, nano), or programs that require Ctrl+C /
    Ctrl+D signals. After starting, use send_pty_input to send keystrokes
    and read_pty_output to read output.

    Input
    -----
    config : dict
        {
            "command": "python",
            "cols": 120,
            "rows": 40
        }

        command : str - The interactive program to launch (e.g. python, node).
        cols : int - Terminal width (default 120).
        rows : int - Terminal height (default 40).

    Output
    ------
    StartPTYSessionOutput
        - session_id: Unique ID for this PTY session.
        - status: Current session status.
        - success: Whether the session was started successfully.
    """
    try:
        user_id = ctx.user_id
        if not await terminal_manager.is_connected(user_id):
            return StartPTYSessionOutput(
                success=False,
                status="error",
                session_id="",
            )

        ws = await terminal_manager.get_ws(user_id)
        if ws is None:
            return StartPTYSessionOutput(success=False, status="error", session_id="")

        session_id = pty_session_manager.create_session(user_id, config.command)

        # If WS is on another worker, output arrives via Redis pub/sub, not local queue
        if isinstance(ws, RemoteWebSocketProxy):
            pty_session_manager.set_remote(session_id)

        await ws.send_json({
            "type": "pty_start",
            "session_id": session_id,
            "command": config.command,
            "cols": config.cols,
            "rows": config.rows,
        })

        # Wait for pty_started confirmation (up to 10 seconds)
        # refresh=True to pick up status updates from the worker holding the WS
        for _ in range(100):
            await asyncio.sleep(0.1)
            info = await pty_session_manager.get_session(user_id, session_id, refresh=True)
            if info is None:
                break
            if info.status == PTYSessionStatus.RUNNING:
                return StartPTYSessionOutput(
                    session_id=session_id, status="running", success=True
                )
            if info.status == PTYSessionStatus.ERROR:
                pty_session_manager.remove_session(session_id)
                return StartPTYSessionOutput(
                    session_id=session_id, status="error", success=False
                )

        # Timeout waiting for confirmation
        pty_session_manager.remove_session(session_id)
        return StartPTYSessionOutput(
            session_id=session_id, status="timeout", success=False
        )

    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Error in start_pty_session: {e}")
        return e


async def send_pty_input(
    config: SendPTYInputConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
) -> SendPTYInputOutput | Exception:
    """
    Description
    -----------
    Sends keystrokes or text to a running PTY session started with
    start_pty_session. Use \\n for Enter key. Only needed for interactive
    programs (Python REPL, debuggers, etc.) — for shell commands, use
    run_terminal_command with session_id instead.

    Input
    -----
    config : dict
        {
            "session_id": "<uuid>",
            "input_data": "print('hello')\\n"
        }

        session_id : str - The PTY session ID.
        input_data : str - Text to send (\\n = Enter).

    Output
    ------
    SendPTYInputOutput
        - success: Whether the input was sent.
        - message: Status message.
    """
    try:
        user_id = ctx.user_id
        info = await pty_session_manager.get_session(user_id, config.session_id)
        if info is None:
            return SendPTYInputOutput(success=False, message="Session not found")

        if info.status not in (PTYSessionStatus.RUNNING, PTYSessionStatus.STARTING):
            return SendPTYInputOutput(
                success=False,
                message=f"Session is {info.status.value}, cannot send input",
            )

        ws = await terminal_manager.get_ws(user_id)
        if ws is None:
            return SendPTYInputOutput(success=False, message="CLI agent not connected")

        await ws.send_json({
            "type": "pty_input",
            "session_id": config.session_id,
            "data": config.input_data,
        })

        return SendPTYInputOutput(success=True, message="Input sent")

    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Error in send_pty_input: {e}")
        return e


async def read_pty_output(
    config: ReadPTYOutputConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
) -> ReadPTYOutputOutput | Exception:
    """
    Description
    -----------
    Reads accumulated output from a PTY session started with
    start_pty_session. Only needed for interactive programs (Python REPL,
    debuggers, etc.) — for shell commands, use run_terminal_command with
    session_id instead, which returns output automatically.

    Input
    -----
    config : dict
        {
            "session_id": "<uuid>",
            "max_chars": 10000,
            "timeout_sec": 5.0
        }

        session_id : str - The PTY session ID.
        max_chars : int - Max characters to return (default 10000).
        timeout_sec : float - Seconds to wait if no output yet (default 5.0).

    Output
    ------
    ReadPTYOutputOutput
        - output: The accumulated PTY output text.
        - session_status: Current session status.
        - exit_code: Exit code if session has exited, else None.
        - success: True if read succeeded.
    """
    try:
        user_id = ctx.user_id
        info = await pty_session_manager.get_session(user_id, config.session_id)
        if info is None:
            return ReadPTYOutputOutput(success=False, session_status="not_found")

        output = await pty_session_manager.drain_output(
            config.session_id,
            max_chars=config.max_chars,
            timeout_sec=config.timeout_sec,
        )

        # Refresh info after drain (status may have changed)
        info = await pty_session_manager.get_session(user_id, config.session_id)
        status = info.status.value if info else "removed"
        exit_code = info.exit_code if info else None

        return ReadPTYOutputOutput(
            output=output if output else "(no output)",
            session_status=status,
            exit_code=exit_code,
            success=True,
        )

    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Error in read_pty_output: {e}")
        return e


async def close_pty_session(
    config: ClosePTYSessionConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
) -> ClosePTYSessionOutput | Exception:
    """
    Description
    -----------
    Closes a PTY session started with start_pty_session and cleans up
    resources. Sends a kill signal to the CLI agent to terminate the PTY
    process.

    Input
    -----
    config : dict
        {
            "session_id": "<uuid>"
        }

        session_id : str - The PTY session ID to close.

    Output
    ------
    ClosePTYSessionOutput
        - success: Whether the session was closed.
        - message: Status message.
    """
    try:
        user_id = ctx.user_id
        info = await pty_session_manager.get_session(user_id, config.session_id)
        if info is None:
            return ClosePTYSessionOutput(success=False, message="Session not found")

        ws = await terminal_manager.get_ws(user_id)
        if ws is not None:
            try:
                await ws.send_json({
                    "type": "pty_close",
                    "session_id": config.session_id,
                })
            except Exception:
                pass  # Best-effort close signal

        pty_session_manager.remove_session(config.session_id)
        return ClosePTYSessionOutput(success=True, message="Session closed")

    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Error in close_pty_session: {e}")
        return e
