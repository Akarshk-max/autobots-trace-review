from typing import List, Literal

import supabase_auth
from fastapi import APIRouter, Depends, Request
from motor.motor_asyncio import AsyncIOMotorDatabase
from openai.types.vector_stores import VectorStoreFile, VectorStoreFileBatch, VectorStoreFileDeleted

from src.autobots import SettingsProvider
from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.conn.openai.openai_vector_stores.openai_vector_store_file_batches.openai_vector_store_file_batches_model import (
    CreateVectorStoreFileBatch,
    ListVectorStoreFilesInBatch,
    RetrieveVectorStoreFileBatch,
)
from src.autobots.conn.openai.openai_vector_stores.openai_vector_store_files.openai_vector_store_files_model import (
    DeleteVectorStoreFile,
)
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=f"{SettingsProvider.sget().API_OPENAI_STORAGE}/vector_store_file_batches",
    tags=[SettingsProvider.sget().API_OPENAI_STORAGE],
)


@router.post("/")
async def create_vector_store_files(
    request: Request,
    openai_provider: Literal["openai", "azure"],
    vector_store_files: CreateVectorStoreFileBatch,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> VectorStoreFileBatch:
    ctx = request.state.context
    user = UserORM(**user_res.model_dump())
    client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=openai_provider)
    openai_vector_store_file_batches = client.openai_vector_store_file_batches
    files = await openai_vector_store_file_batches.create(vector_store_files)
    return files


@router.get("/list")
async def list_vector_store_files(
    request: Request,
    openai_provider: Literal["openai", "azure"],
    vector_store_id: str,
    batch_id: str,
    limit: int | None = 10,
    order: Literal["desc", "asc"] | None = "desc",
    after: str | None = None,
    before: str | None = None,
    filter: Literal["in_progress", "completed", "failed", "cancelled"] | None = None,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> List[VectorStoreFile]:
    ctx = request.state.context
    user = UserORM(**user_res.model_dump())
    client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=openai_provider)
    openai_vector_store_file_batches = client.openai_vector_store_file_batches
    vector_store_files_in_batch = ListVectorStoreFilesInBatch(
        vector_store_id=vector_store_id,
        batch_id=batch_id,
        limit=limit,
        order=order,
        after=after,
        before=before,
        filter=filter,
    )
    vector_store_files = await openai_vector_store_file_batches.list(vector_store_files_in_batch)
    return vector_store_files


@router.get("/")
async def get_vector_store_files(
    request: Request,
    openai_provider: Literal["openai", "azure"],
    retrieve_vector_store_files: RetrieveVectorStoreFileBatch,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> VectorStoreFileBatch:
    ctx = request.state.context
    user = UserORM(**user_res.model_dump())
    client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=openai_provider)
    openai_vector_store_file_batches = client.openai_vector_store_file_batches
    file_batch = await openai_vector_store_file_batches.retrieve(retrieve_vector_store_files)
    return file_batch


@router.delete("/")
async def delete_vector_store_files(
    request: Request,
    openai_provider: Literal["openai", "azure"],
    vector_store: DeleteVectorStoreFile,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> VectorStoreFileDeleted:
    ctx = request.state.context
    user = UserORM(**user_res.model_dump())
    client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=openai_provider)
    openai_vector_store_files = client.openai_vector_store_files
    deleted = await openai_vector_store_files.delete(vector_store)
    return deleted
