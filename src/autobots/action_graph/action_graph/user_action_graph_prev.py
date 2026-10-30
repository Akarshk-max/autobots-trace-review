import copy
from typing import List, Optional

from fastapi import BackgroundTasks, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots import SettingsProvider
from src.autobots.action.action.action_doc_model import ActionUpdate
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action_graph.action_graph.action_graph_prev import ActionGraph
from src.autobots.action_graph.action_graph.action_graph_crud import ActionGraphCRUD
from src.autobots.action_graph.action_graph.action_graph_doc_model_prev import (
    ActionGraphCreate,
    ActionGraphDoc,
    ActionGraphDocCreate,
    ActionGraphDocFind,
    ActionGraphDocsFound,
    ActionGraphDocUpdate,
    ActionGraphFind,
    ActionGraphLiteDoc,
    ActionGraphSharedWithDocFind,
    ActionGraphUpdate,
)
from src.autobots.action_graph.action_graph_result.action_graph_result_model_doc import (
    ActionGraphResultDoc,
)
from src.autobots.action_graph.action_graph_result.user_action_graph_result_prev import (
    UserActionGraphResult,
)
from src.autobots.api.webhook import Webhook
from src.autobots.conn.temporal.action_graph.shared import ActionGraphWorkflowInput
from src.autobots.conn.temporal.action_graph.workflow import ActionGraphWorkflow
from src.autobots.batch.batch_service import BatchService
from src.autobots.core.clock.clock import Clock
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.data_model.space_id import SpaceId
from src.autobots.event_result.event_result_model import EventResultStatus, EventType
from src.autobots.llm_func.llm_func import LlmFunc
from src.autobots.space.space_crud import SpaceMemberCRUD
from src.autobots.user.user import User
from src.autobots.user.user_orm_model import UserORM


class UserActionGraphs:
    def __init__(self, user: UserORM, db: AsyncIOMotorDatabase):
        self.user = user
        self.user_id = str(user.id)
        self.space_member = self.user.space_member
        self.db = db
        self.action_graph_crud = ActionGraphCRUD(self.db)

    async def create(
        self,
        action_graph_create: ActionGraphCreate,
    ) -> ActionGraphDoc:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        action_graph_doc_create = ActionGraphDocCreate(
            user_id=self.user_id, space_id=space_id, **action_graph_create.model_dump()
        )
        action_graph_doc = await self.action_graph_crud.insert_one(action_graph_doc_create)
        return action_graph_doc

    # async def list_owned_or_published(
    #         self, action_graph_find: ActionGraphFind,
    #         or_action_graph_find: List[ActionGraphFind | ActionGraphPublishedDocFind] | None = None,
    #         limit: int = 100, offset: int = 0
    # ) -> ActionGraphDocsFound:
    #     action_graph_doc_find = ActionGraphDocFind(user_id=self.user_id, **action_graph_find.model_dump())
    #     paged_docs = await self.action_graph_crud.find_page(
    #         doc_find=action_graph_doc_find,
    #         or_find_queries=or_action_graph_find,
    #         limit=limit,
    #         offset=offset
    #     )
    #     action_graph_docs = []
    #     for action_graph_doc in paged_docs.docs:
    #         action_graph_docs.append(ActionGraphLiteDoc.model_validate(action_graph_doc))
    #     return ActionGraphDocsFound(
    #         docs=action_graph_docs,
    #         total_count=paged_docs.total_count,
    #         limit=paged_docs.limit,
    #         offset=paged_docs.offset
    #     )

    async def list_owned_or_shared(
        self,
        action_graph_find: ActionGraphFind,
        or_action_graph_find: List[ActionGraphFind | ActionGraphSharedWithDocFind] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> ActionGraphDocsFound:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None

        action_graph_doc_find = ActionGraphDocFind(
            user_id=self.user_id, space_id=space_id, **action_graph_find.model_dump()
        )

        or_queries = list(or_action_graph_find) if or_action_graph_find else []
        # Space-based sharing: include resources shared with this space
        if space_id:
            or_queries.append(ActionGraphSharedWithDocFind(shared_with=SpaceId(space_id=space_id)))

        # NOTE: `offset` is treated as a page index (0-based) for API backward compatibility.
        paged_docs = await self.action_graph_crud.find_page(
            doc_find=action_graph_doc_find,
            or_find_queries=or_queries if or_queries else None,
            limit=limit,
            offset=offset,
        )
        action_graph_docs = []
        for action_graph_doc in paged_docs.docs:
            action_graph_docs.append(ActionGraphLiteDoc.model_validate(action_graph_doc))
        return ActionGraphDocsFound(
            docs=action_graph_docs,
            total_count=paged_docs.total_count,
            limit=paged_docs.limit,
            offset=paged_docs.offset,
        )

    async def get_owned(self, action_graph_id: str) -> ActionGraphDoc | None:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        action_graph_doc_find = ActionGraphDocFind(
            id=action_graph_id, user_id=self.user_id, space_id=space_id
        )
        action_graph_doc = await self.action_graph_crud.find_one(action_graph_doc_find)
        return action_graph_doc

    # async def get_owned_or_published(
    #         self, ctx: Context, action_graph_id: str
    # ) -> ActionGraphDoc | None:
    #     action_graph_doc_find = ActionGraphDocFind(id=action_graph_id, user_id=self.user_id)
    #     action_graph_published_doc_find = ActionGraphPublishedDocFind(id=action_graph_id)
    #     action_graph_doc = await self.action_graph_crud.find_one(action_graph_doc_find,
    #                                                              [action_graph_published_doc_find])
    #     synced_action_graph_doc = await self.sync_and_save_node_details_label_with_action_name(
    #         ctx=ctx, db=self.db, user=self.user, action_graph_doc=action_graph_doc
    #     )
    #     return synced_action_graph_doc

    async def get_owned_or_shared(
        self,
        ctx: Context,
        action_graph_id: str,
    ) -> ActionGraphDoc | None:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None

        assert self.user.email is not None, (
            "User email is required to get_owned_or_shared action graph"
        )
        action_graph_doc_find = ActionGraphDocFind(
            id=action_graph_id, user_id=self.user_id, space_id=space_id
        )
        or_queries = [
            ActionGraphSharedWithDocFind(
                id=action_graph_id, shared_with=self.user.email, space_id=space_id
            )
        ]
        if space_id:
            or_queries.append(
                ActionGraphSharedWithDocFind(
                    id=action_graph_id, shared_with=SpaceId(space_id=space_id)
                )
            )
        action_graph_doc: ActionGraphDoc | None = await self.action_graph_crud.find_one(
            action_graph_doc_find, or_queries
        )
        if not action_graph_doc:
            logger.bind(ctx=ctx, code=AppCode.USER_ACTION_GRAPH).error(
                f"Action Graph {action_graph_id} not found for user {self.user.email}"
            )
            return None
        synced_action_graph_doc = await self.sync_and_save_node_details_label_with_action_name(
            ctx=ctx, db=self.db, user=self.user, action_graph_doc=action_graph_doc
        )
        return synced_action_graph_doc

    # async def get_owned_or_published_by_name(
    #         self, action_graph_name: str
    # ) -> ActionGraphLiteDoc | None:
    #     selected_action_graph: ActionGraphLiteDoc | None = None
    #
    #     action_graph_doc_find = ActionGraphDocFind(name=action_graph_name, user_id=self.user_id)
    #     action_graph_doc_page = await self.action_graph_crud.find_page(action_graph_doc_find, )
    #     if len(action_graph_doc_page.docs) != 0:
    #         selected_action_graph_dict = action_graph_doc_page.docs[0]
    #         selected_action_graph = ActionGraphLiteDoc.model_validate(selected_action_graph_dict)
    #         for action_graph_doc in action_graph_doc_page.docs:
    #             action_graph_doc = ActionGraphLiteDoc.model_validate(action_graph_doc)
    #             if action_graph_doc.version > selected_action_graph.version:
    #                 selected_action_graph = action_graph_doc
    #     elif action_graph_doc_page.total_count == 0:
    #
    #         action_graph_published_doc_find = ActionGraphPublishedDocFind(name=action_graph_name)
    #         action_graph_doc_published_page = await self.action_graph_crud.find_page(
    #             action_graph_doc_find, [action_graph_published_doc_find]
    #         )
    #         if len(action_graph_doc_published_page.docs) > 1:
    #             logger.error("Multiple published action graphs found with same name")
    #         elif len(action_graph_doc_published_page.docs) == 1:
    #             selected_action_graph_dict = action_graph_doc_published_page.docs[0]
    #             selected_action_graph = ActionGraphLiteDoc.model_validate(selected_action_graph_dict)
    #         else:
    #             logger.error("No action graph found with name")
    #
    #     return selected_action_graph

    async def get_owned_or_shared_by_name(
        self,
        ctx: Context,
        action_graph_name: str,
    ) -> ActionGraphLiteDoc | None:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        selected_action_graph: ActionGraphLiteDoc | None = None

        action_graph_doc_find = ActionGraphDocFind(
            name=action_graph_name, user_id=self.user_id, space_id=space_id
        )
        action_graph_doc_page = await self.action_graph_crud.find_page(
            action_graph_doc_find,
        )
        if len(action_graph_doc_page.docs) != 0:
            selected_action_graph_dict = action_graph_doc_page.docs[0]
            selected_action_graph = ActionGraphLiteDoc.model_validate(selected_action_graph_dict)
            for action_graph_doc in action_graph_doc_page.docs:
                action_graph_doc = ActionGraphLiteDoc.model_validate(action_graph_doc)
                if action_graph_doc.version > selected_action_graph.version:
                    selected_action_graph = action_graph_doc
        elif action_graph_doc_page.total_count == 0:
            or_queries = [
                ActionGraphSharedWithDocFind(
                    name=action_graph_name, shared_with=self.user.email, space_id=space_id
                )
            ]
            if space_id:
                or_queries.append(
                    ActionGraphSharedWithDocFind(
                        name=action_graph_name, shared_with=SpaceId(space_id=space_id)
                    )
                )
            action_graph_doc_shared_page = await self.action_graph_crud.find_page(
                action_graph_doc_find, or_queries
            )
            if len(action_graph_doc_shared_page.docs) > 1:
                logger.bind(ctx=ctx, code=AppCode.USER_ACTION_GRAPH, action_graph_name=action_graph_name).error(
                    "Multiple shared action graphs found with same name"
                )
            elif len(action_graph_doc_shared_page.docs) == 1:
                selected_action_graph_dict = action_graph_doc_shared_page.docs[0]
                selected_action_graph = ActionGraphLiteDoc.model_validate(
                    selected_action_graph_dict
                )
            else:
                logger.bind(ctx=ctx, code=AppCode.USER_ACTION_GRAPH, action_graph_name=action_graph_name).error(
                    "No action graph found with name"
                )

        return selected_action_graph

    async def search(self, query: str, limit: int = 10, offset: int = 0) -> ActionGraphDocsFound:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        q = (query or "").strip()
        if not q:
            return ActionGraphDocsFound(docs=[], total_count=0, limit=limit, offset=offset)

        doc_find = ActionGraphDocFind(user_id=self.user_id, space_id=space_id)
        or_find_queries = [
            ActionGraphSharedWithDocFind(shared_with=self.user.email, space_id=space_id)
        ]
        if space_id:
            or_find_queries.append(
                ActionGraphSharedWithDocFind(shared_with=SpaceId(space_id=space_id))
            )

        docs_page = await self.action_graph_crud.search_text_page(
            doc_find=doc_find,
            or_find_queries=or_find_queries,
            q=q,
            searchable_paths=["name"],
            limit=limit,
            offset=offset,
        )
        action_graph_docs = []
        for action_graph_doc in docs_page.docs:
            action_graph_docs.append(ActionGraphLiteDoc.model_validate(action_graph_doc))
        return ActionGraphDocsFound(
            docs=action_graph_docs,
            total_count=docs_page.total_count,
            limit=docs_page.limit,
            offset=docs_page.offset,
        )

    async def update(
        self,
        app_state: AppState,
        action_graph_id: str,
        action_graph_update: ActionGraphUpdate,
    ) -> ActionGraphDoc | Exception:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        ctx = Context()
        try:
            if action_graph_update.shared_with is not None:
                await self.sync_action_for_shared_with(
                    ctx=ctx,
                    app_state=app_state,
                    action_graph_id=action_graph_id,
                    action_graph_update=action_graph_update,
                )
            action_graph_doc_update = ActionGraphDocUpdate(
                id=action_graph_id, user_id=self.user_id, **action_graph_update.model_dump()
            )
            action_graph_doc = await self.action_graph_crud.update_one(action_graph_doc_update)
            return action_graph_doc
            # if action_graph_update.is_published is not None:
            #     await self.sync_action_for_is_published(
            #         ctx=ctx, action_graph_id=action_graph_id, action_graph_update=action_graph_update
            #     )
            # action_graph_doc_update = ActionGraphDocUpdate(
            #     id=action_graph_id, user_id=self.user_id, **action_graph_update.model_dump()
            # )
            # action_graph_doc = await self.action_graph_crud.update_one(action_graph_doc_update)
            # return action_graph_doc
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.USER_ACTION_GRAPH).exception("Error in update Action Graph")
            return e

    # async def sync_action_for_is_published(
    #         self, ctx: Context, action_graph_id: str, action_graph_update: ActionGraphUpdate
    # ):
    #     try:
    #         # sync is_publish for all actions of the graph
    #         action_graph_doc = await self.get_owned(action_graph_id)
    #         if not action_graph_doc:
    #             return HTTPException(status_code=404, detail="Graph Not Found")
    #         user_actions = UserActions(user=self.user, db=self.db)
    #         action_update = ActionUpdate(is_published=action_graph_update.is_published)
    #         for node_id, action_id in action_graph_doc.nodes.items():
    #             if action_id:
    #                 action_doc = await user_actions.get_action(action_id=action_id)
    #                 if not action_doc:
    #                     return HTTPException(status_code=404, detail="Action Not Found")
    #                 if action_doc and action_doc.is_published != action_graph_update.is_published:
    #                     updated_action = await user_actions.update_action(
    #                         ctx=ctx, action_id=action_id, action_update=action_update
    #                     )
    #                     logger.bind(
    #                         ctx=ctx, action_id=updated_action.id, action_name=updated_action.name
    #                     ).info("Published Action")
    #     except Exception as e:
    #         logger.bind(ctx=ctx).exception("Error in publish Action Graph")
    #         raise e

    async def sync_action_for_shared_with(
        self,
        ctx: Context,
        app_state: AppState,
        action_graph_id: str,
        action_graph_update: ActionGraphUpdate,
    ):
        if action_graph_update.shared_with is None:
            return
        try:
            # sync share_with for all actions of the graph
            action_graph_doc = await self.get_owned(action_graph_id)
            if not action_graph_doc:
                return HTTPException(status_code=404, detail="Graph Not Found")
            user_actions = UserActions(user=self.user, db=self.db)

            old_shared_with = action_graph_doc.shared_with if action_graph_doc else []
            new_shared_with = action_graph_update.shared_with
            added_shared_with = [v for v in new_shared_with if v not in old_shared_with]
            removed_shared_with = [v for v in old_shared_with if v not in new_shared_with]

            for node_id, action_id in action_graph_doc.nodes.items():
                if action_id:
                    action_doc = await user_actions.get_action(action_id=action_id)
                    if not action_doc:
                        logger.bind(ctx=ctx, code=AppCode.USER_ACTION_GRAPH, action_id=action_id).error("Action Not Found")
                    if action_doc and (added_shared_with or removed_shared_with):
                        original_shared_with = (
                            action_doc.shared_with.copy() if action_doc.shared_with else []
                        )
                        updated_shared_with = [
                            v for v in original_shared_with if v not in removed_shared_with
                        ]
                        for v in added_shared_with:
                            if v not in updated_shared_with:
                                updated_shared_with.append(v)

                        action_update = ActionUpdate(shared_with=updated_shared_with)
                        updated_action = await user_actions.update_action(
                            ctx=ctx, app_state=app_state, action_id=action_id, action_update=action_update
                        )
                        logger.bind(
                            ctx=ctx, code=AppCode.USER_ACTION_GRAPH, action_id=updated_action.id, action_name=updated_action.name
                        ).info("Updated Shared With for Action")
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.USER_ACTION_GRAPH).exception("Error in Share of Action Graph")
            raise e

    async def sync_and_save_node_details_label_with_action_name(
        self,
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        action_graph_doc: ActionGraphDoc,
    ):
        try:
            # sync node_details.label for all actions of the graph
            user_actions = UserActions(user=user, db=db)
            is_require_update = False
            node_details = copy.deepcopy(action_graph_doc.node_details)
            for node_id, node in node_details.items():
                action_id = node.data.actionId
                if not action_id:
                    continue
                action_lite_doc = await user_actions.get_lite_action_by_id(action_id=action_id)
                if not action_lite_doc:
                    continue
                action_name = action_lite_doc.name
                label = node.data.label
                if action_name != label:
                    node.data.label = action_name
                    is_require_update = True

            if is_require_update:
                action_graph_update = ActionGraphUpdate(node_details=node_details)
                action_graph_doc_update = ActionGraphDocUpdate(
                    id=action_graph_doc.id, user_id=str(user.id), **action_graph_update.model_dump()
                )
                synced_action_graph = await self.action_graph_crud.update_one(
                    doc_update=action_graph_doc_update
                )
                logger.bind(
                    ctx=ctx,
                    code=AppCode.USER_ACTION_GRAPH,
                    action_graph_id=synced_action_graph.id,
                    action_graph_name=synced_action_graph.name,
                ).info("Updated ActionGraph for Node Detail label")
                return synced_action_graph
            else:
                return action_graph_doc
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.USER_ACTION_GRAPH).exception(
                f"Error in Updating ActionGraph for Node Detail label, returning original action_graph: {str(e)}"
            )
            return action_graph_doc

    async def delete(self, action_graph_id: str) -> int:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        action_graph_doc_find = ActionGraphDocFind(
            id=action_graph_id, user_id=self.user_id, space_id=space_id
        )
        delete_result = await self.action_graph_crud.delete_many(action_graph_doc_find)
        return delete_result.deleted_count

    # async def run(
    #         self, action_graph_id: str, input: TextObj, db: Database = Depends(get_mongo_db)
    # ) -> Dict[str, Any]:
    #     action_graph_doc_find = ActionGraphDocFind(id=action_graph_id, user_id=self.user_id)
    #     action_docs = await ActionGraphCRUD(db).find(action_graph_doc_find)
    #     if len(action_docs) != 1:
    #         raise HTTPException(405, "Action Graph cannot be run")
    #     resp = await ActionGraph.run(self.user, input, action_docs[0].nodes, action_docs[0].graph, db)
    #     return resp

    async def batch_run_in_background(
        self,
        ctx: Context,
        app_state: AppState,
        user_actions: UserActions,
        user_action_graph_result: UserActionGraphResult,
        action_graph_id: str,
        input: DataBlock,
        action_graph_result_id: Optional[str] = None,
        action_graph_node_id: Optional[str] = None,
        is_rerun_graph: bool = False,
        is_empty_downstream_only: bool = False,
        background_tasks: BackgroundTasks = None,
        webhook: Webhook | None = None,
        batch_size: Optional[int] = None,
        action_graph_doc: ActionGraphDoc | None = None,
        temporal_client=None,
        access_token: str = None,
    ) -> List[ActionGraphResultDoc]:
        if action_graph_doc is None:
            action_graph_doc = await self.get_owned_or_shared(ctx=ctx, action_graph_id=action_graph_id)
        if not action_graph_doc:
            raise HTTPException(405, "Action Graph cannot be run")

        title = None
        if not action_graph_result_id:
            title_data = f"Name: {action_graph_doc.name}\nInput: {str(input.text)}"
            default_title = (
                f"{action_graph_doc.name} - {Clock.get_date_time().strftime('%Y-%m-%d %H:%M:%S')}"
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

        batches: List[List[str]] = []
        if batch_size is None or batch_size <= 0 or batch_size > 100:
            batch_size = SettingsProvider.sget().RUNNABLE_BATCH_LIMIT_FOR_URLS
            batch_size_source = "settings (default/capped)"
        else:
            batch_size_source = "user"
        logger.bind(ctx=ctx, code=AppCode.USER_ACTION_GRAPH, batch_size=batch_size, source=batch_size_source).info(
            f"Using batch_size={batch_size} from {batch_size_source}"
        )
        for i in range(0, len(input.urls), batch_size):
            batches.append(input.urls[i : i + batch_size])
        logger.bind(ctx=ctx, code=AppCode.USER_ACTION_GRAPH, total_batches=len(batches)).info(
            f"Created batches: {len(batches)}"
        )

        # Record the batch now that the fan-out width is known, and use its id as
        # the batch_id every run carries.
        batch_doc = await BatchService.create_batch_for_run(
            ctx=ctx,
            user=self.user,
            db=self.db,
            runnable_type=EventType.action_graph_run,
            runnable_id=action_graph_doc.id,
            runnable_name=action_graph_doc.name,
            name=title or "",
            total=len(batches) if batches else 1,
            batch_size=batch_size,
        )
        batch_id: str = str(batch_doc.id)

        results = []
        if len(batches) > 0:
            for batch_num, batch_urls in enumerate(batches):
                logger.bind(ctx=ctx, code=AppCode.USER_ACTION_GRAPH, batch_num=batch_num, total_batches=len(batches)).info(
                    f"Processing batch {batch_num + 1} of {len(batches)}"
                )
                batch_input = input.model_copy(deep=True)
                batch_input.urls = batch_urls

                title_override = f"{batch_num + 1}/{len(batches)} {title}" if title else None

                result: ActionGraphResultDoc | None = await self.run_in_background(
                    ctx=ctx,
                    app_state=app_state,
                    user_actions=user_actions,
                    user_action_graph_result=user_action_graph_result,
                    action_graph_id=action_graph_id,
                    input=batch_input,
                    action_graph_result_id=action_graph_result_id,
                    action_graph_node_id=action_graph_node_id,
                    is_rerun_graph=is_rerun_graph,
                    is_empty_downstream_only=is_empty_downstream_only,
                    title_override=title_override,
                    background_tasks=background_tasks,
                    webhook=webhook,
                    temporal_client=temporal_client,
                    access_token=access_token,
                    batch_id=batch_id,
                )
                status = result.status if result else EventResultStatus.error
                logger.bind(
                    ctx=ctx,
                    code=AppCode.USER_ACTION_GRAPH,
                    batch_num=batch_num,
                    # batch_input=batch_input,
                    status=status,
                    # action_graph_run=result,
                ).info(f"Running batch {batch_num + 1} of {len(batches)}")
                # if result:
                logger.bind(
                    ctx=ctx,
                    code=AppCode.USER_ACTION_GRAPH,
                    batch_num=batch_num,
                    # batch_input=batch_input,
                    status=status,
                    # action_graph_run=result,
                ).info(f"Processed batch {batch_num + 1} of {len(batches)}")
                results.append(result)
                # logger.bind(
                #     ctx=ctx, batch_num=batch_num, batch_input=batch_input, status=status, result=result
                # ).info(f"Completed batch {batch_num + 1} of {len(batches)}")
        else:  # no batches
            result: ActionGraphResultDoc | None = await self.run_in_background(
                ctx=ctx,
                app_state=app_state,
                user_actions=user_actions,
                user_action_graph_result=user_action_graph_result,
                action_graph_id=action_graph_id,
                input=input,
                action_graph_result_id=action_graph_result_id,
                action_graph_node_id=action_graph_node_id,
                is_rerun_graph=is_rerun_graph,
                is_empty_downstream_only=is_empty_downstream_only,
                title_override=title,
                background_tasks=background_tasks,
                webhook=webhook,
                temporal_client=temporal_client,
                access_token=access_token,
                batch_id=batch_id,
            )
            logger.bind(
                ctx=ctx,
                code=AppCode.USER_ACTION_GRAPH,
                status=result.status,
                # action_graph_run=result,
            ).info("Processed graph")
            results.append(result)

        return results

    async def run_in_background(
        self,
        ctx: Context,
        app_state: AppState,
        user_actions: UserActions,
        user_action_graph_result: UserActionGraphResult,
        action_graph_id: str,
        input: DataBlock,
        action_graph_result_id: Optional[str] = None,
        action_graph_node_id: Optional[str] = None,
        is_rerun_graph: bool = False,
        is_continue_run_graph: bool = False,
        is_empty_downstream_only: bool = False,
        title_override: str | None = None,
        background_tasks: BackgroundTasks = None,
        webhook: Webhook | None = None,
        fire_and_forget: bool = False,
        action_graph_doc: ActionGraphDoc | None = None,
        temporal_client=None,
        access_token: str = None,
        batch_id: str | None = None,
    ) -> ActionGraphResultDoc | None:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        if action_graph_doc is None:
            action_graph_doc = await self.get_owned_or_shared(ctx=ctx, action_graph_id=action_graph_id)
        if not action_graph_doc:
            raise HTTPException(405, "Action Graph cannot be run")
        if not action_graph_result_id and is_continue_run_graph:
            raise HTTPException(405,"Action Graph cannot be continued until ran!!")
        if temporal_client:
            action_graph_result_doc = await user_action_graph_result.init(
                app_state=app_state,
                action_graph_result_id=action_graph_result_id,
                action_graph_doc=action_graph_doc,
                action_graph_input_dict=input.model_dump(),
                title_override=title_override,
                db=self.db,
                user=self.user,
                webhook=webhook,
                ctx=ctx,
            )
            workflow_input = ActionGraphWorkflowInput(
                action_graph_id=action_graph_doc.id,
                action_graph_result_id=action_graph_result_doc.id,
                user_id=str(self.user.id),
                space_id=action_graph_result_doc.space_id,
                trace_id=ctx.trace_id,
                access_token=access_token,
                input=input.model_dump(),
                webhook=webhook.model_dump() if webhook else None,
                action_graph_node_id=action_graph_node_id,
                is_rerun_graph=is_rerun_graph,
                is_continue_run_graph=is_continue_run_graph,
                is_empty_downstream_only=is_empty_downstream_only,
                batch_id=batch_id,
                start_to_close_timeout_minutes=SettingsProvider.sget().ACTION_GRAPH_START_TO_CLOSE_TIMEOUT_MINUTES,
            )
            await temporal_client.start_workflow(
                ActionGraphWorkflow.run,
                workflow_input,
                id=f"action-graph-{action_graph_result_doc.id}",
                task_queue=SettingsProvider.sget().TEMPORAL_TASK_QUEUE,
            )
            return action_graph_result_doc

        resp = await ActionGraph.run_in_background(
            ctx=ctx,
            app_state=app_state,
            action_graph_doc=action_graph_doc,
            action_graph_input_dict=input.model_dump(),
            user_actions=user_actions,
            user_action_graph_result=user_action_graph_result,
            action_graph_result_id=action_graph_result_id,
            action_graph_node_id=action_graph_node_id,
            is_rerun_graph=is_rerun_graph,
            is_continue_run_graph=is_continue_run_graph,
            is_empty_downstream_only=is_empty_downstream_only,
            title_override=title_override,
            background_tasks=background_tasks,
            webhook=webhook,
            fire_and_forget=fire_and_forget,
            batch_id=batch_id,
        )
        return resp

    @staticmethod
    async def run_scheduled_action_graph(
        ctx: Context,
        app_state: AppState,
        runnable_id: str,
        user_id: str,
        input: DataBlock,
        # background_tasks: BackgroundTasks = None, # Using Background thread will crash app
        run_id: Optional[str] = None,
        webhook: Webhook | None = None,
        space_id: str | None = None,
        db: AsyncIOMotorDatabase = next(get_mongo_db()),
    ) -> ActionGraphResultDoc | None:
        old_ctx = ctx
        ctx = Context(user_id=user_id)
        logger.bind(
            ctx=ctx,
            code=AppCode.USER_ACTION_GRAPH,
            old_ctx=old_ctx,
            action_graph_id=runnable_id,
            action_graph_result_id=run_id,
        ).info("Started run of scheduled graph")
        user_orm = UserORM(id=user_id)
        user_doc = await User.get(ctx=ctx, user=user_orm, id=user_id, db=db)
        user_orm = UserORM.model_validate(user_doc.model_dump())

        # Resolve space_member from stored space_id and attach to user_orm
        if space_id:
            user_orm.space_member = await SpaceMemberCRUD(db).find_membership(
                space_id=space_id, user_id=str(user_orm.id)
            )

        user_actions = UserActions(user_orm, db)
        user_action_graph_result = UserActionGraphResult(user_orm, db)
        resp = await UserActionGraphs(user=user_orm, db=db).run_in_background(
            ctx=ctx,
            app_state=app_state,
            user_actions=user_actions,
            user_action_graph_result=user_action_graph_result,
            action_graph_id=runnable_id,
            input=input,
            action_graph_result_id=run_id,
            # background_tasks,
            webhook=webhook,
        )
        logger.bind(ctx=ctx, code=AppCode.USER_ACTION_GRAPH, action_graph_id=runnable_id, action_graph_result_id=run_id).info(
            "Completed run of scheduled graph"
        )
        return resp