import io
import os
import tempfile
from typing import Type

import httpx
import pandas as pd
from fastapi import HTTPException, UploadFile
from loguru import logger
from pydantic import BaseModel

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.abc.ActionABC import (
    ActionABC,
    ActionConfigCreateType,
    ActionConfigType,
    ActionConfigUpdateType,
    ActionInputType,
    ActionOutputType,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.core.cancellation.blocking_work import run_blocking
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.files.user_files_model import FileMeta
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM


class CSVToParquetReq(BaseModel):
    csv_urls: list[str]
    compression: str | None = "snappy"


class CSVToParquetRes(BaseModel):
    parquet_urls: list[str]


class ActionCSVToParquet(
    ActionABC[CSVToParquetReq, CSVToParquetReq, CSVToParquetReq, DataBlock, DataBlocks]
):
    type = ActionType.action_text2text_csv_to_parquet

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return CSVToParquetReq

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return CSVToParquetReq

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return CSVToParquetReq

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    @staticmethod
    def get_description() -> str:
        return """
Convert a CSV file from a URL into Parquet format.

Description
-----------
Downloads a CSV file, converts it into Parquet format using pandas + pyarrow,
uploads the result to S3, and returns the public S3 URL.

Input
-----
{
    "csv_url": "https://example.com/data.csv",
    "compression": "snappy"
}

Fields
------
csv_url
URL pointing to a CSV file.

compression
Optional parquet compression type (snappy, gzip, brotli, zstd).

Output
------
{
    "parquet_url": "https://s3-bucket/.../data.parquet"
}
"""

    @staticmethod
    def validate_urls(urls: list[str]):
        for url in urls:
            if not url.startswith("http://") and not url.startswith("https://"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid URL: {url}",
                )

    @staticmethod
    async def download_csv(url: str, ctx: Context) -> bytes:
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await client.get(url)

                if response.status_code != 200:
                    raise HTTPException(
                        status_code=response.status_code,
                        detail=f"Failed to download CSV. HTTP Status: {response.status_code}",
                    )

                return response.content

        except HTTPException:
            raise
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error downloading CSV: {e}")
            raise HTTPException(status_code=500, detail=f"Error downloading CSV: {e}")

    @staticmethod
    def _remove_parquet_file(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

    @staticmethod
    def convert_csv_to_parquet(csv_bytes: bytes, compression: str) -> str:
        try:
            df = pd.read_csv(io.BytesIO(csv_bytes))

            tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".parquet")

            df.to_parquet(
                tmp_file.name,
                engine="pyarrow",
                compression=compression,
            )

            return tmp_file.name

        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"CSV → Parquet conversion failed: {str(e)}",
            )

    @staticmethod
    async def upload_to_s3(
        parquet_path: str,
        ctx: Context,
        user: UserORM,
    ) -> FileMeta:

        aws_s3 = get_public_s3(object_prefix="csv-to-parquet-tool")
        user_files = UserFiles(user=user, s3=aws_s3)

        with open(parquet_path, "rb") as f:
            upload_file = [
                UploadFile(
                    filename="converted.parquet",
                    file=f,
                )
            ]

            file_metas = await user_files.upload_files(ctx=ctx, files=upload_file)
        return file_metas[0]

    async def run_action(
        self,
        ctx: Context,
        app_state: AppState,
        action_input: DataBlock,
        notify: Notify | None = None,
    ) -> DataBlocks | Exception:

        try:
            urls = action_input.urls
            self.validate_urls(urls)
            parquet_urls = []
            for url in urls:
                if notify:
                    notify(ctx, f"Downloading CSV: {url}")
                csv_bytes = await self.download_csv(url, ctx)
                if notify:
                    notify(ctx, "Converting CSV to Parquet")
                parquet_path = await run_blocking(
                    app_state,
                    self.convert_csv_to_parquet,
                    csv_bytes,
                    self.action_config.compression,
                )

                try:
                    if notify:
                        notify(ctx, "Uploading Parquet to S3")

                    file_meta = await self.upload_to_s3(
                        parquet_path,
                        ctx,
                        self.user,
                    )
                finally:
                    self._remove_parquet_file(parquet_path)
                parquet_urls.append(str(file_meta.url))

            return DataBlocks(
                blocks=[
                    DataBlock(
                        urls=parquet_urls,
                        text="\n".join(parquet_urls),
                    )
                ]
            )
        except Exception as e:
            logger.bind(ctx=ctx).exception(
                f"Error in CSV to Parquet action: {str(e)}"
            )
            return e


    async def run_tool(
        self,
        action_config: CSVToParquetReq,
        ctx: Context,
        app_state: AppState,
        notify: Notify | None = None,
    ) -> CSVToParquetRes | Exception:

        try:
            self.validate_urls(action_config.csv_urls)
            parquet_urls = []
            for url in action_config.csv_urls:
                csv_bytes = await self.download_csv(url, ctx)
                parquet_path = await run_blocking(
                    app_state,
                    self.convert_csv_to_parquet,
                    csv_bytes,
                    action_config.compression,
                )
                try:
                    file_meta = await self.upload_to_s3(
                        parquet_path,
                        ctx,
                        self.user,
                    )
                finally:
                    self._remove_parquet_file(parquet_path)
                parquet_urls.append(str(file_meta.url))

            return CSVToParquetRes(parquet_urls=parquet_urls)

        except Exception as e:
            logger.bind(ctx=ctx).exception(
                f"Error in CSV to Parquet tool: {str(e)}"
            )
            return e