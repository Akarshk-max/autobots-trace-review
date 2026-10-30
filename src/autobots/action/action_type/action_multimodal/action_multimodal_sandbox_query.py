# src/autobots/action/action_type/action_multimodal/action_multimodal_sandbox_query.py

from typing import List, Optional, Type

import httpx
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field, HttpUrl

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.abc.ActionABC import ActionABC
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState

class SandboxQueryConfig(BaseModel):
    query: Optional[str] = Field(
        default=None,
        description="A default question to ask. Will be overridden by chat input.",
        json_schema_extra={"control_type": "text_input", "title": "Default Query (Optional)"},
    )
    file_urls: Optional[List[HttpUrl]] = Field(
        default=None,
        description="Default file URLs. Will be overridden by chat attachments.",
        json_schema_extra={
            # There isn't a confirmed control for a list of strings.
            # Using "text_input" is the safest bet, but is not ideal for a list.
            # A text_area would be a better UX, but requires asking Ajay to add it.
            "control_type": "text_input",
            "title": "Default File URLs (Optional, single URL only)",
        },
    )


class ActionMultimodalSandboxQuery(
    ActionABC[SandboxQueryConfig, SandboxQueryConfig, SandboxQueryConfig, DataBlock, DataBlocks]
):
    type = ActionType.multimodal_sandbox_query

    @staticmethod
    def get_description() -> str:
        return (
            "Answers a question using one or more temporarily provided files (PDFs, "
            "images, etc.). Files are sent to a sandbox RAG service for on-the-fly "
            "processing and are not persisted long-term.\n\n"
            "Input (config):\n"
            "  {\n"
            '      "query": "What does this document say about X?",     # optional on config, can be supplied at runtime\n'
            '      "file_urls": ["https://example.com/doc1.pdf", ...],  # one or more file URLs\n'
            "  }\n\n"
            "  query: The natural-language question to ask about the provided files. "
            "         Runtime input (action input text) overrides the config value.\n\n"
            "  file_urls: A list of file URLs (images, PDFs, etc.) to be processed by "
            "             the sandbox service. Runtime attachments (action input URLs) "
            "             override the config value.\n\n"
            "Behavior:\n"
            "  - Validates that at least one file URL and a query are present (either in "
            "    the config or supplied at runtime).\n"
            "  - Sends a JSON payload containing the file URLs and query to the sandbox "
            "    ingestion/search endpoint using the caller's access token for auth.\n"
            "  - The sandbox service performs temporary ingestion and returns the "
            "    summarized/answer content. Files are not stored permanently by this action.\n\n"
            "Output:\n"
            "  A DataBlocks object containing one or more DataBlock entries representing "
            "  the sandbox service's answers or context. On error, an exception is returned."
        )

    @staticmethod
    def get_config_create_type() -> Type[SandboxQueryConfig]:
        return SandboxQueryConfig

    @staticmethod
    def get_config_update_type() -> Type[SandboxQueryConfig]:
        return SandboxQueryConfig

    @staticmethod
    def get_config_type() -> Type[SandboxQueryConfig]:
        return SandboxQueryConfig

    @staticmethod
    def get_input_type() -> Type[DataBlock]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[DataBlocks]:
        return DataBlocks

    def __init__(
        self,
        action_config: SandboxQueryConfig,
        user: UserORM | None = None,
        db: AsyncIOMotorDatabase | None = None,
    ):
        super().__init__(action_config=action_config, user=user, db=db)

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        settings = SettingsProvider.sget()

        # Prioritize data from the standard input, but fall back to the config for tool use
        urls = action_input.urls or self.action_config.file_urls
        query = action_input.text or self.action_config.query

        if not urls or not query:
            return ValueError("Sandbox Query requires at least one file URL and a question.")

        if not self.user.access_token:
            return ValueError("An access token is required for this action.")

        try:
            # The action now sends a simple JSON payload, no file downloads needed.
            payload = {
                "file_urls": [str(url) for url in urls],  # Ensure URLs are strings
                "query": query,
            }
            headers = {"Authorization": f"Bearer {self.user.access_token}"}

            sandbox_endpoint = (
                f"{settings.INGESTION_WORKER_URL}/api{settings.API_V1_INGESTION}/sandbox/search"
            )
            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await client.post(sandbox_endpoint, json=payload, headers=headers)
                response.raise_for_status()
                return DataBlocks(**response.json())

        except httpx.HTTPStatusError as e:
            logger.exception(
                f"Sandbox service returned an error: {e.response.status_code} - {e.response.text}"
            )
            return Exception(
                f"Error from sandbox service: {e.response.json().get('detail', 'Unknown error')}"
            )
        except Exception as e:
            logger.exception(f"An unexpected error occurred in ActionSandboxQuery: {e}")
            return e

    async def run_tool(
        self, action_config: SandboxQueryConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        """
        Tool entry point for the Sandbox Query action.
        """
        try:
            # The ToolFactory has already instantiated the class with the config.
            # We create a DataBlock from the config to pass to run_action.
            urls = [str(url) for url in (action_config.file_urls or [])]
            query_text = action_config.query or ""
            action_input = DataBlock(
                text=query_text,
                urls=urls,
            )
            return await self.run_action(ctx=ctx, app_state=app_state, action_input=action_input)
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in run_tool for SandboxQuery: {e}")
            return e
