import json
from typing import List, Literal, Tuple

from fastapi import BackgroundTasks, HTTPException, UploadFile
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import HttpUrl
from pymongo.results import DeleteResult

from src.autobots.api.webhook import Webhook
from src.autobots.integrations.app_creds.user_app_creds import UserAppCreds
from src.autobots.integrations.app_types import AppTypes
from src.autobots.conn.aws.aws_s3 import get_aws_s3
from src.autobots.conn.aws.s3 import S3, get_s3
from src.autobots.conn.weaviate.weaviate_store import WeaviateStore
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.redis.async_pool import get_async_redis
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.datastore.datastore_meta_crud import DatastoreMetaCRUD
from src.autobots.datastore.datastore_meta_doc_model import (
    DatastoreMetaDoc,
    DatastoreMetaDocCreate,
    DatastoreMetaDocFind,
)
from src.autobots.datastore.datastore_result.datastore_result_doc_model import (
    DatastoreResultCreate,
    DatastoreResultDoc,
    DatastoreResultUpdate,
)
from src.autobots.datastore.datastore_result.user_datastore_result import UserDatastoreResult
from src.autobots.datastore.multidatastore import MultiDataStore, SearchResult
from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.ingestion.service import IngestionService
from src.autobots.user.user_orm_model import UserORM


class UserDatastore:
    def __init__(
        self,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        s3: S3 = get_s3(),
        openai_provider: Literal["azure", "openai"] = "azure",
    ):
        self.user = user
        self.user_id = str(user.id)
        self.db = db
        self.space_member = self.user.space_member

        # --- V1 - Chroma instance variables ---
        # This instance is used by the V1 methods.
        self.datastore = MultiDataStore(s3, openai_provider=openai_provider)
        # This CRUD points to the 'VectorstoreMetas' collection.
        self.datastore_meta_crud = DatastoreMetaCRUD(self.db, "vectorstore")
        self.user_datastore_result = UserDatastoreResult(user, db)

        # --- V2 - Weaviate instance variables ---
        # The V2 methods will use this service.
        self.ingestion_service = IngestionService(user_id=self.user_id)
        # We reuse the same datastore_meta_crud for V2 methods.
        self.v2_meta_crud = self.datastore_meta_crud

    # ======================================================================
    # --- V1 METHODS (CHROMA BACKEND) ---
    # These methods are preserved to ensure no breaking changes for existing callers.
    # ======================================================================

    async def init(self, name: str) -> DatastoreMetaDoc:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        self.datastore.init(name)
        datastore_meta_doc_create = DatastoreMetaDocCreate(
            user_id=str(self.user.id),
            space_id=space_id,
            datastore_id=self.datastore.id,
            name=self.datastore.name,
        )
        user_datastore_meta = await self.datastore_meta_crud.insert_one(datastore_meta_doc_create)
        return user_datastore_meta

    async def init_id(self, name: str) -> MultiDataStore:
        return self.datastore.hydrate(name)

    async def hydrate(self, datastore_id: str) -> MultiDataStore:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        datastore_meta_doc_find = DatastoreMetaDocFind(
            user_id=str(self.user.id), datastore_id=datastore_id, space_id=space_id
        )
        datastore_metas = await self.datastore_meta_crud.find(datastore_meta_doc_find)
        if len(datastore_metas) != 1:
            raise HTTPException(404, "Datastore not found")
        datastore_meta = datastore_metas[0]
        datastore = self.datastore.hydrate(datastore_meta.datastore_id)
        return datastore

    async def get(self, name: str) -> DatastoreMetaDoc:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        datastore_meta_doc_find = DatastoreMetaDocFind(
            user_id=str(self.user.id), name=name, space_id=space_id
        )
        datastore_metas = await self.datastore_meta_crud.find(datastore_meta_doc_find)
        if len(datastore_metas) != 1:
            raise HTTPException(405, "Datastore not found")
        return datastore_metas[0]

    async def get_by_datastore_id(self, datastore_id: str) -> DatastoreMetaDoc:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        datastore_meta_doc_find = DatastoreMetaDocFind(
            user_id=str(self.user.id), datastore_id=datastore_id, space_id=space_id
        )
        datastore_metas = await self.datastore_meta_crud.find(datastore_meta_doc_find)
        if len(datastore_metas) != 1:
            raise HTTPException(405, "Datastore not found")
        return datastore_metas[0]

    async def list(self, limit: int = 100, offset: int = 0) -> List[DatastoreMetaDoc]:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        datastore_meta_doc_find = DatastoreMetaDocFind(user_id=str(self.user.id), space_id=space_id)
        datastore_metas = await self.datastore_meta_crud.find(
            datastore_meta_doc_find, limit, offset
        )
        return datastore_metas

    async def delete(self, datastore_id: str) -> DeleteResult:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        # delete s3 and pinecone entries
        # user_datastore = await self.hydrate(datastore_id)
        # await user_datastore.empty_and_close()
        # delete meta entry
        datastore_meta_doc_find = DatastoreMetaDocFind(
            user_id=str(self.user.id), datastore_id=datastore_id, space_id=space_id
        )
        deleted_result = await self.datastore_meta_crud.delete_many(datastore_meta_doc_find)
        return deleted_result

    async def put_resource_async(
        self,
        ctx: Context,
        datastore_id: str,
        data: str | None = None,
        files: List[UploadFile] | None = None,
        urls: List[HttpUrl] | None = None,
        chunk_size: int = 500,
        ocr_detail: Literal["low", "medium", "high"] = "low",
        background_tasks: BackgroundTasks | None = None,
        webhook: Webhook | None = None,
    ) -> DatastoreResultDoc | None:
        user_datastore = await self.hydrate(datastore_id)
        if user_datastore is None:
            raise HTTPException(404, "Datastore not found")
        datastore_result_create = DatastoreResultCreate(status=EventResultStatus.processing)
        datastore_result_doc = await self.user_datastore_result.create_datastore_result(
            datastore_result_create
        )

        # Read file contents before passing to background task
        file_contents = []
        if files:
            for file in files:
                content = await file.read()
                file_contents.append((file.filename, content))

        if background_tasks:
            # Run in background
            background_tasks.add_task(
                self.put_resource,
                ctx=ctx,
                datastore_result_doc=datastore_result_doc,
                user_datastore_result=self.user_datastore_result,
                user_datastore=user_datastore,
                data=data,
                files=file_contents,  # Pass file contents instead of UploadFile objects
                urls=urls,
                chunk_size=chunk_size,
                ocr_detail=ocr_detail,
                top_k=10,
                webhook=webhook,
            )
        else:
            # For testing
            await self.put_resource(
                ctx=ctx,
                datastore_result_doc=datastore_result_doc,
                user_datastore_result=self.user_datastore_result,
                user_datastore=user_datastore,
                data=data,
                files=file_contents,  # Pass file contents instead of UploadFile objects
                urls=urls,
                chunk_size=chunk_size,
                ocr_detail=ocr_detail,
                webhook=webhook,
            )

        return datastore_result_doc

    async def put_resource_s3_async(
        self,
        ctx: Context,
        user: UserORM,
        datastore_id: str,
        data: str | None = None,
        urls: List[HttpUrl] | None = None,
        chunk_size: int = 500,
        top_k: int = 10,
        ocr_detail: Literal["low", "medium", "high"] = "low",
        background_tasks: BackgroundTasks | None = None,
        webhook: Webhook | None = None,
    ) -> DatastoreResultDoc | None:
        try:
            user_datastore = await self.hydrate(datastore_id)
        except Exception as e:
            logger.exception(f"Cannot hydrate datastore, creating new: {str(e)}")
            datastore_meta = await self.init(datastore_id)
            user_datastore = self.datastore.hydrate(datastore_meta.datastore_id)
        datastore_result_create = DatastoreResultCreate(status=EventResultStatus.processing)
        datastore_result_doc = await self.user_datastore_result.create_datastore_result(
            datastore_result_create
        )
        db = next(get_mongo_db())
        app_creds_doc = await UserAppCreds.get_app_creds_by_app_name(
            ctx=Context(), db=db, user=user, app_name=AppTypes.aws , decrypt=True
        )
        aws_s3 = get_aws_s3(
            region_name=app_creds_doc.creds["region_name"],
            aws_access_key_id=app_creds_doc.creds["aws_access_key_id"],
            aws_secret_access_key=app_creds_doc.creds["aws_secret_access_key"],
            bucket_name=app_creds_doc.creds["bucket_name"],
            object_prefix=app_creds_doc.creds["object_prefix"],
        )
        objects = await aws_s3.list()
        file_contents = []

        for obj in objects:
            try:
                filename = await aws_s3.get_filename(obj.key)
                file_bytes = await aws_s3.get_bytes(ctx=ctx, key=obj.key)
                file_contents.append((filename, file_bytes))
            except Exception as e:
                logger.bind(key=obj.key).exception(
                    f"Error in getting bytes for the file {obj.key}, error: {str(e)}"
                )

        if background_tasks:
            # Run in background
            background_tasks.add_task(
                self.put_resource,
                ctx=ctx,
                datastore_result_doc=datastore_result_doc,
                user_datastore_result=self.user_datastore_result,
                user_datastore=user_datastore,
                data=data,
                urls=urls,
                top_k=top_k,
                files=file_contents,  # Pass file contents instead of UploadFile objects
                chunk_size=chunk_size,
                ocr_detail=ocr_detail,
                webhook=webhook,
            )
        else:
            # For testing
            await self.put_resource(
                ctx=ctx,
                datastore_result_doc=datastore_result_doc,
                user_datastore_result=self.user_datastore_result,
                user_datastore=user_datastore,
                data=data,
                urls=urls,
                top_k=top_k,
                files=file_contents,  # Pass file contents instead of UploadFile objects
                chunk_size=chunk_size,
                ocr_detail=ocr_detail,
                webhook=webhook,
            )
        return datastore_result_doc

    async def put_resource(
        self,
        ctx: Context,
        datastore_result_doc: DatastoreResultDoc,
        user_datastore_result: UserDatastoreResult,
        user_datastore: MultiDataStore,
        data: str | None = None,
        files: List[Tuple[str, bytes]] | None = None,
        urls: List[HttpUrl] | None = None,
        chunk_size: int = 500,
        top_k: int = 10,
        ocr_detail: Literal["low", "medium", "high"] = "low",
        webhook: Webhook | None = None,
    ) -> DatastoreResultDoc:
        if files:
            try:
                for file in files:
                    datastore_results_2 = await user_datastore.put_data(
                        ctx, user=self.user, db=self.db, file=file
                    )
                    assert datastore_results_2
                datastore_result_doc = await user_datastore_result.update_datastore_result(
                    datastore_result_doc.id,
                    DatastoreResultUpdate(**datastore_result_doc.model_dump()),
                )
            except Exception as e:
                logger.bind(ctx=ctx).exception(f"Error in put resource: {str(e)}")

        # if urls:
        #     try:
        #         for url in urls:
        #             datastore_results_3 = await user_datastore.put_data(str(url))
        #             datastore_result_doc.result.status_for = datastore_result_doc.result.status_for + datastore_results_3
        #         datastore_result_doc = await user_datastore_result.update_datastore_result(
        #             datastore_result_doc.id, DatastoreResultUpdate(**datastore_result_doc.model_dump())
        #         )
        #     except Exception as e:
        #         logger.error(str(e))

        datastore_result_doc.status = EventResultStatus.success
        datastore_result_doc = await user_datastore_result.update_datastore_result(
            datastore_result_doc.id, DatastoreResultUpdate(**datastore_result_doc.model_dump())
        )

        if webhook:
            await webhook.send(datastore_result_doc.model_dump())
        return datastore_result_doc

    async def get_datastore_put_result(
        self, datastore_put_result_id: str
    ) -> DatastoreResultDoc | None:
        return await self.user_datastore_result.get_datastore_result(datastore_put_result_id)

    # ======================================================================
    # --- NEW V2 METHODS (WEAVIATE BACKEND) ---
    # These methods provide the new functionality using Weaviate.
    # They are called explicitly when new features require the Weaviate backend.
    # ======================================================================

    async def v2_create_collection(self, collection_name: str) -> DatastoreMetaDoc:
        """[V2] Creates a new collection in Weaviate and logs it in MongoDB."""
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        logger.info(
            f"[V2] Initializing Weaviate collection '{collection_name}' for user '{self.user_id}'"
        )
        store = await WeaviateStore.create_store(
            collection_name=collection_name, user_id=self.user_id
        )
        await store.close()

        meta_create = DatastoreMetaDocCreate(
            user_id=self.user_id,
            space_id=space_id,
            datastore_id=collection_name,  # For Weaviate, the name is the ID
            name=collection_name,
        )
        return await self.v2_meta_crud.insert_one(meta_create)

    async def v2_queue_ingestion_job(
        self, collection_name: str, s3_key: str, original_filename: str
    ):
        """
        [V2] Creates and queues a job payload for the ingestion worker.
        This method is clean of any web-framework or file-handling logic.
        """
        job_payload = {
            "user_id": self.user_id,
            "s3_key": s3_key,
            "collection_name": collection_name,
            "original_filename": original_filename,
            "job_type": "high_priority",
        }
        settings = SettingsProvider.sget()
        r = await get_async_redis()
        await r.lpush(
            settings.REDIS_INGESTION_HIGH_PRIORITY_QUEUE, json.dumps(job_payload)
        )
        logger.info(f"Queued job for S3 key {s3_key} into collection {collection_name}")

    async def v2_search(
        self, collection_name: str, query: str, top_k: int = 10
    ) -> List[SearchResult]:
        """[V2] Searches a Weaviate collection for a user. This is a stateless method."""
        logger.info(
            f"[V2] Searching Weaviate collection '{collection_name}' for user '{self.user_id}'"
        )
        store = await WeaviateStore.create_store(
            collection_name=collection_name, user_id=self.user_id
        )
        try:
            results_with_scores = await store.similarity_search_with_score(query, k=top_k)

            search_results = [
                SearchResult(
                    data=doc.page_content,
                    sources=doc.metadata.get("source"),
                    score=score,
                    metadata=doc.metadata,
                )
                for doc, score in results_with_scores
            ]
            return search_results
        finally:
            await store.close()

    async def v2_get_collection_meta(self, collection_name: str) -> DatastoreMetaDoc:
        """[V2] Gets the metadata for a Weaviate collection."""
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        meta_find = DatastoreMetaDocFind(
            user_id=self.user_id, name=collection_name, space_id=space_id
        )
        meta_docs = await self.v2_meta_crud.find(meta_find)

        # Filter for the correct backend in case of name collision
        for doc in meta_docs:
            if doc.metadata.get("backend") == "weaviate":
                return doc
        raise HTTPException(404, f"Weaviate collection with name '{collection_name}' not found.")

    async def v2_list_collections(
        self, limit: int = 100, offset: int = 0
    ) -> List[DatastoreMetaDoc]:
        """[V2] Lists all Weaviate collections for the user."""
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        meta_find = DatastoreMetaDocFind(user_id=self.user_id, space_id=space_id)
        all_docs = await self.v2_meta_crud.find(meta_find, limit=limit, offset=offset)

        # Filter for only Weaviate collections
        weaviate_docs = [doc for doc in all_docs if doc.metadata.get("backend") == "weaviate"]
        return weaviate_docs

    async def v2_delete_collection(self, collection_name: str) -> bool:
        """[V2] Deletes a Weaviate collection and its metadata."""
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        logger.info(
            f"[V2] Deleting Weaviate collection '{collection_name}' for user '{self.user_id}'"
        )

        # 1. Delete the collection from Weaviate
        store = await WeaviateStore.create_store(
            collection_name=collection_name, user_id=self.user_id
        )
        try:
            await store.delete_collection()
        except Exception as e:
            logger.error(f"Failed to delete collection from Weaviate backend: {e}")
            # Decide if you want to fail here or proceed to delete metadata anyway.
            # It's safer to fail.
            return False
        finally:
            await store.close()

        # 2. Delete the metadata from MongoDB
        meta_find = DatastoreMetaDocFind(
            user_id=self.user_id, name=collection_name, space_id=space_id
        )
        delete_result = await self.v2_meta_crud.delete_many(meta_find)

        return delete_result.deleted_count > 0
