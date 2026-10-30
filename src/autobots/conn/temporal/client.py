import asyncio
from temporalio.client import Client
from src.autobots.conn.temporal.converter import get_temporal_data_converter
from src.autobots.core.settings import SettingsProvider

temporal_client: Client | None = None
temporal_lock = asyncio.Lock()


async def get_temporal_client() -> Client:
    global temporal_client
    if temporal_client is not None:
        return temporal_client
    async with temporal_lock:
        if temporal_client is not None:
            return temporal_client
        settings = SettingsProvider.sget()
        temporal_client = await Client.connect(
            settings.TEMPORAL_ADDRESS,
            namespace=settings.TEMPORAL_NAMESPACE,
            api_key=settings.TEMPORAL_API_KEY,
            tls=settings.ENV != "local",
            data_converter=get_temporal_data_converter(),
        )
        return temporal_client
