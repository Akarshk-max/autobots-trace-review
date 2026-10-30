import io
import json
import os
import tempfile
import typing
from io import StringIO
from typing import Type

import httpx
import pandas as pd
from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.abc.ActionABC import ActionABC
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.core.cancellation.blocking_work import run_blocking
from src.autobots.core.url_utils import extract_urls
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM


def _read_csv_text(csv_text: str) -> pd.DataFrame:
    return pd.read_csv(
        StringIO(csv_text),
        keep_default_na=False,
        na_values=[],
        true_values=[],
        false_values=[],
        dtype=str,
    )


def _write_frames_csv(frames: typing.List[pd.DataFrame], out_path: str) -> None:
    pd.concat(frames, ignore_index=True).to_csv(out_path, index=False)


class CSVMergeConfig(BaseModel):
    urls: typing.List[str] = []
    text: str = ""  # Optional: LLM can pass text containing URLs; we extract them


class ActionCSVMerge(
    ActionABC[CSVMergeConfig, CSVMergeConfig, CSVMergeConfig, DataBlock, DataBlocks]
):
    type = ActionType.csv_merge

    @staticmethod
    def get_config_create_type() -> Type[CSVMergeConfig]:
        return CSVMergeConfig

    @staticmethod
    def get_config_update_type() -> Type[CSVMergeConfig]:
        return CSVMergeConfig

    @staticmethod
    def get_config_type() -> Type[CSVMergeConfig]:
        return CSVMergeConfig

    @staticmethod
    def get_input_type() -> Type[DataBlock]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[DataBlocks]:
        return DataBlocks

    @staticmethod
    def get_description() -> str:
        return """
            Description
            -----------
            Downloads multiple CSV files from URLs, merges them vertically into a single
            pandas DataFrame, writes to a temporary CSV, uploads to S3, and returns the
            public S3 URL. URLs can be passed as explicit list or embedded in text (we
            extract http/https URLs). Temporary files are cleaned up. All CSVs read as strings.

            Input
            -----
            config : dict
                Either or both of:
                text : str
                    Pass the entire text containing CSV URLs. We extract http/https URLs.
                    Example: "Here are the files: https://example.com/a.csv and https://example.com/b.csv"
                urls : list[str]
                    Explicit list of CSV file URLs.
        """

    @staticmethod
    def _collect_urls_from_config(config: CSVMergeConfig) -> typing.List[str]:
        """Collect URLs from config.urls and by extracting from config.text."""
        urls: typing.List[str] = list(config.urls)
        if config.text:
            extracted = extract_urls(
                config.text,
                allowed_schemes={"http", "https"},
                unique=True,
                validate=True,
            )
            for u in extracted:
                if u not in urls:
                    urls.append(u)
        return urls

    @staticmethod
    async def update_config(
        ctx: Context,
        app_state: AppState,
        config: CSVMergeConfig,
        config_update: CSVMergeConfig,
        user: UserORM,
        db: AsyncIOMotorDatabase,
    ) -> CSVMergeConfig:
        urls = ActionCSVMerge._collect_urls_from_config(config or CSVMergeConfig())
        for u in ActionCSVMerge._collect_urls_from_config(config_update or CSVMergeConfig()):
            if u not in urls:
                urls.append(u)
        return CSVMergeConfig(urls=urls)

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        try:
            # Extract URLs from text if process_url_in_text is enabled
            if action_input.metadata.process_url_in_text:
                action_input.process_url_in_text(ctx)
                # logger.bind(ctx=ctx).info(
                #     "Extracted URLs from text field in CSV Merge action"
                # )
            # Build config from input datablock: urls + extract from text
            input_config = CSVMergeConfig(urls=action_input.urls, text=action_input.text or "")
            config = await ActionCSVMerge.update_config(
                ctx, app_state, self.action_config, input_config, user=self.user, db=self.db
            )
            urls = ActionCSVMerge._collect_urls_from_config(config)
            merged_csv = await self.merge_csvs(
                ctx=ctx, app_state=app_state, config=CSVMergeConfig(urls=urls)
            )

            datablocks = DataBlocks(blocks=[DataBlock(urls=merged_csv.urls)])
            return datablocks
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in {ActionCSVMerge.__name__}: {str(e)}")
            return e

    async def run_tool(
        self, action_config: CSVMergeConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> CSVMergeConfig | Exception:
        try:
            config = await self.create_config(
                ctx=ctx, app_state=app_state, config_create=action_config, user=self.user, db=self.db
            )
            urls = ActionCSVMerge._collect_urls_from_config(config)
            if not urls:
                raise HTTPException(
                    status_code=400,
                    detail="No URLs to merge. Pass 'text' with URLs embedded, or 'urls' array.",
                )
            merged_csv = await self.merge_csvs(
                ctx=ctx, app_state=app_state, config=CSVMergeConfig(urls=urls)
            )

            return merged_csv
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in run tool: {str(e)}")
            return e

    @staticmethod
    async def _expand_json_urls(
        client: httpx.AsyncClient,
        ctx: Context,
        urls: typing.List[str],
    ) -> typing.List[str]:
        """Replace any .json URLs with the list of URLs they contain."""
        expanded: typing.List[str] = []
        for url in urls:
            if url.lower().split("?")[0].endswith(".json"):
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    data = json.loads(response.text)
                    inner_urls = data.get("urls", []) if isinstance(data, dict) else []
                    if inner_urls:
                        logger.bind(ctx=ctx).info(
                            f"CSV merge: expanded JSON URL {url} into {len(inner_urls)} URLs"
                        )
                        expanded.extend(inner_urls)
                        continue
                except Exception as e:
                    logger.bind(ctx=ctx).warning(
                        f"CSV merge: failed to expand JSON URL {url}: {e}; treating as CSV"
                    )
            expanded.append(url)
        return expanded

    async def merge_csvs(
        self,
        ctx: Context,
        app_state: AppState,
        config: CSVMergeConfig,
        notify: Notify | None = None,
    ) -> CSVMergeConfig:
        try:
            frames: typing.List[pd.DataFrame] = []
            failed_urls: typing.List[typing.Tuple[str, str]] = []

            # Download and merge CSVs, skipping failed ones
            async with httpx.AsyncClient() as client:
                csv_urls = await self._expand_json_urls(client, ctx, config.urls)
                for url in csv_urls:
                    app_state.raise_if_cancelled()
                    try:
                        response = await client.get(url)
                        response.raise_for_status()
                        frames.append(
                            await run_blocking(app_state, _read_csv_text, response.text)
                        )
                    except Exception as e:
                        failed_urls.append((url, str(e)))
                        logger.bind(ctx=ctx).warning(
                            f"CSV merge: skipping failed URL {url}: {e}"
                        )
                        continue

            if not frames:
                raise HTTPException(
                    status_code=400,
                    detail=f"No data was merged from the provided URLs. All {len(csv_urls)} URL(s) failed."
                    + (
                        f" Failures: {[f'{u}: {err}' for u, err in failed_urls]}"
                        if failed_urls
                        else ""
                    ),
                )

            if failed_urls:
                logger.bind(ctx=ctx).info(
                    f"CSV merge: merged {len(csv_urls) - len(failed_urls)} of {len(csv_urls)} CSVs; "
                    f"skipped {len(failed_urls)} failed: {[u for u, _ in failed_urls]}"
                )

            # Save to temporary file
            with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp_file:
                tmp_path = tmp_file.name

            try:
                await run_blocking(app_state, _write_frames_csv, frames, tmp_path)

                # Upload to S3
                with open(tmp_path, "rb") as f:
                    s3_url = await get_public_s3().put_file_obj(
                        io.BytesIO(f.read()),
                        f"merged_csv_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}_{str(gen_uuid())}.csv",
                    )
                return CSVMergeConfig(urls=[str(s3_url)])

            finally:
                # Clean up temp file
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in csv_merge: {str(e)}")
            raise
