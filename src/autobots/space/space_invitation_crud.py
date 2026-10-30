from datetime import datetime

from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase

from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.database.mongo_base_crud import CRUDBase, DocFindPage
from src.autobots.core.database.mongo_base_model import CollectionDocSearch
from src.autobots.space.space_invitation_doc_model import (
    InvitationStatus,
    SpaceInvitationDoc,
    SpaceInvitationDocCreate,
    SpaceInvitationDocFind,
    SpaceInvitationDocUpdate,
    SpaceInvitationLiteDoc,
)


class SpaceInvitationCRUD(
    CRUDBase[
        SpaceInvitationDoc,
        SpaceInvitationLiteDoc,
        SpaceInvitationDocCreate,
        SpaceInvitationDocFind,
        SpaceInvitationDocUpdate,
        CollectionDocSearch,
    ]
):
    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        self.document: AsyncIOMotorCollection = db[SpaceInvitationDoc.__collection__]
        super().__init__(SpaceInvitationDoc, SpaceInvitationLiteDoc, self.document)

    async def find_pending_by_space(
        self, space_id: str, limit: int = 100, offset: int = 0
    ) -> DocFindPage:
        find_params = {"space_id": space_id, "status": InvitationStatus.pending}
        docs = await self._find_raw(find_params=find_params, limit=limit, offset=offset)
        total_count = await self.count(find_params=find_params)
        return DocFindPage(docs=docs, total_count=total_count, limit=limit, offset=offset)

    async def find_pending_by_email(
        self, email: str, limit: int = 100, offset: int = 0
    ) -> DocFindPage:
        find_params = {
            "invitee_email": email,
            "status": InvitationStatus.pending,
            "expires_at": {"$gt": datetime.now()},
        }
        docs = await self._find_raw(find_params=find_params, limit=limit, offset=offset)
        total_count = await self.count(find_params=find_params)
        return DocFindPage(docs=docs, total_count=total_count, limit=limit, offset=offset)

    async def find_pending_duplicate(
        self, space_id: str, invitee_email: str
    ) -> SpaceInvitationDoc | None:
        find_params = {
            "space_id": space_id,
            "invitee_email": invitee_email,
            "status": InvitationStatus.pending,
        }
        doc = await self.document.find_one(find_params)
        if doc is None:
            return None
        doc["_id"] = str(doc.get("_id"))
        return SpaceInvitationDoc.model_validate(doc)

    async def find_by_id(self, invitation_id: str) -> SpaceInvitationDoc | None:
        from bson import ObjectId

        try:
            object_id = ObjectId(invitation_id)
        except Exception:
            object_id = invitation_id
        doc = await self.document.find_one({"_id": object_id})
        if doc is None:
            return None
        doc["_id"] = str(doc.get("_id"))
        return SpaceInvitationDoc.model_validate(doc)

    async def update_status(
        self, invitation_id: str, invited_by: str, status: InvitationStatus
    ) -> SpaceInvitationDoc | None:
        """Update invitation status directly (bypasses CRUDBase.update_one which
        requires user_id on the document — invitations use invited_by instead)."""
        from datetime import datetime

        from bson import ObjectId
        from pymongo import ReturnDocument

        try:
            object_id = ObjectId(invitation_id)
        except Exception:
            object_id = invitation_id

        updated = await self.document.find_one_and_update(
            filter={"_id": object_id, "invited_by": invited_by},
            update={"$set": {"status": status.value, "updated_at": datetime.now()}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is None:
            return None
        updated["_id"] = str(updated.get("_id"))
        return SpaceInvitationDoc.model_validate(updated)

    async def delete_by_space_id(self, space_id: str):
        await self.document.delete_many({"space_id": space_id})
