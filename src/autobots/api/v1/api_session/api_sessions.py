from typing import Optional

import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots import SettingsProvider
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.session.session_crud import SessionCRUD
from src.autobots.session.session_doc_model import (
    SessionCreate,
    SessionDoc,
    SessionDocCreate,
    SessionDocFind,
    SessionDocsFound,
    SessionDocUpdate,
    SessionFind,
    SessionUpdate,
)
from src.autobots.space.space_auth import get_optional_space_member
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=SettingsProvider.sget().API_SESSIONS,
    tags=[SettingsProvider.sget().API_SESSIONS],
)


@router.post("/")
async def create_session(
    session_create: SessionCreate,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> SessionDoc:
    try:
        user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
        session_doc_create = SessionDocCreate(
            user_id=user_orm.id,
            space_id=space_member.space_id if space_member else None,
            **session_create.model_dump(),
        )
        session_crud = SessionCRUD(db)
        session_doc = await session_crud.insert_one(session_doc_create)
        if isinstance(session_doc, Exception):
            raise session_doc
        return session_doc
    except HTTPException:
        raise
    except Exception as e:
        logger.error(str(e))
        raise HTTPException(500, detail=str(e))


@router.get("/")
async def list_sessions(
    id: Optional[str] = None,
    name: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> SessionDocsFound:
    try:
        user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
        session_find = SessionFind(id=id, name=name)
        session_doc_find = SessionDocFind(
            user_id=user_orm.id,
            space_id=space_member.space_id if space_member else None,
            **session_find.model_dump(exclude_none=True),
        )
        session_crud = SessionCRUD(db)
        session_docs = await session_crud.find_page(
            doc_find=session_doc_find, limit=limit, offset=offset
        )
        return SessionDocsFound(
            docs=session_docs.docs,
            total_count=session_docs.total_count,
            limit=session_docs.limit,
            offset=session_docs.offset,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(str(e))
        raise HTTPException(500, detail=str(e))


@router.get("/search/text")
async def search_sessions(
    q: Optional[str] = None,
    limit: int = 10,
    offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> SessionDocsFound:
    try:
        user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
        session_doc_find = SessionDocFind(
            user_id=user_orm.id,
            space_id=space_member.space_id if space_member else None,
        )
        session_crud = SessionCRUD(db)
        docs_page = await session_crud.search_text_page(
            doc_find=session_doc_find,
            q=q or "",
            searchable_paths=["name"],
            limit=limit,
            offset=offset,
        )
        return SessionDocsFound(
            docs=docs_page.docs,
            total_count=docs_page.total_count,
            limit=docs_page.limit,
            offset=docs_page.offset,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(str(e))
        raise HTTPException(500, detail=str(e))


@router.get("/{id}")
async def get_session(
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> SessionDoc:
    try:
        user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
        session_doc_find = SessionDocFind(
            id=id,
            user_id=user_orm.id,
            space_id=space_member.space_id if space_member else None,
        )
        session_crud = SessionCRUD(db)
        session_doc = await session_crud.find_one(session_doc_find)
        if not session_doc:
            raise HTTPException(404, detail="Session not found")
        return session_doc
    except HTTPException:
        raise
    except Exception as e:
        logger.error(str(e))
        raise HTTPException(500, detail=str(e))


@router.put("/{id}")
async def update_session(
    id: str,
    session_update: SessionUpdate,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> SessionDoc:
    try:
        user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
        session_doc_update = SessionDocUpdate(
            id=id,
            user_id=user_orm.id,
            **session_update.model_dump(exclude_none=True),
        )
        session_crud = SessionCRUD(db)
        session_doc = await session_crud.update_one(session_doc_update)
        if isinstance(session_doc, Exception):
            raise session_doc
        return session_doc
    except HTTPException:
        raise
    except Exception as e:
        logger.error(str(e))
        raise HTTPException(500, detail=str(e))


@router.delete("/{id}")
async def delete_session(
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> SessionDoc:
    try:
        user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
        session_doc_find = SessionDocFind(
            id=id,
            user_id=user_orm.id,
            space_id=space_member.space_id if space_member else None,
        )
        session_crud = SessionCRUD(db)
        session_doc = await session_crud.find_one(session_doc_find)
        if not session_doc:
            raise HTTPException(404, detail="Session not found")
        delete_result = await session_crud.delete_many(session_doc_find)
        if isinstance(delete_result, Exception):
            raise delete_result
        return session_doc
    except HTTPException:
        raise
    except Exception as e:
        logger.error(str(e))
        raise HTTPException(500, detail=str(e))
