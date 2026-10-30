# src/autobots/action/action_type/action_text2text/action_text2text_daytona.py
#
# NOTE FOR MAINTAINERS
# --------------------
# This action relies on the exact Daytona SDK version pinned in pyproject.toml
# and uv.lock:
#   - sandbox.process.exec(command, timeout=...) executes the command via a shell
#     inside the sandbox (effectively /bin/sh -c <command>).
#   - SDK lifecycle waits use Daytona's built-in WebSocket state stream when
#     available and automatically fall back to sparse REST polling.
#   - Autobots still polls its own done.json/run.log/artifacts.json markers;
#     those represent runner completion, not Daytona sandbox lifecycle state.
# Therefore:
#   - Do NOT wrap commands in "bash -lc" (bash may not exist in minimal images).
#   - Avoid double-wrapping commands in "sh -lc" unless SDK behavior changes.
#
# Future consideration: prebaking deps:
# ------------------------------------------------------
# Today, this tool primarily relies on runtime `pip install` inside the sandbox. This can be slow/flaky
# for large dependency graphs (network + resolver/backtracking + native wheels).
#
# Pre-baking dependencies into a custom Daytona snapshot or a custom base image could improve reliability,
# but this is NOT currently exposed as a first-class tool configuration. (We do not currently let the tool
# caller choose an arbitrary snapshot/image; we mostly rely on the default Daytona python environment and
# our compute preset tiers.)
#
# If runtime pip becomes a frequent source of failures/timeouts, consider a follow-up change to:
#   - add config fields (e.g. snapshot/image), and
#   - plumb them through DaytonaClient.create_sandbox(...)
#
import asyncio
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import List, Literal, Optional, Type
from urllib.parse import urlparse, urlunparse

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks, Metadata
from src.autobots.action.action_type.abc.ActionABC import ActionABC
from src.autobots.action.action_type.action_text2text.daytona_conn_forward import (
    resolve_direct_database_env,
    resolve_forwardable_env,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.daytona.artifacts.daytona_artifacts import (
    build_expected_artifacts,
    join_s3_key,
    materialize_artifact_urls,
    reconcile_artifacts_from_s3,
)
from src.autobots.conn.daytona.daytona_client import (
    DaytonaOperationError,
    DaytonaPermissionError,
)
from src.autobots.conn.daytona.state.daytona_job_model import DaytonaJobDoc, DaytonaJobDocCreate
from src.autobots.conn.daytona.state.daytona_token import (
    create_connections_token,
    create_job_token,
)
from src.autobots.conn.daytona.state.user_daytona_jobs import UserDaytonaJobs
from src.autobots.core.settings import SettingsProvider
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.files.file_storage_resolver import (
    resolve_job_output_storage,
    resolve_output_storage,
    resolve_storage_for_url,
)
from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM


def _novnc_ui_url(preview_url: str, *, page: str = "vnc.html") -> str:
    """
    Daytona signed preview URLs for noVNC sometimes open at "/" which can show a directory listing.
    Users expect the actual noVNC client page, usually /vnc.html.

    This rewrites:
      https://<host>                -> https://<host>/vnc.html
      https://<host>/              -> https://<host>/vnc.html
      https://<host>/something/    -> https://<host>/something/vnc.html
    and preserves query/fragment.

    If the URL already ends with ".html", we leave it unchanged.
    """
    u = (preview_url or "").strip()
    if not u:
        return u
    try:
        p = urlparse(u)
        if (p.path or "").lower().endswith(".html"):
            return u

        page = (page or "vnc.html").lstrip("/")
        path = p.path or ""
        if path in ("", "/"):
            new_path = "/" + page
        elif path.endswith("/"):
            new_path = path + page
        else:
            # if it's some non-html path, treat it as a directory-ish prefix
            new_path = path + "/" + page

        return urlunparse((p.scheme, p.netloc, new_path, p.params, p.query, p.fragment))
    except Exception:
        # best-effort; don't fail tool output on URL formatting
        return u


def _sanitize_rel_posix_path(p: str) -> str:
    """
    Tool-side output_files sanitizer.
    - Allow subfolders
    - Forbid absolute paths, backslashes, and '..' traversal
    """
    if not p or not isinstance(p, str):
        raise ValueError("Invalid output filename")
    p = p.strip().replace("\\", "/")
    if len(p) > 512:
        raise ValueError("Invalid output filename: too long")
    if p.startswith("/"):
        raise ValueError("Invalid output filename: absolute paths not allowed")
    pp = PurePosixPath(p)
    if pp.is_absolute():
        raise ValueError("Invalid output filename: absolute paths not allowed")
    if ".." in pp.parts:
        raise ValueError("Invalid output filename: path traversal not allowed")
    norm = str(pp)
    while norm.startswith("./"):
        norm = norm[2:]
    if norm in {".", ""}:
        raise ValueError("Invalid output filename")
    return norm


def _make_daytona_safe_s3_get_url(url: str, *, region: str, allowed_buckets: set[str]) -> str:
    """
    Daytona sandbox workaround: rewrite UNSIGNED/public S3 HTTPS URLs from virtual-hosted style
    to regional path-style.

    Motivation:
      In practice, Daytona sandboxes have shown better connectivity to:
        https://s3.<region>.amazonaws.com/<bucket>/<key>
      compared to:
        https://<bucket>.s3.amazonaws.com/<key>

    Safety:
      - Never rewrite presigned/signed URLs (rewriting host/path would invalidate signature).
      - Only rewrite URLs pointing to our own buckets to avoid breaking arbitrary user-provided S3 URLs.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""

    # If it looks signed/presigned (SigV4), do not rewrite.
    # (We intentionally check for SigV4 markers, not "any query", because some public URLs
    # may include benign query params.)
    q_raw = parsed.query or ""
    q = q_raw.lower()
    if (
        "x-amz-signature=" in q
        or "x-amz-algorithm=" in q
        or "x-amz-credential=" in q
        or "x-amz-date=" in q
        or "x-amz-security-token=" in q
    ):
        return url

    # Handle common virtual-hosted S3 patterns:
    #   <bucket>.s3.amazonaws.com
    #   <bucket>.s3.<region>.amazonaws.com
    bucket = None
    suffix_global = ".s3.amazonaws.com"
    suffix_regional = f".s3.{region}.amazonaws.com"
    if host.endswith(suffix_global):
        bucket = host[: -len(suffix_global)]
    elif host.endswith(suffix_regional):
        bucket = host[: -len(suffix_regional)]

    if not bucket or bucket not in allowed_buckets:
        return url

    scheme = parsed.scheme or "https"
    # Preserve query/fragment since we only rewrite unsigned URLs.
    qs = f"?{q_raw}" if q_raw else ""
    frag = f"#{parsed.fragment}" if parsed.fragment else ""
    return f"{scheme}://s3.{region}.amazonaws.com/{bucket}{parsed.path}{qs}{frag}"


# Official default Daytona snapshot tiers (vCPU/GiB mapped via snapshot name).
_DAYTONA_COMPUTE_PRESETS: dict[str, dict[str, int | str]] = {
    "small": {"cpu": 1, "memory_gib": 1, "snapshot": "daytona-small"},
    "medium": {"cpu": 2, "memory_gib": 4, "snapshot": "daytona-medium"},
    "large": {"cpu": 4, "memory_gib": 8, "snapshot": "daytona-large"},
}


@dataclass(frozen=True)
class DaytonaLifecyclePolicy:
    ephemeral: bool
    auto_stop_interval: int
    auto_archive_interval: int | None
    auto_delete_interval: int
    ttl_minutes: int | None


def _derive_lifecycle_policy(
    config: "DaytonaSandboxConfig",
    settings,
) -> DaytonaLifecyclePolicy:
    if config.lifecycle == "retained":
        return DaytonaLifecyclePolicy(
            ephemeral=False,
            auto_stop_interval=int(config.idle_timeout_minutes),
            auto_archive_interval=int(settings.DAYTONA_RETAINED_AUTO_ARCHIVE_INTERVAL_MINUTES),
            auto_delete_interval=int(config.retention_minutes),
            ttl_minutes=None,
        )

    if config.lifecycle == "persistent":
        return DaytonaLifecyclePolicy(
            ephemeral=False,
            auto_stop_interval=int(config.idle_timeout_minutes),
            auto_archive_interval=int(settings.DAYTONA_PERSISTENT_AUTO_ARCHIVE_INTERVAL_MINUTES),
            auto_delete_interval=-1,
            ttl_minutes=None,
        )

    runtime = int(config.max_runtime_minutes)
    auto_stop = runtime + int(settings.DAYTONA_EPHEMERAL_AUTO_STOP_GRACE_MINUTES)
    ttl = runtime + int(settings.DAYTONA_EPHEMERAL_TTL_GRACE_MINUTES)
    ttl = min(
        ttl,
        int(settings.DAYTONA_MAX_EPHEMERAL_TTL_MINUTES),
    )
    auto_stop = min(auto_stop, ttl)

    return DaytonaLifecyclePolicy(
        ephemeral=True,
        auto_stop_interval=auto_stop,
        auto_archive_interval=None,
        auto_delete_interval=0,
        ttl_minutes=ttl,
    )


def _cleanup_mode(
    *,
    created_sandbox: bool,
    lifecycle: str,
) -> Literal["delete", "stop", "none"]:
    if not created_sandbox:
        return "none"
    if lifecycle == "ephemeral":
        return "delete"
    return "stop"


def _job_cleanup_mode(job) -> Literal["delete", "stop", "none"]:
    # Reused sandboxes are never automatically stopped or deleted.
    if bool(getattr(job, "reused_sandbox", False)):
        return "none"

    created = bool(getattr(job, "created_sandbox", True))
    lifecycle = str(getattr(job, "lifecycle", "ephemeral"))

    # Preserve historical keep_sandbox=true records.
    if bool(getattr(job, "keep_sandbox", False)) and lifecycle == "ephemeral":
        return "none"

    return _cleanup_mode(
        created_sandbox=created,
        lifecycle=lifecycle,
    )


def _job_has_forwarded_connections(job) -> bool:
    """True when the job was started with app or database connections forwarded."""
    return bool(
        getattr(job, "sandbox_owner_user_id", None) or getattr(job, "forward_databases", None)
    )


def _normalize_pid(raw: str | None) -> str | None:
    """
    Extract a single numeric PID from background-start output.

    The start command echoes exactly one PID, but shells (and transport layers) can
    add extra lines. Only a clean digit string is usable: `runner_pid` is later fed
    to `int()` by the cancel handler, so anything else must be rejected here rather
    than stored and made to blow up later.
    """
    for line in reversed((raw or "").splitlines()):
        candidate = line.strip()
        if candidate.isdigit():
            return candidate
    return None


# Start the runner as a background job in its own session/process group when `setsid`
# is available, so the started PID is also its process-group id and a later negative-PID
# kill terminates any children the user's code spawned (children inherit the forwarded
# credentials env, so killing only the parent is not enough). Without `setsid` this
# degrades to a plain background job (previous behaviour), just without the group kill.
# Written as a single unambiguous background job followed by exactly one `echo $!`:
# an `a && b & echo $! || c & echo $!` form hits shell operator precedence and prints
# the PID twice.
_RUNNER_START_CMD = (
    "if command -v setsid >/dev/null 2>&1; then "
    "  if command -v nohup >/dev/null 2>&1; then "
    "    setsid nohup python3 -u runner.py > run.log 2>&1 < /dev/null & "
    "  else "
    "    setsid python3 -u runner.py > run.log 2>&1 < /dev/null & "
    "  fi; "
    "else "
    "  if command -v nohup >/dev/null 2>&1; then "
    "    nohup python3 -u runner.py > run.log 2>&1 < /dev/null & "
    "  else "
    "    python3 -u runner.py > run.log 2>&1 < /dev/null & "
    "  fi; "
    "fi; "
    "echo $!"
)


class DaytonaSandboxConfig(BaseModel):
    """
    Single-tool multi-operation config. Only 'operation' is required in schema;
    server-side validator enforces required fields per operation.
    """

    model_config = ConfigDict(extra="forbid")

    operation: Literal["run", "status", "cancel", "list", "stop"] = "run"

    reuse_sandbox_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional (run/stop): use this exact existing Daytona sandbox "
            "instead of creating a new sandbox. Reuse never changes the "
            "sandbox lifecycle and never automatically stops or deletes it."
        ),
    )

    lifecycle: Literal["ephemeral", "retained", "persistent"] = Field(
        default="ephemeral",
        description=(
            "Lifecycle for a newly created sandbox. Defaults to ephemeral. "
            "retained and persistent must be explicitly requested."
        ),
    )
    max_runtime_minutes: int = Field(
        default=10,
        ge=1,
        le=1440,
        description=(
            "Declared maximum runtime for a newly created ephemeral sandbox. "
            "Used to derive native Daytona auto-stop and hard TTL. "
            "For seconds-long fire-and-forget jobs, explicitly use 1."
        ),
    )
    retention_minutes: Optional[int] = Field(
        default=None,
        ge=30,
        le=43200,
        description=(
            "Required for lifecycle='retained'. Continuously stopped minutes "
            "after which Daytona deletes the sandbox."
        ),
    )
    idle_timeout_minutes: Optional[int] = Field(
        default=None,
        ge=0,
        le=43200,
        description=(
            "Required for retained/persistent creation. Minutes of inactivity "
            "before Daytona stops the sandbox. Use 0 only when a long-running "
            "background process requires auto-stop to remain disabled."
        ),
    )
    confirm_persistent: bool = Field(
        default=False,
        description=(
            "Must be true for lifecycle='persistent'. Persistent sandboxes "
            "have no automatic deletion interval."
        ),
    )

    need_vnc_link: bool = Field(
        default=False,
        description=(
            "Optional (run/status): ensure Daytona VNC/noVNC desktop stack is running and return a signed preview URL "
            "for browser-based desktop access."
        ),
    )
    vnc_link_ttl_sec: int = Field(
        default=900,
        ge=1,
        le=86400,
        description="TTL (seconds) for the signed VNC/noVNC preview URL.",
    )
    vnc_port: int = Field(
        default=6080,
        ge=3000,
        le=9999,
        description="HTTP port for noVNC inside the sandbox (commonly 6080; preview gateway typically supports 3000..9999).",
    )

    artifact_mode: Literal["auto", "callback", "presigned", "server_sync", "none"] = Field(
        default="auto",
        description=(
            "Artifact transfer strategy. auto tries callback if reachable (ping), otherwise uses presigned. "
            "server_sync uploads from server on completion. none disables uploads."
        ),
    )

    # run
    code: Optional[str] = Field(default=None, description="Python code to execute in Daytona.")
    language: Literal["python"] = "python"

    compute_profile: Literal["default", "small", "medium", "large", "custom"] = Field(
        default="default",
        description=(
            "Compute sizing for the sandbox. "
            "small/medium/large map to Daytona snapshot tiers. "
            "custom uses cpu + memory_gib (best-effort). "
            "Implementation prefers create-time sizing (image+resources) when supported; "
            "otherwise may fall back to smaller resources."
        ),
    )
    cpu: Optional[int] = Field(
        default=None,
        ge=1,
        description="Requested vCPU count (used when compute_profile='custom').",
    )
    memory_gib: Optional[int] = Field(
        default=None,
        ge=1,
        description="Requested memory in GiB (used when compute_profile='custom').",
    )

    pip_packages: List[str] = Field(default_factory=list)
    input_urls: List[str] = Field(
        default_factory=list, description="URLs to download into the sandbox before running."
    )
    output_files: List[str] = Field(
        default_factory=list, description="Filenames to upload to S3 when done."
    )
    output_storage_scope: Literal["meetkiwi_public", "meetkiwi_private", "customer_s3"] = Field(
        default="meetkiwi_public",
        description=(
            "Storage destination for generated output_files only. meetkiwi_public preserves "
            "the existing public URL behavior; private destinations return presigned GET URLs."
        ),
    )
    output_storage_connection_name: Optional[str] = Field(
        default=None,
        description=(
            "Saved AWS connection name used only with output_storage_scope='customer_s3'. "
            "If omitted, the default AWS connection is used; if none exists, output falls "
            "back to MeetKiwi private storage."
        ),
    )

    # --- Connection forwarding (AUT-1593) ---
    # MCP connection forwarding (a `forward_mcp_servers` grant list plus a bridge in
    # api_daytona.py) was designed alongside these fields and then deliberately deferred,
    # not forgotten: this repo already ships a live MCP client, so building a second one
    # here would be throwaway work. The design that still has to be implemented --
    # including the two things that client gets wrong today (fail-open tool allowlisting
    # and no SSRF guard) -- is written up in docs/conn_mcp_blueprint.md.
    forward_connections: List[str] = Field(
        default_factory=list,
        description=(
            "Saved app connection names (as saved in the user's app connections, not app "
            "types) whose stored credentials should be resolved fresh and injected into this "
            "sandbox run as environment variables. Empty by default."
        ),
    )
    forward_databases: List[str] = Field(
        default_factory=list,
        description=(
            "Saved database connection names (UserDbAuth) usable via the connections bridge "
            "during this run."
        ),
    )
    forward_databases_direct: List[str] = Field(
        default_factory=list,
        description=(
            "Saved DB connection names materialized directly into the sandbox environment as a "
            "connection string, for real read/write and bulk operations. Unlike forward_databases "
            "(read-only bridge), this hands the sandbox a usable database credential with whatever "
            "permissions that connection has. Use only when the job genuinely needs write/bulk "
            "access."
        ),
    )

    # Advanced: pip install tuning (runner-side)
    # If omitted, runner defaults apply (currently 300s wall timeout, etc).
    pip_wall_timeout_sec: Optional[int] = Field(
        default=None,
        ge=30,
        le=3600,
        description=(
            "Advanced: wall-clock timeout (seconds) for pip install inside the sandbox runner. "
            "If omitted, the runner default is used."
        ),
    )
    pip_socket_timeout_sec: Optional[int] = Field(
        default=None,
        ge=5,
        le=600,
        description=(
            "Advanced: pip network socket timeout (seconds) used with pip --timeout. "
            "If omitted, the runner default is used."
        ),
    )
    pip_retries: Optional[int] = Field(
        default=None,
        ge=0,
        le=10,
        description=(
            "Advanced: pip retry count used with pip --retries. "
            "If omitted, the runner default is used."
        ),
    )

    wait_for_completion_sec: int = Field(
        default=60,
        ge=0,
        le=300,
        description=(
            "If >0, poll up to N seconds and return completed result if finished; if 0, return immediately (fire-and-forget) with job_id."
        ),
    )

    # status/cancel
    job_id: Optional[str] = Field(default=None, description="Job id returned by operation=run.")

    @model_validator(mode="after")
    def _validate_compute(self):
        operation = getattr(self, "operation", "run")
        reuse_id = (getattr(self, "reuse_sandbox_id", None) or "").strip()

        if operation == "stop":
            if not reuse_id:
                raise ValueError("reuse_sandbox_id is required for operation='stop'")
            return self

        # Only validate run-specific fields for run operation.
        if operation != "run":
            return self

        output_connection_name = (self.output_storage_connection_name or "").strip() or None
        self.output_storage_connection_name = output_connection_name
        if self.output_storage_scope != "customer_s3" and output_connection_name:
            raise ValueError(
                "output_storage_connection_name requires output_storage_scope='customer_s3'"
            )

        if reuse_id:
            if self.retention_minutes is not None:
                raise ValueError("retention_minutes cannot be changed while reusing a sandbox")
            if self.idle_timeout_minutes is not None:
                raise ValueError("idle_timeout_minutes cannot be changed while reusing a sandbox")
            if self.confirm_persistent:
                raise ValueError("confirm_persistent cannot be used while reusing a sandbox")
        elif self.lifecycle == "ephemeral":
            if self.retention_minutes is not None:
                raise ValueError("retention_minutes is only valid for lifecycle='retained'")
            if self.idle_timeout_minutes is not None:
                raise ValueError(
                    "idle_timeout_minutes is only valid for retained/persistent sandboxes"
                )
            if self.confirm_persistent:
                raise ValueError("confirm_persistent is only valid for lifecycle='persistent'")
        elif self.lifecycle == "retained":
            if self.retention_minutes is None:
                raise ValueError("retention_minutes is required for lifecycle='retained'")
            if self.idle_timeout_minutes is None:
                raise ValueError("idle_timeout_minutes is required for lifecycle='retained'")
            if self.confirm_persistent:
                raise ValueError("confirm_persistent is only valid for lifecycle='persistent'")
        elif self.lifecycle == "persistent":
            if not self.confirm_persistent:
                raise ValueError("lifecycle='persistent' requires confirm_persistent=true")
            if self.idle_timeout_minutes is None:
                raise ValueError("idle_timeout_minutes is required for lifecycle='persistent'")
            if self.retention_minutes is not None:
                raise ValueError("retention_minutes cannot be used with lifecycle='persistent'")

        # Reused sandboxes ignore sizing; sizing is a create-time concern only.
        if reuse_id:
            return self

        prof = self.compute_profile or "default"
        if prof == "default":
            # Contract: default does not accept explicit cpu/memory. Use compute_profile="custom".
            if self.cpu is not None or self.memory_gib is not None:
                raise ValueError("cpu/memory_gib require compute_profile='custom'")

        if prof in _DAYTONA_COMPUTE_PRESETS:
            # If cpu/memory provided along with preset, they must match exactly.
            preset = _DAYTONA_COMPUTE_PRESETS[prof]
            pcpu = int(preset["cpu"])  # type: ignore[arg-type]
            pmem = int(preset["memory_gib"])  # type: ignore[arg-type]
            if self.cpu is not None and int(self.cpu) != pcpu:
                raise ValueError(f"compute_profile='{prof}' requires cpu={pcpu}")
            if self.memory_gib is not None and int(self.memory_gib) != pmem:
                raise ValueError(f"compute_profile='{prof}' requires memory_gib={pmem}")
        elif prof == "custom":
            if self.cpu is None or self.memory_gib is None:
                raise ValueError("compute_profile='custom' requires cpu and memory_gib")

        return self


class ActionText2TextDaytona(
    ActionABC[
        DaytonaSandboxConfig, DaytonaSandboxConfig, DaytonaSandboxConfig, DataBlock, DataBlocks
    ]
):
    type = ActionType.text2text_daytona

    @staticmethod
    def get_description() -> str:
        return (
            "daytona_sandbox: Run Python code in a remote Daytona sandbox (short or long runs).\n\n"
            "This is ONE tool with multiple operations:\n"
            "1) operation='run': starts a job. If it finishes within wait_for_completion_sec, returns output and artifact URLs.\n"
            "   Otherwise returns a job_id that you can poll.\n"
            "   Tip: set wait_for_completion_sec=0 for fire-and-forget (return job_id immediately).\n"
            "2) operation='status': polls a job_id and returns status + log tail + artifact URLs when completed.\n"
            "3) operation='cancel': cancels a job_id.\n"
            "4) operation='list': list your recent Autobots Daytona jobs from the job database; "
            "this does not enumerate Daytona provider sandboxes.\n"
            "5) operation='stop': stop reuse_sandbox_id without deleting it. "
            "Stopping an ephemeral sandbox may cause Daytona to delete it.\n\n"
            "Lifecycle for newly created sandboxes:\n"
            "- Minimal run calls default to lifecycle='ephemeral'.\n"
            "- Ephemeral sandboxes are deleted on observed completion and also have "
            "native auto-stop, auto-delete, and a finite TTL.\n"
            "- Short jobs are normally detected and deleted during the default 60-second completion wait.\n"
            "- Set max_runtime_minutes to the expected upper bound for long or fire-and-forget jobs.\n"
            "- For a seconds-long fire-and-forget job, use max_runtime_minutes=1; "
            "native lifecycle intervals are minute-based.\n"
            "- lifecycle='retained' requires retention_minutes and idle_timeout_minutes.\n"
            "- lifecycle='persistent' requires idle_timeout_minutes and confirm_persistent=true.\n"
            "- idle_timeout_minutes=0 disables auto-stop; use it only for intentional "
            "long-running background processes.\n\n"
            "Sandbox reuse:\n"
            "- reuse_sandbox_id uses the exact supplied sandbox.\n"
            "- Reuse never changes lifecycle settings and never automatically stops or deletes the sandbox.\n"
            "- A Sandbox Read key can execute in an already-started sandbox but cannot resume a stopped one.\n"
            "- If creation or resume is denied, the tool does not switch credentials automatically.\n\n"
            "Optional VNC/noVNC (run/status):\n"
            "- need_vnc_link: if true, start/ensure the desktop stack and return a signed noVNC URL.\n"
            "- vnc_port: noVNC port (default 6080).\n"
            "- vnc_link_ttl_sec: signed URL TTL.\n\n"
            "Starting VNC does not disable or otherwise change sandbox auto-stop.\n\n"
            "Note: vnc_url points to /vnc.html (noVNC UI), not the directory listing.\n\n"
            "Advanced pip tuning (optional; run only):\n"
            "- pip_wall_timeout_sec: wall-clock cap for pip install inside the sandbox runner (default 300).\n"
            "- pip_socket_timeout_sec: pip --timeout for network operations (default 60).\n"
            "- pip_retries: pip --retries (default 3).\n"
            "  Use these only if pip installs are slow/flaky or you hit pip timeouts (e.g., heavy deps).\n\n"
            "Compute sizing:\n"
            "- Use compute_profile='small'|'medium'|'large' for official Daytona tiers.\n"
            "  small=1 vCPU/1 GiB, medium=2 vCPU/4 GiB, large=4 vCPU/8 GiB.\n"
            "- For custom sizing, use compute_profile='custom' plus cpu=<int> and memory_gib=<int>.\n\n"
            "Note: custom sizing is best-effort. If Daytona cannot apply the requested CPU/RAM "
            "(quota/limits/backend feature availability), the job will continue running on the already-provisioned size.\n\n"
            "For long jobs: call operation='run' (get job_id), then later call operation='status' with that job_id.\n\n"
            "Connection forwarding (run only; all optional and empty by default):\n"
            "- forward_connections: list of saved app connection NAMES (not app types; a user may "
            "have several connections of the same app type). The user's stored app credentials are "
            "resolved at run start and injected as env vars named "
            "AUTOBOTS_CONN_<CONNECTIONNAME>_<HEADERNAME> (uppercased, non-alphanumeric -> '_'), "
            "e.g. a connection named 'workday-prod' -> AUTOBOTS_CONN_WORKDAY_PROD_AUTHORIZATION; an "
            "app may set several such vars. AWS connections are special-cased to the standard boto3 "
            "vars AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_DEFAULT_REGION. Credentials are "
            "resolved once per run and NOT refreshed, so OAuth tokens may expire during very long "
            "jobs. Only header values are forwarded, not base-URL/tenant info: sandbox code must "
            "already know the target host.\n"
            "- forward_databases: NOT YET AVAILABLE. This will be a read-only query bridge "
            "(list of saved DB connection names, queryable for the duration of the run without handing "
            "the sandbox real credentials). Setting this field returns an error right now -- use "
            "forward_databases_direct instead.\n"
            "- forward_databases_direct: list of saved DB connection names for real read/write and bulk "
            "work. The actual connection string is injected as AUTOBOTS_DB_<NAME>_URL (uppercased, "
            "non-alphanumeric -> '_'); sandbox code connects with its own client (sqlalchemy, psycopg2, "
            "...) and does full CRUD. BigQuery connections additionally get "
            "AUTOBOTS_DB_<NAME>_CREDENTIALS_JSON (a JSON string). Supports MySQL, Postgres, Oracle, "
            "BigQuery, plus Snowflake and Databricks -- those URLs use SQLAlchemy dialect prefixes "
            "(snowflake://, databricks://), so set pip_packages (e.g. ['snowflake-sqlalchemy'] or "
            "['databricks-sqlalchemy']) before calling create_engine(). Cloud SQL and SQLite connections "
            "cannot be forwarded this way and are skipped with a warning. This hands the sandbox the "
            "connection's full permissions: prefer forward_databases when a bounded read suffices.\n"
            "- MCP connection forwarding is not available; there is no config field for it. "
            "Do not attempt it.\n\n"
            "Output storage (generated output_files only):\n"
            "- output_storage_scope='meetkiwi_public' preserves public output URLs.\n"
            "- output_storage_scope='meetkiwi_private' stores outputs privately and returns signed download URLs.\n"
            "- output_storage_scope='customer_s3' uses output_storage_connection_name, or the default saved AWS connection. "
            "If no default AWS connection exists, it falls back to MeetKiwi private storage; an explicitly named missing or invalid connection returns an error.\n"
            "- Private MeetKiwi and customer S3 download URLs are presigned for up to seven days. Each operation='status' call generates fresh URLs from the stored artifact locations.\n"
            "- Output storage works independently with artifact_mode='auto', 'callback', 'presigned', or 'server_sync'; artifact_mode='none' uploads nothing and returns no artifact URLs.\n"
            "- output storage is independent from input_urls and does not change where inputs were uploaded.\n\n"
            "Artifact note: if a sandbox has expired, operation='status' may still return artifact URLs by "
            "reconciling declared output_files against S3 under the job prefix (if the sandbox uploaded them)."
        )

    @staticmethod
    def get_config_create_type() -> Type[DaytonaSandboxConfig]:
        return DaytonaSandboxConfig

    @staticmethod
    def get_config_update_type() -> Type[DaytonaSandboxConfig]:
        return DaytonaSandboxConfig

    @staticmethod
    def get_config_type() -> Type[DaytonaSandboxConfig]:
        return DaytonaSandboxConfig

    @staticmethod
    def get_input_type() -> Type[DataBlock]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[DataBlocks]:
        return DataBlocks

    def __init__(
        self, action_config: DaytonaSandboxConfig, user: UserORM, db: AsyncIOMotorDatabase
    ):
        super().__init__(action_config=action_config, user=user, db=db)

    @staticmethod
    def _fmt_bool(v: object) -> str:
        return "true" if bool(v) else "false"

    def _mirror_sandbox_temp_logs_to_server(
        self,
        *,
        ctx: Context,
        job_id: str,
        sandbox_id: str,
        log_tail: str,
    ) -> None:
        """
        Mirror sandbox TEMP diagnostic lines into API-server logs (Datadog-visible).

        The runner executes inside the Daytona sandbox, so it cannot use our Loguru pipeline.
        We therefore emit TEMP diagnostic markers via `print(...)` in the runner, then mirror
        those lines from `run.log` here with logger.bind(ctx=ctx,...).
        """
        if not log_tail:
            return

        lines = log_tail.splitlines()
        picked: list[str] = []
        for ln in lines:
            # Only mirror TEMP S3 diagnostics (keep DD noise low).
            # These prefixes are emitted by the runner's TEMP diagnostics block.
            if ln.startswith("[TEMP_S3_DIAG_") or ln.startswith("[TEMP_S3_DIAG"):
                picked.append(ln)

        if not picked:
            return

        # --- TEMP LOGS START (DAYTONA SANDBOX DIAG MIRROR) ---
        b = logger.bind(ctx=ctx, job_id=job_id, sandbox_id=sandbox_id)
        b.warning("--- TEMP DAYTONA SANDBOX DIAG MIRROR START ---")
        for ln in picked[-40:]:
            # Keep the original sandbox line intact for grep/search.
            b.warning(f"SANDBOX_LOG {ln}")
        b.warning("--- TEMP DAYTONA SANDBOX DIAG MIRROR END ---")
        # --- TEMP LOGS END (DAYTONA SANDBOX DIAG MIRROR) ---

    async def _server_sync_missing_artifacts(
        self,
        *,
        ctx: Context,
        daytona_conn,
        uj: UserDaytonaJobs,
        job_id: str,
        sandbox_id: str,
        job_workdir: str,
        storage,
        s3_prefix: str,
        artifact_mode: str,
        declared_outputs: list[str],
        artifacts: dict,
    ) -> dict:
        """
        Server-side sync fallback:
        - If the sandbox could not upload artifacts (presigned/callback issues), download outputs from sandbox
          and upload to S3 from the API server.
        - This is best-effort and should not crash the tool.
        """
        artifacts = dict(artifacts or {})
        declared_outputs = declared_outputs or []
        missing = [fn for fn in declared_outputs if fn and fn not in artifacts]

        # Respect artifact_mode="none": never upload artifacts.
        if artifact_mode == "none" or not missing or not s3_prefix:
            return artifacts

        # --- TEMP LOGS START (DAYTONA SERVER_SYNC DIAG) ---
        logger.bind(
            ctx=ctx,
            job_id=job_id,
            sandbox_id=sandbox_id,
            artifact_mode=artifact_mode,
            missing=missing,
        ).warning(
            "--- TEMP DAYTONA SERVER_SYNC DIAG START --- "
            "Artifacts missing; attempting server_sync fallback"
        )
        # --- TEMP LOGS END (DAYTONA SERVER_SYNC DIAG) ---

        try:
            synced = dict(artifacts)
            uploaded: list[str] = []
            failed: list[str] = []
            with tempfile.TemporaryDirectory() as td:
                for fn in missing:
                    # fn is already sanitized relative posix path; preserve structure to avoid collisions.
                    local_path = os.path.join(td, fn)
                    os.makedirs(os.path.dirname(local_path), exist_ok=True)
                    try:
                        await daytona_conn.download_file_to_path(
                            ctx=ctx,
                            sandbox_id=sandbox_id,
                            remote_path=f"{job_workdir.rstrip('/')}/{fn}",
                            local_path=local_path,
                            timeout_sec=1800,
                        )
                        with open(local_path, "rb") as f:
                            url = await storage.upload_fileobj(
                                ctx=ctx,
                                file_obj=f,
                                filename=join_s3_key(s3_prefix, fn),
                            )
                        if url:
                            synced[fn] = str(url)
                            uploaded.append(fn)
                        else:
                            failed.append(fn)
                    except Exception as e:
                        failed.append(fn)
                        logger.bind(ctx=ctx, job_id=job_id, filename=fn).warning(
                            f"--- TEMP DAYTONA SERVER_SYNC DIAG --- failed to sync {fn}: {e}"
                        )

            await uj.update_by_job_id(ctx=ctx, job_id=job_id, update={"artifacts": synced})

            # --- TEMP LOGS START (DAYTONA SERVER_SYNC DIAG) ---
            logger.bind(
                ctx=ctx,
                job_id=job_id,
                sandbox_id=sandbox_id,
                uploaded=uploaded,
                failed=failed,
            ).info(
                "--- TEMP DAYTONA SERVER_SYNC DIAG END --- "
                f"server_sync completed: uploaded_count={len(uploaded)} failed_count={len(failed)}"
            )
            # --- TEMP LOGS END (DAYTONA SERVER_SYNC DIAG) ---

            return synced
        except Exception as e:
            logger.bind(ctx=ctx, job_id=job_id, sandbox_id=sandbox_id).warning(
                f"--- TEMP DAYTONA SERVER_SYNC DIAG END --- server_sync fallback failed: {e}"
            )
            return artifacts

    async def run_action(
        self,
        ctx: Context,
        app_state: AppState,
        action_input: DataBlock,
        notify: Notify | None = None,
    ) -> DataBlocks | Exception:
        # For ActionGraph / saved action runs we keep the canonical output type: DataBlocks.
        # Internally we reuse the tool logic (which returns a single DataBlock).
        cfg = self.action_config.model_copy(deep=True)
        if cfg.operation == "run" and action_input.text and action_input.text.strip():
            cfg.code = action_input.text
        block_or_err = await self.run_tool(
            action_config=cfg, ctx=ctx, app_state=app_state, notify=notify
        )
        if isinstance(block_or_err, Exception):
            return block_or_err
        return DataBlocks(blocks=[block_or_err])

    async def run_tool(
        self,
        action_config: DaytonaSandboxConfig,
        ctx: Context,
        app_state: AppState,
        notify: Notify | None = None,
    ) -> DataBlock | Exception:
        settings = SettingsProvider.sget()
        user = self.user
        db = self.db

        # Runtime validation (allows saved actions to pass code via action_input.text in run_action)
        if action_config.operation == "run":
            if not action_config.code or not action_config.code.strip():
                return DataBlock(
                    text="Error: code is required for operation='run'.",
                    metadata=Metadata(),
                )
        if action_config.operation in ("status", "cancel"):
            if not action_config.job_id:
                return DataBlock(
                    text="Error: job_id is required for operation='status'/'cancel'.",
                    metadata=Metadata(),
                )

        # list is purely DB-side
        if action_config.operation == "list":
            uj = UserDaytonaJobs(user=user, db=db)
            docs = await uj.list(ctx=ctx, limit=30)
            payload = []
            for d in docs:
                artifacts = d.artifacts or {}
                expected_artifacts = getattr(d, "expected_artifacts", None) or {}
                try:
                    resolved = await resolve_job_output_storage(ctx=ctx, db=db, job=d)
                    artifacts = await materialize_artifact_urls(
                        ctx=ctx,
                        storage=resolved.storage,
                        storage_scope=resolved.scope,
                        s3_prefix=resolved.s3_prefix,
                        artifacts=artifacts,
                    )
                    expected_artifacts = await materialize_artifact_urls(
                        ctx=ctx,
                        storage=resolved.storage,
                        storage_scope=resolved.scope,
                        s3_prefix=resolved.s3_prefix,
                        artifacts=expected_artifacts,
                    )
                except Exception as exc:
                    logger.bind(ctx=ctx, job_id=d.job_id).warning(
                        f"Failed to materialize Daytona artifact URLs: {exc}"
                    )
                    artifacts = {}
                    expected_artifacts = {}
                payload.append(
                    {
                        "job_id": d.job_id,
                        "sandbox_id": d.sandbox_id,
                        "status": d.status,
                        "reused_sandbox": bool(getattr(d, "reused_sandbox", False)),
                        "reused_sandbox_id": getattr(d, "reused_sandbox_id", None),
                        "created_sandbox": bool(getattr(d, "created_sandbox", True)),
                        "lifecycle": getattr(d, "lifecycle", "ephemeral"),
                        "job_workdir": getattr(d, "job_workdir", ""),
                        "cleanup_completed": bool(getattr(d, "cleanup_completed", False)),
                        "cleanup_error": getattr(d, "cleanup_error", "") or "",
                        "output_storage_scope": getattr(
                            d, "output_storage_scope", "meetkiwi_public"
                        ),
                        "output_storage_connection_name": getattr(
                            d, "output_storage_connection_name", None
                        ),
                        "artifacts": artifacts,
                        "expected_artifacts": expected_artifacts,
                    }
                )
            return DataBlock(text=json.dumps(payload, ensure_ascii=False), metadata=Metadata())

        # obtain Daytona client (BYO key + system fallback)
        daytona_conn = await AppConn.get_daytona(ctx=ctx, db=db, user=user)
        if isinstance(daytona_conn, Exception):
            return daytona_conn

        uj = UserDaytonaJobs(user=user, db=db)

        if action_config.operation == "stop":
            sandbox_id = (action_config.reuse_sandbox_id or "").strip()
            try:
                await daytona_conn.stop_sandbox(
                    ctx=ctx,
                    sandbox_id=sandbox_id,
                    timeout_sec=60,
                )
            except DaytonaPermissionError:
                return DataBlock(
                    text=(
                        "Error [DAYTONA_STOP_PERMISSION_REQUIRED]: "
                        "This Daytona credential cannot stop the requested sandbox. "
                        "Sandbox Write permission is required."
                    ),
                    metadata=Metadata(),
                )
            except DaytonaOperationError as exc:
                return DataBlock(
                    text=(
                        "Error [DAYTONA_STOP_FAILED]: "
                        f"Failed to stop sandbox_id={sandbox_id}: {exc}"
                    ),
                    metadata=Metadata(),
                )
            return DataBlock(
                text=(f"sandbox_id={sandbox_id}\nstatus=stopped\nfilesystem_preserved=true"),
                metadata=Metadata(),
            )

        if action_config.operation == "cancel":
            job = await uj.get_by_job_id(ctx=ctx, job_id=action_config.job_id)
            if not job:
                return ValueError("Job not found")
            cleanup_mode = _job_cleanup_mode(job)
            cleanup_completed = bool(getattr(job, "cleanup_completed", False))
            cleanup_error = ""

            # Best-effort session cleanup (if session-based execution was used)
            if getattr(job, "session_id", None) and job.sandbox_id:
                try:
                    await daytona_conn.delete_session(
                        ctx=ctx, sandbox_id=job.sandbox_id, session_id=job.session_id
                    )
                except Exception:
                    pass

            if cleanup_mode == "none":
                # The job does not own the reused sandbox, so there is no
                # sandbox cleanup to perform.
                cleanup_completed = True
                # ...but the runner process keeps running, and for a job with forwarded
                # connections it still holds materialized credentials (forward_databases_direct
                # writes real database URLs into the process environment). Revoking the
                # bridge does not reach those, so terminate the runner explicitly.
                # Best-effort: never block the cancel flow on it.
                runner_pid = getattr(job, "runner_pid", None)
                if runner_pid and job.sandbox_id and _job_has_forwarded_connections(job):
                    try:
                        # runner_pid is only ever stored as a normalized digit string.
                        pid_int = int(runner_pid)
                        # Verify identity before signaling (a stale PID may have been
                        # reused by an unrelated process), then kill the whole process
                        # group so children that inherited the forwarded credentials go
                        # too; the plain-PID kill is the fallback for the no-setsid case.
                        # Both are no-ops if the target is already gone, hence `; true`.
                        kill_cmd = (
                            f'if [ -d "/proc/{pid_int}" ] && '
                            f'[ "$(readlink -f "/proc/{pid_int}/cwd" 2>/dev/null)" '
                            f'= "{getattr(job, "job_workdir", "") or ""}" ]; then '
                            f"kill -9 -- -{pid_int} 2>/dev/null; "
                            f"kill -9 -- {pid_int} 2>/dev/null; "
                            f"fi; true"
                        )
                        await daytona_conn.exec(
                            ctx=ctx,
                            sandbox_id=job.sandbox_id,
                            command=kill_cmd,
                            timeout_sec=10,
                        )
                    except Exception as e:
                        logger.bind(ctx=ctx, job_id=job.job_id, runner_pid=runner_pid).warning(
                            f"Failed to terminate runner process on cancel: {e}"
                        )
            elif job.sandbox_id and not cleanup_completed:
                try:
                    if cleanup_mode == "delete":
                        await daytona_conn.delete_sandbox(ctx=ctx, sandbox_id=job.sandbox_id)
                    elif cleanup_mode == "stop":
                        await daytona_conn.stop_sandbox(ctx=ctx, sandbox_id=job.sandbox_id)
                    cleanup_completed = True
                except Exception as e:
                    cleanup_error = str(e)[:500]
                    logger.bind(ctx=ctx, job_id=job.job_id, sandbox_id=job.sandbox_id).warning(
                        f"Cancel: sandbox cleanup failed: {e}"
                    )

            cancel_update = {
                "status": "canceled",
                "cleanup_completed": cleanup_completed,
                "cleanup_error": cleanup_error,
            }
            # Guaranteed revocation: a canceled job can no longer use the connections bridge.
            if _job_has_forwarded_connections(job):
                cancel_update["connections_revoked"] = True
            await uj.update_by_job_id(
                ctx=ctx,
                job_id=job.job_id,
                update=cancel_update,
            )
            return DataBlock(
                text=(
                    f"job_id={job.job_id}\n"
                    f"sandbox_id={job.sandbox_id}\n"
                    f"status=canceled\n"
                    f"cleanup_mode={cleanup_mode}\n"
                    f"cleanup_completed={self._fmt_bool(cleanup_completed)}\n"
                    f"cleanup_error={cleanup_error}"
                ),
                metadata=Metadata(),
            )

        if action_config.operation == "status":
            job = await uj.get_by_job_id(ctx=ctx, job_id=action_config.job_id)
            if not job:
                return ValueError("Job not found")
            cleanup_mode = _job_cleanup_mode(job)
            cleanup_completed = bool(getattr(job, "cleanup_completed", False))
            cleanup_error = getattr(job, "cleanup_error", "") or ""
            vnc_url: str | None = None
            vnc_error: str | None = None
            vnc_warning: str | None = None

            job_mode = getattr(job, "artifact_mode", "auto") or "auto"
            try:
                job_output_storage = await resolve_job_output_storage(ctx=ctx, db=db, job=job)
            except Exception as exc:
                job_output_storage = None
                logger.bind(ctx=ctx, job_id=job.job_id).warning(
                    f"Failed to resolve Daytona output storage: {exc}"
                )
            mongo_status = (job.status or "running").strip()
            mongo_terminal = mongo_status in ("completed", "failed", "canceled")

            # For old terminal jobs, do NOT waste time polling the sandbox unless VNC is requested.
            # Status can be served from Mongo + S3 reconciliation (+ optional server_sync if sandbox still reachable).
            skip_sandbox_poll = bool(mongo_terminal and not action_config.need_vnc_link)

            poll_failed = False
            poll_skipped = False
            if skip_sandbox_poll:
                # We intentionally skip polling for already-terminal jobs (unless VNC requested).
                # We still allow downstream S3 reconciliation / server_sync checks to run.
                poll_skipped = True
                status = mongo_status
                log_tail = job.log_tail or ""
                artifacts = job.artifacts or {}
            else:
                try:
                    status, log_tail, artifacts = await self._poll_daytona_job(
                        ctx=ctx,
                        daytona=daytona_conn,
                        sandbox_id=job.sandbox_id,
                        job_workdir=getattr(job, "job_workdir", "") or ".",
                    )
                except Exception as e:
                    poll_failed = True
                    # sandbox may have been deleted/expired; don't crash the tool
                    logger.bind(ctx=ctx, job_id=job.job_id, sandbox_id=job.sandbox_id).warning(
                        f"Status: failed to poll sandbox (may be deleted/expired): {e}"
                    )
                    # Don't force FAILED on transient Daytona/network issues. Preserve last known status.
                    status = mongo_status or "running"
                    log_tail = (job.log_tail or "") + f"\n[WARN] failed to poll sandbox: {e}"
                    artifacts = job.artifacts or {}

            # Monotonic artifacts: never overwrite existing artifacts with {} from a running poll.
            update = {"status": status, "log_tail": log_tail}
            # Guaranteed revocation: a terminal job can no longer use the connections bridge.
            if _job_has_forwarded_connections(job) and status in (
                "completed",
                "failed",
                "canceled",
            ):
                update["connections_revoked"] = True
            # Minimal-safe policy: only write artifacts when we have a non-empty dict.
            # This prevents accidental regression where a terminal poll reads missing/empty artifacts.json and writes {} to Mongo.
            if artifacts:
                update["artifacts"] = artifacts
            await uj.update_by_job_id(ctx=ctx, job_id=job.job_id, update=update)

            # --- TEMP LOGS START (DAYTONA SANDBOX DIAG MIRROR) ---
            # Mirror sandbox S3 presigned diagnostics into server logs so they appear in Datadog.
            self._mirror_sandbox_temp_logs_to_server(
                ctx=ctx,
                job_id=job.job_id,
                sandbox_id=job.sandbox_id,
                log_tail=log_tail,
            )
            # --- TEMP LOGS END (DAYTONA SANDBOX DIAG MIRROR) ---

            # S3 reconciliation fallback:
            # Works even if sandbox expired, as long as bytes were uploaded to S3.
            declared = getattr(job, "output_files", None) or []
            expected = getattr(job, "expected_artifacts", None) or {}
            missing = [fn for fn in declared if fn and fn not in (artifacts or {})]

            if (
                job_output_storage
                and job_mode != "none"
                and (
                    (poll_failed or poll_skipped)
                    or (status in ("completed", "failed", "canceled") and missing)
                )
            ):
                try:
                    merged = await reconcile_artifacts_from_s3(
                        ctx=ctx,
                        storage=job_output_storage.storage,
                        s3_prefix=getattr(job, "s3_prefix", "") or "",
                        output_files=declared,
                        expected_artifacts=expected,
                        artifacts=artifacts or {},
                    )
                    if merged != (artifacts or {}):
                        artifacts = merged
                        await uj.update_by_job_id(
                            ctx=ctx, job_id=job.job_id, update={"artifacts": artifacts}
                        )
                        if (poll_failed or poll_skipped) and status == "running" and artifacts:
                            log_tail = (
                                log_tail or ""
                            ) + "\n[INFO] sandbox unavailable; recovered artifact URLs from S3"
                            await uj.update_by_job_id(
                                ctx=ctx, job_id=job.job_id, update={"log_tail": log_tail}
                            )
                except Exception:
                    pass

            # --- TEMP LOGS START (DAYTONA STATUS DIAG) ---
            logger.bind(
                ctx=ctx,
                job_id=job.job_id,
                sandbox_id=job.sandbox_id,
                status=status,
                artifact_mode=getattr(job, "artifact_mode", None),
                declared=getattr(job, "output_files", None),
                artifacts_keys=sorted(list((artifacts or {}).keys()))[:20],
            ).info("--- TEMP DAYTONA STATUS DIAG START --- polled status")
            # --- TEMP LOGS END (DAYTONA STATUS DIAG) ---

            # Server-side sync fallback:
            # If job is done but artifacts are missing (callback/presigned not usable due to network), download outputs and upload to S3.
            if status == "completed" and job_mode != "none" and job_output_storage:
                # Respect artifact_mode="none": never upload artifacts (but still allow cleanup below).
                declared2 = getattr(job, "output_files", None) or []
                missing2 = [fn for fn in declared2 if fn and fn not in (artifacts or {})]
                if missing2 and getattr(job, "s3_prefix", None):
                    # If polling failed, the sandbox might still exist (transient Daytona/toolbox issue).
                    # Probe quickly before attempting a potentially expensive server_sync.
                    if poll_failed or poll_skipped:
                        try:
                            code, out = await daytona_conn.exec(
                                ctx=ctx,
                                sandbox_id=job.sandbox_id,
                                command="echo OK",
                                timeout_sec=5,
                            )
                            if code != 0:
                                raise RuntimeError(out[:200] if out else "probe failed")
                        except Exception:
                            # Sandbox likely unavailable; don't attempt server_sync.
                            missing2 = []

                if missing2 and getattr(job, "s3_prefix", None):
                    artifacts = await self._server_sync_missing_artifacts(
                        ctx=ctx,
                        daytona_conn=daytona_conn,
                        uj=uj,
                        job_id=job.job_id,
                        sandbox_id=job.sandbox_id,
                        job_workdir=getattr(job, "job_workdir", "") or ".",
                        storage=job_output_storage.storage,
                        s3_prefix=job.s3_prefix,
                        artifact_mode=job_mode,
                        declared_outputs=declared2,
                        artifacts=artifacts,
                    )
                    # After server_sync, reconcile from S3 again to backfill/confirm.
                    try:
                        merged2 = await reconcile_artifacts_from_s3(
                            ctx=ctx,
                            storage=job_output_storage.storage,
                            s3_prefix=getattr(job, "s3_prefix", "") or "",
                            output_files=declared2,
                            expected_artifacts=expected,
                            artifacts=artifacts or {},
                        )
                        if merged2 != (artifacts or {}):
                            artifacts = merged2
                            await uj.update_by_job_id(
                                ctx=ctx, job_id=job.job_id, update={"artifacts": artifacts}
                            )
                    except Exception:
                        pass

            # Only offer VNC link if the sandbox will not be deleted in this same status call.
            will_delete = status in ("completed", "failed", "canceled") and cleanup_mode == "delete"
            if action_config.need_vnc_link and job.sandbox_id and not will_delete:
                try:
                    # If sandbox is reachable, don't block on start_sandbox().
                    # If unreachable, do a shorter best-effort wake-up (status should not hang for 120s).
                    reachable = False
                    try:
                        code, out = await daytona_conn.exec(
                            ctx=ctx,
                            sandbox_id=job.sandbox_id,
                            command="echo OK",
                            timeout_sec=5,
                        )
                        reachable = code == 0 and "OK" in (out or "")
                    except Exception:
                        reachable = False

                    if not reachable:
                        try:
                            await daytona_conn.start_sandbox(
                                ctx=ctx, sandbox_id=job.sandbox_id, timeout_sec=30
                            )
                        except DaytonaPermissionError:
                            vnc_error = (
                                "DAYTONA_RESUME_PERMISSION_REQUIRED: "
                                "The sandbox is stopped or archived and this "
                                "credential cannot resume it."
                            )
                            raise

                    await daytona_conn.ensure_vnc(
                        ctx=ctx,
                        sandbox_id=job.sandbox_id,
                        novnc_port=int(action_config.vnc_port),
                        wait_ready_sec=20,
                    )
                    vnc_url = await daytona_conn.get_signed_preview_url(
                        ctx=ctx,
                        sandbox_id=job.sandbox_id,
                        port=int(action_config.vnc_port),
                        expires_in_seconds=int(action_config.vnc_link_ttl_sec),
                    )
                    vnc_url = _novnc_ui_url(vnc_url, page="vnc.html")
                except Exception as e:
                    vnc_error = str(e)[:400]
            elif action_config.need_vnc_link and will_delete:
                vnc_warning = (
                    "sandbox will be deleted in this status call "
                    "(cleanup_mode=delete); not returning vnc_url"
                )

            # Cleanup only sandboxes created by this job. Reused sandboxes are
            # never automatically stopped or deleted.
            if (
                status in ("completed", "failed", "canceled")
                and cleanup_mode != "none"
                and not cleanup_completed
            ):
                try:
                    if getattr(job, "session_id", None) and job.sandbox_id:
                        await daytona_conn.delete_session(
                            ctx=ctx, sandbox_id=job.sandbox_id, session_id=job.session_id
                        )
                    if cleanup_mode == "delete" and job.sandbox_id:
                        await daytona_conn.delete_sandbox(ctx=ctx, sandbox_id=job.sandbox_id)
                    elif cleanup_mode == "stop" and job.sandbox_id:
                        await daytona_conn.stop_sandbox(ctx=ctx, sandbox_id=job.sandbox_id)
                    cleanup_completed = True
                    cleanup_error = ""
                    await uj.update_by_job_id(
                        ctx=ctx,
                        job_id=job.job_id,
                        update={
                            "cleanup_completed": True,
                            "cleanup_error": "",
                        },
                    )
                except Exception as e:
                    cleanup_error = str(e)[:500]
                    await uj.update_by_job_id(
                        ctx=ctx,
                        job_id=job.job_id,
                        update={
                            "cleanup_completed": False,
                            "cleanup_error": cleanup_error,
                        },
                    )
                    logger.bind(ctx=ctx, job_id=job.job_id, sandbox_id=job.sandbox_id).warning(
                        f"Failed to cleanup Daytona sandbox: {e}"
                    )
            elif (
                status in ("completed", "failed", "canceled")
                and cleanup_mode == "none"
                and not cleanup_completed
            ):
                # Reused sandboxes intentionally have no automatic cleanup.
                cleanup_completed = True
                cleanup_error = ""
                await uj.update_by_job_id(
                    ctx=ctx,
                    job_id=job.job_id,
                    update={
                        "cleanup_completed": True,
                        "cleanup_error": "",
                    },
                )

            response_artifacts = {}
            if job_output_storage:
                response_artifacts = await materialize_artifact_urls(
                    ctx=ctx,
                    storage=job_output_storage.storage,
                    storage_scope=job_output_storage.scope,
                    s3_prefix=job_output_storage.s3_prefix,
                    artifacts=artifacts or {},
                )
            artifact_urls = list(response_artifacts.values())
            return DataBlock(
                text=(
                    f"job_id={job.job_id}\n"
                    f"sandbox_id={job.sandbox_id}\n"
                    f"status={status}\n"
                    f"output_storage_scope={getattr(job, 'output_storage_scope', 'meetkiwi_public')}\n"
                    f"output_storage_connection_name={getattr(job, 'output_storage_connection_name', None) or ''}\n"
                    f"cleanup_mode={cleanup_mode}\n"
                    f"cleanup_completed={self._fmt_bool(cleanup_completed)}\n"
                    f"cleanup_error={cleanup_error}\n"
                    f"vnc_url={vnc_url or ''}\n"
                    f"vnc_error={vnc_error or ''}\n"
                    f"vnc_warning={vnc_warning or ''}\n\n"
                    f"{log_tail}"
                ),
                urls=artifact_urls,
                metadata=Metadata(),
            )

        # operation == "run"
        callback_base = (settings.DAYTONA_CALLBACK_BASE_URL or settings.APP_BASE_URL).strip()
        if (
            action_config.artifact_mode == "callback"
            and action_config.output_files
            and not callback_base
        ):
            return ValueError(
                "artifact_mode='callback' requires DAYTONA_CALLBACK_BASE_URL or APP_BASE_URL "
                "so the sandbox can call /v1/daytona/presign-put."
            )

        # Bridged connections need a reachable callback base. Materialized ones
        # (forward_connections, forward_databases_direct) go straight into the process
        # environment and reach the database or API directly, so they do not.
        if action_config.forward_databases and not callback_base:
            return ValueError(
                "Remote database connections require DAYTONA_CALLBACK_BASE_URL or "
                "APP_BASE_URL to be configured"
            )

        # The bridge's serving endpoints (/v1/daytona/connections/...) don't exist yet
        # (separate, not-yet-landed follow-up). Without this check the run would still
        # be accepted, the sandbox would be ownership-bound, and a connections token
        # would be minted for a feature that 404s the moment anything tries to use it
        # -- reject up front instead of letting that surprise show up mid-run.
        if action_config.forward_databases:
            return ValueError(
                "forward_databases is not available yet (the connections query bridge "
                "endpoint has not landed). Use forward_databases_direct for now, which "
                "materializes credentials directly and does not depend on it."
            )

        # Sanitize declared output filenames once; persisted to DB and used for presigned/server_sync.
        try:
            safe_output_files = [
                _sanitize_rel_posix_path(fn) for fn in (action_config.output_files or [])
            ]
        except Exception as e:
            return ValueError(f"Invalid output_files: {e}")

        job_id = f"dj_{gen_uuid()}"
        try:
            resolved_output_storage = await resolve_output_storage(
                ctx=ctx,
                db=db,
                user=user,
                requested_scope=action_config.output_storage_scope,
                connection_name=action_config.output_storage_connection_name,
                job_id=job_id,
            )
        except ValueError as exc:
            return exc
        output_storage = resolved_output_storage.storage
        s3_prefix = resolved_output_storage.s3_prefix

        expected_artifacts = await build_expected_artifacts(
            ctx=ctx,
            storage=output_storage,
            s3_prefix=s3_prefix,
            output_files=safe_output_files,
        )
        # create job token for sandbox uploads
        job_token = create_job_token(user_id=str(user.id), job_id=job_id, s3_prefix=s3_prefix)

        # If output_files provided, generate presigned PUT URLs for presigned/auto modes.
        # This allows uploads even when callback is not reachable.
        presigned_env: dict[str, str] = {}
        if safe_output_files and action_config.artifact_mode in ("auto", "presigned"):
            # Signed upload URLs are generated up-front for sandbox upload.
            # For long-running jobs, SigV4 presigned URLs support up to 7 days.
            # NOTE: effective maximum can still be constrained by credential expiration (e.g., short-lived STS creds).
            presigned_ttl_sec = 7 * 24 * 3600
            for i, fn in enumerate(safe_output_files):
                key = join_s3_key(s3_prefix, fn)
                signed = await output_storage.generate_signed_upload(
                    key=key,
                    expiration=presigned_ttl_sec,
                    upload_profile="daytona",
                )
                put_url = signed.url
                public_url = signed.public_url

                # --- TEMP LOGS START (DAYTONA PRESIGN DIAG) ---
                try:
                    pu = urlparse(put_url)
                    pr = {
                        "put_host": pu.netloc,
                        "put_path": pu.path,
                        "put_query_len": len(pu.query or ""),
                        "has_x_amz_sig": "X-Amz-Signature=" in (pu.query or ""),
                        "public_host": (urlparse(public_url).netloc if public_url else ""),
                    }
                    logger.bind(ctx=ctx, job_id=job_id, filename=fn, presign=pr).info(
                        "--- TEMP DAYTONA PRESIGN DIAG START --- generated presigned put/public urls"
                    )
                    logger.bind(ctx=ctx, job_id=job_id, filename=fn).info(
                        "--- TEMP DAYTONA PRESIGN DIAG END ---"
                    )
                except Exception:
                    pass
                # --- TEMP LOGS END (DAYTONA PRESIGN DIAG) ---

                presigned_env[f"AUTOBOTS_PRESIGNED_PUT_{i}"] = put_url
                presigned_env[f"AUTOBOTS_PRESIGNED_PUBLIC_{i}"] = public_url
                presigned_env[f"AUTOBOTS_PRESIGNED_HEADERS_{i}"] = json.dumps(
                    signed.headers,
                    separators=(",", ":"),
                )

        # sandbox_env is passed to create_sandbox(env_vars=...), which is sandbox-WIDE and
        # persistent: every future process in that sandbox inherits it. No forwarded
        # connection credential (app auth, direct database URL, bridge token) is ever put
        # here -- those go only into process_env (built below), the per-command environment
        # handed to start_background(env=...).
        # This does not make sandbox_env credential-free: it still carries the pre-existing
        # artifact job token and the presigned upload URLs (TTLs up to 7 days), as it did
        # before connection forwarding existed. That is a separate, deliberately narrow
        # exposure -- a leaked artifact token only authorizes presigned PUTs within this
        # one job's own S3 prefix -- and is out of scope here.
        sandbox_env = {
            "AUTOBOTS_JOB_ID": job_id,
            "AUTOBOTS_JOB_TOKEN": job_token,
            "AUTOBOTS_ARTIFACT_MODE": action_config.artifact_mode,
            # If callback_base is empty, keep API_BASE empty (runner will skip callback).
            "AUTOBOTS_API_BASE": (
                callback_base.rstrip("/")
                + (getattr(settings, "API_v1", None) or getattr(settings, "API_V1", None) or "/v1")
                + "/daytona"
                if callback_base
                else ""
            ),
        }
        sandbox_env.update(presigned_env)

        # Optional pip tuning passed from tool caller -> sandbox runner via env vars.
        # If these are not set, runner uses its defaults.
        if action_config.pip_wall_timeout_sec is not None:
            sandbox_env["AUTOBOTS_PIP_WALL_TIMEOUT_SEC"] = str(
                int(action_config.pip_wall_timeout_sec)
            )
        if action_config.pip_socket_timeout_sec is not None:
            sandbox_env["AUTOBOTS_PIP_SOCKET_TIMEOUT_SEC"] = str(
                int(action_config.pip_socket_timeout_sec)
            )
        if action_config.pip_retries is not None:
            sandbox_env["AUTOBOTS_PIP_RETRIES"] = str(int(action_config.pip_retries))

        # For now we default DISPLAY=:0 when VNC is requested (we will validate :0 vs :1 in a probe script).
        if action_config.need_vnc_link:
            sandbox_env.setdefault("DISPLAY", ":0")

        # --- Connection forwarding (AUT-1593) ---
        # Forwarded connection credentials live only in process_env, which is handed to a
        # single command; none of them are written to disk or persisted sandbox-wide.
        has_forwarded = bool(
            action_config.forward_connections
            or action_config.forward_databases
            or action_config.forward_databases_direct
        )
        credential_space_id = (
            user.space_member.space_id if getattr(user, "space_member", None) else None
        )
        process_env = dict(sandbox_env)
        if action_config.forward_connections:
            process_env.update(
                await resolve_forwardable_env(
                    ctx=ctx,
                    db=db,
                    user=user,
                    connection_names=action_config.forward_connections,
                )
            )
        if action_config.forward_databases_direct:
            # Pure materialization, exactly like AWS above: the sandbox connects with
            # its own DB client, so this needs no bridge, callback base or token.
            process_env.update(
                await resolve_direct_database_env(
                    ctx=ctx,
                    db=db,
                    user=user,
                    connection_names=action_config.forward_databases_direct,
                )
            )
        if action_config.forward_databases:
            # Token outlives the declared runtime by a small grace window; terminal job
            # status and the runner's eager revoke close it earlier in practice.
            connections_ttl_sec = int(action_config.max_runtime_minutes) * 60 + 600
            process_env["AUTOBOTS_CONNECTIONS_URL"] = (
                sandbox_env["AUTOBOTS_API_BASE"] + "/connections"
            )
            process_env["AUTOBOTS_CONNECTIONS_TOKEN"] = create_connections_token(
                user_id=str(user.id), job_id=job_id, ttl_seconds=connections_ttl_sec
            )

        # Sandbox acquisition (new vs reuse)
        reuse_id = (action_config.reuse_sandbox_id or "").strip()
        reused = bool(reuse_id)
        if reused:
            try:
                await daytona_conn.start_sandbox(ctx=ctx, sandbox_id=reuse_id, timeout_sec=120)
            except DaytonaPermissionError:
                return DataBlock(
                    text=(
                        "Error [DAYTONA_RESUME_PERMISSION_REQUIRED]: "
                        "The requested sandbox is stopped or archived, but this "
                        "Daytona credential cannot resume it. Use a credential "
                        "with Sandbox Write permission, or ask the sandbox owner "
                        "or support to start it."
                    ),
                    metadata=Metadata(),
                )
            except Exception as e:
                logger.bind(ctx=ctx, sandbox_id=reuse_id).warning(
                    f"reuse_sandbox_id: start_sandbox failed (will fall back to exec probe): {e}"
                )
            # Confirm reachability via exec (Toolbox fallback works cross-worker).
            try:
                code, out = await daytona_conn.exec(
                    ctx=ctx, sandbox_id=reuse_id, command="echo OK", timeout_sec=15
                )
                if code != 0:
                    raise RuntimeError(out[:200] if out else "probe failed")
            except Exception as e:
                return DataBlock(
                    text=(
                        f"Error: reuse_sandbox_id={reuse_id} is not reachable. "
                        f"Tried programmatic start + exec probe and still failed. err={e}"
                    ),
                    metadata=Metadata(),
                )

        # -------------------------
        # CPU/RAM sizing
        # -------------------------
        # NOTE:
        # We persist requested_cpu/requested_memory_gib in the job doc for audit/debug.
        # However, compute sizing is best-effort (quota/limits/backend availability), actual resources may be smaller.
        # Phase-1 policy: if compute sizing cannot be applied, we continue with the already-provisioned sandbox size.
        snapshot: str | None = None
        requested_cpu: int | None = action_config.cpu
        requested_mem: int | None = action_config.memory_gib

        prof = action_config.compute_profile or "default"
        if prof in _DAYTONA_COMPUTE_PRESETS:
            preset = _DAYTONA_COMPUTE_PRESETS[prof]
            snapshot = str(preset["snapshot"])  # type: ignore[assignment]
            requested_cpu = int(preset["cpu"])  # type: ignore[assignment]
            requested_mem = int(preset["memory_gib"])  # type: ignore[assignment]
        elif prof == "custom":
            # Ensure we have a consistent baseline that can always be resized upward.
            snapshot = "daytona-small"
        else:
            snapshot = None

        # Validate against configured caps (defaults match Daytona org default limits).
        max_cpu = int(getattr(settings, "DAYTONA_MAX_CPU", 4) or 4)
        max_mem = int(getattr(settings, "DAYTONA_MAX_MEMORY_GIB", 8) or 8)
        if requested_cpu is not None and not (1 <= int(requested_cpu) <= max_cpu):
            return DataBlock(
                text=f"Error: cpu must be between 1 and {max_cpu}.", metadata=Metadata()
            )
        if requested_mem is not None and not (1 <= int(requested_mem) <= max_mem):
            return DataBlock(
                text=f"Error: memory_gib must be between 1 and {max_mem}.", metadata=Metadata()
            )

        # Create sandbox:
        # - Presets (small/medium/large): snapshot tier already encodes sizing => do NOT pass cpu/memory to client.
        # - Custom: pass cpu/memory to client => it will prefer create-time image+resources sizing,
        #   and may fall back to resize/snapshot behavior depending on SDK/deployment support.
        cpu_for_client: int | None = None
        mem_for_client: int | None = None
        if prof == "custom":
            cpu_for_client = requested_cpu
            mem_for_client = requested_mem

        lifecycle_policy = None if reused else _derive_lifecycle_policy(action_config, settings)

        if reused:
            # lightweight shim with .sandbox_id field (we only need sb.sandbox_id from here onward)
            sb = type("SB", (), {"sandbox_id": reuse_id})()
        else:
            try:
                assert lifecycle_policy is not None
                sb = await daytona_conn.create_sandbox(
                    ctx=ctx,
                    language=action_config.language,
                    env_vars=sandbox_env,
                    snapshot=snapshot,
                    cpu=cpu_for_client,
                    memory_gib=mem_for_client,
                    ephemeral=lifecycle_policy.ephemeral,
                    ttl_minutes=lifecycle_policy.ttl_minutes,
                    auto_stop_interval=lifecycle_policy.auto_stop_interval,
                    auto_archive_interval=lifecycle_policy.auto_archive_interval,
                    auto_delete_interval=lifecycle_policy.auto_delete_interval,
                )
            except DaytonaPermissionError:
                return DataBlock(
                    text=(
                        "Error [DAYTONA_CREATE_PERMISSION_REQUIRED]: "
                        "This Daytona credential cannot create sandboxes. "
                        "It can only reuse an existing started sandbox. "
                        "Provide reuse_sandbox_id. To create a new sandbox, "
                        "use a credential with Sandbox Write permission or contact support."
                    ),
                    metadata=Metadata(),
                )

        # Sandbox-subject binding (AUT-1593): once a sandbox has run with forwarded
        # connections it stays bound to that credential subject, so leftover state from
        # one user's connection-enabled run can never be picked up by another user's run
        # on the same (reused/persistent) sandbox.
        #
        # NOTE: this carries a small, accepted TOCTOU race - two concurrent first-uses of
        # the same brand-new sandbox could both pass. Deliberately not closed with a
        # distributed lock in this pass: each customer runs on their own isolated Daytona
        # account/sandbox, so the exposure does not justify that infrastructure.
        #
        # The check also runs on ANY reuse, not only when this call forwards something:
        # a bound sandbox may still hold a previous forwarding-enabled process's
        # credentials in its environment and memory, so a different user must not be
        # able to reuse it just by omitting forward_* from their own request. A sandbox
        # that was never bound has no such document and passes through untouched.
        if has_forwarded or reused:
            prior = await db[DaytonaJobDoc.__collection__].find_one(
                {"sandbox_id": sb.sandbox_id, "sandbox_owner_user_id": {"$ne": None}},
                sort=[("created_at", -1)],
            )
            if prior and prior["sandbox_owner_user_id"] != str(user.id):
                logger.bind(ctx=ctx, job_id=job_id, sandbox_id=sb.sandbox_id).warning(
                    "Refusing to run in a sandbox bound to another user's connections"
                )
                return DataBlock(
                    text="Error: this sandbox is bound to a different user's connections.",
                    metadata=Metadata(),
                )

        # Optional: prepare VNC early so a human can connect immediately while the runner starts.
        vnc_url: str | None = None
        vnc_error: str | None = None
        vnc_warning: str | None = None
        if action_config.need_vnc_link:
            try:
                await daytona_conn.ensure_vnc(
                    ctx=ctx,
                    sandbox_id=sb.sandbox_id,
                    novnc_port=int(action_config.vnc_port),
                    wait_ready_sec=20,
                )
                vnc_url = await daytona_conn.get_signed_preview_url(
                    ctx=ctx,
                    sandbox_id=sb.sandbox_id,
                    port=int(action_config.vnc_port),
                    expires_in_seconds=int(action_config.vnc_link_ttl_sec),
                )
                vnc_url = _novnc_ui_url(vnc_url, page="vnc.html")
            except Exception as e:
                vnc_error = str(e)[:400]

        # write runner script
        # Daytona-safe input downloads:
        # For each input URL, resolve which storage owns it and presign private
        # objects so the sandbox runner can download them with a plain
        # `requests.get(...)` (no AWS credentials forwarded into the sandbox).
        # - MeetKiwi public: pass through (optionally rewritten to path-style)
        # - MeetKiwi private: presign GET with deployment creds
        # - Customer S3: presign GET with the matching customer AWS connection
        # - External URL: pass through unchanged
        allowed_buckets = {
            settings.AWS_S3_BUCKET_NAME,
            settings.AWS_S3_PUBLIC_BUCKET_NAME,
        }
        daytona_input_urls: List[str] = []
        for u in action_config.input_urls or []:
            try:
                resolved = await resolve_storage_for_url(url=u, ctx=ctx, db=db, user=user)
            except Exception as exc:
                logger.bind(ctx=ctx, input_url=u).warning(
                    f"Failed to resolve storage for Daytona input URL; passing through unchanged: {exc}"
                )
                resolved = None

            if resolved is None:
                # External URL: apply the existing path-style rewrite for our own
                # public buckets (best-effort), otherwise pass through as-is.
                daytona_input_urls.append(
                    _make_daytona_safe_s3_get_url(
                        u, region=settings.AWS_S3_BUCKET_REGION, allowed_buckets=allowed_buckets
                    )
                )
            elif resolved.scope == "meetkiwi_public":
                # Public object: no presigning needed, keep the path-style rewrite.
                daytona_input_urls.append(
                    _make_daytona_safe_s3_get_url(
                        u, region=settings.AWS_S3_BUCKET_REGION, allowed_buckets=allowed_buckets
                    )
                )
            else:
                # Private object (MeetKiwi private or customer S3): presign a
                # short-lived GET URL. The runner fetches it via plain HTTP.
                presigned = await resolved.storage.generate_presigned_get_url(
                    resolved.key, expiration=21600
                )
                logger.bind(
                    ctx=ctx,
                    input_url=u,
                    storage_scope=resolved.scope,
                    connection_name=resolved.connection_name,
                ).info("Presigned private Daytona input URL")
                daytona_input_urls.append(presigned)

        runner_py = self._build_runner_py(
            code=action_config.code or "",
            pip_packages=action_config.pip_packages,
            input_urls=daytona_input_urls,
            output_files=safe_output_files,
        ).encode("utf-8")

        job_workdir = f"/home/daytona/.autobots_jobs/{job_id}"

        # persist job doc BEFORE the runner exists.
        # A runner can hit the connections bridge (or /connections/revoke) within
        # milliseconds of starting; if the job document is not in Mongo yet those calls
        # 404, and an early revoke is a silent no-op that is never retried. The document
        # is therefore inserted as "queued" here and transitioned to "running" once the
        # process has actually been launched below.
        doc = DaytonaJobDocCreate(
            user_id=str(user.id),
            job_id=job_id,
            sandbox_id=sb.sandbox_id,
            # Legacy field retained for old readers only.
            keep_sandbox=bool(reused or action_config.lifecycle != "ephemeral"),
            reused_sandbox_id=reuse_id if reused else None,
            reused_sandbox=reused,
            created_sandbox=not reused,
            lifecycle="existing" if reused else action_config.lifecycle,
            job_workdir=job_workdir,
            cleanup_completed=False,
            cleanup_error="",
            requested_cpu=requested_cpu,
            requested_memory_gib=requested_mem,
            status="queued",
            language=action_config.language,
            pip_packages=action_config.pip_packages,
            input_urls=action_config.input_urls,
            output_files=safe_output_files,
            output_storage_scope=resolved_output_storage.scope,
            output_storage_connection_name=resolved_output_storage.connection_name,
            s3_prefix=s3_prefix,
            # Only known once the runner has actually been started.
            session_id=None,
            runner_pid=None,
            log_tail="",
            artifacts={},
            expected_artifacts=expected_artifacts,
            artifact_mode=action_config.artifact_mode,
            # Connection forwarding (AUT-1593)
            sandbox_owner_user_id=str(user.id) if has_forwarded else None,
            connections_revoked=False,
            credential_space_id=credential_space_id,
            forward_databases=list(action_config.forward_databases or []),
        )
        await uj.create(ctx=ctx, doc=doc)

        async def _mark_job_failed(error_text: str) -> None:
            """
            Best-effort terminal update for a failure between the "queued" insert and
            the "running" transition, so the job never stays stuck at "queued" and the
            connections bridge stops serving it.
            """
            failure_update: dict = {
                "status": "failed",
                "error_message": error_text[:500],
                "log_tail": error_text[:500],
            }
            if has_forwarded:
                failure_update["connections_revoked"] = True
            try:
                await uj.update_by_job_id(ctx=ctx, job_id=job_id, update=failure_update)
            except Exception as upd_err:
                logger.bind(ctx=ctx, job_id=job_id).warning(
                    f"Failed to record job failure in Mongo: {upd_err}"
                )

        # start in background and return quickly
        # writes run.log, done.json, artifacts.json in cwd
        session_id: str | None = None
        started_marker: str = ""
        pid: str | None = None
        normalized_pid: str | None = None
        runner_cmd = "python3 -u runner.py > run.log 2>&1"
        try:
            mkdir_code, mkdir_out = await daytona_conn.exec(
                ctx=ctx,
                sandbox_id=sb.sandbox_id,
                command=f"mkdir -p {job_workdir}",
                timeout_sec=30,
            )
            if mkdir_code != 0:
                mkdir_err = f"Error: failed to create job workdir={job_workdir}: {mkdir_out[:300]}"
                await _mark_job_failed(mkdir_err)
                return DataBlock(text=mkdir_err, metadata=Metadata())

            # Prefer Daytona SDK fs.upload_file (avoids command-length limits and shell quoting issues).
            # Falls back internally if fs.upload_file is not available.
            await daytona_conn.write_file(
                ctx=ctx,
                sandbox_id=sb.sandbox_id,
                path=f"{job_workdir}/runner.py",
                content=runner_py,
            )

            if reused or has_forwarded:
                # Reuse mode: inject env per-command (no secrets written to disk; no reliance on create-time env_vars).
                # Forwarded connections use the same path even on a brand-new sandbox: the
                # session path below cannot carry a per-command environment, so its only
                # source of environment is the persistent sandbox-wide env_vars.
                pid = await daytona_conn.start_background(
                    ctx=ctx,
                    sandbox_id=sb.sandbox_id,
                    command=_RUNNER_START_CMD,
                    env=process_env,
                    cwd=job_workdir,
                )
                started_marker = (
                    f"reused sandbox; started pid={pid}" if reused else f"started pid={pid}"
                )
            else:
                try:
                    session_id = await daytona_conn.create_session(
                        ctx=ctx, sandbox_id=sb.sandbox_id
                    )
                    cmd_id = await daytona_conn.execute_session_command(
                        ctx=ctx,
                        sandbox_id=sb.sandbox_id,
                        session_id=session_id,
                        command=f"cd {job_workdir} && {runner_cmd}",
                    )
                    # cmd_id is useful for future enhancements (polling via get_session_command/get_session_command_logs).
                    started_marker = f"started session_id={session_id} cmd_id={cmd_id}"
                except NotImplementedError:
                    # Fallback: Avoid hard dependency on nohup/setsid; not all minimal
                    # images include them (see _RUNNER_START_CMD).
                    pid = await daytona_conn.start_background(
                        ctx=ctx,
                        sandbox_id=sb.sandbox_id,
                        command=_RUNNER_START_CMD,
                        cwd=job_workdir,
                    )
                    started_marker = f"started pid={pid}"
        except Exception as start_err:
            await _mark_job_failed(f"Error: failed to start runner: {start_err}")
            raise

        # The runner process now exists: promote the job out of "queued".
        running_update: dict = {
            "status": "running",
            "log_tail": started_marker,
            "session_id": session_id,
        }
        if pid is not None:
            # Only the background-process paths have a PID; cancel uses it to terminate
            # a runner in a sandbox this job does not own the lifecycle of.
            # Store only a clean numeric PID: cancel calls int() on this value, so a
            # malformed capture must be dropped here rather than break cancel later.
            normalized_pid = _normalize_pid(str(pid))
            if normalized_pid is not None:
                running_update["runner_pid"] = normalized_pid
            else:
                logger.bind(ctx=ctx, job_id=job_id, raw_pid=str(pid)).warning(
                    "Could not parse runner PID from background start output; "
                    "cancel will not be able to terminate the runner process."
                )
        await uj.update_by_job_id(ctx=ctx, job_id=job_id, update=running_update)

        # optionally wait a bit
        if action_config.wait_for_completion_sec > 0:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + action_config.wait_for_completion_sec
            try:
                while True:
                    if loop.time() >= deadline:
                        break
                    status, log_tail, artifacts = await self._poll_daytona_job(
                        ctx=ctx,
                        daytona=daytona_conn,
                        sandbox_id=sb.sandbox_id,
                        job_workdir=job_workdir,
                    )
                    upd = {"status": status, "log_tail": log_tail}
                    # Minimal-safe monotonic artifacts: only write when non-empty.
                    if artifacts:
                        upd["artifacts"] = artifacts
                    # Guaranteed revocation: a terminal job can no longer use the bridge.
                    if has_forwarded and status in ("completed", "failed", "canceled"):
                        upd["connections_revoked"] = True
                    await uj.update_by_job_id(ctx=ctx, job_id=job_id, update=upd)
                    if status in ("completed", "failed"):
                        # --- TEMP LOGS START (DAYTONA SANDBOX DIAG MIRROR) ---
                        # Mirror sandbox S3 presigned diagnostics into server logs so they appear in Datadog.
                        self._mirror_sandbox_temp_logs_to_server(
                            ctx=ctx,
                            job_id=job_id,
                            sandbox_id=sb.sandbox_id,
                            log_tail=log_tail,
                        )
                        # --- TEMP LOGS END (DAYTONA SANDBOX DIAG MIRROR) ---

                        # Important: short runs that complete within wait_for_completion_sec should behave like operation="status".
                        # If artifacts are missing due to presigned/callback issues, attempt server_sync BEFORE deleting sandbox.
                        if status == "completed":
                            artifacts = await self._server_sync_missing_artifacts(
                                ctx=ctx,
                                daytona_conn=daytona_conn,
                                uj=uj,
                                job_id=job_id,
                                sandbox_id=sb.sandbox_id,
                                job_workdir=job_workdir,
                                storage=output_storage,
                                s3_prefix=s3_prefix,
                                artifact_mode=action_config.artifact_mode or "auto",
                                declared_outputs=safe_output_files,
                                artifacts=artifacts,
                            )
                            # Backfill from S3 in case runner uploaded but did not record artifacts.json.
                            try:
                                merged = await reconcile_artifacts_from_s3(
                                    ctx=ctx,
                                    storage=output_storage,
                                    s3_prefix=s3_prefix,
                                    output_files=safe_output_files,
                                    expected_artifacts=expected_artifacts,
                                    artifacts=artifacts or {},
                                )
                                if merged != (artifacts or {}):
                                    artifacts = merged
                                    await uj.update_by_job_id(
                                        ctx=ctx, job_id=job_id, update={"artifacts": artifacts}
                                    )
                            except Exception:
                                pass

                        # Best-effort cleanup: match operation="status" pattern (delete session then sandbox).
                        cleanup_mode = _cleanup_mode(
                            created_sandbox=not reused,
                            lifecycle="existing" if reused else action_config.lifecycle,
                        )
                        cleanup_completed = cleanup_mode == "none"
                        cleanup_error = ""
                        try:
                            if session_id and sb.sandbox_id:
                                await daytona_conn.delete_session(
                                    ctx=ctx, sandbox_id=sb.sandbox_id, session_id=session_id
                                )
                            if cleanup_mode == "delete":
                                await daytona_conn.delete_sandbox(ctx=ctx, sandbox_id=sb.sandbox_id)
                            elif cleanup_mode == "stop":
                                await daytona_conn.stop_sandbox(ctx=ctx, sandbox_id=sb.sandbox_id)
                            cleanup_completed = True
                        except Exception as e:
                            cleanup_error = str(e)[:500]
                            logger.bind(ctx=ctx, job_id=job_id, sandbox_id=sb.sandbox_id).warning(
                                f"Failed to cleanup Daytona sandbox after run completion: {e}"
                            )

                        await uj.update_by_job_id(
                            ctx=ctx,
                            job_id=job_id,
                            update={
                                "cleanup_completed": cleanup_completed,
                                "cleanup_error": cleanup_error,
                            },
                        )

                        text = (
                            f"job_id={job_id}\n"
                            f"sandbox_id={sb.sandbox_id}\n"
                            f"status={status}\n"
                            f"reused_sandbox={self._fmt_bool(reused)}\n"
                            f"lifecycle={'existing' if reused else action_config.lifecycle}\n"
                            f"output_storage_scope={resolved_output_storage.scope}\n"
                            f"output_storage_connection_name={resolved_output_storage.connection_name or ''}\n"
                            f"cleanup_mode={cleanup_mode}\n"
                            f"cleanup_completed={self._fmt_bool(cleanup_completed)}\n"
                            f"cleanup_error={cleanup_error}\n"
                            f"vnc_url={(vnc_url if cleanup_mode != 'delete' else '') or ''}\n"
                            f"vnc_error={vnc_error or ''}\n"
                            f"vnc_warning={vnc_warning or ''}\n\n"
                            f"{log_tail}"
                        )
                        response_artifacts = await materialize_artifact_urls(
                            ctx=ctx,
                            storage=output_storage,
                            storage_scope=resolved_output_storage.scope,
                            s3_prefix=s3_prefix,
                            artifacts=artifacts or {},
                        )
                        return DataBlock(
                            text=text,
                            urls=list(response_artifacts.values()),
                            metadata=Metadata(),
                        )
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                # Cancellation takes the same lifecycle-aware cleanup path as normal
                # completion, and records the outcome so a canceled job never stays
                # stuck with cleanup_completed=False.
                cancel_cleanup_mode = _cleanup_mode(
                    created_sandbox=not reused,
                    lifecycle="existing" if reused else action_config.lifecycle,
                )
                cancel_cleanup_completed = cancel_cleanup_mode == "none"
                cancel_cleanup_error = ""
                try:
                    if session_id and sb.sandbox_id:
                        await daytona_conn.delete_session(
                            ctx=ctx, sandbox_id=sb.sandbox_id, session_id=session_id
                        )
                    if cancel_cleanup_mode == "delete":
                        await daytona_conn.delete_sandbox(ctx=ctx, sandbox_id=sb.sandbox_id)
                    elif cancel_cleanup_mode == "stop":
                        await daytona_conn.stop_sandbox(ctx=ctx, sandbox_id=sb.sandbox_id)
                    elif (
                        cancel_cleanup_mode == "none"
                        and has_forwarded
                        and normalized_pid
                        and sb.sandbox_id
                    ):
                        # Same reasoning as operation="cancel": the sandbox survives (we
                        # don't own its lifecycle), but the runner still holds forwarded
                        # credentials in its process environment. Kill it directly rather
                        # than leave them live until the process exits on its own.
                        # Best-effort: never let this block re-raising the cancellation.
                        try:
                            pid_int = int(normalized_pid)
                            kill_cmd = (
                                f'if [ -d "/proc/{pid_int}" ] && '
                                f'[ "$(readlink -f "/proc/{pid_int}/cwd" 2>/dev/null)" '
                                f'= "{job_workdir}" ]; then '
                                f"kill -9 -- -{pid_int} 2>/dev/null; "
                                f"kill -9 -- {pid_int} 2>/dev/null; "
                                f"fi; true"
                            )
                            await daytona_conn.exec(
                                ctx=ctx,
                                sandbox_id=sb.sandbox_id,
                                command=kill_cmd,
                                timeout_sec=10,
                            )
                        except Exception as e:
                            logger.bind(ctx=ctx, job_id=job_id, runner_pid=normalized_pid).warning(
                                f"Failed to terminate runner process on cancellation: {e}"
                            )
                    cancel_cleanup_completed = True
                except Exception as e:
                    cancel_cleanup_error = str(e)[:500]
                    logger.bind(ctx=ctx, job_id=job_id, sandbox_id=sb.sandbox_id).warning(
                        f"Failed to cleanup Daytona sandbox after cancellation: {e}"
                    )
                try:
                    cancel_update = {
                        "cleanup_completed": cancel_cleanup_completed,
                        "cleanup_error": cancel_cleanup_error,
                    }
                    # Guaranteed revocation: a canceled job can no longer use the bridge,
                    # same as the normal-completion and explicit-cancel-op paths.
                    if has_forwarded:
                        cancel_update["connections_revoked"] = True
                    await uj.update_by_job_id(ctx=ctx, job_id=job_id, update=cancel_update)
                except Exception as e:
                    logger.bind(ctx=ctx, job_id=job_id, sandbox_id=sb.sandbox_id).warning(
                        f"Failed to persist cleanup state after cancellation: {e}"
                    )
                raise

        # still running
        return DataBlock(
            text=(
                f"job_id={job_id}\n"
                f"sandbox_id={sb.sandbox_id}\n"
                f"status=running\n"
                f"reused_sandbox={self._fmt_bool(reused)}\n"
                f"lifecycle={'existing' if reused else action_config.lifecycle}\n"
                f"output_storage_scope={resolved_output_storage.scope}\n"
                f"output_storage_connection_name={resolved_output_storage.connection_name or ''}\n"
                f"vnc_url={vnc_url or ''}\n"
                f"vnc_error={vnc_error or ''}\n"
                f"vnc_warning={vnc_warning or ''}\n\n"
                "Job started. Call daytona_sandbox again with operation='status' and this job_id to poll."
            ),
            metadata=Metadata(),
        )

    async def _poll_daytona_job(
        self,
        ctx: Context,
        daytona,
        sandbox_id: str,
        job_workdir: str,
    ) -> tuple[str, str, dict]:
        # check done marker
        _, done_flag = await daytona.exec(
            ctx=ctx,
            sandbox_id=sandbox_id,
            command="test -f done.json && echo DONE || echo RUNNING",
            cwd=job_workdir,
            timeout_sec=20,
        )
        done_flag = done_flag.strip()

        # tail logs
        _, log_tail = await daytona.exec(
            ctx=ctx,
            sandbox_id=sandbox_id,
            command="tail -n 200 run.log 2>/dev/null || true",
            cwd=job_workdir,
            timeout_sec=20,
        )
        log_tail = log_tail.strip()

        artifacts = {}
        if done_flag == "DONE":
            # read done.json to decide success/fail
            _, done_json = await daytona.exec(
                ctx=ctx,
                sandbox_id=sandbox_id,
                command="cat done.json 2>/dev/null || true",
                cwd=job_workdir,
                timeout_sec=20,
            )
            status = "completed"
            try:
                done = json.loads(done_json) if done_json else {}
                if int(done.get("exit_code", 0)) != 0:
                    status = "failed"
            except Exception:
                pass

            # read artifacts if exists
            _, artifacts_json = await daytona.exec(
                ctx=ctx,
                sandbox_id=sandbox_id,
                command="cat artifacts.json 2>/dev/null || true",
                cwd=job_workdir,
                timeout_sec=20,
            )
            try:
                artifacts = json.loads(artifacts_json) if artifacts_json else {}
                if not isinstance(artifacts, dict):
                    artifacts = {}
            except Exception:
                artifacts = {}

            return status, log_tail, artifacts

        return "running", log_tail, {}

    def _build_runner_py(
        self, code: str, pip_packages: List[str], input_urls: List[str], output_files: List[str]
    ) -> str:
        """
        Runner runs inside the sandbox:
        - installs deps
        - downloads inputs
        - executes user code (in-process)
        - uploads declared outputs via presign callback
        - writes done.json + artifacts.json
        """
        # Keep it simple and robust; user code runs in same python process for now.
        # If you want stricter isolation later, write code to user_code.py and subprocess python3 user_code.py.
        return f"""\
import json, os, sys, subprocess, time, urllib.parse

JOB_ID = os.environ.get("AUTOBOTS_JOB_ID", "")
JOB_TOKEN = os.environ.get("AUTOBOTS_JOB_TOKEN", "")
API_BASE = os.environ.get("AUTOBOTS_API_BASE", "")  # .../v1/daytona

# Connections bridge (only set when this run forwards database connections).
CONNECTIONS_URL = os.environ.get("AUTOBOTS_CONNECTIONS_URL", "")  # .../v1/daytona/connections
CONNECTIONS_TOKEN = os.environ.get("AUTOBOTS_CONNECTIONS_TOKEN", "")

# NOTE:
# These AUTOBOTS_PIP_* values are read inside the SANDBOX runner (not the API server).
# They can be overridden by passing them via `env_vars` when creating the Daytona sandbox.
# The daytona_sandbox tool exposes pip_wall_timeout_sec/pip_socket_timeout_sec/pip_retries and,
# when provided by the tool caller, passes them through as AUTOBOTS_PIP_* env vars.
PIP_WALL_TIMEOUT_SEC = int(os.environ.get("AUTOBOTS_PIP_WALL_TIMEOUT_SEC", "300"))
PIP_SOCKET_TIMEOUT_SEC = int(os.environ.get("AUTOBOTS_PIP_SOCKET_TIMEOUT_SEC", "60"))
PIP_RETRIES = int(os.environ.get("AUTOBOTS_PIP_RETRIES", "3"))
PIP_TAIL_LINES = int(os.environ.get("AUTOBOTS_PIP_TAIL_LINES", "120"))

# IMPORTANT:
# pip can "succeed" but install into a location not visible on sys.path (e.g. user-site disabled).
# To make imports deterministic, install into a known target directory and prepend it to sys.path.
#
# Also: pip's --target does not overwrite existing files unless --upgrade is used.
# To avoid staleness across retries, we default to a per-job target directory.
PIP_TARGET_DIR = os.environ.get(
    "AUTOBOTS_PIP_TARGET_DIR",
    os.path.join(os.getcwd(), ".autobots_pip", (JOB_ID or "job")),
)
PIP_TARGET_DIR = os.path.abspath(PIP_TARGET_DIR)
os.makedirs(PIP_TARGET_DIR, exist_ok=True)
if PIP_TARGET_DIR not in sys.path:
    sys.path.insert(0, PIP_TARGET_DIR)

# MAINTAINER NOTE (pip --target rationale and caveats)
# ----------------------------------------------------
# We intentionally install with `pip install --target <dir>` and prepend that dir to sys.path.
# This avoids a known failure mode in constrained environments where pip "succeeds" but installs
# into a location that is not importable by the current interpreter (user-site disabled / permission
# fallback / interpreter mismatch).
#
# Caveats (why the flags below exist):
# - pip `--target` does not overwrite existing files unless `--upgrade` is used.
# - without `--ignore-installed`, pip may say "already satisfied" due to system site-packages and
#   skip populating the target dir, which defeats the point of using --target.
# - namespace packages can be tricky if multiple installs reuse the same target; we mitigate by using a
#   per-job target dir (based on JOB_ID) and `--upgrade`.
# - `.pth` files inside the target dir are not processed like site-packages at interpreter startup.
#   If we ever hit a package that relies on `.pth` behavior, consider switching to a per-job venv
#   or baking stable deps into the Daytona snapshot/image.

def _tail(text, n=PIP_TAIL_LINES):
    lines = (text or "").splitlines()
    return "\\n".join(lines[-n:])

def _decode_maybe(x):
    if x is None:
        return ""
    if isinstance(x, bytes):
        return x.decode("utf-8", errors="replace")
    return str(x)

def _pip_install(pkgs):
    \"\"\"Robust, bounded pip install.
    We need both:
      - pip socket/retry controls (network robustness)
      - a wall-clock subprocess timeout (prevents indefinite hangs in resolver/build/etc.)
    \"\"\"
    if not pkgs:
        return
    pkgs = [p for p in pkgs if p and isinstance(p, str)]
    if not pkgs:
        return

    cmd = [
        sys.executable, "-m", "pip", "install",
        "--target", PIP_TARGET_DIR,
        "--upgrade",
        "--ignore-installed",
        "--no-warn-script-location",
        "--quiet",
        "--disable-pip-version-check",
        "--no-input",
        "--progress-bar", "off",
        "--retries", str(PIP_RETRIES),
        "--timeout", str(PIP_SOCKET_TIMEOUT_SEC),
        "--no-cache-dir",
        "--prefer-binary",
    ] + pkgs

    print("[setup] pip target:", PIP_TARGET_DIR, flush=True)
    print("[setup] pip install:", pkgs, flush=True)
    t0 = time.monotonic()
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=PIP_WALL_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as e:
        out = _decode_maybe(getattr(e, "stdout", None))
        dt = round(time.monotonic() - t0, 3)
        print("[setup] pip TIMEOUT after", dt, "sec", flush=True)
        if out:
            print("[setup] pip tail (timeout):\\n" + _tail(out), flush=True)
        raise RuntimeError("pip install timed out after %s sec" % PIP_WALL_TIMEOUT_SEC)

    out = p.stdout or ""
    dt = round(time.monotonic() - t0, 3)
    if p.returncode != 0:
        print("[setup] pip FAILED rc=", p.returncode, "after", dt, "sec", flush=True)
        if out:
            print("[setup] pip tail (failed):\\n" + _tail(out), flush=True)
        raise RuntimeError("pip install failed rc=%s" % p.returncode)

    # Keep success logs short (avoid huge run.log)
    print("[setup] pip OK after", dt, "sec", flush=True)

def _ensure_requests():
    try:
        import requests  # noqa: F401
        return
    except Exception:
        _pip_install(["requests"])

def _revoke_connections():
    # Best-effort eager revocation so forwarded database access stops the moment
    # this run ends. The API also revokes on terminal job status, so failures here
    # only widen the window, never leave access open indefinitely.
    if not CONNECTIONS_URL or not CONNECTIONS_TOKEN:
        return
    try:
        _ensure_requests()
        import requests
        requests.post(
            CONNECTIONS_URL + "/revoke",
            headers={{"Authorization": "Bearer " + CONNECTIONS_TOKEN}},
            timeout=10,
        )
    except Exception:
        return

def _download(url, out_name):
    _ensure_requests()
    import requests
    r = requests.get(url, stream=True, timeout=300)
    r.raise_for_status()
    with open(out_name, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024*1024):
            if chunk:
                f.write(chunk)

def _presign_put(filename):
    _ensure_requests()
    import requests
    r = requests.post(API_BASE + "/presign-put", json={{
        "job_token": JOB_TOKEN,
        "filename": filename,
        "expiration_sec": 900
    }}, timeout=60)
    r.raise_for_status()
    return r.json()

def _upload_file(local_path, remote_name=None):
    _ensure_requests()
    import requests
    def _artifact_complete(remote_name):
        # Best-effort metadata persistence: lets the API update Mongo without waiting for status polling.
        if not API_BASE:
            return
        try:
            requests.post(
                API_BASE + "/artifact-complete",
                json={{
                    "job_token": JOB_TOKEN,
                    "filename": remote_name,
                }},
                timeout=10,
            )
        except Exception:
            return
    if remote_name is None:
        remote_name = os.path.basename(local_path)
    requested_mode = os.environ.get("AUTOBOTS_ARTIFACT_MODE", "auto")
    mode = requested_mode

    # auto: try callback if reachable, else presigned
    if mode == "auto":
        if API_BASE:
            try:
                r = requests.get(API_BASE + "/ping", timeout=3)
                if r.status_code == 200:
                    mode = "callback"
            except Exception:
                mode = "presigned"
        else:
            mode = "presigned"
    if requested_mode != mode:
        print("[upload_mode_resolved]", "requested="+str(requested_mode), "resolved="+str(mode), "api_base_set="+str(bool(API_BASE)))
    else:
        print("[upload_mode_resolved]", "requested="+str(requested_mode), "resolved="+str(mode))

    put_url = ""
    public_url = ""
    upload_headers = {{}}
    if mode == "callback":
        if not API_BASE:
            return ""
        try:
            pres = _presign_put(remote_name)
            put_url = pres.get("presigned_put_url", "")
            public_url = pres.get("public_url", "")
            upload_headers = pres.get("headers") or {{}}
            if not isinstance(upload_headers, dict):
                upload_headers = {{}}
        except Exception as e:
            print("[upload_presign_failed]", remote_name, str(e)[:200])
            return ""
    elif mode == "presigned":
        outs = {output_files!r}
        idx = None
        for i, fn in enumerate(outs):
            if fn == remote_name:
                idx = i
                break
        if idx is None:
            print("[upload_presigned_no_match]", remote_name)
            return ""
        put_url = os.environ.get(f"AUTOBOTS_PRESIGNED_PUT_{{idx}}", "")
        public_url = os.environ.get(f"AUTOBOTS_PRESIGNED_PUBLIC_{{idx}}", "")
        try:
            upload_headers = json.loads(
                os.environ.get(f"AUTOBOTS_PRESIGNED_HEADERS_{{idx}}", "{{}}")
            )
            if not isinstance(upload_headers, dict):
                upload_headers = {{}}
        except Exception:
            upload_headers = {{}}
    else:
        # server_sync / none
        return ""

    if not put_url or not public_url:
        print("[upload_skip_missing_urls]", remote_name, "mode="+str(mode))
        return ""

    # --- TEMP LOGS START (S3 PRESIGNED UPLOAD DIAG) ---
    try:
        pu = urllib.parse.urlparse(put_url)
        print("[TEMP_S3_DIAG_START]",
              "name="+str(remote_name),
              "mode="+str(mode),
              "put_host="+str(pu.netloc),
              "put_path="+str(pu.path),
              "put_query_len="+str(len(pu.query or "")),
              "has_sig="+str("X-Amz-Signature=" in (pu.query or "")))
    except Exception:
        pass
    # --- TEMP LOGS END (S3 PRESIGNED UPLOAD DIAG) ---

    try:
        with open(local_path, "rb") as f:
            up = requests.put(
                put_url,
                data=f,
                headers=upload_headers,
                timeout=3600,
                allow_redirects=False,
            )
    except Exception as e:
        # Important: do not fail the job due to upload connectivity/SSL/etc.
        print("[upload_exception]", remote_name, str(e)[:200])
        return ""
    if up.status_code not in (200, 204):
        # --- TEMP LOGS START (S3 PRESIGNED UPLOAD DIAG) ---
        try:
            loc = up.headers.get("Location", "") or up.headers.get("location", "")
            br = up.headers.get("x-amz-bucket-region", "") or up.headers.get("X-Amz-Bucket-Region", "")
            print(
                "[TEMP_S3_DIAG_PUT_FAIL]",
                remote_name,
                "status="+str(up.status_code),
                "x_amz_bucket_region="+str(br),
                "location="+str(loc)[:200],
            )
            # body often contains PermanentRedirect XML; keep small
            print("[TEMP_S3_DIAG_PUT_FAIL_BODY]", (up.text or "")[:300].replace("\\n"," "))
            print("[TEMP_S3_DIAG_END]", remote_name)
        except Exception:
            pass
        # --- TEMP LOGS END (S3 PRESIGNED UPLOAD DIAG) ---
        # Do not fail the entire job for upload issues; status can server_sync fallback later.
        print("[upload_failed_status]", remote_name, "status="+str(up.status_code))
        return ""
    _artifact_complete(remote_name)
    return public_url

def main():
    artifacts = {{}}
    try:
        pkgs = list({pip_packages!r})
        # runner needs requests for downloads/uploads
        if "requests" not in pkgs:
            pkgs.append("requests")
        if pkgs:
            _pip_install(pkgs)

        # download inputs
        urls = {input_urls!r}
        if urls:
            print("[setup] downloading inputs...")
            for u in urls:
                name = u.split("?")[0].split("/")[-1] or "input.bin"
                print("[setup] downloading", u, "->", name)
                _download(u, name)

        # run user code
        print("[run] executing user code")
        user_globals = {{"__name__": "__main__"}}
        exec({code!r}, user_globals, user_globals)

        # upload outputs
        outs = {output_files!r}
        if outs:
            print("[upload] uploading outputs:", outs)
            for fn in outs:
                if os.path.exists(fn):
                    url = _upload_file(fn, fn)
                    if url:
                        artifacts[fn] = url
                        print("[upload_ok]", fn, url)
                    else:
                        print("[upload_skip_or_failed]", fn)
                else:
                    print("[upload_skip_not_found]", fn)

        with open("artifacts.json", "w", encoding="utf-8") as f:
            json.dump(artifacts, f)

        with open("done.json", "w", encoding="utf-8") as f:
            json.dump({{"exit_code": 0}}, f)

    except Exception as e:
        try:
            with open("done.json", "w", encoding="utf-8") as f:
                json.dump({{"exit_code": 1, "error": str(e)}}, f)
        finally:
            raise
    finally:
        _revoke_connections()

if __name__ == "__main__":
    main()
"""
