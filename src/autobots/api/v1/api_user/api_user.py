from typing import Dict, List, Optional, Union

import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field
from starlette.requests import Request

from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.settings import SettingsProvider
from src.autobots.user.user_directory import (
    MAX_PROFILE_LOOKUPS,
    Profile,
    ProfileScope,
    resolve_profiles,
)


class ProfileRequest(BaseModel):
    """Who to resolve, and where the caller saw them.

    `scope` is optional so an existing caller keeps working, but a request
    without one resolves the caller and nobody else - the endpoint is a
    directory of what you can already see, not of everyone.
    """

    user_ids: Union[str, List[str]] = Field(default_factory=list)
    emails: List[str] = Field(default_factory=list)
    scope: Optional[ProfileScope] = None


router = APIRouter(
    prefix=SettingsProvider.sget().API_USERS, tags=[SettingsProvider.sget().API_USERS]
)


@router.post("/profiles", response_model=Dict[str, Profile])
async def get_user_profiles(
    request: Request,
    profile_request: ProfileRequest,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> Dict[str, Profile]:
    """Display names for ids and emails the caller has already been shown.

    Subjects outside the scope come back as placeholders rather than an error,
    so a page never fails to render over a name it could not resolve - and so
    the response cannot be used to probe for accounts.
    """
    ctx = request.state.context
    raw_ids = profile_request.user_ids
    user_ids = [raw_ids] if isinstance(raw_ids, str) else list(raw_ids)
    emails = list(profile_request.emails)

    if len(user_ids) + len(emails) > MAX_PROFILE_LOOKUPS:
        raise HTTPException(
            status_code=413,
            detail=f"At most {MAX_PROFILE_LOOKUPS} subjects per request",
        )
    # The scope needs its own bound. It is not part of the subject count, and an
    # unbounded one turns into an unbounded `$in` - which stops being a query and
    # starts being a way to make the server do arbitrary work.
    if profile_request.scope and len(profile_request.scope.ids) > MAX_PROFILE_LOOKUPS:
        raise HTTPException(
            status_code=413,
            detail=f"At most {MAX_PROFILE_LOOKUPS} scope ids per request",
        )

    profiles = await resolve_profiles(
        db,
        caller_id=str(user_res.user.id),
        caller_email=user_res.user.email,
        user_ids=user_ids,
        emails=emails,
        scope=profile_request.scope,
    )

    logger.bind(ctx=ctx).info(
        f"Resolved {sum(1 for p in profiles.values() if p.user_id)}/{len(profiles)} profiles"
    )
    return profiles
