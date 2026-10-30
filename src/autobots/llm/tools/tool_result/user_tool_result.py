from bson import ObjectId
from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument

from src.autobots.event_result.event_result_crud import EventResultCRUD
from src.autobots.event_result.event_result_model import (
    EventResultDocCreate,
    EventResultDocFind,
    EventResultDocUpdate,
    EventType,
)
from src.autobots.llm.tools.tool_result.tool_result_doc_model import (
    TERMINAL_TOOL_RUN_STATUSES,
    ToolResultCreate,
    ToolResultDoc,
    ToolResultDocsFound,
    ToolResultFind,
    ToolResultLiteDoc,
    ToolResultUpdate,
)
from src.autobots.user.user_orm_model import UserORM


class UserToolResult:

    def __init__(self, user: UserORM, db: AsyncIOMotorDatabase):
        self.user = user
        self.user_id = str(user.id)
        self.space_member = user.space_member
        self.event_result_crud = EventResultCRUD(db)

    async def create_tool_result(self, tool_result: ToolResultCreate) -> ToolResultDoc:
        space_id = self.space_member.space_id if self.space_member else None
        doc_create = EventResultDocCreate(
            user_id=self.user_id, space_id=space_id, **tool_result.model_dump()
        )
        doc = await self.event_result_crud.insert_one(doc_create)
        if isinstance(doc, Exception):
            raise doc
        return ToolResultDoc.model_validate(doc.model_dump(by_alias=True))

    async def get_tool_result(self, tool_result_id: str) -> ToolResultDoc | None:
        doc_find = EventResultDocFind(
            id=tool_result_id, user_id=self.user_id, type=EventType.tool_run
        )
        doc = await self.event_result_crud.find_one(doc_find)
        if doc is None:
            return None
        return ToolResultDoc.model_validate(doc.model_dump(by_alias=True))

    async def update_tool_result(
        self, tool_result_id: str, tool_result_update: ToolResultUpdate
    ) -> ToolResultDoc:
        doc_update = EventResultDocUpdate(
            id=tool_result_id,
            user_id=self.user_id,
            **tool_result_update.model_dump(exclude_none=True),
        )
        doc = await self.event_result_crud.update_one(doc_update.to_mongo_update())
        if isinstance(doc, Exception):
            raise HTTPException(404, "Tool run not found")
        return ToolResultDoc.model_validate(doc.model_dump(by_alias=True))

    async def list_tool_results(
        self,
        tool_result_find: ToolResultFind,
        limit: int = 20,
        offset: int = 0,
    ) -> ToolResultDocsFound:
        space_id = self.space_member.space_id if self.space_member else None
        tool_result_find.type = EventType.tool_run
        doc_find = EventResultDocFind(
            user_id=self.user_id,
            space_id=space_id,
            **tool_result_find.model_dump(exclude_none=True),
        )
        # offset is a page index, matching how every other event result listing pages.
        page = await self.event_result_crud.find_page(
            doc_find=doc_find, limit=limit, offset=offset
        )

        tool_result_docs = []
        for event_result_doc in page.docs:
            try:
                tool_result_docs.append(
                    ToolResultLiteDoc.model_validate(event_result_doc.model_dump())
                )
            except Exception as e:
                logger.exception(f"Error in list_tool_results: {str(e)}")

        return ToolResultDocsFound(
            docs=tool_result_docs,
            total_count=page.total_count,
            limit=page.limit,
            offset=page.offset,
        )

    async def delete_tool_result(self, tool_result_id: str) -> ToolResultDoc | None:
        tool_result_doc = await self.get_tool_result(tool_result_id)
        if tool_result_doc is None:
            return None

        await self.event_result_crud.document.delete_one(
            {"_id": self._coerce_mongo_id(tool_result_id), "user_id": self.user_id}
        )
        return tool_result_doc

    @staticmethod
    def _coerce_mongo_id(id_value: str) -> str | ObjectId:
        try:
            return ObjectId(str(id_value))
        except Exception:
            return str(id_value)

    async def settle_tool_result(
        self, tool_result_id: str, tool_result_update: ToolResultUpdate
    ) -> ToolResultDoc | None:
        doc_update = EventResultDocUpdate(
            id=tool_result_id,
            user_id=self.user_id,
            **tool_result_update.model_dump(exclude_none=True),
        ).to_mongo_update()
        set_fields = doc_update.model_dump(exclude_none=True)
        set_fields.pop("id", None)

        updated_doc = await self.event_result_crud.document.find_one_and_update(
            filter={
                "_id": self._coerce_mongo_id(tool_result_id),
                "user_id": self.user_id,
                "status": {"$nin": [status.value for status in TERMINAL_TOOL_RUN_STATUSES]},
            },
            update={"$set": set_fields},
            return_document=ReturnDocument.AFTER,
        )
        if updated_doc is None:
            return None
        updated_doc["_id"] = str(updated_doc.get("_id"))
        return ToolResultDoc.model_validate(updated_doc)
