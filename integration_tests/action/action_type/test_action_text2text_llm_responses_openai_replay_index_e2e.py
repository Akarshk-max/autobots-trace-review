import io
import time
from copy import deepcopy
from typing import Any, Dict, Optional, Tuple
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
# We do NOT want setup_test_db() / seeded test users.
# -----------------------------------------------------------------------------
@pytest_asyncio.fixture(scope="session", autouse=True)
async def start():
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


def _blocks_text(run_obj) -> str:
    blocks = (run_obj.output_dict or {}).get("blocks") or []
    if blocks and isinstance(blocks, list) and isinstance(blocks[0], dict):
        return blocks[0].get("text") or ""
    return ""


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


def _find_container_citation(cfg_input: list[Any]) -> Optional[Tuple[int, int, int]]:
    for i in range(len(cfg_input) - 1, -1, -1):
        item = cfg_input[i]
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message" or item.get("role") != "assistant":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for j, part in enumerate(content):
            if not isinstance(part, dict) or part.get("type") != "output_text":
                continue
            anns = part.get("annotations")
            if not isinstance(anns, list):
                continue
            for k, ann in enumerate(anns):
                if isinstance(ann, dict) and ann.get("type") == "container_file_citation":
                    return i, j, k
    return None


def _poison_remove_index(cfg: Dict[str, Any]) -> bool:
    inp = cfg.get("input")
    if not isinstance(inp, list):
        return False
    loc = _find_container_citation(inp)
    if not loc:
        return False
    i, j, k = loc
    ann = inp[i]["content"][j]["annotations"][k]
    if not isinstance(ann, dict):
        return False
    before = deepcopy(ann)
    ann.pop("index", None)
    logger.bind(path=f"input[{i}].content[{j}].annotations[{k}]", before=before, after=ann).warning(
        "Poisoned replay history (removed container_file_citation.index)"
    )
    return True


@pytest.mark.timeout(240)
async def test_replay_missing_index_e2e(ctx, db, user):
    """
    End-to-end repro test that matches frontend behavior:
    - Uses Sourav user_id/email so AppConnOpenAI chooses Ownership.user and reads key from Mongo (AppAuth)
    - Runs the real text2text_llm_responses_openai action with tools=file_search+code_interpreter
    - Does two turns and asserts no replay 400 missing annotations[*].index
    """
    settings = SettingsProvider.sget()
    if not getattr(settings, "AWS_S3_PUBLIC_BUCKET_NAME", None):
        pytest.skip("Missing AWS_S3_PUBLIC_BUCKET_NAME (required for S3 upload in this E2E test)")

    # Confirm user OpenAI auth exists in Mongo and log suffix for debugging
    # integration_doc = await AppConnHelper.get_app_auth(ctx=ctx, db=db, user=user, app_type=AppTypes.openai)
    # auth = OpenaiAuthModel.model_validate(integration_doc.auth)
    # logger.info("Mongo OpenAI key suffix for Sourav user: {}", (auth.api_key or "")[-4:])

    run_id = uuid4().hex[:10]
    model = "gpt-5.2"

    # Upload deterministic text so file_search can cite it (no dependency on hardcoded URLs)
    file_text = (
        "Jan 2021:\nPerformance was steady. Revenue was 100.\n\n"
        "Feb 2021:\nWe saw a doubling of performance. Revenue was 200.\n\n"
        "Mar 2021:\nThe quarter ended on a high note. Revenue was 300.\n"
    )
    file_url = await _upload_text_to_public_s3(
        ctx, user, file_text, f"tests/replay_index/{run_id}/bmg_mentions.txt"
    )
    logger.info("Uploaded test file_url={}", file_url)

    cfg = LlmResponsesOpenaiConfig(
        provider="openai",
        model=model,
        store=True,
        stream=False,
        truncation="auto",
        reasoning={"effort": "medium"},
        input=[{"role": "system", "content": "You are a helpful assistant"}],
        tools=["file_search", "code_interpreter"],
        tool_choice="auto",
        max_output_tokens=900,
        tool_output_text_to_file_char_limit=75000,
    )

    action_doc = ActionDoc(
        id=f"e2e_replay_index_{run_id}",
        user_id=user.id,
        name=f"e2e-replay-index-{run_id}",
        type=ActionType.text2text_llm_responses_openai,
        config=cfg.model_dump(exclude_none=True),
        results=[],
    )

    prompt1 = (
        "Use file_search on the attached file to find exact lines that mention Jan 2021, Feb 2021, Mar 2021.\n"
        "Requirements:\n\n"
        "Return exactly 3 bullet points (one per month).\n"
        "Each bullet must include a direct quote from the file and include a file_search citation.\n"
        "After that, use code interpreter to create a CSV named stunnin_file_breakdown.csv with columns month,revenue "
        "and rows for Jan/Feb/Mar 2021 using the values you quoted. Save to /mnt/data/stunnin_file_breakdown.csv.\n"
        "In the final answer, include a markdown link in this exact format:\n"
        "[stunnin_file_breakdown.csv](sandbox:/mnt/data/stunnin_file_breakdown.csv)\n"
        "Do not paste the CSV contents."
    )

    app_state = AppState()
    run1 = await ActionFactory.run_action(
        ctx=ctx,
        app_state=app_state,
        action_doc=action_doc,
        action_input_dict=DataBlock(text=prompt1, urls=[file_url]).model_dump(exclude_none=True),
        user=user,
        db=db,
    )
    assert not isinstance(run1, Exception)
    assert run1.status.value == "success", _blocks_text(run1)
    logger.info("===== TURN 1 MODEL OUTPUT (user-visible) =====\n{}", _blocks_text(run1))
    logger.info("===== TURN 1 RAW output_dict keys ===== {}", list((run1.output_dict or {}).keys()))

    # Persist config for replay (matches production multi-turn behavior)
    action_doc.config = run1.config_dict
    action_doc.input = run1.input_dict
    action_doc.output = run1.output_dict
    action_doc.results.append(ActionResult(input=run1.input_dict, output=run1.output_dict))

    # Deterministic repro: remove index if present
    poisoned = _poison_remove_index(action_doc.config)
    if not poisoned:
        logger.warning("No container_file_citation found to poison; test may be non-deterministic.")

    prompt2 = (
        "Great—now re-check Jan–Mar 2021 again from the same attached file and append a new column "
        "rechecked=true to the CSV (keep the same filename). Then provide the link again."
    )

    run2 = await ActionFactory.run_action(
        ctx=ctx,
        app_state=app_state,
        action_doc=action_doc,
        action_input_dict=DataBlock(text=prompt2).model_dump(exclude_none=True),
        user=user,
        db=db,
    )
    assert not isinstance(run2, Exception)
    logger.info("===== TURN 2 MODEL OUTPUT (user-visible) =====\n{}", _blocks_text(run2))
    logger.info("===== TURN 2 RAW output_dict keys ===== {}", list((run2.output_dict or {}).keys()))
    logger.info("===== TURN 2 blocks full ===== {}", (run2.output_dict or {}).get("blocks"))

    text2 = _blocks_text(run2)
    assert "Missing required parameter" not in text2, text2
    assert ".annotations" not in text2, text2
    assert ".index" not in text2, text2
