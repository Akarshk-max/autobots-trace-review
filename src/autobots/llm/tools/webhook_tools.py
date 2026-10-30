from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from src.autobots.core.database.mongo_base_crud import DocFindPage
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM
from src.autobots.webhook.user_webhook import UserWebhook
from src.autobots.webhook.webhook_model import WebhookCreate, WebhookDoc, WebhookFind, WebhookUpdate
from src.autobots.data_model.app_state import AppState

class WebhookTools:
    class WebhookId(BaseModel):
        webhook_id: str

    class WebhookUpdateConfig(BaseModel):
        webhook_id: str
        webhook_update: WebhookUpdate

    class WebhookCreateConfig(BaseModel):
        webhook_create: WebhookCreate
        path_prefix: str = ""

    @staticmethod
    async def create_webhook(
        config: WebhookCreateConfig,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
    ) -> WebhookDoc | HTTPException:
        """
        Description:
        - Create a new webhook for a runnable.
        - Registers a webhook in the system and returns the created webhook document,
          including a generated webhook_id and webhook_path.

        When to use:
        - Use when a runnable should be triggered by an external HTTP callback.
        - Use to provision a webhook endpoint for a specific runnable (and optional run_id),
          with an optional path prefix.

        Input Config:
        {
          "webhook_create": {
            "runnable_id": "<runnable_id>",
            "run_id": "<run_id_or_null>",
            "app_type": "<app_type_or_null>",   // optional, e.g. "slack"
            "input": {                          // optional
              "text": "<input payload or parameters>"
            }
          },
          "path_prefix": "/hooks/my-prefix"     // optional, default ""
        }

        Fields:
        - webhook_create (object, required):
            - runnable_id (string, required):
                - Identifier of the runnable that will be invoked when the webhook is called.
            - run_id (string, optional):
                - Optional run identifier used to correlate or scope webhook-triggered runs.
            - app_type (string, optional):
                - Application type for webhook challenge handling (e.g. "slack").
                - When set, enables automatic handling of webhook verification challenges.
            - input (DataBlock, optional):
                - Default input payload forwarded to the runnable when the webhook is called.
        - path_prefix (string, optional):
            - Static prefix added to the webhook path.
            - Useful for grouping or namespacing webhook URLs.
        """
        try:
            webhook_doc = await UserWebhook.create(
                ctx=ctx,
                db=db,
                user=user,
                webhook_create=config.webhook_create,
                path_prefix=config.path_prefix,
            )
            logger.bind(ctx=ctx).info(f"Webhook created with ID: {webhook_doc.webhook_id}")
            return webhook_doc
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return (
                e
                if isinstance(e, HTTPException)
                else HTTPException(500, "Error in creating webhook")
            )

    @staticmethod
    async def get_webhook(
        config: WebhookId,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
    ) -> WebhookDoc | HTTPException:
        """
        Description:
        - Retrieve a single webhook document by its webhook_id.
        - Returns the stored configuration and metadata of the webhook.

        When to use:
        - Use to inspect the details of an existing webhook.
        - Use to verify that a webhook exists and see its runnable_id, run_id, and path.

        Input Config:
        {
          "webhook_id": "<webhook_id>"
        }

        Fields:
        - webhook_id (string, required):
            - Identifier of the webhook to fetch.
        """
        try:
            logger.bind(ctx=ctx).info(
                f"Attempting to retrieve webhook with ID: {config.webhook_id}"
            )
            webhook_doc = await UserWebhook.get(ctx=ctx, db=db, user=user, id=config.webhook_id)
            logger.bind(ctx=ctx).info(
                f"Retrieved webhook: {getattr(webhook_doc, 'webhook_id', None)}"
            )
            if webhook_doc is None:
                return HTTPException(404, "Webhook not found")
            return webhook_doc
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return (
                e
                if isinstance(e, HTTPException)
                else HTTPException(500, "Error in getting webhook")
            )

    @staticmethod
    async def update_webhook(
        config: WebhookUpdateConfig,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
    ) -> WebhookDoc | HTTPException:
        """
        Description:
        - Update an existing webhook's configuration.
        - Typically used to change the runnable_id or run_id associated with the webhook.

        When to use:
        - Use when the target runnable or run_id for a webhook has changed.
        - Use to rewire an existing webhook to a different runnable or run instance.

        Input Config:
        {
          "webhook_id": "<webhook_id>",
          "webhook_update": {
            "runnable_id": "<new_runnable_id>",   // optional
            "run_id": "<new_run_id>",             // optional
            "app_type": "<app_type_or_null>",     // optional, e.g. "slack"
            "input": {                            // optional
              "text": "<new_input_payload>"
            }
          }
        }

        Fields:
        - webhook_id (string, required):
            - Identifier of the webhook to update.
        - webhook_update (object, required):
            - runnable_id (string, optional):
                - New runnable_id to associate with the webhook.
            - run_id (string, optional):
                - New run_id to associate with the webhook.
            - app_type (string, optional):
                - Application type for webhook challenge handling (e.g. "slack").
                - When set, enables automatic handling of webhook verification challenges.
            - input (DataBlock, optional):
                - New input payload to associate with the webhook.
        """
        try:
            logger.bind(ctx=ctx).info(f"Attempting to update webhook with ID: {config.webhook_id}")
            webhook_doc = await UserWebhook.update(
                ctx=ctx,
                db=db,
                user=user,
                id=config.webhook_id,
                webhook_update=config.webhook_update,
            )
            return webhook_doc
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return (
                e
                if isinstance(e, HTTPException)
                else HTTPException(500, "Error in updating webhook")
            )

    @staticmethod
    async def find_webhook(
        config: WebhookFind,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
    ) -> DocFindPage | HTTPException:
        """
        Description:
        - Find and list webhooks matching specified filter criteria.
        - Returns a paginated result with matching webhook documents.

        When to use:
        - Use to search for all webhooks belonging to a user that match certain fields.
        - Use to list webhooks by runnable_id, webhook_id, or internal id.

        Input Config:
        {
          "id": "<internal_id>",          // optional
          "webhook_id": "<webhook_id>",   // optional
          "runnable_id": "<runnable_id>", // optional
          "app_type": "<app_type>"        // optional, e.g. "slack"
        }

        Fields:
        - id (string, optional):
            - Internal database id (if supported by the backend).
        - webhook_id (string, optional):
            - Specific webhook_id to filter by.
        - runnable_id (string, optional):
            - Filter by runnable_id to list all webhooks bound to a runnable.
        - app_type (string, optional):
            - Filter by application type (e.g. "slack") to list webhooks for a specific app.
        """
        try:
            webhook_doc = await UserWebhook.list(
                ctx=ctx,
                db=db,
                user=user,
                webhook_find=config,
            )
            return webhook_doc
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return (
                e
                if isinstance(e, HTTPException)
                else HTTPException(500, "Error in finding webhooks")
            )

    @staticmethod
    async def delete_webhook(
        config: WebhookId,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
    ) -> int | HTTPException:
        """
        Description:
        - Delete an existing webhook by its webhook_id.
        - Returns the number of deleted documents.

        When to use:
        - Use to remove webhook endpoints that are no longer needed.
        - Use to clean up stale or unused webhooks for a user or runnable.

        Input Config:
        {
          "webhook_id": "<webhook_id>"
        }

        Fields:
        - webhook_id (string, required):
            - Identifier of the webhook to delete.
        """
        try:
            # logger.bind(ctx=ctx).info(f"Attempting to delete webhook with ID: {config.webhook_id}")
            logger.bind(ctx=ctx).info(f"Attempting to delete webhook with ID: {config.webhook_id}")
            delete_result = await UserWebhook.delete(
                ctx=ctx,
                db=db,
                user=user,
                id=config.webhook_id,
            )
            logger.bind(ctx=ctx).info(f"Deleted webhook count: {delete_result.deleted_count}")
            return delete_result.deleted_count
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return (
                e
                if isinstance(e, HTTPException)
                else HTTPException(500, "Error in deleting webhook")
            )
