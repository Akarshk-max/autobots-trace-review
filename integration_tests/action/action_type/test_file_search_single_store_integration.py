import io
import time
from typing import Any, Optional
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from loguru import logger
from openai import AsyncOpenAI

from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.conn.openai.openai_responses.openai_responses import (
    LlmResponsesOpenaiConfig,
    OpenaiResponses,
)
from src.autobots.conn.openai.openai_responses.response_model import OpenaiResponsesReq
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.user.user_orm_model import UserORM

pytestmark = [pytest.mark.asyncio]


# -----------------------------------------------------------------------------
# IMPORTANT: override the session autouse `start()` from tests/conftest.py
# We do NOT want setup_test_db() or sleep(15) for this integration test.
# -----------------------------------------------------------------------------
@pytest_asyncio.fixture(scope="session", autouse=True)
async def start():
    yield


def _skip_reason_if_missing_secrets(settings: Any) -> Optional[str]:
    missing = []
    if not getattr(settings, "OPENAI_API_KEY", None):
        missing.append("OPENAI_API_KEY")
    # For S3 upload via get_public_s3(), we usually need these to be set in settings.
    # If your get_public_s3() works without explicit AWS creds (e.g., IAM role), this check is conservative.
    if not getattr(settings, "AWS_S3_PUBLIC_BUCKET_NAME", None):
        missing.append("AWS_S3_PUBLIC_BUCKET_NAME")
    if missing:
        return "Missing required settings: " + ", ".join(missing)
    return None


async def _wait_until_public(url: str, timeout_sec: int = 30) -> None:
    """
    Poll a public URL using HEAD until it returns 200.
    Avoids eventual-consistency timing issues after upload.
    """
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
    # tiny helper to avoid importing asyncio in every file section
    import asyncio

    await asyncio.sleep(seconds)


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
    """
    Returns the single vector_store_id.
    """
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


@pytest.mark.asyncio
async def test_openai_responses_file_search_single_store_multi_turn_integration_s3():
    """
    Integration test for THIS PR:
    - Uses real S3 uploads (public bucket)
    - Uses real OpenAI files + vector stores
    - Calls OpenaiResponses.build_responses_file_search_tool_param directly across 3 turns
    - Asserts:
        * one vector store reused across turns
        * file_search tool contains exactly one vector_store_id
        * file_id_to_vector_store_map maps all files to that same store
    """
    settings = SettingsProvider.sget()
    reason = _skip_reason_if_missing_secrets(settings)
    if reason:
        pytest.skip(reason)

    run_id = uuid4().hex[:10]
    user_id = f"it-file-search-{run_id}"
    user = UserORM(id=user_id, email=f"{user_id}@example.com")
    ctx = Context(user_id=user.id)

    # 1) Upload 3 small files to public S3 (unique names)
    s3 = get_public_s3(settings=settings)
    user_files = UserFiles(user=user, s3=s3, settings=settings)

    file_urls: list[str] = []
    file_contents: list[bytes] = []
    for turn in (1, 2, 3):
        content = f"run_id={run_id}\nturn={turn}\n".encode("utf-8")
        filename = f"tests_file_search_single_store/{run_id}/turn{turn}.txt"
        metas = await user_files.upload_binary(ctx=ctx, file_obj=io.BytesIO(content), filename=filename)
        url = str(metas[0].url)
        file_urls.append(url)
        file_contents.append(content)

        # Ensure it's public before we use it elsewhere
        await _wait_until_public(url)

    # 2) Create OpenAI client + connector
    openai_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    connector = OpenaiResponses(provider="openai", openai_client=openai_client)

    # 3) Create config with file_search enabled
    cfg = LlmResponsesOpenaiConfig(
        provider="openai",
        model="gpt-4.1-mini",
        input=[{"role": "system", "content": "Test harness. Reply OK."}],
        tools=["file_search"],
        store=True,
        truncation="auto",
        max_output_tokens=64,
    )

    created_openai_file_ids: list[str] = []
    vs_id_first: Optional[str] = None

    try:
        # 4) Multi-turn: each turn adds one new OpenAI file, then we run the connector method
        # We keep the original S3 URL as the key in file_url_to_file_object_map.
        for turn in (1, 2, 3):
            # Upload file bytes to OpenAI Files API (source is the same bytes uploaded to S3)
            # (We already verified the S3 URL becomes public.)
            fo = await openai_client.files.create(
                file=(f"{run_id}-turn{turn}.txt", file_contents[turn - 1]),
                purpose="assistants",
            )
            created_openai_file_ids.append(fo.id)
            cfg.file_url_to_file_object_map[file_urls[turn - 1]] = fo

            # Call connector method with ALL known file_ids so far (like the real action does)
            file_ids_so_far = [cfg.file_url_to_file_object_map[u].id for u in file_urls[:turn]]
            await connector.build_responses_file_search_tool_param(
                ctx=ctx,
                responses_req=cfg,
                openai_client=openai_client,
                file_ids=file_ids_so_far,
            )

            # Assert shape via dict dump (mirrors persisted config serialization)
            cfg_dict = cfg.model_dump(exclude_none=True)
            vs_id = assert_single_store_invariant(cfg_dict, expected_min_files=turn)

            if vs_id_first is None:
                vs_id_first = vs_id
            else:
                assert vs_id == vs_id_first, (
                    f"Expected same vector store across turns. First={vs_id_first}, now={vs_id}"
                )

            # Optional local validation: ensure request model dumps cleanly
            _ = OpenaiResponsesReq(**cfg_dict)

        logger.info("Integration test succeeded. Single vector store id: {}", vs_id_first)

    finally:
        # Best-effort cleanup of OpenAI resources created by this test run.
        # This is safe and scoped (only deletes what we created), and failures should not fail the test.
        if vs_id_first:
            try:
                await openai_client.vector_stores.delete(vector_store_id=vs_id_first)
            except Exception as e:
                logger.warning("Best-effort cleanup: failed to delete vector store {}: {}", vs_id_first, e)

        for fid in created_openai_file_ids:
            try:
                await openai_client.files.delete(fid)
            except Exception as e:
                logger.warning("Best-effort cleanup: failed to delete OpenAI file {}: {}", fid, e)
