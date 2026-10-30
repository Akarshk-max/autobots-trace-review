import pytest

from src.autobots.run_metrics.run_metrics_cache import RunMetricsCache
from src.autobots.run_metrics.run_metrics_scope import RunMetricsScope, ScopeKind

USER = "cache-user"
SPACE = "cache-space"


class _FakeRedis:
    def __init__(self):
        self.counters = {}

    async def incr(self, key):
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def get(self, key):
        value = self.counters.get(key)
        return None if value is None else str(value).encode()


@pytest.fixture
def redis(monkeypatch):
    fake = _FakeRedis()

    async def _fake_redis():
        return fake

    monkeypatch.setattr(RunMetricsCache, "_redis", staticmethod(_fake_redis))
    return fake


def test_a_personal_scope_watches_its_own_counter():
    scope = RunMetricsScope(ScopeKind.personal, USER)
    assert scope.generation_key() == f"user:{USER}"


def test_a_member_in_a_space_still_watches_their_own_counter():
    """`own_in_space` shows only this member's runs, so another member's run is irrelevant."""
    scope = RunMetricsScope(ScopeKind.own_in_space, USER, SPACE)
    assert scope.generation_key() == f"user:{USER}"


def test_a_whole_space_scope_watches_the_space_counter():
    """It shows every member's runs, so any member's run must invalidate it."""
    scope = RunMetricsScope(ScopeKind.whole_space, USER, SPACE)
    assert scope.generation_key() == f"space:{SPACE}"


@pytest.mark.asyncio
async def test_a_personal_run_bumps_only_the_user(redis):
    await RunMetricsCache.bump(USER, None)
    assert list(redis.counters) == [f"run_metrics:generation:user:{USER}"]


@pytest.mark.asyncio
async def test_a_run_inside_a_space_bumps_the_member_and_the_space(redis):
    await RunMetricsCache.bump(USER, SPACE)
    assert set(redis.counters) == {
        f"run_metrics:generation:user:{USER}",
        f"run_metrics:generation:space:{SPACE}",
    }


@pytest.mark.asyncio
async def test_another_members_run_invalidates_the_whole_space_page(redis):
    """The defect: an admin's cached page survived a colleague's completed run."""
    admin = RunMetricsScope(ScopeKind.whole_space, "admin", SPACE)
    before = await RunMetricsCache._generation(admin.generation_key())

    await RunMetricsCache.bump("some-other-member", SPACE)

    after = await RunMetricsCache._generation(admin.generation_key())
    assert after > before, "a colleague's run must change the admin's cache key"


@pytest.mark.asyncio
async def test_recording_survives_redis_being_down(monkeypatch):
    """A metric is already stored by this point; failing to invalidate must not raise."""

    async def _broken():
        raise RuntimeError("redis down")

    monkeypatch.setattr(RunMetricsCache, "_redis", staticmethod(_broken))
    await RunMetricsCache.bump(USER, SPACE)
    assert await RunMetricsCache._generation(f"user:{USER}") == 0
