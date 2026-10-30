# Terminal Tool System — Complete Technical Reference

This document describes the entire terminal command execution system end-to-end: how the LLM runs commands on a user's local machine, how the data flows between components, how sessions work, how multiple CLI agents are managed per user with sticky pairing, how output is captured, how restricted mode enforces path safety, and every known edge case and recovery mechanism.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture & Components](#2-architecture--components)
3. [Multi-Agent Connection Management](#3-multi-agent-connection-management)
4. [Data Flow — End to End](#4-data-flow--end-to-end)
5. [Two Execution Modes](#5-two-execution-modes)
6. [The Sentinel Pattern](#6-the-sentinel-pattern)
7. [PTY Session Lifecycle](#7-pty-session-lifecycle)
8. [WebSocket Message Protocol](#8-websocket-message-protocol)
9. [File System Operations](#9-file-system-operations)
10. [Output Collection & Sentinel Detection](#10-output-collection--sentinel-detection)
11. [The Poisoned Shell Problem & Recovery](#11-the-poisoned-shell-problem--recovery)
12. [Restricted Mode & Path Validation](#12-restricted-mode--path-validation)
13. [Cross-Worker Communication (Redis)](#13-cross-worker-communication-redis)
14. [CLI Agent Implementation](#14-cli-agent-implementation)
15. [Tool Registration](#15-tool-registration)
16. [Interactive PTY Tools (Low-Level)](#16-interactive-pty-tools-low-level)
17. [Platform Differences (macOS vs Windows)](#17-platform-differences-macos-vs-windows)
18. [Key Files Reference](#18-key-files-reference)
19. [Known Edge Cases & Gotchas](#19-known-edge-cases--gotchas)

---

## 1. System Overview

The terminal tool system allows an LLM agent to execute shell commands on a user's local machine. The user runs a CLI agent (kiwi-cli) on their machine, which connects to the Autobots server via WebSocket. When the LLM needs to run a command, the server sends it through the WebSocket to the CLI agent, which executes it locally and streams the output back.

A single user can have **multiple CLI agents** connected simultaneously (e.g., different machines or different project directories). The server uses a **sticky pairing** system to ensure each chat/conversation consistently uses the same CLI agent across multiple commands.

```
+------------------+        WebSocket         +------------------+        LLM Tool Call       +-----------+
|  User's Machine  | <---------------------> |  Autobots Server  | <----------------------> |  LLM Agent |
|  (kiwi-cli)      |   pty_input / pty_output |  (FastAPI)        |   run_terminal_command   |            |
+------------------+                          +------------------+                           +-----------+
|  User's Machine  | <---------------------> |                    |
|  (kiwi-cli #2)   |   (second agent)        |                    |
+------------------+                          +------------------+
```

**Why this architecture?**
- The LLM cannot run commands directly on the user's machine (security, network isolation).
- The CLI agent acts as a trusted local executor, controlled by the user.
- The user decides what scope the LLM has (restricted vs full access).
- Multiple agents per user allow different machines/directories to be connected simultaneously.

---

## 2. Architecture & Components

### Server-Side Components

| Component | File | Role |
|-----------|------|------|
| **Terminal Tool** | `src/autobots/llm/tools/terminal_tool.py` | The LLM-facing tool. Wraps commands with sentinels, sends via WebSocket, collects output, parses exit codes. This is what the LLM calls. |
| **Read Files Tool** | `src/autobots/llm/tools/read_files_tool.py` | LLM-facing tool for reading files/folders from the user's machine. Sends paths via WebSocket, CLI agent uploads to S3, returns URLs. |
| **PTY Tools** | `src/autobots/llm/tools/pty_tools.py` | Low-level interactive PTY tools (start_pty_session, send_pty_input, read_pty_output, close_pty_session). Used for fully interactive programs like Python REPL, vim, debuggers. |
| **PTY Session Manager** | `src/autobots/terminal/pty_session_manager.py` | Singleton that tracks all PTY sessions. Stores session state (status, platform, mode, allowed_dirs), buffers output in asyncio.Queue, handles Redis-based cross-worker session sync. |
| **Terminal Connection Manager** | `src/autobots/terminal/terminal_connection_manager.py` | Singleton that manages all CLI agent connections. Maps `agent_id -> WebSocket`. Handles multi-agent per user, sticky pairing, connection lifecycle, heartbeats, request/response futures, and Redis-based cross-worker message relay. |
| **WebSocket Endpoint** | `src/autobots/api/v1/api_terminal/api_terminal.py` | The FastAPI WebSocket endpoint (`/v1/terminal/ws/connect`). Authenticates CLI agents, dispatches incoming messages (pty_output, pty_started, pty_exited, result, fs_result) to the session/connection managers. |

### Client-Side Component (CLI Agent)

| Component | File | Role |
|-----------|------|------|
| **Kiwi CLI** | `~/Documents/kiwi-cli/src/kiwi_cli/main.py` | The CLI agent distributed as a PyPI package (`pip install kiwi-ai-cli`). Entry point: `kiwi connect`. Contains PTYProcess (Unix), PipeProcess (Windows), path validator, file system operations handler, and the WebSocket message loop. This is the **only** CLI implementation. |

---

## 3. Multi-Agent Connection Management

### Overview

The server supports multiple CLI agents per user. Each agent is identified by a unique `agent_id` (UUID generated by the CLI on connect). The `TerminalConnectionManager` tracks all agents and assigns them to consumers (chats/conversations) via a sticky pairing system.

### Agent Connection Info

Each connected CLI agent has metadata stored in `AgentConnInfo`:

```python
class AgentConnInfo(BaseModel):
    agent_id: str          # UUID identifying this CLI agent
    user_id: str           # Owner of the agent
    instance_id: str       # Server worker instance holding the WebSocket
    connected_at: float    # Unix timestamp
    platform: str          # "posix", "darwin", or "win32"
    mode: str              # "restricted" or "full"
    allowed_dirs: list[str]  # Paths allowed in restricted mode
```

### Local In-Memory State

Each server worker maintains:

```python
_agent_connections: Dict[agent_id, WebSocket]       # Live WebSocket connections on this worker
_agent_info: Dict[agent_id, AgentConnInfo]           # Metadata for local agents
_user_agents: Dict[user_id, Set[agent_id]]           # Which agents belong to each user
_pending_futures: Dict[agent_id, Dict[request_id, Future]]  # Pending request/response futures
_background_tasks: Dict[agent_id, List[asyncio.Task]]       # Heartbeat + command listener tasks
```

### Redis State (Distributed)

For multi-worker coordination, agent state is also stored in Redis:

| Redis Key | Type | Purpose |
|-----------|------|---------|
| `terminal:connected_agents` | Set | All connected agent IDs across all workers |
| `terminal:user_agents:{user_id}` | Set | Agent IDs belonging to a specific user |
| `terminal:agent:{agent_id}` | String (JSON) | `AgentConnInfo` for an agent |
| `terminal:heartbeat:agent:{agent_id}` | String (with TTL=30s) | Heartbeat — refreshed every 15s |
| `terminal:available_agents:{user_id}` | Sorted Set | Unpaired agents available for claiming (scored by connect time) |
| `terminal:pair:consumer:{user_id}:{consumer_id}` | String | Maps consumer to assigned agent_id |
| `terminal:pair:agent:{agent_id}` | String | Maps agent back to `{user_id}:{consumer_id}` |

### Sticky Pairing System

Each chat/conversation is called a "consumer." The pairing system ensures a consumer consistently uses the same CLI agent across multiple commands within a conversation.

**Consumer ID Resolution** (`resolve_consumer_id()`):

Priority order: `ctx.consumer_id` > `ctx.conversation_id` > `ctx.chat_id` > `ctx.session_id`

**Agent Assignment** (`resolve_agent_for_consumer()`):

1. Check if consumer already has a paired agent (`terminal:pair:consumer:{user_id}:{consumer_id}`).
2. If yes and agent is still alive (heartbeat valid): reuse it.
3. If not: claim the oldest unpaired agent from `terminal:available_agents:{user_id}`.
4. Claiming uses a **Lua script** (`_CLAIM_OLDEST_AVAILABLE_AGENT_LUA`) for atomic assignment — prevents race conditions.
5. Validate the claimed agent is still connected before returning.
6. Auto-heals stale entries during iteration.

**Release** (`release_consumer_pairing()`):

- Unpairs consumer from agent.
- Requeues agent to the available pool if still connected.

### Connect/Disconnect Lifecycle

**Connect:**
1. Register agent with metadata in local state.
2. Persist to Redis (agent info, user set, available pool, heartbeat).
3. Spawn two background tasks:
   - `_command_listener()`: Subscribes to Redis channel for relayed commands from other workers.
   - `_heartbeat_loop()`: Refreshes heartbeat key every 15 seconds.

**Disconnect:**
1. Cancel all background tasks.
2. Remove from local dicts.
3. Release any pairing (potentially requeuing agent).
4. Clean up all Redis keys.

### Remote Agent Detection

When a tool call needs to reach an agent:
1. Check local `_agent_connections` first (fast path).
2. If not found locally, check Redis heartbeat to confirm agent exists on another worker.
3. Return either local WebSocket or `RemoteWebSocketProxy` (duck-types WebSocket.send_json via Redis pub/sub).

---

## 4. Data Flow — End to End

### One-Off Command Flow

```
LLM calls run_terminal_command(command="ls -la", session_id=null)
    |
    v
terminal_tool.py: resolve_agent_for_consumer(ctx) → agent_id
    |
    v
terminal_tool.py: send_command_to_agent(agent_id, command)
    |
    v
WebSocket message: {"type": "command", "request_id": "<trace_id>:<hex>", "command": "ls -la"}
    |
    v
CLI Agent receives message, runs command via asyncio.create_subprocess_shell()
    |
    v
CLI Agent sends result: {"type": "result", "request_id": "...", "stdout": "...", "stderr": "...", "exit_code": 0}
    |
    v
TerminalConnectionManager.resolve_response() resolves the asyncio.Future
    |
    v
terminal_tool.py formats output, returns TerminalCommandOutput to LLM
```

### Persistent Session Command Flow

```
LLM calls run_terminal_command(command="cd /project && ls", session_id="new")
    |
    v
terminal_tool.py._run_in_session():
    1. Resolves agent via resolve_agent_for_consumer(ctx)
    2. Creates session via pty_session_manager.create_session()
    3. Sends pty_start message to CLI agent
    4. Polls until session status = RUNNING (up to 10 seconds)
    5. Drains initial bash prompt output
    6. Wraps command with sentinel: "cd /project && ls; echo __CMD_DONE__$?__CMD_DONE__\n"
    7. Sends pty_input message to CLI agent
    8. Collects output until sentinel pattern is found or timeout
    9. Parses exit code from sentinel, cleans output, returns result
```

```
CLI Agent receives pty_start:
    1. Spawns PTYProcess (Unix) or PipeProcess (Windows) running bash/cmd.exe
    2. Sends pty_started back with platform info
    3. Starts read loop that forwards all PTY output via pty_output messages

CLI Agent receives pty_input:
    1. (Restricted mode) Validates command against allowed directories
    2. If blocked: sends error + sentinel as pty_output, skips execution
    3. If allowed: writes data to PTY stdin
    4. PTY output is captured by read loop and sent as pty_output messages
```

### Read Files Flow

```
LLM calls read_files_from_user_system(paths=["/home/user/report.pdf", "/home/user/project/"])
    |
    v
read_files_tool.py: terminal_manager.send_read_files_to_agent(agent_id, paths=[...])
    |
    v
WebSocket message: {"type": "read_files", "request_id": "<trace_id>", "paths": [...]}
    |
    v
CLI Agent receives message:
    1. (Restricted mode) Validates each path against allowed_dirs
    2. For each valid path:
       - If file: read as-is
       - If directory: zip into a temp .zip file
    3. Uploads all files via POST {http_base_url}/v1/files/public/
       (multipart form, Authorization: Bearer <token>)
    4. Collects S3 URLs from the response
    5. Cleans up temp zip files
    |
    v
CLI Agent sends: {"type": "read_files_result", "request_id": "...",
                   "urls": ["https://s3.../report.pdf", "https://s3.../project.zip"],
                   "errors": []}
    |
    v
api_terminal.py: terminal_manager.resolve_generic_response() resolves the asyncio.Future
    |
    v
read_files_tool.py returns ReadFilesOutput to LLM
```

**Key design decision:** File data never flows through the WebSocket. The CLI agent uploads directly to S3 via the server's HTTP API. This avoids WebSocket size limits and is faster for large files.

### Multi-Worker Request Flow

```
Worker A (handling LLM tool call, agent is on Worker B)
    |
    v
Publish to Redis channel: terminal:cmd:agent:{agent_id}
    +-- Subscribe to response channel: terminal:res:{request_id}
    |
    v
Worker B (_command_listener receives from Redis)
    |-- Extract request, forward to local WebSocket
    |
    v
CLI Agent on Worker B executes command, sends result
    |
    v
Worker B (_handle_relayed_message)
    |-- Publish result to Redis channel: terminal:res:{request_id}
    |
    v
Worker A (receives response from Redis subscription)
    |-- Resolves original Future, returns to LLM
```

---

## 5. Two Execution Modes

### One-Off Mode (`session_id=null`)

- Each command runs in a fresh subprocess (`asyncio.create_subprocess_shell`).
- No state persists between calls (no cd, no env vars, no aliases).
- Uses the TerminalConnectionManager's `send_command_to_agent()` request/response pattern.
- The server sends `{"type": "command", ...}` and the CLI responds with `{"type": "result", ...}`.
- Max output: 50KB per command.
- Best for simple independent commands: `whoami`, `ls -la`, `cat file.txt`.

### Persistent Session Mode (`session_id="new"` or existing ID)

- A real shell process (bash on Unix, cmd.exe on Windows) runs persistently.
- Shell state (cd, env vars, aliases, history) persists across calls.
- Uses PTY (Unix) or subprocess pipes (Windows) for the shell process.
- Commands are wrapped with a sentinel pattern to detect completion.
- The server sends `{"type": "pty_input", ...}` and reads output from the output queue.
- Best for multi-step workflows: `cd /project`, then `npm install`, then `npm test`.

**When to use which:**
- User says "go to /some/dir and run X" -> session (state persists)
- User needs `cd`, `export`, or `source` to persist -> session
- Multi-step workflows (build, test, deploy) -> session
- Simple one-off commands (ls, cat, whoami) -> one-off (simpler, faster)

---

## 6. The Sentinel Pattern

The sentinel is the mechanism used to detect when a command finishes executing inside a persistent PTY session and to capture its exit code.

### How It Works

**Sentinel string:** `__CMD_DONE__`

**Exit pattern regex:** `__CMD_DONE__(\d+)__CMD_DONE__`

**Command wrapping:**
- Unix/macOS: `{command}; echo __CMD_DONE__$?__CMD_DONE__\n`
- Windows: `{command} & echo __CMD_DONE__%ERRORLEVEL%__CMD_DONE__\n`

The semicolon (`;` on Unix) or ampersand (`&` on Windows) ensures the echo runs even if the command fails. The `$?` (Unix) or `%ERRORLEVEL%` (Windows) captures the exit code of the preceding command.

**Example:**
```
Input command:  ls -la /tmp
Wrapped (Unix): ls -la /tmp; echo __CMD_DONE__$?__CMD_DONE__\n
```

The shell executes `ls -la /tmp`, then `echo __CMD_DONE__0__CMD_DONE__` (assuming success). The server sees `__CMD_DONE__0__CMD_DONE__` in the output, extracts exit code `0`, and knows the command is done.

### Why Two Sentinel Matches on Unix

Unix PTY sessions echo the input back to the output. So when the server sends:
```
ls -la; echo __CMD_DONE__$?__CMD_DONE__
```

The output contains:
1. **Echoed command line** includes `__CMD_DONE__0__CMD_DONE__` (match 1 — `$?` is expanded by the shell before echoing)
2. **Actual echo command output** includes `__CMD_DONE__0__CMD_DONE__` (match 2)

That's why the server expects **2 matches on Unix** and **1 match on Windows** (Windows pipe sessions don't echo input back).

**Fallback:** If only 1 match is found after 1 second, it's accepted. This handles edge cases like script mode where the echo behavior differs.

### Output Cleaning

After sentinel detection, the output is cleaned by removing all lines that contain the sentinel string:
```python
for line in accumulated.split("\n"):
    if _SENTINEL in line:
        continue
    clean_lines.append(line)
```

This strips both the echoed command and the sentinel output line.

---

## 7. PTY Session Lifecycle

### Session States

```python
class PTYSessionStatus(str, Enum):
    STARTING = "starting"   # Session created, waiting for CLI to confirm
    RUNNING = "running"     # CLI confirmed PTY is up, ready for commands
    EXITED = "exited"       # Shell process exited (normal or error)
    ERROR = "error"         # Failed to start or fatal error
```

### State Transitions

```
create_session()     mark_started()        mark_exited()
    STARTING ---------> RUNNING -----------> EXITED
        |                   |
        |                   +---------------> ERROR (mark_error())
        +-----------------------------------> ERROR (mark_error())
```

### Session Creation (server-side)

1. `pty_session_manager.create_session(user_id, consumer_id, agent_id, "bash")` generates a UUID session_id and stores a `PTYSessionInfo` object.
2. An `asyncio.Queue` is created for buffering output chunks.
3. Session info is stored in Redis synchronously (other workers need to find it).
4. A `pty_start` WebSocket message is sent to the CLI agent.
5. Server polls every 100ms for up to 10 seconds, refreshing from Redis, until status becomes RUNNING.
6. Initial bash prompt output is drained (discarded).

### Session Startup (CLI-side)

1. CLI receives `pty_start` message.
2. Creates `PTYProcess` (Unix) or `PipeProcess` (Windows) with the specified shell command.
3. Spawns the shell process with the appropriate I/O mechanism.
4. Starts a background read loop that continuously reads output and sends it as `pty_output` WebSocket messages.
5. Sends `pty_started` back to the server with platform info, mode, and allowed_dirs.

### Session Data Model

```python
class PTYSessionInfo(BaseModel):
    session_id: str          # UUID
    user_id: str             # Owner
    consumer_id: str         # Chat/conversation that owns this session
    agent_id: str            # CLI agent running the session
    command: str             # Shell command (usually "bash")
    status: PTYSessionStatus # Current state
    exit_code: int | None    # Set when exited
    error: str | None        # Set on error
    created_at: float        # Unix timestamp
    platform: str            # "posix", "darwin", or "win32" (from CLI)
    mode: str                # "full" or "restricted"
    allowed_dirs: list[str]  # Paths allowed in restricted mode
```

### Session Cleanup

- When a CLI agent disconnects, `pty_session_manager.cleanup_agent(agent_id)` removes all sessions for that agent.
- Individual sessions can be removed via `remove_session(session_id)`.
- Redis keys are cleaned up (fire-and-forget).
- Sessions have a 1-hour TTL in Redis (`PTY_SESSION_TTL = 3600`).

---

## 8. WebSocket Message Protocol

All messages are JSON objects with a `type` field.

### Authentication

| Direction | Message | Description |
|-----------|---------|-------------|
| CLI -> Server | `{"type": "auth", "token": "<jwt>", "agent_id": "<uuid>", "platform": "<platform>", "mode": "<restricted\|full>", "allowed_dirs": [...]}` | First message, authenticates the CLI agent with metadata |
| Server -> CLI | `{"type": "auth_ok", "user_id": "<id>", "agent_id": "<id>"}` | Authentication successful, confirms agent_id |
| Server -> CLI | `{"type": "error", "message": "..."}` | Authentication failed |

### One-Off Commands

| Direction | Message | Description |
|-----------|---------|-------------|
| Server -> CLI | `{"type": "command", "request_id": "<id>", "command": "ls -la"}` | Execute a one-off command |
| CLI -> Server | `{"type": "result", "request_id": "<id>", "stdout": "...", "stderr": "...", "exit_code": 0}` | Command result |

### Read Files

| Direction | Message | Description |
|-----------|---------|-------------|
| Server -> CLI | `{"type": "read_files", "request_id": "<id>", "paths": ["/path/to/file", "/path/to/dir"]}` | Read files/folders from user's machine and upload to S3 |
| CLI -> Server | `{"type": "read_files_result", "request_id": "<id>", "urls": ["https://s3.../file.pdf"], "errors": []}` | S3 URLs for uploaded files + any per-path errors |

### PTY Session Management

| Direction | Message | Description |
|-----------|---------|-------------|
| Server -> CLI | `{"type": "pty_start", "session_id": "<uuid>", "command": "bash", "cols": 200, "rows": 50}` | Create a new PTY session |
| CLI -> Server | `{"type": "pty_started", "session_id": "<uuid>", "platform": "posix", "mode": "restricted", "allowed_dirs": [...]}` | PTY is up and running |
| Server -> CLI | `{"type": "pty_input", "session_id": "<uuid>", "data": "ls -la\n"}` | Send keystrokes/commands to PTY stdin |
| CLI -> Server | `{"type": "pty_output", "session_id": "<uuid>", "data": "total 4\ndrwx..."}` | Output from PTY stdout/stderr |
| Server -> CLI | `{"type": "pty_close", "session_id": "<uuid>"}` | Close/kill the PTY session |
| Server -> CLI | `{"type": "pty_interrupt", "session_id": "<uuid>"}` | Kill child processes of the shell. Uses psutil to kill children while keeping the shell alive. |
| Server -> CLI | `{"type": "pty_restart", "session_id": "<uuid>"}` | Kill and respawn the shell process in the same cwd. CLI responds with `pty_started`. |
| CLI -> Server | `{"type": "pty_exited", "session_id": "<uuid>", "exit_code": 0}` | PTY process exited |
| CLI -> Server | `{"type": "pty_error", "session_id": "<uuid>", "error": "..."}` | PTY process error |

### File System Operations

| Direction | Message | Description |
|-----------|---------|-------------|
| Server -> CLI | `{"type": "<fs_op>", "request_id": "<id>", ...}` | File system operation (see [Section 9](#9-file-system-operations)) |
| CLI -> Server | `{"type": "fs_result", "request_id": "<id>", "success": true, ...}` | File system operation result |

### Keepalive

| Direction | Message | Description |
|-----------|---------|-------------|
| Server -> CLI | `{"type": "ping"}` | Keepalive ping |
| CLI -> Server | `{"type": "pong"}` | Keepalive response |

---

## 9. File System Operations

The CLI agent supports 13 file system operations, plus chunked write support for large files. All operations go through `handle_fs_request()` on the CLI side, which validates paths against allowed directories in restricted mode.

### Supported Operations

| Operation | Description | Key Parameters |
|-----------|-------------|----------------|
| `read_file` | Read file content | `path`, optional `start_line`, `end_line`, `max_chars` |
| `write_file` | Write/overwrite/create a file | `path`, `content` |
| `append_file` | Append content to a file | `path`, `content` |
| `replace_in_file` | Find & replace text in a file | `path`, `old_text`, `new_text` |
| `stat_file` | Get file metadata | `path` — returns size, mtime, ctime, exists |
| `list_dir` | List directory contents | `path`, optional `recursive` |
| `mkdir` | Create directories | `path` |
| `move_path` | Move/rename files or directories | `source`, `destination` |
| `delete_path` | Delete files or directories | `path` |
| `replace_line_range` | Replace specific line range | `path`, `start_line`, `end_line`, `new_content` |
| `insert_at_line` | Insert content at a specific line | `path`, `line`, `content` |
| `search_in_file` | Search with regex/case sensitivity | `path`, `pattern`, optional `case_sensitive`, `regex` |
| `apply_unified_diff` | Apply a unified diff patch | `path`, `diff` |

### Chunked Write Support

For large files, the CLI supports chunked uploads:

1. **`file_write_start`** — Begins a chunked write, creates a temp file, returns `upload_id`.
2. **`file_write_chunk`** — Appends a chunk to the temp file using `upload_id`.
3. **`file_write_finish`** — Atomically moves the temp file to the target path.

Session management uses `upload_id` to track in-progress chunked writes. Temp files are cleaned up on disconnect.

### Path Validation

All file system operations validate paths against the allowed directories list (in restricted mode). If a path is outside the allowed directories, the operation is rejected with an error — the file is never accessed.

---

## 10. Output Collection & Sentinel Detection

When a command is sent to a PTY session, the server enters a polling loop to collect output and detect the sentinel.

### The Collection Loop (server-side)

```python
accumulated = ""
elapsed = 0.0
poll_sec = min(2.0, config.timeout_sec)

while elapsed < config.timeout_sec:
    # Drain output from the session's asyncio.Queue
    chunk = await pty_session_manager.drain_output(
        session_id, max_chars=100000, timeout_sec=poll_sec
    )
    if chunk:
        accumulated += chunk

    # Check for sentinel matches
    sentinel_matches = list(_EXIT_PATTERN.finditer(accumulated))
    expected_matches = 1 if info.platform == "win32" else 2
    if len(sentinel_matches) >= expected_matches:
        break
    # Fallback: single match after 1 second
    if len(sentinel_matches) == 1 and elapsed > 1.0:
        break

    # Check if session died
    info = pty_session_manager.get_session(user_id, session_id)
    if info and info.status in (EXITED, ERROR):
        break

    elapsed += poll_sec
```

### drain_output()

The `drain_output()` method reads from the session's `asyncio.Queue`:
1. If the queue is empty, waits up to `timeout_sec` for the first chunk.
2. Then drains all remaining chunks without waiting (up to `max_chars`).
3. Returns the concatenated output string.

For remote sessions (cross-worker), it subscribes to a Redis pub/sub channel instead.

### Exit Code Parsing

After the loop:
```python
matches = list(_EXIT_PATTERN.finditer(accumulated))
if matches:
    last_match = matches[-1]
    exit_code = int(last_match.group(1))  # e.g., "0" from __CMD_DONE__0__CMD_DONE__
```

The **last** match is used because the echoed command may contain a different exit code than the actual result (if $? was already set from a previous command).

---

## 11. The Poisoned Shell Problem & Recovery

### The Problem

When the LLM sends a syntactically broken or multi-line command, the sentinel wrapper gets **absorbed into the broken syntax**. The shell enters a state waiting for more input (to close the quote, heredoc, or here-string), and the sentinel never executes.

**Examples of poisoning commands:**

1. **Unclosed quote:**
   ```
   echo "hello world; echo __CMD_DONE__$?__CMD_DONE__
   ```
   The shell waits for the closing `"`. The sentinel is part of the string.

2. **Heredoc:**
   ```
   cat << 'EOF'
   content; echo __CMD_DONE__$?__CMD_DONE__
   ```
   The shell waits for `EOF` on its own line. Sentinel is heredoc content.

3. **PowerShell here-string:**
   ```
   @'
   some content
   '@ & echo __CMD_DONE__%ERRORLEVEL%__CMD_DONE__
   ```
   Everything after `@'` is here-string content. Sentinel never runs.

**Consequences of a poisoned shell:**
1. The sentinel never appears in the output.
2. The server times out after `timeout_sec` (default 120 seconds).
3. The shell is stuck in a bad state, waiting for more input.
4. Any subsequent command sent to the same session gets appended to the broken input.
5. The session is **permanently unusable** — every future command also times out.

### Prevention: LLM Guidance

The `command` field on `TerminalCommandConfig` includes an explicit description:
```
IMPORTANT: Must be a single-line command. Do NOT use heredocs (<<EOF),
here-strings (@'), multi-line strings, or unclosed quotes — these will
break the session. For multi-line content, use 'echo ... > file' or 'printf'.
```

This tells the LLM to avoid dangerous syntax. It's not foolproof but significantly reduces the risk.

### Recovery: Automatic Shell Reset

Recovery is **platform-aware** because Unix and Windows have fundamentally different interrupt mechanisms.

#### Unix/macOS Recovery

When a timeout occurs on a Unix PTY session, the server sends `\x03` (Ctrl-C) which triggers SIGINT to the foreground process group:

1. `\x03` (Ctrl-C / SIGINT) interrupts whatever the shell is doing (breaks out of unclosed quotes, heredocs, running processes).
2. `\n` (newline) clears any partial input on the command line.
3. Sent twice to handle nested states (e.g., a subshell inside a heredoc).
4. A fresh sentinel is sent to flush the shell back to a clean `$` prompt.
5. Recovery output is drained until the sentinel appears (up to 6 seconds), ensuring nothing leaks into the next command.

#### Windows Recovery (Two-Phase)

On Windows, `\x03` written to piped stdin is a **literal byte**, NOT a console interrupt signal. `PipeProcess` uses subprocess pipes (no PTY/console), so console control events don't work. Recovery uses a two-phase approach:

**Phase 1 — Interrupt (kill child processes):**
1. Server sends `{"type": "pty_interrupt", "session_id": "..."}` to the CLI.
2. CLI uses `psutil` to find child processes of `cmd.exe` (e.g., `ping.exe`) and kills them recursively.
3. `cmd.exe` itself stays alive, preserving the session's working directory and environment variables.
4. Server sends a recovery sentinel. If `cmd.exe` is now at its prompt, the sentinel executes and recovery succeeds.

**Phase 2 — Restart (fallback):**
If the sentinel doesn't appear within 6 seconds (meaning `cmd.exe` itself is stuck, e.g., `set /p` waiting for stdin):
1. Server sends `{"type": "pty_restart", "session_id": "..."}` to the CLI.
2. CLI captures `cmd.exe`'s current working directory via `psutil`, kills the old process, and spawns a new `cmd.exe` in the same directory.
3. CLI sends `pty_started` back to the server. The server picks this up via the existing `mark_started()` handler.
4. Server drains startup output from the new shell.
5. **Limitation:** Environment variables set via `set` in the session are lost. The LLM is warned about this in the timeout message.

#### Additional Safeguard: Pre-Command Drain

Before sending any command to an existing session, the server drains stale output from the queue (0.1s timeout). This catches any recovery output that arrived late due to async latency (CLI -> WebSocket -> server queue), preventing stale sentinel matches from being attributed to the new command.

**The error message returned to the LLM includes:**
```
Command timed out after 120 seconds. The shell has been recovered.
Avoid multi-line syntax like heredocs, here-strings, or unclosed quotes.
Use simple single-line commands instead.
```

If recovery failed, the message says: `Recovery may have failed — consider starting a new session.`
If the Windows session was restarted, it adds: `Note: the shell was restarted; environment variables set in this session have been lost.`

---

## 12. Restricted Mode & Path Validation

### Overview

Restricted mode limits the LLM to executing commands that only access files within a set of allowed directories. The user controls this via CLI flags:

```bash
# Restricted to current directory only (default)
kiwi connect

# Restricted to current directory + additional directories
kiwi connect --allow ~/Documents --allow ~/Projects

# Full access (no restrictions)
kiwi connect --scope full
```

### How Restriction is Enforced

Restriction is enforced at **two levels**:

1. **CLI-side (primary enforcement):** The CLI agent validates every `pty_input`, `command`, and file system operation before execution. If a command references a path outside allowed directories, it is blocked and never reaches the shell.

2. **Server-side (LLM awareness):** The server appends a `[Restricted mode: commands limited to ...]` note to command output so the LLM knows about the constraints.

### Path Validation Algorithm

The `validate_command()` function in the CLI uses a two-pass approach:

#### Pass 1: Token-Based Extraction

```python
tokens = shlex.split(command)  # Shell-aware tokenization
for token in tokens:
    path = _extract_path(token)  # Returns path if token looks like a path
```

`_extract_path(token)` identifies paths by checking:
- Starts with `/` (Unix absolute path) — skipped on Windows (could be a flag like `/d`)
- Starts with `~` (home directory path)
- Matches `C:\...` (Windows absolute path)
- Matches `\\...` (UNC path)
- Contains `..` in path components (parent directory traversal)
- Strip trailing shell metacharacters (`;`, `|`, `&`) before checking
- Strip leading redirection operators (`>`, `>>`, `<`, `2>`, etc.)
- Skip safe system paths (`/dev/null`, `/dev/stdin`, `/dev/stdout`, etc.)

#### Pass 2: Regex Scan for Embedded Paths

Token-based extraction misses paths embedded in quoted strings (e.g., `powershell -Command "Set-Location 'D:\tmp'"`). The regex scan catches these:

```python
_EMBEDDED_UNIX_PATH = re.compile(r'(?:^|[\s\'";=])(/[^\s\'";|&>]+)')
_EMBEDDED_HOME_PATH = re.compile(r'(?:^|[\s\'";=])(~[^\s\'";|&>]*)')
_EMBEDDED_WIN_PATH  = re.compile(r'(?:^|[\s\'";=])([A-Za-z]:[/\\][^\s\'";|&>]*)')
_EMBEDDED_UNC_PATH  = re.compile(r'(?:^|[\s\'";=])(\\\\[^\s\'";|&>]+)')
_EMBEDDED_DOTDOT    = re.compile(r'(?:^|[\s\'";=])(\.\.[/\\][^\s\'";|&>]*)')
```

**Important:** On Windows, `_EMBEDDED_UNIX_PATH` is skipped because `/x` tokens are command flags (e.g., `/d`, `/b`, `/ad`), not paths.

#### False Positive Prevention

Regex-scanned paths can produce false positives (e.g., `echo "hello /world"` catches `/world`). To prevent this:

```python
if from_regex and not os.path.exists(resolved):
    continue  # Skip — probably a string literal, not a real path
```

Regex-scanned paths are only flagged if they **actually exist on disk**. Token-extracted paths are always checked (they're more likely to be intentional path references).

#### Path Resolution

Extracted paths are resolved to absolute paths:
```python
def _resolve_path(path, base_dir):
    path = os.path.expanduser(path)       # ~ -> /home/user
    if not os.path.isabs(path):
        path = os.path.join(base_dir, path)  # Relative -> absolute
    return os.path.realpath(path)          # Resolve symlinks
```

**The `base_dir` is critical:** For PTY sessions, it's the shell's **actual current working directory** (obtained via `psutil.Process(pid).cwd()`), not the original allowed directory. This is because `cd subdir` changes the shell's cwd, so `..` should resolve relative to the new location.

```python
def validate_command(command, allowed_dirs, pid=None):
    base_dir = allowed_dirs[0]
    if pid is not None:
        real_cwd = _get_process_cwd(pid)
        if real_cwd:
            base_dir = real_cwd
```

#### Allowed Directory Check

```python
def _is_within_allowed(resolved_path, allowed_dirs):
    resolved = os.path.normcase(resolved_path)
    if sys.platform == "darwin":
        resolved = resolved.lower()  # macOS is case-insensitive
    for allowed in allowed_dirs:
        allowed_norm = os.path.normcase(allowed)
        if sys.platform == "darwin":
            allowed_norm = allowed_norm.lower()
        if resolved == allowed_norm or resolved.startswith(allowed_norm + os.sep):
            return True
    return False
```

**macOS case insensitivity:** macOS (HFS+/APFS) is typically case-insensitive. `/Users/aryansh/Documents` and `/Users/aryansh/documents` are the same directory. The comparison uses `.lower()` on darwin to handle this.

### Blocked Command Handling in PTY Sessions

When a command is blocked in restricted mode, the CLI does NOT send it to the shell. Instead, it fabricates a response:

```python
err_msg = (
    f"\r\n{reason}\r\n"
    f"{_SENTINEL}1{_SENTINEL}\r\n"
)
await ws.send(json.dumps({
    "type": "pty_output",
    "session_id": session_id,
    "data": err_msg,
}))
```

The **sentinel with exit code 1** is included so the server detects "command completion" and doesn't wait 120 seconds for a response that will never come.

### Safe Paths Whitelist

These paths are always allowed, even in restricted mode:

```python
_SAFE_PATHS = frozenset({
    "/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr",
    "/dev/zero", "/dev/random", "/dev/urandom", "/dev/tty",
})
```

Example: `cat file.txt > /dev/null` won't be blocked for referencing `/dev/null`.

### Full Mode

When `mode == "full"` (or `--scope full`), no path validation is performed. Commands are sent directly to the shell without any checks. The `validate_command()` function is not called.

---

## 13. Cross-Worker Communication (Redis)

The Autobots server may run multiple worker instances behind a load balancer. A CLI agent connects to one worker, but the LLM's tool call may land on a different worker. Redis bridges this gap.

### Connection Awareness

When a CLI agent connects:
1. The worker adds the `agent_id` to a Redis set (`terminal:connected_agents`).
2. Agent info is stored in `terminal:agent:{agent_id}` (JSON).
3. Agent is added to `terminal:user_agents:{user_id}` set.
4. Agent is added to `terminal:available_agents:{user_id}` sorted set (scored by connect time).
5. A heartbeat key (`terminal:heartbeat:agent:{agent_id}`) is set with TTL=30s.
6. A background task refreshes the heartbeat every 15 seconds.

When another worker needs to reach an agent:
1. Check local connections first (fast path).
2. Check Redis heartbeat to confirm the agent is alive.
3. Use `RemoteWebSocketProxy` to relay messages via Redis pub/sub.

### RemoteWebSocketProxy

When the LLM's tool call lands on a worker that doesn't hold the WebSocket:

```python
class RemoteWebSocketProxy:
    """Duck-types WebSocket.send_json() by publishing to Redis instead."""

    async def send_json(self, data):
        channel = f"terminal:cmd:agent:{self.agent_id}"
        payload = json.dumps({"type": "relay_ws_send", "data": data, "instance_id": _INSTANCE_ID})
        await _redis_safe(get_redis().publish, channel, payload)
```

The worker holding the actual WebSocket has a background `_command_listener` task subscribed to `terminal:cmd:agent:{agent_id}`. When it receives a message, it forwards `data` to the local WebSocket.

### Relayed Message Types

The relay pattern supports these message types:

- `relay_request` — Forwards a command request to a local agent, collects the response, and publishes it back to the response channel (`terminal:res:{request_id}`).
- `relay_ws_send` — Forwards raw WebSocket messages (used by PTY tools via `RemoteWebSocketProxy`).

### Request/Response Pattern

**Local request** (`_send_ws_request_local()`):
1. Create `asyncio.Future`, store in `_pending_futures[agent_id][request_id]`.
2. Send JSON via WebSocket.
3. Wait for response with timeout (default 120s).
4. Agent response resolves the Future via `resolve_response()`.

**Remote request** (`_send_ws_request_remote()`):
1. Publish to Redis channel: `terminal:cmd:agent:{agent_id}`.
2. Subscribe to response channel: `terminal:res:{request_id}`.
3. Remote worker's `_command_listener()` receives, forwards to local agent.
4. Agent response published back to response channel.
5. Original worker receives and resolves.

**Request ID format:** `{trace_id}:{random_hex}` for uniqueness across retries.

### PTY Output Relay

PTY output is relayed via Redis pub/sub channel `terminal:pty_out:{session_id}`:
1. CLI agent sends `pty_output` to the WebSocket endpoint.
2. The endpoint calls `pty_session_manager.enqueue_output()` (local queue) AND `_publish_pty_event_fire_and_forget()` (Redis publish).
3. The requesting worker calls `pty_session_manager.set_remote(session_id)` which starts a background Redis subscriber.
4. The subscriber feeds Redis data into the local `asyncio.Queue`.
5. `drain_output()` reads from the local queue as usual.

### Session Info Sync

Session info is stored in Redis under `terminal:pty_session:{session_id}` with 1-hour TTL. Critical writes (create, mark_started) are synchronous. Status updates are asynchronous (fire-and-forget). Any worker can read session info from Redis using `get_session(refresh=True)`.

---

## 14. CLI Agent Implementation

The CLI agent is distributed as the **kiwi-cli** PyPI package (`pip install kiwi-ai-cli`), with the entry point `kiwi connect`. The source lives in a **separate repository** at `~/Documents/kiwi-cli/`.

### CLI Usage

```bash
kiwi connect [--server SERVER] [--email EMAIL] [--token TOKEN]
             [--scope {restricted,full}] [--allow PATH]...
```

### Server Presets

```python
SERVER_PRESETS = {
    "app": "wss://api.runkai.ai",       # Production
    "dev": "wss://dev.api.myautobots.com", # Development
    "local": "ws://localhost:8000",         # Local testing
}
```

Both `ws` and `http` URLs are derived — the CLI uses WebSocket for command communication and HTTP for file uploads and authentication.

### Authentication

- HTTP POST to `{http_base_url}/v1/auth/token` with `{"username": email, "password": password}`.
- Returns JWT `access_token`.
- Can be bypassed with `--token` flag or `KIWI_AUTH_TOKEN` environment variable.

### PTYProcess (Unix/macOS)

Uses actual pseudo-terminals:
1. `pty.openpty()` creates a master/slave fd pair.
2. The shell process is spawned with stdin/stdout/stderr connected to the slave fd, with `preexec_fn=os.setsid` (new process group).
3. The master fd is set to non-blocking mode via `fcntl`.
4. A read loop uses `select.select()` with 0.5s timeout to read 4KB chunks from the master fd.
5. Input is written to the master fd via `os.write()`.
6. Terminal size is set via `ioctl(TIOCSWINSZ)`.

**Key behavior:** PTY sessions **echo input back**. When you write `ls\n` to the master fd, the output includes `ls\r\n` (the echo) followed by the directory listing. This is why the server expects 2 sentinel matches on Unix.

### PipeProcess (Windows)

Uses subprocess pipes (no PTY on Windows):
1. `cmd.exe /Q /D /V:ON /K` is spawned with piped stdin/stdout, stderr merged into stdout.
2. Output is read via 4KB chunks from stdout pipe.
3. Input is written via `self.proc.stdin.write()` + `drain()`.
4. Uses `CREATE_NEW_PROCESS_GROUP` for process group management.

**Key behavior:** Pipe sessions **do NOT echo input**. Only 1 sentinel match is expected.

**Windows-specific features:**
- Sentinel wrapping: `command & echo __CMD_DONE__!ERRORLEVEL!__CMD_DONE__` (delayed expansion enabled via `/V:ON`).
- Timeout watchdog: auto-interrupts hung commands after 120s, restarts shell.
- Ctrl+Break instead of Ctrl+C for interrupts.
- Automatic shell restart on interrupt.

### Command Display Logging

When the LLM sends a command to a PTY session, the CLI strips the sentinel wrapper and displays the clean command:

```python
def _clean_cmd_for_display(data):
    cmd = data.strip()
    idx = cmd.find(_SENTINEL)
    if idx > 0:
        cmd = cmd[:idx]
        for sep in ("; echo ", "& echo "):
            if cmd.endswith(sep):
                cmd = cmd[:-len(sep)]
    return cmd.strip()
```

This shows the user: `[PTY abc12345] $ ls -la /tmp` instead of the raw wrapped command.

### Timeout & Watchdog Mechanisms

**One-off commands:** 120s default timeout. Kill process if exceeded.

**PTY sessions:**
- Windows: 120s watchdog per command (monitors sentinel output, auto-cancels).
- Timeout triggers Ctrl+Break + restart shell + notify server.
- Unix: Relies on server-side timeout and Ctrl-C recovery.

---

## 15. Tool Registration

Tools are registered in `src/autobots/action/action_type/tool_routes.py`:

```python
ToolRegistry.register("run_terminal_command", run_terminal_command)
ToolRegistry.register("read_files_from_user_system", read_files_from_user_system)
ToolRegistry.register("start_pty_session", start_pty_session)
ToolRegistry.register("send_pty_input", send_pty_input)
ToolRegistry.register("read_pty_output", read_pty_output)
ToolRegistry.register("close_pty_session", close_pty_session)
```

- `run_terminal_command` is the **primary** tool for all shell command execution.
- `read_files_from_user_system` reads files/folders from the user's machine and returns S3 URLs.
- The PTY tools (`start_pty_session`, etc.) are lower-level and should only be used for fully interactive programs.

---

## 16. Interactive PTY Tools (Low-Level)

These tools (`src/autobots/llm/tools/pty_tools.py`) provide direct PTY access for interactive programs that need real-time keystroke I/O:

| Tool | Purpose | When to Use |
|------|---------|-------------|
| `start_pty_session` | Launch an interactive program (python, node, vim) | Only for fully interactive programs |
| `send_pty_input` | Send keystrokes to the running PTY | After start_pty_session |
| `read_pty_output` | Read accumulated output from PTY | After send_pty_input |
| `close_pty_session` | Kill the PTY process and clean up | When done with the session |

**Important:** For regular shell commands, the LLM should use `run_terminal_command` with `session_id`, NOT these tools. These are only for programs that require real-time interaction (REPL, debugger, editor).

The difference:
- `run_terminal_command` with session_id: Sends command, waits for sentinel, returns clean output. One call = one command.
- PTY tools: Manual keystroke-by-keystroke interaction. The LLM must manually send input, read output, and decide when the command is done.

---

## 17. Platform Differences (macOS vs Windows)

| Feature | macOS/Linux | Windows |
|---------|-------------|---------|
| PTY implementation | `PTYProcess` (real PTY via `pty.openpty()`) | `PipeProcess` (subprocess pipes) |
| Shell | `/bin/sh -c bash` | `cmd.exe /Q /D /V:ON /K` (from `%COMSPEC%`) |
| Sentinel separator | `;` (semicolon) | `&` (ampersand) |
| Exit code variable | `$?` | `!ERRORLEVEL!` (delayed expansion) |
| Input echo | Yes (PTY echoes input) | No (pipes don't echo) |
| Expected sentinel matches | 2 | 1 |
| `/x` tokens | Treated as paths | Treated as flags (skipped) |
| Case sensitivity | Case-sensitive (Linux), case-insensitive (macOS) | Case-insensitive |
| Platform string | `"posix"` or `"darwin"` | `"win32"` |
| Interrupt mechanism | `\x03` -> SIGINT | Ctrl+Break / psutil kill children |
| Unix path regex | Applied | Skipped (false positives with `/d`, `/b`, etc.) |
| Timeout watchdog | Server-side | CLI-side (120s per command) |

### macOS Case Insensitivity

macOS uses HFS+ or APFS, both typically case-insensitive. `/Users/aryansh/Documents` and `/Users/aryansh/documents` refer to the same directory. The path validator uses `.lower()` on `sys.platform == "darwin"` to handle this correctly.

---

## 18. Key Files Reference

### Server-Side

| File | Description |
|------|-------------|
| `src/autobots/llm/tools/terminal_tool.py` | **Primary LLM tool.** `run_terminal_command()` — handles both one-off and session modes. Contains sentinel constants, command wrapping, output collection, exit code parsing, shell recovery logic. Resolves agents via `resolve_agent_for_consumer()`. |
| `src/autobots/llm/tools/read_files_tool.py` | **File reading tool.** `read_files_from_user_system()` — sends file paths to CLI agent, which uploads to S3 and returns URLs. |
| `src/autobots/llm/tools/pty_tools.py` | **Low-level PTY tools.** `start_pty_session()`, `send_pty_input()`, `read_pty_output()`, `close_pty_session()`. For interactive programs only. |
| `src/autobots/terminal/pty_session_manager.py` | **Session state manager (singleton).** Tracks sessions per agent, buffers output in asyncio.Queue, handles Redis-based cross-worker session sync. Key class: `PTYSessionManager`, model: `PTYSessionInfo` (includes `consumer_id` and `agent_id`). |
| `src/autobots/terminal/terminal_connection_manager.py` | **Connection manager (singleton).** Maps `agent_id -> WebSocket`. Handles multi-agent per user, sticky pairing (consumer <-> agent), available agent pool with Lua atomic claiming, heartbeats, request/response futures, Redis-based cross-worker relay. Key class: `TerminalConnectionManager`, model: `AgentConnInfo`, helper: `RemoteWebSocketProxy`. |
| `src/autobots/api/v1/api_terminal/api_terminal.py` | **WebSocket endpoint.** `/v1/terminal/ws/connect`. Authenticates CLI agents (with `agent_id`, `platform`, `mode`, `allowed_dirs`), routes incoming messages to session/connection managers, publishes PTY events to Redis. Debug endpoint: `GET /v1/terminal/status`. |
| `src/autobots/action/action_type/tool_routes.py` | **Tool registration.** `register_tools()` registers all 6 terminal/file tools with the ToolRegistry. |

### Client-Side

| File | Description |
|------|-------------|
| `~/Documents/kiwi-cli/src/kiwi_cli/main.py` | **The CLI agent (separate repo).** Distributed as `pip install kiwi-ai-cli`, entry point: `kiwi connect`. Contains PTYProcess (Unix), PipeProcess (Windows), path validator, 13 file system operation handlers, chunked write support, restricted mode enforcement, WebSocket message loop, file upload to S3. Auth via email/password login or `--token` / `KIWI_AUTH_TOKEN`. |

### Tests

| File | Description |
|------|-------------|
| `tests/llm/tools/test_terminal_tool.py` | Tests for terminal_tool.py. Uses FakeWebSocket to simulate CLI agent. Tests: echo, pwd, multiline output, special chars, env vars, piped commands, failing commands, timeouts, connection management, concurrent commands, config validation, singleton behavior. |
| `tests/llm/tools/test_terminal_tool_llm.py` | LLM integration tests for the terminal tool. |
| `tests/llm/tools/test_read_files_tool.py` | Tests for read_files_tool.py. Unit tests for config/output models. Integration tests require local server + CLI agent. |

---

## 19. Known Edge Cases & Gotchas

### Sentinel-Related

1. **Multi-line commands poison the shell.** Heredocs, here-strings, unclosed quotes cause the sentinel to be absorbed. Recovery: Ctrl-C is sent automatically on timeout. Prevention: LLM is instructed to use single-line commands only.

2. **Echo vs no-echo.** Unix PTY echoes input (2 sentinel matches expected). Windows pipes don't (1 match expected). The fallback accepts 1 match after 1 second on any platform.

3. **Command output contains sentinel string.** If a command's output literally contains `__CMD_DONE__`, it could cause premature detection. This is unlikely in practice but theoretically possible. The sentinel string was chosen to be unique enough to avoid collisions.

### Path Validation

4. **macOS case insensitivity.** `/Users/aryansh/Documents` and `/Users/aryansh/documents` are the same directory. The validator uses `.lower()` on macOS.

5. **Windows `/x` flags.** Tokens like `/d`, `/b`, `/ad` are command flags, not paths. On Windows, tokens starting with `/` are skipped in path extraction, and the Unix path regex is skipped entirely.

6. **Regex false positives.** `echo "hello /world"` catches `/world` as a path. Fix: regex-scanned paths require `os.path.exists(resolved)` before blocking.

7. **`..` resolution.** `cd subdir && cd ..` was blocked because `..` resolved against the base allowed dir instead of the shell's actual cwd. Fix: `psutil.Process(pid).cwd()` gets the real cwd for resolution.

8. **Semicolons in sentinel wrapper.** The sentinel wrapper creates tokens like `~/documents;`. Fix: `_extract_path()` strips trailing `;|&` characters.

9. **`/dev/null` blocked.** Commands like `cat file.txt > /dev/null` were blocked. Fix: `_SAFE_PATHS` frozenset whitelist.

### Connection & Multi-Agent

10. **Sentinel timeout on blocked commands.** When the CLI blocks a command in restricted mode, the server never sees a sentinel and waits 120s. Fix: CLI sends `__CMD_DONE__1__CMD_DONE__` as part of the blocked error output.

11. **Cross-worker session startup race.** The session is created on worker A but the CLI is connected to worker B. Session info must be in Redis synchronously before polling starts. Fix: `_store_redis_session(sync=True)` for create and mark_started.

12. **WebSocket disconnection during command.** If the CLI disconnects while a command is pending, the Future is resolved with a ConnectionError. Fix: `_cancel_pending()` in disconnect handler.

13. **Stale agent pairing.** A consumer may be paired with an agent that has since disconnected. Fix: `resolve_agent_for_consumer()` validates heartbeat before returning and auto-heals stale pairings by reclaiming from the available pool.

14. **Race condition on agent claiming.** Two consumers on different workers could try to claim the same unpaired agent simultaneously. Fix: Lua script (`_CLAIM_OLDEST_AVAILABLE_AGENT_LUA`) performs atomic claim from the sorted set.

15. **Agent cleanup on disconnect.** When an agent disconnects, all its pairings must be released and sessions cleaned up. The disconnect handler calls `release_consumer_pairing()` and `pty_session_manager.cleanup_agent(agent_id)`.

### Platform

16. **Windows PipeProcess sentinel detection.** On Windows, the pipe-based session doesn't echo, so sentinel detection differs. Only 1 match is needed.

17. **Windows shell selection.** Uses `cmd.exe /Q /D /V:ON /K`. PowerShell commands must be invoked explicitly (e.g., `powershell -Command "..."`).

18. **Shell startup time.** PTY sessions take a moment to initialize. Server polls for up to 10 seconds (100 polls * 100ms) waiting for RUNNING status. Initial prompt output is drained and discarded.

### Redis

19. **Redis operation timeouts.** Individual Redis ops timeout after 5 seconds, PTY-related ops after 10 seconds. Operations are wrapped in `_redis_safe()` to prevent Redis failures from crashing the system.

20. **Heartbeat grace period.** Heartbeat TTL is 30s, refreshed every 15s. This gives a 15s window for detection of dead agents before the heartbeat expires.
