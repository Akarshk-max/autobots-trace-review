from typing import List, Literal

import supabase_auth
from fastapi import APIRouter, Depends, Request
from motor.motor_asyncio import AsyncIOMotorDatabase
from openai.types import VectorStore, VectorStoreDeleted

from src.autobots import SettingsProvider
from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.conn.openai.openai_vector_stores.openai_vector_stores.openai_vector_stores_model import (
    CreateVectorStore,
    DeleteVectorStore,
    ListVectorStore,
    RetrieveVectorStore,
    UpdateVectorStore,
)
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=f"{SettingsProvider.sget().API_OPENAI_STORAGE}/vector_stores",
    tags=[SettingsProvider.sget().API_OPENAI_STORAGE],
)


@router.post("/")
async def create_vector_store(
    request: Request,
    openai_provider: Literal["openai", "azure"],
    new_vector_store: CreateVectorStore,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> VectorStore:
    ctx = request.state.context
    user = UserORM(**user_res.model_dump())
    client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=openai_provider)
    openai_vector_stores_client = client.openai_vector_stores
    vector_store = await openai_vector_stores_client.create(new_vector_store)
    return vector_store


@router.get("/list")
async def list_vector_stores(
    request: Request,
    openai_provider: Literal["openai", "azure"],
    limit: int = 10,
    order: Literal["desc", "asc"] = "desc",
    after: str | None = None,
    before: str | None = None,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> List[VectorStore]:
    ctx = request.state.context
    user = UserORM(**user_res.model_dump())
    client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=openai_provider)
    openai_vector_stores_client = client.openai_vector_stores
    vector_stores = ListVectorStore(limit=limit, order=order, after=after, before=before)
    stores = await openai_vector_stores_client.list(vector_stores)
    return stores


@router.get("/")
async def get_vector_store(
    request: Request,
    openai_provider: Literal["openai", "azure"],
    retrieve_vector_store: RetrieveVectorStore,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> VectorStore:
    ctx = request.state.context
    user = UserORM(**user_res.model_dump())
    client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=openai_provider)
    openai_vector_stores_client = client.openai_vector_stores
    store = await openai_vector_stores_client.retrieve(retrieve_vector_store)
    return store


@router.put("/")
async def update_vector_store(
    request: Request,
    openai_provider: Literal["openai", "azure"],
    vector_store: UpdateVectorStore,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> VectorStore:
    ctx = request.state.context
    user = UserORM(**user_res.model_dump())
    client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=openai_provider)
    openai_vector_stores_client = client.openai_vector_stores
    store = await openai_vector_stores_client.update(vector_store)
    return store


@router.delete("/")
async def delete_vector_store(
    request: Request,
    openai_provider: Literal["openai", "azure"],
    vector_store: DeleteVectorStore,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> VectorStoreDeleted:
    ctx = request.state.context
    user = UserORM(**user_res.model_dump())
    client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=openai_provider)
    openai_vector_stores_client = client.openai_vector_stores
    store = await openai_vector_stores_client.delete(vector_store)
    return store
