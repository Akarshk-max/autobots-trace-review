from datetime import datetime
from typing import Dict, List, Optional, Tuple

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase
from pymongo import UpdateOne
from pymongo.errors import BulkWriteError

from src.autobots.knowledge_base.kb_doc_model import (
    KnowledgeBase,
    KnowledgeBaseMembership,
    KnowledgeBaseRole,
)


class KnowledgeBaseCRUD:
    """
    Lightweight CRUD helper for KnowledgeBase documents.
    """

    def __init__(self, db: AsyncIOMotorDatabase):
        self.collection: AsyncIOMotorCollection = db["KnowledgeBases"]

    async def get(self, kb_id: str) -> Optional[KnowledgeBase]:
        doc = await self.collection.find_one({"_id": kb_id})
        return KnowledgeBase.model_validate(doc) if doc else None

    async def list_for_owner(self, owner_user_id: str) -> List[KnowledgeBase]:
        """
        Returns KBs where the user is the owner.
        (Membership-based KBs are fetched separately.)
        """
        kb_docs = await self.collection.find({"owner_user_id": owner_user_id}).to_list(length=None)
        return [KnowledgeBase.model_validate(d) for d in kb_docs]

    async def create(self, kb: KnowledgeBase) -> KnowledgeBase:
        data = kb.model_dump(by_alias=True, exclude_defaults=False)
        await self.collection.insert_one(data)
        return kb

    async def update_timestamp(self, kb_id: str) -> None:
        await self.collection.update_one(
            {"_id": kb_id}, {"$set": {"updated_at": datetime.utcnow()}}, upsert=False
        )


class KnowledgeBaseMembershipCRUD:
    """
    CRUD helper for KnowledgeBaseMembership documents.
    """

    def __init__(self, db: AsyncIOMotorDatabase):
        self.collection: AsyncIOMotorCollection = db["KnowledgeBaseMemberships"]

    async def get_for_user(self, kb_id: str, user_id: str) -> Optional[KnowledgeBaseMembership]:
        doc = await self.collection.find_one({"kb_id": kb_id, "user_id": user_id})
        # Backwards-compat: older upserts may have created ObjectId _id values.
        if doc and "_id" in doc and not isinstance(doc["_id"], str):
            doc["_id"] = str(doc["_id"])
        return KnowledgeBaseMembership.model_validate(doc) if doc else None

    async def list_for_kb(self, kb_id: str) -> List[KnowledgeBaseMembership]:
        docs = await self.collection.find({"kb_id": kb_id}).to_list(length=None)
        # Backwards-compat: ensure _id is always a string for the response model.
        for d in docs:
            if "_id" in d and not isinstance(d["_id"], str):
                d["_id"] = str(d["_id"])
        return [KnowledgeBaseMembership.model_validate(d) for d in docs]

    async def add_or_update_member(
        self, kb_id: str, user_id: str, role: KnowledgeBaseRole
    ) -> KnowledgeBaseMembership:
        now = datetime.utcnow()
        # Deterministic string id for stable API responses + easy joins.
        # IMPORTANT: Keep the filter on (kb_id, user_id) for backwards-compat with
        # any pre-existing membership docs that may already exist with ObjectId _id.
        member_id = f"{kb_id}:{user_id}"
        await self.collection.update_one(
            {"kb_id": kb_id, "user_id": user_id},
            {
                "$set": {
                    "kb_id": kb_id,
                    "user_id": user_id,
                    "role": role.value,
                    "updated_at": now,
                },
                # Ensure newly created docs use a string _id (Mongo will ignore this for existing docs).
                "$setOnInsert": {"_id": member_id, "created_at": now},
            },
            upsert=True,
        )
        doc = await self.collection.find_one({"kb_id": kb_id, "user_id": user_id})
        if doc and "_id" in doc and not isinstance(doc["_id"], str):
            doc["_id"] = str(doc["_id"])
        return KnowledgeBaseMembership.model_validate(doc)

    async def remove_member(self, kb_id: str, user_id: str) -> bool:
        # Backwards-compat: delete by (kb_id, user_id) so it works regardless of _id type.
        res = await self.collection.delete_one({"kb_id": kb_id, "user_id": user_id})
        return res.deleted_count == 1

    async def bulk_upsert_members(
        self, kb_id: str, user_role_pairs: List[Tuple[str, str]]
    ) -> Dict[str, int]:
        """
        Efficiently upserts multiple members using MongoDB bulk_write.
        Returns dictionary with 'inserted', 'modified', and 'matched' counts.
        """
        if not user_role_pairs:
            return {"inserted": 0, "modified": 0, "matched": 0}

        now = datetime.utcnow()
        operations = []

        for user_id, role_value in user_role_pairs:
            member_id = f"{kb_id}:{user_id}"
            # Upsert logic: Update role if exists, Insert if not
            op = UpdateOne(
                {"kb_id": kb_id, "user_id": user_id},
                {
                    "$set": {
                        "kb_id": kb_id,
                        "user_id": user_id,
                        "role": role_value,
                        "updated_at": now,
                    },
                    "$setOnInsert": {"_id": member_id, "created_at": now},
                },
                upsert=True,
            )
            operations.append(op)

        try:
            # ordered=False allows the DB to process parallelly and doesn't stop on one error
            result = await self.collection.bulk_write(operations, ordered=False)
            return {
                "inserted": result.upserted_count,
                "modified": result.modified_count,
                "matched": result.matched_count,
            }
        except BulkWriteError as e:
            # Summarize errors instead of dumping the huge details object
            write_errors = e.details.get("writeErrors", [])
            logger.warning(
                f"Bulk upsert partial failure for KB {kb_id}. "
                f"{len(write_errors)} errors. First error: {write_errors[0] if write_errors else 'Unknown'}"
            )

            # Return counts of what actually succeeded
            return {
                "inserted": e.details.get("nInserted", 0) + e.details.get("nUpserted", 0),
                "modified": e.details.get("nModified", 0),
                "matched": e.details.get("nMatched", 0),
            }
