import io
import time
from typing import Optional
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from loguru import logger

from src.autobots.action.action.action_doc_model import ActionDoc, ActionResult
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.action.action_type.tool_routes import register_tools
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.conn.openai.openai_responses.openai_responses import LlmResponsesOpenaiConfig
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.user.user_orm_model import UserORM

pytestmark = [pytest.mark.asyncio]


# -----------------------------------------------------------------------------
# IMPORTANT: override the session autouse `start()` from tests/conftest.py
# We do NOT want setup_test_db() or sleep(15) for this live "Sourav user" test.
# -----------------------------------------------------------------------------
@pytest_asyncio.fixture(scope="session", autouse=True)
async def start():
    register_tools()
    yield


@pytest_asyncio.fixture(scope="session")
def user():
    # Same as autobot-mini live test pattern
    return UserORM(
        id="56dd105c-606e-4f6d-8e88-8001d8f4f552",
        email="sourav@runkai.ai",
    )


@pytest_asyncio.fixture(scope="session")
def ctx(user):
    return Context(user_id=user.id)


def _extract_file_search_vs_ids_from_cfg_dict(cfg_dict: dict) -> list[str]:
    for t in cfg_dict.get("tools") or []:
        if isinstance(t, dict) and t.get("type") == "file_search":
            return list(t.get("vector_store_ids") or [])
    return []


def _extract_vs_ids_from_map(cfg_dict: dict) -> set[str]:
    m = cfg_dict.get("file_id_to_vector_store_map") or {}
    out: set[str] = set()
    for v in m.values():
        if isinstance(v, dict) and v.get("id"):
            out.add(v["id"])
    return out


def assert_single_store_invariant(cfg_dict: dict, *, expected_min_files: int) -> str:
    vs_ids = _extract_file_search_vs_ids_from_cfg_dict(cfg_dict)
    assert len(vs_ids) == 1, f"Expected exactly 1 vector_store_id in tools, got: {vs_ids}"
    vs_id = vs_ids[0]

    map_vs_ids = _extract_vs_ids_from_map(cfg_dict)
    assert map_vs_ids == {vs_id}, f"Expected map store ids == {{{vs_id}}}, got: {map_vs_ids}"

    file_map = cfg_dict.get("file_id_to_vector_store_map") or {}
    assert len(file_map) >= expected_min_files, (
        f"Expected at least {expected_min_files} file_ids mapped, got {len(file_map)}"
    )
    return vs_id


async def _wait_until_public(url: str, timeout_sec: int = 30) -> None:
    deadline = time.time() + timeout_sec
    async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
        while time.time() < deadline:
            try:
                r = await client.head(url)
                if r.status_code == 200:
                    return
            except Exception:
                pass
            await asyncio_sleep(1)
    raise RuntimeError(f"URL did not become public within {timeout_sec}s: {url}")


async def asyncio_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


async def _upload_text_to_public_s3(ctx: Context, user: UserORM, text: str, filename: str) -> str:
    settings = SettingsProvider.sget()
    s3 = get_public_s3(settings=settings)
    uf = UserFiles(user=user, s3=s3, settings=settings)

    metas = await uf.upload_binary(
        ctx=ctx,
        file_obj=io.BytesIO(text.encode("utf-8")),
        filename=filename,
    )
    url = str(metas[0].url)
    await _wait_until_public(url)
    return url


@pytest.mark.parametrize(
    "tools",
    [
        ["file_search"],  # file_search alone
        ["file_search", "code_interpreter"],  # file_search + other hosted tool
    ],
    ids=["file_search_only", "file_search_plus_code_interpreter"],
)
async def test_file_search_single_vector_store_multi_turn_e2e_sourav(
    ctx,
    db,
    user,
    set_test_settings,  # idempotent settings/tool registration
    tools,
):
    """
    E2E test for THIS PR, autobot-mini style:
    - live Mongo + real Sourav user
    - real S3 uploads
    - run ActionFactory 3 turns to reproduce the old failing scenario
    - assert single vector store reused (no "max 2 vector stores" 400)
    - isolate runs using UUID-based filenames; NO destructive cleanup of Sourav data
    """
    settings = SettingsProvider.sget()
    if not getattr(settings, "OPENAI_API_KEY", None):
        pytest.skip("Missing OPENAI_API_KEY (required for Responses API live test)")
    if not getattr(settings, "AWS_S3_PUBLIC_BUCKET_NAME", None):
        pytest.skip("Missing AWS_S3_PUBLIC_BUCKET_NAME (required for S3 live test)")

    run_id = uuid4().hex[:10]
    action_id = f"e2e_file_search_single_store_{run_id}"
    action_name = f"e2e-file-search-single-store-{run_id}"

    cfg = LlmResponsesOpenaiConfig(
        provider="openai",
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": "You are a test agent. Reply with OK."},
        ],
        tools=tools,
        store=True,
        truncation="auto",
        max_output_tokens=64,
        # Keep it simple; we only need request validation + tool shaping.
        tool_choice="auto",
        max_tool_calls=1,
    )

    action_doc = ActionDoc(
        id=action_id,
        user_id=user.id,
        name=action_name,
        type=ActionType.text2text_llm_responses_openai,
        config=cfg.model_dump(exclude_none=True),
    )

    vs_id_first: Optional[str] = None

    for turn in (1, 2, 3):
        # Upload a fresh file each turn (unique run_id prevents collisions between test runs)
        filename = f"tests_file_search_single_store/{run_id}/turn{turn}.txt"
        content = f"run_id={run_id}\nturn={turn}\n"
        file_url = await _upload_text_to_public_s3(ctx, user, content, filename)

        action_input = DataBlock(
            text="OK",
            urls=[file_url],
        ).model_dump()

        app_state = AppState()
        res = await ActionFactory.run_action(ctx, app_state, action_doc, action_input, user=user, db=db)
        if isinstance(res, Exception):
            # This will catch the prior regression: "maximum of 2 vector stores allowed"
            raise res

        cfg_out = res.config_dict
        assert isinstance(cfg_out, dict), "Expected config_dict to be a dict"
        vs_id = assert_single_store_invariant(cfg_out, expected_min_files=turn)

        if vs_id_first is None:
            vs_id_first = vs_id
        else:
            assert vs_id == vs_id_first, (
                f"Expected same vector store across turns. First={vs_id_first}, now={vs_id}"
            )

        logger.info(
            "Turn {} OK. tools_mode={} vs_id={}",
            turn,
            tools,
            vs_id,
        )

        # Persist for next turn (simulates multi-turn config persistence in Mongo)
        action_doc.config = cfg_out
        action_doc.input = res.input_dict
        action_doc.output = res.output_dict
        action_doc.results.append(ActionResult(input=action_doc.input, output=action_doc.output))

    logger.info("E2E multi-turn test succeeded. Single vector store id: {}", vs_id_first)
