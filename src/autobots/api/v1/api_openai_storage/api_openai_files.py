from typing import Annotated, List, Literal

import supabase_auth
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from motor.motor_asyncio import AsyncIOMotorDatabase
from openai import BadRequestError
from openai.types import FileDeleted, FileObject

from src.autobots import SettingsProvider
from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.conn.openai.openai_files.openai_files_model import (
    FileContent,
    FileCreate,
    FileDelete,
    FileList,
    FileRetrieve,
)
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=f"{SettingsProvider.sget().API_OPENAI_STORAGE}/files",
    tags=[SettingsProvider.sget().API_OPENAI_STORAGE],
)


@router.post("/")
async def upload_file(
    request: Request,
    file: Annotated[UploadFile, File(description="File as UploadFile")],
    # background_tasks: BackgroundTasks,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> FileObject:
    ctx = request.state.context
    user = UserORM(**user_res.model_dump())
    client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider="azure")
    openai_file_client = client.openai_files
    # background_tasks.add_task(openai_file_client.create, file_create)
    file_create = FileCreate(
        file=(file.filename, await file.read(), file.content_type), purpose="assistants"
    )
    file_obj = await openai_file_client.create(ctx=ctx, file_create=file_create)
    return file_obj


@router.post("/azure")
async def upload_file_on_azure(
    request: Request,
    file: Annotated[UploadFile, File(description="File as UploadFile")],
    # background_tasks: BackgroundTasks,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> FileObject:
    ctx = request.state.context
    user = UserORM(**user_res.model_dump())
    client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider="azure")
    openai_file_client = client.openai_files
    # background_tasks.add_task(openai_file_client.create, file_create)
    file_create = FileCreate(
        file=(file.filename, await file.read(), file.content_type), purpose="assistants"
    )
    file_obj = await openai_file_client.create(ctx=ctx, file_create=file_create)
    return file_obj


@router.post("/openai")
async def upload_file_on_openai(
    request: Request,
    file: Annotated[UploadFile, File(description="File as UploadFile")],
    # background_tasks: BackgroundTasks,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> FileObject:
    ctx = request.state.context
    user = UserORM(**user_res.model_dump())
    client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider="openai")
    openai_file_client = client.openai_files
    # background_tasks.add_task(openai_file_client.create, file_create)
    file_create = FileCreate(
        file=(file.filename, await file.read(), file.content_type), purpose="assistants"
    )
    file_obj = await openai_file_client.create(ctx=ctx, file_create=file_create)
    return file_obj


@router.get("/list")
async def list_files_info(
    request: Request,
    openai_provider: Literal["openai", "azure"],
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> List[FileObject]:
    ctx = request.state.context
    user = UserORM(**user_res.model_dump())
    client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=openai_provider)
    openai_file_client = client.openai_files
    file_list = FileList(extra_query={"user_id": user_res.user.id})
    files_gen = await openai_file_client.list(file_list)
    return files_gen.data


@router.get("/")
async def get_file_info(
    request: Request,
    openai_provider: Literal["openai", "azure"],
    file_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> FileObject:
    ctx = request.state.context
    user = UserORM(**user_res.model_dump())
    client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=openai_provider)
    openai_file_client = client.openai_files
    file_retrieve = FileRetrieve(file_id=file_id, extra_query={"user_id": user_res.user.id})
    file = await openai_file_client.retrieve(file_retrieve)
    return file


@router.get("/content")
async def get_file(
    request: Request,
    openai_provider: Literal["openai", "azure"],
    file_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> str:
    ctx = request.state.context
    user = UserORM(**user_res.model_dump())
    try:
        client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=openai_provider)
        openai_file_client = client.openai_files
        file_content = FileContent(file_id=file_id, extra_query={"user_id": user_res.user.id})
        resp = await openai_file_client.retrieve_content(file_content)
        return resp.text
    except (BadRequestError, Exception) as e:
        if isinstance(e, BadRequestError):
            raise HTTPException(status_code=e.status_code, detail=e.message)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/")
async def delete_file(
    request: Request,
    openai_provider: Literal["openai", "azure"],
    file_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> FileDeleted:
    ctx = request.state.context
    user = UserORM(**user_res.model_dump())
    client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=openai_provider)
    openai_file_client = client.openai_files
    file_delete = FileDelete(file_id=file_id, extra_query={"user_id": user_res.user.id})
    file_deleted = await openai_file_client.delete(file_delete)
    return file_deleted
