import pytest


class FakeRedis:
    def __init__(self):
        self._store: dict[str, int] = {}
        self.expire_calls: list[tuple[str, int]] = []

    async def get(self, key: str):
        if key not in self._store:
            return None
        return str(self._store[key]).encode("utf-8")

    async def incr(self, key: str):
        self._store[key] = int(self._store.get(key, 0)) + 1
        return self._store[key]

    async def expire(self, key: str, seconds: int):
        self.expire_calls.append((key, seconds))
        return True


@pytest.mark.asyncio
async def test_redis_generation_get_and_bump():
    from src.autobots.core.cache.redis_generation import (
        SearchGenerationKey,
        bump_search_generation,
        get_search_generation,
    )
    from src.autobots.event_result.event_result_model import EventType

    r = FakeRedis()
    k = SearchGenerationKey(user_id="u1", event_type=EventType.action_run)

    assert await get_search_generation(key=k, redis_client=r) == 0

    new_val = await bump_search_generation(key=k, redis_client=r, expire_seconds=10)
    assert new_val == 1
    assert await get_search_generation(key=k, redis_client=r) == 1

    new_val2 = await bump_search_generation(key=k, redis_client=r, expire_seconds=10)
    assert new_val2 == 2
    assert await get_search_generation(key=k, redis_client=r) == 2

    assert len(r.expire_calls) >= 1
