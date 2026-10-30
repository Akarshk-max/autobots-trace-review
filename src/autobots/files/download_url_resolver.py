"""
Resolve file URLs into time-limited download URLs for API responses.

Phase 1 scope (Autobots-owned storage only):

  - MeetKiwi public S3 bucket   -> returned unchanged (publicly readable)
  - MeetKiwi private S3 bucket  -> presigned GET URL, but only for objects
                                   under {AWS_S3_FILES_FOLDER}/{user_id}/
                                   owned by the authenticated user
  - Customer S3 buckets         -> returned unchanged (phase 2)
  - External URLs               -> returned unchanged

Canonical URLs must remain stable: presigned URLs expire and must not be
persisted in actions, databases, or logs. Signing happens only at this
response boundary, and the generated URLs are never logged.
"""

from fastapi import HTTPException
from loguru import logger

from src.autobots.conn.aws.aws_s3 import get_public_s3, get_s3
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.files.user_files_model import ResolvedDownloadUrl
from src.autobots.user.user_orm_model import UserORM

# Presigned GET URL lifetime bounds (seconds).
MIN_EXPIRATION_SEC = 60
MAX_EXPIRATION_SEC = 7 * 24 * 60 * 60
DEFAULT_EXPIRATION_SEC = 24 * 60 * 60


def _user_files_prefix(user_id: str) -> str:
    files_folder = SettingsProvider.sget().AWS_S3_FILES_FOLDER.strip("/")
    return f"{files_folder}/{user_id}/"


async def resolve_system_download_url(
    ctx: Context, user: UserORM, url: str, expiration_sec: int
) -> ResolvedDownloadUrl:
    expiration_sec = max(MIN_EXPIRATION_SEC, min(expiration_sec, MAX_EXPIRATION_SEC))

    # Public bucket: publicly readable, return as-is.
    try:
        public_storage = get_public_s3(object_prefix="")
    except ValueError:
        public_storage = None
    if public_storage is not None and await public_storage.get_key_if_owned_url(url):
        return ResolvedDownloadUrl(original_url=url, url=url, signed=False)

    private_storage = get_s3(object_prefix="")
    key = await private_storage.get_key_if_owned_url(url)
    if key is None:
        # External URL or customer-owned bucket: pass through unchanged.
        return ResolvedDownloadUrl(original_url=url, url=url, signed=False)

    # Phase 1 only presigns S3; other deployment providers (e.g. Azure) pass through.
    # To support Azure signing later, add generate_signed_download() to AzureBlobStorage
    # using generate_blob_sas(read=True) — same pattern as its existing copy()/upload SAS —
    # then remove this guard and call generate_signed_download() on both providers.
    if getattr(private_storage, "provider", None) != "s3":
        return ResolvedDownloadUrl(original_url=url, url=url, signed=False)

    if not key.lstrip("/").startswith(_user_files_prefix(str(user.id))):
        raise HTTPException(status_code=403, detail="File does not belong to the current user")

    signed_url = await private_storage.generate_presigned_get_url(
        key=key,
        expiration=expiration_sec,
        url_style="default",
    )
    logger.bind(
        ctx=ctx,
        user_id=str(user.id),
        bucket=private_storage.container_name,
        key=key,
        expiration_sec=expiration_sec,
    ).info("Signed private S3 download URL")
    return ResolvedDownloadUrl(original_url=url, url=signed_url, signed=True)


async def resolve_system_download_urls(
    ctx: Context,
    user: UserORM,
    urls: list[str],
    expiration_sec: int = DEFAULT_EXPIRATION_SEC,
) -> list[ResolvedDownloadUrl]:
    resolved_list = []
    for url in urls:
        resolved = await resolve_system_download_url(
            ctx=ctx, user=user, url=url, expiration_sec=expiration_sec
        )
        resolved_list.append(resolved)
    return resolved_list
