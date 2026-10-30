from typing import List

from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase

from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.database.mongo_base_crud import CRUDBase, DocFindPage
from src.autobots.core.database.mongo_base_model import CollectionDocSearch
from src.autobots.space.space_doc_model import (
    SpaceDoc,
    SpaceDocCreate,
    SpaceDocFind,
    SpaceDocUpdate,
    SpaceLiteDoc,
    SpaceMemberDoc,
    SpaceMemberDocCreate,
    SpaceMemberDocFind,
    SpaceMemberDocUpdate,
    SpaceMemberLiteDoc,
)


class SpaceCRUD(
    CRUDBase[
        SpaceDoc,
        SpaceLiteDoc,
        SpaceDocCreate,
        SpaceDocFind,
        SpaceDocUpdate,
        CollectionDocSearch,
    ]
):
    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        self.document: AsyncIOMotorCollection = db[SpaceDoc.__collection__]
        super().__init__(SpaceDoc, SpaceLiteDoc, self.document)

    async def find_by_ids(
        self, space_ids: List[str], limit: int = 100, offset: int = 0
    ) -> DocFindPage:
        """Find Spaces by a list of space IDs (used after querying memberships)."""
        from bson import ObjectId

        object_ids = []
        for sid in space_ids:
            try:
                object_ids.append(ObjectId(sid))
            except Exception:
                object_ids.append(sid)

        find_params = {"_id": {"$in": object_ids}}
        docs = await self._find_raw(find_params=find_params, limit=limit, offset=offset)
        total_count = await self.count(find_params=find_params)
        return DocFindPage(docs=docs, total_count=total_count, limit=limit, offset=offset)


class SpaceMemberCRUD(
    CRUDBase[
        SpaceMemberDoc,
        SpaceMemberLiteDoc,
        SpaceMemberDocCreate,
        SpaceMemberDocFind,
        SpaceMemberDocUpdate,
        CollectionDocSearch,
    ]
):
    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        self.document: AsyncIOMotorCollection = db[SpaceMemberDoc.__collection__]
        super().__init__(SpaceMemberDoc, SpaceMemberLiteDoc, self.document)

    async def find_by_space_id(
        self, space_id: str, limit: int = 100, offset: int = 0
    ) -> DocFindPage:
        """Find all members of a space."""
        find_params = {"space_id": space_id}
        docs = await self._find_raw(find_params=find_params, limit=limit, offset=offset)
        total_count = await self.count(find_params=find_params)
        return DocFindPage(docs=docs, total_count=total_count, limit=limit, offset=offset)

    async def find_by_user_id(
        self, user_id: str, limit: int = 100, offset: int = 0
    ) -> DocFindPage:
        """Find all spaces a user belongs to."""
        find_params = {"user_id": user_id}
        docs = await self._find_raw(find_params=find_params, limit=limit, offset=offset)
        total_count = await self.count(find_params=find_params)
        return DocFindPage(docs=docs, total_count=total_count, limit=limit, offset=offset)

    async def distinct_space_ids_for_user(self, user_id: str) -> List[str]:
        """Every space the user belongs to, ids only.

        Cheaper than paging the memberships when the caller only needs the set
        to test grants against.
        """
        space_ids = await self.document.distinct("space_id", {"user_id": user_id})
        return [space_id for space_id in space_ids if space_id]

    async def distinct_user_ids_for_spaces(self, space_ids: List[str]) -> List[str]:
        """Everyone in the given spaces, ids only. Membership is checked by the caller."""
        if not space_ids:
            return []
        user_ids = await self.document.distinct(
            "user_id", {"space_id": {"$in": space_ids}}
        )
        return [user_id for user_id in user_ids if user_id]

    async def find_membership(self, space_id: str, user_id: str) -> SpaceMemberDoc | None:
        """Find a specific user's membership in a specific space."""
        find_params = {"space_id": space_id, "user_id": user_id}
        doc = await self.document.find_one(find_params)
        if doc is None:
            return None
        doc["_id"] = str(doc.get("_id"))
        return SpaceMemberDoc.model_validate(doc)

    async def find_by_id_unscoped(self, membership_id: str) -> SpaceMemberDoc | None:
        """Find a SpaceMemberDoc by _id without any implicit space scoping.

        NOTE: We cannot reuse CRUDBase.find_one(id=...) because CRUDBase injects
        space_id=None for models that declare space_id, which would incorrectly
        filter out real membership docs.
        """
        from bson import ObjectId

        if not membership_id:
            return None
        try:
            obj_id = ObjectId(membership_id)
        except Exception:
            obj_id = membership_id

        doc = await self.document.find_one({"_id": obj_id})
        if doc is None:
            return None
        doc["_id"] = str(doc.get("_id"))
        return SpaceMemberDoc.model_validate(doc)


    async def delete_by_space_id(self, space_id: str):
        """Delete all memberships for a space."""
        await self.document.delete_many({"space_id": space_id})

    async def count_owners(self, space_id: str) -> int:
        """Count the number of owners in a space."""
        return await self.document.count_documents({"space_id": space_id, "role": "owner"})
