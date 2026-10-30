# src/autobots/ingestion/s3_utils.py
from loguru import logger

from src.autobots.conn.object_storage.factory import get_public_storage


async def cleanup_staging_prefix(staging_job_key: str):
    """
    Safely deletes the job-specific folder within the object-storage staging area.

    This function derives the parent folder from the provided key and ensures
    it only ever deletes objects within the 'staging/' prefix for safety.
    The selected object-storage provider performs the prefix deletion.
    """
    if not staging_job_key:
        logger.warning("cleanup_staging_prefix called with an empty key. No action taken.")
        return

    # Derive the job-specific folder (e.g., "staging/job_uuid/") from the file key
    parts = staging_job_key.split("/")
    if len(parts) < 2:
        logger.error(f"Invalid staging key format for cleanup: {staging_job_key}")
        return

    prefix_to_delete = "/".join(parts[:-1]) + "/"

    # --- SAFETY CHECK: Hardcode the function to only operate on the staging area ---
    if not prefix_to_delete.startswith("staging/"):
        logger.error(
            f"DANGER: Attempted to clean up a prefix outside of the staging area: '{prefix_to_delete}'. Operation aborted."
        )
        return

    logger.info(f"Preparing to clean up staging prefix: {prefix_to_delete}")

    storage = get_public_storage(object_prefix="")

    try:
        deleted = await storage.delete_prefix(prefix_to_delete)
        if not deleted:
            logger.info(f"No objects found under prefix '{prefix_to_delete}'. Already clean.")
            return

        logger.success(
            f"Successfully cleaned up {len(deleted)} object(s) from staging prefix: "
            f"{prefix_to_delete}"
        )
    except Exception as e:
        logger.exception(
            f"An error occurred during staging cleanup for prefix '{prefix_to_delete}': {e}"
        )
