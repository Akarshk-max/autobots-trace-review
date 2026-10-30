import io
from datetime import datetime
from typing import List

import pytest
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from src.autobots.action_graph.action_graph_result.user_action_graph_result import (
    UserActionGraphResult,
)
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_crud import EventResultAdminCRUD
from src.autobots.event_result.event_result_model import EventResultDoc, EventType
from src.autobots.files.user_files_model import FileMeta
from src.autobots.user.user import User
from src.autobots.user.user_orm_model import UserORM


class RunsData(BaseModel):
    run_id: str
    user_id: str


@pytest.mark.skip("used only for adhoc work: migrate event results from mongo to s3")
@pytest.mark.asyncio
async def test_migrate_event_result_of_action_graph_result(
    ctx: Context, db: AsyncIOMotorDatabase, set_test_settings
):
    event_result_crud = EventResultAdminCRUD(db=db)
    find_params = {
        "type": EventType.action_graph_run.value,
    }
    limit = 100
    offset = -1
    completed_run_ids: List[RunsData] = []
    error_run_ids: List[RunsData] = []
    while True:
        offset += 1
        print(f"Offset: {offset}")
        docs: List[EventResultDoc] = await event_result_crud._find_raw(
            find_params=find_params, limit=limit, offset=offset
        )
        if len(docs) == 0:
            break

        for doc in docs:
            run_id: str = doc.id
            user_id: str = doc.user_id
            user: UserORM = await User.get(ctx=ctx, user=UserORM(id=user_id), id=user_id, db=db)
            user_action_graph_result = UserActionGraphResult(user=user, db=db)
            fs_crud = user_action_graph_result.fs_crud
            run_filename = f"{run_id}.json"
            file_bytes = await fs_crud.get_bytes(ctx=ctx, filename=run_filename)
            if file_bytes:
                continue

            json_str = doc.model_dump_json()
            json_bytes = io.BytesIO(json_str.encode("utf-8"))
            filemetas: List[FileMeta] = await fs_crud.upload_binary(
                ctx=ctx, filename=run_filename, file_obj=json_bytes
            )
            for filemeta in filemetas:
                if not filemeta.url:
                    print(f"Failed for user_id/run_id: {user_id} / {run_id}")
                    error_run_ids.append(RunsData(run_id=run_id, user_id=user_id))
                else:
                    print(f"Success for user_id/run_id: {user_id} / {run_id}")
                    completed_run_ids.append(RunsData(run_id=run_id, user_id=user_id))

    date_time_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    with open(f"completed_run_ids_{date_time_str}.json", "w") as f:
        f.write(str(completed_run_ids))

    with open(f"error_run_ids_{date_time_str}.json", "w") as f:
        f.write(str(error_run_ids))

    print("Completed")
