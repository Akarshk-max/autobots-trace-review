import asyncio
import re

from loguru import logger
from pydantic import BaseModel, Field, model_validator

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

_SENTINEL = "__CMD_DONE__"
_EXIT_PATTERN = re.compile(rf"{_SENTINEL}(\d+){_SENTINEL}")


class TerminalCommandConfig(BaseModel):
    command: str = Field(
        ...,
        description=(
            "The shell command to execute. IMPORTANT: Must be a single-line command. "
            "Do NOT use heredocs (<<EOF), here-strings (@'), multi-line strings, "
            "or unclosed quotes — these will break the session and cause a timeout. "
            "For multi-line content, use 'echo ... > file' or 'printf'.\n\n"
            "COMMANDS THAT CAUSE TIMEOUTS (avoid these in sessions):\n"
            "- Heredocs: cat <<EOF ... EOF\n"
            "- Here-strings: cat <<<'text'\n"
            "- Unclosed quotes: echo \"hello (missing closing quote)\n"
            "- Multi-line strings: echo 'line1\\nline2' with literal newlines\n"
            "- Interactive commands that wait for stdin: python (without -c), "
            "node (without -e), cat (without a file), read, vim, nano, less, more\n"
            "- Commands that prompt for confirmation without -y or --yes: "
            "rm -i, apt upgrade (use apt upgrade -y), pip install (already non-interactive)\n"
            "- Long-running blocking commands without timeout: ping (use ping -c N), "
            "tail -f (use timeout N tail -f), watch\n\n"
            "SAFE ALTERNATIVES:\n"
            "- Multi-line file content: printf 'line1\\nline2\\n' > file.txt\n"
            "- Python one-liners: python3 -c 'print(\"hello\")'\n"
            "- Node one-liners: node -e 'console.log(\"hello\")'\n"
            "- Bounded pings: ping -c 4 google.com\n"
            "- Bounded tail: timeout 10 tail -f logfile.txt\n"
            "- Non-interactive rm: rm -f file.txt (not rm -i)"
        ),
    )
    timeout_sec: int = Field(120, ge=1, le=600)
    session_id: str | None = Field(
        None,
        description=(
            'Optional PTY session ID for persistent shell state. '
            'Use "new" to create a new session. Omit for one-off commands.'
        ),
    )
    interactive: bool = Field(
        False,
        description=(
            "Set to true for commands that require user interaction "
            "(e.g., login prompts, OAuth flows, confirmation dialogs). "
            "The command will be handed to the user's terminal directly "
            "so they can see prompts and type responses. "
            "Use this for: gh auth login, npm login, gcloud auth login, ssh-keygen, etc."
        ),
    )
    restart_session: str = Field(
        "never",
        description=(
            "Whether to restart the PTY shell before executing this command. "
            "Use 'auto' to let the server restart the session only when the previous "
            "command on this session timed out (the shell may be unresponsive). "
            "Use 'never' (default) for normal operation. "
            "Only meaningful when session_id is set (persistent PTY sessions). "
            "IMPORTANT: After any command times out on a session, you MUST set "
            "restart_session='auto' on your very next command to that same session. "
            "This ensures the shell is healthy before proceeding."
        ),
    )
    
    @model_validator(mode="after")
    def _validate_restart_session(self):
        valid = {"never", "auto"}
        if self.restart_session not in valid:
            raise ValueError(
                f"restart_session must be one of {sorted(valid)}, "
                f"got {self.restart_session!r}"
            )
        return self

    @model_validator(mode="after")
    def _default_timeout_by_mode(self):
        """Default timeout differs for one-off vs. session/interactive commands.

        Defaults (only when caller did not explicitly set timeout_sec):
        - One-off, non-interactive (session_id is None and interactive=False): 30s
        - Interactive=True OR session_id is set: 120s
        """
        try:
            fields_set = getattr(self, "model_fields_set", set()) or set()
        except Exception:
            fields_set = set()
        if "timeout_sec" not in fields_set:
            self.timeout_sec = 120 if (self.interactive or self.session_id is not None) else 30
        return self

class TerminalCommandOutput(BaseModel):
    output: str
    success: bool
    session_id: str | None = None


async def _resolve_agent_for_ctx(ctx: Context) -> tuple[str | None, str | None]:
    try:
        agent_id = await terminal_manager.resolve_agent_for_consumer(ctx)
    except ValueError as e:
        return None, f"Error: {e}"

    if not agent_id:
        return (
            None,
            "Error: No unpaired terminal CLI agent connected for this chat. "
            "Start another `kiwi-runtime connect` or release an existing chat-terminal pairing.",
        )

    return agent_id, None


async def run_terminal_command(
    config: TerminalCommandConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
) -> TerminalCommandOutput | Exception:
    """
    Description
    -----------
    Executes a terminal command on the user's local machine via a connected CLI
    agent. This is the PRIMARY tool for all shell command execution. Requires
    the user to have an active CLI agent connection.

    TWO MODES:
    1. One-off (session_id=null): Run a single independent command. Each call
       starts fresh — no state carries over.
    2. Persistent session (session_id="new" or existing ID): Shell state (cd,
       env vars, aliases) persists across calls. Use this when the user needs
       to work in a specific directory, set up environment, or run a sequence
       of related commands.

    WHEN TO USE PERSISTENT SESSIONS:
    - User says "go to /some/dir and run X" → session_id="new", then reuse ID
    - User needs cd, export, or source to persist → use session_id
    - Multi-step workflows (build, test, deploy) → use session_id
    - Simple one-off commands (ls, cat, whoami) → omit session_id

    AVOID THESE — THEY CAUSE TIMEOUTS:
    Commands that break the session's shell state or block indefinitely will
    cause a timeout (up to timeout_sec). The session may auto-recover via
    Ctrl-C, but the command output is lost.

    1. Multi-line / heredoc syntax:
       BAD:  cat <<EOF\nhello\nEOF
       GOOD: printf 'hello\n' > file.txt

    2. Unclosed quotes or strings:
       BAD:  echo "hello world        (missing closing quote)
       GOOD: echo "hello world"

    3. Interactive programs that wait for stdin:
       BAD:  python, node, cat (no args), read, vim, nano, less, more
       GOOD: python3 -c 'print("hello")', node -e 'console.log("hi")'

    4. Commands that prompt for user confirmation:
       BAD:  rm -i file.txt, apt upgrade
       GOOD: rm -f file.txt, apt upgrade -y

    5. Unbounded long-running commands:
       BAD:  ping google.com, tail -f log.txt, watch df -h
       GOOD: ping -c 4 google.com, timeout 10 tail -f log.txt

    6. PowerShell here-strings (Windows):
       BAD:  @'\nsome content\n'@
       GOOD: powershell -Command "Write-Output 'content'"

    If a command does time out, the shell is automatically recovered (Ctrl-C).
    Rephrase the command as a single-line equivalent and retry.

    Input
    -----
    config : dict
        {
            "command": "ls -la",
            "timeout_sec": 120,
            "session_id": null
        }

        command : str
            The shell command to execute on the user's local machine.

        timeout_sec : int
            Maximum time in seconds to wait for the command to complete.
            Must be between 1 and 600. Defaults to 120.

        session_id : str | null
            Set to "new" to create a persistent shell session. Pass a
            previously returned session_id to reuse that session. Working
            directory, environment variables, and shell state persist across
            calls within the same session. Omit or set to null for one-off
            commands.

        restart_session : str
            Only meaningful with persistent sessions (session_id set).
            "never" (default): normal operation.
            "auto": restart the PTY shell before running this command if
            the previous command on this session timed out. Use this when
            you suspect the shell is unresponsive due to a heredoc, unclosed
            quote, or similar issue.

            IMPORTANT RULE: After any command times out on a session, you
            MUST set restart_session='auto' on your very next command to
            that same session. This ensures the shell is healthy before
            proceeding with further work.

    Output
    ------
    TerminalCommandOutput
        - output: The combined stdout/stderr output from the command
        - success: Whether the command completed successfully (exit_code == 0)
        - session_id: The session ID if using a persistent session, else null

    Examples
    --------
    One-off: {"command": "whoami", "session_id": null}
    New session: {"command": "cd /project && ls", "session_id": "new"}
    Reuse session: {"command": "make build", "session_id": "<returned-id>"}
    """
    try:
        # Persistent session mode
        if config.session_id is not None:
            return await _run_in_session(config, ctx)

        # One-off mode (interactive or normal)
        agent_id, error = await _resolve_agent_for_ctx(ctx)
        if error:
            return TerminalCommandOutput(output=error, success=False)

        msg_type = "interactive" if config.interactive else "command"
        result = await terminal_manager.send_command_to_agent(
            ctx=ctx,
            agent_id=agent_id,
            command=config.command,
            timeout_sec=config.timeout_sec,
            msg_type=msg_type,
        )

        if "error" in result:
            return TerminalCommandOutput(output=result["error"], success=False)

        if result.get("success") is False and result.get("message"):
            return TerminalCommandOutput(output=result["message"], success=False)

        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        exit_code = result.get("exit_code", -1)

        output_parts = []
        if stdout:
            output_parts.append(stdout)
        if stderr:
            output_parts.append(f"STDERR:\n{stderr}")
        output = "\n".join(output_parts) if output_parts else "(no output)"

        return TerminalCommandOutput(output=output, success=exit_code == 0)

    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Error in run_terminal_command: {str(e)}")
        return e



async def _run_in_session(
    config: TerminalCommandConfig, ctx: Context
) -> TerminalCommandOutput:
    """Execute a command in a persistent PTY shell session."""
    user_id = ctx.user_id
    try:
        consumer_id = terminal_manager.resolve_consumer_id(ctx)
    except ValueError as e:
        return TerminalCommandOutput(output=f"Error: {e}", success=False, session_id=config.session_id)
    session_id = config.session_id

    # Create new PTY session on this chat's paired agent
    if session_id == "new":
        agent_id, error = await _resolve_agent_for_ctx(ctx)
        if error:
            return TerminalCommandOutput(output=error, success=False)

        ws = await terminal_manager.get_ws(agent_id)
        if ws is None:
            return TerminalCommandOutput(
                output="Error: Assigned terminal agent is no longer connected.",
                success=False,
            )

        session_id = pty_session_manager.create_session(
            user_id=user_id,
            consumer_id=consumer_id,
            agent_id=agent_id,
            command="bash",
        )

        if isinstance(ws, RemoteWebSocketProxy):
            pty_session_manager.set_remote(session_id)

        await ws.send_json({
            "type": "pty_start",
            "session_id": session_id,
            "command": "bash",
            "cols": 200,
            "rows": 50,
        })

        for i in range(100):
            await asyncio.sleep(0.1)
            info = await pty_session_manager.get_session(session_id, refresh=True)
            if info is None:
                logger.bind(ctx=ctx).warning(
                    f"PTY session {session_id[:8]} disappeared at poll {i}"
                )
                return TerminalCommandOutput(
                    output="Error: Session disappeared during startup.",
                    success=False,
                )
            if i % 10 == 0:
                logger.bind(ctx=ctx).info(
                    f"PTY session {session_id[:8]} poll {i}: status={info.status.value}"
                )
            if info.status == PTYSessionStatus.RUNNING:
                logger.bind(ctx=ctx).info(
                    f"PTY session {session_id[:8]} is RUNNING after {i} polls"
                )
                break
            if info.status == PTYSessionStatus.ERROR:
                pty_session_manager.remove_session(session_id)
                return TerminalCommandOutput(
                    output=f"Error: Failed to start session: {info.error}",
                    success=False,
                )
        else:
            logger.bind(ctx=ctx).warning(
                f"PTY session {session_id[:8]} timed out after 100 polls, "
                f"last status={info.status.value}"
            )
            pty_session_manager.remove_session(session_id)
            return TerminalCommandOutput(
                output="Error: Session start timed out.",
                success=False,
            )

        await pty_session_manager.drain_output(session_id, max_chars=10000, timeout_sec=1.0)

    info = await pty_session_manager.get_session(session_id, refresh=True)
    if info is None:
        return TerminalCommandOutput(
            output="Error: Session not found. Use session_id=\"new\" to create one.",
            success=False,
        )

    if info.user_id != user_id:
        return TerminalCommandOutput(
            output="Error: Session does not belong to this user.",
            success=False,
        )

    if info.consumer_id != consumer_id:
        return TerminalCommandOutput(
            output="Error: Session belongs to a different chat.",
            success=False,
            session_id=session_id,
        )

    if info.status not in (PTYSessionStatus.RUNNING, PTYSessionStatus.STARTING):
        return TerminalCommandOutput(
            output=f"Error: Session is {info.status.value}.",
            success=False,
            session_id=session_id,
        )

    agent_id = info.agent_id
    if not await terminal_manager.is_agent_connected(agent_id):
        return TerminalCommandOutput(
            output="Error: The terminal agent for this PTY session is no longer connected.",
            success=False,
            session_id=session_id,
        )

    platform = info.platform
    ws = terminal_manager.get_ws(agent_id)
    if ws is None:
        return TerminalCommandOutput(
            output="Error: The terminal agent for this PTY session is no longer reachable.",
            success=False,
            session_id=session_id,
        )

    if isinstance(ws, RemoteWebSocketProxy):
        pty_session_manager.set_remote(session_id)

    should_restart = False
    if config.restart_session == "auto" and info.last_command_timed_out:
        logger.bind(ctx=ctx).info(
            f"Session {session_id[:8]} last command timed out; "
            f"restart_session='auto' → restarting PTY shell"
        )
        should_restart = True

    if should_restart:
        await ws.send_json({
            "type": "pty_restart",
            "session_id": session_id,
        })
        for _ in range(50):
            await asyncio.sleep(0.1)
            info_refreshed = await pty_session_manager.get_session(session_id, refresh=True)
            if info_refreshed and info_refreshed.status == PTYSessionStatus.RUNNING:
                break
        else:
            pty_session_manager.remove_session(session_id)
            return TerminalCommandOutput(
                output="Error: Session restart timed out — the PTY shell could not be restarted. "
                       "Try a fresh session with session_id='new'.",
                success=False,
            )
        await pty_session_manager.drain_output(
            session_id, max_chars=10000, timeout_sec=1.0
        )
        # Re-read info (platform / mode may have been updated by mark_started).
        info = await pty_session_manager.get_session(session_id, refresh=True)
        if info is None:
            return TerminalCommandOutput(
                output="Error: Session disappeared after restart.",
                success=False,
            )
        platform = info.platform

    if platform == "win32":
        wrapped_cmd = f"{config.command} & echo. & echo {_SENTINEL}%ERRORLEVEL%{_SENTINEL}\n"
    else:
        wrapped_cmd = f"{config.command}; printf '\\n'; echo {_SENTINEL}$?{_SENTINEL}\n"

    stale = await pty_session_manager.drain_output(
        session_id, max_chars=100000, timeout_sec=0.1
    )
    if stale:
        logger.bind(ctx=ctx).debug(
            f"Drained {len(stale)} stale chars before command in session {session_id[:8]}"
        )

    await ws.send_json({
        "type": "pty_input",
        "session_id": session_id,
        "data": wrapped_cmd,
        "timeout_sec": config.timeout_sec,
    })

    logger.bind(ctx=ctx).info(
        f"Sent session command: user_id={user_id}, consumer_id={consumer_id}, "
        f"agent_id={agent_id}, session={session_id[:8]}, command={config.command[:100]}"
    )

    accumulated = ""
    elapsed = 0.0
    poll_sec = min(2.0, config.timeout_sec)

    while elapsed < config.timeout_sec:
        chunk = await pty_session_manager.drain_output(
            session_id, max_chars=100000, timeout_sec=poll_sec
        )
        if chunk:
            accumulated += chunk

        sentinel_matches = list(_EXIT_PATTERN.finditer(accumulated))
        expected_matches = 1 if platform == "win32" else 2
        if len(sentinel_matches) >= expected_matches:
            break
        if len(sentinel_matches) == 1 and elapsed > 1.0:
            break

        info = await pty_session_manager.get_session(session_id)
        if info and info.status in (PTYSessionStatus.EXITED, PTYSessionStatus.ERROR):
            break

        elapsed += poll_sec

    exit_code = -1
    matches = list(_EXIT_PATTERN.finditer(accumulated))
    if matches:
        last_match = matches[-1]
        exit_code = int(last_match.group(1))
        await pty_session_manager.mark_command_timed_out(session_id, timed_out=False)

    logger.bind(ctx=ctx).debug(
        f"Session {session_id[:8]} output: "
        f"matches={len(matches)}, exit_code={exit_code}, "
        f"elapsed={elapsed:.1f}s, accumulated_len={len(accumulated)}"
    )

    if matches:
        last_line_start = accumulated.rfind("\n", 0, last_match.start())
        if last_line_start >= 0:
            accumulated = accumulated[:last_line_start]
        else:
            accumulated = ""

        if len(matches) >= 2:
            first_match = matches[0]
            first_line_end = accumulated.find("\n", first_match.end())
            if first_line_end >= 0:
                accumulated = accumulated[first_line_end + 1:]
            else:
                accumulated = ""

    clean_lines = []
    for line in accumulated.split("\n"):
        if _SENTINEL in line:
            continue
        clean_lines.append(line.rstrip("\r"))

    output = "\n".join(clean_lines).strip()
    if not output:
        output = "(no output)"

    success = exit_code == 0 if matches else False

    if not matches and elapsed >= config.timeout_sec:
        await pty_session_manager.mark_command_timed_out(session_id, timed_out=True)

        logger.bind(ctx=ctx).warning(
            f"Command timed out — attempting shell recovery for session {session_id[:8]}"
        )
        recovered = False

        if ws is not None:
            recovery_sentinel = f"echo {_SENTINEL}1{_SENTINEL}\n"

            if platform == "win32":
                await ws.send_json({
                    "type": "pty_interrupt",
                    "session_id": session_id,
                })
                await asyncio.sleep(0.5)

                await ws.send_json({
                    "type": "pty_input",
                    "session_id": session_id,
                    "data": recovery_sentinel,
                    "timeout_sec": 10,
                })

                recovery_buf = ""
                for _ in range(6):
                    chunk = await pty_session_manager.drain_output(
                        session_id, max_chars=100000, timeout_sec=1.0
                    )
                    if chunk:
                        recovery_buf += chunk
                    if _SENTINEL in recovery_buf:
                        recovered = True
                        break

                if not recovered:
                    logger.bind(ctx=ctx).warning(
                        f"Interrupt failed, restarting session {session_id[:8]}"
                    )
                    await ws.send_json({
                        "type": "pty_restart",
                        "session_id": session_id,
                    })
                    for _ in range(50):
                        await asyncio.sleep(0.1)
                        refreshed = await pty_session_manager.get_session(session_id, refresh=True)
                        if refreshed and refreshed.status == PTYSessionStatus.RUNNING:
                            recovered = True
                            break
                    await pty_session_manager.drain_output(
                        session_id, max_chars=10000, timeout_sec=1.0
                    )
            else:
                recovery_seq = "\x03\n"
                await ws.send_json({
                    "type": "pty_input",
                    "session_id": session_id,
                    "data": recovery_seq,
                    "timeout_sec": 5,
                })
                await asyncio.sleep(0.3)
                await ws.send_json({
                    "type": "pty_input",
                    "session_id": session_id,
                    "data": recovery_seq,
                    "timeout_sec": 5,
                })
                await asyncio.sleep(0.3)
                await ws.send_json({
                    "type": "pty_input",
                    "session_id": session_id,
                    "data": recovery_sentinel,
                    "timeout_sec": 5,
                })
                recovery_buf = ""
                for _ in range(6):
                    chunk = await pty_session_manager.drain_output(
                        session_id, max_chars=100000, timeout_sec=1.0
                    )
                    if chunk:
                        recovery_buf += chunk
                    if _SENTINEL in recovery_buf:
                        recovered = True
                        break

        recovery_note = "The shell has been recovered." if recovered else (
            "Recovery may have failed — consider starting a new session."
        )
        output = (
            f"Command timed out after {config.timeout_sec} seconds. "
            f"{recovery_note} "
            f"Avoid multi-line syntax like heredocs, here-strings, or unclosed quotes. "
            f"Use simple single-line commands instead.\n{output}"
        )
        success = False

    session_info = await pty_session_manager.get_session(session_id)
    restriction_note = ""
    if session_info and session_info.mode == "restricted" and session_info.allowed_dirs:
        restriction_note = (
            f"\n[Restricted mode: commands limited to {', '.join(session_info.allowed_dirs)}]"
        )

    return TerminalCommandOutput(
        output=output + restriction_note,
        success=success,
        session_id=session_id,
    )