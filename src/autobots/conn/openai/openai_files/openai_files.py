import asyncio
import time

from fastapi import HTTPException
from loguru import logger
from openai import AsyncAzureOpenAI, AsyncOpenAI, BadRequestError
from openai._base_client import AsyncPaginator
from openai._legacy_response import HttpxBinaryResponseContent
from openai.pagination import AsyncPage
from openai.types import FileDeleted, FileObject
from tenacity import retry, stop_after_attempt, wait_exponential

from src.autobots.conn.openai.openai_files.openai_files_model import (
    FileContent,
    FileCreate,
    FileDelete,
    FileList,
    FileRetrieve,
)
from src.autobots.data_model.context import Context


class OpenaiFiles:
    def __init__(self, openai_client: AsyncAzureOpenAI | AsyncOpenAI):
        self.client = openai_client

    async def list(self, file_list: FileList) -> AsyncPaginator[FileObject, AsyncPage[FileObject]]:
        try:
            return await self.client.files.list(**file_list.model_dump(exclude_none=True))
        except Exception as e:
            logger.exception(str(e))

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=20, max=30),
        reraise=True,
    )
    async def create(self, ctx: Context, file_create: FileCreate) -> FileObject:
        start_time = time.time()
        max_time = 600
        try:
            file_obj = await self.client.files.create(**file_create.model_dump(exclude_none=True))
            file_obj_lite = {
                "openai_file_id": file_obj.id,
                "filename": file_obj.filename,
                "status": file_obj.status,
                "status_details": file_obj.status_details,
                "created_at": file_obj.created_at,
                "purpose": file_obj.purpose,
            }
            logger.bind(ctx=ctx, openai_file=file_obj_lite).info("File sent to openai")
            # TODO: file_obj.status is going to be deprecated. Use self.retrieve to check if file is available

            is_processed = False
            while not is_processed:
                if file_obj.status == "processed":
                    logger.bind(ctx=ctx, openai_file=file_obj_lite).info("File uploaded to openai")
                    is_processed = True
                    break
                elif file_obj.status == "uploaded":
                    curr_time = time.time()
                    time_elapsed = curr_time - start_time
                    if time_elapsed >= max_time:
                        logger.bind(ctx=ctx, openai_file=file_obj_lite).info(
                            "File upload to openai timed out"
                        )
                        raise HTTPException(
                            status_code=500, detail="Error in Openai file_obj status"
                        )
                    await asyncio.sleep(10)
                    file_obj = await self.client.files.retrieve(file_obj.id)
                    continue
                elif file_obj.status == "error":
                    logger.bind(ctx=ctx, openai_file=file_obj_lite).info(
                        "File upload to openai failed"
                    )
                    raise HTTPException(status_code=500, detail="Error in Openai file_obj status")
                else:
                    logger.bind(ctx=ctx, openai_file=file_obj_lite).info(
                        "File upload to openai failed"
                    )
                    raise HTTPException(status_code=500, detail="Error in Openai file_obj status")
            return file_obj
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in Openai file create: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Error in Openai file create: {str(e)}")
        #
        #
        # try:
        #     file_obj = await self.client.files.create(**file_create.model_dump(exclude_none=True))
        #     logger.bind(ctx=ctx, openai_file=file_obj.model_dump()).info("File uploaded to openai")
        #     if file_obj.status == "error":
        #         raise HTTPException(status_code=500, detail="Error in Openai file_obj status")
        #     return file_obj
        # except Exception as e:
        #     logger.bind(ctx=ctx, file_create=file_create.model_dump()).exception(f"Error in Openai file create: {str(e)}")
        #     raise HTTPException(status_code=500, detail=f"Error in Openai file create: {str(e)}")

    async def delete(self, file_delete: FileDelete) -> FileDeleted:
        try:
            return await self.client.files.delete(**file_delete.model_dump(exclude_none=True))
        except Exception as e:
            logger.exception(str(e))

    async def retrieve(self, file_retrieve: FileRetrieve) -> FileObject:
        try:
            return await self.client.files.retrieve(**file_retrieve.model_dump(exclude_none=True))
        except Exception as e:
            logger.exception(str(e))

    async def retrieve_content(self, file_content: FileContent) -> HttpxBinaryResponseContent:
        try:
            content = await self.client.files.content(**file_content.model_dump(exclude_none=True))
            return content
        except (BadRequestError, Exception) as e:
            logger.exception(str(e))
            raise
