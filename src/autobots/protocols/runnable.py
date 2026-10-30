from asyncio import Protocol
from typing import Any

from src.autobots.data_model.context import Context


class Runnable(Protocol):
    @staticmethod
    async def run(ctx: Context, kwargs: Any) -> Any: ...
