import time
from io import BytesIO
from typing import Any, Dict, List

from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots import SettingsProvider
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action_graph.action_graph.action_graph_doc_model import (
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
from src.autobots.event_result.turn_authorization import TurnAuthorization
from src.autobots.files.user_files import UserFiles
from src.autobots.llm_func.llm_func import LlmFunc
from src.autobots.run_metrics.run_metrics_doc_model import TERMINAL_STATUSES
from src.autobots.run_metrics.run_metrics_writer import RunMetricsWriter
from src.autobots.user.user_orm_model import UserORM


class UserActionGraphResult(TurnAuthorization):
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

    _RUN_NOT_FOUND = "Action graph result not found"

    async def _owned_or_shared_run(
        self, run_id: str, ctx: Context | None = None
    ) -> ActionGraphResultDoc | None:
        # By keyword: this lookup takes (ctx, id) while the mixin hands over
        # (run_id, ctx), and positionally the two silently trade places.
        return await self.get_owned_or_shared_action_graph_result(
            ctx=ctx, action_graph_result_id=run_id
        )

    def __init__(self, user: UserORM, db: AsyncIOMotorDatabase):
        self.user = user
        self.user_id = str(user.id)
        self.space_member = self.user.space_member
        self.event_result_crud = EventResultCRUD(db)
        # Monotonic timestamp of the last throttled save, per run. Keyed by run id rather than
        # held as a single value because one instance can outlive a single run.
        self._last_flush_at: Dict[str, float] = {}
        self.db = db
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
            return await self._read_shared_payload(
                ActionGraphResultDoc.model_validate(event_result_doc.model_dump()),
                result_id=action_graph_result_id,
                ctx=ctx,
            )
        except Exception as e:
            logger.bind(ctx=ctx).exception(
                f"Error in get_owned_or_shared_action_graph_result: {str(e)}"
            )
            return None

    async def _read_shared_payload(
        self,
        doc: ActionGraphResultDoc,
        *,
        result_id: str,
        ctx: Context,
    ) -> ActionGraphResultDoc:
        """Fetch a shared graph run's payload from the owner's S3 prefix.

        Mongo holds an `ActionGraphLiteDoc`; the full document is written to S3.
        So returning the Mongo record to a grantee hands them a run with its
        nodes and outputs missing, which renders as an empty run rather than as
        an error.

        `get_action_graph_result` already reads S3 for the owned case and cannot
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
            return ActionGraphResultDoc.model_validate_json(file_bytes)
        return doc

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

    async def _write_run_doc(self, ctx: Context, run_doc: ActionGraphResultDoc) -> None:
        """Serialise a run to S3 and its metadata projection to Mongo.

        The only place a run document is written, so `update_action_graph_result` and
        `save_run_doc` cannot produce different bytes for the same document.
        """
        run_doc.updated_at = Clock.datetime_now()
        self._stamp_execution_seconds(run_doc)

        filename = f"{run_doc.id}.json"
        filemetas = await self.fs_crud.upload_binary(
            ctx=ctx,
            file_obj=BytesIO(run_doc.model_dump_json().encode("utf-8")),
            filename=filename,
        )
        assert len(filemetas) == 1
        filemeta = filemetas[0]
        assert filemeta.url is not None, "Event result not saved to file"
        logger.bind(ctx=ctx, action_graph_result_id=run_doc.id).info(
            f"Updated action_graph_result in s3: {run_doc.id}"
        )

        # Mongo holds the metadata projection; S3 holds the full document. Dropping `output` and
        # `node_vectors` here is what that split means in practice.
        #
        # A shallow copy, not `deep=True`: the copy exists only so `result` can be swapped for the
        # lite shape without touching the caller's document, and nothing below mutates it. The deep
        # copy this replaces cloned the entire `output` map on every write only to discard it.
        db_action_graph_result = (
            run_doc.model_copy(update={"result": ActionGraphLiteDoc(**run_doc.result.model_dump())})
            if run_doc.result
            else run_doc
        )
        event_result_doc_update = EventResultDocUpdate(**db_action_graph_result.model_dump())
        event_result_doc = await self.event_result_crud.update_one(event_result_doc_update)
        logger.bind(ctx=ctx, action_graph_result_id=event_result_doc.id).info(
            f"Updated action_graph_result in db: {event_result_doc.id}"
        )
        if run_doc.status in TERMINAL_STATUSES:
            writer = RunMetricsWriter(getattr(self, "db", None))
            await writer.record_action_graph_run(ctx, run_doc)

    def _stamp_execution_seconds(self, run_doc: ActionGraphResultDoc) -> None:
        started_at: Dict[str, float] = self.__dict__.setdefault("_run_started_at", {})
        if run_doc.status not in TERMINAL_STATUSES:
            started_at.setdefault(run_doc.id, time.monotonic())
            return

        started = started_at.pop(run_doc.id, None)
        result = run_doc.result
        if started is None or not isinstance(result, ActionGraphDoc):
            return
        result.execution_seconds = (result.execution_seconds or 0.0) + (
            time.monotonic() - started
        )

    async def _bump_search_cache(self) -> None:
        """Best-effort: prevent stale search results. Never a reason to fail a write."""
        try:
            await self._bust_search_cache_for_user(self.user_id)
        except Exception:
            pass

    async def save_run_doc(
        self,
        ctx: Context,
        run_doc: ActionGraphResultDoc,
        *,
        bump_search_cache: bool = False,
        throttle: bool = False,
    ) -> None:
        """Persist a run document the caller already holds. No read-before-write.

        `update_action_graph_result` fetches the stored document first so it can merge a partial
        patch onto it. The engine has no partial patch - it mutates one document in memory and
        hands the same object back - so that fetch re-reads what the caller just had, doubling the
        S3 traffic of every run.

        Returns nothing on purpose. The engine used to rebind `action_graph_result_doc` to the
        value this call returned, which is how `action_response` - bound once to `result.output` -
        could end up aliasing a detached dict. With nothing to rebind, the document the engine
        holds stays the document that gets written.

        `bump_search_cache` is off by default: search covers `name`, `status` and `result.name`,
        none of which a per-node save changes. `throttle` spaces intermediate saves out; see
        `ACTION_GRAPH_RESULT_FLUSH_INTERVAL_SECONDS`.
        """
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")

        if throttle and not self._flush_is_due(run_doc.id):
            return

        try:
            await self._write_run_doc(ctx, run_doc)
            if bump_search_cache:
                await self._bump_search_cache()
        except Exception as e:
            logger.bind(ctx=ctx, action_graph_result_id=run_doc.id).error(
                f"Error in saving action_graph_result: {str(e)}"
            )
            raise e

    def _flush_is_due(self, run_id: str) -> bool:
        """Has enough time passed since this run's last intermediate save?

        A loop node saves after every iteration so the UI can show partial results, and each save
        rewrites a document that grows as the loop collates - so persisting n results costs about
        n^2/2 bytes. Partial results still have to survive a worker dying, so this spaces the saves
        out rather than removing them, and the unconditional node-level save that follows the loop
        bounds how much a skipped one can cost.
        """
        interval = SettingsProvider.sget().ACTION_GRAPH_RESULT_FLUSH_INTERVAL_SECONDS
        if interval <= 0:
            return True
        now = time.monotonic()
        last = self._last_flush_at.get(run_id)
        if last is not None and now - last < interval:
            return False
        self._last_flush_at[run_id] = now
        return True

    async def update_action_graph_result(
        self,
        ctx: Context,
        action_graph_result_id: str,
        action_graph_update: ActionGraphResultUpdate,
    ) -> ActionGraphResultDoc:
        """Merge a partial patch onto the stored run.

        Fetches first, because a caller holding only a patch - the `PUT /` endpoint, the Temporal
        error path - has no document to merge onto. Callers that already hold the whole document
        should use `save_run_doc` and skip the fetch.
        """
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

            await self._write_run_doc(ctx, file_action_graph_result_doc)
            await self._bump_search_cache()

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
