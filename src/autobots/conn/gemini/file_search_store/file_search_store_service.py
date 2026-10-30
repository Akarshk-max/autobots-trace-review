from typing import Any

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.conn.gemini.file_search_store.file_search_store_client import (
    GeminiFileSearchStoreClient,
)
from src.autobots.conn.gemini.file_search_store.file_search_store_models import (
    GeminiFileSearchStoreDocumentRef,
    GeminiFileSearchStoreRef,
    GeminiFileSearchStoreReq,
    GeminiFileSearchStoreRes,
)
from src.autobots.conn.gemini.message_normalizer_gemini import GeminiMessageNormalizer
from src.autobots.data_model.context import Context
from src.autobots.files.file_downloader import FileDownloader
from src.autobots.user.user_orm_model import UserORM


class GeminiFileSearchStoreService:
    DEFAULT_CHAT_STORE_DISPLAY_PREFIX = "autobots-gemini-chat-store"

    @staticmethod
    def normalize_store_ref(
        req: GeminiFileSearchStoreReq,
    ) -> GeminiFileSearchStoreRef | None:
        return req.normalized_store_ref()

    @staticmethod
    def ensure_store_name(
        req: GeminiFileSearchStoreReq,
    ) -> str:
        store_ref = req.normalized_store_ref()
        if store_ref is None or not store_ref.store_name:
            raise ValueError("store_ref.store_name is required")
        return store_ref.store_name

    @classmethod
    def build_chat_scoped_display_name(
        cls,
        *,
        chat_id: str | None,
        display_name: str | None,
    ) -> str:
        return display_name or f"{cls.DEFAULT_CHAT_STORE_DISPLAY_PREFIX}-{chat_id or 'default'}"

    @staticmethod
    def _normalize_operation_payload(operation: Any) -> dict[str, Any]:
        return GeminiMessageNormalizer.normalize_response(operation)

    @classmethod
    def _document_ref_from_operation(
        cls,
        *,
        input_item: dict[str, Any],
        operation: Any,
        status: str = "uploaded",
    ) -> GeminiFileSearchStoreDocumentRef:
        payload = cls._normalize_operation_payload(operation)
        response_payload = payload.get("response") if isinstance(payload, dict) else None
        document_name = None
        if isinstance(response_payload, dict):
            document_name = response_payload.get("name")
        if document_name is None and isinstance(payload, dict):
            document_name = payload.get("name")

        return GeminiFileSearchStoreDocumentRef(
            document_name=document_name,
            display_name=input_item.get("display_name"),
            source_url=input_item.get("url"),
            gemini_file_name=input_item.get("gemini_file_name"),
            status=status,
            metadata=payload if isinstance(payload, dict) else {},
        )

    @classmethod
    async def create_store(
        cls,
        *,
        req: GeminiFileSearchStoreReq,
        client: GeminiFileSearchStoreClient,
    ) -> GeminiFileSearchStoreRes:
        store_ref = req.normalized_store_ref()
        display_name = req.display_name
        if store_ref and store_ref.scope == "chat_scoped":
            display_name = cls.build_chat_scoped_display_name(
                chat_id=store_ref.chat_id,
                display_name=display_name or store_ref.display_name,
            )
        display_name = display_name or "autobots-gemini-file-search-store"

        store = await client.create_store(
            display_name=display_name,
            embedding_model=req.embedding_model,
        )
        plain_store = GeminiMessageNormalizer.normalize_response(store)
        return GeminiFileSearchStoreRes(
            operation=req.operation,
            summary=f"Created file search store {plain_store.get('name') or display_name}.",
            store=plain_store,
            total_count=1,
            returned_count=1,
        )

    @classmethod
    async def get_store(
        cls,
        *,
        req: GeminiFileSearchStoreReq,
        client: GeminiFileSearchStoreClient,
    ) -> GeminiFileSearchStoreRes:
        store = await client.get_store(name=cls.ensure_store_name(req))
        plain_store = GeminiMessageNormalizer.normalize_response(store)
        return GeminiFileSearchStoreRes(
            operation=req.operation,
            summary=f"Fetched file search store {plain_store.get('name')}",
            store=plain_store,
            total_count=1,
            returned_count=1,
        )

    @classmethod
    async def list_stores(
        cls,
        *,
        req: GeminiFileSearchStoreReq,
        client: GeminiFileSearchStoreClient,
    ) -> GeminiFileSearchStoreRes:
        stores = await client.list_stores()
        plain_stores = [GeminiMessageNormalizer.normalize_response(item) for item in stores]
        return GeminiFileSearchStoreRes(
            operation=req.operation,
            summary=f"Listed {len(plain_stores)} file search stores.",
            stores=plain_stores,
            total_count=len(plain_stores),
            returned_count=len(plain_stores),
        )

    @classmethod
    async def ingest_files(
        cls,
        *,
        req: GeminiFileSearchStoreReq,
        client: GeminiFileSearchStoreClient,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase | None,
    ) -> GeminiFileSearchStoreRes:
        store_name = cls.ensure_store_name(req)
        documents: list[GeminiFileSearchStoreDocumentRef] = []
        failed_items: list[dict[str, Any]] = []

        url_items = [
            item.model_dump(exclude_none=True) if hasattr(item, "model_dump") else dict(item)
            for item in req.files
            if (item.model_dump(exclude_none=True) if hasattr(item, "model_dump") else dict(item)).get("url")
        ]

        downloaded_files = {}
        if url_items:
            downloaded_files = await FileDownloader._download_urls_to_tmpfiles(
                ctx=ctx,
                user=user,
                db=db,
                urls=[item["url"] for item in url_items],
            )

        for raw_item in req.files:
            item = raw_item.model_dump(exclude_none=True) if hasattr(raw_item, "model_dump") else dict(raw_item)
            try:
                if item.get("gemini_file_name"):
                    operation = await client.import_existing_gemini_file(
                        store_name=store_name,
                        gemini_file_name=item["gemini_file_name"],
                        custom_metadata=item.get("custom_metadata"),
                    )
                else:
                    local_path = downloaded_files.get(item.get("url"))
                    if not local_path:
                        raise ValueError(f"Could not download file from url: {item.get('url')}")
                    operation = await client.upload_file_to_store(
                        file_path=local_path,
                        store_name=store_name,
                        display_name=item.get("display_name"),
                        mime_type=item.get("mime_type"),
                        custom_metadata=item.get("custom_metadata"),
                    )

                final_operation = await client.wait_for_operation(operation)
                documents.append(
                    cls._document_ref_from_operation(
                        input_item=item,
                        operation=final_operation,
                        status="ingested",
                    )
                )
            except Exception as exc:
                logger.bind(ctx=ctx).exception("Gemini file search ingest failed: {}", exc)
                failed_items.append({"item": item, "error": str(exc)})

        return GeminiFileSearchStoreRes(
            operation=req.operation,
            summary=(
                f"Ingested {len(documents)} file(s) into {store_name}."
                + (f" Failed: {len(failed_items)}." if failed_items else "")
            ),
            store={"name": store_name},
            documents=[doc.model_dump(exclude_none=True) for doc in documents],
            total_count=len(documents) + len(failed_items),
            returned_count=len(documents),
            failed_items=failed_items,
        )

    @classmethod
    async def delete_documents(
        cls,
        *,
        req: GeminiFileSearchStoreReq,
        client: GeminiFileSearchStoreClient,
        ctx: Context,
    ) -> GeminiFileSearchStoreRes:
        deleted: list[dict[str, Any]] = []
        failed_items: list[dict[str, Any]] = []
        for name in req.document_names:
            try:
                await client.delete_document(name=name, force=req.force)
                deleted.append({"document_name": name, "status": "deleted"})
            except Exception as exc:
                logger.bind(ctx=ctx).exception("Gemini file search delete_document failed: {}", exc)
                failed_items.append({"document_name": name, "error": str(exc)})

        return GeminiFileSearchStoreRes(
            operation=req.operation,
            summary=f"Deleted {len(deleted)} document(s).",
            store={"name": cls.ensure_store_name(req)},
            documents=deleted,
            total_count=len(deleted) + len(failed_items),
            returned_count=len(deleted),
            failed_items=failed_items,
        )

    @classmethod
    async def reindex_documents(
        cls,
        *,
        req: GeminiFileSearchStoreReq,
        client: GeminiFileSearchStoreClient,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase | None,
    ) -> GeminiFileSearchStoreRes:
        deleted_res = None
        if req.document_names:
            deleted_res = await cls.delete_documents(
                req=req,
                client=client,
                ctx=ctx,
            )

        ingest_res = None
        if req.files:
            ingest_req = req.model_copy(update={"operation": "ingest_files"})
            ingest_res = await cls.ingest_files(
                req=ingest_req,
                client=client,
                ctx=ctx,
                user=user,
                db=db,
            )

        deleted_count = len(deleted_res.documents) if deleted_res else 0
        ingested_count = len(ingest_res.documents) if ingest_res else 0
        failed_items = []
        if deleted_res:
            failed_items.extend(deleted_res.failed_items)
        if ingest_res:
            failed_items.extend(ingest_res.failed_items)

        return GeminiFileSearchStoreRes(
            operation=req.operation,
            summary=f"Reindex completed. Deleted {deleted_count}, ingested {ingested_count}.",
            store={"name": cls.ensure_store_name(req)},
            documents=[
                *(deleted_res.documents if deleted_res else []),
                *(ingest_res.documents if ingest_res else []),
            ],
            total_count=deleted_count + ingested_count + len(failed_items),
            returned_count=deleted_count + ingested_count,
            failed_items=failed_items,
        )

    @classmethod
    async def delete_store(
        cls,
        *,
        req: GeminiFileSearchStoreReq,
        client: GeminiFileSearchStoreClient,
    ) -> GeminiFileSearchStoreRes:
        store_name = cls.ensure_store_name(req)
        await client.delete_store(name=store_name, force=req.force)
        return GeminiFileSearchStoreRes(
            operation=req.operation,
            summary=f"Deleted store {store_name}.",
            store={"name": store_name},
            total_count=1,
            returned_count=1,
        )

    @classmethod
    async def query_store(
        cls,
        *,
        req: GeminiFileSearchStoreReq,
        client: GeminiFileSearchStoreClient,
        model: str,
    ) -> GeminiFileSearchStoreRes:
        store_name = cls.ensure_store_name(req)
        response = await client.query_store(
            model=model,
            query=req.query or "",
            store_name=store_name,
            metadata_filter=req.metadata_filter,
        )
        plain = GeminiMessageNormalizer.normalize_response(response)
        return GeminiFileSearchStoreRes(
            operation=req.operation,
            summary=f"Queried store {store_name}.",
            store={"name": store_name},
            query_text=req.query,
            query_response_text=getattr(response, "text", None),
            total_count=1,
            returned_count=1,
            meta={"response": plain},
        )

    @classmethod
    async def stats(
        cls,
        *,
        req: GeminiFileSearchStoreReq,
        client: GeminiFileSearchStoreClient,
    ) -> GeminiFileSearchStoreRes:
        store = await client.get_store(name=cls.ensure_store_name(req))
        plain_store = GeminiMessageNormalizer.normalize_response(store)
        return GeminiFileSearchStoreRes(
            operation=req.operation,
            summary=f"Fetched stats for {plain_store.get('name')}.",
            store=plain_store,
            total_count=1,
            returned_count=1,
        )

    @classmethod
    async def execute(
        cls,
        *,
        req: GeminiFileSearchStoreReq,
        client: GeminiFileSearchStoreClient,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase | None,
        model: str,
    ) -> GeminiFileSearchStoreRes:
        if req.operation == "create_store":
            return await cls.create_store(req=req, client=client)
        if req.operation == "get_store":
            return await cls.get_store(req=req, client=client)
        if req.operation == "list_stores":
            return await cls.list_stores(req=req, client=client)
        if req.operation == "ingest_files":
            return await cls.ingest_files(req=req, client=client, ctx=ctx, user=user, db=db)
        if req.operation == "reindex_documents":
            return await cls.reindex_documents(req=req, client=client, ctx=ctx, user=user, db=db)
        if req.operation == "delete_documents":
            return await cls.delete_documents(req=req, client=client, ctx=ctx)
        if req.operation == "delete_store":
            return await cls.delete_store(req=req, client=client)
        if req.operation == "query_store":
            return await cls.query_store(req=req, client=client, model=model)
        if req.operation == "stats":
            return await cls.stats(req=req, client=client)
        raise ValueError(f"Unsupported operation: {req.operation}")