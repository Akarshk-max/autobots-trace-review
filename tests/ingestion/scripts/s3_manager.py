# tests/ingestion/scripts/s3_manager.py

# Example usage: Upload a file to S3, list objects under a prefix, and delete objects under a prefix.
# Upload: poetry run python -m tests.ingestion.scripts.s3_manager upload /tmp/autobots_demo.txt users/dummyuser123/
# List: poetry run python -m tests.ingestion.scripts.s3_manager list users/dummyuser123/
# Delete: poetry run python -m tests.ingestion.scripts.s3_manager delete users/dummyuser123/

import argparse
import asyncio
import sys
from pathlib import Path

from loguru import logger

# --- Setup Project Path ---
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ruff: noqa: E402
from src.autobots.conn.aws.aws_s3 import AwsS3, get_aws_s3
from src.autobots.core.settings import SettingsProvider


# --- Helper to get a root-level S3 client ---
def get_s3_root_client() -> AwsS3:
    """
    Initializes an S3 client by getting settings from the central SettingsProvider.
    This function relies on the provider having been initialized beforehand.
    """
    settings = SettingsProvider.sget()

    return get_aws_s3(
        settings.AWS_S3_BUCKET_REGION,
        settings.AWS_ACCESS_KEY_ID,
        settings.AWS_SECRET_ACCESS_KEY,
        settings.AWS_S3_PUBLIC_BUCKET_NAME,
        object_prefix="",
    )


# --- CLI Action Handlers and main() function ---
async def list_objects(s3_client: AwsS3, prefix: str) -> list:
    """Reliably lists objects under a given prefix using direct boto3 calls."""
    logger.info(f"Listing objects under prefix: s3://{s3_client.bucket.name}/{prefix}")
    objects_iterator = await asyncio.to_thread(s3_client.bucket.objects.filter, Prefix=prefix)
    return list(objects_iterator)


async def delete_prefix_robustly(s3_client: AwsS3, prefix: str):
    """Reliably deletes all objects under a given prefix."""
    objects_to_delete = await list_objects(s3_client, prefix)

    if not objects_to_delete:
        logger.warning(f"No objects found under prefix '{prefix}'. Nothing to delete.")
        return

    keys_to_delete = [{"Key": obj.key} for obj in objects_to_delete]
    logger.info(f"Found {len(keys_to_delete)} object(s) to delete.")

    response = await asyncio.to_thread(
        s3_client.bucket.delete_objects, Delete={"Objects": keys_to_delete}
    )

    deleted = response.get("Deleted", [])
    errors = response.get("Errors", [])

    if deleted:
        logger.success(
            f"Deletion request sent. S3 reported {len(deleted)} item(s) in the manifest."
        )
    if errors:
        logger.error(f"S3 reported {len(errors)} errors during deletion:")
        for error in errors:
            logger.error(f"  - Key: {error.get('Key')}, Error: {error.get('Message')}")


# --- CLI Action Handlers ---
async def handle_list(args):
    s3_client = get_s3_root_client()
    objects = await list_objects(s3_client, args.prefix)
    if objects:
        print(f"\nFound {len(objects)} object(s):")
        for obj in objects:
            print(f"  - s3://{s3_client.bucket.name}/{obj.key} (Size: {obj.size} bytes)")
    else:
        print("\nNo objects found.")


async def handle_delete(args):
    s3_client = get_s3_root_client()
    prefix = args.prefix
    if not prefix.endswith("/"):
        prefix += "/"

    print(f"\nPreparing to delete ALL objects under prefix: s3://{s3_client.bucket.name}/{prefix}")
    confirm = input("This is irreversible. Are you sure? (yes/no): ")
    if confirm.lower() != "yes":
        print("Deletion cancelled.")
        return

    await delete_prefix_robustly(s3_client, prefix)


async def handle_upload(args):
    s3_client = get_s3_root_client()
    local_path = Path(args.local_path)
    if not local_path.exists():
        logger.error(f"Local path not found: {local_path}")
        return

    # Ensure prefix ends with a slash for clean joins
    prefix = args.prefix.rstrip("/") + "/"

    # If a remote key is given, use it. Otherwise, construct it from prefix and filename.
    remote_key = args.remote_key or (prefix + local_path.name)

    logger.info(f"Uploading '{local_path}' to 's3://{s3_client.bucket.name}/{remote_key}'...")

    try:
        await asyncio.to_thread(s3_client.bucket.upload_file, str(local_path), remote_key)
        logger.success("Upload complete.")
    except Exception as e:
        logger.error(f"Upload failed: {e}")


async def main():
    # Explicitly initialize the SettingsProvider.
    SettingsProvider.set_env_file(".env.local")
    SettingsProvider.set()

    # Now that settings are loaded, the rest of the script can run.
    parser = argparse.ArgumentParser(
        description="A general-purpose S3 management tool for the public bucket."
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    # List command
    list_parser = subparsers.add_parser("list", help="List objects under a prefix.")
    list_parser.add_argument("prefix", help="The S3 prefix to list (e.g., 'staging/').")

    # Delete command
    delete_parser = subparsers.add_parser("delete", help="Delete all objects under a prefix.")
    delete_parser.add_argument("prefix", help="The S3 prefix to delete (e.g., 'staging/job123/').")

    # Upload command
    upload_parser = subparsers.add_parser("upload", help="Upload a local file.")
    upload_parser.add_argument("local_path", help="The path to the local file to upload.")
    upload_parser.add_argument("prefix", help="The S3 prefix to upload into.")
    upload_parser.add_argument(
        "--remote-key",
        help="Optional: full remote key for the object. Defaults to <prefix>/<filename>.",
    )

    args = parser.parse_args()

    # Configure logger for clean script output
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    if args.action == "list":
        await handle_list(args)
    elif args.action == "delete":
        await handle_delete(args)
    elif args.action == "upload":
        await handle_upload(args)


if __name__ == "__main__":
    asyncio.run(main())
