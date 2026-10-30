# from typing import Generator
#
# from sqlalchemy.orm import Session
#
# from autobots.database import base
#

from fastapi import Request
from supabase_auth import UserResponse

from src.autobots.auth.security import (
    get_offline_user_from_access_token,
    get_online_admin_user_from_access_token,
)

# def get_db() -> Generator[Session, None, None]:
#     return base.get_db()


async def get_user_from_cookie(request: Request) -> UserResponse | None:
    token = request.cookies.get("Authorization")
    if not token:
        return None
    user = await get_offline_user_from_access_token(
        api_key_query=None, api_key_header=None, api_key_cookie=token
    )
    if not user:
        return None
    return user


async def get_admin_user_from_cookie(request: Request) -> UserResponse | None:
    token = request.cookies.get("Authorization")
    if not token:
        return None
    try:
        user = await get_online_admin_user_from_access_token(
            api_key_query=None, api_key_header=None, api_key_cookie=token
        )
        if not user:
            return None
        return user
    except Exception:
        return None


# Uncomment for tests only
# async def get_current_user(
#     # We now depend on the simplest token extractor, which does not do validation.
#     token: str = Depends(get_access_token),
# ) -> UserORM:
#     """
#     This is the primary dependency for getting the current authenticated user.
#     In production/dev, it validates the JWT via Supabase.
#     In local/test environments, it bypasses the external validation and returns a
#     mock user, allowing inter-service tests to succeed.
#     """
#     settings = SettingsProvider.sget()
#
#     # This is the key change: check for 'local' or 'test'
#     if settings.ENV in ["local", "test"]:
#         logger.warning(
#             f"ENV is '{settings.ENV}'. Bypassing Supabase validation and returning a mock user object."
#         )
#         try:
#             # We can still decode the token locally to get the user_id for tenancy.
#             # This doesn't make an external call.
#             from jose import jwt
#
#             payload = jwt.decode(
#                 token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
#             )
#             user_id = payload.get("sub")
#             if not user_id:
#                 raise ValueError("Token is missing 'sub' claim.")
#
#             return UserORM(
#                 id=user_id,
#                 email=f"test-user@{user_id[:8]}.autobots.test",
#                 app_metadata={},
#                 user_metadata={},
#                 aud="authenticated",
#                 created_at=datetime.utcnow(),
#                 updated_at=datetime.utcnow(),
#             )
#         except Exception as e:
#             logger.error(
#                 f"Failed to decode token in test mode, returning fallback user. Error: {e}"
#             )
#             # Fallback for a completely malformed token
#             return UserORM(
#                 id="a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", email="fallback-test-user@example.com"
#             )
#
#     # In all other environments (e.g., 'dev', 'prod'), run the real authentication.
#     try:
#         user_res = await online_decode_access_token(token)
#         return UserORM(**user_res.user.model_dump())
#     except Exception as e:
#         raise HTTPException(status_code=403, detail=f"Could not validate credentials: {e}")
