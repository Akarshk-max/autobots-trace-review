import asyncio
import mimetypes
import os
from io import BytesIO
from pathlib import Path
from typing import IO, List, Literal, Optional
from urllib.parse import unquote, urlparse

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from loguru import logger
from mypy_boto3_s3.service_resource import ObjectSummary, S3ServiceResource
from mypy_boto3_s3.type_defs import DeletedObjectTypeDef
from pydantic import HttpUrl
from tenacity import retry, stop_after_attempt, wait_exponential_jitter, wait_random

from src.autobots.conn.object_storage.models import SignedUpload
from src.autobots.core.settings import Settings
from src.autobots.data_model.context import Context


class AwsS3:
    provider = "s3"

    def __init__(
        self,
        region_name: str,
        aws_access_key_id: str,
        aws_secret_access_key: str,
        bucket_name: str,
        object_prefix: str = "default",
        is_system_storage: bool = False,
    ):
        aws_s3: S3ServiceResource = boto3.resource(
            service_name="s3",
            region_name=region_name,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
        )
        self.bucket = aws_s3.Bucket(bucket_name)
        self.container_name = bucket_name
        self.is_system_storage = is_system_storage
        # self.s3 = s3
        # self.object_prefix = f"{object_prefix}" if object_prefix != "" else object_prefix
        self.object_prefix = None
        self.set_object_prefix(object_prefix)

        # NOTE:
        # Store these for creating alternate S3 clients (presign variants) without relying on
        # botocore private fields (e.g. _request_signer._credentials).
        #
        # Motivation: Daytona sandboxes have been observed to fail when uploading to virtual-hosted
        # S3 endpoints (<bucket>.s3.amazonaws.com) but succeed with regional path-style endpoints
        # (s3.<region>.amazonaws.com/<bucket>/<key>).
        self._region_name = region_name
        self._aws_access_key_id = aws_access_key_id
        self._aws_secret_access_key = aws_secret_access_key
        self._presign_client_regional_path = None

        # We need a low-level client for advanced S3 operations.
        # This is currently used by the scheduler for efficient server-side file copies.
        self.s3_client = boto3.client(
            "s3",
            region_name=region_name,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            config=Config(signature_version="s3v4"),
        )

    def _get_upload_extra_args(self, filename: str) -> dict:
        """
        HTML + images: render in browser
        Everything else: force download
        """
        ext = Path(filename).suffix.lower()
        base_name = os.path.basename(filename)

        # Let mimetypes guess a reasonable Content-Type
        guessed_type, _ = mimetypes.guess_type(filename)
        content_type = guessed_type or "application/octet-stream"

        # Treat these as "open in browser"
        html_exts = {".html", ".htm"}

        image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".svg"}

        inline_exts = {".pdf"}

        if ext in html_exts:
            return {
                "ContentType": "text/html; charset=utf-8",
            }

        if ext in image_exts:
            return {
                "ContentType": content_type,
                "ContentDisposition": f'inline; filename="{base_name}"',
            }

        if ext in inline_exts:
            return {
                "ContentType": content_type,
                "ContentDisposition": f'inline; filename="{base_name}"',
            }

        # Everything else: force download
        return {
            "ContentType": content_type,
            "ContentDisposition": f'attachment; filename="{base_name}"',
        }

    def set_object_prefix(self, object_prefix: str) -> None:
        normalized = (object_prefix or "").strip("/")
        self.object_prefix = f"{normalized}/" if normalized else ""

    async def put(self, ctx: Context, data: str, filename: str) -> int:
        object_path = self.object_prefix + filename
        try:
            with BytesIO(data.encode("utf-8")) as file_obj:
                extra_args = self._get_upload_extra_args(filename)
                self.bucket.upload_fileobj(file_obj, object_path, ExtraArgs=extra_args)
                return len(data)
        except Exception as e:
            logger.bind(ctx=ctx).error(str(e))
        return -1

    async def put_file_obj(self, file_obj: BytesIO, filename: str) -> HttpUrl:
        """TO BE DEPRECATED, use put_file_obj_v1 instead"""
        object_path = self.object_prefix + filename
        try:
            length = len(file_obj.getvalue())
            extra_args = self._get_upload_extra_args(filename)
            logger.debug(f"Uploading file to s3 with extra args: {extra_args}")
            self.bucket.upload_fileobj(file_obj, object_path, ExtraArgs=extra_args)
            object_url = await self.get_https_url(object_path)
            logger.debug(f"File written to s3, filename: {object_url}, size: {length}")
            return object_url
        except Exception as e:
            logger.error(str(e))
        return ""

    async def upload_fileobj(
        self, ctx: Context, file_obj: IO | StreamingBody | StreamingBody, filename: str
    ) -> HttpUrl:
        """
        Uploads any file type
        """
        try:
            url = await self.upload_fileobj_retry(ctx=ctx, file_obj=file_obj, filename=filename)
            return url
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in upload_fileobj: {str(e)}")
            raise

    @retry(wait=wait_exponential_jitter(0.5, 10), stop=stop_after_attempt(4), reraise=True)
    async def upload_fileobj_retry(
        self, ctx: Context, file_obj: IO | StreamingBody | StreamingBody, filename: str
    ) -> HttpUrl:
        """
        Uploads any file type
        """
        object_path = f"{self.object_prefix}{filename}"
        try:
            extra_args = self._get_upload_extra_args(filename)
            self.bucket.upload_fileobj(file_obj, object_path, ExtraArgs=extra_args)
            object_url = await self.get_https_url(object_path)
            logger.bind(ctx=ctx).debug(f"Fileobj written to s3, filename: {object_url}")
            return object_url
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in upload_fileobj_retry: {str(e)}")
            raise
        # return ""

    async def download_fileobj(
        self, ctx: Context, filename: str, fileobj: IO | StreamingBody | StreamingBody
    ) -> HttpUrl:
        """
        Downloads any file type
        """
        object_path = self.object_prefix + filename
        try:
            self.bucket.download_fileobj(object_path, fileobj)
            object_url = await self.get_https_url(object_path)
            logger.debug(f"Fileobj Downloaded, filename: {object_url}")
            return object_url
        except Exception as e:
            logger.bind(
                ctx=ctx, bucket=self.bucket, object_path=object_path, filename=filename
            ).error(f"Error in download_fileobj: {str(e)}")
            raise

    async def download_key_to_fileobj(self, ctx: Context, key: str, fileobj: IO[bytes]) -> None:
        try:
            if key.startswith("/"):
                key = key[1:]
            await asyncio.to_thread(self.bucket.download_fileobj, key, fileobj)
        except Exception as e:
            logger.bind(ctx=ctx, key=key).exception(f"Error in download_key_to_fileobj: {str(e)}")
            raise

    async def get(self, filename: str) -> str:
        object_path = self.object_prefix + filename
        try:
            with BytesIO() as res_bytes:
                self.bucket.download_fileobj(object_path, res_bytes)
                res_data = res_bytes.getvalue().decode("utf-8")
                return res_data
        except Exception as e:
            logger.error(str(e))

    async def get_bytes(self, ctx: Context, key: str) -> bytes:
        try:
            data = await self.get_bytes_retry(ctx=ctx, key=key)
            return data
        except Exception as e:
            logger.bind(ctx=ctx, key=key).exception(f"Error in get bytes: {str(e)}")

    @retry(wait=wait_random(min=2, max=4), stop=stop_after_attempt(20), reraise=True)
    async def get_bytes_retry(self, ctx: Context, key: str) -> bytes:
        try:
            if key.startswith("/"):
                key = key[1:]
            with BytesIO() as res_bytes:
                self.bucket.download_fileobj(key, res_bytes)
                return res_bytes.getvalue()
        except Exception as e:
            logger.bind(ctx=ctx, key=key).exception(f"Retrying in get bytes: {str(e)}")
            raise

    async def delete(self, filename: str) -> list[DeletedObjectTypeDef]:
        object_path = filename
        if filename and not filename.startswith(self.object_prefix):
            object_path = self.object_prefix + filename
        return await self.delete_key(object_path)

    async def delete_key(self, key: str) -> list[DeletedObjectTypeDef]:
        key = (key or "").lstrip("/")
        try:
            delete_res = await asyncio.to_thread(
                self.bucket.delete_objects,
                Delete={"Objects": [{"Key": key}]},
            )
            return delete_res.get("Deleted", [])
        except Exception as e:
            logger.error(str(e))
            return []

    async def delete_https_url(self, url: HttpUrl) -> list[DeletedObjectTypeDef]:
        key = await self.get_key_from_https_url(url)
        deleted = await self.delete_prefix(prefix=key)
        return deleted

    async def list(self, prefix: Optional[str] = None, limit: int = 300) -> List[ObjectSummary]:
        try:
            if not prefix:
                prefix = self.object_prefix
            elif prefix and not prefix.startswith(self.object_prefix):
                prefix = self.object_prefix + prefix
            s3_objects = []
            size = 0
            for s3_object in self.bucket.objects.filter(Prefix=prefix):
                if size >= limit:
                    break
                size += 1
                if s3_object.key.endswith("/"):
                    continue
                s3_objects.append(s3_object)
            return s3_objects
        except Exception as e:
            logger.exception(f"Error in listing Aws S3 files: {str(e)}")

    async def delete_prefix(self, prefix: str) -> List[DeletedObjectTypeDef]:
        if prefix and not prefix.startswith(self.object_prefix):
            prefix = self.object_prefix + prefix
        else:
            extra_prefix = prefix.split(self.object_prefix)[-1]
            prefix = self.object_prefix + extra_prefix
        s3_objects = await self.list(prefix=prefix)
        deleted = []
        for s3_object in s3_objects:
            # partial_key = s3_object.key.replace(self.object_prefix, "")
            deleted_in_this_iter = await self.delete(s3_object.key)
            deleted += deleted_in_this_iter
        return deleted

    async def get_https_url_from_s3_uri(self, s3_uri: str) -> HttpUrl:
        key = await self.get_key_from_s3_uri(url=s3_uri)
        https_url = await self.get_https_url(key)
        return https_url

    async def get_s3_uri_from_https_url(self, http_url: str) -> str:
        key = await self.get_key_from_https_url(url=http_url)
        if key.startswith("/"):
            key = key[1:]
        s3_uri = await self.get_s3_uri(key)
        return s3_uri

    async def get_https_url(self, key: str) -> HttpUrl:
        """
        key is the complete object_path
        """
        if key.startswith("/"):
            key = key[1:]
        return HttpUrl(f"https://{self.bucket.name}.s3.amazonaws.com/{key}")

    async def get_key_from_https_url(self, url: HttpUrl | str) -> str:
        """
        key is the complete object_path
        """
        if isinstance(url, str):
            url = HttpUrl(url)
        key = url.path
        if key.startswith("/"):
            key = key[1:]
        return key

    async def get_s3_uri(self, key: str) -> str:
        """
        key is the complete object_path
        """
        if key.startswith("/"):
            key = key[1:]
        return f"s3://{self.bucket.name}/{key}"

    async def get_key_from_s3_uri(self, url: str) -> str:
        """
        key is the complete object_path
        """
        if url.startswith("s3://"):
            protocol_removed = url.replace("s3://", "")
            parts = protocol_removed.split("/")
            key = "/".join(parts[1:])
            if key.startswith("/"):
                key = key[1:]
            return key

    async def get_key(self, url: str) -> str:
        key = ""
        if url.startswith("s3://"):
            key = await self.get_key_from_s3_uri(url)
        elif url.startswith("https://"):
            key = await self.get_key_from_https_url(url)
        return key

    async def get_filename(self, key: str) -> str:
        """
        key is the s3 object_path
        """
        return key.split("/")[-1]

    async def get_url(self, key: str) -> str:
        return str(await self.get_https_url(key))

    async def get_key_if_owned_url(self, url: str) -> str | None:
        parsed = urlparse(str(url))
        if parsed.scheme.lower() != "https":
            return None

        host = (parsed.netloc or "").lower()
        path = unquote((parsed.path or "").lstrip("/"))
        bucket = self.container_name.lower()

        # Virtual-hosted S3:
        #   <bucket>.s3.amazonaws.com/key
        #   <bucket>.s3.<region>.amazonaws.com/key
        if host.startswith(f"{bucket}.") and host.endswith(".amazonaws.com"):
            return path or None

        # Path-style S3:
        #   s3.amazonaws.com/<bucket>/key
        #   s3.<region>.amazonaws.com/<bucket>/key
        is_path_style = host == "s3.amazonaws.com" or (
            host.startswith("s3.") and host.endswith(".amazonaws.com")
        )
        if is_path_style and path:
            first, separator, key = path.partition("/")
            if separator and first.lower() == bucket:
                return key or None

        return None

    async def exists(self, key: str) -> bool:
        key = (key or "").lstrip("/")

        def _head() -> None:
            self.s3_client.head_object(Bucket=self.container_name, Key=key)

        try:
            await asyncio.to_thread(_head)
            return True
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    async def copy(self, source_key: str, destination_key: str) -> None:
        source_key = (source_key or "").lstrip("/")
        destination_key = (destination_key or "").lstrip("/")

        await asyncio.to_thread(
            self.s3_client.copy_object,
            CopySource={"Bucket": self.container_name, "Key": source_key},
            Bucket=self.container_name,
            Key=destination_key,
        )

    def _get_presign_client_regional_path(self):
        """
        Lazy, cached S3 client used only for generating Daytona-safe presigned PUT URLs.
        We keep it separate from self.s3_client to avoid altering behavior for other callers.
        """
        if self._presign_client_regional_path is not None:
            return self._presign_client_regional_path

        region = self._region_name

        # --- TEMP LOGS START (S3 PRESIGN CLIENT DIAG) ---
        # No ctx available here; keep this debug-only and easy to remove.
        logger.bind(bucket=self.bucket.name, region=region).debug(
            "--- TEMP S3 PRESIGN CLIENT DIAG START --- building regional path-style presign client"
        )
        # --- TEMP LOGS END (S3 PRESIGN CLIENT DIAG) ---

        # Regional path-style endpoint:
        #   https://s3.<region>.amazonaws.com/<bucket>/<key>
        # This endpoint shape has proven more reliable in Daytona sandboxes.
        self._presign_client_regional_path = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=f"https://s3.{region}.amazonaws.com",
            aws_access_key_id=self._aws_access_key_id,
            aws_secret_access_key=self._aws_secret_access_key,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        return self._presign_client_regional_path

    async def generate_presigned_put_url(
        self,
        key: str,
        expiration: int = 3600,
        *,
        url_style: Literal["default", "regional_path"] = "default",
    ) -> str:
        """
        Generate a presigned PUT URL for a specific S3 key.
        Max expiration is constrained by the credentials used to sign the request.

        url_style:
          - "default": use the standard client behavior (may produce virtual-hosted style)
          - "regional_path": force regional path-style endpoint
              https://s3.<region>.amazonaws.com/<bucket>/<key>?X-Amz-...

        Motivation:
          Daytona sandboxes have been seen to reset connections when uploading to
          virtual-hosted S3 URLs like https://<bucket>.s3.amazonaws.com/<key>?...
          but succeed against regional path-style URLs.
        """
        if key.startswith("/"):
            key = key[1:]

        if url_style == "regional_path":
            client = self._get_presign_client_regional_path()
        else:
            client = self.s3_client

        return client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket.name, "Key": key},
            ExpiresIn=expiration,
        )

    async def generate_presigned_get_url(
        self,
        key: str,
        expiration: int = 21600,
        *,
        url_style: Literal["default", "regional_path"] = "regional_path",
    ) -> str:
        """
        Generate a short-lived presigned GET URL for a specific S3 key.

        Used to grant Daytona sandboxes (or any external downloader) time-limited
        read access to private objects without forwarding long-lived AWS
        credentials into the sandbox process.

        Defaults:
          - expiration=21600 (6 hours). IAM-user credentials support up to 7
            days (604800s); 6h is a conservative default that comfortably covers
            the sandbox-creation + pip-install + input-download window while
            keeping blast radius small.
          - url_style="regional_path" because Daytona sandboxes have been
            observed to handle path-style regional S3 endpoints better than
            virtual-hosted style (same rationale as the PUT presign path).

        Max expiration is constrained by the credentials used to sign the
        request (IAM user keys: 7 days; STS/temporary creds: session lifetime).
        """
        if key.startswith("/"):
            key = key[1:]

        if url_style == "regional_path":
            client = self._get_presign_client_regional_path()
        else:
            client = self.s3_client

        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket.name, "Key": key},
            ExpiresIn=expiration,
        )

    async def generate_signed_upload(
        self,
        key: str,
        expiration: int = 3600,
        *,
        upload_profile: str = "default",
    ) -> SignedUpload:
        url_style: Literal["default", "regional_path"] = (
            "regional_path" if upload_profile == "daytona" else "default"
        )
        key = (key or "").lstrip("/")
        put_url = await self.generate_presigned_put_url(
            key=key,
            expiration=expiration,
            url_style=url_style,
        )
        public_url = str(await self.get_https_url(key))
        return SignedUpload(
            url=put_url,
            public_url=public_url,
            key=key,
            headers={},
        )

    async def generate_presigned_put_url_for_file(
        self,
        filename: str,
        expiration: int = 3600,
        *,
        url_style: Literal["default", "regional_path"] = "default",
    ) -> tuple[str, str]:
        """
        Uses current object_prefix + filename. Returns (presigned_put_url, public_https_url).
        """
        key = f"{self.object_prefix}{filename}"
        if key.startswith("/"):
            key = key[1:]
        put_url = await self.generate_presigned_put_url(
            key=key,
            expiration=expiration,
            url_style=url_style,
        )
        public_url = str(await self.get_https_url(key))
        return put_url, public_url


def get_aws_s3(
    region_name: str,
    aws_access_key_id: str,
    aws_secret_access_key: str,
    bucket_name: str,
    object_prefix: str = "default",
) -> AwsS3:
    return AwsS3(
        region_name=region_name,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        bucket_name=bucket_name,
        object_prefix=object_prefix,
    )


def get_s3(
    settings: Settings | None = None,
    object_prefix: str = "default",
):
    """Compatibility alias for deployment-level private object storage."""
    from src.autobots.conn.object_storage.factory import get_storage

    return get_storage(settings=settings, object_prefix=object_prefix)


def get_s3_for_image_upload(
    settings: Settings | None = None,
    object_prefix: str = "",
):
    """Compatibility alias for deployment-level public image storage."""
    from src.autobots.conn.object_storage.factory import get_image_storage

    return get_image_storage(settings=settings, object_prefix=object_prefix)


def get_public_s3(
    settings: Settings | None = None,
    object_prefix: str = "default",
):
    """Compatibility alias for deployment-level public object storage."""
    from src.autobots.conn.object_storage.factory import get_public_storage

    return get_public_storage(settings=settings, object_prefix=object_prefix)
