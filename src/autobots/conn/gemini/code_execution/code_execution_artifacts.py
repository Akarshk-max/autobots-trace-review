import base64
import mimetypes
from io import BytesIO
from typing import Any

from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.conn.gemini.code_execution.code_execution_models import (
    GeminiCodeExecutionArtifact,
)
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.user.user_orm_model import UserORM


class GeminiCodeExecutionArtifacts:
    @staticmethod
    def _extension_from_mime(mime_type: str | None) -> str:
        if not mime_type:
            return ".bin"
        ext = mimetypes.guess_extension(mime_type)
        return ext or ".bin"

    @classmethod
    def _extract_inline_data(cls, part: Any) -> tuple[bytes | None, str | None, str | None]:
        inline_data = getattr(part, "inline_data", None) or getattr(part, "inlineData", None)
        if inline_data is None and isinstance(part, dict):
            inline_data = part.get("inline_data") or part.get("inlineData")

        if inline_data is None:
            return None, None, None

        data = None
        mime_type = None
        display_name = None

        if isinstance(inline_data, dict):
            data = inline_data.get("data")
            mime_type = inline_data.get("mime_type") or inline_data.get("mimeType")
            display_name = inline_data.get("display_name") or inline_data.get("displayName")
        else:
            data = getattr(inline_data, "data", None)
            mime_type = getattr(inline_data, "mime_type", None) or getattr(inline_data, "mimeType", None)
            display_name = getattr(inline_data, "display_name", None) or getattr(inline_data, "displayName", None)

        if data is None:
            return None, mime_type, display_name

        if isinstance(data, bytes):
            return data, mime_type, display_name

        if isinstance(data, str):
            try:
                return base64.b64decode(data), mime_type, display_name
            except Exception:
                return data.encode("utf-8"), mime_type, display_name

        return None, mime_type, display_name

    @classmethod
    async def persist_inline_artifacts(
        cls,
        *,
        ctx: Context,
        user: UserORM,
        parts: list[Any],
    ) -> list[GeminiCodeExecutionArtifact]:
        user_files = UserFiles(user=user, s3=get_public_s3())
        artifacts: list[GeminiCodeExecutionArtifact] = []

        for idx, part in enumerate(parts):
            file_bytes, mime_type, display_name = cls._extract_inline_data(part)
            if not file_bytes:
                continue

            extension = cls._extension_from_mime(mime_type)
            filename = display_name or f"gemini_code_exec_{gen_uuid()}{extension}"
            uploaded = await user_files.upload_binary(
                ctx=ctx,
                file_obj=BytesIO(file_bytes),
                filename=filename,
            )
            url = str(uploaded[0].url) if uploaded and uploaded[0].url else None
            artifacts.append(
                GeminiCodeExecutionArtifact(
                    file_name=filename,
                    mime_type=mime_type,
                    url=url,
                    source_part_index=idx,
                )
            )
        return artifacts

    @staticmethod
    def artifacts_to_datablock_urls(
        artifacts: list[GeminiCodeExecutionArtifact | dict[str, Any]],
    ) -> list[str]:
        urls: list[str] = []
        for item in artifacts:
            if hasattr(item, "model_dump"):
                item = item.model_dump(exclude_none=True)
            if isinstance(item, dict):
                url = item.get("url")
                if url:
                    urls.append(str(url))
        deduped: list[str] = []
        seen: set[str] = set()
        for url in urls:
            if url not in seen:
                seen.add(url)
                deduped.append(url)
        return deduped
