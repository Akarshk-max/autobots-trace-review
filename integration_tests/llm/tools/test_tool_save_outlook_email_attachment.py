import uuid

import pytest

from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.data_model.context import Context
from src.autobots.llm.tools.tool_save_outlook_mail_attachment import (
    SaveOutlookAttachmentConfig,
    save_outlook_email_attachment_tool,
)
from src.autobots.user.user_orm_model import UserORM


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message_id",
    [
        "AAMkADE4ZWJkMjA2LTNjZTQtNGI2Yy04ZjEwLWQwYzY5YzI5ZWNhNABGAAAAAACV3y7yIziiQr81hteZ5S52BwDumJafJ46AQKFkb%2BAhPVWJAAAAAAEMAADumJafJ46AQKFkb%2BAhPVWJAABftFDLAAA="
    ],
)
@pytest.mark.skip("Have to setup creds in test env")
async def test_save_outlook_attachment_happy_path(message_id, set_test_settings):
    ctx = Context()
    user_id = uuid.UUID("e29f3392-6124-40f5-8e24-fd6b5ebff875")
    user = UserORM(id=str(user_id))
    db = next(get_mongo_db())

    config = SaveOutlookAttachmentConfig(
        message_id=message_id,
    )
    file_metas = await save_outlook_email_attachment_tool(config=config, ctx=ctx, user=user, db=db)

    assert file_metas.file_metas[0].url is not None
