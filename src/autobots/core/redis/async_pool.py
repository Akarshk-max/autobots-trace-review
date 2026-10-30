from redis.asyncio import Redis
from redis_fastapi.deps import _get_pool_state

_app = None


def set_app(app) -> None:
    global _app
    _app = app


async def get_async_redis() -> Redis:
    if _app is None:
        raise RuntimeError("App not set — ensure the lifespan calls set_app(app)")
    return _get_pool_state(_app).get_async_client()
