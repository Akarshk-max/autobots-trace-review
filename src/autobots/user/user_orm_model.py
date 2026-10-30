from datetime import datetime
from typing import Any, Dict, List

import supabase_auth
from pydantic import BaseModel, ConfigDict

from src.autobots.core.database.mongo_base_crud import DocFindPage
from src.autobots.data_model.created_at import CreatedAt
from src.autobots.data_model.id import ID
from src.autobots.data_model.MongoId import MongoID
from src.autobots.data_model.updated_at import UpdatedAt
from src.autobots.data_model.user_id import UserId
from src.autobots.space.space_doc_model import SpaceMemberDoc


class UserORM(supabase_auth.User):
    app_metadata: Dict[str, Any] | None = None
    user_metadata: Dict[str, Any] | None = None
    aud: str | None = None
    created_at: datetime | None = None
    access_token: str | None = None
    space_member: SpaceMemberDoc | None = None


class UserCreate(UserORM):
    pass


class UserDocCreate(UserId, UserCreate, CreatedAt, UpdatedAt):
    pass


# class UserRead(UserORM):
class UserRead(BaseModel):
    id: str


class UserDocRead(UserId, UserRead):
    # user_id: str | None = None
    pass


class UserUpdate(UserCreate):
    pass


class UserDocUpdate(ID, UserId, UserUpdate):
    pass


class UserDeleteDoc(UserRead):
    pass


class UserDoc(MongoID, UserDocCreate):
    __collection__ = "Users"

    model_config = ConfigDict(populate_by_name=True)


class UserDocsPage(DocFindPage):
    docs: List[UserDoc]
