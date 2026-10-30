import uuid
from typing import List

import supabase_auth
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots import SettingsProvider
from src.autobots.action.action.common_action_models import TextObj
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.conn.aws.aws_s3 import get_s3
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.datastore.datastore_meta_doc_model import DatastoreMetaDoc

# from src.autobots.datastore.datastore_result.datastore_result_doc_model import DatastoreResultDoc
from src.autobots.datastore.multidatastore import SearchResult
from src.autobots.datastore.user_multidatastore import UserDatastore
from src.autobots.files.user_files import UserFiles
from src.autobots.space.space_auth import get_optional_space_member
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=SettingsProvider.sget().API_VECTORSTORE, tags=[SettingsProvider.sget().API_VECTORSTORE]
)

# ==========================================
# --- V1 Endpoints (Chroma Backend) ---
# ==========================================


@router.post("/")
async def create_datastore(
    name: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> DatastoreMetaDoc:
    try:
        user_orm = UserORM(id=user_res.user.id, space_member=space_member)
        user_datastore_meta = await UserDatastore(user_orm, db).init(name)
        return user_datastore_meta
    except Exception as e:
        logger.error(str(e))
        raise HTTPException(500, "unable to create datastore")


@router.get("/")
async def list_datastore(
    limit: int = 100,
    offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> List[DatastoreMetaDoc]:
    try:
        user_orm = UserORM(id=user_res.user.id, space_member=space_member)
        user_datastore_meta = await UserDatastore(user_orm, db).list(limit, offset)
        return user_datastore_meta
    except Exception as e:
        logger.error(str(e))
        raise HTTPException(500, "unable to list datastore")


@router.get("/{name}")
async def get_datastore(
    name: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> DatastoreMetaDoc:
    try:
        user_orm = UserORM(id=user_res.user.id, space_member=space_member)
        user_datastore_meta = await UserDatastore(user_orm, db).get(name)
        return user_datastore_meta
    except Exception as e:
        logger.error(str(e))
        raise HTTPException(500, "unable to get datastore")


# @router.post("/{id}/store_text")
# async def store_text(
#         datastore_id: str,
#         text: TextObj,
#         chunk_token_size: int = 512,
#         user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#         db: AsyncIOMotorDatabase = Depends(get_mongo_db)
# ):
#     try:
#         user_orm = UserORM(id=UUID(user_res.user.id))
#         user_datastore = await UserDatastore(user_orm, db).hydrate(datastore_id)
#         await user_datastore.put_data(data=text.text, chunk_token_size=chunk_token_size)
#         return {"done": "ok"}
#     except Exception as e:
#         logger.error(str(e))
#         raise HTTPException(500, "Error while storing text in datastore")


# @router.post("/store_image_to_s3")
# async def upload_image_files_to_s3(
#         files: Annotated[List[UploadFile], File(description="file as UploadFile")],
#         user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token)
# ) -> List[HttpUrl]:
#     try:
#         user = UserORM(id=UUID(user_res.user.id))
#         random_uuid = uuid.uuid4()
#         s3 = get_public_s3(object_prefix=f'claid/{user.id}/{random_uuid}/input/')

#         result = []
#         for file in files:
#             # Convert BinaryIO to BytesIO
#             bytes_io = BytesIO(await file.read())
#             object_url = await s3.put_file_obj(file_obj=bytes_io, filename=file.filename)
#             result.append(object_url)
#         return result

#     except Exception as e:
#         logger.error(str(e))
#         raise HTTPException(500, "Error while storing files in datastore")


# @router.post("/{id}/store_urls")
# async def store_urls(
#         datastore_id: str,
#         urls: List[HttpUrl],
#         chunk_size: int = 500,
#         ocr_detail: Literal["low", "medium", "high"] = "low",
#         user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#         db: AsyncIOMotorDatabase = Depends(get_mongo_db)
# ):
#     try:
#         user_orm = UserORM(id=UUID(user_res.user.id))
#         user_datastore = await UserDatastore(user_orm, db).hydrate(datastore_id)

#         hi_res_model_name = "yolox"
#         if ocr_detail == "low":
#             hi_res_model_name = "yolox_quantized"  # noqa E701
#         elif ocr_detail == "medium":
#             hi_res_model_name = "yolox"  # noqa E701
#         elif ocr_detail == "high":
#             hi_res_model_name = "chipper"  # noqa E701

#         partition_parameters_params = PartitionParametersParams(
#             combine_under_n_chars=chunk_size,
#             strategy="hi_res",
#             hi_res_model_name=hi_res_model_name,
#             pdf_infer_table_structure=True
#         )

#         await user_datastore.put_urls(urls=urls, partition_parameters_params=partition_parameters_params)
#         return {"done": "ok"}
#     except Exception as e:
#         logger.error(str(e))
#         raise HTTPException(500, "Error while storing URLs in datastore")


@router.post("/{id}/search")
async def search(
    datastore_id: str,
    query: TextObj,
    top_k: int = 10,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> List[SearchResult]:
    try:
        user_orm = UserORM(id=user_res.user.id, space_member=space_member)
        user_datastore = await UserDatastore(user_orm, db).hydrate(datastore_id)
        results = await user_datastore.search(query=query.text, top_k=top_k)
        return results
    except Exception as e:
        logger.error(str(e))
        raise HTTPException(500, "Error while search in datastore")


@router.delete("/{id}")
async def delete_datastore(
    datastore_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> DatastoreMetaDoc:
    try:
        user_orm = UserORM(id=user_res.user.id, space_member=space_member)
        datastore_meta_doc = await UserDatastore(user_orm, db).get_by_datastore_id(datastore_id)
        if datastore_meta_doc is None:
            raise HTTPException(400, "Datastore_meta_doc not found")
        delete_result = await UserDatastore(user_orm, db).delete(datastore_id)
        deleted_count = delete_result.deleted_count
        if deleted_count != 1:
            raise HTTPException(500, "Error in deleting datastore_meta_doc")
        return datastore_meta_doc
    except Exception as e:
        logger.error(str(e))
        raise HTTPException(500, "Error while deleting datastore")


# @router.post("/{id}/store_data/async")
# async def store_data_async(
#         background_tasks: BackgroundTasks,
#         datastore_id: str,
#         text: TextObj,
#         webhook: Webhook | None = None,
#         chunk_size: int = 500,
#         user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#         db: AsyncIOMotorDatabase = Depends(get_mongo_db)
# ) -> DatastoreResultDoc | None:
#     try:
#         user = UserORM(id=UUID(user_res.user.id))
#         user_datastore = UserDatastore(user, db)
#         datastore_result_doc = await user_datastore.put_resource_async(
#             datastore_id=datastore_id,
#             data=text.text,
#             files=None,
#             urls=None,
#             chunk_size=chunk_size,
#             background_tasks=background_tasks,
#             webhook=webhook
#         )
#         return datastore_result_doc
#     except Exception as e:
#         logger.error(str(e))
#         raise HTTPException(500, "Error while storing data in datastore")


# @router.post("/{id}/store_files/async")
# async def store_files_async(
#     request: Request,
#     background_tasks: BackgroundTasks,
#     datastore_id: str,
#     files: Annotated[list[UploadFile], File(description="Multiple files as UploadFile")],
#     webhook: Webhook | None = None,
#     chunk_size: int = 500,
#     top_k: int = 10,
#     ocr_detail: Literal["low", "medium", "high"] = "low",
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> DatastoreResultDoc | None:
#     ctx = request.state.context
#     try:
#         user = UserORM(id=user_res.user.id, space_member=space_member)
#         user_datastore = UserDatastore(user, db)
#         datastore_result_doc = await user_datastore.put_resource_async(
#             ctx=ctx,
#             datastore_id=datastore_id,
#             data=None,
#             files=files,
#             urls=None,
#             chunk_size=chunk_size,
#             ocr_detail=ocr_detail,
#             background_tasks=background_tasks,
#             webhook=webhook,
#         )
#         return datastore_result_doc
#     except Exception as e:
#         logger.bind(ctx=ctx).exception(f"Error while storing files in datastore {str(e)}")
#         raise HTTPException(500, f"Error while storing files in datastore {str(e)}")


# @router.post("/{id}/store_files_s3/async")
# async def store_files_s3_async(
#     request: Request,
#     background_tasks: BackgroundTasks,
#     datastore_id: str,
#     webhook: Webhook | None = None,
#     chunk_size: int = 500,
#     top_k: int = 10,
#     ocr_detail: Literal["low", "medium", "high"] = "low",
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> DatastoreResultDoc | None:
#     ctx = request.state.context
#     try:
#         user = UserORM(id=user_res.user.id, space_member=space_member)
#         user_datastore = UserDatastore(user, db)
#         datastore_result_doc = await user_datastore.put_resource_s3_async(
#             user=user,
#             ctx=ctx,
#             datastore_id=datastore_id,
#             data=None,
#             urls=None,
#             chunk_size=chunk_size,
#             ocr_detail=ocr_detail,
#             background_tasks=background_tasks,
#             webhook=webhook,
#         )
#         return datastore_result_doc
#     except Exception as e:
#         logger.bind(ctx=ctx).exception(f"Error while storing Aws S3 files in datastore: {str(e)}")
#         raise HTTPException(500, f"Error while storing Aws S3 files in datastore: {str(e)}")


# @router.post("/{id}/store_urls/async")
# async def store_urls_async(
#         background_tasks: BackgroundTasks,
#         datastore_id: str,
#         urls: List[HttpUrl],
#         webhook: Webhook | None = None,
#         chunk_size: int = 500,
#         ocr_detail: Literal["low", "medium", "high"] = "low",
#         user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#         db: AsyncIOMotorDatabase = Depends(get_mongo_db)
# ) -> DatastoreResultDoc | None:
#     try:
#         user = UserORM(id=UUID(user_res.user.id))
#         user_datastore = UserDatastore(user, db)
#         datastore_result_doc = await user_datastore.put_resource_async(
#             datastore_id=datastore_id,
#             data=None,
#             files=None,
#             urls=urls,
#             chunk_size=chunk_size,
#             ocr_detail=ocr_detail,
#             background_tasks=background_tasks,
#             webhook=webhook
#         )
#         return datastore_result_doc
#     except Exception as e:
#         logger.error(str(e))
#         raise HTTPException(500, "Error while storing urls in datastore")


@router.get("/result/{datastore_put_result_id}")
async def get_datastore_put_result(
    datastore_put_result_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    try:
        user = UserORM(id=user_res.user.id, space_member=space_member)
        user_datastore = UserDatastore(user, db)
        return await user_datastore.get_datastore_put_result(datastore_put_result_id)
    except Exception as e:
        logger.error(str(e))
        raise HTTPException(500, "Internal server error")


# ==========================================
# --- V2 Endpoints (Weaviate Backend) ---
# ==========================================


@router.post("/v2/collections/", description="Create a new Weaviate-backed collection")
async def v2_create_collection(
    name: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> DatastoreMetaDoc:
    """Creates a new, empty collection ready for ingestion into Weaviate."""
    try:
        user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
        # The V2 method in UserDatastore handles Weaviate-specific creation
        user_datastore_meta = await UserDatastore(user_orm, db).v2_create_collection(name)
        return user_datastore_meta
    except Exception as e:
        logger.error(f"Failed to create V2 collection: {e}")
        raise HTTPException(status_code=500, detail="Unable to create Weaviate collection.")


@router.get("/v2/collections/", description="List all Weaviate-backed collections")
async def v2_list_collections(
    limit: int = 100,
    offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> List[DatastoreMetaDoc]:
    """Lists all collections for the user that are managed by the Weaviate backend."""
    try:
        user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
        weaviate_collections = await UserDatastore(user_orm, db).v2_list_collections(limit, offset)
        return weaviate_collections
    except Exception as e:
        logger.error(f"Failed to list V2 collections: {e}")
        raise HTTPException(status_code=500, detail="Unable to list Weaviate collections.")


@router.post(
    "/v2/collections/{collection_name}/files/async",
    description="Ingest a file into a Weaviate collection",
)
async def v2_ingest_file_into_collection(
    request: Request,
    collection_name: str,
    file: UploadFile,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """
    Accepts a file, stages it to a temporary S3 path, and queues it for ingestion.
    """
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)

    # 1. Stage the file to a temporary location in S3
    s3_client = get_s3()
    s3_client.set_object_prefix("staging/")  # A neutral, temporary staging area

    try:
        # Use UserFiles helper for a consistent and secure filename
        secure_filename_helper = UserFiles(user_orm, s3_client)
        secure_filename = await secure_filename_helper.secure_filename(
            ctx=ctx, filename=file.filename
        )

        if not secure_filename:
            raise HTTPException(status_code=400, detail="Invalid filename provided.")

        # Create a unique path for the staged file to avoid collisions
        staged_s3_key = f"staging/{uuid.uuid4().hex}/{secure_filename}"

        await s3_client.upload_fileobj(ctx, file.file, staged_s3_key)
        logger.info(f"Staged file for ingestion at S3 key: {staged_s3_key}")
    except Exception as e:
        logger.error(f"Failed to stage file to S3: {e}")
        raise HTTPException(status_code=500, detail="Could not stage file for ingestion.")

    # 2. Call the new, clean service method to queue the job
    try:
        user_datastore = UserDatastore(user_orm, db)
        await user_datastore.v2_queue_ingestion_job(collection_name, staged_s3_key, file.filename)
    except Exception as e:
        logger.error(f"Failed to queue ingestion job: {e}")
        # Clean up the orphaned S3 file if queueing fails
        await s3_client.delete(staged_s3_key)
        raise HTTPException(
            status_code=500, detail="Failed to queue the ingestion job after staging."
        )

    return {
        "message": f"File '{file.filename}' accepted and queued for ingestion into '{collection_name}'."
    }


@router.post(
    "/v2/collections/{collection_name}/search", description="Search a Weaviate-backed collection"
)
async def v2_search_collection(
    collection_name: str,
    query: TextObj,
    top_k: int = 10,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> List[SearchResult]:
    """Performs a semantic search within a specific user's tenant in a Weaviate collection."""
    try:
        user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
        # The V2 search method handles multi-tenancy correctly.
        results = await UserDatastore(user_orm, db).v2_search(collection_name, query.text, top_k)
        return results
    except Exception as e:
        logger.error(f"Failed to search V2 collection '{collection_name}': {e}")
        raise HTTPException(status_code=500, detail="Error while searching in Weaviate collection.")


@router.delete(
    "/v2/collections/{collection_name}", description="Delete a Weaviate-backed collection"
)
async def v2_delete_collection(
    collection_name: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> dict:
    """Deletes a Weaviate collection and its associated metadata for the user."""
    try:
        user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)

        # We need a corresponding v2_delete_collection method in UserDatastore
        success = await UserDatastore(user_orm, db).v2_delete_collection(collection_name)

        if not success:
            # This could happen if the collection was already deleted or never existed.
            # A 404 is appropriate here.
            raise HTTPException(
                status_code=404, detail="Collection not found or could not be deleted."
            )

        return {"message": f"Collection '{collection_name}' deleted successfully."}

    except HTTPException as e:
        raise e  # Re-raise known HTTP exceptions
    except Exception as e:
        logger.error(f"Failed to delete V2 collection '{collection_name}': {e}")
        raise HTTPException(status_code=500, detail="Could not delete Weaviate collection.")
