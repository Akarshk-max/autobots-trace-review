import io
import os
import tempfile
from typing import List

import httpx
import pandas as pd
import pymupdf
from fastapi import HTTPException
from loguru import logger
from pydantic import BaseModel

from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.core.cancellation.blocking_work import run_blocking
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify


class PDFConfig(BaseModel):
    urls: List[str]


def _write_merged_pdf(pdf_bytes_list: List[bytes], out_path: str) -> int:
    with pymupdf.open() as merged_doc:
        for pdf_bytes in pdf_bytes_list:
            with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf_doc:
                merged_doc.insert_pdf(pdf_doc)

        page_count = merged_doc.page_count
        if page_count:
            merged_doc.save(out_path)

    return page_count


async def merge_pdf(config: PDFConfig, ctx: Context, app_state: AppState, notify: Notify | None = None) -> str:
    """
    Description
    -----------
    Downloads multiple PDF documents from the list of URLs provided in `config`,
    merges them sequentially into a single PDF using PyMuPDF, saves the merged
    output to a temporary file, uploads the final merged PDF to S3 using the
    project's public S3 client, and returns the public S3 URL. Each PDF is first
    downloaded to a temporary file, opened, appended to the merged document, and
    then deleted. All cleanup is guaranteed even if an error occurs. If no pages
    are successfully merged, an HTTPException is raised.

    Input
    -----
    config : dict
        A dictionary of arguments supplied by the caller.
        Expected structure:
            {
                "urls": [
                    "https://example.com/a.pdf",
                    "https://example.com/b.pdf",
                    ...
                ]
            }

        urls : list[str]
            A list of PDF URLs. Each URL should point directly to a valid PDF file.
            The function downloads each PDF in the order provided and appends it
            sequentially to the merged output document.

    Output
    ------
    str
        A public S3 URL pointing to the merged PDF file stored in object storage.
        If an unexpected exception occurs, the exception object is returned.
    """

    try:
        pdf_bytes_list: List[bytes] = []

        # Download PDFs
        async with httpx.AsyncClient() as client:
            for url in config.urls:
                app_state.raise_if_cancelled()
                response = await client.get(url)
                response.raise_for_status()
                pdf_bytes_list.append(response.content)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
            tmp_path = tmp_file.name

        try:
            page_count = await run_blocking(
                app_state, _write_merged_pdf, pdf_bytes_list, tmp_path
            )
            if page_count == 0:
                raise HTTPException(500, "No pages were merged from the provided URLs")

            # Upload to S3
            with open(tmp_path, "rb") as f:
                s3_url = await get_public_s3().put_file_obj(
                    io.BytesIO(f.read()),
                    f"merged_pdf_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}.pdf",
                )
            return str(s3_url)

        finally:
            # Clean up temp file
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Error in merge_pdf: {str(e)}")
        return e
