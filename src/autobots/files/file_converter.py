import json
import os
import tempfile
import time
from io import StringIO
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlparse

import httpx
import pandas as pd
from docx import Document
from fastapi import HTTPException
from loguru import logger
from pptx import Presentation
from pydantic import BaseModel, HttpUrl

from src.autobots.action.action.common_action_models import TextObj
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.user.user_orm_model import UserORM


class CSV_XLSheetToTxtConfig(BaseModel):
    url: str
    sheet_name: Optional[str] = None
    text_format: Literal["plain", "jsonl"] = "plain"  # "plain" for tab-delimited, or "jsonl"


class FileConverterConfig(BaseModel):
    path: str
    sheet_name: Optional[str] = None
    text_format: Literal["plain", "jsonl"] = "plain"  # "plain" for tab-delimited, or "jsonl"


class FileConverter:
    SUPPORTED_FILE_TYPES = [".csv", ".xls", ".xlsx", ".docx", ".doc", ".pptx", ".ppt"]

    @staticmethod
    async def convert_file_to_txt_file(
        config: FileConverterConfig, ctx: Context, user: UserORM
    ) -> HttpUrl | Exception:
        tmp_txt_path: str | None = None
        t0 = time.perf_counter()
        try:
            logger.bind(ctx=ctx, config=config).debug(
                f"Converting {config.path} to text with sheet_name={config.sheet_name} and text_format={config.text_format}"
            )

            tmp_txt = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
            tmp_txt_path = tmp_txt.name
            tmp_txt.close()

            FileConverter._convert_file_to_text_file(
                file_path=config.path,
                out_path=tmp_txt_path,
                sheet_name=config.sheet_name,
                text_format=config.text_format,
            )
            convert_s = time.perf_counter() - t0
            out_size = os.path.getsize(tmp_txt_path) if os.path.exists(tmp_txt_path) else None
            logger.bind(ctx=ctx, out_size_bytes=out_size, convert_ms=int(convert_s * 1000)).info(
                "File conversion to txt completed"
            )

            txt_file_url: HttpUrl = await FileConverter._upload_txt_file_to_public_s3(
                ctx=ctx, user=user, config=config, txt_file_path=tmp_txt_path
            )
            total_ms = int((time.perf_counter() - t0) * 1000)
            logger.bind(ctx=ctx, total_ms=total_ms, out_size_bytes=out_size).info(
                "File converted and uploaded to public S3"
            )
            return txt_file_url
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error converting {config.path} to text: {e}")
            # propagate as HTTPException if it is one, else wrap
            return e
        finally:
            if tmp_txt_path and os.path.exists(tmp_txt_path):
                try:
                    os.remove(tmp_txt_path)
                except Exception:
                    logger.bind(ctx=ctx, tmp_txt_path=tmp_txt_path).warning(
                        "Failed to remove temp txt file"
                    )

    @staticmethod
    async def csv_xls_doc_ppt_to_txt(
        config: CSV_XLSheetToTxtConfig, ctx: Context, user: UserORM
    ) -> TextObj | Exception:
        """
        Description
        -----------
        Downloads a remote CSV, Excel, Word, or PowerPoint file from the given
        URL and converts it into plain text or JSONL format. The file is first
        fetched into a temporary location, then parsed according to its type.
        Unsupported formats or download failures result in an exception.

        Input
        -----
        config : dict
            Expected structure:
                {
                    "url": "<remote file URL>",
                    "sheet_name": "<optional sheet name for Excel files>",
                    "text_format": "plain" | "jsonl"
                }

            url : str
                Link to the file to be downloaded. Supported types include:
                .csv, .xls, .xlsx, .docx, .doc, .pptx, .ppt, and .gsheet.

            sheet_name : str | None
                Optional name of a specific sheet to load when processing Excel
                documents. If omitted, the default first sheet is used.

            text_format : str
                Output representation of tabular data.
                - "plain": tab-delimited text
                - "jsonl": JSON lines format

        Output
        ------
        dict
            Expected structure:
                {
                    "text": "<converted text content>"
                }

            Returns a `TextObj` containing the extracted text from the file.
            Paragraphs, tables, and slide text are preserved where applicable.
            If the file download, parsing, or conversion fails, the exception is
            logged and returned instead.
        """
        tmp_path: str | None = None
        try:
            logger.bind(ctx=ctx, config=config).debug(
                f"Converting {config.url} to text with sheet_name={config.sheet_name} and text_format={config.text_format}"
            )
            tmp_path = await FileConverter._download_file_to_temp(ctx=ctx, url=config.url)
            file_str = FileConverter._convert_file_to_text(
                tmp_path, config.sheet_name, config.text_format
            )
            return TextObj(text=file_str)
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error converting {config.url} to text: {e}")
            # propagate as HTTPException if it is one, else wrap
            return e
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    logger.bind(ctx=ctx, tmp_path=tmp_path).warning(
                        "Failed to remove temp downloaded file"
                    )

    @staticmethod
    async def _download_file_to_temp(ctx: Context, url: str) -> str:
        ext = Path(urlparse(url).path).suffix.lower()
        t0 = time.perf_counter()

        tmp_path: str | None = None
        csv_path: str | None = None
        try:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext or "")
            tmp_path = tmp.name
            tmp.close()

            bytes_written = 0
            async with httpx.AsyncClient(follow_redirects=True) as session:
                async with session.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        logger.bind(ctx=ctx).error(
                            f"Failed to download {url}: HTTP {resp.status_code}"
                        )
                        raise HTTPException(
                            status_code=resp.status_code,
                            detail=f"Failed to download file: HTTP {resp.status_code}",
                        )
                    with open(tmp_path, "wb") as f:
                        async for chunk in resp.aiter_bytes():
                            bytes_written += len(chunk)
                            f.write(chunk)

            download_ms = int((time.perf_counter() - t0) * 1000)
            logger.bind(
                ctx=ctx,
                url=url,
                tmp_path=tmp_path,
                size_bytes=bytes_written,
                download_ms=download_ms,
            ).info("Downloaded file to temp")

            # If it's a .gsheet, parse JSON and re-download as CSV
            if ext == ".gsheet":
                logger.bind(ctx=ctx).debug("Downloading Google Sheet export")
                with open(tmp_path, "rb") as f:
                    meta = json.loads(f.read().decode("utf-8"))
                sheet_url = meta.get("url")
                if not sheet_url:
                    raise HTTPException(
                        status_code=400, detail='Invalid .gsheet: missing "url" field'
                    )
                export_url = sheet_url.rstrip("/edit") + "/export?format=csv"

                csv_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
                csv_path = csv_tmp.name
                csv_tmp.close()

                csv_bytes_written = 0
                export_t0 = time.perf_counter()
                async with httpx.AsyncClient(follow_redirects=True) as session:
                    async with session.stream("GET", export_url) as resp2:
                        if resp2.status_code != 200:
                            raise HTTPException(
                                status_code=resp2.status_code,
                                detail=f"Failed to export Google Sheet: HTTP {resp2.status_code}",
                            )
                        with open(csv_path, "wb") as f:
                            async for chunk in resp2.aiter_bytes():
                                csv_bytes_written += len(chunk)
                                f.write(chunk)

                export_ms = int((time.perf_counter() - export_t0) * 1000)
                logger.bind(
                    ctx=ctx,
                    export_url=export_url,
                    csv_path=csv_path,
                    csv_size_bytes=csv_bytes_written,
                    export_ms=export_ms,
                ).info("Exported Google Sheet to CSV")

                # replace .gsheet json temp file with .csv temp file
                if tmp_path and os.path.exists(tmp_path):
                    os.remove(tmp_path)
                tmp_path = csv_path
                csv_path = None  # ownership transferred to tmp_path
                ext = ".csv"

            assert tmp_path is not None
            return tmp_path
        except Exception:
            # best-effort cleanup of any partial temp files
            if csv_path and os.path.exists(csv_path):
                try:
                    os.remove(csv_path)
                except Exception:
                    logger.bind(ctx=ctx, csv_path=csv_path).warning(
                        "Failed to remove partial csv temp file"
                    )
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    logger.bind(ctx=ctx, tmp_path=tmp_path).warning(
                        "Failed to remove partial temp file"
                    )
            raise
    @staticmethod
    def _convert_file_to_text_file(
        file_path: str,
        out_path: str,
        sheet_name: Optional[str] = None,
        text_format: str = "plain",
    ) -> None:
        suffix = Path(file_path).suffix.lower()
        t0 = time.perf_counter()

        # CSV or Excel
        if suffix in [".csv", ".xls", ".xlsx"]:
            df = None
            if suffix == ".csv":
                df = pd.read_csv(file_path)
            elif suffix in [".xls", ".xlsx"]:
                if sheet_name:
                    df = pd.read_excel(file_path, sheet_name=sheet_name)
                else:
                    df = pd.read_excel(file_path)
                if isinstance(df, dict):
                    df = pd.concat(
                        [sheet.assign(_sheet=name) for name, sheet in df.items()],
                        ignore_index=True,
                    )
            assert df is not None
            if text_format == "jsonl":
                df.to_json(out_path, orient="records", lines=True)
            elif text_format == "plain":
                df.to_csv(out_path, sep="\t", index=False)
            else:
                raise HTTPException(
                    status_code=420, detail=f"Unsupported text format: {text_format}"
                )

        # Word docs
        elif suffix == ".doc":
            raise HTTPException(
                status_code=420,
                detail="Legacy .doc files are not supported. Please convert to .docx.",
            )
        elif suffix == ".docx":
            with open(file_path, "rb") as in_f:
                doc = Document(in_f)
                with open(out_path, "w", encoding="utf-8") as out:
                    for p in doc.paragraphs:
                        if p.text and p.text.strip():
                            out.write(p.text.strip() + "\n")
                    for table in doc.tables:
                        for row in table.rows:
                            cells = [cell.text.strip() for cell in row.cells]
                            out.write("\t".join(cells) + "\n")
        # PowerPoint
        elif suffix in {".pptx", ".ppt"}:
            with open(file_path, "rb") as in_f:
                prs = Presentation(in_f)
                with open(out_path, "w", encoding="utf-8") as out:
                    first = True
                    for slide in prs.slides:
                        if not first:
                            out.write("\n---\n")
                        first = False
                        for shape in slide.shapes:
                            if hasattr(shape, "text") and shape.text and shape.text.strip():
                                out.write(shape.text.strip() + "\n")
        else:
            raise HTTPException(status_code=420, detail=f"Unsupported file type: {suffix}")

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        try:
            out_size = os.path.getsize(out_path)
        except Exception:
            out_size = None
        logger.bind(
            file_path=file_path, out_path=out_path, out_size_bytes=out_size, elapsed_ms=elapsed_ms
        ).info("Converted file to text file")



    @staticmethod
    def _convert_file_to_text(
        file_path: str,
        sheet_name: Optional[str] = None,
        text_format: str = "plain",  # "plain" for tab-delimited, or "jsonl"
    ) -> str:
        """
        Load the downloaded file and convert it to text:
          - tab-delimited or JSON-lines for CSV/Excel
          - plain text with paragraph and table extraction for Word
          - plain text for PowerPoint
        """
        suffix = Path(file_path).suffix.lower()

        # CSV or Excel
        if suffix in [".csv", ".xls", ".xlsx"]:
            df = None
            if suffix == ".csv":
                df = pd.read_csv(file_path)
            # Excel
            elif suffix in [".xls", ".xlsx"]:
                if sheet_name:
                    df = pd.read_excel(file_path, sheet_name=sheet_name)
                else:
                    df = pd.read_excel(file_path)
                # Handle multiple sheets dict
                if isinstance(df, dict):
                    df = pd.concat(
                        [sheet.assign(_sheet=name) for name, sheet in df.items()], ignore_index=True
                    )
            assert df is not None
            if text_format == "jsonl":
                return df.to_json(orient="records", lines=True)
            elif text_format == "plain":
                return df.to_csv(sep="\t", index=False)
            else:
                raise HTTPException(
                    status_code=420, detail=f"Unsupported text format: {text_format}"
                )
        # Word docs
        elif suffix == ".doc":
            raise HTTPException(
                status_code=420,
                detail="Legacy .doc files are not supported. Please convert to .docx.",
            )
        elif suffix == ".docx":
            with open(file_path, "rb") as in_f:
                doc = Document(in_f)
                buf = StringIO()
                # extract paragraphs
                for p in doc.paragraphs:
                    if p.text and p.text.strip():
                        buf.write(p.text.strip() + "\n")
                # extract tables
                for table in doc.tables:
                    for row in table.rows:
                        cells = [cell.text.strip() for cell in row.cells]
                        buf.write("\t".join(cells) + "\n")
                return buf.getvalue().rstrip("\n")
        # PowerPoint
        elif suffix in {".pptx", ".ppt"}:
            with open(file_path, "rb") as in_f:
                prs = Presentation(in_f)
                buf = StringIO()
                first_slide = True
                for slide in prs.slides:
                    if not first_slide:
                        buf.write("\n---\n")
                    first_slide = False
                    for shape in slide.shapes:
                        if hasattr(shape, "text") and shape.text and shape.text.strip():
                            buf.write(shape.text.strip() + "\n")
                return buf.getvalue().rstrip("\n")
        else:
            raise HTTPException(status_code=420, detail=f"Unsupported file type: {suffix}")
    @staticmethod
    async def _upload_txt_file_to_public_s3(
        ctx: Context, user: UserORM, config: FileConverterConfig, txt_file_path: str
    ) -> HttpUrl:
        """Upload a local txt file to the public S3 bucket (streaming)."""
        s3 = get_public_s3()
        user_files = UserFiles(user=user, s3=s3)
        old_filename = Path(config.path).name
        old_file_parts = old_filename.split(".")
        new_filename = "".join(old_file_parts[:-1]) + ".txt"
        upload_t0 = time.perf_counter()
        txt_size = os.path.getsize(txt_file_path) if os.path.exists(txt_file_path) else None
        logger.bind(
            ctx=ctx,
            old_filename=old_filename,
            new_filename=new_filename,
            txt_size_bytes=txt_size,
        ).info("Uploading converted txt to public S3")
        with open(txt_file_path, "rb") as f:
            file_metas = await user_files.upload_binary(
                ctx=ctx, file_obj=f, filename=new_filename
            )
        upload_ms = int((time.perf_counter() - upload_t0) * 1000)
        logger.bind(ctx=ctx, upload_ms=upload_ms, txt_size_bytes=txt_size).info(
            "Uploaded converted txt to public S3"
        )
        return file_metas[0].url



    @staticmethod
    async def _upload_txt_to_public_s3(
        ctx: Context, user: UserORM, config: FileConverterConfig, file_str: str
    ) -> HttpUrl:
        tmp_txt_path: str | None = None
        try:
            tmp_txt = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
            tmp_txt_path = tmp_txt.name
            tmp_txt.close()
            with open(tmp_txt_path, "w", encoding="utf-8") as f:
                f.write(file_str)
            return await FileConverter._upload_txt_file_to_public_s3(
                ctx=ctx, user=user, config=config, txt_file_path=tmp_txt_path
            )
        finally:
            if tmp_txt_path and os.path.exists(tmp_txt_path):
                try:
                    os.remove(tmp_txt_path)
                except Exception:
                    logger.bind(ctx=ctx, tmp_txt_path=tmp_txt_path).warning(
                        "Failed to remove temp txt file"
                    )

    @staticmethod
    async def convert_bytes_to_txt(
        config: CSV_XLSheetToTxtConfig, ctx: Context, user: UserORM
    ) -> TextObj | Exception:
        tmp_path: str | None = None
        try:
            logger.bind(ctx=ctx, config=config).debug(
                f"Converting {config.url} to text with sheet_name={config.sheet_name} and text_format={config.text_format}"
            )
            tmp_path = await FileConverter._download_file_to_temp(ctx=ctx, url=config.url)
            file_str = FileConverter._convert_bytes_to_text(
                tmp_path, config.sheet_name, config.text_format
            )
            return TextObj(text=file_str)
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error converting {config.url} to text: {e}")
            # propagate as HTTPException if it is one, else wrap
            return e
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    logger.bind(ctx=ctx, tmp_path=tmp_path).warning(
                        "Failed to remove temp downloaded file"
                    )

    @staticmethod
    def _convert_bytes_to_text(
        file_path: str,
        sheet_name: Optional[str] = None,
        text_format: str = "plain",  # "plain" for tab-delimited, or "jsonl"
    ) -> str:
        """
        Load the downloaded file and convert it to text:
          - tab-delimited or JSON-lines for CSV/Excel
          - plain text with paragraph and table extraction for Word
          - plain text for PowerPoint
        """
        suffix = Path(file_path).suffix.lower()

        # CSV or Excel
        if suffix in [".csv", ".xls", ".xlsx"]:
            df = None
            if suffix == ".csv":
                df = pd.read_csv(file_path)
            # Excel
            elif suffix in [".xls", ".xlsx"]:
                if sheet_name:
                    df = pd.read_excel(file_path, sheet_name=sheet_name)
                else:
                    df = pd.read_excel(file_path)
                # Handle multiple sheets dict
                if isinstance(df, dict):
                    df = pd.concat(
                        [sheet.assign(_sheet=name) for name, sheet in df.items()], ignore_index=True
                    )
            assert df is not None
            if text_format == "jsonl":
                return df.to_json(orient="records", lines=True)
            elif text_format == "plain":
                return df.to_csv(sep="\t", index=False)
            else:
                raise HTTPException(
                    status_code=420, detail=f"Unsupported text format: {text_format}"
                )
        # Word docs
        elif suffix == ".doc":
            raise HTTPException(
                status_code=420,
                detail="Legacy .doc files are not supported. Please convert to .docx.",
            )
        elif suffix == ".docx":
            with open(file_path, "rb") as in_f:
                doc = Document(in_f)
                buf = StringIO()
                for p in doc.paragraphs:
                    if p.text and p.text.strip():
                        buf.write(p.text.strip() + "\n")
                for table in doc.tables:
                    for row in table.rows:
                        cells = [cell.text.strip() for cell in row.cells]
                        buf.write("\t".join(cells) + "\n")
                return buf.getvalue().rstrip("\n")
        # PowerPoint
        elif suffix in {".pptx", ".ppt"}:
            with open(file_path, "rb") as in_f:
                prs = Presentation(in_f)
                buf = StringIO()
                first_slide = True
                for slide in prs.slides:
                    if not first_slide:
                        buf.write("\n---\n")
                    first_slide = False
                    for shape in slide.shapes:
                        if hasattr(shape, "text") and shape.text and shape.text.strip():
                            buf.write(shape.text.strip() + "\n")
                return buf.getvalue().rstrip("\n")
        else:
            raise HTTPException(status_code=420, detail=f"Unsupported file type: {suffix}")
