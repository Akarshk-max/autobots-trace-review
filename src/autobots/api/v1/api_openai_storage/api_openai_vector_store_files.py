from typing import List, Literal

import supabase_auth
from fastapi import APIRouter, Depends, Request
from motor.motor_asyncio import AsyncIOMotorDatabase
from openai.types.vector_stores import VectorStoreFile

from src.autobots import SettingsProvider
from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.conn.openai.openai_vector_stores.openai_vector_store_files.openai_vector_store_files_model import (
    CreateVectorStoreFile,
    ListVectorStoreFiles,
)
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=f"{SettingsProvider.sget().API_OPENAI_STORAGE}/vector_store_files",
    tags=[SettingsProvider.sget().API_OPENAI_STORAGE],
)


@router.post("/")
async def create_vector_store_files(
    request: Request,
    openai_provider: Literal["openai", "azure"],
    vector_store_files: CreateVectorStoreFile,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> VectorStoreFile:
    ctx = request.state.context
    user = UserORM(**user_res.model_dump())
    client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=openai_provider)
    openai_vector_store_files = client.openai_vector_store_files
    files = await openai_vector_store_files.create(vector_store_files)
    return files


@router.get("/list")
async def list_vector_store_files(
    request: Request,
    openai_provider: Literal["openai", "azure"],
    vector_store_id: str,
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
    openai_vector_store_files = client.openai_vector_store_files
    vector_store_files_in_batch = ListVectorStoreFiles(
        vector_store_id=vector_store_id,
        limit=limit,
        order=order,
        after=after,
        before=before,
        filter=filter,
    )
    vector_store_files = await openai_vector_store_files.list(vector_store_files_in_batch)
    return vector_store_files
