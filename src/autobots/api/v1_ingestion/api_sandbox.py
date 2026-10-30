# src/autobots/api/v1_ingestion/api_sandbox.py

import asyncio
import uuid
from typing import List
from urllib.parse import urlparse

import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from openai.types.chat import ChatCompletionUserMessageParam
from pydantic import BaseModel, HttpUrl
from starlette.requests import Request

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.conn.object_storage.factory import get_public_storage
from src.autobots.conn.openai.openai_chat.chat_model import ChatReq
from src.autobots.conn.weaviate.weaviate_store import WeaviateStore
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.data_model.app_state import AppState
from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.files.user_files import UserFiles
from src.autobots.ingestion.ingestion_llm_connector import IngestionLLMConnector
from src.autobots.ingestion.service import IngestionService
from src.autobots.ingestion.url_import_utils import import_public_url_to_public_s3_for_sandbox
from src.autobots.space.space_auth import get_optional_space_member
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.token_usage.token_usage_doc_model import TokenUsageSource
from src.autobots.token_usage.token_usage_service import TokenUsageService
from src.autobots.user.user_orm_model import UserORM

# Uncomment for tests only
# from typing import Optional
# from src.autobots.api.deps import get_current_user

router = APIRouter(prefix="/sandbox", tags=["Sandbox RAG"])

PROMPT_TEMPLATE = (
    "Based on the following context from the user-provided file(s), please provide a comprehensive answer to the user's question. "
    "If the context is insufficient, state that you cannot answer from the provided information.\n\n"
    "--- CONTEXT ---\n{context}\n\n"
    "--- USER'S QUESTION ---\n{question}"
)


class SandboxSearchRequest(BaseModel):
    file_urls: List[HttpUrl]
    query: str


@router.post(
    "/search",
    response_model=DataBlocks,
    summary="Perform an ephemeral RAG search on files from URLs.",
)
async def perform_sandbox_search(
    request: Request,
    request_body: SandboxSearchRequest,
    user_res: supabase_auth.UserResponse = Depends(
        get_offline_user_from_access_token
    ),  # Use for production, comment for tests
    # _test_user: Optional[UserORM] = Depends(get_current_user), # Uncomment for tests only
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    ctx = request.state.context
    app_state: AppState = request.state.app_state
    # user = _test_user # Uncomment for tests only
    user = UserORM(
        **user_res.user.model_dump(), space_member=space_member
    )  # Use for production, comment for tests

    # Weaviate class names must start with an uppercase letter and cannot contain hyphens.
    # We remove hyphens from the user's UUID to ensure the name is valid.
    sanitized_user_id = str(user.id).replace("-", "")
    collection_name = f"Sandbox_{sanitized_user_id}_{uuid.uuid4().hex[:8]}"
    store = None
    cleanup_keys: list[str] = []
    public_s3 = None

    app_state = app_state.child_context(
        source=TokenUsageSource.sandbox,
        source_id=collection_name,
    )
    TokenUsageService.start_collection(app_state)
    sandbox_status = EventResultStatus.success

    try:
        # --- 1. Create temporary store and ingest files directly ---
        logger.bind(ctx=ctx).info(
            f"Creating sandbox '{collection_name}' and ingesting files directly..."
        )
        store = await WeaviateStore.create_store(
            collection_name=collection_name, user_id=str(user.id)
        )
        ingestion_service = IngestionService(user_id=str(user.id))
        public_s3 = get_public_storage()
        filename_sanitizer = UserFiles(user=user, s3=public_s3)

        ingest_tasks = []
        for url_obj in request_body.file_urls:
            raw_url = str(url_obj)
            # Fix query-param filenames (file.pdf?x=1 -> file.pdf)
            display_name = await UserFiles.extract_file_name(ctx=ctx, file_url=raw_url)
            # Sanitize so suffix detection works reliably in _execute_ingestion_job
            display_name = await filename_sanitizer.secure_filename(ctx=ctx, filename=display_name)

            in_bucket_key = await public_s3.get_key_if_owned_url(raw_url)
            if in_bucket_key:
                s3_key = in_bucket_key
                # Provide persisted_* to avoid get_permanent_url fallback warnings
                # Canonicalize URL if query/fragment present (presigned URLs)
                parsed = urlparse(raw_url)
                if parsed.query or parsed.fragment:
                    canonical_url = str(await public_s3.get_https_url(in_bucket_key))
                else:
                    canonical_url = raw_url
                job_data = {"persisted_s3_url": canonical_url, "persisted_s3_key": in_bucket_key}
            else:
                imported = await import_public_url_to_public_s3_for_sandbox(
                    ctx=ctx,
                    user=user,
                    public_s3=public_s3,
                    remote_url=raw_url,
                    original_filename=display_name,
                )
                s3_key = imported.s3_key
                job_data = {
                    "persisted_s3_url": imported.s3_url,
                    "persisted_s3_key": imported.s3_key,
                }
                cleanup_keys.append(imported.s3_key)
                # Use the sanitized name produced by the importer as canonical
                display_name = imported.original_filename

            ingest_tasks.append(
                ingestion_service._execute_ingestion_job(
                    s3_key=s3_key,
                    original_filename=display_name,
                    collection_name=collection_name,
                    db=db,
                    targets=["weaviate"],  # Sandbox only needs Weaviate
                    user=user,
                    ctx=ctx,
                    app_state=app_state,
                    store=store,  # Pass the created store to the execution logic
                    job_data=job_data,
                )
            )

        await asyncio.gather(*ingest_tasks)

        # --- 2. Query the Data ---
        # We can query immediately because the await above confirms ingestion is complete.
        search_results = await store.similarity_search(query=request_body.query, k=10)
        if not search_results:
            final_response = DataBlocks(
                blocks=[
                    DataBlock(
                        text="I couldn't find any relevant information in the uploaded files."
                    )
                ]
            )
            # --- VERIFICATION LOG ---
            logger.bind(ctx=ctx, final_response=final_response.model_dump(mode="json")).info(
                "Final Sandbox response constructed (no results found)."
            )
            return final_response

        context_str = "\n".join([doc.page_content for doc in search_results])
        final_prompt = PROMPT_TEMPLATE.format(context=context_str, question=request_body.query)

        # settings = SettingsProvider.sget()
        provider = "openai"  # "azure" if settings.OPENAI_TYPE_IS_AZURE else "openai"
        openai_client = await IngestionLLMConnector.get_openai_client(ctx, user, db, provider)

        openai_chat = openai_client.openai_chat
        chat_req = ChatReq(
            model="gpt-4.1",
            messages=[ChatCompletionUserMessageParam(role="user", content=final_prompt)],
        )
        chat_res = await openai_chat.chat(ctx=ctx, chat_req=chat_req, app_state=app_state)

        answer = (
            chat_res.choices[0].message.content
            if chat_res.choices
            else "Could not generate a response."
        )
        final_response = DataBlocks(blocks=[DataBlock(text=answer)])

        # --- VERIFICATION LOG ---
        logger.bind(ctx=ctx, final_response=final_response.model_dump(mode="json")).info(
            "Final Sandbox response constructed (successful answer)."
        )

        return final_response

    except HTTPException:
        sandbox_status = EventResultStatus.error
        raise
    except Exception as e:
        sandbox_status = EventResultStatus.error
        logger.bind(ctx=ctx).exception(f"Error in sandbox search: {e}")
        raise HTTPException(
            status_code=500, detail="An internal error occurred during the sandbox search."
        )
    finally:
        await TokenUsageService.flush(
            ctx=ctx,
            app_state=app_state,
            user=user,
            db=db,
            status=sandbox_status,
        )

        # Best-effort cleanup of any S3 objects we uploaded for sandbox
        if cleanup_keys and public_s3:
            for key in cleanup_keys:
                try:
                    await public_s3.delete_key(key)
                except Exception as e:
                    logger.bind(ctx=ctx, key=key).warning(f"Failed to cleanup sandbox S3 key: {e}")

        if store:
            await store.delete_collection()
            await store.close()
            logger.bind(ctx=ctx).info(f"Cleaned up ephemeral sandbox collection: {collection_name}")
