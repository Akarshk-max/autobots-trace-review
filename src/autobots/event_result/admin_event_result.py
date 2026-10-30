import json

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots import SettingsProvider
from src.autobots.conn.aws.aws_s3 import get_s3
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_crud import EventResultAdminCRUD
from src.autobots.event_result.event_result_model import EventResultDoc
from src.autobots.files.user_files import UserFiles
from src.autobots.user.user_orm_model import UserORM


class AdminEventResult:
    def __init__(self, user: UserORM, db: AsyncIOMotorDatabase):
        self.user = user
        self.user_id = str(user.id)
        assert SettingsProvider.is_system_email(user.email)
        self.db = db
        self.event_result_admin_crud = EventResultAdminCRUD(db)

    async def get_event_result_by_id(self, ctx: Context, id: str) -> EventResultDoc | dict | None:
        try:
            event_result_doc: EventResultDoc = (
                await self.event_result_admin_crud.get_event_result_by_id(ctx=ctx, event_doc_id=id)
            )
            if event_result_doc.type == "action_graph_run":
                user = UserORM(id=event_result_doc.user_id)
                s3 = get_s3()
                fs_crud = UserFiles(
                    user=user, s3=s3, top_level_folder=SettingsProvider.sget().AWS_S3_RUNS_FOLDER
                )
                filename = f"{event_result_doc.id}.json"
                data = await fs_crud.get_bytes(ctx=ctx, filename=filename)
                data_dict = json.loads(data)
                return data_dict
            
            if event_result_doc.type == "action_run" and event_result_doc.file_url:
                user = UserORM(id=event_result_doc.user_id)
                s3 = get_s3()
                fs_crud = UserFiles(
                    user=user, s3=s3, top_level_folder=SettingsProvider.sget().AWS_S3_ACTION_RUNS_FOLDER
                )
                filename = f"{event_result_doc.id}.json"
                data = await fs_crud.get_bytes(ctx=ctx, filename=filename)
                data_dict = json.loads(data)
                return data_dict
                
            return event_result_doc
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
        return None
