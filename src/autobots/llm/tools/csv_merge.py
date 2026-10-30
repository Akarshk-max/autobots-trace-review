import io
import os
import tempfile
from io import StringIO
from typing import List

import httpx
import pandas as pd
from loguru import logger
from pydantic import BaseModel

from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.core.cancellation.blocking_work import run_blocking
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify


class CSVConfig(BaseModel):
    urls: List[str]


def _write_merged_csv(csv_texts: List[str], out_path: str) -> None:
    frames = [
        pd.read_csv(
            StringIO(csv_text),
            keep_default_na=False,
            na_values=[],
            true_values=[],
            false_values=[],
            dtype=str,
        )
        for csv_text in csv_texts
    ]
    pd.concat(frames, ignore_index=True).to_csv(out_path, index=False)


async def csv_merge(config: CSVConfig, ctx: Context, app_state: AppState, notify: Notify | None = None) -> str:
    """
    Description
    -----------
    Downloads multiple CSV files from the URLs provided in `config`, merges them
    vertically into a single pandas DataFrame, writes the merged data to a temporary
    CSV file, uploads that file to S3 using the project's public S3 client, and then
    returns the public S3 URL of the merged output. Temporary files are cleaned up
    regardless of success or failure. All CSVs are read as strings with NA handling
    disabled to avoid unintended type conversion.

    Input
    -----
    config : dict
        A dictionary of arguments supplied by the caller.
        Expected structure:
            {
                "urls": [
                    "https://example.com/a.csv",
                    "https://example.com/b.csv",
                    ...
                ]
            }

        urls : list[str]
            A list of CSV file URLs. Each URL should point directly to a valid
            CSV file. The function downloads each CSV in the order provided and
            merges them vertically.

    Output
    ------
    str
        A public S3 URL pointing to the merged CSV file that was generated and uploaded.
    """
    try:
        csv_texts: List[str] = []

        # Download CSVs
        async with httpx.AsyncClient() as client:
            for url in config.urls:
                app_state.raise_if_cancelled()
                response = await client.get(url)
                response.raise_for_status()
                csv_texts.append(response.text)

        if not csv_texts:
            raise ValueError("No data was merged from the provided URLs")

        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp_file:
            tmp_path = tmp_file.name

        try:
            await run_blocking(app_state, _write_merged_csv, csv_texts, tmp_path)

            # Upload to S3
            with open(tmp_path, "rb") as f:
                s3_url = await get_public_s3().put_file_obj(
                    io.BytesIO(f.read()),
                    f"merged_csv_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}_{str(gen_uuid())}.csv",
                )
            return str(s3_url)

        finally:
            # Clean up temp file
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Error in csv_merge: {str(e)}")
        raise
