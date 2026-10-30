import asyncio
from typing import Dict

import loguru
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.action.action_type.action_types import ActionType
from src.autobots.core.database.mongo_base import get_mongo_db

logger = loguru.logger.bind(script=__name__)

# Define the mapping of old action types to new ones
ACTION_TYPE_MAPPING: Dict[str, str] = {
    "text2text_llm_chat_openai": "llm_openai",  # Example mapping
}

# Define where action types can be found in different collections
COLLECTION_TYPE_PATHS = {
    "Actions": [  # Capitalized collection names
        {"path": "type", "is_array": False},
        {"path": "results.type", "is_array": True},
    ],
    "ActionGraphs": [
        {"path": "nodes.action.type", "is_array": True},
        {"path": "nodes.results.type", "is_array": True},
    ],
    "ActionResults": [
        {"path": "type", "is_array": False},
        {"path": "result.type", "is_array": False},
    ],
    "EventResults": [
        {"path": "type", "is_array": False},
        {"path": "result.type", "is_array": False},
        {"path": "result.results.type", "is_array": True},
    ],
}


async def print_connection_details(db: AsyncIOMotorDatabase) -> None:
    """
    Print database connection details
    """
    try:
        db_name = db.name
        conn_string = str(db.client.address)
        collections = await db.list_collection_names()

        logger.info("\n=== Database Connection Details ===")
        logger.info(f"Database Name: {db_name}")
        logger.info(f"Connection: {conn_string}")
        logger.info("\nAvailable Collections:")
        for collection in collections:
            count = await db[collection].count_documents({})
            logger.info(f"  - {collection}: {count} documents")

            # Print sample document with type field
            sample = await db[collection].find_one({"type": {"$exists": True}})
            if sample:
                logger.info(f"    Sample document in {collection}:")
                logger.info(f"    ID: {sample.get('_id')}")
                logger.info(f"    Type: {sample.get('type')}")
                logger.info(f"    Name: {sample.get('name')}")

    except Exception as e:
        logger.error(f"Error getting connection details: {str(e)}")
        raise


async def inspect_current_types(db: AsyncIOMotorDatabase) -> None:
    """
    Inspect and log current action types in all collections
    """
    try:
        logger.info("\n=== Current Action Types Inspection ===")

        for collection_name, field_paths in COLLECTION_TYPE_PATHS.items():
            collection = db[collection_name]

            logger.info(f"\nInspecting {collection_name} collection:")

            # Check if collection exists
            collections = await db.list_collection_names()
            if collection_name not in collections:
                logger.warning(f"Collection {collection_name} not found in database!")
                continue

            # Get total count
            total_count = await collection.count_documents({})
            logger.info(f"Total documents in collection: {total_count}")

            # Sample documents
            logger.info(f"\nSample documents from {collection_name}:")
            async for doc in collection.find({"type": {"$exists": True}}).limit(3):
                logger.info(f"Document ID: {doc.get('_id')}")
                logger.info(f"Type: {doc.get('type')}")
                logger.info(f"Name: {doc.get('name')}")
                logger.info("---")

            # Check each field path
            for field_config in field_paths:
                field_path = field_config["path"]

                # Count documents with this field
                field_count = await collection.count_documents({field_path: {"$exists": True}})
                logger.info(f"\nField '{field_path}' exists in {field_count} documents")

                # Get distinct types
                pipeline = [
                    {"$match": {field_path: {"$exists": True}}},
                    {"$group": {"_id": f"${field_path}"}},
                    {"$match": {"_id": {"$ne": None}}},
                ]

                distinct_types = await collection.aggregate(pipeline).to_list(None)
                types = [t["_id"] for t in distinct_types if t["_id"]]

                if types:
                    logger.info(f"Found types in {field_path}:")
                    for type_value in types:
                        count = await collection.count_documents({field_path: type_value})
                        logger.info(f"  - {type_value}: {count} documents")

                        # Check if this type is in our mapping
                        if type_value in ACTION_TYPE_MAPPING:
                            logger.info(
                                f"    ⚠️ This type will be migrated to: {ACTION_TYPE_MAPPING[type_value]}"
                            )
                else:
                    logger.info(f"No types found in {field_path}")

    except Exception as e:
        logger.error(f"Error during inspection: {str(e)}")
        raise


async def update_field(collection, old_type: str, new_type: str, field_config: Dict) -> int:
    """
    Update a specific field path in a collection
    Returns number of documents updated
    """
    field_path = field_config["path"]
    is_array = field_config["is_array"]

    try:
        # Log the update attempt
        logger.debug(f"Attempting update: {field_path} from '{old_type}' to '{new_type}'")

        # First, check if documents exist with the old type
        count_before = await collection.count_documents({field_path: {"$eq": old_type}})
        logger.debug(f"Found {count_before} documents with type '{old_type}' in {field_path}")

        if is_array:
            if "nodes.action.type" in field_path:
                logger.debug(f"Updating nested array field: {field_path}")
                query = {"nodes": {"$elemMatch": {"action.type": {"$eq": old_type}}}}
                update = {"$set": {"nodes.$[node].action.type": new_type}}
                array_filters = [{"node.action.type": {"$eq": old_type}}]

                logger.debug(f"Query: {query}")
                logger.debug(f"Update: {update}")
                logger.debug(f"Array filters: {array_filters}")

                result = await collection.update_many(query, update, array_filters=array_filters)
            elif "nodes.results.type" in field_path:
                logger.debug(f"Updating nested results array field: {field_path}")
                query = {
                    "nodes": {
                        "$elemMatch": {"results": {"$elemMatch": {"type": {"$eq": old_type}}}}
                    }
                }
                update = {"$set": {"nodes.$[node].results.$[result].type": new_type}}
                array_filters = [
                    {"node.results": {"$exists": True}},
                    {"result.type": {"$eq": old_type}},
                ]

                logger.debug(f"Query: {query}")
                logger.debug(f"Update: {update}")
                logger.debug(f"Array filters: {array_filters}")

                result = await collection.update_many(query, update, array_filters=array_filters)
            else:
                logger.debug(f"Updating simple array field: {field_path}")
                base_path = field_path.rsplit(".", 1)[0]
                query = {field_path: {"$eq": old_type}}
                update = {"$set": {f"{base_path}.$[elem].type": new_type}}
                array_filters = [{"elem.type": {"$eq": old_type}}]

                logger.debug(f"Query: {query}")
                logger.debug(f"Update: {update}")
                logger.debug(f"Array filters: {array_filters}")

                result = await collection.update_many(query, update, array_filters=array_filters)
        else:
            logger.debug(f"Updating simple field: {field_path}")
            query = {field_path: {"$eq": old_type}}
            update = {"$set": {field_path: new_type}}

            logger.debug(f"Query: {query}")
            logger.debug(f"Update: {update}")

            result = await collection.update_many(query, update)

        # Check the results
        modified_count = result.modified_count
        matched_count = result.matched_count

        logger.debug(f"Update results - Matched: {matched_count}, Modified: {modified_count}")

        # Verify the update
        count_after = await collection.count_documents({field_path: {"$eq": old_type}})
        logger.debug(
            f"After update: {count_after} documents still have type '{old_type}' in {field_path}"
        )

        if modified_count > 0:
            logger.success(f"Successfully updated {modified_count} documents")
        else:
            logger.warning(f"No documents were modified. Matched: {matched_count}")

        return modified_count

    except Exception as e:
        logger.error(f"Error updating field {field_path}: {str(e)}")
        logger.exception(e)
        raise


async def migrate_action_types(db: AsyncIOMotorDatabase) -> None:
    """
    Migrate action types in all relevant collections
    """
    try:
        total_updates = 0

        for collection_name, field_paths in COLLECTION_TYPE_PATHS.items():
            logger.info(f"\nProcessing collection: {collection_name}")
            collection = db[collection_name]

            # First, check if collection exists
            if collection_name not in await db.list_collection_names():
                logger.warning(f"Collection {collection_name} not found, skipping...")
                continue

            # Update documents for each old action type
            for old_type, new_type in ACTION_TYPE_MAPPING.items():
                logger.info(f"Processing type migration: {old_type} -> {new_type}")

                # Verify new type exists in current ActionType enum
                if new_type not in ActionType.__members__.values():
                    logger.warning(f"New action type {new_type} not found in ActionType enum")
                    continue

                # Update each field path that might contain action types
                for field_config in field_paths:
                    logger.debug(f"Processing field: {field_config['path']}")
                    updates = await update_field(collection, old_type, new_type, field_config)
                    total_updates += updates

                    if updates > 0:
                        logger.success(
                            f"Updated {updates} documents in {collection_name} "
                            f"field {field_config['path']} from {old_type} to {new_type}"
                        )
                    else:
                        logger.debug(
                            f"No updates needed in {collection_name} field {field_config['path']}"
                        )

        logger.success(f"Migration completed. Total documents updated: {total_updates}")

    except Exception as e:
        logger.error(f"Error during migration: {str(e)}")
        logger.exception(e)
        raise


async def validate_migration(db: AsyncIOMotorDatabase) -> None:
    """
    Validate that no documents still use old action types
    """
    try:
        old_types = ACTION_TYPE_MAPPING.keys()

        for collection_name, field_paths in COLLECTION_TYPE_PATHS.items():
            collection = db[collection_name]

            for old_type in old_types:
                for field_config in field_paths:
                    field_path = field_config["path"]
                    # Build query for the specific field path
                    query = {field_path: old_type}
                    count = await collection.count_documents(query)

                    if count > 0:
                        logger.warning(
                            f"Found {count} documents in {collection_name} "
                            f"field {field_path} still using old type: {old_type}"
                        )

    except Exception as e:
        logger.error(f"Error during validation: {str(e)}")
        raise


async def rollback_migration(db: AsyncIOMotorDatabase) -> None:
    """
    Rollback the migration if needed
    """
    try:
        # Reverse the mapping
        reverse_mapping = {v: k for k, v in ACTION_TYPE_MAPPING.items()}
        total_rollbacks = 0

        for collection_name, field_paths in COLLECTION_TYPE_PATHS.items():
            collection = db[collection_name]

            for new_type, old_type in reverse_mapping.items():
                for field_config in field_paths:
                    rollbacks = await update_field(collection, new_type, old_type, field_config)
                    total_rollbacks += rollbacks

                    if rollbacks > 0:
                        logger.info(
                            f"Rolled back {rollbacks} documents in {collection_name} "
                            f"field {field_config['path']} from {new_type} to {old_type}"
                        )

        logger.info(f"Rollback completed. Total documents rolled back: {total_rollbacks}")

    except Exception as e:
        logger.error(f"Error during rollback: {str(e)}")
        raise


async def main():
    """
    Main migration script with safety checks
    """
    db = next(get_mongo_db())

    try:
        # Print connection details
        logger.info("\n=== Starting Migration Script ===")
        await print_connection_details(db)

        # Inspect current state
        logger.info("\nInspecting current state of action types...")
        await inspect_current_types(db)

        # Confirm mappings
        logger.info("\nAction type mappings to be applied:")
        for old_type, new_type in ACTION_TYPE_MAPPING.items():
            logger.info(f"  {old_type} -> {new_type}")

        # Backup check
        logger.info("\n⚠️  Please ensure you have backed up your database before proceeding!")
        input("Press Enter to continue or Ctrl+C to abort...")

        # Run migration
        logger.info("\nStarting migration...")
        await migrate_action_types(db)

        # Validate migration
        logger.info("\nValidating migration...")
        await validate_migration(db)

        # Ask for confirmation
        logger.info("\nMigration completed. Please verify the changes.")
        confirmation = input("Type 'CONFIRM' to keep changes or anything else to rollback: ")

        if confirmation != "CONFIRM":
            logger.info("\nRolling back changes...")
            await rollback_migration(db)
            logger.info("Rollback completed.")
        else:
            logger.info("\nMigration successfully completed and confirmed.")

    except Exception as e:
        logger.error(f"\nMigration failed: {str(e)}")
        logger.info("Attempting rollback...")
        await rollback_migration(db)
        raise


if __name__ == "__main__":
    asyncio.run(main())
