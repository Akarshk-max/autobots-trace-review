from fastapi import APIRouter

from src.autobots import SettingsProvider

# from src.autobots.datastore.datastore_result.datastore_result_doc_model import DatastoreResultDoc
# from src.autobots.datastore.user_datastore import UserDatastore
#
router = APIRouter(
    prefix=SettingsProvider.sget().API_DATASTORE, tags=[SettingsProvider.sget().API_DATASTORE]
)

#
# @router.post("/")
# async def create_datastore(
#     request: Request,
#     name: str,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> DatastoreMetaDoc:
#     ctx = request.state.context
#     try:
#         user_orm = UserORM(id=user_res.user.id)
#         unstructured_io_client: UnstructuredIO = await AppConn.get_unstructured_io(
#             ctx=ctx, user=user_orm, db=db
#         )
#         user_datastore_meta = await UserDatastore(user_orm, db, unstructured_io_client).init(name)
#         return user_datastore_meta
#     except Exception as e:
#         logger.error(str(e))
#         raise HTTPException(500, "unable to create datastore")
#
#
# @router.get("/")
# async def list_datastore(
#     request: Request,
#     limit: int = 100,
#     offset: int = 0,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> List[DatastoreMetaDoc]:
#     ctx = request.state.context
#     try:
#         user_orm = UserORM(id=user_res.user.id)
#         unstructured_io_client: UnstructuredIO = await AppConn.get_unstructured_io(
#             ctx=ctx, user=user_orm, db=db
#         )
#         user_datastore_meta = await UserDatastore(user_orm, db, unstructured_io_client).list(
#             limit, offset
#         )
#         return user_datastore_meta
#     except Exception as e:
#         logger.error(str(e))
#         raise HTTPException(500, "unable to list datastore")
#
#
# @router.get("/{name}")
# async def get_datastore(
#     request: Request,
#     name: str,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> DatastoreMetaDoc:
#     ctx = request.state.context
#     try:
#         user_orm = UserORM(id=user_res.user.id)
#         unstructured_io_client: UnstructuredIO = await AppConn.get_unstructured_io(
#             ctx=ctx, user=user_orm, db=db
#         )
#         user_datastore_meta = await UserDatastore(user_orm, db, unstructured_io_client).get(name)
#         return user_datastore_meta
#     except Exception as e:
#         logger.error(str(e))
#         raise HTTPException(500, "unable to get datastore")
#
#
# @router.post("/{id}/store_text")
# async def store_text(
#     request: Request,
#     datastore_id: str,
#     text: TextObj,
#     chunk_token_size: int = 512,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ):
#     ctx = request.state.context
#     try:
#         user_orm = UserORM(id=user_res.user.id)
#         unstructured_io_client: UnstructuredIO = await AppConn.get_unstructured_io(
#             ctx=ctx, user=user_orm, db=db
#         )
#         user_datastore = await UserDatastore(user_orm, db, unstructured_io_client).hydrate(
#             datastore_id
#         )
#         await user_datastore.put_data(ctx=ctx, data=text.text, chunk_token_size=chunk_token_size)
#         return {"done": "ok"}
#     except Exception as e:
#         logger.error(str(e))
#         raise HTTPException(500, "Error while storing text in datastore")
#
#
# @router.post("/{id}/store_file")
# async def upload_files(
#     request: Request,
#     datastore_id: str,
#     files: Annotated[list[UploadFile], File(description="Multiple files as UploadFile")],
#     chunk_size: int = 500,
#     ocr_detail: Literal["low", "medium", "high"] = "low",
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ):
#     ctx = request.state.context
#     try:
#         user = UserORM(id=user_res.user.id)
#         unstructured_io_client: UnstructuredIO = await AppConn.get_unstructured_io(
#             ctx=ctx, user=user, db=db
#         )
#         user_datastore = await UserDatastore(user, db, unstructured_io_client).hydrate(datastore_id)
#
#         hi_res_model_name = "yolox"
#         if ocr_detail == "low":
#             hi_res_model_name = "yolox_quantized"  # noqa E701
#         elif ocr_detail == "medium":
#             hi_res_model_name = "yolox"  # noqa E701
#         elif ocr_detail == "high":
#             hi_res_model_name = "chipper"  # noqa E701
#
#         partition_parameters_params = PartitionParametersParams(
#             combine_under_n_chars=chunk_size,
#             strategy="hi_res",
#             hi_res_model_name=hi_res_model_name,
#             pdf_infer_table_structure=True,
#         )
#         await user_datastore.put_files(files, partition_parameters_params)
#         return {"done": "ok"}
#     except Exception as e:
#         logger.error(str(e))
#         raise HTTPException(500, "Error while storing files in datastore")
#
#
# @router.post("/store_image_to_s3")
# async def upload_image_files_to_s3(
#     files: Annotated[List[UploadFile], File(description="file as UploadFile")],
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
# ) -> List[HttpUrl]:
#     try:
#         user = UserORM(id=user_res.user.id)
#         random_uuid = uuid.uuid4()
#         s3 = get_public_s3(object_prefix=f"claid/{user.id}/{random_uuid}/input/")
#
#         result = []
#         for file in files:
#             # Convert BinaryIO to BytesIO
#             bytes_io = BytesIO(await file.read())
#             object_url = await s3.put_file_obj(file_obj=bytes_io, filename=file.filename)
#             result.append(object_url)
#         return result
#
#     except Exception as e:
#         logger.error(str(e))
#         raise HTTPException(500, "Error while storing files in datastore")
#
#
# @router.post("/{id}/store_urls")
# async def store_urls(
#     request: Request,
#     datastore_id: str,
#     urls: List[HttpUrl],
#     chunk_size: int = 500,
#     ocr_detail: Literal["low", "medium", "high"] = "low",
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ):
#     ctx = request.state.context
#     try:
#         user_orm = UserORM(id=user_res.user.id)
#         unstructured_io_client: UnstructuredIO = await AppConn.get_unstructured_io(
#             ctx=ctx, user=user_orm, db=db
#         )
#         user_datastore = await UserDatastore(user_orm, db, unstructured_io_client).hydrate(
#             datastore_id
#         )
#
#         hi_res_model_name = "yolox"
#         if ocr_detail == "low":
#             hi_res_model_name = "yolox_quantized"  # noqa E701
#         elif ocr_detail == "medium":
#             hi_res_model_name = "yolox"  # noqa E701
#         elif ocr_detail == "high":
#             hi_res_model_name = "chipper"  # noqa E701
#
#         partition_parameters_params = PartitionParametersParams(
#             combine_under_n_chars=chunk_size,
#             strategy="hi_res",
#             hi_res_model_name=hi_res_model_name,
#             pdf_infer_table_structure=True,
#         )
#
#         await user_datastore.put_urls(
#             urls=urls, partition_parameters_params=partition_parameters_params
#         )
#         return {"done": "ok"}
#     except Exception as e:
#         logger.error(str(e))
#         raise HTTPException(500, "Error while storing URLs in datastore")
#
#
# @router.post("/{id}/search")
# async def search(
#     request: Request,
#     datastore_id: str,
#     query: TextObj,
#     top_k: int = 10,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> List[str]:
#     ctx = request.state.context
#     try:
#         user_orm = UserORM(id=user_res.user.id)
#         unstructured_io_client: UnstructuredIO = await AppConn.get_unstructured_io(
#             ctx=ctx, user=user_orm, db=db
#         )
#         user_datastore = await UserDatastore(user_orm, db, unstructured_io_client).hydrate(
#             datastore_id
#         )
#         results = await user_datastore.search(query=query.text, top_k=top_k)
#         return results
#     except Exception as e:
#         logger.error(str(e))
#         raise HTTPException(500, "Error while search in datastore")
#
#
# @router.delete("/{id}")
# async def delete_datastore(
#     request: Request,
#     datastore_id: str,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> DatastoreMetaDoc:
#     ctx = request.state.context
#     try:
#         user_orm = UserORM(id=user_res.user.id)
#         unstructured_io_client: UnstructuredIO = await AppConn.get_unstructured_io(
#             ctx=ctx, user=user_orm, db=db
#         )
#         datastore_meta_doc = await UserDatastore(
#             user_orm, db, unstructured_io_client
#         ).get_by_datastore_id(datastore_id)
#         if datastore_meta_doc is None:
#             raise HTTPException(400, "Datastore_meta_doc not found")
#         delete_result = await UserDatastore(user_orm, db, unstructured_io_client).delete(
#             datastore_id
#         )
#         deleted_count = delete_result.deleted_count
#         if deleted_count != 1:
#             raise HTTPException(500, "Error in deleting datastore_meta_doc")
#         return datastore_meta_doc
#     except Exception as e:
#         logger.error(str(e))
#         raise HTTPException(500, "Error while deleting datastore")
#
#
# @router.post("/{id}/store_data/async")
# async def store_data_async(
#     request: Request,
#     background_tasks: BackgroundTasks,
#     datastore_id: str,
#     text: TextObj,
#     webhook: Webhook | None = None,
#     chunk_size: int = 500,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> DatastoreResultDoc | None:
#     ctx = request.state.context
#     try:
#         user = UserORM(id=user_res.user.id)
#         unstructured_io_client: UnstructuredIO = await AppConn.get_unstructured_io(
#             ctx=ctx, user=user, db=db
#         )
#         user_datastore = UserDatastore(user, db, unstructured_io_client)
#         datastore_result_doc = await user_datastore.put_resource_async(
#             datastore_id=datastore_id,
#             data=text.text,
#             files=None,
#             urls=None,
#             chunk_size=chunk_size,
#             background_tasks=background_tasks,
#             webhook=webhook,
#         )
#         return datastore_result_doc
#     except Exception as e:
#         logger.error(str(e))
#         raise HTTPException(500, "Error while storing data in datastore")
#
#
# @router.post("/{id}/store_files/async")
# async def store_files_async(
#     request: Request,
#     background_tasks: BackgroundTasks,
#     datastore_id: str,
#     files: Annotated[list[UploadFile], File(description="Multiple files as UploadFile")],
#     webhook: Webhook | None = None,
#     chunk_size: int = 500,
#     ocr_detail: Literal["low", "medium", "high"] = "low",
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> DatastoreResultDoc | None:
#     ctx = request.state.context
#     try:
#         user = UserORM(id=user_res.user.id)
#         unstructured_io_client: UnstructuredIO = await AppConn.get_unstructured_io(
#             ctx=ctx, user=user, db=db
#         )
#         user_datastore = UserDatastore(user, db, unstructured_io_client)
#         datastore_result_doc = await user_datastore.put_resource_async(
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
#         logger.error(str(e))
#         raise HTTPException(500, "Error while storing files in datastore")
#
#
# @router.post("/{id}/store_urls/async")
# async def store_urls_async(
#     request: Request,
#     background_tasks: BackgroundTasks,
#     datastore_id: str,
#     urls: List[HttpUrl],
#     webhook: Webhook | None = None,
#     chunk_size: int = 500,
#     ocr_detail: Literal["low", "medium", "high"] = "low",
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> DatastoreResultDoc | None:
#     ctx = request.state.context
#     try:
#         user = UserORM(id=user_res.user.id)
#         unstructured_io_client: UnstructuredIO = await AppConn.get_unstructured_io(
#             ctx=ctx, user=user, db=db
#         )
#         user_datastore = UserDatastore(user, db, unstructured_io_client)
#         datastore_result_doc = await user_datastore.put_resource_async(
#             datastore_id=datastore_id,
#             data=None,
#             files=None,
#             urls=urls,
#             chunk_size=chunk_size,
#             ocr_detail=ocr_detail,
#             background_tasks=background_tasks,
#             webhook=webhook,
#         )
#         return datastore_result_doc
#     except Exception as e:
#         logger.error(str(e))
#         raise HTTPException(500, "Error while storing urls in datastore")
#
#
# @router.get("/result/{datastore_put_result_id}")
# async def get_datastore_put_result(
#     request: Request,
#     datastore_put_result_id: str,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ):
#     ctx = request.state.context
#     try:
#         user = UserORM(id=user_res.user.id)
#         unstructured_io_client: UnstructuredIO = await AppConn.get_unstructured_io(
#             ctx=ctx, user=user, db=db
#         )
#         user_datastore = UserDatastore(user, db, unstructured_io_client)
#         return await user_datastore.get_datastore_put_result(datastore_put_result_id)
#     except Exception as e:
#         logger.error(str(e))
#         raise HTTPException(500, "Internal server error")
