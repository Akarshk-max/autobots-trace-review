import os
import tempfile
from typing import Type

import httpx
import pymupdf
from fastapi import HTTPException
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
from src.autobots.core.cancellation.blocking_work import run_blocking
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify


class PDFParserReq(BaseModel):
    pdf_url: str = None
    use_ocr: bool = False  # Add OCR option


class PDFParserRes(BaseModel):
    extracted_text: str


class ActionPDFParser(ActionABC[PDFParserReq, PDFParserReq, PDFParserReq, DataBlock, DataBlocks]):
    type = ActionType.action_text2text_pdf_parser

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return PDFParserReq

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return PDFParserReq

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return PDFParserReq

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    @staticmethod
    def get_description() -> str:
        return (
            "Extracts text from a PDF located at a given URL. The tool downloads the "
            "PDF, parses its pages, and returns the combined extracted text. OCR can be "
            "optionally enabled, although plain text extraction is used by default.\n\n"
            "Input:\n"
            "  {\n"
            '      "pdf_url": "https://example.com/document.pdf",\n'
            '      "use_ocr": false\n'
            "  }\n\n"
            "  pdf_url: A direct URL to a PDF file that should be downloaded and parsed.\n"
            "  use_ocr: Optional boolean flag to enable OCR-based extraction if available.\n\n"
            "Output:\n"
            "  An object containing the extracted text in the field `extracted_text`. "
            "If an unexpected error occurs, the exception is returned."
        )

    @staticmethod
    def remove_file(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

    @staticmethod
    def extract_text(pdf_path: str, use_ocr: bool = False) -> str:
        text = ""
        with pymupdf.open(pdf_path) as doc:
            for page in doc:
                text += page.get_text()
                # if use_ocr:
                #     text += page.get_textpage_ocr().extractText()
                # else:
                #     text += page.get_text()
        return text

    @staticmethod
    async def download_pdf(url: str, ctx: Context) -> str:
        """Download PDF from a URL and save it to a temporary file."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        try:
            async with httpx.AsyncClient(timeout=300.0, verify=False, headers=headers) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        logger.bind(ctx=ctx).exception(
                            f"Failed to download PDF. HTTP Status: {response.status_code}"
                        )
                        raise HTTPException(
                            status_code=response.status_code,
                            detail=f"Failed to download PDF. HTTP Status: {response.status_code}",
                        )

                    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
                    temp_path = temp_file.name
                    downloaded = False

                    try:
                        async for chunk in response.aiter_bytes():
                            if chunk:
                                temp_file.write(chunk)
                        downloaded = True
                    finally:
                        temp_file.close()
                        if not downloaded:
                            ActionPDFParser.remove_file(temp_path)

                    return temp_path
        except HTTPException:
            raise
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error downloading PDF: {e}")
            raise HTTPException(status_code=500, detail=f"Error downloading PDF: {e}")

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        try:
            pdf_path = await ActionPDFParser.download_pdf(action_input.urls[0], ctx)
            try:
                extracted_text = await run_blocking(
                    app_state,
                    ActionPDFParser.extract_text,
                    pdf_path,
                    self.action_config.use_ocr,
                )
            finally:
                ActionPDFParser.remove_file(pdf_path)
            return DataBlocks(blocks=[DataBlock(text=extracted_text)])
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in running PDF parser action: {str(e)}")
            return e

    async def run_tool(
        self, action_config: PDFParserReq, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> PDFParserRes | Exception:
        try:
            pdf_path = await ActionPDFParser.download_pdf(action_config.pdf_url, ctx)
            try:
                text = await run_blocking(
                    app_state,
                    ActionPDFParser.extract_text,
                    pdf_path,
                    action_config.use_ocr,
                )
            finally:
                ActionPDFParser.remove_file(pdf_path)

            return PDFParserRes(extracted_text=text)
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in running PDF parser action: {str(e)}")
            return e
