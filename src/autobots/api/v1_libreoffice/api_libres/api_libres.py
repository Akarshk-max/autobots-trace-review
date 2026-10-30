import asyncio
import io
import mimetypes
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List

import supabase_auth
from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pandas import Timestamp
from starlette.requests import Request

from src.autobots import SettingsProvider
from src.autobots.auth.security import get_access_token, get_offline_user_from_access_token
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.utils import gen_uuid  # your existing UUID helper
from src.autobots.files.file_downloader import FileDownloader

router = APIRouter(
    prefix=SettingsProvider.sget().API_LIBREOFFICE, tags=[SettingsProvider.sget().API_LIBREOFFICE]
)


def guess_mime_from_url(url: str) -> str | None:
    # strip query and fragment
    clean = url.split("?")[0].split("#")[0]
    mime, _ = mimetypes.guess_type(clean)
    return mime


def _ensure_writable_dir(pref: Path) -> Path:
    try:
        pref.mkdir(parents=True, exist_ok=True)
        test = pref / ".write_test"
        test.write_text("ok", encoding="utf-8")
        test.unlink(missing_ok=True)
        return pref
    except Exception:
        # Fallback to /tmp
        fallback = Path(tempfile.gettempdir()) / "pdf_out"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


@router.post("/")
async def convert_to_pdf(
    request: Request,
    url: str = Query(
        ...,
        description="Source file URL (s3://... or http(s)://...)",
    ),
    supported_file_formats: List[str] = Query(
        ["DOCX", "CSV", "HTML", "JSON", "XLSX"],
        description="Supported source file formats (case-insensitive, no leading dot)",
    ),
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    token: str = Depends(get_access_token),
) -> str:
    """
    Behaviour:
    - CSV / XLS / XLSX  -> convert with LibreOffice to CSV, then rename to .txt, upload, return URL
    - PDF               -> no conversion, just copy and upload
    - Other supported   -> convert to PDF via LibreOffice, upload, return URL
    """
    ctx = request.state.context
    try:
        tmp_dir = Path(tempfile.gettempdir())

        # Try to preserve a reasonable filename + extension if possible
        url_name = Path(url.split("?")[0].split("#")[0]).name or gen_uuid()
        if "." not in url_name:
            url_name += ".bin"

        # Download source file to a temp file
        download_file_path_dict = await FileDownloader._download_urls_to_tmpfiles(
            ctx=ctx,
            user=user_res.user,
            db=db,
            urls=[url],
        )
        download_file_path_str = download_file_path_dict.get(url)
        if not download_file_path_str:
            raise HTTPException(500, "Failed to download file")

        download_file_path = Path(download_file_path_str)

        # Normalize extension
        file_ext = download_file_path.suffix.lower().lstrip(".")  # e.g. "csv"
        file_ext_upper = file_ext.upper()  # e.g. "CSV"

        # Output directory (shared for all cases)
        out_dir = _ensure_writable_dir(tmp_dir / "file_out")

        # Prepare a writable LO user profile (avoids RO $HOME issues in containers)
        lo_profile = Path(tempfile.gettempdir()) / "lo_profile"
        lo_profile.mkdir(parents=True, exist_ok=True)
        lo_user_install = f"file://{lo_profile.as_posix()}"

        # Choose soffice path: configured path if present, else rely on PATH
        soffice = SettingsProvider.sget().LIBREOFFICE_PATH
        if not Path(soffice).exists():
            soffice = "soffice"

        upload_path: Path

        # ---- Case 1: spreadsheets -> TXT (via CSV) ----
        if file_ext in {"csv", "xlsx", "xls"}:
            # Use LibreOffice to convert to CSV via StarCalc filter
            # Then rename produced *.csv to *.txt
            lo_filter = [
                "--convert-to",
                "csv:Text - txt - csv (StarCalc)",
            ]

            proc = await asyncio.to_thread(
                subprocess.run,
                [
                    soffice,
                    "-env:UserInstallation=" + lo_user_install,
                    "--headless",
                    *lo_filter,
                    str(download_file_path),
                    "--outdir",
                    str(out_dir),
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={**os.environ, "HOME": str(Path(tempfile.gettempdir()))},
            )

            produced_csv = out_dir / f"{download_file_path.stem}.csv"
            if not produced_csv.exists():
                raise FileNotFoundError(
                    f"LibreOffice did not produce {produced_csv}\n"
                    f"STDOUT:\n{proc.stdout.decode(errors='ignore')}\n"
                    f"STDERR:\n{proc.stderr.decode(errors='ignore')}"
                )

            # Rename CSV -> TXT
            txt_path = out_dir / f"{download_file_path.stem}.txt"
            if txt_path.exists():
                txt_path.unlink()
            produced_csv.rename(txt_path)

            upload_path = txt_path

        # ---- Case 2: already PDF -> just copy ----
        elif file_ext == "pdf":
            upload_path = out_dir / f"{download_file_path.stem}.pdf"
            if upload_path.exists():
                upload_path.unlink()
            shutil.copy2(download_file_path, upload_path)

        # ---- Case 3: other supported formats -> PDF via LibreOffice ----
        elif file_ext_upper in supported_file_formats:
            lo_filter = ["--convert-to", "pdf"]

            proc = await asyncio.to_thread(
                subprocess.run,
                [
                    soffice,
                    "-env:UserInstallation=" + lo_user_install,
                    "--headless",
                    *lo_filter,
                    str(download_file_path),
                    "--outdir",
                    str(out_dir),
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={**os.environ, "HOME": str(Path(tempfile.gettempdir()))},
            )

            produced_pdf = out_dir / f"{download_file_path.stem}.pdf"
            if not produced_pdf.exists():
                raise FileNotFoundError(
                    f"LibreOffice did not produce {produced_pdf}\n"
                    f"STDOUT:\n{proc.stdout.decode(errors='ignore')}\n"
                    f"STDERR:\n{proc.stderr.decode(errors='ignore')}"
                )

            final_pdf = out_dir / f"{download_file_path.stem}.pdf"
            if produced_pdf != final_pdf:
                if final_pdf.exists():
                    final_pdf.unlink()
                produced_pdf.rename(final_pdf)

            upload_path = final_pdf

        # ---- Case 4: unsupported format ----
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported source file format: {file_ext_upper}",
            )

        if not upload_path.exists():
            raise FileNotFoundError(f"Expected output missing at {upload_path}")

        # Build S3 key based on final extension
        ext_for_key = upload_path.suffix.lstrip(".") or "bin"
        s3_key = (
            f"converted_{ext_for_key}_"
            f"{Timestamp.now().strftime('%Y%m%d_%H%M%S')}_{gen_uuid()}."
            f"{ext_for_key}"
        )

        with open(upload_path, "rb") as f:
            s3_url = await get_public_s3().put_file_obj(
                io.BytesIO(f.read()),
                s3_key,
            )

        return str(s3_url)

    except Exception as e:
        logger.bind(ctx=ctx).exception(str(e))
        raise e if isinstance(e, HTTPException) else HTTPException(500, "Error in converting file")
