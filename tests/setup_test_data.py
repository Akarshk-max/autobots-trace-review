from time import sleep

from loguru import logger

from src.autobots import SettingsProvider
from src.autobots.integrations.integration_model import (
    ApiKeyAuthModel,
    IntegrationCreate,
    IntegrationDoc,
)
from src.autobots.integrations.integration_type.gemini_auth import GeminiAuthModel
from src.autobots.integrations.integration_type.openai_auth import OpenaiAuthModel
from src.autobots.integrations.user_integration import UserIntegration
from src.autobots.integrations.app_types import AppTypes
from src.autobots.user.user import User
from src.autobots.user.user_orm_model import UserCreate, UserDoc, UserORM

# class TestWide(BaseModel):
#     model_config = ConfigDict(arbitrary_types_allowed=True)
#
#     ctx: Context
#     user: UserORM
#
#     def to_string(self):
#         text = ""
#         for k, v in self.__dict__.items():
#             text += f"{k}: {v}\n"
#         return text[:-1]
#
#
# def setup_test_wide():
#     ctx = Context()
#     user = UserORM(id="test_user_id", email="test_user@runkai.ai")
#     # db = next(get_mongo_db())
#     test_wide = TestWide(
#         ctx=ctx,
#         user=user,
#         # db=db,
#     )
#     return test_wide
#
#
# TEST_WIDE: TestWide = setup_test_wide()


async def setup_test_db(ctx, db, user):
    settings = SettingsProvider.sget()
    logger.bind(ctx=ctx).info("🔥 Set up test data starting...")

    # User
    user_doc: UserDoc | None = None
    try:
        user_doc = await User.get(ctx=ctx, user=user, id=user.id, db=db)
    except Exception as e:
        logger.bind(ctx=ctx).error(str(e))
    if not user_doc:
        user_create = UserCreate(id=user.id, email=user.email)
        user_doc = await User.create(ctx=ctx, user=user, user_create=user_create, db=db)
        logger.bind(ctx=ctx).info(f"Created user doc: {user_doc.user_id}")
    assert user_doc
    user = UserORM.model_validate(user_doc)

    # AppAuth Openai
    # These lookups are existence checks only — the doc's `auth` is never read here, so
    # they must not decrypt. Decrypting would require the real ENCRYPTION_* keys and
    # fail the whole session against a DB holding records encrypted with other keys.
    app_type = AppTypes.openai
    integration_doc = await UserIntegration.get_by_app_name(ctx=ctx, db=db, user=user, app_name=app_type)
    if not integration_doc:
        auth = OpenaiAuthModel(api_key=settings.OPENAI_API_KEY, org_id=settings.OPENAI_ORG_ID)
        integration_create = IntegrationCreate(app_name=app_type, name=app_type.value, auth=auth.model_dump())
        integration_doc: IntegrationDoc = await UserIntegration.create(
            ctx=ctx, db=db, user=user, integration_create=integration_create
        )
        # assert integration_doc
        # logger.bind(ctx=ctx).info(f"Created app auth doc for Openai: {integration_doc}")

    # AppAuth gemini
    app_type = AppTypes.gemini
    integration_doc = await UserIntegration.get_by_app_name(ctx=ctx, db=db, user=user, app_name=app_type)
    if not integration_doc:
        auth = GeminiAuthModel(api_key=settings.GEMINI_API_KEY)
        integration_create = IntegrationCreate(app_name=app_type, name=app_type.value, auth=auth.model_dump())
        integration_doc: IntegrationDoc = await UserIntegration.create(
            ctx=ctx, db=db, user=user, integration_create=integration_create
        )
        # assert integration_doc
        # logger.bind(ctx=ctx).info(f"Created app auth doc for Gemini: {integration_doc}")

    # AppAuth claude
    app_type = AppTypes.claude
    integration_doc = await UserIntegration.get_by_app_name(ctx=ctx, db=db, user=user, app_name=app_type)
    if not integration_doc:
        auth = ApiKeyAuthModel(api_key=settings.CLAUDE_API_KEY)
        integration_create = IntegrationCreate(app_name=app_type, name=app_type.value, auth=auth.model_dump())
        integration_doc: IntegrationDoc = await UserIntegration.create(
            ctx=ctx, db=db, user=user, integration_create=integration_create
        )
        # assert integration_doc
        # logger.bind(ctx=ctx).info(f"Created app auth doc for Claude: {integration_doc}")

    # mcp_create = MCPCreate(
    #     server_name="deepwiki",
    #     server_type="SSE",
    #     server_url="https://mcp.deepwiki.com/mcp"
    # )

    # await UserMCP(user=user, db=db).create_mcp(ctx=ctx, mcp_create=mcp_create)

    # mcp_create_2 = MCPCreate(
    #     server_name="dmcp",
    #     server_type="SSE",
    #     server_url="https://dmcp-server.deno.dev/sse",
    #     description="A Dungeons and Dragons MCP server to assist with dice rolling."
    # )

    # await UserMCP(user=user, db=db).create_mcp(ctx=ctx, mcp_create=mcp_create_2)

    logger.bind(ctx=ctx).info("🔥 Set up test data complete")
    sleep(15)
