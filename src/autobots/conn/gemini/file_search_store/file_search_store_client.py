import asyncio
import os
import tempfile
from typing import Any

from google.genai import types

from src.autobots.conn.gemini.gemini_client import GeminiClient


class GeminiFileSearchStoreClient:
    def __init__(self, gemini_client: GeminiClient):
        self.gemini_client = gemini_client

    @property
    def aclient(self):
        return self.gemini_client.aclient

    async def create_store(
        self,
        *,
        display_name: str,
        embedding_model: str = "models/gemini-embedding-2",
    ) -> Any:
        return await self.aclient.file_search_stores.create(
            config={
                "display_name": display_name,
                "embedding_model": embedding_model,
            }
        )

    async def get_store(self, *, name: str) -> Any:
        return await self.aclient.file_search_stores.get(name=name)

    async def list_stores(self) -> list[Any]:
        pager = await self.aclient.file_search_stores.list()
        return [item async for item in pager]

    async def delete_store(
        self,
        *,
        name: str,
        force: bool = False,
    ) -> Any:
        return await self.aclient.file_search_stores.delete(
            name=name,
            config={"force": force},
        )

    async def upload_file_to_store(
        self,
        *,
        file_path: str,
        store_name: str,
        display_name: str | None = None,
        mime_type: str | None = None,
        custom_metadata: list[dict[str, Any]] | None = None,
    ) -> Any:
        config: dict[str, Any] = {}

        if display_name:
            config["display_name"] = display_name

        if mime_type:
            config["mime_type"] = mime_type

        if custom_metadata:
            config["custom_metadata"] = custom_metadata

        return await self.aclient.file_search_stores.upload_to_file_search_store(
            file=file_path,
            file_search_store_name=store_name,
            config=config or None,
        )

    async def import_existing_gemini_file(
        self,
        *,
        store_name: str,
        gemini_file_name: str,
        custom_metadata: list[dict[str, Any]] | None = None,
    ) -> Any:
        config: dict[str, Any] = {}

        if custom_metadata:
            config["custom_metadata"] = custom_metadata

        return await self.aclient.file_search_stores.import_file(
            file_search_store_name=store_name,
            file_name=gemini_file_name,
            config=config or None,
        )

    async def wait_for_operation(
        self,
        operation: Any,
        max_polls: int = 120,
        poll_interval_s: float = 3,
    ) -> Any:
        current = operation

        for attempt in range(max_polls):
            if getattr(current, "done", False):
                return current

            current = await self.aclient.operations.get(current)

            if getattr(current, "done", False):
                return current

            if attempt < max_polls - 1:
                await asyncio.sleep(poll_interval_s)

        raise TimeoutError(
            f"Gemini file-search operation did not complete "
            f"after {max_polls} polls"
        )

    async def delete_document(
        self,
        *,
        name: str,
        force: bool = False,
    ) -> Any:
        return await self.aclient.file_search_stores.documents.delete(
            name=name,
            config={"force": force},
        )

    async def query_store(
        self,
        *,
        model: str,
        query: str,
        store_name: str,
        metadata_filter: str | None = None,
    ) -> Any:
        retrieval_resource: dict[str, Any] = {
            "file_search_store_names": [store_name]
        }

        if metadata_filter:
            retrieval_resource["metadata_filter"] = metadata_filter

        return await self.gemini_client.generate_content(
            model=model,
            contents=query,
            config=types.GenerateContentConfig(
                tools=[
                    types.Tool(
                        file_search=types.FileSearch(
                            **retrieval_resource
                        )
                    )
                ]
            ),
        )

    async def upload_text_document(
        self,
        *,
        store_name: str,
        display_name: str,
        text: str,
        custom_metadata: list[dict[str, Any]] | None = None,
    ) -> Any:
        tmp_path = ""

        try:
            with tempfile.NamedTemporaryFile(
                delete=False,
                suffix=".txt",
                mode="w",
                encoding="utf-8",
            ) as tmp:
                tmp.write(text)
                tmp_path = tmp.name

            return await self.upload_file_to_store(
                file_path=tmp_path,
                store_name=store_name,
                display_name=display_name,
                mime_type="text/plain",
                custom_metadata=custom_metadata,
            )

        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass