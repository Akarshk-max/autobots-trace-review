import asyncio
from functools import lru_cache

from src.autobots import SettingsProvider


@lru_cache
def get_semaphore() -> asyncio.Semaphore:
    settings = SettingsProvider.sget()
    concurrency_limit = settings.ACTION_GRAPH_LOOPS_CONCURRENCY_LIMIT
    semaphore = asyncio.Semaphore(concurrency_limit)
    return semaphore
