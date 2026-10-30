"""
Resolve a file URL to the object-storage instance that owns it.

The resolver is used by the Daytona run path to decide, for each input URL,
whether it points at:

  - MeetKiwi public storage  -> no presigning needed (public object)
  - MeetKiwi private storage -> presign a short-lived GET URL with deployment creds
  - Customer S3 storage      -> presign a short-lived GET URL with the matching
                                 customer's saved AWS connection
  - An external URL          -> pass through unchanged

For customer S3, a user may hold multiple named AWS connections (each with its
own bucket). The bucket name lives inside the encrypted ``creds`` dict, so we
cannot filter by bucket in MongoDB directly. Instead we list all of the user's
AWS connections, decrypt each lazily, and stop on the first whose bucket
matches the URL's host. In practice a user has 1-3 AWS connections, so this is
at most 1-3 decryptions per resolution.
"""

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional
from urllib.parse import urlparse

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.conn.aws.aws_s3 import AwsS3, get_aws_s3
from src.autobots.conn.object_storage.factory import get_public_storage, get_storage
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_creds.app_creds_crud import AppCredsCRUD
from src.autobots.integrations.app_creds.app_creds_model import AppCredsDocRead
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.crypto.helpers import decrypt_if_needed
from src.autobots.integrations.integration_crud import IntegrationCRUD
from src.autobots.integrations.integration_model import IntegrationDocRead
from src.autobots.user.user_orm_model import UserORM

StorageScope = Literal["meetkiwi_public", "meetkiwi_private", "customer_s3"]


@dataclass(frozen=True)
class ResolvedStorage:
    """The storage instance, object key, and scope label for a matched URL."""

    storage: AwsS3
    key: str
    scope: StorageScope
    # Only set for customer_s3; identifies which named AWS connection was used.
    connection_name: Optional[str] = None


@dataclass(frozen=True)
class ResolvedOutputStorage:
    storage: AwsS3
    scope: StorageScope
    s3_prefix: str
    connection_name: Optional[str] = None


def _extract_bucket_from_url(url: str) -> Optional[str]:
    """
    Best-effort extraction of the S3 bucket name from an HTTPS URL.

    Handles:
      - Virtual-hosted: <bucket>.s3.amazonaws.com/<key>
                       <bucket>.s3.<region>.amazonaws.com/<key>
      - Path-style:     s3.amazonaws.com/<bucket>/<key>
                       s3.<region>.amazonaws.com/<bucket>/<key>
    """
    parsed = urlparse(str(url))
    if parsed.scheme.lower() != "https":
        return None
    host = (parsed.netloc or "").lower()
    if not host.endswith(".amazonaws.com"):
        return None

    # <bucket>.s3.amazonaws.com  or  <bucket>.s3.<region>.amazonaws.com
    if ".s3." in host:
        return host.split(".s3.")[0] or None

    # s3.amazonaws.com/<bucket>/...  or  s3.<region>.amazonaws.com/<bucket>/...
    if host.startswith("s3."):
        path = (parsed.path or "").lstrip("/")
        if "/" in path:
            return path.split("/", 1)[0] or None

    return None


async def _resolve_deployment_storage(
    url: str, user: UserORM
) -> Optional[tuple[AwsS3, str, StorageScope]]:
    """
    Check if the URL belongs to MeetKiwi public or private deployment storage.
    Returns (storage, key, scope) on match, None otherwise.
    """
    try:
        public_storage = get_public_storage(object_prefix="")
    except ValueError:
        public_storage = None
    if public_storage is not None:
        key = await public_storage.get_key_if_owned_url(url)
        if key is not None:
            return public_storage, key, "meetkiwi_public"

    private_storage = get_storage(object_prefix="")
    key = await private_storage.get_key_if_owned_url(url)
    if key is None:
        return None

    files_folder = SettingsProvider.sget().AWS_S3_FILES_FOLDER.strip("/")
    user_prefix = f"{files_folder}/{user.id}/"
    if key.lstrip("/").startswith(user_prefix):
        return private_storage, key, "meetkiwi_private"
    return None


async def _list_user_aws_connections(
    ctx: Context, db: AsyncIOMotorDatabase, user: UserORM
) -> list[tuple[str, dict]]:
    """Fetch folded and legacy AWS connections, preferring folded credentials by name."""
    space_member = getattr(user, "space_member", None)
    space_id = space_member.space_id if space_member else None
    integration_page = await IntegrationCRUD(db=db).get_by_name(
        doc_find=IntegrationDocRead(
            user_id=str(user.id),
            space_id=space_id,
            app_name=AppTypes.aws,
        ),
        limit=100,
        offset=0,
    )
    legacy_page = await AppCredsCRUD(db=db).get_by_name(
        doc_find=AppCredsDocRead(
            user_id=str(user.id),
            space_id=space_id,
            app_name=AppTypes.aws,
        ),
        limit=100,
        offset=0,
    )

    connections: list[tuple[str, dict]] = []
    names: set[str] = set()
    for docs in (integration_page.docs, legacy_page.docs):
        for doc in docs:
            if not doc.name or doc.name in names or not doc.creds:
                continue
            try:
                creds = decrypt_if_needed(doc.creds)
                if not creds:
                    continue
                connections.append((doc.name, creds))
                names.add(doc.name)
            except Exception as exc:
                logger.bind(
                    ctx=ctx, app_name=AppTypes.aws.value, doc_id=str(getattr(doc, "id", ""))
                ).warning(f"Failed to decrypt AWS app creds while resolving storage: {exc}")
    return connections


async def _resolve_customer_storage(
    ctx: Context, db: AsyncIOMotorDatabase, user: UserORM, url: str
) -> Optional[ResolvedStorage]:
    """
    Check if the URL belongs to one of the user's customer AWS S3 connections.

    Iterates all of the user's AWS connections (bucket names are encrypted at
    rest, so we cannot filter by bucket in MongoDB), decrypts each, and returns
    the connection whose bucket and user-scoped prefix match the URL.
    """
    url_bucket = _extract_bucket_from_url(url)
    if not url_bucket:
        # Not an S3 HTTPS URL we can bucket-match; let caller treat as external.
        return None

    connections = await _list_user_aws_connections(ctx=ctx, db=db, user=user)
    for connection_name, creds in connections:
        bucket_name = creds.get("bucket_name") or ""
        if not bucket_name or bucket_name.lower() != url_bucket.lower():
            continue
        configured_prefix = (creds.get("object_prefix") or "default").strip("/")
        user_prefix = f"{configured_prefix}/{user.id}/"
        try:
            aws_s3 = get_aws_s3(
                region_name=creds.get("region_name") or "",
                aws_access_key_id=creds["aws_access_key_id"],
                aws_secret_access_key=creds["aws_secret_access_key"],
                bucket_name=bucket_name,
                object_prefix=user_prefix,
            )
        except Exception as exc:
            logger.bind(ctx=ctx, connection_name=connection_name).warning(
                f"Failed to build customer AwsS3 for connection '{connection_name}': {exc}"
            )
            continue

        key = await aws_s3.get_key_if_owned_url(url)
        if key is not None and key.lstrip("/").startswith(user_prefix):
            return ResolvedStorage(
                storage=aws_s3,
                key=key,
                scope="customer_s3",
                connection_name=connection_name,
            )
    return None


async def _get_aws_connection(
    *,
    ctx: Context,
    db: AsyncIOMotorDatabase,
    user_id: str,
    space_id: str | None,
    connection_name: str | None,
) -> tuple[str, dict] | None:
    query = {
        "user_id": user_id,
        "space_id": space_id,
        "app_name": AppTypes.aws,
    }
    if connection_name:
        query["name"] = connection_name
    else:
        query["default"] = True

    integration = await IntegrationCRUD(db=db).find_one(doc_find=IntegrationDocRead(**query))
    if integration and integration.name and integration.creds:
        try:
            creds = decrypt_if_needed(integration.creds)
            if creds:
                return integration.name, creds
        except Exception as exc:
            logger.bind(ctx=ctx, connection_name=integration.name).warning(
                f"Failed to decrypt customer AWS connection: {exc}"
            )

    legacy = await AppCredsCRUD(db=db).find_one(doc_find=AppCredsDocRead(**query))
    if legacy and legacy.name and legacy.creds:
        try:
            creds = decrypt_if_needed(legacy.creds)
            if creds:
                return legacy.name, creds
        except Exception as exc:
            logger.bind(ctx=ctx, connection_name=legacy.name).warning(
                f"Failed to decrypt legacy customer AWS connection: {exc}"
            )
    return None


def _build_customer_s3(*, creds: dict) -> AwsS3:
    required = ("region_name", "aws_access_key_id", "aws_secret_access_key", "bucket_name")
    missing = [name for name in required if not creds.get(name)]
    if missing:
        raise ValueError("Missing customer S3 configuration: " + ", ".join(missing))
    return get_aws_s3(
        region_name=creds["region_name"],
        aws_access_key_id=creds["aws_access_key_id"],
        aws_secret_access_key=creds["aws_secret_access_key"],
        bucket_name=creds["bucket_name"],
        object_prefix="",
    )


async def resolve_output_storage(
    *,
    ctx: Context,
    db: AsyncIOMotorDatabase,
    user: UserORM,
    requested_scope: StorageScope,
    connection_name: str | None,
    job_id: str,
) -> ResolvedOutputStorage:
    settings = SettingsProvider.sget()
    system_prefix = f"{settings.AWS_S3_FILES_FOLDER.strip('/')}/{user.id}/daytona/{job_id}/"

    if requested_scope != "customer_s3":
        factory = get_public_storage if requested_scope == "meetkiwi_public" else get_storage
        storage = factory(object_prefix="")
        if not isinstance(storage, AwsS3):
            raise ValueError("Daytona output storage currently supports S3 only")
        return ResolvedOutputStorage(
            storage=storage,
            scope=requested_scope,
            s3_prefix=system_prefix,
        )

    space_member = getattr(user, "space_member", None)
    space_id = space_member.space_id if space_member else None
    connection = await _get_aws_connection(
        ctx=ctx,
        db=db,
        user_id=str(user.id),
        space_id=space_id,
        connection_name=connection_name,
    )
    if connection is None:
        if connection_name:
            raise ValueError(f"AWS connection '{connection_name}' not found")
        storage = get_storage(object_prefix="")
        if not isinstance(storage, AwsS3):
            raise ValueError("Daytona output storage currently supports S3 only")
        return ResolvedOutputStorage(
            storage=storage,
            scope="meetkiwi_private",
            s3_prefix=system_prefix,
        )

    resolved_name, creds = connection
    try:
        storage = _build_customer_s3(creds=creds)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"AWS connection '{resolved_name}' has invalid S3 credentials") from exc
    configured_prefix = (creds.get("object_prefix") or "default").strip("/")
    return ResolvedOutputStorage(
        storage=storage,
        scope="customer_s3",
        s3_prefix=f"{configured_prefix}/{user.id}/daytona/{job_id}/",
        connection_name=resolved_name,
    )


async def resolve_job_output_storage(
    *,
    ctx: Context,
    db: AsyncIOMotorDatabase,
    job: Mapping[str, Any] | Any,
) -> ResolvedOutputStorage:
    def value(name: str, default: Any = None) -> Any:
        if isinstance(job, Mapping):
            return job.get(name, default)
        return getattr(job, name, default)

    scope = value("output_storage_scope", "meetkiwi_public") or "meetkiwi_public"
    if scope not in {"meetkiwi_public", "meetkiwi_private", "customer_s3"}:
        raise ValueError(f"Unsupported Daytona output storage scope: {scope}")
    s3_prefix = value("s3_prefix", "") or ""
    connection_name = value("output_storage_connection_name")

    if scope != "customer_s3":
        factory = get_public_storage if scope == "meetkiwi_public" else get_storage
        storage = factory(object_prefix="")
        if not isinstance(storage, AwsS3):
            raise ValueError("Daytona output storage currently supports S3 only")
        return ResolvedOutputStorage(
            storage=storage,
            scope=scope,
            s3_prefix=s3_prefix,
        )

    if not connection_name:
        raise ValueError("Customer S3 Daytona job is missing its AWS connection name")
    connection = await _get_aws_connection(
        ctx=ctx,
        db=db,
        user_id=str(value("user_id", "")),
        space_id=value("credential_space_id"),
        connection_name=connection_name,
    )
    if connection is None:
        raise ValueError(f"AWS connection '{connection_name}' is no longer available")
    resolved_name, creds = connection
    try:
        storage = _build_customer_s3(creds=creds)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"AWS connection '{resolved_name}' has invalid S3 credentials") from exc
    return ResolvedOutputStorage(
        storage=storage,
        scope="customer_s3",
        s3_prefix=s3_prefix,
        connection_name=resolved_name,
    )


async def resolve_storage_for_url(
    url: str, ctx: Context, db: AsyncIOMotorDatabase, user: UserORM
) -> Optional[ResolvedStorage]:
    """
    Resolve which storage instance owns a file URL.

    Returns a ResolvedStorage (storage + key + scope) on match, or None if the
    URL does not belong to any known MeetKiwi or customer S3 bucket (i.e. it is
    an external URL that should be passed through unchanged).

    Resolution order:
      1. MeetKiwi public storage
      2. MeetKiwi private storage
      3. Customer S3 (iterate user's AWS connections, match by bucket)
    """
    if not url:
        return None

    deployment = await _resolve_deployment_storage(url, user)
    if deployment is not None:
        storage, key, scope = deployment
        return ResolvedStorage(storage=storage, key=key, scope=scope)

    customer = await _resolve_customer_storage(ctx=ctx, db=db, user=user, url=url)
    if customer is not None:
        return customer

    return None
