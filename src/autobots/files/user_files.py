import os
import re
import unicodedata
from io import BytesIO
from typing import IO, AsyncIterator, List, Optional
from urllib.parse import unquote

import httpx
from botocore.response import StreamingBody
from fastapi import HTTPException, UploadFile
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.conn.object_storage.protocol import ObjectStorage
from src.autobots.conn.unstructured_io.unstructured_io import UnstructuredIO
from src.autobots.core.settings import Settings, SettingsProvider
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.context import Context
from src.autobots.files.user_files_model import FileMeta, FilesAndProcessingParams
from src.autobots.user.user_orm_model import UserORM


class UserFiles:
    def __init__(
        self,
        user: UserORM,
        s3: ObjectStorage,
        settings: Settings = SettingsProvider.sget(),
        top_level_folder: str = "",
        connection_name: str | None = None,
    ):
        self._user = user
        self._s3 = s3
        _s3_obj_prefix = f"{settings.AWS_S3_FILES_FOLDER}/{str(user.id)}/"
        if top_level_folder:
            _s3_obj_prefix = _s3_obj_prefix.replace(settings.AWS_S3_FILES_FOLDER, top_level_folder)
        if getattr(s3, "is_system_storage", False) is True:
            self._s3.set_object_prefix(_s3_obj_prefix)
        self._download_path = f"{settings.TEMPORARY_DIR}/{_s3_obj_prefix}"
        self.unstructured = None
        # self.unstructured = get_unstructured_io()  # also here
        # Storage-source metadata used to populate FileMeta on upload.
        # For system storage we infer the scope from the configured bucket; for
        # customer storage the caller must supply the named connection.
        self._connection_name = connection_name
        self._storage_scope = self._infer_storage_scope(settings)

    def _infer_storage_scope(self, settings: Settings) -> str | None:
        """
        Determine the storage scope label to attach to FileMeta records.
        - Deployment public bucket  -> "meetkiwi_public"
        - Deployment private bucket -> "meetkiwi_private"
        - Customer S3 (non-system)  -> "customer_s3"
        """
        if getattr(self._s3, "is_system_storage", False) is not True:
            return "customer_s3"
        bucket = getattr(self._s3, "container_name", None) or ""
        public_bucket = getattr(settings, "AWS_S3_PUBLIC_BUCKET_NAME", None) or ""
        if bucket and public_bucket and bucket == public_bucket:
            return "meetkiwi_public"
        return "meetkiwi_private"

    def _build_file_meta(
        self,
        name: str,
        url: str,
    ) -> FileMeta:
        """Construct a FileMeta populated with storage-source metadata."""
        bucket = getattr(self._s3, "container_name", None)
        # Reconstruct the object key from prefix + filename. For system storage
        # the prefix is set in __init__; for customer storage it is whatever the
        # caller supplied via get_aws_s3(...).
        prefix = getattr(self._s3, "object_prefix", "") or ""
        key = f"{prefix}{name}" if prefix else name
        return FileMeta(
            name=name,
            url=str(url),
            storage_scope=self._storage_scope,
            bucket=bucket,
            key=key,
            connection_name=self._connection_name,
        )

    async def init_unstructured_io(self, ctx: Context, db: AsyncIOMotorDatabase):
        from src.autobots.integrations.app_conn.app_conn import AppConn

        self.unstructured: UnstructuredIO = await AppConn.get_unstructured_io(
            ctx=ctx, user=self._user, db=db
        )

    async def upload_files(self, ctx: Context, files: List[UploadFile]) -> List[FileMeta]:
        assert str(self._user.id) in self._s3.object_prefix
        uploaded_files = []
        for upload_file in files:
            secure_filename = await self.secure_filename(ctx, upload_file.filename)
            uploaded_file = await self._s3.upload_fileobj(ctx, upload_file.file, secure_filename)
            if not uploaded_file:
                raise RuntimeError(f"Failed to upload file: {secure_filename}")
            file_meta = self._build_file_meta(name=secure_filename, url=str(uploaded_file))
            uploaded_files.append(file_meta)
        return uploaded_files

    async def upload_binary(
        self, ctx: Context, file_obj: IO | StreamingBody, filename: str
    ) -> List[FileMeta]:
        secure_filename = await self.secure_filename(ctx, filename)
        uploaded_file = await self._s3.upload_fileobj(ctx, file_obj, secure_filename)
        file_meta = self._build_file_meta(name=secure_filename, url=str(uploaded_file))
        return [file_meta]

    async def upload_url(self, ctx: Context, file_url: str) -> List[FileMeta]:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(file_url)
            resp.raise_for_status()
            binary_data = resp.content

            filename = await UserFiles.extract_file_name(
                ctx=ctx, file_url=file_url, salt=ctx.trace_id
            )
            with BytesIO(binary_data) as file_obj:
                file_metas = await self.upload_binary(ctx=ctx, file_obj=file_obj, filename=filename)
                return file_metas

    async def list_files(self, ctx: Context, filename: Optional[str] = None) -> List[FileMeta]:
        assert str(self._user.id) in self._s3.object_prefix
        # secure_prefix = await self.secure_filename(filename)
        if filename is None:
            secure_prefix = ""  # list all under user prefix
        else:
            secure_prefix = await self.secure_filename(ctx, filename)
        file_metas = []
        object_summaries = await self._s3.list(secure_prefix)
        for object_summary in object_summaries:
            filename = await self._s3.get_filename(object_summary.key)
            url = await self._s3.get_https_url(object_summary.key)
            file_metas.append(
                self._build_file_meta(name=filename, url=str(url)).model_copy(
                    update={"key": object_summary.key}
                )
            )
        return file_metas

    async def delete_files(self, ctx: Context, filenames: List[str]) -> List[str]:
        assert str(self._user.id) in self._s3.object_prefix
        objects = []
        for filename in filenames:
            secure_filename = await self.secure_filename(ctx, filename)
            deleted = await self._s3.delete(secure_filename)
            for deleted_object in deleted:
                deleted_key = deleted_object.get("Key")
                deleted_filename = await self._s3.get_filename(deleted_key)
                objects.append(deleted_filename)
        logger.info(f"Deleted files from S3: {objects}")
        return objects

    async def processed_file_and_yield(
        self, ctx: Context, files_and_processing_params: FilesAndProcessingParams
    ) -> AsyncIterator[str]:
        for file_and_params in files_and_processing_params.files_and_params:
            async for download_path_filename in self.download_file_and_yield(
                ctx=ctx, filenames=[file_and_params.filename]
            ):
                with open(download_path_filename, "rb") as downloaded_file:
                    secure_filename = await self.secure_filename(ctx, downloaded_file.name)
                    filename = downloaded_file.name.split("/")[-1]
                    # file = UploadFile(filename=f"{filename}", file=downloaded_file)
                    file_content = downloaded_file.read()
                    chunks = await self.unstructured.get_file_chunks(
                        (filename, file_content), file_and_params.processing_params
                    )
                    processed = (
                        f"########\nFilename: {secure_filename}\nContent:\n"
                        + " ".join(chunks)
                        + "\n\n"
                    )
                    logger.info(f"Processed file: {secure_filename}")
                    yield processed

    async def get_file_contents(
        self, ctx: Context, filename: str, range: List | None = None
    ) -> str:
        secure_filename = await self.secure_filename(ctx, filename)
        logger.bind(ctx=ctx, filename=filename).info(f"Downloading file: {secure_filename}")
        try:
            assert str(self._user.id) in self._download_path
            if not os.path.exists(f"{self._download_path}"):
                os.makedirs(f"{self._download_path}")
            download_path_filename = f"{self._download_path}{secure_filename}"
            with open(download_path_filename, "wb") as file:
                downloaded = await self._s3.download_fileobj(
                    ctx=ctx, filename=secure_filename, fileobj=file
                )
                assert downloaded
            with open(download_path_filename, "r", encoding="utf-8") as file:
                return file.read()
        except Exception as e:
            logger.bind(ctx=ctx, filename=filename).error(str(e))

    async def download_file_and_yield(
        self, ctx: Context, filenames: List[str]
    ) -> AsyncIterator[str]:
        self.assert_user_isolation_on_system_buckets(ctx=ctx)
        assert str(self._user.id) in self._download_path

        if not os.path.exists(f"{self._download_path}"):
            os.makedirs(f"{self._download_path}")

        for filename in filenames:
            secure_filename = await self.secure_filename(ctx, filename)
            try:
                download_path_filename = f"{self._download_path}{secure_filename}"
                with open(download_path_filename, "wb") as file:
                    downloaded = await self._s3.download_fileobj(
                        ctx=ctx, filename=secure_filename, fileobj=file
                    )
                    assert downloaded
                    logger.info(f"Downloaded file to file system: {download_path_filename}")
                yield download_path_filename

                await self.delete_downloaded_files(ctx, [secure_filename])
            except Exception as e:
                logger.bind(ctx=ctx, filename=filename).error(str(e))

    async def get_bytes(self, ctx: Context, filename: str) -> bytes | None:
        assert str(self._user.id) in self._download_path
        secure_filename = await self.secure_filename(ctx, filename)
        key = f"{self._s3.object_prefix}/{secure_filename}".replace("//", "/")
        file_bytes = await self._s3.get_bytes(ctx=ctx, key=key)
        return file_bytes

    async def delete_downloaded_files(self, ctx: Context, filenames: List[str]) -> List[str]:
        assert str(self._user.id) in self._download_path
        objects = []
        for filename in filenames:
            secure_filename = await self.secure_filename(ctx, filename)
            filepath = f"{self._download_path}{secure_filename}"
            if os.path.isfile(filepath):
                os.remove(filepath)
                objects.append(filepath)
            logger.info(f"Deleted file from file system: {filepath}")
        return objects

    # Characters disallowed on Windows + ASCII control chars (and works fine on Unix/macOS)
    # _INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
    _INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')  # Windows-illegal + ASCII control chars
    _WS = re.compile(r"\s+", flags=re.UNICODE)  # catches NBSP + other unicode whitespace
    _DOTS = re.compile(r"\.{2,}")

    # Windows reserved base names (case-insensitive)
    _RESERVED = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }

    async def secure_filename(self, ctx: Context, filename: str | None) -> str | None:
        new_name = None
        try:
            if not isinstance(filename, str):
                filename = str(filename)

            filename = unquote(filename)
            filename = unicodedata.normalize("NFC", filename)
            filename = UserFiles._WS.sub("_", filename)
            filename = UserFiles._INVALID.sub("_", filename)
            filename = filename.replace("#", "_").replace("+", "_")
            filename = UserFiles._DOTS.sub(".", filename)

            parts = filename.split(".")
            if len(parts) > 2:
                filename = "_".join(parts[:-1]) + "." + parts[-1]

            base, ext = os.path.splitext(filename)

            if not ext:
                raise HTTPException(detail="Filename must have a valid extension", status_code=400)

            ext = ext.lower()

            base = base.strip(" ._")
            if not base:
                base = gen_uuid()

            if base.upper() in UserFiles._RESERVED:
                base += "_"

            new_name = base + ext
            return new_name

            # filename = unicodedata.normalize("NFC", filename)
            # filename = UserFiles._INVALID.sub("_", filename)
            # filename = unquote(filename)
            #
            # filename = filename.replace("/", "_")
            # filename = filename.replace("\\", "_")
            # filename = filename.replace(" ", "_")
            # filename = filename.replace("#", "_")
            # filename = filename.replace("+", "_")
            # # assert ".." not in filename, "Filename cannot contain .."
            # filename_parts = filename.split(".")
            # if len(filename_parts) > 2:
            #     new_filename = "_".join(filename_parts[:-1]) + "." + filename_parts[-1]
            #     filename = new_filename
            # assert len(filename.split(".")) == 2, "Filename must have a valid extension"
            #
            # new_name = filename
            # if new_name is None:
            #     return None
            #
            # base, ext = os.path.splitext(new_name)
            # if base.upper() in UserFiles._RESERVED:
            #     base += "_"
            # base = base.strip(" .") or gen_uuid()
            # new_name = base + ext.lower()
            # return new_name

        except Exception as e:
            logger.error(f"Error while securing filename: {filename}, error: {str(e)}")
            raise HTTPException(status_code=400, detail="Filename is not valid")

    def assert_user_isolation_on_system_buckets(
        self, ctx: Context, settings=SettingsProvider.sget()
    ):
        if getattr(self._s3, "is_system_storage", False) is True:
            assert str(self._user.id) in self._s3.object_prefix

    @staticmethod
    async def extract_file_name(ctx: Context, file_url: str, salt: str = "") -> str:
        filename = file_url.split("?")[0]
        filename = filename.split("/")[-1]
        if salt:
            filename = f"{salt}_{filename}"
        return filename

    @staticmethod
    async def extract_file_ext(ctx: Context, file_url: str) -> str:
        filename = await UserFiles.extract_file_name(ctx=ctx, file_url=file_url)
        return os.path.splitext(filename)[1].lower()
