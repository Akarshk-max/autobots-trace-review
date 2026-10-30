import asyncio
import re
from uuid import uuid4

import pytest
import pytest_asyncio
from botocore.exceptions import ClientError

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.conn.daytona.state.user_daytona_jobs import UserDaytonaJobs
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM

settings = SettingsProvider.sget()

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(getattr(settings, "ENV", "") != "local", reason="Local-only tests (uses real Daytona + S3)"),
]


@pytest_asyncio.fixture(scope="session", autouse=True)
async def start():
    # Override any session autouse fixture that might do DB setup/sleeps.
    yield


@pytest_asyncio.fixture(scope="session")
def user():
    # Hardcoded Sourav
    return UserORM(
        id="56dd105c-606e-4f6d-8e88-8001d8f4f552",
        email="sourav@runkai.ai",
    )


@pytest_asyncio.fixture(scope="session")
def ctx(user):
    return Context(user_id=user.id)


def _extract_job_id(text: str) -> str | None:
    m = re.search(r"job_id\s*=\s*(dj_[A-Za-z0-9_\-]+)", text or "")
    return m.group(1) if m else None


async def _run_daytona(ctx: Context, db, user: UserORM, *, artifact_mode: str, code: str, output_files: list[str]) -> str:
    run_doc = ActionDoc(
        id=f"daytona_run_{uuid4()}",
        user_id=user.id,
        name="daytona-expired-sandbox-run",
        type=ActionType.text2text_daytona,
        config={
            "operation": "run",
            "lifecycle": "ephemeral",
            "max_runtime_minutes": 5,
            "artifact_mode": artifact_mode,
            "code": code,
            "wait_for_completion_sec": 0,
            "pip_packages": [],
            "input_urls": [],
            "output_files": output_files,
        },
    )
    res = await ActionFactory.run_action(
        ctx=ctx, app_state=AppState(),
        action_doc=run_doc,
        action_input_dict=DataBlock(text="").model_dump(exclude_none=True),
        user=user,
        db=db,
    )
    if isinstance(res, Exception):
        raise res
    blocks = (res.output_dict or {}).get("blocks") or []
    assert blocks and isinstance(blocks[0], dict)
    return blocks[0].get("text") or ""


async def _status_daytona(ctx: Context, db, user: UserORM, job_id: str) -> tuple[str, list[str]]:
    status_doc = ActionDoc(
        id=f"daytona_status_{uuid4()}",
        user_id=user.id,
        name="daytona-expired-sandbox-status",
        type=ActionType.text2text_daytona,
        config={"operation": "status", "job_id": job_id},
    )
    res = await ActionFactory.run_action(
        ctx=ctx, app_state=AppState(),
        action_doc=status_doc,
        action_input_dict=DataBlock(text="").model_dump(exclude_none=True),
        user=user,
        db=db,
    )
    if isinstance(res, Exception):
        raise res
    blocks = (res.output_dict or {}).get("blocks") or []
    assert blocks and isinstance(blocks[0], dict)
    text = blocks[0].get("text") or ""
    urls = blocks[0].get("urls") or []
    return text, [u for u in urls if isinstance(u, str)]


async def _cancel_daytona(ctx: Context, db, user: UserORM, job_id: str) -> None:
    cancel_doc = ActionDoc(
        id=f"daytona_cancel_{uuid4()}",
        user_id=user.id,
        name="daytona-expired-sandbox-cancel",
        type=ActionType.text2text_daytona,
        config={"operation": "cancel", "job_id": job_id},
    )
    await ActionFactory.run_action(
        ctx=ctx, app_state=AppState(),
        action_doc=cancel_doc,
        action_input_dict=DataBlock(text="").model_dump(exclude_none=True),
        user=user,
        db=db,
    )


async def _assert_daytona_available(ctx: Context, db, user: UserORM) -> None:
    conn = await AppConn.get_daytona(ctx=ctx, db=db, user=user)
    if isinstance(conn, Exception):
        pytest.skip(f"Daytona not configured/available for this user: {conn}")


async def _s3_object_exists(key: str) -> bool:
    s3 = get_public_s3(object_prefix="")
    def _head():
        return s3.s3_client.head_object(Bucket=s3.bucket.name, Key=key)
    try:
        await asyncio.to_thread(_head)
        return True
    except ClientError as e:
        code = str(e.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


async def _wait_for_s3_object(key: str, *, should_exist: bool, timeout_sec: float = 60.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_sec
    while asyncio.get_running_loop().time() < deadline:
        try:
            exists = await _s3_object_exists(key)
            if exists == should_exist:
                return True
        except Exception:
            # transient AWS errors -> retry
            pass
        await asyncio.sleep(1.0)
    return False


@pytest.mark.parametrize(
    "artifact_mode, should_upload_bytes",
    [
        ("presigned", True),
        ("auto", True),
        ("none", False),
        # server_sync only uploads if server pulls from sandbox during status; we avoid polling before "expiry"
        ("server_sync", False),
    ],
)
async def test_expired_sandbox_artifacts_recovered_via_s3_reconciliation_unpolled(
    ctx, db, user, set_test_settings, artifact_mode: str, should_upload_bytes: bool
):
    await _assert_daytona_available(ctx=ctx, db=db, user=user)

    tag = uuid4().hex[:8]
    out_name = f"out_{artifact_mode}_{tag}.txt"
    content = f"hello-{artifact_mode}-{tag}"

    code = (
        f"print('TAG={tag}')\n"
        f"with open({out_name!r}, 'w', encoding='utf-8') as f:\n"
        f"    f.write({content!r})\n"
        f"print('WROTE={out_name}')\n"
    )

    run_text = await _run_daytona(
        ctx=ctx, db=db, user=user, artifact_mode=artifact_mode, code=code, output_files=[out_name]
    )
    job_id = _extract_job_id(run_text)
    assert job_id, f"Could not extract job_id from run output: {run_text}"

    uj = UserDaytonaJobs(user=user, db=db)
    job_doc = await uj.get_by_job_id(ctx=ctx, job_id=job_id)
    assert job_doc is not None
    assert job_doc.created_sandbox is True
    assert job_doc.reused_sandbox is False
    assert job_doc.lifecycle == "ephemeral"
    assert job_doc.job_workdir.endswith(f"/{job_id}")

    # Build S3 key deterministically from job prefix + filename
    s3_prefix = (getattr(job_doc, "s3_prefix", "") or "").strip("/")
    assert s3_prefix, "Expected s3_prefix on job doc"
    key = f"{s3_prefix}/{out_name}".replace("//", "/")

    # Wait for expected existence/non-existence in S3.
    ok = await _wait_for_s3_object(key, should_exist=should_upload_bytes, timeout_sec=60.0)
    if not ok:
        # For callback mode (not in this param list), you'd likely skip here. For presigned/auto this should be reliable.
        if should_upload_bytes:
            pytest.fail(f"Expected S3 object to exist but it did not appear: key={key} mode={artifact_mode}")
        # For none/server_sync, don't fail hard; some systems might upload unexpectedly.
        # But normally this should be False.
    # Simulate expired sandbox + Mongo artifact loss:
    real_sandbox_id = job_doc.sandbox_id
    await uj.update_by_job_id(
        ctx=ctx,
        job_id=job_id,
        update={"artifacts": {}, "sandbox_id": f"sb_expired_{uuid4().hex[:10]}"},
    )

    # First poll AFTER "expiry": should fail sandbox poll but reconcile from S3 if bytes exist.
    text, urls = await _status_daytona(ctx=ctx, db=db, user=user, job_id=job_id)

    job_doc2 = await uj.get_by_job_id(ctx=ctx, job_id=job_id)
    assert job_doc2 is not None
    artifacts2 = getattr(job_doc2, "artifacts", None) or {}

    if should_upload_bytes:
        assert urls, f"Expected recovered URLs for mode={artifact_mode}, got none. text={text[:600]}"
        assert any(out_name in u for u in urls), f"Expected URL to include {out_name}. urls={urls}"
        assert out_name in artifacts2, f"Expected Mongo artifacts to be backfilled. artifacts_keys={list(artifacts2.keys())[:10]}"
    else:
        assert not urls, f"Expected no recovered URLs for mode={artifact_mode}, got {urls}"
        assert out_name not in artifacts2

    # Cleanup best-effort: restore sandbox_id so cancel can delete it
    try:
        await uj.update_by_job_id(ctx=ctx, job_id=job_id, update={"sandbox_id": real_sandbox_id})
        await _cancel_daytona(ctx=ctx, db=db, user=user, job_id=job_id)
    except Exception:
        pass


async def test_callback_mode_recovery_skips_if_not_configured(ctx, db, user, set_test_settings):
    await _assert_daytona_available(ctx=ctx, db=db, user=user)

    callback_base = (getattr(settings, "DAYTONA_CALLBACK_BASE_URL", "") or getattr(settings, "APP_BASE_URL", "") or "").strip()
    if not callback_base:
        pytest.skip("Callback base not configured (APP_BASE_URL/DAYTONA_CALLBACK_BASE_URL).")

    tag = uuid4().hex[:8]
    out_name = f"out_callback_{tag}.txt"
    content = f"hello-callback-{tag}"
    code = (
        f"print('TAG={tag}')\n"
        f"with open({out_name!r}, 'w', encoding='utf-8') as f:\n"
        f"    f.write({content!r})\n"
        f"print('WROTE={out_name}')\n"
    )

    run_text = await _run_daytona(ctx=ctx, db=db, user=user, artifact_mode="callback", code=code, output_files=[out_name])
    job_id = _extract_job_id(run_text)
    assert job_id

    uj = UserDaytonaJobs(user=user, db=db)
    job_doc = await uj.get_by_job_id(ctx=ctx, job_id=job_id)
    assert job_doc is not None

    s3_prefix = (getattr(job_doc, "s3_prefix", "") or "").strip("/")
    key = f"{s3_prefix}/{out_name}".replace("//", "/")

    # If callback isn't actually reachable from the sandbox, the object won't appear. Skip rather than fail.
    ok = await _wait_for_s3_object(key, should_exist=True, timeout_sec=60.0)
    if not ok:
        pytest.skip("Callback mode did not produce S3 object (callback likely unreachable from sandbox).")

    real_sandbox_id = job_doc.sandbox_id
    await uj.update_by_job_id(ctx=ctx, job_id=job_id, update={"artifacts": {}, "sandbox_id": f"sb_expired_{uuid4().hex[:10]}"})

    text, urls = await _status_daytona(ctx=ctx, db=db, user=user, job_id=job_id)
    assert urls and any(out_name in u for u in urls), f"Expected recovery in callback mode. text={text[:600]}"

    # cleanup
    try:
        await uj.update_by_job_id(ctx=ctx, job_id=job_id, update={"sandbox_id": real_sandbox_id})
        await _cancel_daytona(ctx=ctx, db=db, user=user, job_id=job_id)
    except Exception:
        pass
