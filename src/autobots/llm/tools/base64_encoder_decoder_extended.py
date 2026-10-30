import base64
import hashlib
import io
import textwrap
from typing import Dict, List, Literal, Optional

import httpx
import pandas as pd
from loguru import logger
from pydantic import BaseModel, Field, field_validator

from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

def filter_nsfw_content(s: str) -> str:
    """Hook for content filtering. Returns input unchanged if no filter is configured."""
    return s


Base64Variant = Literal["standard", "base64url"]
Preset = Literal["none", "github_contents_api"]


class Base64Input(BaseModel):
    """
    One item for encoding/decoding.

    Provide exactly ONE source:
      - text: plain text (encode) OR base64 string (decode)
      - bytes_b64: base64 that represents *raw bytes* (encode only, treated as bytes input)
      - url: download bytes (encode) OR download base64 text (decode)

    Notes
    -----
    - For encode: 'text' is encoded using options.charset.
    - For encode:  'url' is treated as binary bytes.
    - For decode: we interpret the *base64 string* from 'text' OR read base64 text from url.
    """

    name: str = "input"
    text: Optional[str] = None
    bytes_b64: Optional[str] = None
    url: Optional[str] = None

    @field_validator("text")
    @classmethod
    def _filter_text(cls, v: Optional[str]) -> Optional[str]:
        return filter_nsfw_content(v) if isinstance(v, str) else v

    @field_validator("bytes_b64")
    @classmethod
    def _strip_ws(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if isinstance(v, str) else v

    @field_validator("url")
    @classmethod
    def _strip_url(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if isinstance(v, str) else v


class Base64EncodeOptions(BaseModel):
    """
    Small, LLM-friendly encode options.
    """

    preset: Preset = "none"

    variant: Base64Variant = "standard"
    padding: bool = True
    line_wrap: int = 0  # 0=no wraps; 76=classic MIME wraps
    charset: str = "utf-8"

    include_sha256: bool = False
    include_lengths: bool = True

    # Convenience for API drop-in (GitHub uses "content")
    as_json_key: Literal["content", "encoded"] = "content"

    # When the base64 output exceeds this many chars, save the original raw
    # bytes to S3 and return a url instead of the full base64 in the result.
    # This prevents LLM context truncation when encoding large files.
    # Set to 0 to always keep the full base64 in the result.
    save_to_file_threshold: int = 0


class Base64DecodeOptions(BaseModel):
    """
    Small, LLM-friendly decode options.
    """

    variant: Base64Variant = "standard"
    padding: bool = True
    charset: str = "utf-8"

    include_sha256: bool = False
    include_lengths: bool = True

    save_to_file: bool = True  # When decoded bytes aren't valid text, save to S3 and return URL


class Base64EncodeRequest(BaseModel):
    """
    Description
    -----------
    Encodes one or many inputs into base64 and returns structured results.

    Inputs
    ------
    inputs : List[Base64Input]
        Each item must provide exactly one of:
          - text
          - bytes_b64
          - url

    options : Base64EncodeOptions
        Controls variant, padding, line wrapping, metadata, and presets.

    Notes
    -----
    - Never raises for expected failures: per-item errors return ok=False.
    """

    inputs: List[Base64Input] = Field(default_factory=list)
    options: Base64EncodeOptions = Field(default_factory=Base64EncodeOptions)


class Base64DecodeRequest(BaseModel):
    """
    Description
    -----------
    Decodes one or many base64 strings and returns decoded plain-text strings.

    Inputs
    ------
    inputs : List[Base64Input]
        Each item must provide exactly one of:
          - text (base64 string)
          - url (URL returning base64 string as text)

        (bytes_b64 is ignored for decode; use text instead.)

    options : Base64DecodeOptions
        Controls variant, padding, charset, and metadata.

    Notes
    -----
    - Never raises for expected failures: per-item errors return ok=False.
    """

    inputs: List[Base64Input] = Field(default_factory=list)
    options: Base64DecodeOptions = Field(default_factory=Base64DecodeOptions)


class Base64Result(BaseModel):
    name: str
    ok: bool = True

    # Encoder output
    base64: Optional[str] = None

    # Decoder output
    text: Optional[str] = None

    # File output (when binary content is saved to S3)
    url: Optional[str] = None

    # Metadata
    byte_length: Optional[int] = None
    char_length: Optional[int] = None
    sha256: Optional[str] = None

    # Error as data
    error: Optional[str] = None


class Base64Response(BaseModel):
    """
    results: per-item outcomes
    payloads: convenience list for API calls (e.g., [{"content": "<b64>"}])
    urls: collected file URLs for tool_factory auto-extraction
    """

    results: List[Base64Result]
    payloads: Optional[List[Dict[str, str]]] = None
    urls: List[str] = []


# ----------------- helpers (no raises to caller) -----------------


def _apply_encode_preset(opts: Base64EncodeOptions) -> Base64EncodeOptions:
    if opts.preset == "github_contents_api":
        # GitHub Contents API expects standard base64, padding, no wraps, key="content"
        opts = opts.model_copy(
            update={
                "variant": "standard",
                "padding": True,
                "line_wrap": 0,
                "charset": "utf-8",
                "as_json_key": "content",
            }
        )
    return opts


def _fix_padding(s: str) -> str:
    # Strip existing padding so we work with data chars only
    data = s.rstrip("=")
    remainder = len(data) % 4
    if remainder == 1:
        # 1 data char mod 4 is impossible in valid base64 — the last char is
        # a truncation artifact (e.g. LLM cutting off output).  Drop it so the
        # rest can still be decoded.
        data = data[:-1]
        remainder = 0
    return data + ("=" * ((-len(data)) % 4))


def _b64encode_bytes(data: bytes, opts: Base64EncodeOptions) -> str:
    if opts.variant == "base64url":
        out = base64.urlsafe_b64encode(data).decode("ascii")
    else:
        out = base64.b64encode(data).decode("ascii")

    if not opts.padding:
        out = out.rstrip("=")

    if opts.line_wrap and opts.line_wrap > 0:
        out = "\n".join(textwrap.wrap(out, width=opts.line_wrap))

    return out


def _b64decode_to_bytes(b64: str, variant: Base64Variant, padding: bool) -> bytes:
    s = (b64 or "").strip().replace("\n", "").replace("\r", "").replace(" ", "")
    if padding:
        s = _fix_padding(s)

    if variant == "base64url":
        return base64.urlsafe_b64decode(s.encode("ascii"))
    return base64.b64decode(s.encode("ascii"))


async def _download_url_bytes(url: str, timeout_s: float = 30.0) -> bytes | Exception:
    try:
        async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.content
    except Exception as e:
        return e


async def _download_url_text(url: str, timeout_s: float = 30.0) -> str | Exception:
    try:
        async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.text
    except Exception as e:
        return e


def _guess_extension(data: bytes) -> str:
    """Guess a file extension from magic bytes. Falls back to .bin."""
    signatures: list[tuple[bytes, str]] = [
        (b"%PDF", ".pdf"),
        (b"\x89PNG\r\n\x1a\n", ".png"),
        (b"\xff\xd8\xff", ".jpg"),
        (b"GIF87a", ".gif"),
        (b"GIF89a", ".gif"),
        (b"PK\x03\x04", ".zip"),
        (b"RIFF", ".webp"),  # could also be .wav/.avi — good enough
    ]
    for magic, ext in signatures:
        if data[: len(magic)] == magic:
            return ext

    # Check for text-based formats (no magic bytes)
    try:
        text = data[:4096].decode("utf-8")
        stripped = text.lstrip()
        if stripped.startswith(("{", "[")):
            return ".json"
        if stripped.startswith(("<?xml", "<html", "<!DOCTYPE", "<HTML", "<!doctype")):
            return ".html" if "html" in stripped[:100].lower() else ".xml"
        if stripped.startswith("<"):
            return ".xml"
        # Valid UTF-8 text without binary control chars → treat as .txt
        if not any(c < " " and c not in "\t\n\r" for c in text):
            return ".txt"
    except (UnicodeDecodeError, ValueError):
        pass

    return ".bin"


async def _upload_bytes_to_s3(data: bytes, name: str, ext_override: str | None = None) -> str:
    """Upload raw bytes to public S3 and return the HTTPS URL."""
    timestamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
    ext = ext_override if ext_override else _guess_extension(data)
    filename = f"base64_decoded_{name}_{timestamp}{ext}"
    s3 = get_public_s3(object_prefix="base64-decoder")
    url = await s3.put_file_obj(io.BytesIO(data), filename)
    return str(url)


def _one_source(item: Base64Input) -> tuple[bool, str]:
    provided = [
        item.text is not None,
        item.bytes_b64 is not None,
        item.url is not None,
    ]
    if sum(provided) != 1:
        return False, "Provide exactly one of: text, bytes_b64, url"
    return True, ""


# ----------------- encoder -----------------


async def base64_encoder_extended(
    config: Base64EncodeRequest,
    ctx: Context,
    app_state: AppState,
    notify: Notify | None = None,
) -> Base64Response | Exception:
    """
    Description
    -----------

    Purpose
    -------
    Encode text or downloaded binary content into a base64 string, with batch support and
    API-friendly outputs.

    This tool is intentionally designed for LLM workflows where you often need base64 for:
    - GitHub Contents API commits
    - Gmail attachments (PDFs/images as base64)
    - Any API that requires "content": "<base64>" fields

    This tool NEVER raises for expected failures.
    If something goes wrong for a specific input, that input returns `ok=false` and an `error`
    string. Other inputs in the same call still succeed.

    When to Use
    -----------
    Use this tool whenever you have:
    1) Plain text you want to base64 encode, or
    2) A URL to a binary file (PDF/image/zip/etc.) you want to base64 encode, or
    3) Multiple items to encode in one tool call (batch mode), or
    4) You want “GitHub-safe” encoding via a preset (no line wraps, standard base64).

    Input Shape
    -----------
    config : Base64EncodeRequest | dict

    A request has two top-level fields:

    1) inputs (required)
    A list of items to encode.

    2) options (optional)
    Controls how encoding is done (variant, padding, wrap) + metadata + presets.

    Inputs (required)
    -----------------
    `inputs` is a list of objects; each object must provide exactly ONE source:

    - text: string
        Plain text to encode (converted to bytes using `options.charset`)

    - url: string
        A URL to download. The response body (bytes) is encoded.

    - bytes_b64: string  (advanced)
        Base64 string that represents raw bytes.
        Use this only when another tool already produced binary bytes as base64 and you want
        to re-encode them with consistent options (variant/padding/wrap).

    Exactly ONE of {text, url, bytes_b64} must be present per input item.
    If multiple or none are provided, that item returns ok=false.

    Options (all optional)
    ----------------------
    options:
    - preset: "none" | "github_contents_api"
        Helper mode for GitHub Contents API.
        When set to "github_contents_api", the tool automatically uses:
        - variant="standard"
        - padding=true
        - line_wrap=0
        - charset="utf-8"
        - as_json_key="content"

    - variant: "standard" | "base64url"
        standard: regular base64 (+ and /)
        base64url: URL-safe base64 (- and _)

    - padding: true | false
        Whether to keep '=' padding characters.

    - line_wrap: integer
        0 means no line breaks (recommended for most APIs).
        76 is legacy MIME style wrapping.

    - charset: string
        Encoding used when converting input text to bytes (default: "utf-8").

    - include_sha256: true | false
        If true, includes sha256 hash of the original bytes.

    - include_lengths: true | false
        If true, includes byte_length and char_length.

    - as_json_key: "content" | "encoded"
        Key used inside `payloads` (GitHub typically expects "content").

    Example Call
    ------------
    {
    "inputs": [
        {"name": "readme", "text": "Hello world"},
        {"name": "pdf", "url": "https://.../new.pdf"}
    ],
    "options": {
        "preset": "github_contents_api",
        "include_sha256": true
    }
    }

    Output Shape
    ------------
    Returns Base64Response with:

    - results: list of per-item outputs (same order as inputs)
    Each result includes:
        { name, ok, base64, url?, byte_length?, char_length?, sha256?, error? }

    When the base64 output exceeds save_to_file_threshold (default 0 chars, i.e. always),
    the original raw bytes are saved to a public S3 URL returned in `url`.
    The `base64` field is truncated in this case to prevent LLM context overflow.
    Use the `url` to access the full original file directly.

    - payloads (optional convenience):
    A list aligned with successful items, each like:
        { "<as_json_key>": "<base64>" }
    This is meant to be pasted directly into API payloads.

    - urls: list of S3 URLs for files saved (for tool_factory auto-extraction)

    Guarantees
    ----------
    - No per-item exceptions are thrown; errors are returned as data.
    - Batch calls are partially successful: one failure does not cancel the whole request.
    """

    try:
        req = config
        req.options = _apply_encode_preset(req.options)

        results: list[Base64Result] = []
        payloads: list[dict[str, str]] = []
        collected_urls: list[str] = []

        for item in req.inputs:
            try:
                ok, msg = _one_source(item)
                if not ok:
                    results.append(Base64Result(name=item.name, ok=False, error=msg))
                    continue

                # Resolve raw bytes
                raw: bytes | None = None

                if item.text is not None:
                    raw = item.text.encode(req.options.charset)

                elif item.url is not None:
                    dl = await _download_url_bytes(item.url)
                    if isinstance(dl, Exception):
                        results.append(
                            Base64Result(
                                name=item.name, ok=False, error=f"url download failed: {dl}"
                            )
                        )
                        continue
                    raw = dl

                elif item.bytes_b64 is not None:
                    # bytes_b64 represents raw bytes; decode it (tolerant padding)
                    try:
                        raw = _b64decode_to_bytes(item.bytes_b64, variant="standard", padding=True)
                    except Exception as e:
                        results.append(
                            Base64Result(name=item.name, ok=False, error=f"invalid bytes_b64: {e}")
                        )
                        continue

                else:
                    results.append(
                        Base64Result(name=item.name, ok=False, error="No valid source found")
                    )
                    continue

                encoded = _b64encode_bytes(raw, req.options)

                res = Base64Result(name=item.name, ok=True, base64=encoded)

                if req.options.include_lengths:
                    res.byte_length = len(raw)
                    res.char_length = len(encoded)

                if req.options.include_sha256:
                    res.sha256 = hashlib.sha256(raw).hexdigest()

                # For large payloads, save the base64 string to S3 as a .txt
                # file so the LLM doesn't need to relay it (it would truncate).
                threshold = req.options.save_to_file_threshold
                if threshold >= 0 and len(encoded) > threshold:
                    try:
                        file_url = await _upload_bytes_to_s3(
                            encoded.encode("ascii"), f"{item.name}_b64", ext_override=".txt"
                        )
                        res.url = file_url
                        collected_urls.append(file_url)
                        # Truncate base64 in response so it fits in LLM context
                        res.base64 = (
                            encoded[:200]
                            + f"... [truncated, {len(encoded)} total chars] "
                            + f"Full base64 saved to: {file_url}"
                        )
                    except Exception as upload_err:
                        logger.bind(ctx=ctx).warning(
                            f"Failed to save encoded file to S3: {upload_err}"
                        )
                        # Keep the full base64 as fallback

                results.append(res)
                payloads.append({req.options.as_json_key: encoded})

            except Exception as e:
                # Never raise; return error as data
                results.append(Base64Result(name=item.name, ok=False, error=str(e)))

        return Base64Response(results=results, payloads=payloads or None, urls=collected_urls)

    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Error in base64_encoder: {str(e)}")
        return e


# ----------------- decoder -----------------


async def base64_decoder_extended(
    config: Base64DecodeRequest,
    ctx: Context,
    app_state: AppState,
    notify: Notify | None = None,
) -> Base64Response | Exception:
    """
    Description
    -----------

    Purpose
    -------
    Decode base64 strings into plain text (UTF-8 by default), with batch support.

    This decoder is intended for inspecting/validating base64 text returned by APIs, such as:
    - GitHub Contents API (file contents returned as base64)
    - Any service that returns base64-encoded strings representing text

    This tool NEVER raises for expected failures.
    If an input is invalid or cannot be decoded to text, that input returns `ok=false`
    and an `error` string.

    When to Use
    -----------
    Use this tool whenever you:
    - Have a base64 string and want the decoded text
    - Need to decode multiple base64 strings in one call
    - Need to decode base64url vs standard base64

    Input Shape
    -----------
    config : Base64DecodeRequest | dict

    A request has two top-level fields:

    1) inputs (required)
    A list of items to decode.

    2) options (optional)
    Controls how decoding is done (variant, padding) and how bytes convert to text (charset).

    Inputs (required)
    -----------------
    Each item in `inputs` must provide exactly ONE source:

    - text: string
        The base64 string to decode.

    - url: string
        A URL that returns a base64 string as TEXT.
        The tool downloads the response text and decodes it.

    Exactly ONE of {text, url} must be present per input item.
    If multiple or none are provided, that item returns ok=false.

    Options (all optional)
    ----------------------
    options:
    - variant: "standard" | "base64url"
        standard: regular base64 (+ and /)
        base64url: URL-safe base64 (- and _)

    - padding: true | false
        If true, missing '=' padding is tolerated and automatically repaired.
        If false, the input is treated as unpadded base64/base64url.

    - charset: string
        Charset used to convert decoded bytes into text (default: "utf-8").

    - include_sha256: true | false
        If true, includes sha256 hash of the decoded bytes.

    - include_lengths: true | false
        If true, includes byte_length (decoded bytes) and char_length (decoded text length).

    - save_to_file: true | false (default: true)
        If true and the decoded bytes are not valid text (e.g., PDF, image, zip),
        the bytes are saved to a public S3 bucket and the result includes a `url`
        field with the HTTPS URL to download the file.
        If false, non-text content returns ok=false with an error message.

    Example Call
    ------------
    {
    "inputs": [
        {"name": "readme", "text": "SGVsbG8gd29ybGQ="},
        {"name": "remote", "url": "https://example.com/base64.txt"}
    ],
    "options": {
        "variant": "standard",
        "charset": "utf-8"
    }
    }

    Output Shape
    ------------
    Returns Base64Response with:

    - results: list of per-item outputs (same order as inputs)
    Each result includes:
        { name, ok, text?, url?, byte_length?, char_length?, sha256?, error? }
      - text is set when decoded bytes are valid text
      - url is set when decoded bytes are binary and save_to_file=true

    - urls: list of public S3 URLs for binary files saved during decoding

    Notes / Limitations
    -------------------
    - By default (save_to_file=true), binary content is uploaded to S3 and a URL is returned.
    - Set save_to_file=false to get the old behavior where non-text content returns ok=false.

    Guarantees
    ----------
    - No per-item exceptions are thrown; errors are returned as data.
    - Batch calls are partially successful: one failure does not cancel the whole request.
    """
    try:
        req = config

        results: list[Base64Result] = []
        collected_urls: list[str] = []

        for item in req.inputs:
            try:
                # For decode we allow exactly one of: text, url
                if item.bytes_b64 is not None:
                    results.append(
                        Base64Result(
                            name=item.name,
                            ok=False,
                            error="bytes_b64 is not supported for decode; use text instead",
                        )
                    )
                    continue
                provided = [item.text is not None, item.url is not None]
                if sum(provided) != 1:
                    results.append(
                        Base64Result(
                            name=item.name,
                            ok=False,
                            error="Decode expects exactly one of: text (base64), url (base64 text)",
                        )
                    )
                    continue

                # Resolve base64 string
                b64s: str | None = None

                if item.text is not None:
                    b64s = item.text

                elif item.url is not None:
                    dl = await _download_url_text(item.url)
                    if isinstance(dl, Exception):
                        results.append(
                            Base64Result(
                                name=item.name, ok=False, error=f"url download failed: {dl}"
                            )
                        )
                        continue
                    b64s = dl

                else:
                    results.append(
                        Base64Result(name=item.name, ok=False, error="No valid source found")
                    )
                    continue

                # Decode to bytes
                try:
                    raw = _b64decode_to_bytes(
                        b64s, variant=req.options.variant, padding=req.options.padding
                    )
                except Exception as e:
                    results.append(
                        Base64Result(name=item.name, ok=False, error=f"invalid base64: {e}")
                    )
                    continue

                # Decode bytes -> text
                try:
                    decoded_text = raw.decode(req.options.charset)
                except Exception:
                    if req.options.save_to_file:
                        try:
                            file_url = await _upload_bytes_to_s3(raw, item.name)
                            res = Base64Result(
                                name=item.name,
                                ok=True,
                                url=file_url,
                                byte_length=len(raw),
                            )
                            if req.options.include_sha256:
                                res.sha256 = hashlib.sha256(raw).hexdigest()
                            results.append(res)
                            collected_urls.append(file_url)
                            continue
                        except Exception as upload_err:
                            results.append(
                                Base64Result(
                                    name=item.name,
                                    ok=False,
                                    error=f"Failed to save decoded bytes to file: {upload_err}",
                                    byte_length=len(raw),
                                )
                            )
                            continue
                    results.append(
                        Base64Result(
                            name=item.name,
                            ok=False,
                            error="Decoded bytes are not valid text for the given charset.",
                            byte_length=len(raw),
                        )
                    )
                    continue

                res = Base64Result(name=item.name, ok=True, text=decoded_text)

                if req.options.include_lengths:
                    res.byte_length = len(raw)
                    res.char_length = len(decoded_text)

                if req.options.include_sha256:
                    res.sha256 = hashlib.sha256(raw).hexdigest()

                results.append(res)

            except Exception as e:
                results.append(Base64Result(name=item.name, ok=False, error=str(e)))

        return Base64Response(results=results, urls=collected_urls)

    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Error in base64_decoder: {str(e)}")
        return e