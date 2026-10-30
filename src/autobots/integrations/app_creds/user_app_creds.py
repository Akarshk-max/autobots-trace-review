from typing import Any, Dict, List

from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import ValidationError
from pymongo.results import DeleteResult

from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_creds.app_creds_crud import AppCredsCRUD
from src.autobots.integrations.app_creds.app_creds_model import (
    AppCredsCreate,
    AppCredsDoc,
    AppCredsDocCreate,
    AppCredsDocRead,
    AppCredsDocUpdate,
    AppCredsLiteDocPage,
    AppCredsRead,
    AppCredsUpdate,
)
from src.autobots.integrations.app_type_to_integration_class import AppTypeToIntegrationClass
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.crypto.helpers import decrypt_if_needed, encrypt_if_needed
from src.autobots.integrations.integration_crud import IntegrationCRUD
from src.autobots.integrations.integration_model import IntegrationDoc, IntegrationDocRead
from src.autobots.user.user_orm_model import UserORM


class UserAppCreds:
    # Does User level isolation
    # params common in all functions: user: UserORM, db: AsyncIOMotorDatabase

    @staticmethod
    def _validate_creds_for_app(app_name: AppTypes, creds: dict, phase: str):
        try:
            AppTypeToIntegrationClass.get(app_name).get_creds_model_type().model_validate(creds)
        except ValidationError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": f"Invalid credentials payload for {app_name}",
                    "phase": phase,
                    "errors": exc.errors(),
                },
            )
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": f"Credential validation failed for {app_name}",
                    "phase": phase,
                    "errors": [str(exc)],
                },
            )

    @staticmethod
    def _strip_spaces_from_creds(data: dict) -> dict:
        """Recursively strip whitespace from all string values in a dict"""
        if not isinstance(data, dict):
            return data

        result = {}
        for key, value in data.items():
            if isinstance(value, str):
                result[key] = value.strip()
            elif isinstance(value, dict):
                result[key] = UserAppCreds._strip_spaces_from_creds(value)
            elif isinstance(value, list):
                result[key] = [
                    UserAppCreds._strip_spaces_from_creds(item)
                    if isinstance(item, dict)
                    else item.strip()
                    if isinstance(item, str)
                    else item
                    for item in value
                ]
            else:
                result[key] = value  # Keep None values, numbers, booleans, etc. as-is
        return result


    @staticmethod
    def _folded(doc: IntegrationDoc | None, decrypt: bool) -> AppCredsDoc | None:
        """The creds half of a connection, read off the record that also holds its auth.

        Credentials used to live in their own collection. They now ride on the AppAuth
        document, so the two halves of one connection cannot disagree. This hands back
        the shape callers already expect, so the reads below can prefer the folded copy
        without every call site knowing anything changed.
        """
        if not doc:
            return None
        creds = decrypt_if_needed(doc.creds) if decrypt else doc.creds
        if not creds:
            return None
        return AppCredsDoc(
            id=str(doc.id),
            user_id=doc.user_id,
            space_id=doc.space_id,
            app_name=doc.app_name,
            name=doc.name,
            default=doc.default if doc.default is not None else True,
            creds=creds,
        )

    @staticmethod
    async def app_creds_types(ctx: Context) -> List[str]:
        return AppTypes.list()

    @staticmethod
    async def app_creds_data_type(ctx: Context, app_name: AppTypes) -> Dict[str, Any]:
        return AppTypeToIntegrationClass.get(app_name).get_creds_model_type().model_json_schema()


    @staticmethod
    async def _mirror_onto_integration(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user_id: str,
        space_id: str | None,
        app_name: Any,
        name: str | None,
        creds: Dict[str, Any],
    ) -> None:
        """Write credentials onto the connection's own record as well as the old collection.

        Reads take the folded copy first, so a write that lands only in `AppCreds` leaves
        the engine authenticating with credentials the user has already replaced. While
        both stores exist, a write to one is a write to both.

        No matching record means the credentials are the only half of this connection that
        exists yet, and a connection with one half is still a connection — so it gets a
        record, with `auth` empty rather than absent. Credentials are stored exactly as
        they arrive here: already encrypted, and not re-encrypted.
        """
        try:
            await IntegrationCRUD(db=db).set_creds(
                identity={
                    "user_id": user_id,
                    "space_id": space_id,
                    "app_name": str(getattr(app_name, "value", app_name)),
                    "name": name,
                },
                creds=creds,
            )
        except Exception as e:
            # The credentials are stored either way; the folded copy is a cache of them
            # until the old collection goes. Losing the mirror is worth a loud log, not a
            # failed request that tells the user their credentials did not save.
            logger.bind(ctx=ctx, code=AppCode.APP_CREDS, name=name).exception(
                f"Could not mirror app creds onto the integration record: {str(e)}"
            )


    @staticmethod
    async def store_creds(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        app_name: AppTypes,
        name: str,
        creds: Dict[str, Any],
    ) -> None:
        """Save credentials sent with a connect call, before the user is sent to a provider.

        Several providers build their consent URL out of the customer's own client id and
        secret, and their callback needs them again to exchange the code. Under the old
        two-step flow those arrived by a separate `POST /app-creds` first. A single connect
        call has to store them itself, or the URL is built from nothing and the callback
        has nothing to exchange with.

        Validated and stripped the same way the credentials endpoint does, so one route
        cannot accept what the other rejects.
        """
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None

        UserAppCreds._validate_creds_for_app(app_name, creds, phase="connect")
        cleaned = UserAppCreds._strip_spaces_from_creds(creds)
        await UserAppCreds._mirror_onto_integration(
            ctx=ctx,
            db=db,
            user_id=str(user.id),
            space_id=space_id,
            app_name=app_name,
            name=name,
            creds=encrypt_if_needed(cleaned),
        )
        logger.bind(ctx=ctx, code=AppCode.APP_CREDS, app_name=str(app_name), name=name).info(
            "Stored credentials sent with connect"
        )

    @staticmethod
    async def create_app_creds(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        app_creds_create: AppCredsCreate,
        decrypt: bool = False,
    ) -> AppCredsDoc | None:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        crud = AppCredsCRUD(db=db)
        # Validate creds type matches app type with explicit 400s for schema problems
        UserAppCreds._validate_creds_for_app(app_creds_create.app_name, app_creds_create.creds, phase="create")

        # Strip whitespace from all string values in creds
        app_creds_create.creds = UserAppCreds._strip_spaces_from_creds(app_creds_create.creds)
        app_creds_create.creds = encrypt_if_needed(app_creds_create.creds)
        logger.bind(ctx=ctx, code=AppCode.APP_CREDS, app_name=app_creds_create.app_name).info(
            "Stripped whitespace from app creds during creation"
        )

        # Check for duplicate name within the same app type
        existing_query = AppCredsDocRead(
            user_id=str(user.id),
            space_id=space_id,
            name=app_creds_create.name,
            app_name=app_creds_create.app_name,
        )
        existing = await crud.find_one(doc_find=existing_query)
        if existing:
            # Check if app_auth exists for this combo — if it does, this is a legitimate
            # connection and we should not overwrite. If not, the creds are orphaned from
            # an aborted Auth flow and can be safely overwritten.
            app_auth_crud = IntegrationCRUD(db=db)
            integration_doc = await app_auth_crud.find_one(doc_find=IntegrationDocRead(
                user_id=str(user.id),
                space_id=space_id,
                name=app_creds_create.name,
                app_name=app_creds_create.app_name,
            ))
            if integration_doc:
                raise HTTPException(
                    status_code=409,
                    detail=f"App creds with name '{app_creds_create.name}' already exists for app '{app_creds_create.app_name}'",
                )
            await crud.delete_many(
                doc_find=AppCredsDocRead(
                    user_id=str(user.id), space_id=space_id, id=existing.id
                )
            )
            logger.bind(ctx=ctx, code=AppCode.APP_CREDS).info(
                f"Overwriting orphaned app creds '{existing.id}' for '{app_creds_create.name}' ({app_creds_create.app_name})"
            )

        create_doc = AppCredsDocCreate(
            user_id=str(user.id),
            space_id=space_id,
            **app_creds_create.model_dump(exclude_none=True),
        )

        doc = await crud.insert_one(create_doc)
        match doc:
            case AppCredsDoc():
                logger.bind(ctx=ctx, code=AppCode.APP_CREDS).info(
                    f"Created app creds for app_name: {app_creds_create.app_name}"
                )
                await UserAppCreds._mirror_onto_integration(
                    ctx=ctx, db=db, user_id=str(user.id), space_id=space_id,
                    app_name=doc.app_name, name=doc.name, creds=doc.creds,
                )
                if decrypt:
                    doc.creds = decrypt_if_needed(doc.creds)
                return AppCredsDoc.model_validate(doc)
            case _:
                logger.bind(ctx=ctx, code=AppCode.APP_CREDS, error=str(doc)).error("Cannot create app creds")
        return None

    @staticmethod
    async def list_app_creds(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        app_creds_list: AppCredsRead,
        limit: int = 100,
        offset: int = 0,
    ) -> AppCredsLiteDocPage:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        crud = AppCredsCRUD(db=db)
        find_doc = AppCredsDocRead(
            user_id=str(user.id),
            space_id=space_id,
            **app_creds_list.model_dump(exclude_none=True),
        )
        page = await crud.find_page(doc_find=find_doc, limit=limit, offset=offset)
        app_creds_page = AppCredsLiteDocPage.model_validate(page.model_dump(exclude_none=True))
        return app_creds_page

    @staticmethod
    async def get_app_creds(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        app_creds_id: str,
        decrypt: bool = False,
    ) -> AppCredsDoc | None:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        crud = AppCredsCRUD(db=db)
        doc_find = AppCredsDocRead(user_id=str(user.id), space_id=space_id, id=app_creds_id)
        doc = await crud.find_one(doc_find=doc_find)
        if doc and decrypt:
            doc.creds = decrypt_if_needed(doc.creds)
        return AppCredsDoc.model_validate(doc) if doc else None

    @staticmethod
    async def get_app_creds_by_app_name(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        app_name: AppTypes,
        default: bool = True,
        decrypt: bool = False,
    ) -> AppCredsDoc | None:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        auth_doc = await IntegrationCRUD(db=db).find_one(
            doc_find=IntegrationDocRead(
                user_id=str(user.id), space_id=space_id, app_name=app_name, default=default
            )
        )
        folded = UserAppCreds._folded(auth_doc, decrypt)
        if folded:
            return folded

        crud = AppCredsCRUD(db=db)
        doc_find = AppCredsDocRead(user_id=str(user.id), space_id=space_id, app_name=app_name, default=default)
        doc = await crud.find_one(doc_find=doc_find)
        if doc and decrypt:
            doc.creds = decrypt_if_needed(doc.creds)
        return AppCredsDoc.model_validate(doc) if doc else None

    @staticmethod
    async def get_app_creds_by_name(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        name: str,
        app_name: AppTypes | None = None,
        decrypt: bool = False,
    ) -> AppCredsDoc | None:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        auth_doc = await IntegrationCRUD(db=db).find_one(
            doc_find=IntegrationDocRead(
                user_id=str(user.id), space_id=space_id, name=name, app_name=app_name
            )
        )
        folded = UserAppCreds._folded(auth_doc, decrypt)
        if folded:
            return folded

        # Not folded yet: a connection made before the two were joined, or one whose
        # creds were written by the old /app-creds route.
        crud = AppCredsCRUD(db=db)
        doc_find = AppCredsDocRead(
            user_id=str(user.id), space_id=space_id, name=name, app_name=app_name
        )
        doc = await crud.find_one(doc_find=doc_find)
        if doc and decrypt:
            doc.creds = decrypt_if_needed(doc.creds)
        return AppCredsDoc.model_validate(doc) if doc else None

    @staticmethod
    async def update_app_creds(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        id: str,
        app_creds_update: AppCredsUpdate,
        decypt: bool = False,
    ) -> AppCredsDoc | None:
        space_member = user.space_member
        space_id = space_member.space_id if space_member else None
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        crud = AppCredsCRUD(db=db)
        # Validate creds type matches app type with explicit 400s for schema problems
        UserAppCreds._validate_creds_for_app(app_creds_update.app_name, app_creds_update.creds, phase="update")

        # Strip whitespace from all string values in creds
        app_creds_update.creds = UserAppCreds._strip_spaces_from_creds(app_creds_update.creds)
        app_creds_update.creds = encrypt_if_needed(app_creds_update.creds)
        logger.bind(ctx=ctx, code=AppCode.APP_CREDS, app_name=app_creds_update.app_name).info(
            "Stripped whitespace from app creds during update"
        )

        # Check for duplicate name within the same app type (excluding the record being updated)
        existing_query = AppCredsDocRead(
            user_id=str(user.id),
            space_id=space_id,
            name=app_creds_update.name,
            app_name=app_creds_update.app_name,
        )
        existing = await crud.find_one(doc_find=existing_query)
        if existing and existing.id != id:
            raise HTTPException(
                status_code=409,
                detail=f"App creds with name '{app_creds_update.name}' already exists for app '{app_creds_update.app_name}'",
            )

        doc_update = AppCredsDocUpdate(
            id=id, user_id=str(user.id), **app_creds_update.model_dump(exclude_none=True)
        )
        doc = await crud.update_one(doc_update=doc_update)
        match doc:
            case AppCredsDoc():
                logger.bind(ctx=ctx, code=AppCode.APP_CREDS).info(
                    f"Updated app creds for app_name: {app_creds_update.app_name}"
                )
                await UserAppCreds._mirror_onto_integration(
                    ctx=ctx, db=db, user_id=str(user.id), space_id=space_id,
                    app_name=doc.app_name, name=doc.name, creds=doc.creds,
                )
                if decypt:
                    doc.creds = decrypt_if_needed(doc.creds)
                return AppCredsDoc.model_validate(doc)
            case _:
                logger.bind(ctx=ctx, code=AppCode.APP_CREDS, error=str(doc)).error("Cannot update app creds")
        return None


    @staticmethod
    async def _clear_mirrored_creds(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        identities: List[Dict[str, Any]],
    ) -> None:
        """Take the credentials off the connection's record when they are deleted.

        Reads take the folded copy first, so credentials deleted from the old collection
        alone would go on authenticating requests. A record left with neither half is not
        a connection any more, so it goes too.
        """
        crud = IntegrationCRUD(db=db)
        for identity in identities:
            try:
                await crud.clear_creds(identity=identity)
            except Exception as e:
                logger.bind(ctx=ctx, code=AppCode.APP_CREDS).exception(
                    f"Could not clear mirrored creds from the integration record: {str(e)}"
                )

    @staticmethod
    async def delete_app_creds(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        app_creds_delete: AppCredsRead,
    ) -> int | None:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        crud = AppCredsCRUD(db=db)
        find_doc = AppCredsDocRead(
            user_id=str(user.id),
            space_id=space_id,
            **app_creds_delete.model_dump(exclude_none=True),
        )
        # A name-only delete is ambiguous when the same name exists for several app types.
        # Refuse instead of deleting creds belonging to other apps.
        if not app_creds_delete.id and app_creds_delete.name and not app_creds_delete.app_name:
            matches = await crud.find_page(doc_find=find_doc, limit=2)
            if matches.total_count > 1:
                raise HTTPException(
                    status_code=409,
                    detail=f"Multiple app creds named '{app_creds_delete.name}' exist. Specify 'app_name' or 'id' to delete.",
                )
        # Read the identities before the rows go, so the folded copies can be found after.
        doomed = await crud.find_page(doc_find=find_doc, limit=1000)
        identities = [
            {
                "user_id": d.user_id,
                "space_id": d.space_id,
                "app_name": str(getattr(d.app_name, "value", d.app_name)),
                "name": d.name,
            }
            for d in doomed.docs
        ]

        deleted = await crud.delete_many(doc_find=find_doc)
        await UserAppCreds._clear_mirrored_creds(ctx=ctx, db=db, identities=identities)
        match deleted:
            case DeleteResult():
                app_name_msg = (
                    f" for app_name: {app_creds_delete.app_name}"
                    if app_creds_delete.app_name
                    else ""
                )
                logger.bind(ctx=ctx, code=AppCode.APP_CREDS).info(
                    f"Deleted {deleted.deleted_count} app creds{app_name_msg}"
                )
                return deleted.deleted_count
            case Exception():
                logger.bind(ctx=ctx, code=AppCode.APP_CREDS, error=str(deleted)).error("Cannot delete app creds")
        return None
