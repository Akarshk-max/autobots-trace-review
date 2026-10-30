# src/autobots/ingestion/media_utils.py

import base64
import mimetypes
import tempfile
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from google.genai import types
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.conn.gemini.gemini import GeminiClient
from src.autobots.conn.gemini.gemini_model import ChatReqGemini
from src.autobots.data_model.context import Context
from src.autobots.ingestion.exceptions import RetryableIngestionError
from src.autobots.ingestion.ingestion_llm_connector import IngestionLLMConnector
from src.autobots.user.user_orm_model import UserORM

# ---------------------------------------------------------------------------
# MIME-TYPE BOOTSTRAP
# ---------------------------------------------------------------------------
# Gemini File API uploads can fail in slim containers where the OS/Python
# mimetype database is incomplete. The google-genai SDK relies on mimetype
# inference from the local environment. If it cannot infer a type, it raises:
#   "Unknown mime type ... please set the `mime_type` argument"
#
# We intentionally do NOT modify the Gemini client wrapper. Instead, we
# "teach" Python's mimetypes module about the media formats we support so the
# SDK can infer them from the temp file suffix.
#
# Scope: only media formats that go through Gemini in this service.
# (Documents are routed to Unstructured, which does not depend on this.)
#
# Safe: mimetypes.add_type is idempotent and only affects this process.

# Images (routed to Gemini as inline bytes today; these entries are harmless and
# help if/when we ever use Gemini File API uploads for images).
mimetypes.add_type("image/png", ".png")
mimetypes.add_type("image/jpeg", ".jpg")
mimetypes.add_type("image/jpeg", ".jpeg")
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("image/heic", ".heic")
mimetypes.add_type("image/heif", ".heif")

# Audio (Gemini File API uploads)
mimetypes.add_type("audio/mpeg", ".mp3")
mimetypes.add_type("audio/wav", ".wav")
mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("audio/ogg", ".ogg")
mimetypes.add_type("audio/ogg", ".oga")
mimetypes.add_type("audio/flac", ".flac")
mimetypes.add_type("audio/aac", ".aac")
mimetypes.add_type("audio/opus", ".opus")

# Video (Gemini File API uploads)
mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/x-m4v", ".m4v")
mimetypes.add_type("video/quicktime", ".mov")
mimetypes.add_type("video/x-msvideo", ".avi")
mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("video/3gpp", ".3gp")
mimetypes.add_type("video/h265", ".hevc")
mimetypes.add_type("video/h265", ".h265")

# --- HELPER FUNCTIONS ---

def _resolve_mime_type(
    *,
    filename: str | None = None,
    source_url: str | None = None,
    file_path: Path | None = None,
    default: str | None = None,
 ) -> str | None:
    candidates = [
        filename,
        Path(urlparse(source_url).path).name if source_url else None,
        file_path.name if file_path is not None else None,
        str(file_path) if file_path is not None else None,
    ]
    for candidate in candidates:
        if not candidate:
            continue
        guessed, _ = mimetypes.guess_type(str(candidate))
        if guessed:
            return guessed
    return default


def _extract_text_from_gemini_response(response: Any) -> str:
    if response is None:
        return ""

    text_value = getattr(response, "text", None)
    if isinstance(text_value, str) and text_value.strip():
        return text_value.strip()

    collected: list[str] = []
    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            part_text = getattr(part, "text", None)
            if isinstance(part_text, str) and part_text.strip():
                collected.append(part_text.strip())

    return "\n".join(collected).strip()



async def _upload_file_to_gemini(
    ctx: Context, gemini_client: GeminiClient, file_path: Path, display_name: str
) -> Any | None:  # Use Any for the type hint
    try:
        mime_type = _resolve_mime_type(filename=display_name, file_path=file_path)
        # --- FIX: Directly await the async method instead of using asyncio.to_thread ---
        file_obj = await gemini_client.upload_file(
            ctx=ctx,
            path=str(file_path),
            mime_type=mime_type,
            display_name=display_name,
        )
        return file_obj
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Failed to upload file '{display_name}' to Gemini: {e}")
        return None


async def _delete_gemini_file(ctx: Context, gemini_client: GeminiClient, file_name: str):
    """
    Deletes a file from the Gemini File API.
    """
    try:
        # --- FIX: Directly await the async method instead of using asyncio.to_thread ---
        await gemini_client.delete_file(ctx=ctx, gemini_filename=file_name)
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Could not delete Gemini file '{file_name}': {e}")


async def _download_temp_file(url: str, max_size_bytes: int = 200 * 1024 * 1024) -> Path | None:
    """Downloads a file from a URL to a temporary local path."""
    temp_file_path: Path | None = None
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
            # Handle URLs with query parameters to get the suffix
            path_without_query = urlparse(url).path
            suffix = Path(path_without_query).suffix

            # FIX: Gracefully fall back to GET if HEAD request fails
            try:
                head_response = await client.head(url, timeout=10)
                head_response.raise_for_status()
                content_length = head_response.headers.get("content-length")
                if content_length and int(content_length) > max_size_bytes:
                    logger.error(
                        f"File at {url} is too large via HEAD: {content_length} bytes (max: {max_size_bytes})"
                    )
                    return None
            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                logger.warning(
                    f"HEAD request failed for {url}: {e}. Proceeding with streaming download and size check."
                )

            exceeded_size = False
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tfile:
                    temp_file_path = Path(tfile.name)
                    downloaded_size = 0
                    async for chunk in response.aiter_bytes():
                        downloaded_size += len(chunk)
                        if downloaded_size > max_size_bytes:
                            logger.error(
                                f"File download from {url} exceeded max size of {max_size_bytes} bytes."
                            )
                            exceeded_size = True
                            break
                        tfile.write(chunk)

            if exceeded_size and temp_file_path:
                temp_file_path.unlink(missing_ok=True)
                return None
            return temp_file_path
    except Exception as e:
        # Ensure temp file is cleaned up on any exception during download/streaming.
        if temp_file_path:
            temp_file_path.unlink(missing_ok=True)
        logger.error(f"Failed to download temporary file from {url}: {e}")
        return None


# --- MAIN FUNCTION ---


async def summarize_media_content(
    media_type: Literal["image", "audio", "video"],
    user: UserORM,
    db: AsyncIOMotorDatabase,
    ctx: Context,
    content_b64: str = None,
    content_url: str = None,
    original_filename: str = "media_file",
) -> str:
    """
    Gets a text description for any media type using the IngestionLLMConnector.
    """
    temp_file_path: Path | None = None
    gemini_file: Any | None = None
    gemini_client = await IngestionLLMConnector.get_gemini_client(ctx=ctx, user=user, db=db)
    try:
        parts = []
        prompt = ""
        model_name = "gemini-2.5-flash"

        if media_type == "image":
            if not content_b64:
                raise ValueError("Image summarization requires base64 content.")
            prompt = "Provide a detailed, factual description of this image. Focus on text, data, and charts. This will be used for a retrieval system."
            image_mime_type = _resolve_mime_type(
                filename=original_filename,
                source_url=content_url,
                default="image/jpeg",
            )
            # --- FIX: Construct proper Part and Blob objects instead of raw dictionaries ---
            parts.append(types.Part(text=prompt))
            parts.append(
                types.Part(
                    inline_data=types.Blob(
                        mime_type=image_mime_type,
                        data=base64.b64decode(content_b64),
                    )
                )
            )

        elif media_type in ["audio", "video"]:
            if not content_url:
                raise ValueError("Audio/Video transcription requires a content URL.")

            temp_file_path = await _download_temp_file(content_url)  # Uses new size guard
            if not temp_file_path:
                raise RetryableIngestionError(
                    f"Failed to download media content from {original_filename} for summarization."
                )

            logger.info(f"Uploading {media_type} file to Gemini for processing...")
            gemini_file = await _upload_file_to_gemini(
                ctx=ctx,
                gemini_client=gemini_client,
                file_path=temp_file_path,
                display_name=original_filename,
            )
            if not gemini_file:
                raise RuntimeError("File upload to Gemini failed.")
            logger.success(f"Successfully uploaded file to Gemini: {gemini_file.name}")

            model_name = "gemini-2.5-flash"  # We may consider gemini-2.5-pro as well
            prompt = "Provide a full, detailed transcript of the audio or video content. If there is no speech, describe the sounds or visual events."

            # --- FIX IS HERE ---
            # Construct proper Part objects instead of appending a dictionary and the raw file object
            parts.append(types.Part(text=prompt))
            parts.append(
                types.Part(
                    file_data=types.FileData(
                        mime_type=gemini_file.mime_type, file_uri=gemini_file.uri
                    )
                )
            )
            # --- END FIX ---

        else:
            raise ValueError(f"Unsupported media type for summarization: {media_type}")

        request = ChatReqGemini(
            model=model_name, contents=[types.Content(role="user", parts=parts)]
        )

        response_message = await gemini_client.generate(ctx=ctx, user=user, chat_req=request, db=db)

        transcription = _extract_text_from_gemini_response(response_message)
        if transcription:
            return transcription  # Success path

        # If we reach here, Gemini returned no text. This is a permanent failure for this file.
        raise ValueError("Gemini response did not contain a usable text part.")

    except ValueError as e:
        # Re-raise permanent failures so the worker doesn't retry them.
        raise e
    except Exception as e:
        logger.error(f"Failed to process media '{original_filename}' with Gemini: {e}")
        # Re-raise as a retryable error, assuming most failures (network, etc.) are transient.
        raise RetryableIngestionError(f"Media processing failed for '{original_filename}'") from e
    finally:
        # Clean up the temporary local file and the uploaded Gemini file
        if temp_file_path and temp_file_path.exists():
            temp_file_path.unlink()
        if gemini_file:
            await _delete_gemini_file(
                ctx=ctx, gemini_client=gemini_client, file_name=gemini_file.name
            )
            logger.info(f"Cleaned up Gemini file: {gemini_file.name}")


async def pre_parse_document_with_gemini(
    user: UserORM,
    db: AsyncIOMotorDatabase,
    ctx: Context,
    content_url: str,
    original_filename: str = "document.pdf",
) -> str:
    """
    Uses Gemini File API to perform OCR and text extraction on a full document.
    Returns a single string of the document's text content.
    """
    logger.bind(ctx=ctx).info(f"Starting Gemini pre-parsing for document: {original_filename}")
    temp_file_path: Path | None = None
    MAX_GEMINI_TEXT_SIZE_BYTES = 50 * 1024 * 1024  # 50MB limit
    gemini_file: Any | None = None
    gemini_client = await IngestionLLMConnector.get_gemini_client(ctx=ctx, user=user, db=db)

    try:
        temp_file_path = await _download_temp_file(content_url)
        if not temp_file_path:
            raise RetryableIngestionError(
                f"Failed to download document from {content_url} for Gemini pre-parsing."
            )

        logger.bind(ctx=ctx).info(
            f"Uploading document '{original_filename}' to Gemini for pre-parsing..."
        )
        gemini_file = await _upload_file_to_gemini(
            ctx=ctx,
            gemini_client=gemini_client,
            file_path=temp_file_path,
            display_name=original_filename,
        )
        if not gemini_file:
            raise RuntimeError("File upload to Gemini failed during pre-parsing.")
        logger.bind(ctx=ctx).success(
            f"Successfully uploaded document to Gemini: {gemini_file.name}"
        )

        prompt = (
            "Extract all text from the provided document. If the document contains images, perform OCR to extract text from them. "
            "If it contains tables, preserve their structure as best as possible in the text. "
            "Return only the full, concatenated text content of the document."
        )

        parts = [
            types.Part(text=prompt),
            types.Part(
                file_data=types.FileData(mime_type=gemini_file.mime_type, file_uri=gemini_file.uri)
            ),
        ]

        request = ChatReqGemini(
            model="gemini-2.5-flash", contents=[types.Content(role="user", parts=parts)]
        )

        response_message = await gemini_client.generate(ctx=ctx, user=user, chat_req=request, db=db)

        extracted_text = _extract_text_from_gemini_response(response_message)
        if extracted_text:
            encoded_text = extracted_text.encode("utf-8")
            if len(encoded_text) > MAX_GEMINI_TEXT_SIZE_BYTES:
                logger.warning(
                    f"Gemini returned {len(encoded_text) / (1024 * 1024):.2f}MB of text, "
                    f"exceeding the {MAX_GEMINI_TEXT_SIZE_BYTES / (1024 * 1024):.2f}MB limit. "
                    "Truncating to prevent memory issues."
                )
                # FIX: Use byte-level truncation and safe decoding to prevent corrupting
                # multi-byte UTF-8 characters.
                truncated_bytes = encoded_text[:MAX_GEMINI_TEXT_SIZE_BYTES]
                extracted_text = truncated_bytes.decode("utf-8", errors="ignore")
            # --- END SAFETY GUARDS ---

            # === STEP 2: DIAGNOSTIC LOGGING: VALIDATE OCR OUTPUT ===
            # This now logs the actual data being passed downstream.
            logger.bind(ctx=ctx).info(
                f"[DIAGNOSTIC] Gemini OCR for '{original_filename}' completed. Length: {len(extracted_text)} chars."
            )
            # Log a snippet at DEBUG level for less verbose default logging
            logger.bind(ctx=ctx).debug(
                f"--- Gemini Text Snippet ---\n{extracted_text[:2000]}...\n-------------------------"
            )
            # ===============================================

            # --- STEP 3: PROCEED WITH THE SANITIZED TEXT ---
            if extracted_text:
                logger.bind(ctx=ctx).success(
                    f"Successfully pre-parsed document '{original_filename}' with Gemini."
                )
                return extracted_text

        # This will be raised if the initial response was empty or if the text was empty after stripping.
        raise RetryableIngestionError("Gemini pre-parsing returned no usable text content.")

    except Exception as e:
        logger.bind(ctx=ctx).exception(
            f"Failed to pre-parse document '{original_filename}' with Gemini: {e}"
        )
        raise  # Re-raise to allow the worker's retry logic to catch it
    finally:
        if temp_file_path and temp_file_path.exists():
            temp_file_path.unlink()
        if gemini_file:
            await _delete_gemini_file(
                ctx=ctx, gemini_client=gemini_client, file_name=gemini_file.name
            )
            logger.bind(ctx=ctx).info(
                f"Cleaned up Gemini file used for pre-parsing: {gemini_file.name}"
            )
