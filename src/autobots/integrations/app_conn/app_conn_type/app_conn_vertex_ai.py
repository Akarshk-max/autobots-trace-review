from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.conn.claude.claude_vertex_client import VertexClaude, get_conn_vertex_claude
from src.autobots.conn.gemini.gemini import GeminiClient, get_conn_vertex_gemini
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_conn.app_conn_helper import AppConnHelper
from src.autobots.integrations.app_type_to_integration_class import AppTypeToIntegrationClass
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import IntegrationDoc
from src.autobots.integrations.integration_type.vertex_ai_auth import VertexAIAuthModel
from src.autobots.user.user_orm_model import UserORM


async def _get_vertex_auth(
    ctx: Context,
    db: AsyncIOMotorDatabase,
    user: UserORM,
) -> VertexAIAuthModel:
    app_type = AppTypes.vertex_ai
    integration_doc: IntegrationDoc = await AppConnHelper.get_app_auth(
        ctx=ctx, db=db, user=user, app_type=app_type
    )
    if not integration_doc:
        raise HTTPException(status_code=404, detail="Vertex AI app auth not found")
    app_auth_class = AppTypeToIntegrationClass.get(app_type)
    return app_auth_class.get_auth_model_type().model_validate(integration_doc.auth)


class AppConnVertexAI:
    @staticmethod
    async def get_claude(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
    ) -> VertexClaude:
        try:
            auth = await _get_vertex_auth(ctx, db, user)
            conn = get_conn_vertex_claude(
                project_id=auth.project_id,
                region=auth.region,
                service_account_json=auth.service_account_json,
            )
            logger.bind(ctx=ctx).info("Got Vertex Claude conn for user")
            return conn
        except Exception as e:
            logger.bind(ctx=ctx, error=str(e)).error("Cannot get Vertex Claude conn")
            raise

    @staticmethod
    async def get_gemini(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
    ) -> GeminiClient:
        try:
            auth = await _get_vertex_auth(ctx, db, user)
            conn = get_conn_vertex_gemini(
                project_id=auth.project_id,
                region=auth.region,
                service_account_json=auth.service_account_json,
            )
            logger.bind(ctx=ctx).info("Got Vertex Gemini conn for user")
            return conn
        except Exception as e:
            logger.bind(ctx=ctx, error=str(e)).error("Cannot get Vertex Gemini conn")
            raise
