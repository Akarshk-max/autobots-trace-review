from typing import Dict, List

import httpx
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from src.autobots.integrations.user_integration import UserIntegration
from src.autobots.integrations.app_types import AppTypes
from src.autobots.data_model.context import Context
from src.autobots.llm.tools.tool_save_files import (
    FileMetas,
    FileObj,
    SaveFilesToolConfig,
    save_files_tool,
)
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState


class SaveOutlookAttachmentConfig(BaseModel):
    message_id: str


async def save_outlook_email_attachment_tool(
    config: SaveOutlookAttachmentConfig,
    ctx: Context,
    app_state: AppState,
    user: UserORM,
    db: AsyncIOMotorDatabase,
    notify: Notify | None = None,
) -> FileMetas | Exception:
    """
    Description
    -----------
    Downloads all attachments from a specific Outlook email message using
    Microsoft Graph. The function retrieves the user's Outlook access token,
    queries the message's attachment endpoint, extracts valid file objects,
    and saves them using the project's file-saving tool. Attachments without a
    name or content are skipped with a contextual warning. Any network or API
    errors are logged and returned.

    Input
    -----
    config : dict
        Expected structure:
            {
                "message_id": "<Outlook message identifier>"
            }

        message_id : str
            The identifier of the Outlook email message whose attachments
            should be downloaded.

    Output
    ------
    FileMetas | Exception
        Returns a collection of metadata describing all successfully saved files.
        If the download or save workflow fails due to network issues, Outlook API
        errors, or unexpected conditions, the exception is logged and returned.

    """
    url = f"https://graph.microsoft.com/v1.0/me/messages/{config.message_id}/attachments"

    try:
        user_app_auth = UserIntegration()
        headers: Dict[str, str] = await user_app_auth.auth_header(
            ctx=ctx, db=db, user=user, app_name=AppTypes.outlook, decrypt=True
        )

        files: List[FileObj] = []
        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()

            response_json = response.json()
            assert response_json is not None, "Response JSON is empty."

            for attachment in response_json.get("value", []):
                name = attachment.get("name")
                content = attachment.get("contentBytes")

                if not name or not content:
                    logger.bind(ctx=ctx).warning(
                        "Skipping invalid attachment with missing name or content."
                    )
                    continue

                file_obj = FileObj(file_name=name, base64_content=content)
                files.append(file_obj)

        config = SaveFilesToolConfig(files=files)
        file_metas = await save_files_tool(config=config, ctx=ctx, app_state=app_state, user=user)
        return file_metas

    except httpx.RequestError as e:
        logger.bind(ctx=ctx).exception(f"HTTP request error (Outlook attachments): {e}")
        return e
    except httpx.HTTPStatusError as e:
        logger.bind(ctx=ctx).exception(f"HTTP status error (Outlook attachments): {e}")
        return e
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Error in save_outlook_email_attachment_tool: {e}")
        return e
