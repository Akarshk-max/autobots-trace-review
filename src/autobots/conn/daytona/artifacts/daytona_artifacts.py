import asyncio
from typing import Dict, Iterable, Optional, Tuple

from loguru import logger

from src.autobots.conn.object_storage.factory import get_public_storage
from src.autobots.conn.object_storage.protocol import ObjectStorage
from src.autobots.data_model.context import Context


def join_s3_key(prefix: str, rel_path: str) -> str:
    """
    Join an object-storage prefix and relative filename safely.

    Both inputs are expected to be sanitized relative paths without traversal.
    """
    prefix = (prefix or "").strip("/")
    rel_path = (rel_path or "").strip("/")

    if not prefix:
        key = rel_path
    elif not rel_path:
        key = prefix
    else:
        key = f"{prefix}/{rel_path}"

    key = key.strip()
    while "//" in key:
        key = key.replace("//", "/")
    return key.lstrip("/")


async def build_expected_artifacts(
    *,
    ctx: Optional[Context],
    s3_prefix: str,
    output_files: Iterable[str],
    storage: ObjectStorage | None = None,
    max_files: int = 50,
) -> Dict[str, str]:
    """
    Build deterministic expected public URLs for declared outputs.

    This does not confirm that the objects exist.
    """
    output_files = list(output_files or [])[:max_files]
    if not s3_prefix or not output_files:
        return {}

    storage = storage or get_public_storage(object_prefix="")
    expected: Dict[str, str] = {}

    for filename in output_files:
        key = join_s3_key(s3_prefix, filename)
        try:
            expected[filename] = str(await storage.get_url(key))
        except Exception as exc:
            logger.bind(
                ctx=ctx,
                key=key,
                filename=filename,
            ).debug(f"Failed to build expected artifact URL: {exc}")

    return expected


async def reconcile_artifacts_from_s3(
    *,
    ctx: Optional[Context],
    s3_prefix: str,
    output_files: Iterable[str],
    expected_artifacts: Dict[str, str],
    artifacts: Dict[str, str],
    storage: ObjectStorage | None = None,
    max_files: int = 50,
    concurrency: int = 10,
) -> Dict[str, str]:
    """
    Confirm expected artifacts in object storage and return a merged mapping.

    The compatibility function name is retained for existing callers.
    Existing artifacts are never removed.
    """
    output_files = list(output_files or [])[:max_files]
    artifacts = dict(artifacts or {})
    expected_artifacts = dict(expected_artifacts or {})

    if not s3_prefix or not output_files:
        return artifacts

    storage = storage or get_public_storage(object_prefix="")
    semaphore = asyncio.Semaphore(max(1, int(concurrency)))

    async def _check_one(filename: str) -> Tuple[str, bool, str]:
        if filename in artifacts:
            return filename, False, ""

        key = join_s3_key(s3_prefix, filename)

        async with semaphore:
            try:
                exists = await storage.exists(key)
            except Exception as exc:
                logger.bind(
                    ctx=ctx,
                    key=key,
                    filename=filename,
                ).debug(f"Object-storage existence check failed: {exc}")
                return filename, False, ""

        if not exists:
            return filename, False, ""

        url = expected_artifacts.get(filename)
        if not url:
            try:
                url = str(await storage.get_url(key))
            except Exception:
                url = ""

        return filename, True, url

    missing = [filename for filename in output_files if filename and filename not in artifacts]
    if not missing:
        return artifacts

    results = await asyncio.gather(*(_check_one(filename) for filename in missing))
    for filename, exists, url in results:
        if exists and url:
            artifacts[filename] = url

    return artifacts


async def materialize_artifact_urls(
    *,
    ctx: Optional[Context],
    storage: ObjectStorage,
    storage_scope: str,
    s3_prefix: str,
    artifacts: Dict[str, str],
    expiration: int = 7 * 24 * 3600,
) -> Dict[str, str]:
    artifacts = dict(artifacts or {})
    if storage_scope == "meetkiwi_public" or not artifacts:
        return artifacts
    generate_presigned_get_url = getattr(storage, "generate_presigned_get_url", None)
    if not callable(generate_presigned_get_url):
        raise ValueError("Private Daytona artifact downloads currently support S3 only")

    materialized: Dict[str, str] = {}
    for filename in artifacts:
        key = join_s3_key(s3_prefix, filename)
        try:
            materialized[filename] = await generate_presigned_get_url(
                key=key,
                expiration=expiration,
            )
        except Exception as exc:
            logger.bind(ctx=ctx, key=key, filename=filename).warning(
                f"Failed to generate private Daytona artifact URL: {exc}"
            )
    return materialized
