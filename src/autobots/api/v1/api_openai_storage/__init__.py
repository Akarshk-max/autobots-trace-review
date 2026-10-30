from fastapi import APIRouter

from src.autobots.api.v1.api_openai_storage import (
    api_openai_files,
    api_openai_vector_store_file_batches,
    api_openai_vector_store_files,
    api_openai_vector_stores,
)

router = APIRouter()

router.include_router(api_openai_files.router)
router.include_router(api_openai_vector_stores.router)
router.include_router(api_openai_vector_store_file_batches.router)
router.include_router(api_openai_vector_store_files.router)
