from io import BytesIO
from typing import Any, Dict, Optional

from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic_extra_types.pendulum_dt import DateTime

from src.autobots import SettingsProvider
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action_team.action_team.action_team_doc_model import (
    ActionTeamDoc,
    ActionTeamLiteDoc,
)
from src.autobots.action_team.action_team_result.action_team_result_model_doc import (
    ActionTeamResultCreate,
    ActionTeamResultDoc,
    ActionTeamResultDocsFound,
    ActionTeamResultFind,
    ActionTeamResultLiteDoc,
    ActionTeamResultUpdate,
)
from src.autobots.conn.aws.aws_s3 import get_s3
from src.autobots.core.clock.clock import Clock
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_crud import EventResultCRUD
from src.autobots.event_result.event_result_model import (
    EventResultDocCreate,
    EventResultDocFind,
    EventResultDocUpdate,
    EventResultStatus,
    EventType,
)
from src.autobots.event_result.event_result_sharing import (
    build_owned_or_shared_finds,
    build_shared_finds_for_id,
)
from src.autobots.event_result.turn_authorization import TurnAuthorization
from src.autobots.files.user_files import UserFiles
from src.autobots.llm_func.llm_func import LlmFunc
from src.autobots.user.user_orm_model import UserORM


class UserActionTeamResult(TurnAuthorization):
    def __init__(self, user: UserORM, db: AsyncIOMotorDatabase):
        self.user = user
        self.user_id = str(user.id)
        self.space_member = self.user.space_member
        self.event_result_crud = EventResultCRUD(db)
        s3 = get_s3()
        self.fs_crud = UserFiles(
            user=user, s3=s3, top_level_folder=SettingsProvider.sget().AWS_S3_RUNS_FOLDER
        )

    _RUN_NOT_FOUND = "Action team result not found"

    async def _owned_or_shared_run(
        self, run_id: str, ctx: Context | None = None
    ) -> ActionTeamResultDoc | None:
        # By keyword: this lookup takes (ctx, id) while the mixin hands over
        # (run_id, ctx), and positionally the two silently trade places.
        return await self.get_owned_or_shared_action_team_result(
            ctx=ctx, action_team_result_id=run_id
        )

    async def create_action_team_result(
        self,
        ctx: Context,
        action_team_result: ActionTeamResultCreate,
    ) -> ActionTeamResultDoc | None:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        try:
            lite_result = action_team_result.model_copy(deep=True)
            lite_result.result = ActionTeamLiteDoc(**action_team_result.result.model_dump())
            event_result_doc_create = EventResultDocCreate(
                user_id=self.user_id, space_id=space_id, **lite_result.model_dump()
            )
            event_result_doc = await self.event_result_crud.insert_one(event_result_doc_create)
            db_result_doc = ActionTeamResultDoc.model_validate(event_result_doc.model_dump())

            db_result_doc.result = action_team_result.result
            filename = f"{db_result_doc.id}.json"
            filemetas = await self.fs_crud.upload_binary(
                ctx=ctx,
                file_obj=BytesIO(db_result_doc.model_dump_json().encode("utf-8")),
                filename=filename,
            )
            assert len(filemetas) == 1
            filemeta = filemetas[0]
            assert filemeta.url is not None, "Event result not saved to file"

            event_result_doc_update = EventResultDocUpdate(
                id=event_result_doc.id, user_id=self.user_id, file_url=str(filemeta.url)
            )
            await self.event_result_crud.update_one(event_result_doc_update)

            return db_result_doc
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error while creating action team result: {str(e)}")
            raise e

    async def list_page_action_team_result(
        self,
        action_team_result_find: ActionTeamResultFind,
        limit: int = 100,
        offset: int = 0,
    ) -> ActionTeamResultDocsFound:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        action_team_result_find.type = EventType.action_team_run
        # No exclude_none: _build_filter drops the Nones. A non-None default would hard-filter.
        event_result_doc_find = EventResultDocFind(
            user_id=self.user_id, space_id=space_id, **action_team_result_find.model_dump()
        )
        doc_find_page = await self.event_result_crud.find_page(
            doc_find=event_result_doc_find, limit=limit, offset=offset
        )
        result_docs = []
        for event_result_doc in doc_find_page.docs:
            result_lite_doc = ActionTeamResultLiteDoc.model_validate(event_result_doc.model_dump())
            result_docs.append(result_lite_doc)
        return ActionTeamResultDocsFound(
            docs=result_docs,
            total_count=doc_find_page.total_count,
            limit=doc_find_page.limit,
            offset=doc_find_page.offset,
        )

    async def list_page_owned_or_shared_action_team_result(
        self,
        action_team_result_find: ActionTeamResultFind,
        limit: int = 100,
        offset: int = 0,
    ) -> ActionTeamResultDocsFound:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        action_team_result_find.type = EventType.action_team_run

        event_result_doc_find, or_find_queries = build_owned_or_shared_finds(
            result_find=action_team_result_find,
            user_id=self.user_id,
            user_email=self.user.email,
            space_id=space_id,
        )
        doc_find_page = await self.event_result_crud.find_page(
            doc_find=event_result_doc_find,
            or_find_queries=or_find_queries,
            limit=limit,
            offset=offset,
        )
        result_docs = []
        for event_result_doc in doc_find_page.docs:
            result_docs.append(
                ActionTeamResultLiteDoc.model_validate(event_result_doc.model_dump())
            )
        return ActionTeamResultDocsFound(
            docs=result_docs,
            total_count=doc_find_page.total_count,
            limit=doc_find_page.limit,
            offset=doc_find_page.offset,
        )

    async def get_owned_or_shared_action_team_result(
        self,
        ctx: Context,
        action_team_result_id: str,
    ) -> ActionTeamResultDoc | None:
        """Separate from `get_action_team_result`, which gates continuing a run -
        a grantee must not be able to write into one."""
        owned = await self.get_action_team_result(ctx, action_team_result_id)
        if owned is not None:
            return owned
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        try:
            doc_find, or_find_queries = build_shared_finds_for_id(
                result_id=action_team_result_id,
                event_type=EventType.action_team_run,
                user_email=self.user.email,
                space_id=space_id,
            )
            event_result_doc = await self.event_result_crud.find_one(
                doc_find, or_find_queries
            )
            if event_result_doc is None:
                return None
            return await self._read_shared_payload(
                ActionTeamResultDoc.model_validate(event_result_doc.model_dump()),
                result_id=action_team_result_id,
                ctx=ctx,
            )
        except Exception as e:
            logger.bind(ctx=ctx).exception(
                f"Error in get_owned_or_shared_action_team_result: {str(e)}"
            )
            return None

    async def _read_shared_payload(
        self,
        doc: ActionTeamResultDoc,
        *,
        result_id: str,
        ctx: Context,
    ) -> ActionTeamResultDoc:
        """Fetch a shared team run's payload from the owner's S3 prefix.

        Mongo holds an `ActionTeamLiteDoc`; the full document is written to S3.
        So returning the Mongo record to a grantee hands them a run with its
        thread missing - every leader and member turn - which renders as an empty
        conversation rather than as an error.

        `get_action_team_result` already reads S3 for the owned case and cannot
        be reused, because `UserFiles` prefixes every key with the id of the user
        it was built for - `self.fs_crud` points at the caller's prefix while a
        shared payload lives under the owner's.

        Reached only after `build_shared_finds_for_id` has matched the caller
        against the run's grant list, so the authorization decision is already
        made before this reads another user's object. Falls back to the Mongo
        document, matching what the owned path does when S3 has nothing.
        """
        owner_files = UserFiles(
            user=UserORM(id=doc.user_id),
            s3=get_s3(),
            top_level_folder=SettingsProvider.sget().AWS_S3_RUNS_FOLDER,
        )
        file_bytes = await owner_files.get_bytes(ctx=ctx, filename=f"{result_id}.json")
        if file_bytes:
            return ActionTeamResultDoc.model_validate_json(file_bytes)
        return doc

    async def get_action_team_result(
        self,
        ctx: Context,
        action_team_result_id: str,
    ) -> ActionTeamResultDoc | None:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        try:
            filename = f"{action_team_result_id}.json"
            file_bytes = await self.fs_crud.get_bytes(ctx=ctx, filename=filename)
            result_doc: ActionTeamResultDoc | None = None
            if file_bytes:
                result_doc = ActionTeamResultDoc.model_validate_json(file_bytes)
            else:
                event_result_doc_find = EventResultDocFind(
                    id=action_team_result_id, user_id=self.user_id, space_id=space_id
                )
                event_result_doc = await self.event_result_crud.find_one(event_result_doc_find)
                if event_result_doc:
                    result_doc = ActionTeamResultDoc.model_validate(event_result_doc.model_dump())
            if result_doc is None:
                raise HTTPException(status_code=404, detail="Result not found")
            return result_doc
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in getting action_team result: {str(e)}")
        return None

    async def update_action_team_result(
        self,
        ctx: Context,
        action_team_result_id: str,
        action_team_update: ActionTeamResultUpdate,
    ) -> ActionTeamResultDoc:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        try:
            # s3
            file_result_doc: ActionTeamResultDoc | None = await self.get_action_team_result(
                ctx=ctx,
                action_team_result_id=action_team_result_id,
            )
            file_result_doc = file_result_doc.model_copy(
                update=action_team_update.model_dump(
                    exclude_unset=True, exclude_none=True, exclude={"result"}
                ),
            )
            file_result_doc.result = ActionTeamDoc.model_validate(file_result_doc.result)
            if action_team_update.result:
                file_result_doc.result = action_team_update.result
            updated_at: DateTime = Clock.datetime_now()
            file_result_doc.updated_at = updated_at
            filename = f"{file_result_doc.id}.json"
            filemetas = await self.fs_crud.upload_binary(
                ctx=ctx,
                file_obj=BytesIO(file_result_doc.model_dump_json().encode("utf-8")),
                filename=filename,
            )
            assert len(filemetas) == 1
            filemeta = filemetas[0]
            assert filemeta.url is not None, "Event result not saved to file"
            logger.bind(
                ctx=ctx,
                action_team_result_id=file_result_doc.id,
            ).info(f"Updated action_team_result in s3: {file_result_doc.id}")

            # mongo
            db_result = file_result_doc.model_copy(deep=True)
            if file_result_doc.result:
                db_result.result = ActionTeamLiteDoc(**file_result_doc.result.model_dump())
            event_result_doc_update = EventResultDocUpdate(**db_result.model_dump())
            await self.event_result_crud.update_one(event_result_doc_update)
            logger.bind(
                ctx=ctx,
                action_team_result_id=action_team_result_id,
            ).info(f"Updated action_team_result in db: {action_team_result_id}")

            return file_result_doc
        except Exception as e:
            logger.bind(ctx=ctx).error(f"Error in updating action_team_result: {str(e)}")
            raise e

    async def search(
        self, ctx: Context, query: str, limit: int = 10, offset: int = 0
    ) -> ActionTeamResultDocsFound:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")

        q = (query or "").strip()
        if not q:
            return ActionTeamResultDocsFound(docs=[], total_count=0, limit=limit, offset=offset)

        event_result_docs_page = await self.event_result_crud.search_custom_page(
            user_id=self.user_id,
            event_type=EventType.action_team_run,
            q=q,
            limit=limit,
            offset=offset,
            searchable_paths=["name", "status", "result.name"],
        )
        result_docs = []
        for event_result_doc in event_result_docs_page.docs:
            try:
                result_docs.append(
                    ActionTeamResultLiteDoc.model_validate(event_result_doc.model_dump())
                )
            except Exception as e:
                logger.bind(ctx=ctx).exception(
                    f"Error in search action_team_results: {str(e)}"
                )
        return ActionTeamResultDocsFound(
            docs=result_docs,
            total_count=event_result_docs_page.total_count,
            limit=event_result_docs_page.limit,
            offset=event_result_docs_page.offset,
        )

    async def delete_action_team_result(self, id: str) -> int:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        event_result_doc_find = EventResultDocFind(id=id, user_id=self.user_id, space_id=space_id)
        delete_result = await self.event_result_crud.delete_many(event_result_doc_find)
        return delete_result.deleted_count
    
    async def init(
        self,
        app_state: AppState,
        action_team_result_id: str | None,
        action_team_doc: ActionTeamDoc,
        input_dict: Dict[str, Any],
        db: AsyncIOMotorDatabase,
        user: UserORM,
        ctx: Context | None = None,
        batch_id: str | None = None,
    ) -> ActionTeamResultDoc | None:
        action_team_result_doc: Optional[ActionTeamResultDoc] = None
        action_team_doc.input = input_dict
        action_team_doc.output = {}
        if not action_team_result_id:
            action_team_result_create: ActionTeamResultCreate = ActionTeamResultCreate(
                status=EventResultStatus.processing,
                result=action_team_doc,
                is_saved=False,
                batch_id=batch_id,
            )
            data: str = f"Name: {action_team_doc.name}\nInput: {str(input_dict)}"
            default_title: str = (
                f"{action_team_doc.name} - "
                f"{Clock.get_date_time().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            action_result_name: str = await LlmFunc.gen_title(
                ctx=ctx,
                app_state=app_state,
                db=db,
                user=user,
                title_for=DataBlock(text=data),
                default_title=default_title,
            )
            logger.bind(ctx=ctx, action_result_name=action_result_name).info("Generated Action Team Result Name")
            action_team_result_create.name = action_result_name
            action_team_result_doc: Optional[
                ActionTeamResultDoc
            ] = await self.create_action_team_result(
                ctx=ctx, action_team_result=action_team_result_create
            )
            logger.bind(ctx=ctx, action_team_result_doc_id=action_team_result_doc.id).info(
                f"Created Action Team Run with doc : {action_team_result_doc.model_dump()}"
            )
        else:
            action_team_result_doc: Optional[ActionTeamResultDoc] = await self.get_action_team_result(
                ctx=ctx,
                action_team_result_id=action_team_result_id,
            )
            if not action_team_result_doc:
                raise ValueError(f"Action Team Result with ID {action_team_result_id} not found")
            if not action_team_result_doc.name:
                data: str = f"Name: {action_team_doc.name}\nInput: {str(input_dict)}"
                default_title: str = (
                    f"{action_team_doc.name} - "
                    f"{Clock.get_date_time().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                action_result_name: str = await LlmFunc.gen_title(
                    ctx=ctx,
                    app_state=app_state,
                    db=db,
                    user=user,
                    title_for=DataBlock(text=data),
                    default_title=default_title,
                )
                logger.bind(ctx=ctx, action_result_name=action_result_name).info("Generated Action Team Result Name")
                action_team_result_doc.name = action_result_name
            # Need to update the action_team_result_doc with the new action_team_doc
            action_team_doc.thread = action_team_result_doc.result.thread 
            action_team_result_update: ActionTeamResultUpdate = ActionTeamResultUpdate(
                status=EventResultStatus.processing,
                result=action_team_doc,
            )
        
            await self.update_action_team_result(
                ctx=ctx,
                action_team_result_id=action_team_result_doc.id,
                action_team_update=action_team_result_update,
            )
        
        return action_team_result_doc
            
