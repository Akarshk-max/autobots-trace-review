# tests/action/action_type/test_action_text2text_daytona_e2e.py
import asyncio
import json
import re
import sys
import warnings
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse
from uuid import uuid4

import pytest
import pytest_asyncio
from loguru import logger

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.action.action_type.tool_routes import register_tools
from src.autobots.integrations.user_integration import UserIntegration
from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.integrations.app_types import AppTypes
from src.autobots.conn.daytona.state.user_daytona_jobs import UserDaytonaJobs
from src.autobots.conn.openai.openai_responses.openai_responses import LlmResponsesOpenaiConfig
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM

pytestmark = [pytest.mark.asyncio]


# -----------------------------------------------------------------------------
# IMPORTANT: override the session autouse `start()` from tests/conftest.py
# We do NOT want setup_test_db() or sleep(15) for this live "my user" test.
# -----------------------------------------------------------------------------
@pytest_asyncio.fixture(scope="session", autouse=True)
async def start():
    # Reconfigure Loguru for this test session:
    #  - Avoid huge tracebacks for expected auth fallback paths.
    #  - Filter known-noisy "missing AppAuth doc" logs.
    #
    # Note: we intentionally do this in tests (not in prod code).
    #
    # Also suppress very noisy pydantic serializer warnings which can spam the output
    # when OpenAI Responses returns nested content structures.
    warnings.filterwarnings(
        "ignore",
        category=UserWarning,
        message=r"^Pydantic serializer warnings:.*",
    )
    # Reduce general warnings noise in this E2E module output.
    warnings.filterwarnings(
        "ignore",
        message=r".*PydanticDeprecatedSince20.*",
    )
    warnings.filterwarnings(
        "ignore",
        category=DeprecationWarning,
        message=r".*websockets\.legacy is deprecated.*",
    )

    logger.remove()

    def _exc_text(exc: object) -> str:
        """
        Best-effort exception text extraction for filtering expected exception stack traces.
        Loguru usually provides a RecordException with `.value`.
        """
        if not exc:
            return ""
        val = getattr(exc, "value", None)
        return str(val) if val is not None else str(exc)

    @dataclass(frozen=True)
    class _DropRule:
        """
        A compact, data-driven log drop rule.
        If all specified fields match, the record is dropped.
        """

        name_startswith: str | None = None
        name_endswith: str | None = None
        function: str | None = None

        msg_contains_any: tuple[str, ...] = ()
        msg_contains_all: tuple[str, ...] = ()
        exc_contains_any: tuple[str, ...] = ()

        # For edge cases that are hard to express declaratively.
        predicate: Callable[[str, str, str, str], bool] | None = None

        def matches(self, *, name: str, fn: str, msg: str, exc_text: str) -> bool:
            if self.name_startswith and not name.startswith(self.name_startswith):
                return False
            if self.name_endswith and not name.endswith(self.name_endswith):
                return False
            if self.function and fn != self.function:
                return False

            if self.msg_contains_all and not all(s in msg for s in self.msg_contains_all):
                return False
            if self.msg_contains_any and not any(s in msg for s in self.msg_contains_any):
                return False
            if self.exc_contains_any and not any(s in exc_text for s in self.exc_contains_any):
                return False

            if self.predicate and not self.predicate(name, fn, msg, exc_text):
                return False

            return True

    # Keep this list tight and descriptive; add new silencing logic here (not as more `if` blocks).
    DROP_RULES: tuple[_DropRule, ...] = (
        # Silence settings "Field XYZ is not set" spam (note: earliest settings logs can occur before this fixture runs).
        _DropRule(name_endswith="src.autobots.core.settings", function="check_for_none"),

        # Silence noisy OpenAI plumbing logs (keep Daytona/tool logs).
        _DropRule(
            name_startswith="src.autobots.conn.openai.",
            msg_contains_any=(
                "Response max loop is",
                "Responses Loop:",
                "Starting replay sanitizer process",
                "Completed replay sanitizer process",
                "Openai Responses response",
                "Openai Response Output",
                "Setting openai_responses.code_interpreter memory_limit",
                "openai_responses.code_interpreter file_ids",
                "openai_responses.code_interpreter file_ids before consolidation check",
                "Merging file_ids from tool outputs",
                "Final merged file_ids for tool parameters",
                "No successful code_interpreter_call found for fallback",
                "Using OpenAI responses client",
                "Started background keep-alive task for containers",
            ),
        ),

        # Silence file download/upload chatter (it’s repeated a lot).
        _DropRule(
            name_startswith="src.autobots.files.",
            msg_contains_any=(
                "Downloaded urls to temp files",
                "Uploaded urls to openai",
                "Removed temp file",
                "File sent to openai",
                "File uploaded to openai",
            ),
        ),

        # Silence generic action start/complete noise (keeps the important warnings/errors).
        _DropRule(
            name_endswith="src.autobots.action.action_type.action_factory",
            function="run_action",
            msg_contains_any=("Action run started", "Action run completed"),
        ),

        # Silence repetitive OpenAI internal INFO logs that don't help debugging this E2E.
        _DropRule(
            name_endswith="src.autobots.conn.openai.openai_responses.openai_responses",
            function="update_openai_urls_with_public_s3_urls",
            msg_contains_all=("Extracted attributes from input item",),
        ),

        # Silence expected missing AppAuth logs during system-key fallback
        _DropRule(msg_contains_all=("Cannot get app auth for", "App auth doc not found")),
        _DropRule(
            name_endswith="src.autobots.integrations.app_conn.app_conn_helper",
            msg_contains_all=("Cannot get app auth",),
        ),
        _DropRule(
            name_endswith="src.autobots.integrations.app_conn.app_conn",
            function="get_daytona",
            msg_contains_all=("App auth doc not found",),
        ),

        # The callback-mode negative test currently triggers an unhelpful log line; hide it in test output.
        _DropRule(
            name_endswith="src.autobots.action.action_type.action_factory",
            msg_contains_all=("Error in running action: daytona-direct-run",),
        ),

        # Silence expected Daytona fallback noise in app_conn_daytona.
        _DropRule(
            name_endswith="src.autobots.integrations.app_conn.app_conn_type.app_conn_daytona",
            msg_contains_all=("Unsupported owner mode for Daytona",),
        ),
        _DropRule(
            name_endswith="src.autobots.integrations.app_conn.app_conn_type.app_conn_daytona",
            msg_contains_all=("Cannot get Daytona conn",),
            predicate=lambda _n, _f, m, e: (
                "App auth doc not found" in m or "AppTypes.daytona" in m or "404" in e
            ),
        ),
    )

    def _filter(record: dict) -> bool:
        name = record.get("name", "") or ""
        fn = record.get("function", "") or ""
        msg = record.get("message", "") or ""
        exc = record.get("exception", None)
        exc_text = _exc_text(exc)

        for rule in DROP_RULES:
            if rule.matches(name=name, fn=fn, msg=msg, exc_text=exc_text):
                return False
        return True

    # Important: omit "{exception}" from format so logger.exception() does NOT print stack traces.
    sink_id = logger.add(
        sys.stderr,
        level="INFO",
        filter=_filter,
        backtrace=False,
        diagnose=False,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {name}:{function}:{line} - {message}",
    )

    try:
        register_tools()
        yield
    finally:
        logger.remove(sink_id)
        # restore a basic sink so other tests don't end up with no sink
        logger.add(sys.stderr, level="INFO")



@pytest_asyncio.fixture(scope="session")
def user():
    return UserORM(
        id="56dd105c-606e-4f6d-8e88-8001d8f4f552",
        email="sourav@runkai.ai",
    )


@pytest_asyncio.fixture(scope="session")
def ctx(user):
    return Context(user_id=user.id)


def _maybe_extract_json_text(tool_output: str) -> str:
    """
    Your tool output in OpenAI replay is often a JSON-serialized DataBlock, e.g.
    {"text":"job_id=...\\nstatus=...\\n...", "urls":[...], ...}

    This normalizes to the embedded text for regex extraction.
    """
    s = tool_output.strip()
    if s.startswith("{") and '"text"' in s:
        try:
            obj = json.loads(s)
            if isinstance(obj, dict) and isinstance(obj.get("text"), str):
                return obj["text"]
        except Exception:
            pass
    return tool_output


def _maybe_extract_urls(tool_output: str) -> list[str]:
    s = tool_output.strip()
    if s.startswith("{") and '"urls"' in s:
        try:
            obj = json.loads(s)
            urls = obj.get("urls")
            if isinstance(urls, list):
                return [u for u in urls if isinstance(u, str)]
        except Exception:
            pass
    return []


def _extract_job_id_from_text(text: str) -> Optional[str]:
    text = _maybe_extract_json_text(text)
    m = re.search(r"job_id\s*=\s*(dj_[A-Za-z0-9_\-]+)", text)
    return m.group(1) if m else None


def _extract_sandbox_id_from_text(text: str) -> Optional[str]:
    text = _maybe_extract_json_text(text)
    m = re.search(r"sandbox_id\s*=\s*([A-Za-z0-9_\-]+)", text)
    return m.group(1) if m else None


def _extract_vnc_url_from_text(text: str) -> Optional[str]:
    text = _maybe_extract_json_text(text)
    # allow empty value after '='
    m = re.search(r"vnc_url\s*=\s*(.*)", text)
    if not m:
        return None
    v = (m.group(1) or "").strip()
    if not v or v.lower() in {"none", "null"}:
        return None
    return v.split()[0]


def _extract_status_from_text(text: str) -> Optional[str]:
    text = _maybe_extract_json_text(text)
    m = re.search(r"status\s*=\s*(\w+)", text)
    return m.group(1) if m else None


def _get_tool_call_ids(cfg: Dict[str, Any], tool_name: str) -> set[str]:
    ids: set[str] = set()
    for it in cfg.get("input") or []:
        if not isinstance(it, dict):
            continue
        if it.get("type") == "function_call" and it.get("name") == tool_name:
            cid = it.get("call_id")
            if isinstance(cid, str):
                ids.add(cid)
    return ids


def _get_tool_outputs(cfg: Dict[str, Any], tool_name: str) -> list[str]:
    call_ids = _get_tool_call_ids(cfg, tool_name)
    outs: list[str] = []
    for it in cfg.get("input") or []:
        if not isinstance(it, dict):
            continue
        if it.get("type") != "function_call_output":
            continue
        cid = it.get("call_id")
        if not (isinstance(cid, str) and cid in call_ids):
            continue
        out = it.get("output")
        if isinstance(out, str):
            outs.append(out)
        else:
            outs.append(json.dumps(out, ensure_ascii=False))
    return outs


def _get_tool_calls(cfg: Dict[str, Any], tool_name: str) -> list[dict]:
    """
    Return raw function_call items for a given tool.
    This lets us assert that the model passed the expected arguments (operation/artifact_mode/output_files).
    """
    calls: list[dict] = []
    items = cfg.get("input") or []
    if not isinstance(items, list):
        return calls
    for it in items:
        if isinstance(it, dict) and it.get("type") == "function_call" and it.get("name") == tool_name:
            calls.append(it)
    return calls


def _parse_tool_arguments(call: dict) -> dict:
    """
    OpenAI Responses can store tool arguments under different keys depending on adapter:
      - arguments: JSON string
      - args: dict
      - input: dict
    We normalize to a dict.
    """
    for key in ("arguments", "args", "input"):
        if key not in call:
            continue
        raw = call.get(key)
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                obj = json.loads(raw)
                return obj if isinstance(obj, dict) else {}
            except Exception:
                return {}
    return {}


def _has_daytona_function_call(cfg: Dict[str, Any]) -> bool:
    items = cfg.get("input") or []
    if not isinstance(items, list):
        return False
    return any(
        isinstance(it, dict)
        and it.get("type") == "function_call"
        and it.get("name") == "daytona_sandbox"
        for it in items
    )


async def _maybe_assert_auth(ctx: Context, db, user: UserORM) -> None:
    """
    - OpenAI must be configured for this user.
    - Daytona must be configured either via user AppAuth OR system fallback key in Settings.
    """
    settings = SettingsProvider.sget()

    openai_auth = await UserIntegration.get_by_app_name( # noqa F841
        ctx=ctx, db=db, user=user, app_name=AppTypes.openai
    )
    # assert openai_auth, f"Missing OpenAI auth in Mongo for user_id={user.id} ({user.email})"

    daytona_auth = await UserIntegration.get_by_app_name(
        ctx=ctx, db=db, user=user, app_name=AppTypes.daytona
    )
    if daytona_auth:
        return

    # Accept DAYTONA_API_KEY as the system fallback key in local env.
    if getattr(settings, "DAYTONA_API_KEY", "") and user.email and user.email.endswith("runkai.ai"):
        return

    pytest.skip("Missing Daytona AppAuth for user and no system fallback key (DAYTONA_API_KEY).")


async def _poll_status_direct(
    ctx: Context,
    db,
    user: UserORM,
    job_id: str,
    *,
    attempts: int = 60,
    delay_sec: float = 2.0,
) -> tuple[str, str, list[str]]:
    status_action_doc = ActionDoc(
        id=f"daytona_status_{uuid4()}",
        user_id=user.id,
        name="daytona-status-direct",
        type=ActionType.text2text_daytona,
        config={"operation": "status", "job_id": job_id},
    )

    last_text = ""
    last_urls: list[str] = []
    app_state = AppState()
    for i in range(attempts):
        res = await ActionFactory.run_action(
            ctx=ctx,
            app_state=app_state,
            action_doc=status_action_doc,
            action_input_dict=DataBlock(text="").model_dump(exclude_none=True),
            user=user,
            db=db,
        )
        if isinstance(res, Exception):
            raise res

        blocks = (res.output_dict or {}).get("blocks") or []
        assert blocks and isinstance(blocks[0], dict)
        last_text = blocks[0].get("text") or ""
        last_urls = blocks[0].get("urls") or []
        st = _extract_status_from_text(last_text) or ""
        logger.info("Direct poll {} status={} (first 400 chars):\n{}", i, st, last_text[:400])

        if st in {"completed", "failed", "canceled"}:
            return st, last_text, last_urls

        await asyncio.sleep(delay_sec)

    # Best-effort cleanup: if a job never reaches a terminal state within our poll budget,
    # attempt to cancel it so we don't leak sandboxes in CI/dev.
    try:
        await _cancel_direct(ctx=ctx, db=db, user=user, job_id=job_id)
    except Exception:
        pass

    return _extract_status_from_text(last_text) or "running", last_text, last_urls


async def _cancel_direct(ctx: Context, db, user: UserORM, job_id: str) -> None:
    cancel_doc = ActionDoc(
        id=f"daytona_cancel_{uuid4()}",
        user_id=user.id,
        name="daytona-cancel-direct",
        type=ActionType.text2text_daytona,
        config={"operation": "cancel", "job_id": job_id},
    )
    app_state = AppState()
    _ = await ActionFactory.run_action(
        ctx=ctx,
        app_state=app_state,
        action_doc=cancel_doc,
        action_input_dict=DataBlock(text="").model_dump(exclude_none=True),
        user=user,
        db=db,
    )


async def _list_direct(ctx: Context, db, user: UserORM, limit: int = 30) -> list[dict]:
    list_doc = ActionDoc(
        id=f"daytona_list_{uuid4()}",
        user_id=user.id,
        name="daytona-list-direct",
        type=ActionType.text2text_daytona,
        config={"operation": "list"},
    )
    app_state = AppState()
    res = await ActionFactory.run_action(
        ctx=ctx,
        app_state=app_state,
        action_doc=list_doc,
        action_input_dict=DataBlock(text="").model_dump(exclude_none=True),
        user=user,
        db=db,
    )
    if isinstance(res, Exception):
        raise res
    blocks = (res.output_dict or {}).get("blocks") or []
    assert blocks and isinstance(blocks[0], dict)
    txt = blocks[0].get("text") or "[]"
    try:
        obj = json.loads(txt)
        return obj if isinstance(obj, list) else []
    except Exception:
        return []

async def _run_direct(
    ctx: Context,
    db,
    user: UserORM,
    *,
    code: str,
    compute_profile: str = "default",
    cpu: Optional[int] = None,
    memory_gib: Optional[int] = None,
    output_files: list[str],
    artifact_mode: str,
    wait_for_completion_sec: int = 0,
    max_runtime_minutes: int = 10,
) -> str:
    run_doc = ActionDoc(
        id=f"daytona_direct_run_{uuid4()}",
        user_id=user.id,
        name="daytona-direct-run",
        type=ActionType.text2text_daytona,
        config={
            "operation": "run",
            "lifecycle": "ephemeral",
            "max_runtime_minutes": max_runtime_minutes,
            "artifact_mode": artifact_mode,
            "code": code,
            "compute_profile": compute_profile,
            "cpu": cpu,
            "memory_gib": memory_gib,
            "wait_for_completion_sec": wait_for_completion_sec,
            "output_files": output_files,
            "pip_packages": [],
            "input_urls": [],
        },
    )
    app_state = AppState()
    res = await ActionFactory.run_action(
        ctx=ctx, app_state=app_state, action_doc=run_doc, action_input_dict=DataBlock(text="").model_dump(exclude_none=True), user=user, db=db
    )
    if isinstance(res, Exception):
        raise res
    blocks = (res.output_dict or {}).get("blocks") or []
    assert blocks and isinstance(blocks[0], dict)
    return blocks[0].get("text") or ""

async def test_action_text2text_daytona_e2e_via_openai_responses(ctx, db, user, set_test_settings):
    """
    E2E: OpenAI Responses action -> daytona_sandbox tool -> Daytona -> Mongo job record -> status terminal.
    We validate at least one artifact path without requiring APP_BASE_URL:
      - artifact_mode='auto' should fall back to presigned when API_BASE is empty.
    """
    await _maybe_assert_auth(ctx=ctx, db=db, user=user)

    settings = SettingsProvider.sget()
    callback_base = (getattr(settings, "DAYTONA_CALLBACK_BASE_URL", "") or getattr(settings, "APP_BASE_URL", "") or "").strip()
    has_callback = bool(callback_base)


    tag = uuid4().hex[:10]
    out_name = f"out_{tag}.txt"

    code = (
        f"print('DAYTONA_E2E_TAG={tag}')\n"
        "x = sum(range(1, 101))\n"
        "print('SUM_1_100=', x)\n"
        f"with open({out_name!r}, 'w', encoding='utf-8') as f:\n"
        f"    f.write('hello from {tag}')\n"
        f"print('WROTE_FILE={out_name}')\n"
    )

    tools_quick_guide = (
        "You have access to ONLY these tools:\n"
        "1) daytona_sandbox: run remote Python code in Daytona.\n\n"
        "Rules:\n"
        "- You MUST call daytona_sandbox at least once.\n"
        "- First call operation='run' with wait_for_completion_sec=0 to get a job_id.\n"
        "- For the run call, you MUST pass code exactly as provided.\n"
        f"- For the run call, you MUST set output_files=['{out_name}'].\n"
        "- For the run call, you MUST set artifact_mode='auto'.\n"
        + "- Then call operation='status' with that job_id until status is 'completed' (max 3 polls).\n"
        "- Finally respond with ONLY the final tool output text.\n"
    )

    cfg = LlmResponsesOpenaiConfig(
        provider="openai",
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": "You are Autobots. Execute the task end-to-end using tools."},
            {"role": "user", "content": tools_quick_guide},
        ],
        store=True,
        truncation="auto",
        max_output_tokens=2048,
        tools=["daytona_sandbox"],
        tool_choice="auto",
        max_tool_calls=8,
    )

    action_doc = ActionDoc(
        id=f"daytona_openai_e2e_{uuid4()}",
        user_id=user.id,
        name="daytona-openai-e2e",
        type=ActionType.text2text_llm_responses_openai,
        config=cfg.model_dump(exclude_none=True),
    )

    action_input = DataBlock(
        text=(
            "Run this Python code in Daytona and then poll status until complete:\n\n"
            + code
        )
    ).model_dump(exclude_none=True)
    app_state = AppState()
    res = await ActionFactory.run_action(
        ctx=ctx, app_state=app_state, action_doc=action_doc, action_input_dict=action_input, user=user, db=db
    )
    if isinstance(res, Exception):
        raise res

    cfg_out = res.config_dict
    assert isinstance(cfg_out, dict)

    assert _has_daytona_function_call(cfg_out), "Expected a function_call for daytona_sandbox"

    # Assert the model actually used the new fields we introduced.
    calls = _get_tool_calls(cfg_out, "daytona_sandbox")
    assert calls, "Expected at least one function_call for daytona_sandbox"
    args_list = [_parse_tool_arguments(c) for c in calls]

    # Must have at least one run call with artifact_mode=auto and correct output_files
    has_run = any(a.get("operation") == "run" for a in args_list)
    assert has_run, f"Expected a daytona_sandbox run call. args={args_list}"

    has_expected_run_args = any(
        a.get("operation") == "run"
        and a.get("artifact_mode") == "auto"
        and isinstance(a.get("output_files"), list)
        and out_name in a.get("output_files")
        and (a.get("wait_for_completion_sec") == 0 or a.get("wait_for_completion_sec") is None)
        for a in args_list
    )
    assert has_expected_run_args, f"Expected run args to include artifact_mode=auto and output_files. args={args_list}"

    tool_outputs = _get_tool_outputs(cfg_out, "daytona_sandbox")
    assert tool_outputs, "Expected at least one daytona_sandbox tool output"

    # Extract job_id
    job_id = None
    for out in tool_outputs:
        job_id = _extract_job_id_from_text(out)
        if job_id:
            break
    if not job_id:
        # Print the actual tool outputs so the failure is actionable.
        logger.error("Daytona tool outputs:\n{}", "\n\n---\n\n".join(tool_outputs[-3:]))
        pytest.fail("Could not extract job_id from Daytona tool outputs (tool likely returned an error before starting)")

    # Verify Mongo persistence (dashboard isolation)
    uj = UserDaytonaJobs(user=user, db=db)
    job_doc = await uj.get_by_job_id(ctx=ctx, job_id=job_id)
    assert job_doc, "Expected DaytonaJobs record to exist"
    assert job_doc.created_sandbox is True
    assert job_doc.reused_sandbox is False
    assert job_doc.lifecycle == "ephemeral"
    assert job_doc.job_workdir.endswith(f"/{job_id}")

    # Determine terminal status (prefer tool outputs; fallback to direct polling)
    terminal_hit = None
    terminal_output = ""
    for out in reversed(tool_outputs):
        st = _extract_status_from_text(out)
        if st in {"completed", "failed", "canceled"}:
            terminal_hit = st
            terminal_output = out
            break

    if terminal_hit is None:
        terminal_hit, last_text, _ = await _poll_status_direct(ctx=ctx, db=db, user=user, job_id=job_id)
        terminal_output = last_text

    assert terminal_hit in {"completed", "failed", "canceled"}, f"Expected terminal status, got {terminal_hit}"

    terminal_text = _maybe_extract_json_text(terminal_output)
    terminal_urls = _maybe_extract_urls(terminal_output)

    logger.info("Terminal status={} job_id={}", terminal_hit, job_id)
    logger.info("Terminal text (first 1200):\n{}", terminal_text[:1200])
    logger.info("Terminal urls: {}", terminal_urls)

    assert f"DAYTONA_E2E_TAG={tag}" in terminal_text
    assert "SUM_1_100=" in terminal_text

    # We wrote an output file; we expect a URL even without callback base because auto->presigned or server_sync fallback.
    if not terminal_urls:
        pytest.fail(
            "Expected artifact URLs but got none. "
            f"callback_base_configured={has_callback}. "
            "auto mode should succeed via presigned or server_sync fallback."
        )
    assert any(u.startswith("http://") or u.startswith("https://") for u in terminal_urls)


async def test_daytona_callback_artifact_mode_direct_success_when_base_url_configured(ctx, db, user, set_test_settings):
    """
    This test validates callback-mode success ONLY when callback base URL is configured.
    It should be skipped in local/dev where APP_BASE_URL is not set.
    """
    await _maybe_assert_auth(ctx=ctx, db=db, user=user)

    settings = SettingsProvider.sget()
    callback_base = (getattr(settings, "DAYTONA_CALLBACK_BASE_URL", "") or getattr(settings, "APP_BASE_URL", "") or "").strip()
    if not callback_base:
        pytest.skip("APP_BASE_URL/DAYTONA_CALLBACK_BASE_URL not set; cannot test callback artifact mode")

    tag = uuid4().hex[:10]
    out_name = f"out_cb_{tag}.txt"
    code = (
        f"print('CALLBACK_TAG={tag}')\n"
        f"with open({out_name!r}, 'w', encoding='utf-8') as f:\n"
        f"    f.write('hello from callback {tag}')\n"
        "print('DONE')\n"
    )

    run_text = await _run_direct(
        ctx=ctx, db=db, user=user, code=code, output_files=[out_name], artifact_mode="callback", wait_for_completion_sec=0
    )
    job_id = _extract_job_id_from_text(run_text)
    assert job_id, f"Expected job_id for callback mode, got: {run_text}"

    st, st_txt, urls = await _poll_status_direct(ctx=ctx, db=db, user=user, job_id=job_id, attempts=60, delay_sec=2.0)
    assert st in {"completed", "failed", "canceled"}
    assert f"CALLBACK_TAG={tag}" in st_txt
    assert urls, "Expected URLs for callback mode when callback base is configured"


async def test_daytona_compute_profile_medium_persists_requested_fields_e2e_direct(ctx, db, user, set_test_settings):
    """
    Phase-1: ensure compute_profile preset is accepted and requested fields are persisted.
    We do NOT assert actual provisioned CPU/RAM here (best-effort policy).
    """
    await _maybe_assert_auth(ctx=ctx, db=db, user=user)

    tag = uuid4().hex[:10]
    code = f"print('MEDIUM_COMPUTE_TAG={tag}')\nprint('OK')\n"

    run_text = await _run_direct(
        ctx=ctx,
        db=db,
        user=user,
        code=code,
        compute_profile="medium",
        output_files=[],
        artifact_mode="none",
        wait_for_completion_sec=0,
    )
    job_id = _extract_job_id_from_text(run_text)
    assert job_id, f"Expected job_id, got: {run_text}"

    st, st_txt, _ = await _poll_status_direct(ctx=ctx, db=db, user=user, job_id=job_id, attempts=60, delay_sec=2.0)
    assert st in {"completed", "failed", "canceled"}
    assert f"MEDIUM_COMPUTE_TAG={tag}" in st_txt

    uj = UserDaytonaJobs(user=user, db=db)
    job_doc = await uj.get_by_job_id(ctx=ctx, job_id=job_id)
    assert job_doc is not None
    assert job_doc.requested_cpu == 2
    assert job_doc.requested_memory_gib == 4


async def test_daytona_compute_profile_custom_persists_requested_fields_e2e_direct(ctx, db, user, set_test_settings):
    """
    Phase-1: ensure compute_profile custom is accepted and requested fields are persisted.
    Resize is best-effort; even if resize fails due to quota, job should still run and complete.
    """
    await _maybe_assert_auth(ctx=ctx, db=db, user=user)

    settings = SettingsProvider.sget()
    max_cpu = int(getattr(settings, "DAYTONA_MAX_CPU", 4) or 4)
    max_mem = int(getattr(settings, "DAYTONA_MAX_MEMORY_GIB", 8) or 8)

    # pick a non-tier-ish value, but stay within caps
    cpu = 3 if max_cpu >= 3 else max_cpu
    mem = 6 if max_mem >= 6 else max_mem
    cpu = max(cpu, 1)
    mem = max(mem, 1)

    tag = uuid4().hex[:10]
    code = f"print('CUSTOM_COMPUTE_TAG={tag}')\nprint('CPU={cpu} MEM={mem}')\nprint('OK')\n"

    run_text = await _run_direct(
        ctx=ctx,
        db=db,
        user=user,
        code=code,
        compute_profile="custom",
        cpu=cpu,
        memory_gib=mem,
        output_files=[],
        artifact_mode="none",
        wait_for_completion_sec=0,
    )
    job_id = _extract_job_id_from_text(run_text)
    assert job_id, f"Expected job_id, got: {run_text}"

    st, st_txt, _ = await _poll_status_direct(ctx=ctx, db=db, user=user, job_id=job_id, attempts=60, delay_sec=2.0)
    assert st in {"completed", "failed", "canceled"}
    assert f"CUSTOM_COMPUTE_TAG={tag}" in st_txt

    uj = UserDaytonaJobs(user=user, db=db)
    job_doc = await uj.get_by_job_id(ctx=ctx, job_id=job_id)
    assert job_doc is not None
    assert job_doc.requested_cpu == cpu
    assert job_doc.requested_memory_gib == mem


@pytest.mark.parametrize(
    "artifact_mode",
    ["auto", "presigned", "server_sync", "none", "callback"],
)
async def test_daytona_artifact_modes_e2e_direct(ctx, db, user, set_test_settings, artifact_mode: str):
    """
    Deterministic, exhaustive E2E over artifact modes (no LLM).
    This validates the actual Daytona tool behavior regardless of model adherence.

    Expected behavior when APP_BASE_URL/DAYTONA_CALLBACK_BASE_URL is not set:
      - auto: should still return URLs (runner falls back to presigned; or server_sync fallback on status)
      - presigned: should return URLs
      - server_sync: should return URLs (runner skips upload, server pulls on status)
      - none: should return NO URLs and should NOT server-sync
      - callback: should error early (no base URL), producing no job_id
    """
    await _maybe_assert_auth(ctx=ctx, db=db, user=user)

    settings = SettingsProvider.sget()
    callback_base = (getattr(settings, "DAYTONA_CALLBACK_BASE_URL", "") or getattr(settings, "APP_BASE_URL", "") or "").strip()

    tag = uuid4().hex[:10]
    out_name = f"out_{tag}.txt"
    code = (
        f"print('DIRECT_E2E_TAG={tag}')\n"
        f"with open({out_name!r}, 'w', encoding='utf-8') as f:\n"
        f"    f.write('hello from {tag}')\n"
        f"print('WROTE_FILE={out_name}')\n"
    )

    # callback mode requires base URL; test should assert error path when missing
    if artifact_mode == "callback" and not callback_base:
        run_text = await _run_direct(
            ctx=ctx, db=db, user=user, code=code, output_files=[out_name], artifact_mode="callback", wait_for_completion_sec=0
        )
        assert "requires DAYTONA_CALLBACK_BASE_URL" in run_text or "requires" in run_text.lower()
        assert _extract_job_id_from_text(run_text) is None
        return

    # Run async to force status path
    run_text = await _run_direct(
        ctx=ctx, db=db, user=user, code=code, output_files=[out_name], artifact_mode=artifact_mode, wait_for_completion_sec=0
    )
    job_id = _extract_job_id_from_text(run_text)
    assert job_id, f"Expected job_id for artifact_mode={artifact_mode}, got: {run_text}"

    # Poll
    st, st_txt, urls = await _poll_status_direct(ctx=ctx, db=db, user=user, job_id=job_id, attempts=60, delay_sec=2.0)
    assert st in {"completed", "failed", "canceled"}, f"Expected terminal, got {st} text={st_txt[:400]}"
    assert f"DIRECT_E2E_TAG={tag}" in st_txt

    if artifact_mode == "none":
        assert not urls, f"Expected no URLs for artifact_mode=none, got {urls}"
    else:
        assert urls, f"Expected URLs for artifact_mode={artifact_mode}, got none"
        assert any(u.startswith("http://") or u.startswith("https://") for u in urls)


async def test_action_text2text_daytona_modes_direct(ctx, db, user, set_test_settings):
    """
    Deterministic tool-mode coverage (no LLM):
      - run -> status -> list contains job_id
      - run long -> cancel -> status is canceled
    """
    await _maybe_assert_auth(ctx=ctx, db=db, user=user)

    # --- run fast ---
    tag = uuid4().hex[:10]
    code = f"print('DIRECT_TAG={tag}')\nprint('OK')\n"
    run_doc = ActionDoc(
        id=f"daytona_direct_run_{uuid4()}",
        user_id=user.id,
        name="daytona-direct-run",
        type=ActionType.text2text_daytona,
        config={
            "operation": "run",
            "lifecycle": "ephemeral",
            "max_runtime_minutes": 10,
            "code": code,
            "wait_for_completion_sec": 0,
            "output_files": [],
            "pip_packages": [],
            "input_urls": [],
        },
    )
    app_state = AppState()
    run_res = await ActionFactory.run_action(
        ctx=ctx, app_state=app_state, action_doc=run_doc, action_input_dict=DataBlock(text="").model_dump(), user=user, db=db
    )
    if isinstance(run_res, Exception):
        raise run_res
    txt = ((run_res.output_dict or {}).get("blocks") or [{}])[0].get("text") or ""
    job_id = _extract_job_id_from_text(txt)
    assert job_id, f"Expected job_id in direct run output, got: {txt}"

    st, st_txt, _ = await _poll_status_direct(ctx=ctx, db=db, user=user, job_id=job_id)
    assert st in {"completed", "failed"}
    assert f"DIRECT_TAG={tag}" in st_txt

    # list contains job_id
    listed = await _list_direct(ctx=ctx, db=db, user=user, limit=50)
    assert any(isinstance(it, dict) and it.get("job_id") == job_id for it in listed), "Expected job_id in list() output"

    # Optional: ensure artifacts dict exists in list output (schema stability)
    item = next((it for it in listed if isinstance(it, dict) and it.get("job_id") == job_id), None)
    assert item is not None
    assert "status" in item
    assert item.get("created_sandbox") is True
    assert item.get("lifecycle") == "ephemeral"
    assert isinstance(item.get("job_workdir"), str)
    assert item["job_workdir"].endswith(f"/{job_id}")
    assert item.get("cleanup_completed") is True
    assert item.get("cleanup_error") == ""

    # --- cancel long job ---
    tag2 = uuid4().hex[:10]
    long_code = (
        f"print('CANCEL_TAG={tag2}')\n"
        "import time\n"
        "time.sleep(120)\n"
        "print('SHOULD_NOT_REACH')\n"
    )
    long_run_doc = ActionDoc(
        id=f"daytona_direct_run_long_{uuid4()}",
        user_id=user.id,
        name="daytona-direct-run-long",
        type=ActionType.text2text_daytona,
        config={
            "operation": "run",
            "lifecycle": "ephemeral",
            "max_runtime_minutes": 5,
            "code": long_code,
            "wait_for_completion_sec": 0,
            "output_files": [],
            "pip_packages": [],
            "input_urls": [],
        },
    )
    long_res = await ActionFactory.run_action(
        ctx=ctx, app_state=app_state, action_doc=long_run_doc, action_input_dict=DataBlock(text="").model_dump(), user=user, db=db
    )
    if isinstance(long_res, Exception):
        raise long_res
    long_txt = ((long_res.output_dict or {}).get("blocks") or [{}])[0].get("text") or ""
    long_job_id = _extract_job_id_from_text(long_txt)
    assert long_job_id, f"Expected job_id for cancel test, got: {long_txt}"

    # cancel quickly
    await _cancel_direct(ctx=ctx, db=db, user=user, job_id=long_job_id)

    # status should become canceled (or at least not running, depending on cleanup timing)
    st2, st2_txt, _ = await _poll_status_direct(ctx=ctx, db=db, user=user, job_id=long_job_id, attempts=10, delay_sec=1.0)
    assert st2 in {"canceled", "failed", "completed"}, f"Unexpected post-cancel status={st2} text={st2_txt[:300]}"
    assert "cleanup_completed=true" in st2_txt
    assert "cleanup_error=" in st2_txt

    long_job_doc = await UserDaytonaJobs(
        user=user,
        db=db,
    ).get_by_job_id(
        ctx=ctx,
        job_id=long_job_id,
    )
    assert long_job_doc is not None
    assert long_job_doc.cleanup_completed is True
    assert long_job_doc.cleanup_error == ""


async def test_daytona_persistent_then_reuse_sandbox_e2e_direct(
    ctx,
    db,
    user,
    set_test_settings,
):
    """
    E2E (direct, no LLM) for explicit persistence and reuse:
      - lifecycle='persistent' creates a non-ephemeral sandbox
      - terminal completion stops rather than deletes the created sandbox
      - reuse_sandbox_id starts and reuses that same sandbox
      - reuse does not automatically stop or delete the supplied sandbox
      - filesystem state persists between jobs

    We explicitly delete the sandbox at the end to avoid leaks.
    """
    await _maybe_assert_auth(ctx=ctx, db=db, user=user)

    daytona_conn = await AppConn.get_daytona(ctx=ctx, db=db, user=user)
    if isinstance(daytona_conn, Exception):
        pytest.skip(f"Daytona not available: {daytona_conn}")

    tag = uuid4().hex[:10]
    # Each job has a distinct workdir. Use an absolute path outside those
    # per-job directories to test sandbox-level filesystem persistence.
    persist_file = f"/home/daytona/persist_{tag}.txt"

    # --- Run 1: create a persistent sandbox and write shared filesystem state.
    code1 = (
        f"print('REUSE_TAG={tag}')\n"
        f"with open({persist_file!r}, 'w', encoding='utf-8') as f:\n"
        f"    f.write('hello_persist_{tag}')\n"
        f"print('WROTE_PERSIST_FILE={persist_file}')\n"
    )

    run1_doc = ActionDoc(
        id=f"daytona_reuse_run1_{uuid4()}",
        user_id=user.id,
        name="daytona-reuse-run1",
        type=ActionType.text2text_daytona,
        config={
            "operation": "run",
            "artifact_mode": "none",
            "lifecycle": "persistent",
            "idle_timeout_minutes": 0,
            "confirm_persistent": True,
            "code": code1,
            "wait_for_completion_sec": 0,
            "output_files": [],
            "pip_packages": [],
            "input_urls": [],
        },
    )
    app_state = AppState()
    run1_res = await ActionFactory.run_action(
        ctx=ctx,
        app_state=app_state,
        action_doc=run1_doc,
        action_input_dict=DataBlock(text="").model_dump(exclude_none=True),
        user=user,
        db=db,
    )
    if isinstance(run1_res, Exception):
        raise run1_res
    run1_txt = ((run1_res.output_dict or {}).get("blocks") or [{}])[0].get("text") or ""
    job1 = _extract_job_id_from_text(run1_txt)
    sb1 = _extract_sandbox_id_from_text(run1_txt)
    assert job1, f"Expected job_id in run1 output, got: {run1_txt}"
    assert sb1, f"Expected sandbox_id in run1 output, got: {run1_txt}"

    # Terminal persistent jobs are stopped, not deleted.
    st1, st1_txt, _ = await _poll_status_direct(ctx=ctx, db=db, user=user, job_id=job1, attempts=60, delay_sec=2.0)
    assert st1 in {"completed", "failed", "canceled"}, f"Expected terminal for run1, got {st1} text={st1_txt[:400]}"
    assert f"REUSE_TAG={tag}" in st1_txt, f"Expected tag in run1 logs, got: {st1_txt[:500]}"
    assert "cleanup_mode=stop" in st1_txt

    uj = UserDaytonaJobs(user=user, db=db)
    job1_doc = await uj.get_by_job_id(ctx=ctx, job_id=job1)
    assert job1_doc is not None
    assert job1_doc.created_sandbox is True
    assert job1_doc.reused_sandbox is False
    assert job1_doc.lifecycle == "persistent"
    assert job1_doc.cleanup_completed is True
    assert job1_doc.cleanup_error == ""

    # Restart explicitly to verify that stop preserved the filesystem.
    await daytona_conn.start_sandbox(
        ctx=ctx,
        sandbox_id=sb1,
        timeout_sec=120,
    )
    rc, out = await daytona_conn.exec(
        ctx=ctx,
        sandbox_id=sb1,
        command=f"test -f {persist_file!r} && echo PERSIST_OK || echo PERSIST_MISSING",
        timeout_sec=20,
    )
    assert rc == 0 and "PERSIST_OK" in (out or ""), f"Expected persisted file to exist. rc={rc} out={out!r}"

    # --- Run 2: reuse the same sandbox id and read the file written in run1
    code2 = (
        f"print('REUSE2_TAG={tag}')\n"
        f"data = open({persist_file!r}, 'r', encoding='utf-8').read().strip()\n"
        f"print('PERSIST_READ=' + data)\n"
    )
    run2_doc = ActionDoc(
        id=f"daytona_reuse_run2_{uuid4()}",
        user_id=user.id,
        name="daytona-reuse-run2",
        type=ActionType.text2text_daytona,
        config={
            "operation": "run",
            "artifact_mode": "none",
            "reuse_sandbox_id": sb1,
            "code": code2,
            "wait_for_completion_sec": 0,
            "output_files": [],
            "pip_packages": [],
            "input_urls": [],
        },
    )
    run2_res = await ActionFactory.run_action(
        ctx=ctx,
        app_state=app_state,
        action_doc=run2_doc,
        action_input_dict=DataBlock(text="").model_dump(exclude_none=True),
        user=user,
        db=db,
    )
    if isinstance(run2_res, Exception):
        raise run2_res
    run2_txt = ((run2_res.output_dict or {}).get("blocks") or [{}])[0].get("text") or ""
    job2 = _extract_job_id_from_text(run2_txt)
    sb2 = _extract_sandbox_id_from_text(run2_txt)
    assert job2, f"Expected job_id in run2 output, got: {run2_txt}"
    assert sb2 == sb1, f"Expected reuse sandbox_id to match. sb1={sb1} sb2={sb2} out={run2_txt}"

    st2, st2_txt, _ = await _poll_status_direct(ctx=ctx, db=db, user=user, job_id=job2, attempts=60, delay_sec=2.0)
    assert st2 in {"completed", "failed", "canceled"}, f"Expected terminal for run2, got {st2} text={st2_txt[:400]}"
    assert f"REUSE2_TAG={tag}" in st2_txt
    assert f"PERSIST_READ=hello_persist_{tag}" in st2_txt, f"Expected persisted read in run2 logs. text={st2_txt[:800]}"
    assert "cleanup_mode=none" in st2_txt

    job2_doc = await uj.get_by_job_id(ctx=ctx, job_id=job2)
    assert job2_doc is not None
    assert job2_doc.created_sandbox is False
    assert job2_doc.reused_sandbox is True
    assert job2_doc.lifecycle == "existing"
    assert job2_doc.reused_sandbox_id == sb1
    assert job2_doc.cleanup_completed is True
    assert job2_doc.cleanup_error == ""

    # Cleanup: reuse never automatically stops or deletes the supplied sandbox.
    try:
        await daytona_conn.delete_sandbox(ctx=ctx, sandbox_id=sb1)
    except Exception as e:
        logger.warning("Cleanup delete_sandbox failed (non-fatal): {}", e)


async def test_daytona_vnc_link_best_effort_e2e_direct(ctx, db, user, set_test_settings):
    """
    Best-effort E2E: request signed noVNC preview URL (should point to /vnc.html after your rewrite).
    Skips if the deployed Daytona SDK/runtime does not support computer_use or signed previews.
    """
    await _maybe_assert_auth(ctx=ctx, db=db, user=user)

    tag = uuid4().hex[:10]
    code = f"print('VNC_E2E_TAG={tag}')\nprint('OK')\n"

    run_doc = ActionDoc(
        id=f"daytona_vnc_run_{uuid4()}",
        user_id=user.id,
        name="daytona-vnc-run",
        type=ActionType.text2text_daytona,
        config={
            "operation": "run",
            "artifact_mode": "none",
            "lifecycle": "persistent",
            "idle_timeout_minutes": 0,
            "confirm_persistent": True,
            "need_vnc_link": True,
            "vnc_port": 6080,
            "vnc_link_ttl_sec": 300,
            "code": code,
            "wait_for_completion_sec": 0,
            "output_files": [],
            "pip_packages": [],
            "input_urls": [],
        },
    )
    app_state = AppState()
    res = await ActionFactory.run_action(
        ctx=ctx,
        app_state=app_state,
        action_doc=run_doc,
        action_input_dict=DataBlock(text="").model_dump(exclude_none=True),
        user=user,
        db=db,
    )
    if isinstance(res, Exception):
        raise res

    txt = ((res.output_dict or {}).get("blocks") or [{}])[0].get("text") or ""
    sandbox_id = _extract_sandbox_id_from_text(txt)
    vnc_url = _extract_vnc_url_from_text(txt)

    assert sandbox_id, f"Expected sandbox_id, got: {txt}"

    if not vnc_url:
        logger.warning("No vnc_url returned. Output text:\n{}", txt[:1200])
        pytest.skip("VNC/signed preview not available in this environment.")

    p = urlparse(vnc_url)
    if p.scheme not in {"http", "https"}:
        pytest.skip(f"Unexpected vnc_url scheme: {vnc_url}")

    # After your latest change, vnc_url should land on the UI page (not directory listing)
    assert p.path.endswith("vnc.html"), f"Expected vnc_url to end with vnc.html, got path={p.path!r} url={vnc_url}"

    # Cleanup explicitly. Persistent creation does not auto-delete, and this
    # test does not poll the job to trigger terminal stop cleanup.
    daytona_conn = await AppConn.get_daytona(ctx=ctx, db=db, user=user)
    if not isinstance(daytona_conn, Exception):
        try:
            await daytona_conn.delete_sandbox(ctx=ctx, sandbox_id=sandbox_id)
        except Exception as e:
            logger.warning("Cleanup delete_sandbox failed (non-fatal): {}", e)
