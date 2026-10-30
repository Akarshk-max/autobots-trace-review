import ssl
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.results import DeleteResult

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_creds.app_creds_model import AppCredsDoc, AppCredsRead
from src.autobots.integrations.app_creds.user_app_creds import UserAppCreds
from src.autobots.integrations.app_type_to_integration_class import AppTypeToIntegrationClass
from src.autobots.integrations.app_types import GOOGLE_APP_TYPES, AppTypes
from src.autobots.integrations.crypto.helpers import decrypt_if_needed, encrypt_if_needed
from src.autobots.integrations.integration_crud import IntegrationCRUD
from src.autobots.integrations.integration_lookup import extract_lookup
from src.autobots.integrations.integration_model import (
    IntegrationCreate,
    IntegrationDoc,
    IntegrationDocCreate,
    IntegrationDocRead,
    IntegrationDocUpdate,
    IntegrationLiteDocPage,
    IntegrationRead,
    IntegrationUpdate,
)
from src.autobots.user.user_orm_model import UserORM


class UserIntegration:
    @staticmethod
    async def create(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        integration_create: IntegrationCreate,
        decrypt: bool = False,
    ) -> IntegrationDoc | None:
        """upsert"""
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        crud = IntegrationCRUD(db=db)
        AppTypeToIntegrationClass.get(integration_create.app_name).get_auth_model_type().model_validate(
            integration_create.auth
        )
        lookup = extract_lookup(integration_create.app_name, integration_create.auth)
        integration_create.auth = encrypt_if_needed(integration_create.auth)
        # This upsert deletes before it inserts, so credentials the caller did not resend
        # would vanish with the old document. Re-authenticating must not disconnect the
        # thing it just authenticated.
        if not integration_create.creds:
            existing = await UserIntegration.get_by_name(
                ctx=ctx,
                db=db,
                user=user,
                name=integration_create.name,
                app_name=integration_create.app_name,
            )
            if existing and existing.creds:
                integration_create.creds = existing.creds
        integration_create.creds = encrypt_if_needed(integration_create.creds or {})
        delete_query = IntegrationDocRead(
            user_id=str(user.id),
            space_id=space_id,
            app_name=integration_create.app_name,
            name=integration_create.name,
        )
        deleted = await crud.delete_many(doc_find=delete_query)
        logger.bind(ctx=ctx, code=AppCode.APP_AUTH).info(
            f"Deleted {deleted.deleted_count} app auth for name: {integration_create.name} and app_name: {integration_create.app_name} before create"
        )
        create_doc = IntegrationDocCreate(
            user_id=str(user.id),
            space_id=space_id,
            lookup=lookup,
            **integration_create.model_dump(exclude_none=True),
        )
        doc = await crud.insert_one(create_doc)
        logger.bind(ctx=ctx, code=AppCode.APP_AUTH).info(f"Created app auth for app_name: {integration_create.app_name}")
        match doc:
            case IntegrationDoc():
                if decrypt:
                    doc.auth = decrypt_if_needed(doc.auth or {})
                    doc.creds = decrypt_if_needed(doc.creds or {})
                return IntegrationDoc.model_validate(doc)
            case _:
                logger.bind(ctx=ctx, code=AppCode.APP_AUTH, error=str(doc)).error("Cannot create app auth")
        return None

    @staticmethod
    async def list(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        integration_list: IntegrationRead,
        limit: int = 100,
        offset: int = 0,
    ) -> IntegrationLiteDocPage:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        crud = IntegrationCRUD(db=db)
        find_doc = IntegrationDocRead(
            user_id=str(user.id),
            space_id=space_id,
            **integration_list.model_dump(exclude_none=True),
        )
        page = await crud.find_page(doc_find=find_doc, limit=limit, offset=offset)
        app_auth_page = IntegrationLiteDocPage.model_validate(page.model_dump(exclude_none=True))
        return app_auth_page

    @staticmethod
    async def get(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        integration_id: str,
        decrypt: bool = False,
    ) -> IntegrationDoc | None:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        crud = IntegrationCRUD(db=db)
        doc_find = IntegrationDocRead(user_id=str(user.id), space_id=space_id, id=integration_id)
        doc = await crud.find_one(doc_find=doc_find)
        if doc and decrypt:
            doc.auth = decrypt_if_needed(doc.auth)
        return IntegrationDoc.model_validate(doc) if doc else None

    @staticmethod
    async def get_by_app_name(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        app_name: AppTypes,
        default: bool = True,
        decrypt: bool = False,
    ) -> IntegrationDoc | None:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        crud = IntegrationCRUD(db=db)
        doc_find = IntegrationDocRead(user_id=str(user.id), space_id=space_id, app_name=app_name, default=default)
        doc = await crud.find_one(doc_find=doc_find)
        if doc and decrypt:
            doc.auth = decrypt_if_needed(doc.auth)
        return IntegrationDoc.model_validate(doc) if doc else None

    @staticmethod
    async def get_by_name(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        name: str,
        app_name: AppTypes | None = None,
        decrypt: bool = False,
    ) -> IntegrationDoc | None:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        crud = IntegrationCRUD(db=db)
        doc_find = IntegrationDocRead(user_id=str(user.id), space_id=space_id, name=name)
        if app_name is not None:
            doc_find.app_name = app_name
        doc = await crud.find_one(doc_find=doc_find)
        if doc and decrypt:
            doc.auth = decrypt_if_needed(doc.auth)
        return IntegrationDoc.model_validate(doc) if doc else None

    @staticmethod
    async def get_by_slack_user(
        ctx: Context, db: AsyncIOMotorDatabase, slack_user_id: str, slack_team_id: str | None = None, decrypt: bool = False
    ) -> IntegrationDoc | None:
        try:
            crud = IntegrationCRUD(db=db)
            find_dict = {"app_name": AppTypes.slack, "lookup.authed_user_id": slack_user_id}
            if slack_team_id:
                find_dict.update({"lookup.team_id": slack_team_id})
            user_app_auth = await crud.document.find_one(find_dict)
            if not user_app_auth:
                legacy_dict = {"app_name": AppTypes.slack, "slack_authed_user_id": slack_user_id}
                if slack_team_id:
                    legacy_dict.update({"slack_team_id": slack_team_id})
                user_app_auth = await crud.document.find_one(legacy_dict)
                if not user_app_auth:
                    logger.bind(ctx=ctx,code=AppCode.APP_AUTH, find_dict=find_dict, legacy_dict=legacy_dict).error("Cannot find app auth")
                    raise HTTPException(status_code=404, detail="Cannot find app auth")
            user_app_auth["_id"] = str(user_app_auth.get("_id"))
            if decrypt:
                user_app_auth["auth"] = decrypt_if_needed(user_app_auth.get("auth") or {})
            integration_doc = IntegrationDoc.model_validate(user_app_auth)
            return integration_doc
        except Exception as e:
            logger.bind(ctx=ctx , code=AppCode.APP_AUTH).exception(f"Error in get_by_slack_user: {str(e)}")

    @staticmethod
    async def get_user_by_slack_user(
        ctx: Context, db: AsyncIOMotorDatabase, slack_user_id: str, slack_team_id: str | None = None
    ) -> UserORM | Exception:
        try:
            crud = IntegrationCRUD(db=db)
            find_dict = {
                "app_name": {"$in": [AppTypes.slack, AppTypes.slack_user]},
                "lookup.authed_user_id": slack_user_id,
            }
            if slack_team_id:
                find_dict.update({"lookup.team_id": slack_team_id})
            user_app_auth = await crud.document.find_one(find_dict)
            if not user_app_auth:
                legacy_dict = {
                    "app_name": {"$in": [AppTypes.slack, AppTypes.slack_user]},
                    "slack_authed_user_id": slack_user_id,
                }
                if slack_team_id:
                    legacy_dict.update({"slack_team_id": slack_team_id})
                user_app_auth = await crud.document.find_one(legacy_dict)
                if not user_app_auth:
                    return HTTPException(status_code=403, detail="Slack user not found")
            user_id = user_app_auth.get("user_id")
            user = UserORM(id=user_id)
            return user
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.APP_AUTH).exception(f"Error in get_by_slack_user: {str(e)}")
            return e

    @staticmethod
    async def update(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        id: str,
        integration_update: IntegrationUpdate,
        decrypt: bool = False,
    ) -> IntegrationDoc | None:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        crud = IntegrationCRUD(db=db)
        AppTypeToIntegrationClass.get(integration_update.app_name).get_auth_model_type().model_validate(
            integration_update.auth
        )
        lookup_dict = extract_lookup(integration_update.app_name, integration_update.auth)
        lookup = lookup_dict if lookup_dict else None
        integration_update.auth = encrypt_if_needed(integration_update.auth)
        if integration_update.creds:
            integration_update.creds = encrypt_if_needed(integration_update.creds)
        doc_update = IntegrationDocUpdate(
            id=id, user_id=str(user.id), lookup=lookup, **integration_update.model_dump(exclude_none=True)
        )
        doc = await crud.update_one(doc_update=doc_update)
        match doc:
            case IntegrationDoc():
                logger.bind(ctx=ctx, code=AppCode.APP_AUTH).info(
                    f"Updated app auth for app_name: {integration_update.app_name}"
                )
                if decrypt:
                    doc.auth = decrypt_if_needed(doc.auth or {})
                    doc.creds = decrypt_if_needed(doc.creds or {})
                return IntegrationDoc.model_validate(doc)
            case _:
                logger.bind(ctx=ctx, code=AppCode.APP_AUTH, error=str(doc)).error("Cannot update app auth")
        return None

    @staticmethod
    async def find_docs_for_delete(
        *, ctx: Context, crud: IntegrationCRUD, find_doc: IntegrationDocRead, decrypt: bool = False
    ) -> List[Dict[str, Any]]:
        try:
            find_params = await crud._build_filter(find_doc)
            cursor = crud.document.find(find_params, {"app_name": 1, "name": 1, "auth": 1})
            docs: List[Dict[str, Any]] = []
            async for doc in cursor:
                if decrypt:
                    doc["auth"] = decrypt_if_needed(doc.get("auth") or {})
                docs.append(doc)
            return docs
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.APP_AUTH).exception(
                f"Error finding app auth docs for disconnect: {str(e)}"
            )
            raise HTTPException(
                status_code=500, detail="Error finding app auth docs for disconnect"
            )


    @staticmethod
    async def revoke_external_access_for_delete(
        *, ctx: Context, integration_docs: List[Dict[str, Any]]
    ) -> None:
        """Ask each provider to drop its grant for the connections about to be deleted.

        Best effort: every outcome is logged and none of them stops the delete.
        """
        try:
            for doc in integration_docs:
                app_name_raw = doc.get("app_name")
                try:
                    app_type = app_name_raw if isinstance(app_name_raw, AppTypes) else AppTypes(app_name_raw)
                except Exception:
                    # Unknown/unsupported app type in DB; skip revoke
                    continue
                app_auth_class = AppTypeToIntegrationClass.get(app_type)
                if not app_auth_class:
                    continue
                if not doc.get("auth"):
                    # Credentials stored, consent never completed: nothing was granted.
                    logger.bind(ctx=ctx, code=AppCode.APP_AUTH, app_name=str(app_type)).info(
                        "No auth on this connection; nothing to revoke"
                    )
                    continue
                # All IntegrationType classes inherit a default revoke_external_auth no-op, and some override it.
                try:
                    auth_model = app_auth_class.get_auth_model_type().model_validate(doc.get("auth") or {})
                except Exception as e:
                    logger.bind(ctx=ctx, code=AppCode.APP_AUTH, app_name=str(app_type), error=str(e)).warning(
                        "Unable to parse auth model for revoke; skipping revoke for this connection"
                    )
                    continue
                revoke_rs = await app_auth_class.revoke_external_auth(
                    ctx=ctx, app_creds=None, app_auth=auth_model
                )
                if isinstance(revoke_rs, Exception):
                    logger.bind(ctx=ctx, code=AppCode.APP_AUTH, app_name=str(app_type), error=str(revoke_rs)).warning(
                        "External revoke failed; the connection is still being deleted"
                    )
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.APP_AUTH).exception(
                f"Error during revoke-on-delete; the connection is still being deleted: {str(e)}"
            )
    
    @staticmethod
    async def delete(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        app_auth_delete: IntegrationRead,
        decrypt: bool = False,
    ) -> int | None:
        """Delete (disconnect) AppAuth records matching the given filter.

        This is the backend implementation of "Disconnect" in the UI.

        Important behavior: if the integration supports provider-side revocation (e.g., Google OAuth),
        we revoke the external grant *before* deleting our local AppAuth record. If revocation fails,
        we abort deletion so we don't claim the integration is disconnected while it is still approved
        in the user's provider account.
        """

        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        crud = IntegrationCRUD(db=db)
        find_doc = IntegrationDocRead(
            user_id=str(user.id), space_id=space_id, **app_auth_delete.model_dump(exclude_none=True)
        )

        # User comment: find the doc(s) first (so we know app_name), then revoke provider-side access before delete.
        docs_to_delete = await UserIntegration.find_docs_for_delete(ctx=ctx, crud=crud, find_doc=find_doc, decrypt=decrypt)

        # Skip Google provider-side revocation if other Google-family connections still exist,
        # since revoking a Google token invalidates ALL tokens under the same Google Cloud project.
        delete_ids = {str(d.get("_id")) for d in docs_to_delete}
        google_docs = [d for d in docs_to_delete if d.get("app_name") in GOOGLE_APP_TYPES]
        if google_docs:
            other_google_filter = {
                "user_id": str(user.id),
                "app_name": {"$in": [t.value for t in GOOGLE_APP_TYPES]},
                "_id": {"$nin": [ObjectId(oid) for oid in delete_ids]},
                "space_id": space_id,
            }
            other_google = await crud.document.find_one(other_google_filter, {"_id": 1})
            if other_google:
                logger.bind(ctx=ctx, code=AppCode.APP_AUTH).info(
                    "Skipping Google revocation: other Google-family connections still exist"
                )
                docs_to_revoke = [d for d in docs_to_delete if d.get("app_name") not in GOOGLE_APP_TYPES]
            else:
                docs_to_revoke = docs_to_delete
        else:
            docs_to_revoke = docs_to_delete

        await UserIntegration.revoke_external_access_for_delete(ctx=ctx, integration_docs=docs_to_revoke)
        deleted = await crud.delete_many(doc_find=find_doc)
        match deleted:
            case DeleteResult():
                app_name_msg = (
                    f" for app_name: {app_auth_delete.app_name}" if app_auth_delete.app_name else ""
                )
                logger.bind(ctx=ctx, code=AppCode.APP_AUTH).info(
                    f"Deleted {deleted.deleted_count} app auths{app_name_msg}"
                )
                # Cascade: also delete related app creds
                for doc in docs_to_delete:
                    cred_name = doc.get("name")
                    if not cred_name:
                        continue
                    try:
                        await UserAppCreds.delete_app_creds(
                            ctx=ctx, db=db, user=user,
                            app_creds_delete=AppCredsRead(
                                name=cred_name,
                                app_name=doc.get("app_name") or app_auth_delete.app_name,
                            ),
                        )
                    except Exception as e:
                        logger.bind(ctx=ctx, code=AppCode.APP_AUTH).warning(
                            f"Cascade delete of app creds for '{cred_name}' failed: {e}"
                        )
                return deleted.deleted_count
            case Exception():
                logger.bind(ctx=ctx, code=AppCode.APP_AUTH, error=str(deleted)).error("Cannot delete app auths")
        return None

    @staticmethod  # TODO: change name to get_auth_header
    async def auth_header(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        name: str,
        config: Optional[APIRequest] = None,
        app_name: AppTypes | None = None,
        decrypt: bool = False,
    ) -> Dict[str, str]:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        header = {}
        try:
            assert name is not None
            assert user is not None
            app_creds_doc: AppCredsDoc | None = await UserAppCreds.get_app_creds_by_name(
                ctx=ctx, db=db, user=user, name=name, app_name=app_name, decrypt=decrypt
            )
            integration_doc = await UserIntegration.get_by_name(
                                ctx=ctx, db=db, user=user, name=name, app_name=app_name, decrypt=decrypt
                            )
            if app_name is None:
                app_name = app_creds_doc.app_name if app_creds_doc else integration_doc.app_name
            app_auth_class = AppTypeToIntegrationClass.get(app_name)
            app_creds = (
                app_auth_class.get_creds_model_type().model_validate(app_creds_doc.creds)
                if app_creds_doc and app_creds_doc.creds
                else None
            )
            # `if integration_doc.auth`, not `if integration_doc`: a folded connection that
            # holds credentials and no auth yet is a document with `auth: {}`, and every
            # app's auth model rejects an empty dict. Validating it would throw and this
            # function would answer with no header at all.
            app_auth = (
                app_auth_class.get_auth_model_type().model_validate(integration_doc.auth)
                if integration_doc and integration_doc.auth
                else None
            )
            if app_creds_doc is None and integration_doc is None:
                # Nothing matched, so this request goes out unauthenticated and the
                # provider will answer 401 as if the credentials were broken. The usual
                # cause is a caller naming a connection and leaving app_name at its
                # default of `anon`, which matches no connection: name and app_name are
                # one identity, because the same name can exist under several apps. Say
                # which pair missed, or the next reader spends the afternoon on scopes.
                logger.bind(
                    ctx=ctx, code=AppCode.APP_AUTH, name=name, app_name=str(app_name),
                ).warning(
                    f"No connection matches name={name!r} with app_name={str(app_name)!r}; "
                    "request will be sent with no Authorization header"
                )
                return header

            if integration_doc and app_auth:
                is_refresh = await app_auth_class.is_refresh_required(ctx=ctx, app_auth=app_auth)
                logger.bind(ctx=ctx, name=name, is_refresh_required=is_refresh).info(f"Checking if refresh is required => {is_refresh}")
                if is_refresh:
                    refreshed_auth = await app_auth_class.refresh_auth(
                        ctx=ctx, app_creds=app_creds, app_auth=app_auth
                    )
                    if isinstance(refreshed_auth, Exception):
                        logger.bind(ctx=ctx, name=name).error(f"Failed to refresh auth for {name}: {str(refreshed_auth)}")
                        header = await app_auth_class.get_auth_header(
                            ctx=ctx, app_auth=app_auth, config=config
                        )
                    else:
                        integration_update = IntegrationUpdate(
                            name=name, app_name=integration_doc.app_name, auth=refreshed_auth.model_dump(exclude_none=True)
                        )
                        new_app_auth_doc = await UserIntegration.update(
                            ctx=ctx,
                            db=db,
                            user=user,
                            id=integration_doc.id,
                            integration_update=integration_update,
                            decrypt=decrypt
                        )
                        new_app_auth = app_auth_class.get_auth_model_type().model_validate(
                            new_app_auth_doc.auth
                        )
                        header = await app_auth_class.get_auth_header(
                            ctx=ctx, app_auth=new_app_auth, config=config
                        )
                else:
                    header = await app_auth_class.get_auth_header(
                        ctx=ctx, app_auth=app_auth, config=config
                    )

            # redundency - for api key apps like openai, if the app_creds table has data then even if app_auth dosent have data. 
            # The information is enopugh to create auth header. 
            # But it is not possible for oauth app. For oauth apps we need app_auth
            elif app_creds_doc and not app_auth:
                logger.bind(ctx=ctx, code=AppCode.APP_AUTH).warning("Using redundency - for api key apps to create auth header from creds")
                new_auth = await app_auth_class.create_auth(ctx=ctx, app_creds=app_creds)
                integration_create = IntegrationCreate(
                    name=name, app_name=app_creds_doc.app_name, auth=new_auth.model_dump(exclude_none=True)
                )
                new_app_auth_doc = await UserIntegration.create(
                    ctx=ctx,
                    db=db,
                    user=user,
                    integration_create=integration_create,
                    decrypt=decrypt
                )
                new_app_auth = app_auth_class.get_auth_model_type().model_validate(
                    new_app_auth_doc.auth
                )
                header = await app_auth_class.get_auth_header(
                    ctx=ctx, app_auth=new_app_auth, config=config
                )
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.APP_AUTH, name=name, user=user).exception(
                f"Error in auth_header: {str(e)}"
            )
        return header

    @staticmethod
    async def sync_with_creds(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        name: str,
        app_name: AppTypes,
        decrypt: bool = False,
    ) -> IntegrationDoc | None:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        app_auth_class = AppTypeToIntegrationClass.get(app_name)
        try:
            # Decrypted because this is read to be used, not to be returned — see the note
            # in refresh. What the caller gets back still follows `decrypt`.
            app_creds_doc = await UserAppCreds.get_app_creds_by_name(
                ctx=ctx, db=db, user=user, name=name, app_name=app_name, decrypt=True
            )
            if not app_creds_doc or not app_creds_doc.creds:
                # OAuth integrations (e.g. google_drive, google_gmail) store tokens directly
                # via the OAuth callback — there are no AppCreds to sync from.
                # Return the existing AppAuth if present, so sync is a safe no-op.
                existing = await UserIntegration.get_by_name(
                    ctx=ctx, db=db, user=user, name=name, app_name=app_name, decrypt=decrypt
                )
                if existing:
                    logger.bind(ctx=ctx, code=AppCode.APP_AUTH).info(
                        f"App Auth sync skipped for OAuth app {app_name.value}: returning existing auth"
                    )
                    return existing
                logger.bind(ctx=ctx , code=AppCode.APP_AUTH).warning(
                    f"App Auth sync failed for {app_name.value}: no creds and no existing auth"
                )
                return None
            app_creds = app_auth_class.get_creds_model_type().model_validate(app_creds_doc.creds)
            integration_doc = await UserIntegration.get_by_name(
                ctx=ctx, db=db, user=user, name=name, app_name=app_name, decrypt=True
            )
            if integration_doc:
                app_auth = app_auth_class.get_auth_model_type().model_validate(integration_doc.auth)
                new_auth = await app_auth_class.refresh_auth(
                    ctx=ctx, app_creds=app_creds, app_auth=app_auth
                )
            else:
                new_auth = await app_auth_class.create_auth(ctx=ctx, app_creds=app_creds)
            if integration_doc:
                integration_update = IntegrationUpdate(
                    app_name=app_name, name=name, auth=new_auth.model_dump(exclude_none=True)
                )
                new_app_auth_doc = await UserIntegration.update(
                    ctx=ctx,
                    db=db,
                    user=user,
                    id=integration_doc.id,
                    integration_update=integration_update,
                    decrypt=decrypt
                )
                logger.bind(ctx=ctx, code=AppCode.APP_AUTH).info(f"App Auth sync by update for {app_name.value}")
                return new_app_auth_doc
            else:
                integration_create = IntegrationCreate(
                    app_name=app_name, name=name, auth=new_auth.model_dump(exclude_none=True)
                )
                new_app_auth_doc = await UserIntegration.create(
                    ctx=ctx,
                    db=db,
                    user=user,
                    integration_create=integration_create,
                    decrypt=decrypt
                )
                logger.bind(ctx=ctx, code=AppCode.APP_AUTH).info(f"App Auth sync by create for {app_name.value}")
                return new_app_auth_doc
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.APP_AUTH).exception(f"Error in sync_with_creds: {str(e)}")
    
    @staticmethod
    async def refresh(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        name: str,
        app_name: AppTypes | None = None,
        decrypt: bool = False,
    ) -> IntegrationDoc | None:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        integration_doc = None
        try:
            # `decrypt` says what the caller wants handed back. These two reads are not
            # that: their contents go straight into model_validate, and a stored blob is
            # ciphertext, so reading them any other way validates the envelope and fails.
            app_creds_doc = await UserAppCreds.get_app_creds_by_name(
                ctx=ctx, db=db, user=user, name=name, app_name=app_name, decrypt=True
            )
            # OAuth-only integrations (e.g. google_drive, google_gmail) may have no
            # AppCreds doc — they store tokens directly via the OAuth callback. Use the
            # provided app_name, or fall back to the creds doc's app_name if available.
            effective_app_name = app_name or (app_creds_doc.app_name if app_creds_doc else None)
            if not effective_app_name:
                raise HTTPException(400, f"Could not determine app type for name='{name}'")
            app_auth_class = AppTypeToIntegrationClass.get(effective_app_name)
            app_creds = (
                app_auth_class.get_creds_model_type().model_validate(app_creds_doc.creds)
                if app_creds_doc and app_creds_doc.creds
                else None
            )
            integration_doc = await UserIntegration.get_by_name(
                ctx=ctx, db=db, user=user, name=name, app_name=app_name, decrypt=True
            )
            if not integration_doc:
                raise HTTPException(404, f"No app auth found for name='{name}'")
            app_auth = app_auth_class.get_auth_model_type().model_validate(integration_doc.auth)
            new_auth = await app_auth_class.refresh_auth(
                ctx=ctx, app_creds=app_creds, app_auth=app_auth
            )
            # Providers may return Exception or None instead of raising (per the
            # IntegrationTypeABC.refresh_auth contract). Propagate those as proper errors
            # rather than letting .model_dump() fail with a misleading AttributeError.
            if isinstance(new_auth, HTTPException):
                raise new_auth
            if new_auth is None:
                logger.bind(
                    ctx=ctx, code=AppCode.APP_AUTH, app_name=effective_app_name, name=name
                ).error("Provider returned no refreshed auth")
                raise HTTPException(
                    status_code=502,
                    detail=f"Provider returned no refreshed auth for name='{name}'",
                ) from None
            if isinstance(new_auth, Exception):
                logger.bind(
                    ctx=ctx,
                    code=AppCode.APP_AUTH,
                    app_name=effective_app_name,
                    name=name,
                    error=str(new_auth),
                ).error(f"Provider failed to refresh app auth: {new_auth}")
                raise HTTPException(
                    status_code=502,
                    detail=f"Provider failed to refresh app auth for name='{name}'",
                ) from new_auth
            integration_update = IntegrationUpdate(
                name=name, app_name=integration_doc.app_name, auth=new_auth.model_dump(exclude_none=True)
            )
            new_app_auth_doc = await UserIntegration.update(
                ctx=ctx,
                db=db,
                user=user,
                id=integration_doc.id,
                integration_update=integration_update,
                decrypt=decrypt
            )
            logger.bind(ctx=ctx, code=AppCode.APP_AUTH, app_name=new_app_auth_doc.app_name).info(
                f"App Auth refreshed for {new_app_auth_doc.app_name}"
            )
            return new_app_auth_doc
        except Exception as e:
            logger.bind(
                ctx=ctx, code=AppCode.APP_AUTH, app_name=app_name, name=name
            ).exception(f"Error in Auth refresh: {str(e)}")
            raise

    # @staticmethod
    # async def refresh(ctx: Context, db: AsyncIOMotorDatabase, user: UserORM, app_name: AppTypes) -> IntegrationDoc | None:
    #     app_auth_class = AppTypeToIntegrationClass.get(app_name)
    #     try:
    #         app_creds_doc = await UserAppCreds.get_app_creds_by_app_name(ctx=ctx, db=db, user=user, app_name=app_name)
    #         app_creds = app_auth_class.get_creds_model_type().model_validate(app_creds_doc.creds)
    #         integration_doc = await UserIntegration.get_by_app_name(ctx=ctx, db=db, user=user, app_name=app_name)
    #         app_auth = app_auth_class.get_auth_model_type().model_validate(integration_doc.auth)
    #         new_auth = await app_auth_class.refresh_auth(ctx=ctx, app_creds=app_creds, app_auth=app_auth)
    #         integration_update = IntegrationUpdate(app_name=app_name, auth=new_auth.model_dump(exclude_none=True))
    #         new_app_auth_doc = await UserIntegration.update(
    #             ctx=ctx, db=db, user=user, id=integration_doc.id, integration_update=integration_update
    #         )
    #         logger.bind(ctx=ctx, app_name=app_name.value).info(f"App Auth refreshed for {app_name.value}")
    #         return new_app_auth_doc
    #     except Exception as e:
    #         logger.bind(ctx=ctx).exception(str(e))

    @staticmethod
    async def get_updated_url(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        name: str,
        url: str,
        app_name: AppTypes | None = None,
        decrypt: bool = False,
    ) -> str:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        try:
            app_creds_doc = await UserAppCreds.get_app_creds_by_name(
                ctx=ctx, db=db, user=user, name=name, app_name=app_name, decrypt=decrypt
            )
            integration_doc = await UserIntegration.get_by_name(
                                ctx=ctx, db=db, user=user, name=name, app_name=app_name, decrypt=decrypt
                            )
            if app_name is None:
                app_name = app_creds_doc.app_name if app_creds_doc else integration_doc.app_name
            app_auth_class = AppTypeToIntegrationClass.get(app_name)
            app_creds = (
                app_auth_class.get_creds_model_type().model_validate(app_creds_doc.creds)
                if app_creds_doc and app_creds_doc.creds
                else None
            )
            app_auth = (
                app_auth_class.get_auth_model_type().model_validate(integration_doc.auth)
                if integration_doc and integration_doc.auth
                else None
            )
            if app_creds_doc is None and integration_doc is None:
                return url
            else:
                url = await app_auth_class.get_updated_url(
                    ctx=ctx, app_creds=app_creds, app_auth=app_auth, url=url
                )
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.APP_AUTH, name=name).exception(
                f"Error in get_updated_url: {str(e)}"
            )
        return url

    @staticmethod
    async def get_updated_body(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        name: str,
        body: Optional[Dict[str, Any]] = None,
        app_name: AppTypes | None = None,
        decrypt: bool = False,
    ) -> Optional[Dict[str, Any]]:
        try:
            integration_doc = await UserIntegration.get_by_name(
                ctx=ctx, db=db, user=user, name=name, app_name=app_name, decrypt=decrypt
            )
            if integration_doc is None:
                return body
            app_auth_class = AppTypeToIntegrationClass.get(integration_doc.app_name)
            app_auth = (
                app_auth_class.get_auth_model_type().model_validate(integration_doc.auth)
                if integration_doc and integration_doc.auth
                else None
            )
            body = await app_auth_class.get_updated_body(ctx=ctx, app_auth=app_auth, body=body)
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.APP_AUTH, name=name).exception(
                f"Error in get_updated_body: {str(e)}"
            )
        return body

    @staticmethod
    async def get_ssl_context(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        name: str,
        app_name: AppTypes | None = None,
        decrypt: bool = False,
    ) -> Optional[ssl.SSLContext]:
        """
        Get SSL context for mTLS if required by the app.
        Returns None if the app doesn't require mTLS.
        """
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        try:
            app_creds_doc = await UserAppCreds.get_app_creds_by_name(
                ctx=ctx, db=db, user=user, name=name, app_name=app_name, decrypt=decrypt
            )
            if app_creds_doc is None:
                # OAuth-only apps have no AppCreds — no mTLS certs to retrieve
                return None
            app_auth_class = AppTypeToIntegrationClass.get(app_creds_doc.app_name)
            app_creds = (
                app_auth_class.get_creds_model_type().model_validate(app_creds_doc.creds)
                if app_creds_doc and app_creds_doc.creds
                else None
            )
            ssl_context = await app_auth_class.get_ssl_context(ctx=ctx, app_creds=app_creds)
            return ssl_context
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.APP_AUTH, name=name).exception(
                f"Error in get_ssl_context: {str(e)}"
            )
        return None
