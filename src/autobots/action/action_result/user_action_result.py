import random
import time
from io import BytesIO
from typing import Any, Dict

from bson import ObjectId
from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic_extra_types.pendulum_dt import DateTime
from pymongo import ReturnDocument

from src.autobots import SettingsProvider
from src.autobots.action.action.action_doc_model import ActionDoc, ActionLiteDoc, ActionResult
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_result.action_result_doc_model import (
    ActionResultCreate,
    ActionResultDoc,
    ActionResultDocsFound,
    ActionResultFind,
    ActionResultLiteDoc,
    ActionResultUpdate,
)
from src.autobots.conn.aws.aws_s3 import get_s3
from src.autobots.core.cache.cache_observability import (
    format_bytes,
    get_process_rss_bytes,
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


class UserActionResult(TurnAuthorization):
    _SEARCH_CACHE_LOG_EVERY: int = 200
    _SEARCH_CACHE_SET_COUNTER: int = 0

    _SEARCH_CACHE: InMemoryTTLCache[tuple[str, int, str, int, int], ActionResultDocsFound] = (
        InMemoryTTLCache(ttl_seconds=60, max_items=5000)
    )

    @classmethod
    async def _bust_search_cache_for_user(cls, user_id: str) -> None:
        await bump_search_generation(
            key=SearchGenerationKey(user_id=str(user_id), event_type=EventType.action_run)
        )
        await cls._SEARCH_CACHE.invalidate_where(lambda k: k[0] == str(user_id))
        stats = await cls._SEARCH_CACHE.stats()
        rss = get_process_rss_bytes()
        logger.bind(
            user_id=str(user_id),
            code=AppCode.USER_ACTION_RESULT,
            event_type=EventType.action_run.value,
            cache_approx_size=format_bytes(stats.get("approx_size_bytes")),
            process_rss=format_bytes(rss),
        ).info("Busted action_results search cache")

    def __init__(self, user: UserORM, db: AsyncIOMotorDatabase):
        self.user = user
        self.user_id = str(user.id)
        self.space_member = self.user.space_member
        self.event_result_crud = EventResultCRUD(db)

        s3 = get_s3()
        self.fs_crud = UserFiles(
            user=user,
            s3=s3,
            top_level_folder=SettingsProvider.sget().AWS_S3_ACTION_RUNS_FOLDER,
        )

    @staticmethod
    def _ensure_ctx(ctx: Context | None, *, user_id: str) -> Context:
        return ctx if ctx is not None else Context(user_id=user_id)

    @staticmethod
    def _filename(action_result_id: str) -> str:
        return f"{action_result_id}.json"

    @staticmethod
    def _coerce_mongo_id(id_value: str | ObjectId) -> str | ObjectId:
        try:
            return ObjectId(str(id_value))
        except Exception:
            return str(id_value)

    async def _mongo_set_fields(
        self,
        ctx: Context,
        action_result_id: str,
        set_fields: dict,
    ) -> dict:
        """Update EventResults doc without ever trying to $set the immutable _id field."""
        mongo_id = self._coerce_mongo_id(action_result_id)
        updated_doc = await self.event_result_crud.document.find_one_and_update(
            filter={"_id": mongo_id, "user_id": self.user_id},
            update={"$set": set_fields},
            return_document=ReturnDocument.AFTER,
        )
        if updated_doc is None:
            raise HTTPException(status_code=404, detail="Action Result not found")
        updated_doc["_id"] = str(updated_doc.get("_id"))
        return updated_doc

    async def _rollback_s3_write(
        self, ctx: Context, action_result_id: str, filename: str, existing_s3_bytes: bytes | None
    ) -> None:
        try:
            if existing_s3_bytes is not None:
                await self.fs_crud.upload_binary(
                    ctx=ctx,
                    file_obj=BytesIO(existing_s3_bytes),
                    filename=filename,
                )
            else:
                await self.fs_crud.delete_files(ctx=ctx, filenames=[filename])
        except Exception as rollback_e:  # pragma: no cover
            logger.bind(ctx=ctx, code=AppCode.USER_ACTION_RESULT, action_result_id=action_result_id).warning(
                f"S3 rollback failed (continuing): {str(rollback_e)}"
            )

    async def _mongo_delete_one(self, ctx: Context, action_result_id: str) -> None:
        mongo_id = self._coerce_mongo_id(action_result_id)
        await self.event_result_crud.document.delete_one({"_id": mongo_id, "user_id": self.user_id})
        return

    async def create_action_result(
        self,
        action_result: ActionResultCreate,
        ctx: Context | None = None,
    ) -> ActionResultDoc | None:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        ctx = self._ensure_ctx(ctx, user_id=self.user_id)
        action_result_id: str | None = None
        filename: str | None = None
        try:
            lite_action_result = action_result.model_copy(deep=True)
            lite_action_result.result = ActionLiteDoc(**action_result.result.model_dump())
            event_result_doc_create = EventResultDocCreate(
                user_id=self.user_id, space_id=space_id, **lite_action_result.model_dump()
            )
            event_result_doc = await self.event_result_crud.insert_one(event_result_doc_create)
            db_action_result_doc = ActionResultDoc.model_validate(event_result_doc.model_dump())
            action_result_id = db_action_result_doc.id

            db_action_result_doc.result = action_result.result
            filename = self._filename(action_result_id)
            try:
                filemetas = await self.fs_crud.upload_binary(
                    ctx=ctx,
                    file_obj=BytesIO(db_action_result_doc.model_dump_json().encode("utf-8")),
                    filename=filename,
                )
            except Exception as e:
                logger.bind(ctx=ctx,code=AppCode.USER_ACTION_RESULT, action_result_id=action_result_id).warning(
                    f"S3 upload failed; rolling back Mongo insert: {str(e)}"
                )
                try:
                    await self._mongo_delete_one(ctx=ctx, action_result_id=action_result_id)
                except Exception as rollback_e:  # pragma: no cover
                    logger.bind(ctx=ctx, code=AppCode.USER_ACTION_RESULT, action_result_id=action_result_id).warning(
                        f"Mongo rollback failed (continuing): {str(rollback_e)}"
                    )
                raise
            assert len(filemetas) == 1
            file_url = str(filemetas[0].url)
            assert file_url, "ActionResult not saved to file"
            db_action_result_doc.file_url = file_url

            try:
                await self._mongo_set_fields(
                    ctx=ctx, action_result_id=action_result_id, set_fields={"file_url": file_url}
                )
            except Exception as e:
                logger.bind(ctx=ctx, code=AppCode.USER_ACTION_RESULT, action_result_id=action_result_id).warning(
                    f"Mongo file_url update failed; rolling back S3+Mongo: {str(e)}"
                )
                try:
                    if filename is not None:
                        await self.fs_crud.delete_files(ctx=ctx, filenames=[filename])
                except Exception as rollback_e:  # pragma: no cover
                    logger.bind(ctx=ctx, code=AppCode.USER_ACTION_RESULT, action_result_id=action_result_id).warning(
                        f"S3 rollback failed (continuing): {str(rollback_e)}"
                    )
                try:
                    await self._mongo_delete_one(ctx=ctx, action_result_id=action_result_id)
                except Exception as rollback_e:  # pragma: no cover
                    logger.bind(ctx=ctx, code=AppCode.USER_ACTION_RESULT, action_result_id=action_result_id).warning(
                        f"Mongo rollback failed (continuing): {str(rollback_e)}"
                    )
                raise

            # Best-effort cache invalidation: prevent stale search results after create.
            try:
                await self._bust_search_cache_for_user(self.user_id)
            except Exception:
                pass

            return db_action_result_doc
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.USER_ACTION_RESULT, action_result_id=action_result_id).exception(
                f"Error while creating action result: {str(e)}"
            )
            raise

    async def list_action_result(
        self,
        action_result_find: ActionResultFind,
        limit: int = 100,
        offset: int = 0,
    ) -> ActionResultDocsFound:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        action_result_find.type = EventType.action_run
        event_result_doc_find = EventResultDocFind(
            user_id=self.user_id,
            space_id=space_id,
            **action_result_find.model_dump(exclude_none=True),
        )
        # NOTE: `offset` is treated as a page index (0-based) for API backward compatibility.
        event_result_docs_page = await self.event_result_crud.find_page(
            doc_find=event_result_doc_find, limit=limit, offset=offset
        )
        action_result_docs = []
        for event_result_doc in event_result_docs_page.docs:
            try:
                action_result_doc = ActionResultLiteDoc.model_validate(
                    event_result_doc.model_dump()
                )
                action_result_docs.append(action_result_doc)
            except Exception as e:
                logger.exception(f"Error in list_action_result: {str(e)}")
        return ActionResultDocsFound(
            docs=action_result_docs,
            total_count=event_result_docs_page.total_count,
            limit=event_result_docs_page.limit,
            offset=event_result_docs_page.offset,
        )

    async def list_owned_or_shared_action_results(
        self,
        action_result_find: ActionResultFind,
        limit: int = 100,
        offset: int = 0,
    ) -> ActionResultDocsFound:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        action_result_find.type = EventType.action_run

        event_result_doc_find, or_find_queries = build_owned_or_shared_finds(
            result_find=action_result_find,
            user_id=self.user_id,
            user_email=self.user.email,
            space_id=space_id,
        )
        event_result_docs_page = await self.event_result_crud.find_page(
            doc_find=event_result_doc_find,
            or_find_queries=or_find_queries,
            limit=limit,
            offset=offset,
        )
        action_result_docs = []
        for event_result_doc in event_result_docs_page.docs:
            try:
                action_result_docs.append(
                    ActionResultLiteDoc.model_validate(event_result_doc.model_dump())
                )
            except Exception as e:
                logger.exception(f"Error in list_owned_or_shared_action_results: {str(e)}")
        return ActionResultDocsFound(
            docs=action_result_docs,
            total_count=event_result_docs_page.total_count,
            limit=event_result_docs_page.limit,
            offset=event_result_docs_page.offset,
        )

    async def get_owned_or_shared_action_result(
        self,
        action_result_id: str,
        ctx: Context | None = None,
    ) -> ActionResultDoc | None:
        """Separate from `get_action_result`, which gates continuing a run - a
        grantee must not be able to write into one."""
        owned = await self.get_action_result(action_result_id, ctx)
        if owned is not None:
            return owned
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        try:
            doc_find, or_find_queries = build_shared_finds_for_id(
                result_id=action_result_id,
                event_type=EventType.action_run,
                user_email=self.user.email,
                space_id=space_id,
            )
            event_result_doc = await self.event_result_crud.find_one(
                doc_find, or_find_queries
            )
            if event_result_doc is None:
                return None
            return await self._read_shared_payload(
                ActionResultDoc.model_validate(event_result_doc.model_dump()),
                action_result_id=action_result_id,
                ctx=ctx,
            )
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.USER_ACTION_RESULT).exception(
                f"Error in get_owned_or_shared_action_result: {str(e)}"
            )
            return None

    _RUN_NOT_FOUND = "Action result not found"

    async def _owned_or_shared_run(
        self, run_id: str, ctx: Context | None = None
    ) -> ActionResultDoc | None:
        return await self.get_owned_or_shared_action_result(run_id, ctx)

    async def _read_shared_payload(
        self,
        doc: ActionResultDoc,
        *,
        action_result_id: str,
        ctx: Context | None = None,
    ) -> ActionResultDoc:
        """Fetch a shared run's turns from the owner's S3 prefix.

        Mongo does not hold the conversation. `update_action_result` writes the
        full `ActionDoc` to S3 and stores an `ActionLiteDoc` alongside it, and a
        lite doc has no `results` - so handing the Mongo document to a grantee
        gives them a run with no turns in it, which renders as "no conversation
        data" on a run that plainly has one.

        The owned path already does this, in `get_action_result`. It cannot be
        reused as-is because `UserFiles` prefixes every key with the id of the
        user it was built for: `self.fs_crud` points at the *caller's* prefix,
        and a shared payload lives under the owner's. Hence a second reader,
        built for `doc.user_id`.

        Reached only after `build_shared_finds_for_id` has matched the caller
        against the run's grant list, so the authorization decision is already
        made by the time this reads somebody else's object.
        """
        file_url = (getattr(doc, "file_url", None) or "").strip()
        if not file_url:
            return doc

        owner_files = UserFiles(
            user=UserORM(id=doc.user_id),
            s3=get_s3(),
            top_level_folder=SettingsProvider.sget().AWS_S3_ACTION_RUNS_FOLDER,
        )
        file_bytes = await owner_files.get_bytes(
            ctx=ctx, filename=self._filename(action_result_id)
        )
        if file_bytes:
            return ActionResultDoc.model_validate_json(file_bytes)

        raise HTTPException(
            status_code=404, detail="Action result payload missing in S3"
        )

    async def get_action_result_mongo(
        self,
        action_result_id: str,
        ctx: Context | None = None,
    ) -> ActionResultDoc | None:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        ctx = self._ensure_ctx(ctx, user_id=self.user_id)
        try:
            event_result_doc_find = EventResultDocFind(
                id=action_result_id, user_id=self.user_id, space_id=space_id
            )
            event_result_doc = await self.event_result_crud.find_one(event_result_doc_find)
            if event_result_doc is None:
                return None
            return ActionResultDoc.model_validate(event_result_doc.model_dump())
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.USER_ACTION_RESULT, action_result_id=action_result_id).exception(
                f"Error in get_action_result_mongo: {str(e)}"
            )
            return None

    async def get_action_result(
        self,
        action_result_id: str,
        ctx: Context | None = None,
    ) -> ActionResultDoc | None:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        ctx = self._ensure_ctx(ctx, user_id=self.user_id)

        mongo_doc = await self.get_action_result_mongo(
            action_result_id=action_result_id,
            ctx=ctx,
        )
        if mongo_doc is None:
            return None

        file_url = (getattr(mongo_doc, "file_url", None) or "").strip()
        if not file_url:
            return mongo_doc

        filename = self._filename(action_result_id)
        file_bytes = await self.fs_crud.get_bytes(ctx=ctx, filename=filename)
        if file_bytes:
            return ActionResultDoc.model_validate_json(file_bytes)

        raise HTTPException(status_code=404, detail="Action result payload missing in S3")

    async def search(
        self,
        query: str,
        limit: int = 10,
        offset: int = 0,
    ) -> ActionResultDocsFound:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        q = (query or "").strip()
        if not q:
            return ActionResultDocsFound(docs=[], total_count=0, limit=limit, offset=offset)

        t0 = time.perf_counter()

        gen = await get_search_generation(
            key=SearchGenerationKey(user_id=self.user_id, event_type=EventType.action_run)
        )
        cache_gen = int(gen)
        cache_key = (self.user_id, cache_gen, q.lower(), int(limit), int(offset))

        cached = await self._SEARCH_CACHE.get(cache_key)
        if cached is not None:
            logger.bind(
                user_id=self.user_id,
                code=AppCode.USER_ACTION_RESULT,
                event_type=EventType.action_run.value,
                limit=limit,
                offset=offset,
            ).debug("ActionResults search cache hit")
            return cached

        logger.bind(
            user_id=self.user_id,
            code=AppCode.USER_ACTION_RESULT,
            event_type=EventType.action_run.value,
            limit=limit,
            offset=offset,
        ).debug("ActionResults search cache miss; querying Mongo")

        t_db0 = time.perf_counter()
        event_result_docs_page = await self.event_result_crud.search_custom_page(
            user_id=self.user_id,
            event_type=EventType.action_run,
            q=q,
            limit=limit,
            offset=offset,
            searchable_paths=["name", "status", "result.name", "result.type"],
        )
        mongo_ms = (time.perf_counter() - t_db0) * 1000.0

        action_result_docs = []
        for event_result_doc in event_result_docs_page.docs:
            try:
                action_result_doc = ActionResultLiteDoc.model_validate(
                    event_result_doc.model_dump()
                )
                action_result_docs.append(action_result_doc)
            except Exception as e:
                logger.bind(user_id=self.user_id, code=AppCode.USER_ACTION_RESULT).exception(
                    f"Error in ActionResults search doc parse: {str(e)}"
                )

        result = ActionResultDocsFound(
            docs=action_result_docs,
            total_count=event_result_docs_page.total_count,
            limit=event_result_docs_page.limit,
            offset=event_result_docs_page.offset,
        )
        await self._SEARCH_CACHE.set(cache_key, result)

        total_ms = (time.perf_counter() - t0) * 1000.0
        log = logger.bind(
            user_id=self.user_id,
            code=AppCode.USER_ACTION_RESULT,
            event_type=EventType.action_run.value,
            limit=limit,
            offset=offset,
            returned=len(action_result_docs),
            total_count=result.total_count,
            mongo_ms=round(mongo_ms, 2),
            total_ms=round(total_ms, 2),
        )
        if total_ms >= 750:
            log.warning("ActionResults search slow")
        else:
            log.debug("ActionResults search completed")

        cls = self.__class__
        cls._SEARCH_CACHE_SET_COUNTER += 1
        if cls._SEARCH_CACHE_SET_COUNTER % cls._SEARCH_CACHE_LOG_EVERY == 0:
            stats = await cls._SEARCH_CACHE.stats()
            rss = get_process_rss_bytes()
            logger.bind(
                user_id=self.user_id,
                code=AppCode.USER_ACTION_RESULT,
                event_type=EventType.action_run.value,
                cache_approx_size=format_bytes(stats.get("approx_size_bytes")),
                process_rss=format_bytes(rss),
            ).info("ActionResults search cache memory snapshot")

        return result

    async def update_action_result(
        self,
        action_result_id: str,
        action_update: ActionResultUpdate,
        ctx: Context | None = None,
    ) -> ActionResultDoc:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        ctx = self._ensure_ctx(ctx, user_id=self.user_id)
        filename = self._filename(action_result_id)

        mongo_doc = await self.get_action_result_mongo(
            action_result_id=action_result_id,
            ctx=ctx,
        )
        if mongo_doc is None:
            raise Exception("ActionResult not found")

        file_url = (getattr(mongo_doc, "file_url", None) or "").strip()
        existing_s3_bytes: bytes | None = None

        try:
            if file_url:
                existing_s3_bytes = await self.fs_crud.get_bytes(ctx=ctx, filename=filename)
                if not existing_s3_bytes:
                    raise HTTPException(
                        status_code=404, detail="Action result payload missing in S3"
                    )
                file_action_result_doc = ActionResultDoc.model_validate_json(existing_s3_bytes)
            else:
                file_action_result_doc = mongo_doc

            file_action_result_doc = file_action_result_doc.model_copy(
                update=action_update.model_dump(
                    exclude_unset=True,
                    exclude_none=True,
                    # `file_url` is server-managed and must not be user-controlled.
                    exclude={"result", "file_url"},
                )
            )

            try:
                file_action_result_doc.result = ActionDoc.model_validate(
                    file_action_result_doc.result
                )
            except Exception:
                raise Exception("Stored ActionResult is missing full ActionDoc payload")

            if action_update.result is not None:
                file_action_result_doc.result = ActionDoc.model_validate(action_update.result)

            updated_at: DateTime = Clock.datetime_now()
            file_action_result_doc.updated_at = updated_at

            filemetas = await self.fs_crud.upload_binary(
                ctx=ctx,
                file_obj=BytesIO(file_action_result_doc.model_dump_json().encode("utf-8")),
                filename=filename,
            )
            assert len(filemetas) == 1
            new_file_url = str(filemetas[0].url)
            assert new_file_url, "ActionResult not saved to file"
            file_action_result_doc.file_url = new_file_url

            db_action_result = file_action_result_doc.model_copy(deep=True)
            db_action_result.result = ActionLiteDoc(
                **ActionDoc.model_validate(file_action_result_doc.result).model_dump()
            )
            set_fields = db_action_result.model_dump(exclude_none=True)
            set_fields.pop("id", None)
            set_fields.pop("user_id", None)
            set_fields = EventResultDocUpdate._make_bson_safe(set_fields)

            try:
                await self._mongo_set_fields(
                    ctx=ctx,
                    action_result_id=action_result_id,
                    set_fields=set_fields,
                )
            except Exception as e:
                logger.bind(ctx=ctx, code=AppCode.USER_ACTION_RESULT, action_result_id=action_result_id).warning(
                    f"Mongo update failed; rolling back S3 write: {str(e)}"
                )
                await self._rollback_s3_write(
                    ctx=ctx,
                    action_result_id=action_result_id,
                    filename=filename,
                    existing_s3_bytes=existing_s3_bytes,
                )
                raise

            logger.bind(ctx=ctx, code=AppCode.USER_ACTION_RESULT, action_result_id=action_result_id).info(
                "Updated action_result in s3 and db"
            )
            # Best-effort cache invalidation: prevent stale search results after update.
            try:
                await self._bust_search_cache_for_user(self.user_id)
            except Exception:
                pass

            return file_action_result_doc
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.USER_ACTION_RESULT, action_result_id=action_result_id).exception(
                f"Error in update_action_result: {str(e)}"
            )
            raise

    async def delete_action_result(
        self,
        id: str,
        ctx: Context | None = None,
    ) -> int:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        ctx = self._ensure_ctx(ctx, user_id=self.user_id)

        try:
            filename = self._filename(id)
            await self.fs_crud.delete_files(ctx=ctx, filenames=[filename])
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.USER_ACTION_RESULT, action_result_id=id).warning(
                f"Failed to delete action_result from S3 (continuing): {str(e)}"
            )

        event_result_doc_find = EventResultDocFind(id=id, user_id=self.user_id, space_id=space_id)
        delete_result = await self.event_result_crud.delete_many(event_result_doc_find)
        # Best-effort cache invalidation: prevent stale search results after delete.
        try:
            await self._bust_search_cache_for_user(self.user_id)
        except Exception:
            pass

        return delete_result.deleted_count

    async def branch(
        self,
        source_id: str,
        ctx: Context | None = None,
    ) -> ActionResultDoc:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        ctx = self._ensure_ctx(ctx, user_id=self.user_id)

        source = await self.get_action_result(source_id, ctx=ctx)
        if source is None:
            raise HTTPException(status_code=404, detail="Source action result not found")

        return await self.branch_from_source(source=source, source_id=source_id, ctx=ctx)

    async def branch_from_source(
        self,
        source: ActionResultDoc,
        source_id: str,
        ctx: Context | None = None,
    ) -> ActionResultDoc:
        ctx = self._ensure_ctx(ctx, user_id=self.user_id)

        terminal = {EventResultStatus.success, EventResultStatus.error}
        if source.status not in terminal:
            raise HTTPException(
                status_code=409,
                detail="Cannot branch a run that is still processing",
            )

        branch_result = source.result.model_copy(deep=True)
        branch_name = source.name or branch_result.name
        branch_suffix = random.randint(100, 999)
        branch_create = ActionResultCreate(
            status=source.status,
            result=branch_result,
            is_saved=False,
            parent_action_result_id=source_id,
        )
        if branch_name:
            branch_create.name = f"Branch {branch_suffix} {branch_name}"

        branch = await self.create_action_result(branch_create, ctx=ctx)

        logger.bind(
            ctx=ctx,
            source_id=source_id,
            branch_id=branch.id,
            source_status=source.status.value,
        ).info("Branch created from action result")
        return branch

    async def init(
        self,
        app_state: AppState,
        action_result_id: str | None, 
        action_doc: ActionDoc,
        action_input_dict: Dict[str, Any],
        db: AsyncIOMotorDatabase,
        ctx: Context | None = None,
    ) -> ActionResultDoc | None:
        action_result_doc: ActionResultDoc | None = None
        if action_result_id is not None:
            action_result_doc = await self.get_action_result(
                action_result_id
            )
            if action_result_doc is None:
                logger.bind(ctx=ctx).error(f"Action Result not found {action_result_id}")
                raise HTTPException(status_code=400, detail="Action Result not found")
            logger.bind(
                ctx=ctx,
                action_result_id=action_result_id,
                db_status=action_result_doc.status,
            ).info("run_action_in_background: status before setting processing")
            # Change status back to processing
            action_result_doc.result.input = action_input_dict
            action_result_doc.result.output = {}
            action_result_doc.result.results.append(ActionResult(input=action_input_dict))
            action_result_update: ActionResultUpdate = ActionResultUpdate(
                status=EventResultStatus.processing,
                result=action_result_doc.result,
            )
            if not action_result_doc.name:  # gen title
                title_data = (
                    f"Name: {action_result_doc.result.name}\nInput: {str(action_input_dict)}"
                )
                default_title = f"{action_result_doc.result.name} - {Clock.get_date_time().strftime('%Y-%m-%d %H:%M:%S')}"
                action_result_name = await LlmFunc.gen_title(
                    ctx=ctx,
                    app_state=app_state,
                    db=db,
                    user=self.user,
                    title_for=DataBlock(text=title_data),
                    default_title=default_title,
                )
                action_result_update.name = action_result_name
            action_result_doc = await self.update_action_result(
                action_result_id, action_result_update
            )
        else:
            # Create the initial Action Result
            action_doc.input = action_input_dict
            action_doc.results.append(ActionResult(input=action_input_dict))
            action_result_create: ActionResultCreate = ActionResultCreate(
                status=EventResultStatus.processing, result=action_doc, is_saved=False
            )
            if action_input_dict:  # gen title
                data = f"Name: {action_doc.name}\nInput: {str(action_input_dict)}"
                default_title = (
                    f"{action_doc.name} - {Clock.get_date_time().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                action_result_name = await LlmFunc.gen_title(
                    ctx=ctx,
                    app_state=app_state,
                    db=db,
                    user=self.user,
                    title_for=DataBlock(text=data),
                    default_title=default_title,
                )
                action_result_create.name = action_result_name
            action_result_doc = await self.create_action_result(
                action_result_create
            )
            logger.bind(
                ctx=ctx,
                action_run_id=action_result_doc.id,
                action_id=action_doc.id,
                action_name=action_doc.name,
            ).info("Action run created")

        return action_result_doc            