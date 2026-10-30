from typing import Any, Dict

from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.data_model.context import Context
from src.autobots.data_model.created_at import CreatedAt
from src.autobots.data_model.updated_at import UpdatedAt
from src.autobots.pod.pod_crud import PodCRUD
from src.autobots.pod.pod_doc_model import PodDoc, PodUpdate
from src.autobots.user.user_orm_model import UserORM


class UserPod:
    def __init__(self, user: UserORM, db: AsyncIOMotorDatabase):
        self.user = user
        self.user_id = str(user.id)
        self.db = db
        self.space_member = self.user.space_member
        self.pod_crud = PodCRUD(db)

    def _space_id(self) -> str | None:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        return self.space_member.space_id if self.space_member else None

    async def get(self, ctx: Context) -> PodDoc:
        """Creates the pod if the user has never had one."""
        return await self._upsert({})

    async def update(self, ctx: Context, pod_update: PodUpdate) -> PodDoc:
        return await self._upsert(pod_update.model_dump(exclude_none=True))

    async def _upsert(self, set_fields: Dict[str, Any]) -> PodDoc:
        space_id = self._space_id()
        return await self.pod_crud.upsert(
            match={"user_id": self.user_id, "space_id": space_id},
            set_fields={**set_fields, **UpdatedAt().model_dump()},
            set_on_insert={
                "user_id": self.user_id,
                "space_id": space_id,
                **CreatedAt().model_dump(),
            },
        )
