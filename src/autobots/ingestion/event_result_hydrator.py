# src/autobots/ingestion/event_result_hydrator.py
import json
from typing import Any, Dict

from loguru import logger

from src.autobots.conn.object_storage.factory import get_public_storage, get_storage
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.user.user_orm_model import UserORM


async def hydrate_event_result_from_s3(
    doc: Dict[str, Any], user: UserORM, ctx: Context
) -> Dict[str, Any]:
    """
    Fetches the full EventResult document from S3 if a file_url is present.
    This function handles the "Claim Check" pattern for large results.
    If no file_url exists, it returns the original document as-is (legacy format).
    """
    file_url = doc.get("file_url")
    doc_id = doc.get("_id")  # Get doc_id for logging

    # Fast path: If no file_url, it's a legacy doc with full data in Mongo.
    if not file_url:
        logger.bind(ctx=ctx, doc_id=doc_id).debug(
            "No file_url found. Processing document as legacy (data in MongoDB)."
        )
        return doc

    # Slow path: Rehydrate the full document from S3.
    try:
        logger.bind(ctx=ctx, doc_id=doc_id, file_url=file_url).info(
            "Rehydrating full EventResult from object storage for indexing."
        )

        settings = SettingsProvider.sget()

        public_storage = get_public_storage(object_prefix="")
        public_key = await public_storage.get_key_if_owned_url(str(file_url))

        if public_key is not None:
            storage = public_storage
            storage_scope = "public"
        else:
            private_storage = get_storage(object_prefix="")
            private_key = await private_storage.get_key_if_owned_url(str(file_url))

            if private_key is None:
                logger.bind(
                    ctx=ctx,
                    doc_id=doc_id,
                    file_url=file_url,
                ).warning(
                    "EventResult URL is not owned by configured object storage; using lite data"
                )
                return doc

            storage = private_storage
            storage_scope = "private"

        logger.bind(
            ctx=ctx,
            doc_id=doc_id,
            provider=storage.provider,
            container=storage.container_name,
            storage_scope=storage_scope,
        ).info("Selected object storage for EventResult hydration")

        # The UserFiles helper needs the user and the correct top-level folder from settings.
        # Minimal compatibility tweak:
        # - action_run full payloads live under AWS_S3_ACTION_RUNS_FOLDER
        # - other EventResults (e.g. action_graph_run) continue using AWS_S3_RUNS_FOLDER
        top_level_folder = (
            settings.AWS_S3_ACTION_RUNS_FOLDER
            if doc.get("type") == "action_run"
            else settings.AWS_S3_RUNS_FOLDER
        )
        user_files = UserFiles(user=user, s3=storage, top_level_folder=top_level_folder)

        # The filename is simply the document's ID with a .json extension.
        filename = f"{doc_id}.json"

        file_bytes = await user_files.get_bytes(ctx=ctx, filename=filename)

        if file_bytes:
            full_doc = json.loads(file_bytes)

            # Normalize top-level id key for downstream ingestion code that expects Mongo-style "_id".
            if isinstance(full_doc, dict) and "_id" not in full_doc and "id" in full_doc:
                full_doc["_id"] = full_doc["id"]
            logger.bind(ctx=ctx, doc_id=doc_id).success(
                "Successfully hydrated full EventResult from object storage."
            )
            return full_doc
        else:
            logger.bind(ctx=ctx, doc_id=doc_id).warning(
                "file_url was present but object-storage fetch returned no data. Falling back to lite data in Mongo."
            )
            return doc  # Fallback to the original lite doc

    except Exception as e:
        logger.bind(ctx=ctx, doc_id=doc_id).error(
            f"Failed to fetch full EventResult from object storage: {e}. Using lite data as fallback."
        )
        return doc  # Fallback to the original lite doc
