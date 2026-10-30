from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.autobots.conn.gemini.chat_model_gemini import GeminiChatConfig
from src.autobots.conn.gemini.gemini_chat import GeminiChatRuntime
from src.autobots.files.file_mime_types import (
    guess_extension_from_mime_type,
    resolve_file_mime_type,
)
from src.autobots.files.file_uploader import FileUploader


class FakeGeminiClient:
    def __init__(self, file_obj=None):
        self.file_obj = file_obj
        self.upload_calls = []

    async def get_file(self, **_kwargs):
        if self.file_obj is None:
            raise RuntimeError("file not found")
        return self.file_obj

    async def upload_file(self, **kwargs):
        self.upload_calls.append(kwargs)
        return SimpleNamespace(
            name="files/uploaded",
            uri="https://generativelanguage.googleapis.com/v1beta/files/uploaded",
            mime_type=kwargs.get("mime_type"),
            display_name=kwargs.get("display_name"),
            expiration_time=datetime.now(timezone.utc) + timedelta(hours=48),
        )


def _part_mime_type(part: dict) -> str | None:
    file_data = part.get("file_data") or part.get("fileData") or {}
    return file_data.get("mime_type") or file_data.get("mimeType")


def _part_file_uri(part: dict) -> str | None:
    file_data = part.get("file_data") or part.get("fileData") or {}
    return file_data.get("file_uri") or file_data.get("fileUri")


def _make_file_message(file_uri: str, mime_type: str = "application/pdf") -> dict:
    return {
        "role": "user",
        "parts": [
            {
                "file_data": {
                    "file_uri": file_uri,
                    "mime_type": mime_type,
                }
            }
        ],
    }


def test_resolve_file_mime_type_prefers_specific_guess_over_generic_binary():
    resolved = resolve_file_mime_type(
        direct_candidates=["application/octet-stream"],
        source_url="https://example.com/files/report.pdf",
    )
    assert resolved == "application/pdf"


def test_guess_extension_from_mime_type_pdf():
    assert guess_extension_from_mime_type("application/pdf") == ".pdf"


@pytest.mark.asyncio
async def test_upload_files_to_gemini_passes_resolved_mime_type(tmp_path):
    local_file = tmp_path / "report.pdf"
    local_file.write_bytes(b"%PDF-1.7\n")

    gemini_client = FakeGeminiClient()
    uploaded = await FileUploader._upload_files_to_gemini(
        ctx=SimpleNamespace(),
        user=None,
        db=None,
        file_urls_to_paths={"https://example.com/files/report.pdf": str(local_file)},
        gemini_client=gemini_client,
    )

    assert gemini_client.upload_calls
    call = gemini_client.upload_calls[0]
    assert call["mime_type"] == "application/pdf"
    assert call["display_name"] == "report.pdf"
    assert uploaded["https://example.com/files/report.pdf"].mime_type == "application/pdf"


@pytest.mark.asyncio
async def test_upload_files_to_gemini_uses_text_fallback_for_unknown_text_file(tmp_path):
    local_file = tmp_path / "download"
    local_file.write_text("hello from fallback text file\nline 2\n", encoding="utf-8")

    gemini_client = FakeGeminiClient()
    uploaded = await FileUploader._upload_files_to_gemini(
        ctx=SimpleNamespace(),
        user=None,
        db=None,
        file_urls_to_paths={"https://example.com/files/download": str(local_file)},
        gemini_client=gemini_client,
    )

    assert gemini_client.upload_calls
    call = gemini_client.upload_calls[0]
    assert call["mime_type"] == "text/plain"
    assert call["display_name"] == "download.txt"
    assert uploaded["https://example.com/files/download"].mime_type == "text/plain"


@pytest.mark.asyncio
async def test_file_parts_from_urls_corrects_manifest_mime_from_gemini_file():
    source_url = "https://example.com/files/report.pdf"
    gemini_client = FakeGeminiClient(
        file_obj=SimpleNamespace(
            uri="https://generativelanguage.googleapis.com/v1beta/files/report",
            name="files/report",
            mime_type="application/pdf",
            display_name="report.pdf",
            expiration_time=datetime.now(timezone.utc) + timedelta(hours=48),
        )
    )
    runtime = GeminiChatRuntime(user=SimpleNamespace(), db=None, gemini_client=gemini_client)
    config = GeminiChatConfig(
        uploaded_files_manifest=[
            {
                "source_url": source_url,
                "gemini_file_name": "files/report",
                "file_uri": "https://generativelanguage.googleapis.com/v1beta/files/report",
                "file_name": "report.pdf",
                "mime_type": "application/octet-stream",
            }
        ]
    )

    parts = await runtime._file_parts_from_urls(
        ctx=SimpleNamespace(),
        config=config,
        urls=[source_url],
    )

    assert len(parts) == 1
    assert _part_mime_type(parts[0]) == "application/pdf"
    assert config.uploaded_files_manifest[0]["mime_type"] == "application/pdf"
    assert config.uploaded_files_manifest[0]["last_checked_at"] is not None


@pytest.mark.asyncio
async def test_preflight_refresh_reuploads_due_manifest_and_rewrites_visible_messages(monkeypatch):
    source_url = "https://example.com/files/report.pdf"
    old_uri = "https://generativelanguage.googleapis.com/v1beta/files/old"
    new_uri = "https://generativelanguage.googleapis.com/v1beta/files/new"
    old_time = datetime.now(timezone.utc) - timedelta(hours=48)

    runtime = GeminiChatRuntime(user=SimpleNamespace(), db=None, gemini_client=SimpleNamespace())
    file_message = _make_file_message(old_uri)
    config = GeminiChatConfig(
        messages=[file_message],
        pinned_messages=[file_message],
        turn_history=[{"turn": 1, "messages": [file_message]}],
        uploaded_files_manifest=[
            {
                "source_url": source_url,
                "gemini_file_name": "files/old",
                "file_uri": old_uri,
                "file_name": "report.pdf",
                "mime_type": "application/pdf",
                "uploaded_at": old_time.isoformat().replace("+00:00", "Z"),
                "refresh_after_at": old_time.isoformat().replace("+00:00", "Z"),
                "first_uploaded_turn": 1,
                "last_used_turn": 1,
            }
        ],
    )

    async def fake_upload_urls_to_gemini(**_kwargs):
        return {
            source_url: SimpleNamespace(
                name="files/new",
                uri=new_uri,
                mime_type="application/pdf",
                display_name="report.pdf",
                expiration_time=datetime.now(timezone.utc) + timedelta(hours=48),
            )
        }

    monkeypatch.setattr(FileUploader, "upload_urls_to_gemini", fake_upload_urls_to_gemini)

    await runtime._preflight_refresh_uploaded_files(
        ctx=SimpleNamespace(),
        config=config,
        current_turn_urls=[],
    )

    manifest_entry = config.uploaded_files_manifest[0]
    assert manifest_entry["gemini_file_name"] == "files/new"
    assert manifest_entry["file_uri"] == new_uri
    assert manifest_entry["mime_type"] == "application/pdf"
    assert manifest_entry["uploaded_at"] is not None
    assert manifest_entry["refresh_after_at"] is not None
    assert manifest_entry["last_checked_at"] is not None

    assert _part_file_uri(config.messages[0]["parts"][0]) == new_uri
    assert _part_file_uri(config.pinned_messages[0]["parts"][0]) == new_uri
    assert _part_file_uri(config.turn_history[0]["messages"][0]["parts"][0]) == new_uri


@pytest.mark.asyncio
async def test_file_parts_from_urls_raises_when_uploaded_file_mime_is_still_unknown(monkeypatch):
    source_url = "https://example.com/files/download"
    runtime = GeminiChatRuntime(user=SimpleNamespace(), db=None, gemini_client=SimpleNamespace())
    config = GeminiChatConfig()

    async def fake_upload_urls_to_gemini(**_kwargs):
        return {
            source_url: SimpleNamespace(
                uri="https://generativelanguage.googleapis.com/v1beta/files/download",
                name="files/download",
                mime_type="application/octet-stream",
                display_name="download",
            )
        }

    monkeypatch.setattr(FileUploader, "upload_urls_to_gemini", fake_upload_urls_to_gemini)

    with pytest.raises(HTTPException) as exc_info:
        await runtime._file_parts_from_urls(
            ctx=SimpleNamespace(),
            config=config,
            urls=[source_url],
        )

    assert exc_info.value.status_code == 400
    assert "supported MIME type" in str(exc_info.value.detail)
