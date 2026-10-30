import copy
import json
from typing import Any, AsyncIterable, Dict, List, Optional

from fastapi import BackgroundTasks, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots import SettingsProvider
from src.autobots.action.action.action_crud import ActionCRUD
from src.autobots.action.action.action_doc_model import (
    ActionCreate,
    ActionDoc,
    ActionDocCreate,
    ActionDocFind,
    ActionDocsFound,
    ActionDocUpdate,
    ActionFind,
    ActionLiteDoc,
    ActionResult,
    ActionUpdate,
)
from src.autobots.action.action.common_action_models import (
    DataBlock,
    DataBlocks,
    Filter,
    RunSource,
    stamp_author,
)
from src.autobots.action.action_result.action_result_doc_model import (
    ActionResultCreate,
    ActionResultDoc,
)
from src.autobots.action.action_result.user_action_result import UserActionResult
from src.autobots.action.action_type.action_factory import ActionFactory, RunActionObj
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.api.webhook import Webhook
from src.autobots.batch.batch_service import BatchService
from src.autobots.core.clock.clock import Clock
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.data_model.space_id import SpaceId
from src.autobots.event_result.event_result_model import EventResultStatus, EventType
from src.autobots.llm_func.llm_func import LlmFunc
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.space.space_crud import SpaceMemberCRUD
from src.autobots.user.user import User
from src.autobots.user.user_orm_model import UserORM


class UserActions:
    def __init__(self, user: UserORM, db: AsyncIOMotorDatabase):
        self.user = user
        self.user_id = str(user.id)
        self.db = db
        self.action_crud = ActionCRUD(db)
        self.space_member = self.user.space_member

    async def clone_action_from_doc(
        self,
        ctx: Context,
        source_action: ActionDoc,
        name_override: Optional[str] = None,
        reset_results: bool = True,
    ) -> ActionDoc:
        """
        Clone an action by copying the *stored* ActionDoc.config as-is (no ActionFactory re-processing).

        Why this exists:
        - Some actions have different "create/update payload config" shape vs "stored config" shape.
        - Example: text2text_user_input stores config as DataBlocks {"blocks":[...]} but create expects DataBlock.
        - Using create_action() would validate stored config as create-type and silently drop fields (e.g. URLs).

        This method is used by workflow cloning (action-graph clone) so the cloned workflow is self-contained
        for the destination user.
        """
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        try:
            cloned_config = copy.deepcopy(source_action.config or {})

            action_doc_create = ActionDocCreate(
                user_id=self.user_id,
                space_id=space_id,
                name=name_override if name_override is not None else source_action.name,
                version=source_action.version,
                description=source_action.description,
                user_manual=source_action.user_manual,
                type=source_action.type,
                config=cloned_config,
                is_saved=source_action.is_saved,
                # Reset runtime/run-history fields on clone
                results=[] if reset_results else copy.deepcopy(source_action.results or []),
                input=None,
                output=None,
                # Reset sharing/publishing on clone
                is_published=False,
                shared_with=[],
                time_saved_per_run=source_action.time_saved_per_run,
                dollar_cost_per_hour=source_action.dollar_cost_per_hour,
            )

            new_action_doc = await self.action_crud.insert_one(action_doc_create)
            logger.bind(
                ctx=ctx,
                code=AppCode.ACTION,
                old_action_id=source_action.id,
                new_action_id=new_action_doc.id,
                action_name=new_action_doc.name,
                action_type=str(new_action_doc.type),
            ).info("Cloned action (from_doc) by copying stored config")
            return new_action_doc
        except HTTPException:
            raise
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.ACTION, source_action_id=getattr(source_action, "id", None)).exception(
                f"Error cloning action (from_doc): {str(e)}"
            )
            raise

    async def create_action(self, ctx: Context, app_state: AppState, action_create: ActionCreate) -> ActionDoc | None:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        try:
            # TODO: exclude_none=True
            # Create Config for the Action based on action type
            config = await ActionFactory.create_action_config(
                ctx=ctx,
                app_state=app_state,
                action_type=action_create.type,
                action_config_create=action_create.config,
                user=self.user,
                db=self.db,
            )
            action_create.config = config
            # Now create Action
            action_doc_create = ActionDocCreate(
                user_id=self.user_id, space_id=space_id, **action_create.model_dump()
            )
            action_doc = await self.action_crud.insert_one(action_doc_create)
            return action_doc
        except HTTPException as e:
            logger.bind(ctx=ctx, code=AppCode.ACTION).exception(str(e))
            raise
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.ACTION).exception(str(e))
            raise

    async def list_actions(
        self,
        action_find: ActionFind,
        limit: int = 100,
        offset: int = 0,
    ) -> ActionDocsFound:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        action_doc_find = ActionDocFind(
            user_id=self.user_id, space_id=space_id, **action_find.model_dump()
        )
        paged_docs = await self.action_crud.find_page(
            doc_find=action_doc_find, or_find_queries=None, limit=limit, offset=offset
        )
        action_docs = []
        for action_doc in paged_docs.docs:
            action_docs.append(ActionLiteDoc.model_validate(action_doc))
        return ActionDocsFound(
            docs=action_docs,
            total_count=paged_docs.total_count,
            limit=paged_docs.limit,
            offset=paged_docs.offset,
        )

    # async def list_owned_or_published(
    #         self, action_find: ActionFind,
    #         limit: int = 100, offset: int = 0
    # ) -> ActionDocsFound:
    #     action_doc_find = ActionDocFind(user_id=self.user_id, **action_find.model_dump())
    #     if action_find.is_published:
    #         # Show all actions of the users at all time
    #         action_doc_find.is_published = None
    #     assert action_doc_find.user_id
    #
    #     action_doc_find_published = None
    #     if action_find.is_published:
    #         action_doc_find_published = ActionDocFind(**action_find.model_dump(exclude_none=True))
    #         action_doc_find_published.is_published = True
    #         assert action_doc_find_published.is_published
    #
    #     or_find_queries = None
    #     if action_doc_find_published:
    #         or_find_queries = [action_doc_find_published]
    #     paged_docs = await self.action_crud.find_page(
    #         doc_find=action_doc_find,
    #         or_find_queries=or_find_queries,
    #         limit=limit,
    #         offset=offset
    #     )
    #     action_docs = []
    #     for action_doc in paged_docs.docs:
    #         action_docs.append(ActionLiteDoc.model_validate(action_doc))
    #     return ActionDocsFound(
    #         docs=action_docs,
    #         total_count=paged_docs.total_count,
    #         limit=paged_docs.limit,
    #         offset=paged_docs.offset
    #     )

    async def list_owned_or_shared_with(
        self,
        action_find: ActionFind,
        limit: int = 100,
        offset: int = 0,
    ) -> ActionDocsFound:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None

        action_doc_find = ActionDocFind(
            user_id=self.user_id, space_id=space_id, **action_find.model_dump()
        )
        if action_find.shared_with:
            # Show all actions of the users at all time
            action_doc_find.shared_with = None
        assert action_doc_find.user_id

        or_find_queries = []

        # Email-based sharing
        if action_find.shared_with:
            action_doc_find_shared_with = ActionDocFind(
                **action_find.model_dump(exclude_none=True), space_id=space_id
            )
            action_doc_find_shared_with.user_id = None
            assert self.user.email == action_doc_find_shared_with.shared_with
            or_find_queries.append(action_doc_find_shared_with)

        # Space-based sharing
        if space_id:
            or_find_queries.append(ActionDocFind(shared_with=SpaceId(space_id=space_id)))

        # NOTE: `offset` is treated as a page index (0-based) for API backward compatibility.
        paged_docs = await self.action_crud.find_page(
            doc_find=action_doc_find,
            or_find_queries=or_find_queries if or_find_queries else None,
            limit=limit,
            offset=offset,
        )
        action_docs = []
        for action_doc in paged_docs.docs:
            action_docs.append(ActionLiteDoc.model_validate(action_doc))
        return ActionDocsFound(
            docs=action_docs,
            total_count=paged_docs.total_count,
            limit=paged_docs.limit,
            offset=paged_docs.offset,
        )

    async def list_system_actions(
        self, ctx: Context, action_find: ActionFind, limit: int = 100, offset: int = 0, filter : Filter = None,
    ) -> ActionDocsFound:
        system_user = SettingsProvider.sget().SYSTEM_USER
        
        action_doc_find = ActionDocFind(user_id=system_user, **action_find.model_dump())
        action_doc_find.is_published = True
        # logger.bind(ctx=ctx, code=AppCode.ACTION).info(
        #     f"action_doc_find : {action_doc_find} and filter : {filter}"    
        # )
        filter_system_action_ids = []
        if filter and filter.action_type == "Autocode":
            filter_system_action_ids = (
                SettingsProvider.sget().AUTOCODE_SYSTEM_ACTION_IDS
            )
            logger.bind(ctx=ctx, code=AppCode.ACTION).info(
                f"Filtering system actions for Autocode with system_action_ids : {filter_system_action_ids}"
            )   
        # NOTE: `offset` is treated as a page index (0-based) for API backward compatibility.
        
        action_docs = []
        filtered_total_count = 0
        if filter and filter.action_type:    
            for id in filter_system_action_ids:
                action_lite_doc = await self.get_lite_system_action_by_id(ctx,id)
                if action_lite_doc and self.match(action_lite_doc, action_doc_find):
                    action_docs.append(action_lite_doc)
            filtered_total_count = len(action_docs)
            action_docs = action_docs[offset*limit:offset*limit + limit]
            
        else:   
            paged_docs = await self.action_crud.find_page(
            doc_find=action_doc_find, or_find_queries=None, limit=limit, offset=offset
        )
            for action_doc in paged_docs.docs:
                action_docs.append(ActionLiteDoc.model_validate(action_doc))
        return ActionDocsFound(
            docs=action_docs,
            total_count=paged_docs.total_count if not (filter and filter.action_type) else filtered_total_count,
            limit=paged_docs.limit if not (filter and filter.action_type) else limit,
            offset=paged_docs.offset if not (filter and filter.action_type) else offset,
        )

    async def get_action(self, action_id: str) -> ActionDoc | None:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        try:
            action_doc_find = ActionDocFind(id=action_id, user_id=self.user_id, space_id=space_id)
            action_doc = await self.action_crud.find_one(action_doc_find)
            # if len(action_docs) != 1:
            #     raise HTTPException(404, "Error in finding action")
            return action_doc
        except Exception as e:
            logger.exception(str(e))
        return None



    def match(self, action_lite_doc: ActionLiteDoc, action_doc_find: ActionDocFind) -> bool:
        """
        Check if the action_lite_doc matches the criteria specified in action_doc_find.
        """
        if action_doc_find.id and action_lite_doc.id != action_doc_find.id:
            return False
        if action_doc_find.name and action_lite_doc.name != action_doc_find.name:
            return False
        if action_doc_find.version is not None and action_lite_doc.version != action_doc_find.version:
            return False
        if action_doc_find.type and action_lite_doc.type != action_doc_find.type:
            return False
        if action_doc_find.is_published is not None and action_lite_doc.is_published != action_doc_find.is_published:
            return False
        return True

    
    # async def get_owned_or_published(
    #         self, action_id: str, empty_published_action_config: bool = True
    # ) -> ActionDoc | None:
    #     action_doc = await self.get_action(action_id)
    #     if action_doc is None:
    #         action_doc = await self.get_published_action(action_id, empty_published_action_config)
    #     return action_doc

    async def get_owned_or_shared_with(
        self, action_id: str, empty_shared_action_config: bool = True
    ) -> ActionDoc | None:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        action_doc = await self.get_action(action_id)
        if action_doc is None:
            action_doc = await self.get_shared_action(action_id, empty_shared_action_config)
        return action_doc

    async def get_owned_or_shared_or_system_actions(
        self,
        ctx: Context,
        action_id: str,
        empty_shared_action_config: bool = True,
    ) -> ActionDoc | None:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        action_doc = await self.get_action(action_id)
        if action_doc is None:
            action_doc = await self.get_shared_action(action_id, empty_shared_action_config)
        if action_doc is None:
            action_doc = await self.get_system_action(
                ctx=ctx, id=action_id, empty_shared_action_config=empty_shared_action_config
            )
        return action_doc

    # async def get_published_action(
    #         self, id: str, empty_published_action_config: bool = True
    # ) -> ActionDoc | None:
    #     try:
    #         action_doc_find = ActionDocFind(id=id, is_published=True)
    #         action_doc = await self.action_crud.find_one(action_doc_find)
    #         # if len(action_docs) != 1:
    #         #     raise HTTPException(404, "Error in finding action")
    #         if action_doc and empty_published_action_config:
    #             action_doc.config = {}
    #         return action_doc
    #     except Exception as e:
    #         logger.exception(str(e))
    #     return None

    async def get_shared_action(
        self, id: str, empty_shared_action_config: bool = True
    ) -> ActionDoc | None:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        try:
            # Check email-based sharing
            action_doc_find = ActionDocFind(id=id, shared_with=self.user.email, space_id=space_id)
            or_find_queries = None
            # Also check space-based sharing
            if space_id:
                or_find_queries = [ActionDocFind(id=id, shared_with=SpaceId(space_id=space_id))]
            action_doc = await self.action_crud.find_one(action_doc_find, or_find_queries)
            if action_doc and empty_shared_action_config:
                action_doc.config = {}
            return action_doc
        except Exception as e:
            logger.exception(str(e))
        return None

    async def get_system_action(
        self, ctx: Context, id: str, empty_shared_action_config: bool = True
    ) -> ActionDoc | None:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        try:
            system_user = SettingsProvider.sget().SYSTEM_USER
            # System actions are global — never scoped by space_id
            action_doc_find = ActionDocFind(id=id, user_id=system_user, is_published=True)
            action_doc = await self.action_crud.find_one(action_doc_find)
            # if len(action_docs) != 1:
            #     raise HTTPException(404, "Error in finding action")
            if action_doc and empty_shared_action_config:
                action_doc.config = {}
            return action_doc
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.ACTION).exception(str(e))
        return None
    
    async def get_lite_system_action_by_id(self, ctx: Context, id : str, empty_shared_action_config: bool = True) -> ActionLiteDoc | None:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        try:
            system_user = SettingsProvider.sget().SYSTEM_USER
            # System actions are global — never scoped by space_id
            action_doc_find = ActionDocFind(id=id, user_id=system_user, is_published=True)
            action_lite_docs = await self.action_crud.find(action_doc_find, None,100, 0)
            # if len(action_docs) != 1:
            #     raise HTTPException(404, "Error in finding action")
            if len(action_lite_docs) == 1:
                action_lite_doc = action_lite_docs[0]
                return action_lite_doc
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
        return None

    async def get_lite_action_by_name(self, ctx: Context, action_name: str) -> ActionLiteDoc | None:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        action_doc = None
        try:
            action_doc_find = ActionDocFind(
                user_id=self.user_id, name=action_name, space_id=space_id
            )
            action_docs = await self.action_crud.find(action_doc_find, None, 100, 0)
            if len(action_docs) != 0:
                action_doc = action_docs[0]
                for action in action_docs:
                    if action.version > action_doc.version:
                        action_doc = action
            else:
                shared_action_doc_find = ActionDocFind(
                    name=action_name, shared_with=self.user.email, space_id=space_id
                )
                or_find_queries = None
                if space_id:
                    or_find_queries = [
                        ActionDocFind(name=action_name, shared_with=SpaceId(space_id=space_id))
                    ]
                shared_action_docs = await self.action_crud.find(
                    doc_find=shared_action_doc_find,
                    or_find_queries=or_find_queries,
                )
                if len(shared_action_docs) > 1:
                    logger.bind(ctx=ctx, code=AppCode.ACTION).error(
                        f"Multiple shared actions found for name: {action_name}"
                    )
                    # Pick the one with highest version (like owned/system actions)
                    action_doc = shared_action_docs[0]
                    for action in shared_action_docs:
                        if action.version > action_doc.version:
                            action_doc = action
                    logger.bind(ctx=ctx, code=AppCode.ACTION).info(
                        f"Selected latest version shared action for name: {action_name}"
                    )
                elif len(shared_action_docs) == 1:
                    action_doc = shared_action_docs[0]
                    logger.bind(ctx=ctx, code=AppCode.ACTION).info(
                        f"Found shared action for name: {action_name}"
                    )
                else:
                    logger.bind(ctx=ctx, code=AppCode.ACTION).info(f"No shared actions found for name: {action_name}")

                # Only search system actions if no owned or shared action was found
                if action_doc is None:
                    system_user = SettingsProvider.sget().SYSTEM_USER
                    # System actions are global — never scoped by space_id
                    system_action_doc_find = ActionDocFind(
                        name=action_name, user_id=system_user, is_published=True
                    )
                    system_action_docs = await self.action_crud.find(
                        system_action_doc_find, None, 100, 0
                    )

                    if len(system_action_docs) > 1:
                        logger.bind(ctx=ctx, code=AppCode.ACTION).error(
                            f"Multiple system actions found for name: {action_name}"
                        )
                        # Pick the one with highest version (like owned actions)
                        action_doc = system_action_docs[0]
                        for action in system_action_docs:
                            if action.version > action_doc.version:
                                action_doc = action
                        logger.bind(ctx=ctx, code=AppCode.ACTION).info(
                            f"Selected latest version system action for name: {action_name}"
                        )
                    elif len(system_action_docs) == 1:
                        action_doc = system_action_docs[0]
                        logger.bind(ctx=ctx, code=AppCode.ACTION).info(
                            f"Found system action for name: {action_name}"
                        )
                    else:
                        logger.bind(ctx=ctx, code=AppCode.ACTION).info(
                            f"No system actions found for name: {action_name}"
                        )
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.ACTION, action_name=action_name).exception(str(e))
        return action_doc

        # Old commented code for reference:
        # published_action_doc_find = ActionDocFind(name=action_name, is_published=True)
        # published_action_docs = await self.action_crud.find(published_action_doc_find, None, 100, 0)
        # if len(published_action_docs) > 1:
        #     logger.bind(ctx=ctx).error(f"Multiple published actions found for name: {action_name}")
        # elif len(published_action_docs) == 1:
        #     action_doc = published_action_docs[0]
        #     logger.bind(ctx=ctx).bind(action_doc=action_doc).info(
        #         f"Found published action for name: {action_name}")
        # else:
        #     logger.bind(ctx=ctx).info(f"No published actions found for name: {action_name}")

    async def get_lite_action_by_id(self, action_id: str) -> ActionLiteDoc | None:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        action_doc = None
        try:
            action_doc_find = ActionDocFind(user_id=self.user_id, id=action_id, space_id=space_id)
            action_docs = await self.action_crud.find(action_doc_find, None, 100, 0)
            if len(action_docs) == 1:
                action_doc = action_docs[0]
                return action_doc
        except Exception as e:
            logger.exception(str(e))
        return action_doc

    async def search(self, query: str, limit: int = 10, offset: int = 0) -> ActionDocsFound:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None

        q = (query or "").strip()
        if not q:
            return ActionDocsFound(docs=[], total_count=0, limit=limit, offset=offset)

        doc_find = ActionDocFind(user_id=self.user_id, space_id=space_id)
        or_find_queries = [ActionDocFind(shared_with=self.user.email, space_id=space_id)]
        if space_id:
            or_find_queries.append(ActionDocFind(shared_with=SpaceId(space_id=space_id)))

        docs_page = await self.action_crud.search_text_page(
            doc_find=doc_find,
            or_find_queries=or_find_queries,
            q=q,
            searchable_paths=["name", "type"],
            limit=limit,
            offset=offset,
        )
        action_docs = []
        for action_doc in docs_page.docs:
            action_docs.append(ActionLiteDoc.model_validate(action_doc))
        return ActionDocsFound(
            docs=action_docs,
            total_count=docs_page.total_count,
            limit=docs_page.limit,
            offset=docs_page.offset,
        )

    async def update_action(
        self, ctx: Context, app_state: AppState, action_id: str, action_update: ActionUpdate
    ) -> ActionDoc:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        if action_update.config is not None:
            # Update Config for the Action based on action type
            existing_action_doc = await self.get_action(action_id)
            updated_config = await ActionFactory.update_action_config(
                ctx=ctx,
                app_state=app_state,
                action_type=existing_action_doc.type,
                action_config=existing_action_doc.config,
                action_config_update=action_update.config,
                user=self.user,
                db=self.db,
            )
            action_update.config = updated_config
        # Now create Action
        action_doc_update = ActionDocUpdate(
            id=action_id, user_id=self.user_id, **action_update.model_dump()
        )
        action_doc = await self.action_crud.update_one(action_doc_update)
        return action_doc

    async def delete_action(self, action_id: str) -> int:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        action_doc_find = ActionDocFind(id=action_id, user_id=self.user_id, space_id=space_id)
        delete_result = await self.action_crud.delete_many(action_doc_find)
        return delete_result.deleted_count

    # async def run_action(
    #         self, action_id: str, input: Dict[str, Any]
    # ) -> Any:
    #     action_doc_find = ActionDocFind(id=action_id, user_id=self.user_id)
    #     action_docs = await self.action_crud.find(action_doc_find)
    #     if len(action_docs) != 1:
    #         raise HTTPException(405, "Action cannot be run")
    #     resp: RunActionObj = await ActionFactory.run_action(action_docs[0], input)
    #     return resp.output_dict

    async def run_action_v1(
        self,
        ctx: Context,
        app_state: AppState,
        action_id: str,
        input: Dict[str, Any],
        action_result_id: str = None,
        notify: Notify | None = None,
    ) -> ActionDoc | Exception:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        if action_id == "":
            data_block = DataBlock.model_validate(input)
            data_blocks = DataBlocks(blocks=[data_block])
            output = data_blocks.model_dump()
            action_doc = ActionDoc(
                id="",
                user_id=self.user_id,
                name="",
                type=ActionType.mock_action,
                input=input,
                output=output,  # {"blocks": [input]}
                results=[ActionResult(input=input, output=output)],
            )
            return action_doc

        action_doc_find = ActionDocFind(id=action_id, user_id=self.user_id, space_id=space_id)
        action_doc = await self.action_crud.find_one(action_doc_find)
        if not action_doc:
            raise HTTPException(404, "Action not found")
        logger.bind(ctx=ctx, code=AppCode.ACTION, action_name=action_doc.name, action_id=action_id).info("Run action")
        action_doc.input = input
        app_state.parent_action_id = app_state.action_id
        app_state.parent_action_run_id = app_state.action_run_id
        app_state.action_id = action_doc.id
        app_state.action_name = action_doc.name

        if action_result_id is not None:
            user_action_result = UserActionResult(self.user, self.db)
            action_result_doc: ActionResultDoc | None = await user_action_result.get_action_result(
                action_result_id
            )
            if action_result_doc is None:
                raise HTTPException(404, "Action Result not found")
            app_state.action_run_id = action_result_doc.id
            app_state.action_run_name = action_result_doc.name


        notify(ctx, f"Running {action_doc.name}...") if notify else None
        run_obj: RunActionObj = await ActionFactory.run_action(
            ctx=ctx,
            app_state=app_state,
            action_doc=action_doc,
            action_input_dict=input,
            user=self.user,
            db=self.db,
            notify=notify,
        )
        if isinstance(run_obj, Exception):
            logger.bind(ctx=ctx, code=AppCode.ACTION, action_name=action_doc.name, action_id=action_id).error(
                f"Returning Run_Obj as Exception obj: {str(run_obj)}"
            )
            return run_obj
        action_doc.config = run_obj.config_dict
        action_doc.output = run_obj.output_dict
        if action_doc.results is None:
            action_doc.results = []
        action_doc.results.append(
            ActionResult(input=run_obj.input_dict, output=run_obj.output_dict)
        )
        return action_doc

    async def run_action_stream(
        self, ctx: Context, app_state: AppState, action_id: str, input: Dict[str, Any], action_result_id: str = None
    ) -> AsyncIterable[bytes]:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None

        def send(ev: str, **kw) -> bytes:
            return (json.dumps({"event": ev, **kw}, ensure_ascii=False) + "\n").encode("utf-8")

        if action_id == "":
            data_block = DataBlock.model_validate(input)
            data_blocks = DataBlocks(blocks=[data_block])
            output = data_blocks.model_dump()
            action_doc = ActionDoc(
                id="",
                user_id=self.user_id,
                name="",
                type=ActionType.mock_action,
                input=input,
                output=output,  # {"blocks": [input]}
                results=[ActionResult(input=input, output=output)],
            )
            # return action_doc
            yield send("user-actions-run-action-stream", result=action_doc.model_dump(mode="json"))
        action_doc_find = ActionDocFind(id=action_id, user_id=self.user_id, space_id=space_id)
        # print("Finding action doc...")
        action_doc = await self.action_crud.find_one(action_doc_find)
        # print("Found action doc...",action_doc)
        if not action_doc:
            raise HTTPException(404, "Action not found")

        logger.bind(ctx=ctx, code=AppCode.ACTION, action_name=action_doc.name, action_id=action_id).info("Run action")
        # print("Adding input to action doc...",input)
        action_doc.input = input
        # print("Added input to action doc...")
        # logger.bind(ctx=ctx, code=AppCode.ACTION, action_name=action_doc.name, action_id=action_id).info(
        #     f"Action Input: {input}"
        # )
        if action_result_id is not None:
            user_action_result = UserActionResult(self.user, self.db)
            action_result_doc: ActionResultDoc | None = await user_action_result.get_action_result(
                action_result_id
            )
            if action_result_doc is None:
                raise HTTPException(404, "Action Result not found")
        run_obj: RunActionObj = None
        agen = ActionFactory.run_action_stream(
            ctx=ctx,
            app_state=app_state,
            action_doc=action_doc,
            action_input_dict=input,
            user=self.user,
            db=self.db,
        )
        async for chunk in agen:
            # chunk = await agen.__anext__()
            try:
                obj = json.loads(chunk.decode("utf-8"))
                if obj.get("event") == "action-factory-action-run":
                    # capture for server-side use (log/persist/webhook)
                    run_obj = obj.get("error")
                    if run_obj is None:
                        run_obj = RunActionObj.model_validate(obj.get("result"))
                        if run_obj is None:
                            raise Exception("No return obj found in user actions stream")
                    else:
                        raise Exception(str(run_obj))
            except Exception as e:
                logger.bind(ctx=ctx, code=AppCode.ACTION, action_name=action_doc.name, action_id=action_id).exception(
                    f"Exception in parsing stream chunk: {str(e)}"
                )
                yield send("user-actions-run-action-stream", error=str(e))
                # raise e
            yield chunk

        if isinstance(run_obj, Exception):
            logger.bind(ctx=ctx,code=AppCode.ACTION, action_name=action_doc.name, action_id=action_id).error(
                f"Returning Run_Obj as Exception obj: {str(run_obj)}"
            )
            # return run_obj
            yield send("user-actions-run-action-stream", error=str(run_obj.model_dump()))
        logger.bind(ctx=ctx, code=AppCode.ACTION, action_name=action_doc.name, action_id=action_id).info(
            f"User Action Completed, run obj : {run_obj}"
        )
        action_doc.config = run_obj.config_dict
        action_doc.output = run_obj.output_dict
        if action_doc.results is None:
            action_doc.results = []
        action_doc.results.append(
            ActionResult(input=run_obj.input_dict, output=run_obj.output_dict)
        )
        logger.bind(ctx=ctx, code=AppCode.ACTION, action_name=action_doc.name, action_id=action_id).info(
            f"Yielding final action doc : {action_doc}"
        )
        yield send("user-actions-run-action-stream", result=action_doc.model_dump(mode="json"))
        # return action_doc

    @staticmethod
    async def run_action_doc(
        ctx: Context,
        app_state: AppState,
        action_doc: ActionDoc,
        input: Dict[str, Any],
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify,
    ) -> ActionDoc:
        logger.bind(ctx=ctx, code=AppCode.ACTION, action_name=action_doc.name, action_id=action_doc.id).info(
            "Run action doc"
        )
        action_doc.input = input
        app_state.parent_action_id = app_state.action_id
        app_state.parent_action_run_id = app_state.action_run_id
        app_state.action_id = action_doc.id
        app_state.action_name = action_doc.name
        notify(ctx, f"Running {action_doc.name}") if notify else None
        run_obj: RunActionObj | Exception = await ActionFactory.run_action(
            ctx=ctx,
            app_state=app_state,
            action_doc=action_doc,
            action_input_dict=input,
            user=user,
            db=db,
            notify=notify,
        )
        match run_obj:
            case Exception():
                logger.bind(
                    ctx=ctx, code=AppCode.ACTION, action_name=action_doc.name, action_id=action_doc.id
                ).exception("Exception in run_action_doc: " + str(run_obj))
            case RunActionObj():
                action_doc.config = run_obj.config_dict
                action_doc.output = run_obj.output_dict
                if action_doc.results is None:
                    action_doc.results = []
                action_doc.results.append(
                    ActionResult(input=run_obj.input_dict, output=run_obj.output_dict)
                )
        return action_doc

    @staticmethod
    async def run_scheduled_action(
        ctx: Context,
        runnable_id: str,
        user_id: str,
        input: Dict[str, Any],
        run_id: str | None = None,
        space_id: str | None = None,
        app_state: AppState | None = None,
    ) -> ActionResultDoc | None:
        old_ctx = ctx
        ctx = Context(user_id=user_id)
        # Jobs stored before this signature still replay an app_state of their own.
        app_state = AppState()
        user = UserORM(id=user_id)
        db = next(get_mongo_db())
        user = await User.get(ctx=ctx, user=user, id=user_id, db=db)

        # The schedule's input block was baked into the job kwargs when the
        # schedule was created. Attribute the turn to this fire, not to that.
        input = stamp_author(input, user_id=user_id, source=RunSource.schedule)

        # Resolve space_member from stored space_id and attach to user
        if space_id:
            user.space_member = await SpaceMemberCRUD(db).find_membership(
                space_id=space_id, user_id=str(user.id)
            )

        user_action = UserActions(user=user, db=db)
        action_doc: ActionDoc | None = await user_action.get_owned_or_shared_or_system_actions(
            ctx=ctx,
            action_id=runnable_id,
            empty_shared_action_config=False,
        )
        if not action_doc:
            raise HTTPException(
                status_code=400, detail=f"Action {runnable_id} not found for user {user_id}"
            )
        user_action_result = UserActionResult(user=user, db=db)

        action_result_doc: ActionResultDoc | None = await ActionFactory.run_action_in_background(
            ctx=ctx,
            app_state=app_state,
            action_doc=action_doc,
            action_input_dict=input,
            user_action_result=user_action_result,
            db=db,
            action_result_id=run_id,
        )
        logger.bind(ctx=ctx, old_ctx=old_ctx, code=AppCode.ACTION, action_run_id=action_result_doc.id).info(
            "Scheduled action run"
        )
        return action_result_doc

    async def batch_run_action_in_background(
        self,
        ctx: Context,
        app_state: AppState,
        action_doc: ActionDoc,
        action_input: DataBlock,
        user_action_result: UserActionResult,
        action_result_id: Optional[str] = None,
        db: AsyncIOMotorDatabase = None,
        background_tasks: BackgroundTasks = None,
        webhook: Webhook | None = None,
        batch_size: Optional[int] = None,
    ) -> List[ActionResultDoc]:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        # space_id = space_member.space_id if space_member else None
        # Generate or fetch base title
        title = None
        if not action_result_id:
            title_data = f"Name: {action_doc.name}\nInput: {str(action_input.text if action_input.text else action_input.model_dump())}"
            default_title = (
                f"{action_doc.name} - {Clock.get_date_time().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            title_for = DataBlock(text=title_data)
            title = await LlmFunc.gen_title(
                ctx=ctx,
                app_state=app_state,
                db=self.db,
                user=self.user,
                title_for=title_for,
                default_title=default_title,
            )
        else:
            existing_result = await user_action_result.get_action_result(action_result_id)
            if existing_result and existing_result.name:
                title = existing_result.name
            else:
                title = f"{action_doc.name} - {Clock.get_date_time().strftime('%Y-%m-%d %H:%M:%S')}"

        batches: List[List[str]] = []
        if batch_size is None or batch_size <= 0 or batch_size > 45:
            batch_size = SettingsProvider.sget().RUNNABLE_BATCH_LIMIT_FOR_URLS_ACTIONS
            batch_size_source = "settings (default/capped)"
        else:
            batch_size_source = "user"
        logger.bind(ctx=ctx, code=AppCode.ACTION, batch_size=batch_size, source=batch_size_source).info(
            f"Using batch_size={batch_size} from {batch_size_source}"
        )
        if action_input.urls and len(action_input.urls) > 0:
            for i in range(0, len(action_input.urls), batch_size):
                batches.append(action_input.urls[i : i + batch_size])
            logger.bind(ctx=ctx, total_batches=len(batches)).info(
                f"Created {len(batches)} batches for action"
            )

        # Record the batch now that the fan-out width is known, and use its id as
        # the batch_id every run carries. A rerun with no URLs updates an existing
        # run instead of creating any, so it is not a batch.
        batch_id: str | None = None
        if batches or not action_result_id:
            batch_doc = await BatchService.create_batch_for_run(
                ctx=ctx,
                user=self.user,
                db=self.db,
                runnable_type=EventType.action_run,
                runnable_id=action_doc.id,
                runnable_name=action_doc.name,
                name=title or "",
                total=len(batches) if batches else 1,
                batch_size=batch_size,
            )
            batch_id = str(batch_doc.id)

        results = []
        if len(batches) > 0:
            for batch_num, batch_urls in enumerate(batches):
                logger.bind(ctx=ctx, code=AppCode.ACTION, batch_num=batch_num, total_batches=len(batches)).info(
                    f"Processing batch {batch_num + 1} of {len(batches)}"
                )

                batch_input = action_input.model_copy(deep=True)
                batch_input.urls = batch_urls

                title_with_batch = f"{batch_num + 1}/{len(batches)} {title}" if title else None

                action_doc_copy = ActionDoc.model_validate(action_doc.model_dump())
                action_doc_copy.input = batch_input.model_dump(exclude_none=True)
                action_result_create = ActionResultCreate(
                    status=EventResultStatus.processing,
                    result=action_doc_copy,
                    is_saved=False,
                    name=title_with_batch,
                    batch_id=batch_id,
                )
                batch_result_doc = await user_action_result.create_action_result(
                    action_result_create
                )
                if batch_result_doc is None:
                    logger.bind(ctx=ctx, code=AppCode.ACTION, batch_num=batch_num).error(
                        f"Failed to create action result for batch {batch_num + 1}"
                    )
                    continue
                logger.bind(
                    ctx=ctx, code=AppCode.ACTION, batch_num=batch_num, action_result_id=batch_result_doc.id
                ).info(f"Created action result for batch {batch_num + 1}")

                results.append(batch_result_doc)
        else:
            if action_result_id:
                result = await user_action_result.init(
                    app_state=app_state,
                    action_result_id=action_result_id,
                    action_doc=action_doc,
                    action_input_dict=action_input.model_dump(exclude_none=True),
                    db=db,
                    ctx=ctx,
                )

                if not ctx.consumer_id and result is not None:
                    ctx.consumer_id = result.id
                if result is not None and not ctx.action_run_name:
                    ctx.action_run_name = getattr(result, "name", None)

                app_state.action_run_id = result.id
                app_state.action_run_name = result.name
                results.append(result)
            else:
                action_doc_copy = ActionDoc.model_validate(action_doc.model_dump())
                action_doc_copy.input = action_input.model_dump(exclude_none=True)
                action_result_create = ActionResultCreate(
                    status=EventResultStatus.processing,
                    result=action_doc_copy,
                    is_saved=False,
                    name=title,
                    batch_id=batch_id,
                )
                single_result_doc = await user_action_result.create_action_result(
                    action_result_create
                )
                if single_result_doc is None:
                    logger.bind(ctx=ctx, code=AppCode.ACTION).error("Failed to create action result")
                    raise HTTPException(500, "Failed to create action result")
                results.append(single_result_doc)

        return results
