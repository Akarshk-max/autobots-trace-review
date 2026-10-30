# src/autobots/conn/daytona/daytona_client.py
import asyncio
import base64
import binascii
import importlib
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol, cast
from uuid import uuid4

import httpx
from loguru import logger

from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context

_DAYTONA_MOD = None
try:
    _DAYTONA_MOD = importlib.import_module("daytona")
except Exception:
    _DAYTONA_MOD = None

# Cache SessionExecuteRequest once (avoid repeated importlib lookups).
_SessionExecuteRequest = getattr(_DAYTONA_MOD, "SessionExecuteRequest", None) if _DAYTONA_MOD else None
_SessionCommandLogsResponse = getattr(_DAYTONA_MOD, "SessionCommandLogsResponse", None) if _DAYTONA_MOD else None

class SessionExecuteRequestCtor(Protocol):
    """
    Callable constructor signature for Daytona SessionExecuteRequest.
    Using Protocol makes pylint understand that the object is callable.
    """
    def __call__(self, *, command: str, run_async: bool): ...


@dataclass
class DaytonaSandboxInfo:
    sandbox_id: str


class DaytonaOperationError(RuntimeError):
    def __init__(
        self,
        *,
        operation: str,
        message: str,
        status_code: int | None = None,
    ):
        super().__init__(message)
        self.operation = operation
        self.status_code = status_code


class DaytonaPermissionError(DaytonaOperationError):
    pass


def _extract_status_code(exc: Exception) -> int | None:
    for attr in ("status_code", "status", "http_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value

    response = getattr(exc, "response", None)
    response_status = getattr(response, "status_code", None)
    if isinstance(response_status, int):
        return response_status

    text = str(exc or "")
    match = re.search(r"\b(401|403|404)\b", text)
    return int(match.group(1)) if match else None


def _raise_daytona_operation_error(
    *,
    operation: str,
    exc: Exception,
) -> None:
    status_code = _extract_status_code(exc)
    error_type = (
        DaytonaPermissionError
        if status_code in {401, 403}
        else DaytonaOperationError
    )
    raise error_type(
        operation=operation,
        status_code=status_code,
        message=str(exc),
    ) from exc


class DaytonaClient:
    """
    Thin async wrapper around the Daytona SDK.

    NOTE: Daytona SDK/API details may differ. This wrapper isolates those differences
    so ActionDaytona only calls these methods.

    Maintainer notes:
      - The exact Daytona SDK version is pinned in pyproject.toml and uv.lock.
      - SDK lifecycle waits use Daytona's internal WebSocket state stream when
        available and automatically fall back to polling.
      - sandbox.process.exec(command, timeout=...) is documented to execute the command
        via a shell inside the sandbox (effectively /bin/sh -c <command>).
        So callers may use shell operators like &&, ||, >, &, etc.
      - sandbox.fs.upload_file(bytes, path) exists and is preferred for writing files into
        the sandbox (avoids command-length limits and quoting issues).
      - daytona.delete() is documented to take a Sandbox object, not a string id.
    """

    def __init__(self, api_key: str, api_url: str):
        self.api_key = api_key
        self.api_url = api_url
        # Cache sandbox objects created in-process (same worker). This avoids needing any SDK "get by id".
        # For cross-worker status polling, we fall back to the documented Toolbox HTTP API by sandboxId.
        self._sandbox_cache: dict[str, Any] = {}

        settings = SettingsProvider.sget()
        self.toolbox_base_url = getattr(settings, "DAYTONA_TOOLBOX_BASE_URL", "https://proxy.app.daytona.io/toolbox")

        try:
            # Expected SDK import; adjust to match your chosen Daytona SDK.
            from daytona import Daytona, DaytonaConfig  # type: ignore
        except ImportError as e:
            raise ImportError("Missing dependency: pip install daytona") from e

        cfg = DaytonaConfig(api_key=api_key, api_url=api_url)
        self._client = Daytona(cfg)

    def _cache_sandbox(self, lookup_key: str, sb: Any) -> None:
        """
        Cache sandbox object under both the lookup key and the sandbox's actual id (if available).
        This improves reuse reliability when callers refer to sandboxes by name vs id.
        """
        try:
            if lookup_key:
                self._sandbox_cache[str(lookup_key)] = sb
        except Exception:
            pass
        try:
            sid = getattr(sb, "id", None) or getattr(sb, "sandbox_id", None)
            if sid:
                self._sandbox_cache[str(sid)] = sb
        except Exception:
            pass

    def _get_sandbox_sync(self, sandbox_id: str) -> Any:
        """
        Daytona SDK surface varies. Try common sandbox retrieval methods.
        This is required because long-running jobs may be polled by different workers
        and cannot rely on in-process caching.

        IMPORTANT: The public docs primarily show `daytona.create()` returning a sandbox object
        and do not explicitly document a "get sandbox by id" method.
        We still try common methods if present, but callers should be prepared to fall back
        to the Toolbox HTTP API (which is explicitly documented to accept sandboxId).
       """
        cached = self._sandbox_cache.get(str(sandbox_id))
        if cached is not None:
            return cached

        if hasattr(self._client, "find_one"):
            sb = self._client.find_one(sandbox_id)  # type: ignore
            self._cache_sandbox(sandbox_id, sb)
            return sb
        if hasattr(self._client, "get"):
            sb = self._client.get(sandbox_id)  # type: ignore
            self._cache_sandbox(sandbox_id, sb)
            return sb
        if hasattr(self._client, "retrieve"):
            sb = self._client.retrieve(sandbox_id)  # type: ignore
            self._cache_sandbox(sandbox_id, sb)
            return sb
        sandboxes = getattr(self._client, "sandboxes", None)
        if sandboxes is not None and hasattr(sandboxes, "get"):
            sb = sandboxes.get(sandbox_id)  # type: ignore
            self._cache_sandbox(sandbox_id, sb)
            return sb
        raise AttributeError("Daytona SDK has no sandbox getter (find_one/get/retrieve/sandboxes.get)")

    @staticmethod
    def _py_b64_exec_cmd(py_code: str) -> str:
        """
        Build a shell-safe python3 -c command by base64-encoding the Python source.
        This avoids brittle shell quoting differences across /bin/sh implementations.
        """
        b64 = base64.b64encode((py_code or "").encode("utf-8")).decode("ascii")
        # Use double-quotes for -c, and single-quotes around the base64 payload.
        return (
            "python3 -c "
            + "\"import base64; exec(base64.b64decode('"
            + b64
            + "').decode('utf-8'))\""
        )

    async def _probe_localhost_port(
        self,
        ctx: Context,
        sandbox_id: str,
        *,
        port: int,
        timeout_sec: int = 5,
    ) -> bool:
        py = (
            "import socket\n"
            "s=socket.socket()\n"
            "s.settimeout(1.0)\n"
            f"s.connect(('127.0.0.1',{int(port)}))\n"
            "print('OK')\n"
        )
        cmd = self._py_b64_exec_cmd(py)
        rc, out = await self.exec(ctx=ctx, sandbox_id=sandbox_id, command=cmd, timeout_sec=timeout_sec)
        return rc == 0 and "OK" in (out or "")

    async def ensure_vnc(
        self,
        ctx: Context,
        sandbox_id: str,
        *,
        novnc_port: int = 6080,
        wait_ready_sec: int = 20,
    ) -> dict[str, Any]:
        """
        Best-effort: ensure Daytona computer_use desktop stack is running and noVNC is reachable.

        IMPORTANT:
        - Do NOT call computer_use.start() on every status poll; it can disrupt active sessions.
          We first probe the noVNC port; if already reachable we skip start().
        - After start(), noVNC may take a few seconds to listen; we probe localhost:novnc_port.
        - Starting VNC does not change the sandbox lifecycle policy.
        """
        # Fast-path: if noVNC is already listening, do nothing.
        try:
            if await self._probe_localhost_port(ctx=ctx, sandbox_id=sandbox_id, port=int(novnc_port), timeout_sec=5):
                return {"started": False, "novnc_port": int(novnc_port)}
        except Exception:
            # If probe fails due to stopped sandbox / transient issues, continue to start attempt.
            pass

        sb = await asyncio.to_thread(self._get_sandbox_sync, sandbox_id)

        computer_use = getattr(sb, "computer_use", None) or getattr(sb, "computerUse", None)
        if computer_use is None:
            raise RuntimeError("Sandbox SDK object has no computer_use interface")

        start_fn = getattr(computer_use, "start", None)
        if not callable(start_fn):
            raise RuntimeError("Sandbox computer_use.start() is unavailable in this SDK/runtime")

        start_resp = await asyncio.to_thread(start_fn)

        # Wait for noVNC port to come up.
        deadline = asyncio.get_running_loop().time() + max(0, int(wait_ready_sec))
        while asyncio.get_running_loop().time() < deadline:
            try:
                if await self._probe_localhost_port(
                    ctx=ctx,
                    sandbox_id=sandbox_id,
                    port=int(novnc_port),
                    timeout_sec=5,
                ):
                    return {"started": True, "novnc_port": int(novnc_port), "start_resp": start_resp}
            except Exception:
                pass
            await asyncio.sleep(1)

        logger.bind(ctx=ctx, sandbox_id=sandbox_id, novnc_port=novnc_port).warning(
            f"ensure_vnc: start() called but noVNC not confirmed ready within {wait_ready_sec}s"
        )
        return {
            "started": True,
            "novnc_port": int(novnc_port),
            "start_resp": start_resp,
            "warning": "novnc_not_confirmed_ready",
        }

    async def get_signed_preview_url(
        self,
        ctx: Context,
        sandbox_id: str,
        *,
        port: int,
        expires_in_seconds: int = 900,
    ) -> str:
        """
        Return a signed preview URL for an HTTP service inside the sandbox (e.g. noVNC on 6080).
        """
        sb = await asyncio.to_thread(self._get_sandbox_sync, sandbox_id)

        # Best-effort: reset activity timer. Preview usage may not count as activity.
        try:
            refresh_activity = getattr(sb, "refresh_activity", None)
            if callable(refresh_activity):
                await asyncio.to_thread(refresh_activity)
        except Exception:
            pass

        create_signed = getattr(sb, "create_signed_preview_url", None)
        if not callable(create_signed):
            raise RuntimeError("Sandbox.create_signed_preview_url() is unavailable in this SDK/runtime")

        resp = await asyncio.to_thread(create_signed, int(port), int(expires_in_seconds))
        url = getattr(resp, "url", None) or getattr(resp, "signed_url", None) or resp
        if not url:
            raise RuntimeError("Signed preview URL response missing url")
        return str(url)

    async def start_sandbox(self, ctx: Context, sandbox_id: str, timeout_sec: int = 120) -> None:
        """
        Start or restore an existing sandbox without changing its lifecycle.

        Permission failures are propagated so callers can distinguish a
        restricted read/execute key from a key with Sandbox Write permission.
        """
        try:
            sb = await asyncio.to_thread(self._get_sandbox_sync, sandbox_id)
            refresh_fn = getattr(sb, "refresh_data", None)
            if callable(refresh_fn):
                await asyncio.to_thread(refresh_fn)

            raw_state = getattr(sb, "state", None)
            if raw_state is None:
                state_text = ""
            elif hasattr(raw_state, "value"):
                state_text = str(raw_state.value)
            elif hasattr(raw_state, "name"):
                state_text = str(raw_state.name)
            else:
                state_text = str(raw_state)

            state_norm = (state_text or "").strip()
            if "." in state_norm:
                state_norm = state_norm.split(".")[-1]
            state_norm = state_norm.lower()

            if state_norm in {"started", "starting"}:
                return

            start_fn = getattr(sb, "start", None)
            if not callable(start_fn):
                raise RuntimeError("Sandbox.start() is unavailable")
            try:
                await asyncio.to_thread(start_fn, timeout=timeout_sec)
            except TypeError:
                await asyncio.to_thread(start_fn, timeout_sec)

            refresh_activity = getattr(sb, "refresh_activity", None)
            if callable(refresh_activity):
                await asyncio.to_thread(refresh_activity)
        except Exception as exc:
            _raise_daytona_operation_error(
                operation="start",
                exc=exc,
            )

    async def stop_sandbox(
        self,
        ctx: Context,
        sandbox_id: str,
        timeout_sec: int = 60,
    ) -> None:
        """
        Stop a sandbox.

        For non-ephemeral container sandboxes this preserves filesystem state.
        Daytona deletes ephemeral sandboxes when they stop.
        """
        try:
            sb = await asyncio.to_thread(
                self._get_sandbox_sync,
                sandbox_id,
            )
            stop_fn = getattr(sb, "stop", None)
            if not callable(stop_fn):
                raise RuntimeError("Sandbox.stop() is unavailable")
            try:
                await asyncio.to_thread(stop_fn, timeout=timeout_sec)
            except TypeError:
                await asyncio.to_thread(stop_fn, timeout_sec)
        except Exception as exc:
            _raise_daytona_operation_error(
                operation="stop",
                exc=exc,
            )

    async def create_sandbox(
        self,
        ctx: Context,
        language: str,
        env_vars: Dict[str, str],
        *,
        snapshot: Optional[str] = None,
        cpu: Optional[int] = None,
        memory_gib: Optional[int] = None,
        ephemeral: bool,
        ttl_minutes: Optional[int],
        auto_stop_interval: int,
        auto_archive_interval: Optional[int],
        auto_delete_interval: int,
    ) -> DaytonaSandboxInfo:
        # Daytona SDK class names can vary across versions, and some names may not be exported
        # in a way that static analyzers (pylint) can discover. We use dynamic getattr()
        # to avoid `pylint E0611: no-name-in-module` while still being runtime-correct.
        #
        # IMPORTANT (Phase-1 compute sizing):
        # We validated that snapshot tiers work correctly, and that custom CPU/RAM works reliably
        # when specified at creation time via CreateSandboxFromImageParams(resources=Resources(...)).
        # In some Daytona deployments, POST /sandbox/{id}/resize may return 404 ("Cannot POST .../resize"),
        # so we prefer create-time resources when cpu/memory_gib are provided, and keep resize only
        # as a best-effort fallback for older SDKs/deployments.
        daytona_mod = importlib.import_module("daytona")
        SnapshotParams = getattr(daytona_mod, "CreateSandboxFromSnapshotParams", None)
        ImageParams = getattr(daytona_mod, "CreateSandboxFromImageParams", None)
        CreateParams = getattr(daytona_mod, "CreateSandboxParams", None)
        Resources = getattr(daytona_mod, "Resources", None)

        settings = SettingsProvider.sget()

        req_cpu = int(cpu) if cpu is not None else None
        req_mem = int(memory_gib) if memory_gib is not None else None

        # Validate caps (defensive; primary user-facing validation happens in action layer)
        if req_cpu is not None or req_mem is not None:
            max_cpu = int(getattr(settings, "DAYTONA_MAX_CPU", 4) or 4)
            max_mem = int(getattr(settings, "DAYTONA_MAX_MEMORY_GIB", 8) or 8)
            if req_cpu is not None and not (1 <= req_cpu <= max_cpu):
                raise ValueError(f"cpu must be between 1 and {max_cpu}")
            if req_mem is not None and not (1 <= req_mem <= max_mem):
                raise ValueError(f"memory_gib must be between 1 and {max_mem}")

        lifecycle_kwargs: dict[str, Any] = {
            "ephemeral": bool(ephemeral),
            "auto_stop_interval": int(auto_stop_interval),
            "auto_delete_interval": int(auto_delete_interval),
        }
        if ttl_minutes is not None:
            lifecycle_kwargs["ttl_minutes"] = int(ttl_minutes)
        if auto_archive_interval is not None:
            lifecycle_kwargs["auto_archive_interval"] = int(auto_archive_interval)

        def _apply_network_params(params_obj: Any) -> None:
            """
            Apply only network settings. Lifecycle is passed explicitly through
            SDK constructors and must never be silently omitted.
            """

            # Network restrictions (Daytona docs: allow list must be IPv4 CIDR only; hostnames not supported)
            if getattr(settings, "DAYTONA_NETWORK_BLOCK_ALL", False) and hasattr(params_obj, "network_block_all"):
                try:
                    params_obj.network_block_all = True
                except Exception:
                    pass

            allow_list = (getattr(settings, "DAYTONA_NETWORK_ALLOW_LIST", "") or "").strip()
            if allow_list and hasattr(params_obj, "network_allow_list"):
                try:
                    params_obj.network_allow_list = allow_list
                except Exception:
                    pass

        # ---------------------------------------
        # Preferred path: image + resources sizing
        # ---------------------------------------
        if (req_cpu is not None or req_mem is not None) and ImageParams is not None and callable(ImageParams) and Resources is not None:
            default_image = getattr(
                settings,
                "DAYTONA_DEFAULT_IMAGE",
                "daytonaio/sandbox:latest",
            )
            res = Resources(cpu=req_cpu, memory=req_mem)
            try:
                params = ImageParams(
                    language=language,
                    image=default_image,
                    env_vars=env_vars,
                    resources=res,
                    **lifecycle_kwargs,
                )
            except TypeError as e:
                raise RuntimeError(
                    "Installed Daytona SDK does not support the required "
                    "sandbox lifecycle parameters. Upgrade the pinned Daytona package."
                ) from e

            _apply_network_params(params)

            try:
                sb = await asyncio.to_thread(self._client.create, params)
            except Exception as e:
                if _extract_status_code(e) in {401, 403}:
                    _raise_daytona_operation_error(
                        operation="create",
                        exc=e,
                    )
                # Best-effort fallback to snapshot path below.
                logger.bind(ctx=ctx, requested_cpu=req_cpu, requested_memory_gib=req_mem).warning(
                    f"Image+resources sandbox creation failed; falling back to snapshot path: {e}"
                )
            else:
                sandbox_id = getattr(sb, "id", None) or getattr(sb, "sandbox_id", None)
                assert sandbox_id, "Daytona sandbox id not found"
                self._sandbox_cache[str(sandbox_id)] = sb

                refresh_fn = getattr(sb, "refresh_data", None)
                if callable(refresh_fn):
                    try:
                        await asyncio.to_thread(refresh_fn)
                    except Exception as e:
                        logger.bind(
                            ctx=ctx,
                            sandbox_id=str(sandbox_id),
                        ).warning(
                            f"Sandbox created, but refresh_data failed: {e}"
                        )

                logger.bind(
                    ctx=ctx,
                    sandbox_id=str(sandbox_id),
                    image=default_image,
                    requested_cpu=req_cpu,
                    requested_memory_gib=req_mem,
                    effective_cpu=getattr(sb, "cpu", None),
                    effective_memory=getattr(sb, "memory", None),
                ).info("Daytona sandbox created using image+resources (cpu/memory)")
                return DaytonaSandboxInfo(sandbox_id=str(sandbox_id))

        # ---------------------------------------
        # Snapshot path (tiers / default behavior)
        # ---------------------------------------
        if SnapshotParams is not None:
            try:
                kwargs = {
                    "language": language,
                    "env_vars": env_vars,
                    **lifecycle_kwargs,
                }
                if snapshot:
                    kwargs["snapshot"] = snapshot
                params = SnapshotParams(**kwargs)
            except TypeError as e:
                raise RuntimeError(
                    "Installed Daytona SDK does not support the required "
                    "sandbox lifecycle parameters. Upgrade the pinned Daytona package."
                ) from e
        elif CreateParams is not None:
            try:
                params = CreateParams(
                    language=language,
                    env_vars=env_vars,
                    **lifecycle_kwargs,
                )
            except TypeError as e:
                raise RuntimeError(
                    "Installed Daytona SDK does not support the required "
                    "sandbox lifecycle parameters. Upgrade the pinned Daytona package."
                ) from e
        else:
            raise ImportError(
                "Daytona SDK missing expected sandbox param classes. "
                "Expected CreateSandboxFromSnapshotParams or CreateSandboxParams."
            )

        _apply_network_params(params)

        try:
            sb = await asyncio.to_thread(self._client.create, params)
        except Exception as exc:
            _raise_daytona_operation_error(
                operation="create",
                exc=exc,
            )

        # -------------------------
        # CPU/RAM sizing
        # -------------------------
        # Phase-1 policy: resize is best-effort; on failure we keep the already-provisioned size and proceed.
        if req_cpu is not None or req_mem is not None:
            try:
                resize_fn = getattr(sb, "resize", None)
                refresh_fn = getattr(sb, "refresh_data", None)
                wait_fn = getattr(sb, "wait_for_resize_complete", None)

                if Resources is None or not callable(resize_fn):
                    raise RuntimeError("Daytona SDK missing Resources or sandbox.resize()")

                # Refresh baseline before comparing requested values (avoids stale sb.cpu/sb.memory).
                if callable(refresh_fn):
                    await asyncio.to_thread(refresh_fn)

                cur_cpu = getattr(sb, "cpu", None)
                cur_mem = getattr(sb, "memory", None)

                # Hot resize supports increases only; strict '>' also avoids no-op resize errors.
                do_cpu = req_cpu
                do_mem = req_mem
                if isinstance(cur_cpu, int) and do_cpu is not None and do_cpu <= cur_cpu:
                    do_cpu = None
                if isinstance(cur_mem, int) and do_mem is not None and do_mem <= cur_mem:
                    do_mem = None

                did_resize = False
                if do_cpu is not None or do_mem is not None:
                    res = Resources(cpu=do_cpu, memory=do_mem)
                    await asyncio.to_thread(resize_fn, res)
                    if callable(wait_fn):
                        # Some SDKs may not accept keyword args; be defensive.
                        try:
                            await asyncio.to_thread(wait_fn, timeout=120)
                        except TypeError:
                            await asyncio.to_thread(wait_fn, 120)
                    if callable(refresh_fn):
                        await asyncio.to_thread(refresh_fn)
                    did_resize = True

                b = logger.bind(
                    ctx=ctx,
                    sandbox_id=getattr(sb, "id", None) or getattr(sb, "sandbox_id", None),
                    snapshot=snapshot,
                    requested_cpu=req_cpu,
                    requested_memory_gib=req_mem,
                    effective_cpu=getattr(sb, "cpu", None),
                    effective_memory=getattr(sb, "memory", None),
                )
                if did_resize:
                    b.info("Daytona sandbox compute sizing applied via resize (cpu/memory)")
                else:
                    # Requested values were <= current values (or unreadable current values prevented a strict comparison).
                    # In either case, no resize call was needed/attempted.
                    b.debug("Daytona sandbox compute sizing: no resize needed")
            except Exception as e:
                # Non-fatal: sandbox exists; sizing didn't apply (quota, DaytonaError, etc.).
                logger.bind(ctx=ctx, snapshot=snapshot, requested_cpu=cpu, requested_memory_gib=memory_gib).warning(
                    f"Daytona sandbox resize failed (non-fatal): {e}"
                )

        sandbox_id = getattr(sb, "id", None) or getattr(sb, "sandbox_id", None)
        assert sandbox_id, "Daytona sandbox id not found"
        self._sandbox_cache[str(sandbox_id)] = sb
        logger.bind(ctx=ctx, sandbox_id=sandbox_id).info("Daytona sandbox created")
        return DaytonaSandboxInfo(sandbox_id=str(sandbox_id))

    async def _read_file_b64_via_exec(
        self,
        ctx: Context,
        sandbox_id: str,
        remote_path: str,
        *,
        timeout_sec: int = 120,
        max_bytes: int = 5_000_000,
    ) -> bytes:
        """
        Fallback file download method that does NOT require sandbox.fs APIs.
        Uses process exec (SDK or Toolbox HTTP) to base64-encode the file and print it.

        This is intended for small-to-moderate artifacts. A size guard is included
        to avoid accidentally streaming huge files via stdout.
        """
        # 1) quick size check to avoid huge stdout payloads
        size_cmd = (
            "python3 -c "
            + repr(
                "import os,sys; "
                f"p={remote_path!r}; "
                "print(os.path.getsize(p))"
            )
        )
        code, out = await self.exec(ctx=ctx, sandbox_id=sandbox_id, command=size_cmd, timeout_sec=timeout_sec)
        if code != 0:
            raise RuntimeError(f"Cannot stat remote file: {remote_path} (exit={code}) out={out[:200]}")
        try:
            n = int(out.strip().splitlines()[-1])
        except Exception as e:
            raise RuntimeError(f"Invalid size output for {remote_path}: {out[:200]}") from e
        if n > max_bytes:
            raise RuntimeError(f"Remote file too large for exec/base64 fallback: {remote_path} size={n} max={max_bytes}")

        # 2) base64 encode file and print
        b64_cmd = (
            "python3 -c "
            + repr(
                "import base64; "
                f"p={remote_path!r}; "
                "print(base64.b64encode(open(p,'rb').read()).decode('ascii'))"
            )
        )
        code, b64 = await self.exec(ctx=ctx, sandbox_id=sandbox_id, command=b64_cmd, timeout_sec=timeout_sec)
        if code != 0:
            raise RuntimeError(f"Cannot read remote file: {remote_path} (exit={code}) out={b64[:200]}")
        try:
            return base64.b64decode(b64.strip().encode("ascii"))
        except (binascii.Error, UnicodeError) as e:
            raise RuntimeError(f"Invalid base64 payload while reading {remote_path}: {b64[:200]}") from e

    async def probe_http_get(self, ctx: Context, sandbox_id: str, url: str, timeout_sec: int = 5) -> bool:
        """
        Probe outbound HTTP reachability from inside the sandbox.
        Uses stdlib urllib to avoid extra deps.
        """
        cmd = (
            "python3 -c "
            + repr(
                "import urllib.request; "
                f"urllib.request.urlopen({url!r}, timeout={timeout_sec}).read(1); "
                "print('OK')"
            )
        )
        code, out = await self.exec(ctx=ctx, sandbox_id=sandbox_id, command=cmd, timeout_sec=timeout_sec + 2)
        return code == 0 and "OK" in out

    async def download_file_bytes(self, ctx: Context, sandbox_id: str, remote_path: str, timeout_sec: int = 1800) -> bytes:
        """
        Download a file into memory. Daytona docs describe download_file(remote_path, timeout) -> bytes.
        Default timeout in docs is up to ~30 minutes; keep explicit.
        """
        # Prefer SDK fs.download_file when sandbox object is available in-process.
        try:
            sb = await asyncio.to_thread(self._get_sandbox_sync, sandbox_id)
            fs = getattr(sb, "fs", None)
            if fs is not None and hasattr(fs, "download_file"):
                logger.bind(ctx=ctx, sandbox_id=sandbox_id, remote_path=remote_path).debug(
                    "download_file_bytes: using SDK fs.download_file"
                )
                # SDK signature varies across versions; in some environments passing timeout=...
                # raises: "unexpected keyword argument 'timeout'". We omit it for compatibility.
                # (If the SDK supports timeouts internally, it will use its default.)
                data = await asyncio.to_thread(fs.download_file, remote_path)  # type: ignore
                return data if isinstance(data, bytes) else bytes(data)
        except Exception as e:
            logger.bind(ctx=ctx, sandbox_id=sandbox_id, remote_path=remote_path).debug(
                f"download_file_bytes: SDK fs.download_file unavailable/failed; falling back to exec/base64: {e}"
            )

        # Fallback: read via exec (works even when only Toolbox API is available).
        logger.bind(ctx=ctx, sandbox_id=sandbox_id, remote_path=remote_path).debug(
            "download_file_bytes: using exec/base64 fallback"
        )
        return await self._read_file_b64_via_exec(
            ctx=ctx,
            sandbox_id=sandbox_id,
            remote_path=remote_path,
            timeout_sec=min(timeout_sec, 300),
        )

    async def download_file_to_path(
        self, ctx: Context, sandbox_id: str, remote_path: str, local_path: str, timeout_sec: int = 1800
    ) -> None:
        """
        Stream download to a local path if supported: download_file(remote_path, local_path, timeout=...).
        Falls back to in-memory download for SDKs that don't support streaming signature.
        """
        # Prefer streamed signature if supported by SDK.
        try:
            sb = await asyncio.to_thread(self._get_sandbox_sync, sandbox_id)
            fs = getattr(sb, "fs", None)
            if fs is not None and hasattr(fs, "download_file"):
                try:
                    logger.bind(ctx=ctx, sandbox_id=sandbox_id, remote_path=remote_path).debug(
                        "download_file_to_path: using SDK fs.download_file streaming signature"
                    )
                    # SDK signature varies across versions; in some environments passing timeout=...
                    # raises: "unexpected keyword argument 'timeout'". We omit it for compatibility.
                    # If the SDK supports streaming download (remote_path, local_path), this should work.
                    await asyncio.to_thread(fs.download_file, remote_path, local_path)  # type: ignore
                    return
                except TypeError:
                    # SDK doesn't support streaming signature -> fall back to in-memory
                    logger.bind(ctx=ctx, sandbox_id=sandbox_id, remote_path=remote_path).debug(
                        "download_file_to_path: SDK download_file streaming signature not supported; falling back to bytes"
                    )
                    pass
        except Exception as e:
            logger.bind(ctx=ctx, sandbox_id=sandbox_id, remote_path=remote_path).debug(
                f"download_file_to_path: SDK fs.download_file unavailable/failed; falling back to exec/base64: {e}"
            )

        # In-memory fallback (which itself falls back to exec/base64 if needed)
        logger.bind(ctx=ctx, sandbox_id=sandbox_id, remote_path=remote_path).debug(
            "download_file_to_path: using download_file_bytes fallback"
        )
        data = await self.download_file_bytes(
            ctx=ctx,
            sandbox_id=sandbox_id,
            remote_path=remote_path,
            timeout_sec=timeout_sec,
        )
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(data)

    async def exec(
        self,
        ctx: Context,
        sandbox_id: str,
        command: str,
        timeout_sec: int = 120,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        # Prefer SDK sandbox object when available (same worker). Otherwise fall back to Toolbox API,
        # which is explicitly documented to accept sandboxId endpoints.
        try:
            sb = await asyncio.to_thread(self._get_sandbox_sync, sandbox_id)
            # Docs: sandbox.process.exec(command, cwd=None, env=None, timeout=None)
            res = await asyncio.to_thread(
                sb.process.exec,  # type: ignore
                command,
                cwd=cwd,
                env=env,
                timeout=timeout_sec,
            )
            # Preserve valid exit_code=0 (do NOT use `or` fallbacks).
            exit_code = getattr(res, "exit_code", None)
            if exit_code is None:
                # Compatibility hedge (some SDK surfaces may camelCase).
                exit_code = getattr(res, "exitCode", None)
            if exit_code is None:
                exit_code = -1
            output = getattr(res, "result", "") or ""
            return int(exit_code), str(output)
        except Exception as e:
            logger.bind(ctx=ctx, sandbox_id=sandbox_id).warning(
                f"SDK sandbox retrieval/exec failed; falling back to Toolbox API: {e}"
            )

        url = f"{self.toolbox_base_url}/{sandbox_id}/process/execute"
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}
        payload = {"command": command, "cwd": cwd, "env": env, "timeout": timeout_sec}
        async with httpx.AsyncClient(timeout=timeout_sec + 10) as client:
            r = await client.post(url, headers=headers, json=payload)
            if r.status_code >= 400:
                raise RuntimeError(f"Toolbox exec failed: {r.status_code} {r.text[:500]}")
            data = r.json()
        # API returns exitCode (camelCase)
        exit_code = int(data.get("exitCode", data.get("exit_code", -1)))
        output = str(data.get("result", "") or "")
        return exit_code, output

    async def create_session(self, ctx: Context, sandbox_id: str, session_id: str | None = None) -> str:
        """
        Create a long-running background session in the sandbox.
        Per docs: create_session(session_id: str) -> None
        """
        sb = await asyncio.to_thread(self._get_sandbox_sync, sandbox_id)
        proc = getattr(sb, "process", None)
        if proc is None:
            raise NotImplementedError("Daytona sandbox has no process API; cannot create session")

        create_session = getattr(proc, "create_session", None)
        if not callable(create_session):
            raise NotImplementedError("Daytona Sessions API create_session not available in this SDK/runtime")

        sid = session_id or f"sess_{uuid4().hex[:12]}"
        await asyncio.to_thread(create_session, sid)
        return sid

    async def execute_session_command(
        self, ctx: Context, sandbox_id: str, session_id: str, command: str, timeout_sec: int | None = None
    ) -> str:
        """
        Execute a command in a session.
        Per docs: execute_session_command(session_id, req, timeout=None) -> SessionExecuteResponse (has cmd_id).
        For long-running jobs, set run_async=True.

        Returns:
            cmd_id: The command id to poll via get_session_command/get_session_command_logs.
        """
        sb = await asyncio.to_thread(self._get_sandbox_sync, sandbox_id)
        proc = getattr(sb, "process", None)
        if proc is None:
            raise NotImplementedError("Daytona sandbox has no process API; cannot execute session command")

        exec_session_cmd = getattr(proc, "execute_session_command", None)
        if not callable(exec_session_cmd):
            raise NotImplementedError("Daytona Sessions API execute_session_command not available")

        if _SessionExecuteRequest is None or not callable(_SessionExecuteRequest):
            raise NotImplementedError("SessionExecuteRequest not available in this SDK/runtime")

        # _SessionExecuteRequest is dynamically imported; Protocol cast makes it "callable" to pylint.
        SessionExecuteRequest = cast(SessionExecuteRequestCtor, _SessionExecuteRequest)
        req = SessionExecuteRequest(command=command, run_async=True)
        if timeout_sec is None:
            res = await asyncio.to_thread(exec_session_cmd, session_id, req)
        else:
            res = await asyncio.to_thread(exec_session_cmd, session_id, req, timeout=timeout_sec)

        cmd_id = getattr(res, "cmd_id", None) or getattr(res, "cmdId", None)
        if not cmd_id:
            raise RuntimeError("execute_session_command did not return cmd_id")
        return str(cmd_id)

    async def get_session_command_logs(self, ctx: Context, sandbox_id: str, session_id: str, command_id: str) -> tuple[str, str]:
        """
        Fetch logs for a session command.
        Per docs: get_session_command_logs(session_id, command_id) -> SessionCommandLogsResponse {stdout, stderr}.
        Returns (stdout, stderr).
        """
        sb = await asyncio.to_thread(self._get_sandbox_sync, sandbox_id)
        proc = getattr(sb, "process", None)
        if proc is None:
            raise NotImplementedError("Daytona sandbox has no process API; cannot get session command logs")
        get_logs = (
            getattr(proc, "get_session_command_logs", None)
            or getattr(proc, "get_session_command_logs_async", None)
        )
        if not callable(get_logs):
            raise NotImplementedError("get_session_command_logs not available in this SDK/runtime")
        logs = await asyncio.to_thread(get_logs, session_id, command_id)
        stdout = str(getattr(logs, "stdout", "") or "")
        stderr = str(getattr(logs, "stderr", "") or "")
        return stdout, stderr

    async def delete_session(self, ctx: Context, sandbox_id: str, session_id: str) -> None:
        """
        Best-effort, idempotent session cleanup.

        A missing session or sandbox means cleanup has already happened and is
        therefore considered successful.
        """
        # Try SDK first (same worker).
        try:
            sb = await asyncio.to_thread(self._get_sandbox_sync, sandbox_id)
            proc = getattr(sb, "process", None)
            if proc is not None:
                delete_session = getattr(proc, "delete_session", None)
                if callable(delete_session):
                    await asyncio.to_thread(delete_session, session_id)
                    return
        except Exception as exc:
            if _extract_status_code(exc) == 404:
                return

        # Fallback: Toolbox API (documented).
        try:
            url = f"{self.toolbox_base_url}/{sandbox_id}/process/session/{session_id}"
            headers = {"Authorization": f"Bearer {self.api_key}"}
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.delete(url, headers=headers)
                if r.status_code == 404:
                    return
                if r.status_code >= 400:
                    raise RuntimeError(f"{r.status_code} {r.text[:300]}")
        except Exception as e:
            if _extract_status_code(e) == 404:
                return
            logger.bind(ctx=ctx, sandbox_id=sandbox_id, session_id=session_id).warning(
                f"delete_session failed (non-fatal): {e}"
            )

    async def write_file(self, ctx: Context, sandbox_id: str, path: str, content: bytes) -> None:
        """
        Preferred file upload method: uses the Daytona SDK's native fs.upload_file if available.
        This avoids command-length limits and quoting issues inherent in exec-based writers.
        """
        try:
            sb = await asyncio.to_thread(self._get_sandbox_sync, sandbox_id)
            fs = getattr(sb, "fs", None)
            if fs is not None and hasattr(fs, "upload_file"):
                upload = fs.upload_file  # type: ignore
                # SDK signature can vary. Try bytes-first, then fallback to local-path upload style.
                try:
                    await asyncio.to_thread(upload, content, path)
                    return
                except TypeError:
                    # Fallback: upload_file(local_path, remote_path)
                    tmp_path = None
                    try:
                        with tempfile.NamedTemporaryFile(delete=False) as tmp:
                            tmp.write(content)
                            tmp.flush()
                            tmp_path = tmp.name
                        try:
                            await asyncio.to_thread(upload, tmp_path, path)
                            return
                        except Exception:
                            # fallthrough to exec writer
                            pass
                    finally:
                        if tmp_path:
                            try:
                                os.remove(tmp_path)
                            except Exception:
                                pass
                except Exception:
                    # Continue to exec-based fallback below
                    pass
        except Exception as e:
            logger.bind(ctx=ctx, sandbox_id=sandbox_id, path=path).warning(
                f"write_file: SDK fs.upload_file unavailable (sandbox may be reused cross-worker); "
                f"falling back to exec/base64 writer: {e}"
            )

        await self.write_file_b64(ctx=ctx, sandbox_id=sandbox_id, path=path, content=content)

    async def write_file_b64(self, ctx: Context, sandbox_id: str, path: str, content: bytes) -> None:
        """
        Write a file via a one-line python command to avoid shell quoting issues.
        """
        # Small payload: one-shot.
        if len(content) <= 120_000:
            b64 = base64.b64encode(content).decode("ascii")
            cmd = (
                "python3 -c "
                + repr(
                    f"import base64,os; os.makedirs(os.path.dirname({path!r}) or '.', exist_ok=True); "
                    f"open({path!r},'wb').write(base64.b64decode({b64!r}))"
                )
            )
            code, out = await self.exec(ctx=ctx, sandbox_id=sandbox_id, command=cmd, timeout_sec=120)
            if code != 0:
                raise RuntimeError(f"Failed to write file {path}: {out}")
            return

        # Large payload: chunked base64 append to avoid command-size limits.
        init_cmd = (
            "python3 -c "
            + repr(
                f"import os; os.makedirs(os.path.dirname({path!r}) or '.', exist_ok=True); open({path!r},'wb').close()"
            )
        )
        code, out = await self.exec(ctx=ctx, sandbox_id=sandbox_id, command=init_cmd, timeout_sec=120)
        if code != 0:
            raise RuntimeError(f"Failed to init file {path}: {out}")

        b64_all = base64.b64encode(content).decode("ascii")
        chunk_size = 45_000
        for i in range(0, len(b64_all), chunk_size):
            part = b64_all[i : i + chunk_size]
            cmd = "python3 -c " + repr(f"import base64; open({path!r},'ab').write(base64.b64decode({part!r}))")
            code, out = await self.exec(ctx=ctx, sandbox_id=sandbox_id, command=cmd, timeout_sec=120)
            if code != 0:
                raise RuntimeError(f"Failed to write chunk to {path}: {out}")

    async def start_background(
        self,
        ctx: Context,
        sandbox_id: str,
        command: str,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> str:
        """
        Start a background process and return best-effort PID string.
        """
        # Avoid double-shell. exec() is already shell-based per Daytona SDK docs.
        code, out = await self.exec(
            ctx=ctx,
            sandbox_id=sandbox_id,
            command=command,
            timeout_sec=30,
            cwd=cwd,
            env=env,
        )
        if code != 0:
            raise RuntimeError(f"Failed to start background command: {out}")
        return out.strip()

    async def delete_sandbox(self, ctx: Context, sandbox_id: str) -> None:
        sb_obj: Any = None
        try:
            sb_obj = await asyncio.to_thread(self._get_sandbox_sync, sandbox_id)
            # Daytona SDK docs indicate delete expects a Sandbox object, not a string id.
            await asyncio.to_thread(self._client.delete, sb_obj)  # type: ignore
            logger.bind(ctx=ctx, sandbox_id=sandbox_id).info("Daytona sandbox deleted")
        except Exception as exc:
            if _extract_status_code(exc) == 404:
                logger.bind(ctx=ctx, sandbox_id=sandbox_id).debug(
                    "Daytona sandbox already absent; delete is complete"
                )
                return
            _raise_daytona_operation_error(
                operation="delete",
                exc=exc,
            )
        finally:
            # Invalidate cache best-effort (prevents stale sandbox objects after deletion).
            try:
                self._sandbox_cache.pop(str(sandbox_id), None)
            except Exception:
                pass
            try:
                sid = getattr(sb_obj, "id", None) or getattr(sb_obj, "sandbox_id", None)
                if sid:
                    self._sandbox_cache.pop(str(sid), None)
            except Exception:
                pass
