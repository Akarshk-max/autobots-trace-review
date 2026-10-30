# tests/action/action_type/test_action_text2text_daytona_tool_e2e.py
import asyncio
import json
import re
import time
import warnings
from typing import Any, Dict, Optional
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from loguru import logger

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.action.action_type.tool_routes import register_tools
from src.autobots.integrations.user_integration import UserIntegration
from src.autobots.integrations.app_types import AppTypes
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
    # keep output readable; suppress common warnings noise
    warnings.filterwarnings("ignore", message=r".*PydanticDeprecatedSince20.*")
    warnings.filterwarnings("ignore", category=DeprecationWarning, message=r".*websockets\.legacy is deprecated.*")

    # Use default loguru sink(s) already configured by the suite; we only need tool routes.
    register_tools()
    yield


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
    calls: list[dict] = []
    items = cfg.get("input") or []
    if not isinstance(items, list):
        return calls
    for it in items:
        if isinstance(it, dict) and it.get("type") == "function_call" and it.get("name") == tool_name:
            calls.append(it)
    return calls


def _parse_tool_arguments(call: dict) -> dict:
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

def _fmt(obj: Any, **vars: str) -> Any:
    """
    Recursively apply .format() to strings inside nested dict/list structures.
    This keeps TEST_CASES readable while still allowing {run_id}/{stage1}/{stage2} substitution.
    """
    if isinstance(obj, str):
        return obj.format(**vars)
    if isinstance(obj, list):
        return [_fmt(x, **vars) for x in obj]
    if isinstance(obj, dict):
        return {k: _fmt(v, **vars) for k, v in obj.items()}
    return obj

def _extract_distinct_job_ids(tool_outputs: list[str]) -> list[str]:
    ids: list[str] = []
    for out in tool_outputs:
        jid = _extract_job_id_from_text(out)
        if jid and (not ids or ids[-1] != jid):
            ids.append(jid)
    return ids

def _matches_run_spec(args: dict, spec: dict) -> bool:
    """
    spec supports:
      - output_files_eq: list[str]
      - pip_contains: list[str]
      - min_input_urls: int
      - lifecycle_eq: str
      - max_runtime_minutes_eq: int
    """
    if "output_files_eq" in spec and args.get("output_files") != spec["output_files_eq"]:
        return False
    if "pip_contains" in spec:
        pkgs = args.get("pip_packages")
        if not isinstance(pkgs, list):
            return False
        for p in spec["pip_contains"]:
            if p not in pkgs:
                return False
    if "min_input_urls" in spec:
        urls = args.get("input_urls")
        if not isinstance(urls, list) or len(urls) < int(spec["min_input_urls"]):
            return False
    if "lifecycle_eq" in spec:
        if args.get("lifecycle") != spec["lifecycle_eq"]:
            return False
    if "max_runtime_minutes_eq" in spec:
        try:
            if int(args.get("max_runtime_minutes")) != int(
                spec["max_runtime_minutes_eq"]
            ):
                return False
        except Exception:
            return False

    # Optional advanced pip tuning assertions
    if "pip_wall_timeout_sec_eq" in spec:
        v = args.get("pip_wall_timeout_sec")
        try:
            if int(v) != int(spec["pip_wall_timeout_sec_eq"]):
                return False
        except Exception:
            return False
    if "pip_socket_timeout_sec_eq" in spec:
        v = args.get("pip_socket_timeout_sec")
        try:
            if int(v) != int(spec["pip_socket_timeout_sec_eq"]):
                return False
        except Exception:
            return False
    if "pip_retries_eq" in spec:
        v = args.get("pip_retries")
        try:
            if int(v) != int(spec["pip_retries_eq"]):
                return False
        except Exception:
            return False

    return True

def _find_latest_artifact_url_from_tool_outputs(tool_outputs: list[str], *, filename: str) -> Optional[str]:
    """
    The agent may retry, producing multiple job_ids and multiple status outputs.
    Return the most recent URL that contains the requested filename.
    """
    for out in reversed(tool_outputs):
        urls = _maybe_extract_urls(out)
        if not urls:
            continue
        for u in urls:
            if isinstance(u, str) and filename in u:
                return u
    return None

def _find_latest_terminal_text_with_filename(tool_outputs: list[str], *, filename: str) -> Optional[str]:
    """
    Helpful debug: return the latest terminal tool output text (completed/failed/canceled)
    that includes a URL for the given filename.
    """
    for out in reversed(tool_outputs):
        txt = _maybe_extract_json_text(out)
        st = _extract_status_from_text(txt) or ""
        if st not in {"completed", "failed", "canceled"}:
            continue
        urls = _maybe_extract_urls(out)
        if any(isinstance(u, str) and filename in u for u in urls):
            return txt
    return None


async def _maybe_assert_auth(ctx: Context, db, user: UserORM) -> None:
    """
    - OpenAI must be configured (we accept OPENAI_API_KEY as system fallback).
    - Daytona must be configured either via user AppAuth OR system fallback key in Settings.
    """
    settings = SettingsProvider.sget()
    if not getattr(settings, "OPENAI_API_KEY", ""):
        pytest.skip("Missing OPENAI_API_KEY (required for OpenAI Responses API live test).")

    # Daytona user AppAuth?
    daytona_auth = await UserIntegration.get_by_app_name(ctx=ctx, db=db, user=user, app_name=AppTypes.daytona)
    if daytona_auth:
        return

    # Or system fallback key:
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
        logger.info("Direct poll {} status={} (first 300 chars):\n{}", i, st, last_text[:300])

        if st in {"completed", "failed", "canceled"}:
            return st, last_text, last_urls

        await asyncio.sleep(delay_sec)

    # best-effort cancel on timeout
    cancel_doc = ActionDoc(
        id=f"daytona_cancel_{uuid4()}",
        user_id=user.id,
        name="daytona-cancel-direct",
        type=ActionType.text2text_daytona,
        config={"operation": "cancel", "job_id": job_id},
    )
    app_state = AppState()
    try:
        await ActionFactory.run_action(
            ctx=ctx,
            app_state=app_state,
            action_doc=cancel_doc,
            action_input_dict=DataBlock(text="").model_dump(exclude_none=True),
            user=user,
            db=db,
        )
    except Exception:
        pass

    return _extract_status_from_text(last_text) or "running", last_text, last_urls


async def _http_get_text(url: str, timeout_sec: int = 30) -> str:
    deadline = time.time() + timeout_sec
    async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
        last_err = None
        while time.time() < deadline:
            try:
                r = await client.get(url)
                if r.status_code == 200:
                    return r.text
                last_err = RuntimeError(f"HTTP {r.status_code}")
            except Exception as e:
                last_err = e
            await asyncio.sleep(1)
        raise RuntimeError(f"Failed to GET {url} within {timeout_sec}s: {last_err}")

TEST_CASES: list[dict[str, Any]] = [
    {
        "id": "xlsx_analysis",
        "enabled": True,
        "stage1_filename_tmpl": "data_{run_id}.xlsx",
        "stage2_filename_tmpl": "analysis_{run_id}.txt",
        "prompt_tmpl": """
You have access to ONE tool: daytona_sandbox.

Goal: do a two-stage data workflow using Daytona.

Rules you MUST follow:
1) You MUST perform exactly TWO Daytona jobs (two separate operation='run' tool calls).
2) For EACH job:
   - call operation='run' with wait_for_completion_sec=0
   - set lifecycle='ephemeral'
   - set max_runtime_minutes=10
   - then poll operation='status' with the returned job_id until status is completed (max 6 polls per job).
3) You MUST use artifact_mode='auto' for both jobs.
4) You MUST use output_files exactly as specified for each job.
5) Job 2 MUST use input_urls and MUST include python-calamine in pip_packages.

Job 1 (generate XLSX):
- Use pip_packages=['openpyxl'].
- Write Python code that creates an Excel file named '{stage1}'.
- The file must have a sheet 'Sheet1' with a header row:
    value, square
  and then 10 rows for values 1..10 and their squares.
- Print a line: CREATED_XLSX={stage1}
- Set output_files=['{stage1}'].

Job 2 (analyze XLSX and produce analysis text):
- Use pip_packages=['python-calamine','openpyxl'].
- Use input_urls=[<the XLSX URL returned from Job 1 status output>].
- Write Python code that:
  - Reads '{stage1}' from the downloaded file in the sandbox working directory.
  - Prefer python-calamine to parse the sheet; if it fails for any reason, fall back to openpyxl.
  - Computes:
      sum_values = 55
      avg_values = 5.5
  - Writes a text file '{stage2}' containing at least:
      run_id={run_id}
      sum_values=55
      avg_values=5.5
      engine=<calamine or openpyxl>
  - Print a line: WROTE_ANALYSIS={stage2}
- Set output_files=['{stage2}'].

Finally:
- Respond with ONLY the final daytona_sandbox tool output text from Job 2 status.
""".strip(),
        "run_specs_tmpl": [
            {
                "pip_contains": ["openpyxl"],
                "output_files_eq": ["{stage1}"],
                "lifecycle_eq": "ephemeral",
                "max_runtime_minutes_eq": 10,
            },
            {
                "pip_contains": ["python-calamine"],
                "output_files_eq": ["{stage2}"],
                "min_input_urls": 1,
                "lifecycle_eq": "ephemeral",
                "max_runtime_minutes_eq": 10,
            },
        ],
        "stage2_contains_tmpl": ["run_id={run_id}", "sum_values=55", "avg_values=5.5", "engine="],
        "direct_poll_attempts": 120,
        "direct_poll_delay_sec": 2.0,
    },
    {
        "id": "csv_fuzzy",
        "enabled": True,  # set False to disable this case quickly
        "stage1_filename_tmpl": "names_{run_id}.csv",
        "stage2_filename_tmpl": "fuzzy_report_{run_id}.txt",
        "prompt_tmpl": """
You have access to ONE tool: daytona_sandbox.

Goal: do a two-stage CSV workflow using Daytona and produce a report file.

Rules you MUST follow:
1) You MUST perform exactly TWO Daytona jobs (two separate operation='run' tool calls).
2) For EACH job:
   - call operation='run' with wait_for_completion_sec=0
   - set lifecycle='ephemeral'
   - set max_runtime_minutes=10
   - then poll operation='status' with the returned job_id until status is completed (max 6 polls per job).
3) You MUST use artifact_mode='auto' for both jobs.
4) You MUST use output_files exactly as specified for each job.
5) Job 2 MUST use input_urls and MUST include rapidfuzz in pip_packages.

Job 1 (generate CSV):
- Use pip_packages=[].
- Write '{stage1}' with rows:
  Alice,Paris
  Bob,London
  John,NYC
  Johnny,Boston
- Print: CREATED_CSV={stage1}
- Set output_files=['{stage1}'].

Job 2 (fuzzy match and report):
- Use pip_packages=['rapidfuzz'].
- Use input_urls=[<the CSV URL returned from Job 1 status output>].
- Read '{stage1}' from the sandbox working directory.
- Use rapidfuzz to find the best match for query 'jon' among the names.
- Write '{stage2}' containing at least:
    run_id={run_id}
    query=jon
    best_match=<some name>
    score=<number>
- Print: WROTE_REPORT={stage2}
- Set output_files=['{stage2}'].

Finally:
- Respond with ONLY the final daytona_sandbox tool output text from Job 2 status.
""".strip(),
        "run_specs_tmpl": [
            {
                "output_files_eq": ["{stage1}"],
                "lifecycle_eq": "ephemeral",
                "max_runtime_minutes_eq": 10,
            },
            {
                "pip_contains": ["rapidfuzz"],
                "output_files_eq": ["{stage2}"],
                "min_input_urls": 1,
                "lifecycle_eq": "ephemeral",
                "max_runtime_minutes_eq": 10,
            },
        ],
        "stage2_contains_tmpl": ["run_id={run_id}", "query=jon", "best_match=", "score="],
        # allow extra time: pip install may take longer; direct poll fallback makes this robust
        "direct_poll_attempts": 180,
        "direct_poll_delay_sec": 2.0,
    },

    # -----------------------------
    # Heavy / slower cases (opt-in)
    # -----------------------------
    {
        "id": "heavy_pandas_pyarrow",
        "enabled": False,
        "stage1_filename_tmpl": "big_{run_id}.csv",
        "stage2_filename_tmpl": "big_report_{run_id}.txt",
        "prompt_tmpl": """
You have access to ONE tool: daytona_sandbox.

Goal: two-stage workflow that exercises heavier dependencies.

Rules you MUST follow:
1) Exactly TWO Daytona jobs (two operation='run' calls).
2) Each job: run with wait_for_completion_sec=0, then poll status until completed (max 8 polls per job).
3) artifact_mode='auto' always.
4) output_files exactly as specified.
5) Job 2 MUST use input_urls and MUST include both pandas and pyarrow in pip_packages.

Job 1:
- pip_packages=[]
- Generate a CSV file named '{stage1}' with header: id,value
- Write exactly 50000 rows:
  id from 1..50000
  value = id % 100
- Print: CREATED_BIG_CSV={stage1}
- output_files=['{stage1}']

Job 2:
- pip_packages=['pandas','pyarrow']
- input_urls=[<CSV URL from Job 1 status output>]
- Read '{stage1}' and compute:
    rows=50000
    sum_value = sum(value)
    mean_value = average(value)
- Write '{stage2}' containing at least:
    run_id={run_id}
    rows=50000
    sum_value=
    mean_value=
- Print: WROTE_BIG_REPORT={stage2}
- output_files=['{stage2}']

Finally: respond with ONLY the final daytona_sandbox tool output text from Job 2 status.
""".strip(),
        "run_specs_tmpl": [
            {"output_files_eq": ["{stage1}"]},
            {"pip_contains": ["pandas", "pyarrow"], "output_files_eq": ["{stage2}"], "min_input_urls": 1},
        ],
        "stage2_contains_tmpl": ["run_id={run_id}", "rows=50000", "sum_value=", "mean_value="],
        # Heavier install + IO: allow more time
        "direct_poll_attempts": 300,
        "direct_poll_delay_sec": 2.0,
    },
    {
        "id": "heavy_scikit_learn",
        "enabled": False,
        "stage1_filename_tmpl": "train_{run_id}.csv",
        "stage2_filename_tmpl": "sk_report_{run_id}.txt",
        "prompt_tmpl": """
You have access to ONE tool: daytona_sandbox.

Goal: two-stage workflow that trains a tiny model (heavier deps).

Rules you MUST follow:
1) Exactly TWO Daytona jobs (two operation='run' calls).
2) Each job: run with wait_for_completion_sec=0, then poll status until completed (max 8 polls per job).
3) artifact_mode='auto' always.
4) output_files exactly as specified.
5) Job 2 MUST use input_urls and MUST include scikit-learn in pip_packages.
6) Job 2 MUST set advanced pip tuning params:
   - pip_wall_timeout_sec=1200
   - pip_socket_timeout_sec=120
   - pip_retries=5

Job 1:
- pip_packages=[]
- Create CSV '{stage1}' with header: x1,x2,y
- Create 2000 rows of synthetic data such that:
    y = 1 if (x1 + x2) > 0 else 0
  where x1 and x2 are deterministic pseudo-random floats (seeded).
- Print: CREATED_TRAIN_CSV={stage1}
- output_files=['{stage1}']

Job 2:
- pip_packages=['scikit-learn','pandas']
- Set pip_wall_timeout_sec=1200, pip_socket_timeout_sec=120, pip_retries=5 in the tool call arguments.
- input_urls=[<CSV URL from Job 1 status output>]
- Train a LogisticRegression (or similar) on the dataset and compute accuracy on the same data.
- Write '{stage2}' containing at least:
    run_id={run_id}
    pip_wall_timeout_sec=1200
    pip_socket_timeout_sec=120
    pip_retries=5
    rows=2000
    accuracy=
    model=logreg
- Print: WROTE_SK_REPORT={stage2}
- output_files=['{stage2}']

Finally: respond with ONLY the final daytona_sandbox tool output text from Job 2 status.
""".strip(),
        "run_specs_tmpl": [
            {"output_files_eq": ["{stage1}"]},
            {
                "pip_contains": ["scikit-learn"],
                "output_files_eq": ["{stage2}"],
                "min_input_urls": 1,
                "pip_wall_timeout_sec_eq": 1200,
                "pip_socket_timeout_sec_eq": 120,
                "pip_retries_eq": 5,
            },
        ],
        "stage2_contains_tmpl": [
            "run_id={run_id}",
            "pip_wall_timeout_sec=1200",
            "pip_socket_timeout_sec=120",
            "pip_retries=5",
            "rows=2000",
            "accuracy=",
            "model=logreg",
        ],
        # scikit-learn can take a while to install
        "direct_poll_attempts": 420,
        "direct_poll_delay_sec": 2.0,
    },
    {
        "id": "heavy_polars",
        "enabled": False,
        "stage1_filename_tmpl": "polars_{run_id}.csv",
        "stage2_filename_tmpl": "polars_report_{run_id}.txt",
        "prompt_tmpl": """
You have access to ONE tool: daytona_sandbox.

Goal: exercise a larger wheel install + basic compute using polars.

Rules you MUST follow:
1) Exactly TWO Daytona jobs (two operation='run' calls).
2) Each job: run with wait_for_completion_sec=0, then poll status until completed (max 8 polls per job).
3) artifact_mode='auto' always.
4) output_files exactly as specified.
5) Job 2 MUST use input_urls and MUST include polars in pip_packages.

Job 1:
- pip_packages=[]
- Create CSV '{stage1}' with header: a,b
- Write 200000 rows with:
    a = row index
    b = a % 7
- Print: CREATED_POLARS_CSV={stage1}
- output_files=['{stage1}']

Job 2:
- pip_packages=['polars']
- input_urls=[<CSV URL from Job 1 status output>]
- Read '{stage1}' using polars and compute:
    rows=200000
    sum_b = sum(b)
- Write '{stage2}' containing at least:
    run_id={run_id}
    rows=200000
    sum_b=
- Print: WROTE_POLARS_REPORT={stage2}
- output_files=['{stage2}']

Finally: respond with ONLY the final daytona_sandbox tool output text from Job 2 status.
""".strip(),
        "run_specs_tmpl": [
            {"output_files_eq": ["{stage1}"]},
            {"pip_contains": ["polars"], "output_files_eq": ["{stage2}"], "min_input_urls": 1},
        ],
        "stage2_contains_tmpl": ["run_id={run_id}", "rows=200000", "sum_b="],
        "direct_poll_attempts": 300,
        "direct_poll_delay_sec": 2.0,
    },
]


@pytest.mark.parametrize(
    "case",
    [c for c in TEST_CASES if c.get("enabled")],
    ids=lambda c: c.get("id", "case"),
)
async def test_openai_agent_daytona_tool_workflows_e2e(ctx, db, user, set_test_settings, case: dict[str, Any]):
    await _maybe_assert_auth(ctx=ctx, db=db, user=user)

    run_id = uuid4().hex[:10]
    stage1 = str(case["stage1_filename_tmpl"]).format(run_id=run_id)
    stage2 = str(case["stage2_filename_tmpl"]).format(run_id=run_id)
    vars = {"run_id": run_id, "stage1": stage1, "stage2": stage2}

    user_prompt: str = _fmt(case["prompt_tmpl"], **vars)
    run_specs: list[dict[str, Any]] = _fmt(case["run_specs_tmpl"], **vars)
    stage2_contains: list[str] = _fmt(case["stage2_contains_tmpl"], **vars)
    direct_poll_attempts: int = int(case.get("direct_poll_attempts", 120))
    direct_poll_delay_sec: float = float(case.get("direct_poll_delay_sec", 2.0))


    cfg = LlmResponsesOpenaiConfig(
        provider="openai",
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": "You are an automation agent. Follow the rules exactly."},
            {"role": "user", "content": user_prompt},
        ],
        store=True,
        truncation="auto",
        max_output_tokens=2048,
        tools=["daytona_sandbox"],
        tool_choice="auto",
        max_tool_calls=16,
    )

    action_doc = ActionDoc(
        id=f"openai_daytona_agent_{case.get('id','case')}_{run_id}",
        user_id=user.id,
        name=f"openai-daytona-agent-{case.get('id','case')}-{run_id}",
        type=ActionType.text2text_llm_responses_openai,
        config=cfg.model_dump(exclude_none=True),
    )
    app_state = AppState()
    res = await ActionFactory.run_action(
        ctx=ctx,
        app_state=app_state,
        action_doc=action_doc,
        action_input_dict=DataBlock(text="").model_dump(exclude_none=True),
        user=user,
        db=db,
    )
    if isinstance(res, Exception):
        raise res

    cfg_out = res.config_dict
    assert isinstance(cfg_out, dict)

    calls = _get_tool_calls(cfg_out, "daytona_sandbox")
    assert calls, "Expected at least one daytona_sandbox function_call"

    args_list = [_parse_tool_arguments(c) for c in calls]
    run_calls = [a for a in args_list if a.get("operation") == "run"]
    assert len(run_calls) >= 2, f"Expected >=2 run calls, got {len(run_calls)} args={args_list}"

    # Validate expected run-call shapes (best-effort; model may retry/add extra calls)
    for spec in run_specs:
        ok = any(_matches_run_spec(a, spec) for a in run_calls)
        assert ok, f"Missing expected run spec {spec}. run_calls={run_calls}"

    tool_outputs = _get_tool_outputs(cfg_out, "daytona_sandbox")
    assert tool_outputs, "Expected at least one tool output"

    stage1_url = _find_latest_artifact_url_from_tool_outputs(tool_outputs, filename=stage1)
    assert stage1_url, f"Could not find stage1 artifact URL for {stage1}. Recent outputs={tool_outputs[-3:]}"

    # Stage2 artifact URL might be missing if the agent stopped polling early while job2 was still running.
    stage2_url = _find_latest_artifact_url_from_tool_outputs(tool_outputs, filename=stage2)
    if not stage2_url:
        job_ids = _extract_distinct_job_ids(tool_outputs)
        assert job_ids, f"Could not extract job_id from tool outputs. Recent outputs={tool_outputs[-3:]}"
        last_job_id = job_ids[-1]

        st, st_txt, urls = await _poll_status_direct(
            ctx=ctx,
            db=db,
            user=user,
            job_id=last_job_id,
            attempts=direct_poll_attempts,
            delay_sec=direct_poll_delay_sec,
        )
        assert st in {"completed", "failed", "canceled"}
        stage2_url = next((u for u in (urls or []) if isinstance(u, str) and stage2 in u), None)
        if not stage2_url:
            logger.error("Direct poll terminal text (first 1200):\n{}", _maybe_extract_json_text(st_txt)[:1200])
            logger.error("Direct poll urls: {}", urls)
            pytest.fail(f"Could not find stage2 artifact URL for {stage2} even after direct polling.")

    body = await _http_get_text(stage2_url, timeout_sec=60)
    for s in stage2_contains:
        assert s in body, f"Missing {s!r} in stage2 artifact. First 1200:\n{body[:1200]}"

    logger.info("case_id={} stage1_url={} stage2_url={}", case.get("id"), stage1_url, stage2_url)
