from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from xai_sdk import AsyncClient

from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM


class GrokConnection:
    """
    Holds two XAI SDK clients:
      - api client (api_key)
      - management client (api_key + management_api_key) if management_key provided
    """

    def __init__(
        self,
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        api_key: str,
        management_key: str | None = None,
        notify: Notify | None = None,
    ):
        if not ctx:
            raise ValueError("ctx is required")
        if not user:
            raise ValueError("user is required")
        if db is None:
            raise RuntimeError("Database connection is missing")
        if not api_key:
            raise ValueError("api_key must be provided")

        self.ctx = ctx
        self.user = user
        self.db = db
        self.notify = notify

        self.api_key = api_key
        self.management_key = management_key
        self.client = None

        if self.management_key:
            self.client: AsyncClient | None = (
                AsyncClient(api_key=self.api_key, management_api_key=self.management_key)
                if self.management_key
                else None
            )
        else:
            self.client: AsyncClient = AsyncClient(api_key=self.api_key)

        logger.bind(ctx=self.ctx, user=self.user.id).info("Grok clients created")

    @property
    def xai(self) -> AsyncClient:
        return self.client

    async def close(self) -> None:
        if self.client and hasattr(self.client, "close"):
            await self.client.close()


# this function is for initializing xai async client
def get_grok_conn(
    ctx: Context,
    user: UserORM,
    db: AsyncIOMotorDatabase,
    api_key: str,
    management_key: str | None = None,
    notify: Notify | None = None,
) -> GrokConnection:
    return GrokConnection(
        ctx=ctx,
        user=user,
        db=db,
        api_key=api_key,
        management_key=management_key,
        notify=notify,
    )
