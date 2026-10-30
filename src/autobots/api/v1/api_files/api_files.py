from typing import Annotated, List, Optional

import supabase_auth
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from starlette.requests import Request

from src.autobots import SettingsProvider
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.conn.aws.aws_s3 import AwsS3, get_aws_s3, get_public_s3, get_s3
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.data_model.context import Context
from src.autobots.files.download_url_resolver import resolve_system_download_urls
from src.autobots.files.user_files import UserFiles
from src.autobots.files.user_files_model import (
    FileMeta,
    FilesAndProcessingParams,
    ResolvedDownloadUrl,
    ResolveDownloadUrlsRequest,
)
from src.autobots.integrations.app_creds.user_app_creds import UserAppCreds
from src.autobots.integrations.app_types import AppTypes
from src.autobots.space.space_auth import get_optional_space_member
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=SettingsProvider.sget().API_FILES, tags=[SettingsProvider.sget().API_FILES]
)


async def _get_customer_s3(
    ctx: Context,
    db: AsyncIOMotorDatabase,
    user: UserORM,
    connection_name: str,
) -> AwsS3:
    app_creds_doc = await UserAppCreds.get_app_creds_by_name(
        ctx=ctx,
        db=db,
        user=user,
        name=connection_name,
        app_name=AppTypes.aws,
        decrypt=True,
    )
    if not app_creds_doc:
        raise HTTPException(
            status_code=404,
            detail=f"AWS connection '{connection_name}' not found",
        )
    creds = app_creds_doc.creds
    configured_prefix = (creds.get("object_prefix") or "default").strip("/")
    customer_prefix = f"{configured_prefix}/{user.id}"
    return get_aws_s3(
        region_name=creds["region_name"],
        aws_access_key_id=creds["aws_access_key_id"],
        aws_secret_access_key=creds["aws_secret_access_key"],
        bucket_name=creds["bucket_name"],
        object_prefix=customer_prefix,
    )


@router.post("/")
async def upload_files(
    request: Request,
    files: Annotated[List[UploadFile], File(description="Multiple files as UploadFile")],
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> List[FileMeta]:
    ctx = request.state.context
    try:
        user = UserORM(id=user_res.user.id, space_member=space_member)
        s3 = get_s3()
        settings = SettingsProvider.sget()
        user_files = UserFiles(user, s3, settings)

        uploaded = await user_files.upload_files(ctx, files)
        logger.bind(ctx=ctx, uploaded=uploaded).info("Files uploaded")
        return uploaded
    except Exception as e:
        logger.bind(ctx=ctx).error(str(e))
        match e:
            case HTTPException():
                raise e
            case _:
                raise HTTPException(status_code=500, detail=str(e))


@router.post("/public/")
async def upload_public_files(
    request: Request,
    files: Annotated[List[UploadFile], File(description="Multiple files as UploadFile")],
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> List[FileMeta]:
    ctx = request.state.context
    try:
        user = UserORM(id=user_res.user.id, space_member=space_member)
        s3 = get_public_s3()
        settings = SettingsProvider.sget()
        user_files = UserFiles(user, s3, settings)

        uploaded = await user_files.upload_files(ctx, files)
        logger.bind(ctx=ctx, uploaded=uploaded).info("Files uploaded")
        return uploaded
    except Exception as e:
        logger.bind(ctx=ctx).error(str(e))
        match e:
            case HTTPException():
                raise e
            case _:
                raise HTTPException(status_code=500, detail=str(e))


@router.post("/customer/")
async def upload_customer_files(
    request: Request,
    files: Annotated[List[UploadFile], File(description="Multiple files as UploadFile")],
    connection_name: Annotated[
        str, Form(description="Name of the saved AWS connection to upload to")
    ],
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> List[FileMeta]:
    """
    Upload files to the caller's own connected private AWS S3 bucket.

    The target bucket/credentials are resolved from a saved AWS app credential
    identified by `connection_name` (scoped to the caller's user/space). Files
    are stored under the connection's configured object_prefix to preserve
    tenant isolation on the customer bucket.
    """
    ctx = request.state.context
    try:
        user = UserORM(id=user_res.user.id, space_member=space_member)
        aws_s3 = await _get_customer_s3(
            ctx=ctx,
            db=db,
            user=user,
            connection_name=connection_name,
        )
        settings = SettingsProvider.sget()
        user_files = UserFiles(
            user,
            aws_s3,
            settings,
            connection_name=connection_name,
        )

        uploaded = await user_files.upload_files(ctx, files)
        logger.bind(ctx=ctx, uploaded=uploaded, connection_name=connection_name).info(
            "Files uploaded to customer S3"
        )
        return uploaded
    except Exception as e:
        logger.bind(ctx=ctx, connection_name=connection_name).error(str(e))
        match e:
            case HTTPException():
                raise e
            case _:
                raise HTTPException(status_code=500, detail=str(e))


@router.get("/customer/")
async def get_customer_files(
    request: Request,
    connection_name: str,
    filename: Optional[str] = None,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> List[FileMeta]:
    ctx = request.state.context
    try:
        user = UserORM(id=user_res.user.id, space_member=space_member)
        aws_s3 = await _get_customer_s3(
            ctx=ctx,
            db=db,
            user=user,
            connection_name=connection_name,
        )
        user_files = UserFiles(
            user,
            aws_s3,
            SettingsProvider.sget(),
            connection_name=connection_name,
        )
        return await user_files.list_files(ctx, filename)
    except Exception as e:
        logger.bind(ctx=ctx, connection_name=connection_name).error(str(e))
        match e:
            case HTTPException():
                raise e
            case _:
                raise HTTPException(status_code=500, detail=str(e))


@router.delete("/customer/")
async def delete_customer_files(
    request: Request,
    filenames: List[str],
    connection_name: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> List[str]:
    ctx = request.state.context
    try:
        user = UserORM(id=user_res.user.id, space_member=space_member)
        aws_s3 = await _get_customer_s3(
            ctx=ctx,
            db=db,
            user=user,
            connection_name=connection_name,
        )
        user_files = UserFiles(
            user,
            aws_s3,
            SettingsProvider.sget(),
            connection_name=connection_name,
        )
        return await user_files.delete_files(ctx, filenames)
    except Exception as e:
        logger.bind(ctx=ctx, connection_name=connection_name).error(str(e))
        match e:
            case HTTPException():
                raise e
            case _:
                raise HTTPException(status_code=500, detail=str(e))


@router.get("/")
async def get_files(
    request: Request,
    filename: Optional[str] = None,
    is_public: Optional[bool] = False,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> List[FileMeta]:
    ctx = request.state.context
    try:
        user = UserORM(id=user_res.user.id, space_member=space_member)
        s3 = get_s3()
        if is_public:
            s3 = get_public_s3()
        user_files = UserFiles(user, s3)

        files = await user_files.list_files(ctx, filename)
        return files
    except Exception as e:
        logger.error(str(e))
        raise


@router.delete("/")
async def delete_files(
    request: Request,
    filenames: List[str],
    is_public: Optional[bool] = False,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> List[str]:
    ctx = request.state.context
    try:
        user = UserORM(id=user_res.user.id, space_member=space_member)
        s3 = get_s3()
        if is_public:
            s3 = get_public_s3()
        settings = SettingsProvider.sget()
        user_files = UserFiles(user, s3, settings)

        deleted = await user_files.delete_files(ctx, filenames)
        await user_files.delete_downloaded_files(ctx, filenames)
        return deleted
    except Exception as e:
        logger.bind(ctx=ctx).error(str(e))
        raise


@router.post("/process")
async def process_files(
    request: Request,
    files_and_processing_params: FilesAndProcessingParams,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> List[str]:
    ctx = request.state.context
    try:
        user = UserORM(id=user_res.user.id, space_member=space_member)
        s3 = get_s3()
        settings = SettingsProvider.sget()
        user_files = UserFiles(user, s3, settings)
        await user_files.init_unstructured_io(ctx=ctx, db=db)

        processed_files = []
        async for processed_file in user_files.processed_file_and_yield(
            ctx=ctx, files_and_processing_params=files_and_processing_params
        ):
            processed_files.append(processed_file)
        return processed_files
    except Exception as e:
        logger.error(str(e))
        raise


@router.get("/s3_files")
async def get_files_s3_async(
    request: Request,
    object_prefix: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> List[str] | None:
    ctx = request.state.context
    try:
        user = UserORM(id=user_res.user.id, space_member=space_member)
        app_creds_doc = await UserAppCreds.get_app_creds_by_app_name(
            ctx=Context(), db=db, user=user, app_name=AppTypes.aws, decrypt=True
        )
        aws_s3 = get_aws_s3(
            region_name=app_creds_doc.creds["region_name"],
            aws_access_key_id=app_creds_doc.creds["aws_access_key_id"],
            aws_secret_access_key=app_creds_doc.creds["aws_secret_access_key"],
            bucket_name=app_creds_doc.creds["bucket_name"],
            object_prefix=object_prefix,
        )
        objects = await aws_s3.list()
        files = []
        for obj in objects:
            url = await aws_s3.get_https_url(obj.key)
            files.append(str(url))
        return files
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Error while storing Aws S3 files in datastore: {str(e)}")


@router.post("/resolve-download-urls")
async def resolve_download_urls(
    request: Request,
    body: ResolveDownloadUrlsRequest,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> List[ResolvedDownloadUrl]:
    """
    Resolve file URLs into downloadable URLs.

    - MeetKiwi public S3 URLs are returned unchanged.
    - MeetKiwi private S3 URLs belonging to the caller are presigned
      (default 1 day, max 7 days).
    - Customer S3 and external URLs are returned unchanged.
    """
    ctx = request.state.context
    try:
        user = UserORM(id=user_res.user.id, space_member=space_member)
        return await resolve_system_download_urls(
            ctx=ctx,
            user=user,
            urls=[str(url) for url in body.urls],
            expiration_sec=body.expiration_sec,
        )
    except Exception as e:
        logger.bind(ctx=ctx).error(str(e))
        match e:
            case HTTPException():
                raise e
            case _:
                raise HTTPException(status_code=500, detail=str(e))
