import asyncio
import io
import mimetypes
import secrets
import socket
from ipaddress import ip_address
from typing import List, Optional
from urllib.parse import unquote, urljoin, urlparse

import httpx
from fastapi import UploadFile
from loguru import logger
from pydantic import BaseModel, HttpUrl, model_validator

from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.files.user_files_model import FileMeta
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState


class FileObj(BaseModel):
    url: HttpUrl

    @model_validator(mode="after")
    def validate_url(self):
        return self


class FileMetas(BaseModel):
    file_metas: List[FileMeta]


class SaveFilesToolConfig(BaseModel):
    files: List[FileObj]


def _random_name_8() -> str:
    return secrets.token_urlsafe(8)[:8]


def _sanitize_filename(name: str) -> str:
    name = name.strip().strip('"').strip("'")
    name = name.replace("\x00", "")
    name = name.replace("/", "_").replace("\\", "_")
    if not name or name in {".", ".."}:
        return ""
    return name


def _filename_from_content_disposition(cd: Optional[str]) -> Optional[str]:
    if not cd:
        return None

    parts = [p.strip() for p in cd.split(";") if p.strip()]
    lower_parts = [p.lower() for p in parts]

    for p, lp in zip(parts, lower_parts):
        if lp.startswith("filename*="):
            val = p.split("=", 1)[1].strip()
            if "''" in val:
                _, enc = val.split("''", 1)
                return unquote(enc)
            return unquote(val.strip('"'))

    for p, lp in zip(parts, lower_parts):
        if lp.startswith("filename="):
            val = p.split("=", 1)[1].strip()
            return val.strip('"')

    return None


def _filename_from_url(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
        path = parsed.path or ""
        if not path or path == "/":
            return None
        last = path.rstrip("/").split("/")[-1]
        last = unquote(last)
        return last or None
    except Exception:
        return None


def _ensure_extension(filename: str, content_type: Optional[str]) -> str:
    if "." in filename.strip("."):
        return filename

    if not content_type:
        return filename

    ct = content_type.split(";", 1)[0].strip().lower()
    ext = mimetypes.guess_extension(ct) or ""
    if ext == ".jpe":
        ext = ".jpg"
    return filename + ext if ext else filename


_LOCAL_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "localhost6",
    "localhost6.localdomain6",
}


def _is_bad_ip(ip_str: str) -> bool:
    """
    Returns True if IP is not publicly routable / is unsafe to fetch.
    """
    ip = ip_address(ip_str)

    return any(
        [
            ip.is_private,       # RFC1918, etc
            ip.is_loopback,      # 127.0.0.0/8, ::1
            ip.is_link_local,    # 169.254.0.0/16, fe80::/10
            ip.is_multicast,     # 224.0.0.0/4, ff00::/8
            ip.is_unspecified,   # 0.0.0.0, ::
            ip.is_reserved,      # reserved ranges
        ]
    )


async def _resolves_to_bad_ip(hostname: str) -> bool:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            hostname,
            None,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except Exception:
        return True

    for family, _, _, _, sockaddr in infos:
        if family == socket.AF_INET:
            ip_str = sockaddr[0]
        elif family == socket.AF_INET6:
            ip_str = sockaddr[0]
        else:
            continue

        if _is_bad_ip(ip_str):
            return True

    return False


async def _validate_public_http_url(url: str) -> None:
    parsed = urlparse(url)

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"Only http/https URLs are allowed. Got scheme={scheme!r}")

    if parsed.username or parsed.password:
        raise ValueError("URLs with credentials (userinfo) are not allowed.")

    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("URL must include a hostname.")

    if host in _LOCAL_HOSTNAMES:
        raise ValueError(f"Localhost URL is not allowed: {host}")

    try:
        ip_address(host)
        if _is_bad_ip(host):
            raise ValueError(f"Private/loopback/link-local IP is not allowed: {host}")
    except ValueError:
        # Not an IP literal - hostname; resolve and check
        if await _resolves_to_bad_ip(host):
            raise ValueError(f"Hostname resolves to a private/unsafe IP: {host}")


async def _safe_fetch_follow_redirects(
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout: float = 120.0,
    max_redirects: int = 5,
) -> httpx.Response:
    current = url
    for _ in range(max_redirects + 1):
        await _validate_public_http_url(current)

        resp = await client.get(current, timeout=timeout, follow_redirects=False)

        # Handle redirects ourselves so we can validate Location
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("location")
            if not loc:
                raise ValueError(f"Redirect without Location header from {current}")

            next_url = urljoin(current, loc)
            current = next_url
            continue

        resp.raise_for_status()
        return resp

    raise ValueError(f"Too many redirects while fetching: {url}")


async def _url_to_uploadfile(
    file_obj: FileObj,
    http_client: httpx.AsyncClient,
) -> UploadFile:
    resp = await _safe_fetch_follow_redirects(
        http_client,
        str(file_obj.url),
        timeout=120.0,
        max_redirects=5,
    )

    content = resp.content
    cd = resp.headers.get("content-disposition")
    ct = resp.headers.get("content-type")

    filename = (
        _filename_from_content_disposition(cd)
        or _filename_from_url(str(resp.url))
        or _random_name_8()
    )

    filename = _sanitize_filename(filename) or _random_name_8()
    filename = _ensure_extension(filename, ct)

    return UploadFile(filename=filename, file=io.BytesIO(content))


async def save_files_tool(
    config: SaveFilesToolConfig,
    ctx: Context,
    app_state: AppState,
    user: UserORM,
    notify: Notify | None = None,
) -> FileMetas | Exception:
    """
    TOOL: save_files_tool
    ====================

    What this tool does
    -------------------
    - Takes a list of PUBLIC URLs (http/https).
    - Downloads each URL safely (SSRF protections + redirect validation).
    - Infers the filename automatically (no filename needed in input).
    - Uploads the downloaded bytes to the user's S3 space using UserFiles.upload_files().
    - Returns a list of FileMeta objects for the uploaded files.

    IMPORTANT INPUT RULES
    -------------------------------
    - Input must be a dictionary or json object: {"files": [{"url": "..."} , ...]}
    - Only PUBLIC http/https URLs are allowed.
    - These are REJECTED:
        * non-http(s) URLs (file:, ftp:, data:, etc)
        * localhost URLs (localhost, 127.0.0.1, ::1)
        * private network IPs (10.x, 172.16-31.x, 192.168.x, etc)
        * link-local (169.254.x.x, fe80::/10), multicast, reserved, etc
        * URLs with credentials (user:pass@host)
        * URLs that redirect to any of the blocked destinations above

    Example config 
    -----------------------------------
    {
      "files": [
        {"url": "https://example.com/report.pdf"},
        {"url": "https://raw.githubusercontent.com/org/repo/main/image.png"}
      ]
    }

    Output
    ------
    - On success: FileMetas(file_metas=[...]) where each item is a FileMeta.
    - On unexpected error: returns the Exception object (and logs it).

    Notes
    -----
    - Filename is inferred from headers/URL. If not found, random 8-char name is used.
    - If the inferred filename has no extension, we attempt to add one from Content-Type.
    """

    try:
        aws_s3 = get_public_s3(object_prefix="save-to-s3-tool")
        user_files = UserFiles(user=user, s3=aws_s3)

        files: List[UploadFile] = []

        async with httpx.AsyncClient() as client:
            for idx, file_obj in enumerate(config.files):
                if notify:
                    try:
                        notify(ctx,f"save_files_tool:downloading index={idx} url={file_obj.url}")
                    except Exception:
                        logger.bind(ctx=ctx).warning(
                            "Failed to send download notification for file index {}", idx
                        )

                upload_file = await _url_to_uploadfile(file_obj, client)
                files.append(upload_file)

        file_metas: List[FileMeta] = await user_files.upload_files(ctx=ctx, files=files)

        if notify:
            try:
                notify(ctx,f"save_files_tool:completed={idx} count={len(file_metas)}")
            except Exception:
                logger.bind(ctx=ctx).warning("Failed to send completion notification.")

        return FileMetas(file_metas=file_metas)

    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Error in save_files_tool: {str(e)}")
        return e