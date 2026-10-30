from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.integrations.crypto.helpers import decrypt_if_needed, encrypt_if_needed
from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.context import Context
from src.autobots.mcp.mcp_crud import MCPCRUD
from src.autobots.mcp.mcp_doc_model import (
    MCPCreate,
    MCPDoc,
    MCPDocCreate,
    MCPDocFind,
    MCPDocsFound,
    MCPDocUpdate,
    MCPFind,
    MCPUpdate,
)
from src.autobots.user.user_orm_model import UserORM


class UserMCP:
    def __init__(self, user: UserORM, db: AsyncIOMotorDatabase):
        self.user = user
        self.user_id = str(user.id)
        self.db = db
        self.space_member = self.user.space_member
        self.mcp_crud = MCPCRUD(db)

    @staticmethod
    def _build_auth_envelope(request_headers: dict | None, oauth_token: str | None) -> dict | None:
        """Build an encrypted auth envelope from request_headers and oauth_token.

        Returns None if both are None/empty, otherwise returns the encrypted envelope.
        """
        payload = {}
        if request_headers:
            payload["request_headers"] = request_headers
        if oauth_token:
            payload["oauth_token"] = oauth_token
        if not payload:
            return None
        return encrypt_if_needed(payload)

    @staticmethod
    def _unwrap_auth_envelope(auth: dict | None) -> tuple[dict | None, str | None]:
        """Decrypt the auth envelope and return (request_headers, oauth_token).

        Returns (None, None) if auth is None or empty.
        """
        if not auth:
            return None, None
        decrypted = decrypt_if_needed(auth)
        return decrypted.get("request_headers"), decrypted.get("oauth_token")

    async def create_mcp(
        self,
        ctx: Context,
        mcp_create: MCPCreate,
        decrypt: bool = False,
    ) -> MCPDoc | None:
        try:
            if self.space_member and not self.space_member.space_id:
                raise HTTPException(400, "Invalid space member: missing space_id")
            space_id = self.space_member.space_id if self.space_member else None
            mcp_doc_find = MCPDocFind(user_id=self.user_id, server_name=mcp_create.server_name)
            existing_mcp_doc = await self.mcp_crud.find_one(mcp_doc_find)
            if existing_mcp_doc:
                raise HTTPException(
                    status_code=400, detail="MCP with the same server name already exists"
                )

            # Build encrypted auth envelope from plaintext fields
            auth_envelope = self._build_auth_envelope(
                mcp_create.request_headers, mcp_create.oauth_token
            )

            # Clear plaintext fields — they're now inside the encrypted envelope
            mcp_create.request_headers = None
            mcp_create.oauth_token = None

            mcp_doc_create: MCPDocCreate = MCPDocCreate(
                **mcp_create.model_dump(exclude_none=True),
                user_id=self.user_id,
                space_id=space_id,
                auth=auth_envelope,
            )
            mcp_doc = await self.mcp_crud.insert_one(mcp_doc_create)

            if decrypt and mcp_doc:
                mcp_doc.request_headers, mcp_doc.oauth_token = self._unwrap_auth_envelope(
                    mcp_doc.auth
                )

            return mcp_doc
        except HTTPException as e:
            logger.bind(ctx=ctx, code=AppCode.MCP).exception(str(e))
            raise
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.MCP).exception(str(e))
            raise HTTPException(status_code=500, detail=str(e))

    async def list_mcps(
        self,
        ctx: Context,
        mcp_find: MCPFind,
        limit: int = 100,
        offset: int = 0,
        decrypt: bool = False,
    ) -> MCPDocsFound | None:
        try:
            if self.space_member and not self.space_member.space_id:
                raise HTTPException(400, "Invalid space member: missing space_id")
            space_id = self.space_member.space_id if self.space_member else None
            action_doc_find = MCPDocFind(
                user_id=self.user_id, space_id=space_id, **mcp_find.model_dump()
            )
            paged_docs = await self.mcp_crud.find_page(
                doc_find=action_doc_find, or_find_queries=None, limit=limit, offset=offset
            )
            mcp_docs = []
            for action_doc in paged_docs.docs:
                mcp_doc = MCPDoc.model_validate(action_doc)
                if decrypt:
                    mcp_doc.request_headers, mcp_doc.oauth_token = self._unwrap_auth_envelope(
                        mcp_doc.auth
                    )
                mcp_docs.append(mcp_doc)
            return MCPDocsFound(
                docs=mcp_docs,
                total_count=paged_docs.total_count,
                limit=paged_docs.limit,
                offset=paged_docs.offset,
            )
        except HTTPException as e:
            logger.bind(ctx=ctx, code=AppCode.MCP).exception(str(e))
            raise
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.MCP).exception(str(e))
            raise HTTPException(status_code=500, detail=str(e))

    async def get_mcp(
        self, ctx: Context, mcp_id: str, decrypt: bool = False
    ) -> MCPDoc | None:
        try:
            if self.space_member and not self.space_member.space_id:
                raise HTTPException(400, "Invalid space member: missing space_id")
            space_id = self.space_member.space_id if self.space_member else None
            mcp_doc_find = MCPDocFind(id=mcp_id, user_id=self.user_id, space_id=space_id)
            mcp_doc = await self.mcp_crud.find_one(mcp_doc_find)
            if mcp_doc and decrypt:
                mcp_doc.request_headers, mcp_doc.oauth_token = self._unwrap_auth_envelope(
                    mcp_doc.auth
                )
            return mcp_doc
        except HTTPException as e:
            logger.bind(ctx=ctx, code=AppCode.MCP).exception(str(e))
            raise
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.MCP).exception(str(e))
            raise HTTPException(status_code=500, detail=str(e))

    async def get_mcp_by_server_name(
        self, ctx: Context, mcp_server_name: str, decrypt: bool = False
    ) -> MCPDoc | None:
        try:
            if self.space_member and not self.space_member.space_id:
                raise HTTPException(400, "Invalid space member: missing space_id")
            space_id = self.space_member.space_id if self.space_member else None
            mcp_doc_find = MCPDocFind(
                server_name=mcp_server_name, user_id=self.user_id, space_id=space_id
            )
            mcp_doc = await self.mcp_crud.find_one(mcp_doc_find)
            if mcp_doc and decrypt:
                mcp_doc.request_headers, mcp_doc.oauth_token = self._unwrap_auth_envelope(
                    mcp_doc.auth
                )
            return mcp_doc
        except HTTPException as e:
            logger.bind(ctx=ctx, code=AppCode.MCP).exception(str(e))
            raise
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.MCP).exception(str(e))
            raise HTTPException(status_code=500, detail=str(e))

    async def update_mcp(
        self,
        ctx: Context,
        mcp_id: str,
        mcp_update: MCPUpdate,
        decrypt: bool = False,
    ) -> MCPDoc | None:
        try:
            if self.space_member and not self.space_member.space_id:
                raise HTTPException(400, "Invalid space member: missing space_id")
            mcp_doc_find = MCPDocFind(id=mcp_id, user_id=self.user_id)
            existing_mcp_doc = await self.mcp_crud.find_one(mcp_doc_find)
            if not existing_mcp_doc:
                raise HTTPException(status_code=404, detail="MCP not found")

            # Build encrypted auth envelope from plaintext fields in the update
            auth_envelope = self._build_auth_envelope(
                mcp_update.request_headers, mcp_update.oauth_token
            )

            # Clear plaintext fields — they're now inside the encrypted envelope
            mcp_update.request_headers = None
            mcp_update.oauth_token = None

            mcp_doc_update = MCPDocUpdate(
                id=mcp_id,
                user_id=self.user_id,
                **mcp_update.model_dump(exclude_none=True),
                auth=auth_envelope,
            )
            mcp_doc = await self.mcp_crud.update_one(mcp_doc_update)

            if decrypt and mcp_doc:
                mcp_doc.request_headers, mcp_doc.oauth_token = self._unwrap_auth_envelope(
                    mcp_doc.auth
                )

            return mcp_doc
        except HTTPException as e:
            logger.bind(ctx=ctx, code=AppCode.MCP).exception(str(e))
            raise
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.MCP).exception(str(e))
            raise HTTPException(status_code=500, detail=str(e))

    async def delete_mcp(self, ctx: Context, mcp_id: str) -> int:
        try:
            if self.space_member and not self.space_member.space_id:
                raise HTTPException(400, "Invalid space member: missing space_id")
            space_id = self.space_member.space_id if self.space_member else None
            mcp_doc_find = MCPDocFind(id=mcp_id, user_id=self.user_id, space_id=space_id)
            delete_result = await self.mcp_crud.delete_many(mcp_doc_find)
            return delete_result.deleted_count
        except HTTPException as e:
            logger.bind(ctx=ctx, code=AppCode.MCP).exception(str(e))
            raise
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.MCP).exception(str(e))
            raise HTTPException(status_code=500, detail=str(e))
