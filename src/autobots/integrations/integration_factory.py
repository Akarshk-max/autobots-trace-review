from typing import Dict

from src.autobots.data_model.context import Context
from src.autobots.integrations.app_creds.app_creds_model import AppCredsDoc
from src.autobots.integrations.app_type_to_integration_class import AppTypeToIntegrationClass
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import (
    IntegrationCreate,
    IntegrationDoc,
    IntegrationUpdate,
)


class IntegrationFactory:
    @staticmethod
    async def get_auth_header(
        ctx: Context,
        app_name: AppTypes,
        app_creds_doc: AppCredsDoc | None = None,
        integration_doc: IntegrationDoc | None = None,
    ) -> Dict[str, str]:
        app = AppTypeToIntegrationClass.get(app_name)
        app_creds = (
            app.get_creds_model_type().model_validate(app_creds_doc.creds)
            if app_creds_doc
            else None
        )
        app_auth = app.get_auth_model_type().model_validate(integration_doc) if integration_doc else None

        if app_creds is None and app_auth is None:
            return {}
        if app_creds and app_auth:
            refreshed_auth = await app.refresh_auth(ctx, app_creds, app_auth)
            return {"Authorization": f"Bearer {refreshed_auth.access_token}"}
        if app_creds:
            new_auth = await app.create_auth(ctx, app_creds)
            return {"Authorization": f"Bearer {new_auth.access_token}"}

    @staticmethod
    async def create_auth(
        ctx: Context, app_name: AppTypes, app_creds_doc: AppCredsDoc
    ) -> IntegrationCreate:
        app = AppTypeToIntegrationClass.get(app_name)
        app_creds = (
            app.get_creds_model_type().model_validate(app_creds_doc.creds)
            if app_creds_doc
            else None
        )
        new_auth = await app.create_auth(ctx, app_creds)
        return IntegrationCreate(app_name=app_name, auth=new_auth)

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_name: AppTypes, app_creds_doc: AppCredsDoc, integration_doc: IntegrationDoc
    ) -> IntegrationUpdate:
        app = AppTypeToIntegrationClass.get(app_name)
        app_creds = (
            app.get_creds_model_type().model_validate(app_creds_doc.creds)
            if app_creds_doc
            else None
        )
        app_auth = app.get_auth_model_type().model_validate(integration_doc) if integration_doc else None
        new_auth = await app.refresh_auth(ctx, app_creds, app_auth)
        return IntegrationUpdate(app_name=app_name, auth=new_auth)
