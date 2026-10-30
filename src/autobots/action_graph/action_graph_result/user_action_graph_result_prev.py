import time
from io import BytesIO
from typing import Any, Dict, List

from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic_extra_types.pendulum_dt import DateTime

from src.autobots import SettingsProvider
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action_graph.action_graph.action_graph_doc_model_prev import (
    ActionGraphDoc,
    ActionGraphLiteDoc,
)
from src.autobots.action_graph.action_graph_result.action_graph_result_model_doc import (
    ActionGraphResultCreate,
    ActionGraphResultDoc,
    ActionGraphResultDocsFound,
    ActionGraphResultFind,
    ActionGraphResultLiteDoc,
    ActionGraphResultUpdate,
)
from src.autobots.api.webhook import Webhook
from src.autobots.conn.aws.aws_s3 import get_s3
from src.autobots.core.cache.cache_observability import (
    format_bytes,
    get_process_rss_bytes,
    query_log_fields,
)
from src.autobots.core.cache.in_memory_ttl_cache import InMemoryTTLCache
from src.autobots.core.cache.redis_generation import (
    SearchGenerationKey,
    bump_search_generation,
    get_search_generation,
)
from src.autobots.core.clock.clock import Clock
from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_crud import EventResultCRUD
from src.autobots.event_result.event_result_model import (
    EventResultDocCreate,
    EventResultDocFind,
    EventResultDocUpdate,
    EventResultFind,
    EventResultStatus,
    EventType,
)
from src.autobots.event_result.event_result_sharing import (
    build_owned_or_shared_finds,
    build_shared_finds_for_id,
)
from src.autobots.files.user_files import UserFiles
from src.autobots.llm_func.llm_func import LlmFunc
from src.autobots.user.user_orm_model import UserORM


class UserActionGraphResult:
    _SEARCH_CACHE_LOG_EVERY: int = 200
    _SEARCH_CACHE_SET_COUNTER: int = 0

    _SEARCH_CACHE: InMemoryTTLCache[tuple[str, int, str, int, int], ActionGraphResultDocsFound] = (
        InMemoryTTLCache(ttl_seconds=60, max_items=5000)
    )

    @classmethod
    async def _bust_search_cache_for_user(cls, user_id: str) -> None:
        new_gen = await bump_search_generation(
            key=SearchGenerationKey(user_id=str(user_id), event_type=EventType.action_graph_run)
        )
        invalidated = await cls._SEARCH_CACHE.invalidate_where(lambda k: k[0] == str(user_id))
        stats = await cls._SEARCH_CACHE.stats()
        rss = get_process_rss_bytes()
        logger.bind(
            user_id=str(user_id),
            event_type=EventType.action_graph_run.value,
            new_gen=new_gen,
            invalidated_local=invalidated,
            cache_items=stats.get("items"),
            cache_approx_size=format_bytes(stats.get("approx_size_bytes")),
            process_rss=format_bytes(rss),
        ).info("Busted action_graph_results search cache")

    def __init__(self, user: UserORM, db: AsyncIOMotorDatabase):
        self.user = user
        self.user_id = str(user.id)
        self.space_member = self.user.space_member
        self.event_result_crud = EventResultCRUD(db)
        s3 = get_s3()  # get_s3(object_prefix=f"/runs/")
        self.fs_crud = UserFiles(
            user=user, s3=s3, top_level_folder=SettingsProvider.sget().AWS_S3_RUNS_FOLDER
        )

    async def create_action_graph_result(
        self,
        ctx: Context,
        action_graph_result: ActionGraphResultCreate,
    ) -> ActionGraphResultDoc | None:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        try:
            lite_action_graph_result = action_graph_result.model_copy(deep=True)
            lite_action_graph_result.result = ActionGraphLiteDoc(
                **action_graph_result.result.model_dump()
            )
            event_result_doc_create = EventResultDocCreate(
                user_id=self.user_id, space_id=space_id, **lite_action_graph_result.model_dump()
            )
            event_result_doc = await self.event_result_crud.insert_one(event_result_doc_create)
            db_action_result_doc = ActionGraphResultDoc.model_validate(
                event_result_doc.model_dump()
            )

            db_action_result_doc.result = action_graph_result.result
            filename = f"{db_action_result_doc.id}.json"
            filemetas = await self.fs_crud.upload_binary(
                ctx=ctx,
                file_obj=BytesIO(db_action_result_doc.model_dump_json().encode("utf-8")),
                filename=filename,
            )
            assert len(filemetas) == 1
            filemeta = filemetas[0]
            assert filemeta.url is not None, "Event result not saved to file"

            event_result_doc_update = EventResultDocUpdate(
                id=event_result_doc.id, user_id=self.user_id, file_url=str(filemeta.url)
            )
            event_result_doc = await self.event_result_crud.update_one(event_result_doc_update)
            _ = ActionGraphResultDoc.model_validate(event_result_doc.model_dump())

            # Best-effort cache invalidation: prevent stale search results after create.
            try:
                await self._bust_search_cache_for_user(self.user_id)
            except Exception:
                pass

            return db_action_result_doc
        except Exception as e:
            logger.bind(ctx=ctx).exception("Error while creating event result: str(e)")
            raise e

    async def list_page_action_graph_result(
        self,
        action_graph_result_find: ActionGraphResultFind,
        limit: int = 100,
        offset: int = 0,
    ) -> ActionGraphResultDocsFound:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        action_graph_result_find.type = EventType.action_graph_run
        # No exclude_none: _build_filter drops the Nones. A non-None default would hard-filter.
        event_result_doc_find = EventResultDocFind(
            user_id=self.user_id, space_id=space_id, **action_graph_result_find.model_dump()
        )
        # NOTE: `offset` is treated as a page index (0-based) for API backward compatibility.
        doc_find_page = await self.event_result_crud.find_page(
            doc_find=event_result_doc_find, limit=limit, offset=offset
        )
        action_graph_result_docs = []
        for event_result_doc in doc_find_page.docs:
            action_graph_result_lite_doc = ActionGraphResultLiteDoc.model_validate(
                event_result_doc.model_dump()
            )
            action_graph_result_docs.append(action_graph_result_lite_doc)
        return ActionGraphResultDocsFound(
            docs=action_graph_result_docs,
            total_count=doc_find_page.total_count,
            limit=doc_find_page.limit,
            offset=doc_find_page.offset,
        )

    async def list_page_owned_or_shared_action_graph_result(
        self,
        action_graph_result_find: ActionGraphResultFind,
        limit: int = 100,
        offset: int = 0,
    ) -> ActionGraphResultDocsFound:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        action_graph_result_find.type = EventType.action_graph_run

        event_result_doc_find, or_find_queries = build_owned_or_shared_finds(
            result_find=action_graph_result_find,
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
        action_graph_result_docs = []
        for event_result_doc in doc_find_page.docs:
            action_graph_result_docs.append(
                ActionGraphResultLiteDoc.model_validate(event_result_doc.model_dump())
            )
        return ActionGraphResultDocsFound(
            docs=action_graph_result_docs,
            total_count=doc_find_page.total_count,
            limit=doc_find_page.limit,
            offset=doc_find_page.offset,
        )

    async def get_owned_or_shared_action_graph_result(
        self,
        ctx: Context,
        action_graph_result_id: str,
    ) -> ActionGraphResultDoc | None:
        """Separate from `get_action_graph_result`, which gates continuing a run -
        a grantee must not be able to write into one."""
        owned = await self.get_action_graph_result(ctx, action_graph_result_id)
        if owned is not None:
            return owned
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        try:
            doc_find, or_find_queries = build_shared_finds_for_id(
                result_id=action_graph_result_id,
                event_type=EventType.action_graph_run,
                user_email=self.user.email,
                space_id=space_id,
            )
            event_result_doc = await self.event_result_crud.find_one(
                doc_find, or_find_queries
            )
            if event_result_doc is None:
                return None
            return ActionGraphResultDoc.model_validate(event_result_doc.model_dump())
        except Exception as e:
            logger.bind(ctx=ctx).exception(
                f"Error in get_owned_or_shared_action_graph_result: {str(e)}"
            )
            return None

    async def list_action_graph_result(
        self,
        action_graph_result_find: EventResultFind,
        limit: int = 100,
        offset: int = 0,
    ) -> List[ActionGraphResultDoc]:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        action_graph_result_find.type = EventType.action_graph_run
        event_result_doc_find = EventResultDocFind(
            user_id=self.user_id, space_id=space_id, **action_graph_result_find.model_dump()
        )
        event_result_docs = await self.event_result_crud.find(
            event_result_doc_find, limit=limit, offset=offset
        )
        action_graph_result_docs = []
        for event_result_doc in event_result_docs:
            action_graph_result_doc = ActionGraphResultDoc.model_validate(
                event_result_doc.model_dump()
            )
            action_graph_result_docs.append(action_graph_result_doc)
        return action_graph_result_docs

    async def get_action_graph_result_mongo(
        self,
        ctx: Context,
        action_graph_result_id: str,
        scrub_ip: bool = True,
    ) -> ActionGraphResultDoc | None:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        try:
            event_result_doc_find = EventResultDocFind(
                id=action_graph_result_id, user_id=self.user_id, space_id=space_id
            )
            event_result_doc = await self.event_result_crud.find_one(event_result_doc_find)
            if event_result_doc is None:
                return None
            action_graph_result_doc: ActionGraphResultDoc = ActionGraphResultDoc.model_validate(
                event_result_doc.model_dump()
            )
            if scrub_ip:
                await self.scrub_ip(ctx=ctx, action_graph_result_docs=[action_graph_result_doc])
            return action_graph_result_doc
        except Exception as e:
            logger.error(str(e))
        return None

    async def get_action_graph_result(
        self,
        ctx: Context,
        action_graph_result_id: str,
        scrub_ip: bool = True,
    ) -> ActionGraphResultDoc | None:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        try:
            filename = f"{action_graph_result_id}.json"
            file_bytes = await self.fs_crud.get_bytes(ctx=ctx, filename=filename)
            action_graph_result_doc: ActionGraphResultDoc | None = None
            if file_bytes:
                action_graph_result_doc = ActionGraphResultDoc.model_validate_json(file_bytes)

            else:
                event_result_doc_find = EventResultDocFind(
                    id=action_graph_result_id, user_id=self.user_id, space_id=space_id
                )
                event_result_doc = await self.event_result_crud.find_one(event_result_doc_find)
                if event_result_doc:
                    action_graph_result_doc: ActionGraphResultDoc = (
                        ActionGraphResultDoc.model_validate(event_result_doc.model_dump())
                    )

            if action_graph_result_doc is None:
                raise HTTPException(status_code=404, detail="Result not found")

            if scrub_ip:
                await self.scrub_ip(ctx=ctx, action_graph_result_docs=[action_graph_result_doc])
            return action_graph_result_doc
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in getting action_graph result: {str(e)}")
        return None

    async def search(
        self,
        query: str,
        limit: int = 10,
        offset: int = 0,
    ) -> ActionGraphResultDocsFound:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        q = (query or "").strip()
        if not q:
            return ActionGraphResultDocsFound(docs=[], total_count=0, limit=limit, offset=offset)

        q_fields = query_log_fields(q)
        t0 = time.perf_counter()

        gen = await get_search_generation(
            key=SearchGenerationKey(user_id=self.user_id, event_type=EventType.action_graph_run)
        )
        cache_gen = int(gen)
        cache_key = (self.user_id, cache_gen, q.lower(), int(limit), int(offset))

        cached = await self._SEARCH_CACHE.get(cache_key)
        if cached is not None:
            logger.bind(
                user_id=self.user_id,
                event_type=EventType.action_graph_run.value,
                cache_gen=cache_gen,
                limit=limit,
                offset=offset,
                **q_fields,
            ).debug("ActionGraphResults search cache hit")
            return cached

        logger.bind(
            user_id=self.user_id,
            event_type=EventType.action_graph_run.value,
            cache_gen=cache_gen,
            limit=limit,
            offset=offset,
            **q_fields,
        ).debug("ActionGraphResults search cache miss; querying Mongo")

        t_db0 = time.perf_counter()
        docs_page = await self.event_result_crud.search_custom_page(
            user_id=self.user_id,
            event_type=EventType.action_graph_run,
            q=q,
            limit=limit,
            offset=offset,
            searchable_paths=["name", "status", "result.name"],
        )
        mongo_ms = (time.perf_counter() - t_db0) * 1000.0

        action_graph_result_docs = []
        for event_result_doc in docs_page.docs:
            try:
                action_graph_result_doc = ActionGraphResultLiteDoc.model_validate(
                    event_result_doc.model_dump()
                )
                action_graph_result_docs.append(action_graph_result_doc)
            except Exception as e:
                logger.bind(user_id=self.user_id, **q_fields).exception(
                    f"Error in ActionGraphResults search doc parse: {str(e)}"
                )

        result = ActionGraphResultDocsFound(
            docs=action_graph_result_docs,
            total_count=docs_page.total_count,
            limit=docs_page.limit,
            offset=docs_page.offset,
        )
        await self._SEARCH_CACHE.set(cache_key, result)

        total_ms = (time.perf_counter() - t0) * 1000.0
        log = logger.bind(
            user_id=self.user_id,
            event_type=EventType.action_graph_run.value,
            cache_gen=cache_gen,
            limit=limit,
            offset=offset,
            returned=len(action_graph_result_docs),
            total_count=result.total_count,
            mongo_ms=round(mongo_ms, 2),
            total_ms=round(total_ms, 2),
            **q_fields,
        )
        if total_ms >= 750:
            log.warning("ActionGraphResults search slow")
        else:
            log.debug("ActionGraphResults search completed")

        cls = self.__class__
        cls._SEARCH_CACHE_SET_COUNTER += 1
        if cls._SEARCH_CACHE_SET_COUNTER % cls._SEARCH_CACHE_LOG_EVERY == 0:
            stats = await cls._SEARCH_CACHE.stats()
            rss = get_process_rss_bytes()
            logger.bind(
                user_id=self.user_id,
                event_type=EventType.action_graph_run.value,
                cache_gen=cache_gen,
                cache_items=stats.get("items"),
                cache_approx_size=format_bytes(stats.get("approx_size_bytes")),
                process_rss=format_bytes(rss),
            ).info("ActionGraphResults search cache memory snapshot")

        return result

    async def update_action_graph_result(
        self,
        ctx: Context,
        action_graph_result_id: str,
        action_graph_update: ActionGraphResultUpdate,
    ) -> ActionGraphResultDoc:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        try:
            # s3
            file_action_graph_result_doc: (
                ActionGraphResultDoc | None
            ) = await self.get_action_graph_result(
                ctx=ctx,
                action_graph_result_id=action_graph_result_id,
                scrub_ip=False,
            )
            file_action_graph_result_doc = file_action_graph_result_doc.model_copy(
                update=action_graph_update.model_dump(
                    exclude_unset=True, exclude_none=True, exclude={"result"}
                ),
            )
            file_action_graph_result_doc.result = ActionGraphDoc.model_validate(
                file_action_graph_result_doc.result
            )
            if action_graph_update.result:
                file_action_graph_result_doc.result = action_graph_update.result
            updated_at: DateTime = Clock.datetime_now()
            file_action_graph_result_doc.updated_at = updated_at
            filename = f"{file_action_graph_result_doc.id}.json"
            filemetas = await self.fs_crud.upload_binary(
                ctx=ctx,
                file_obj=BytesIO(file_action_graph_result_doc.model_dump_json().encode("utf-8")),
                filename=filename,
            )
            assert len(filemetas) == 1
            filemeta = filemetas[0]
            assert filemeta.url is not None, "Event result not saved to file"
            logger.bind(
                ctx=ctx,
                action_graph_result_id=file_action_graph_result_doc.id,
            ).info(f"Updated action_graph_result in s3: {file_action_graph_result_doc.id}")

            # mongo
            db_action_graph_result = file_action_graph_result_doc.model_copy(deep=True)
            if file_action_graph_result_doc.result:
                db_action_graph_result.result = ActionGraphLiteDoc(
                    **file_action_graph_result_doc.result.model_dump()
                )
            event_result_doc_update = EventResultDocUpdate(
                # id=action_graph_result_id, user_id=self.user_id,
                **db_action_graph_result.model_dump()
            )
            event_result_doc = await self.event_result_crud.update_one(event_result_doc_update)
            db_action_graph_result_doc = ActionGraphResultDoc.model_validate(
                event_result_doc.model_dump()
            )
            logger.bind(
                ctx=ctx,
                action_graph_result_id=db_action_graph_result_doc.id,
            ).info(f"Updated action_graph_result in db: {db_action_graph_result_doc.id}")

            # Best-effort cache invalidation: prevent stale search results after update.
            try:
                await self._bust_search_cache_for_user(self.user_id)
            except Exception:
                pass

            return file_action_graph_result_doc

            #########

            # lite_action_graph_result_update = action_graph_update.model_copy(deep=True)
            # if action_graph_update.result:
            #     lite_action_graph_result_update.result = ActionGraphLiteDoc(**action_graph_update.result.model_dump())
            # event_result_doc_update = EventResultDocUpdate(
            #     id=action_graph_result_id, user_id=self.user_id, **lite_action_graph_result_update.model_dump()
            # )
            # event_result_doc = await self.event_result_crud.update_one(event_result_doc_update)
            # db_action_graph_result_doc = ActionGraphResultDoc.model_validate(
            #     event_result_doc.model_dump()
            # )
            #
            # file_action_graph_result_doc: ActionGraphResultDoc | None = await self.get_action_graph_result(
            #     ctx=ctx, action_graph_result_id=action_graph_result_id
            # )
            # file_action_graph_result_doc = file_action_graph_result_doc.model_copy(
            #     update=db_action_graph_result_doc.model_dump(exclude_unset=True, exclude_none=True, exclude={"result"}),
            # )
            # file_action_graph_result_doc.result = ActionGraphDoc.model_validate(file_action_graph_result_doc.result)
            # if action_graph_update.result:
            #     file_action_graph_result_doc.result = action_graph_update.result
            #
            # filename = f"{file_action_graph_result_doc.id}.json"
            # filemetas = await self.fs_crud.upload_binary(
            #     file_obj=BytesIO(file_action_graph_result_doc.model_dump_json().encode("utf-8")),
            #     filename=filename,
            # )
            # assert len(filemetas) == 1
            # filemeta = filemetas[0]
            # assert filemeta.url is not None, "Event result not saved to file"
            #
            # return file_action_graph_result_doc
        except Exception as e:
            logger.bind(ctx=ctx).error(f"Error in updating action_graph_result: {str(e)}")
            raise e

    async def delete_action_graph_result(self, id: str) -> int:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        event_result_doc_find = EventResultDocFind(id=id, user_id=self.user_id, space_id=space_id)
        delete_result = await self.event_result_crud.delete_many(event_result_doc_find)

        # Best-effort cache invalidation: prevent stale search results after delete.
        try:
            await self._bust_search_cache_for_user(self.user_id)
        except Exception:
            pass

        return delete_result.deleted_count

    # async def _remove_ip_from_published_graphs(self, action_graph_result_docs: List[ActionGraphResultDoc]) -> None:
    #     # User will not see Config objects of Nodes in a Published Graph
    #     for action_graph_result_doc in action_graph_result_docs:
    #         try:
    #             action_graph_doc: ActionGraphDoc = action_graph_result_doc.result
    #             if action_graph_doc.user_id != self.user_id:
    #                 for node_id in action_graph_doc.output.keys():
    #                     action_graph_doc.output.get(node_id).config = {}
    #         except Exception as e:
    #             logger.exception(str(e))

    async def scrub_ip(
        self, ctx: Context, action_graph_result_docs: List[ActionGraphResultDoc]
    ) -> None:
        # User will not see Config objects of Nodes in a Shared Graph
        for action_graph_result_doc in action_graph_result_docs:
            try:
                action_graph_doc: ActionGraphDoc = action_graph_result_doc.result
                if action_graph_doc.user_id != self.user_id:
                    for node_id in action_graph_doc.output.keys():
                        logger.bind(ctx=ctx, node_id=node_id).info("Removing IP from shared graph")
                        action_graph_doc.output.get(node_id).config = {}
            except Exception as e:
                logger.exception(str(e))
                

    async def init(
        self,
        app_state: AppState,
        action_graph_result_id: str | None,
        action_graph_doc: ActionGraphDoc,
        action_graph_input_dict: Dict[str, Any],
        title_override:str | None,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        webhook: Webhook | None = None,
        ctx: Context | None = None,
        batch_id: str | None = None,
    ) -> ActionGraphResultDoc | None:
        action_graph_result_doc: ActionGraphResultDoc | None = None
        if not action_graph_result_id:
            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).info("Creating Action Graph Run")
            # Create initial Action Graph Result if not provided
            action_graph_doc.input = action_graph_input_dict
            action_graph_doc.output = {}
            action_graph_result_create: ActionGraphResultCreate = ActionGraphResultCreate(
                status=EventResultStatus.processing,
                result=action_graph_doc,
                is_saved=False,
                batch_id=batch_id,
            )
            if title_override:
                action_graph_result_create.name = title_override
            elif action_graph_input_dict:  # gen title
                data = f"Name: {action_graph_doc.name}\nInput: {str(action_graph_input_dict)}"
                default_title = f"{action_graph_doc.name} - {Clock.get_date_time().strftime('%Y-%m-%d %H:%M:%S')}"
                action_result_name = await LlmFunc.gen_title(
                    ctx=ctx,
                    app_state=app_state,
                    db=db,
                    user=user,
                    title_for=DataBlock(text=data),
                    default_title=default_title,
                )
                action_graph_result_create.name = action_result_name

            action_graph_result_doc = await self.create_action_graph_result(
                ctx=ctx, action_graph_result=action_graph_result_create
            )
            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH, action_graph_result_doc_id=action_graph_result_doc.id).info(
                "Created Action Graph Run"
            )
            if webhook:
                await webhook.send(action_graph_result_doc.model_dump())
        else:
            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).info("Find and use Action Graph Result")
            # find and use Action Graph Result
            action_graph_result_doc = await self.get_action_graph_result(
                ctx=ctx, action_graph_result_id=action_graph_result_id, scrub_ip=False
            )

            try:
                # Merging input already present in Action Graph Result if action_graph_input is empty
                action_graph_input = DataBlock.model_validate(action_graph_input_dict)
                if (
                    action_graph_result_doc.result.input
                    and action_graph_input.is_empty()
                    and not action_graph_result_doc.result.output
                ):
                    existing_input = DataBlock.model_validate(
                        action_graph_result_doc.result.input
                    )
                    existing_input.merge([action_graph_input])
                    action_graph_input_dict = existing_input.model_dump()
            except Exception as e:
                logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).error(f"Error merging input to Action Graph Result {e}")

            # Change status back to processing
            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).info("Updating status of Action Graph Result to processing")
            action_graph_result_update: ActionGraphResultUpdate = ActionGraphResultUpdate(
                status=EventResultStatus.processing
            )
            if title_override:
                action_graph_result_update.name = title_override
            elif not action_graph_result_doc.name and action_graph_input_dict:  # gen title
                title_data = f"Name: {action_graph_result_doc.result.name}\nInput: {str(action_graph_input_dict)}"
                default_title = f"{action_graph_result_doc.result.name} - {Clock.get_date_time().strftime('%Y-%m-%d %H:%M:%S')}"
                action_result_name = await LlmFunc.gen_title(
                    ctx=ctx,
                    app_state=app_state,
                    db=db,
                    user=user,
                    title_for=DataBlock(text=title_data),
                    default_title=default_title,
                )
                action_graph_result_update.name = action_result_name

            action_graph_result_doc = await self.update_action_graph_result(
                ctx, action_graph_result_id, action_graph_result_update
            )
        return action_graph_result_doc