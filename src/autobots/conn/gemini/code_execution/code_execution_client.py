import mimetypes
from typing import Any

from google.genai import types
from google.genai.types import FileData, Part
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.conn.gemini.code_execution.code_execution_models import (
    GeminiCodeExecutionPreparedInputFile,
    GeminiCodeExecutionReq,
)
from src.autobots.conn.gemini.gemini_client import GeminiClient
from src.autobots.data_model.context import Context
from src.autobots.files.file_uploader import FileUploader
from src.autobots.user.user_orm_model import UserORM


class GeminiCodeExecutionClient:
    def __init__(self, gemini_client: GeminiClient):
        self.gemini_client = gemini_client

    @staticmethod
    def _guess_mime_type(item: dict[str, Any], file_uri: str | None) -> str:
        mime_type = item.get("mime_type")
        if mime_type:
            return str(mime_type)

        candidates = [
            item.get("display_name"),
            item.get("url"),
            file_uri,
        ]
        for candidate in candidates:
            if not candidate:
                continue
            guessed, _ = mimetypes.guess_type(str(candidate))
            if guessed:
                return guessed
        return "application/octet-stream"

    async def _prepare_input_files(
        self,
        *,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase | None,
        files: list[dict[str, Any]],
    ) -> tuple[list[Part], list[GeminiCodeExecutionPreparedInputFile]]:
        parts: list[Part] = []
        prepared: list[GeminiCodeExecutionPreparedInputFile] = []
        if not files:
            return parts, prepared

        url_items = [item for item in files if item.get("url")]
        uploaded = {}
        if url_items:
            uploaded = await FileUploader.upload_urls_to_gemini(
                ctx=ctx,
                user=user,
                db=db,
                urls=[item["url"] for item in url_items],
                gemini_client=self.gemini_client,
            )

        for item in files:
            if item.get("gemini_file_name"):
                file_obj = await self.gemini_client.get_file(
                    ctx=ctx,
                    gemini_filename=item["gemini_file_name"],
                )
            else:
                file_obj = uploaded.get(item.get("url"))

            if file_obj is None:
                continue

            file_uri = getattr(file_obj, "uri", None)
            gemini_file_name = getattr(file_obj, "name", None) or item.get("gemini_file_name")
            if not gemini_file_name:
                continue

            mime_type = self._guess_mime_type(item, file_uri)
            parts.append(
                Part(
                    file_data=FileData(
                        file_uri=file_uri,
                        mime_type=mime_type,
                    )
                )
            )
            prepared.append(
                GeminiCodeExecutionPreparedInputFile(
                    source_url=item.get("url"),
                    gemini_file_name=str(gemini_file_name),
                    file_uri=file_uri,
                    display_name=item.get("display_name"),
                    mime_type=mime_type,
                )
            )
        return parts, prepared

    async def execute(
        self,
        *,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase | None,
        req: GeminiCodeExecutionReq,
    ) -> tuple[Any, list[GeminiCodeExecutionPreparedInputFile]]:
        files = [
            item.model_dump(exclude_none=True) if hasattr(item, "model_dump") else dict(item)
            for item in req.files
        ]
        parts, prepared_input_files = await self._prepare_input_files(
            ctx=ctx,
            user=user,
            db=db,
            files=files,
        )
        parts.append(Part(text=req.task))

        response = await self.gemini_client.generate_content(
            model=req.model,
            contents=[types.Content(role="user", parts=parts)],
            config=types.GenerateContentConfig(
                tools=[types.Tool(code_execution=types.ToolCodeExecution())]
            ),
        )
        return response, prepared_input_files
