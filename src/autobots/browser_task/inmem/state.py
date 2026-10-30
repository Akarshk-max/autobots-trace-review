import asyncio
from datetime import UTC, datetime, timedelta
from typing import Dict, Iterable, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from src.autobots.browser_task.compiler.models import JobPlan
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM


class SchedulerConfig(BaseModel):
    global_concurrency: int = Field(12, ge=1)
    per_domain_concurrency: int = Field(2, ge=1)
    queue_max_jobs: int = Field(500, ge=1)
    model_config = ConfigDict(extra="forbid")


class DomainState(BaseModel):
    cooldown_until: datetime | None = None
    recent_block_count: int = 0
    recent_fail_count: int = 0
    last_seen: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SchedulerState(BaseModel):
    domain: Dict[str, DomainState] = Field(default_factory=dict)
    cooldown_hits_total: int = 0

    def touch(self, domain: str) -> None:
        st = self.domain.setdefault(domain, DomainState())
        st.last_seen = datetime.now(UTC)

    def in_cooldown(self, domain: str) -> bool:
        st = self.domain.get(domain)
        if not st or not st.cooldown_until:
            return False
        return datetime.now(UTC) < st.cooldown_until

    def next_cooldown_sleep_s(
        self,
        domains: Iterable[str],
        *,
        floor_s: float = 0.05,
        ceil_s: float = 1.0,
    ) -> float:
        now = datetime.now(UTC)
        soonest: datetime | None = None
        for d in domains:
            st = self.domain.get(d)
            if not st or not st.cooldown_until:
                continue
            if soonest is None or st.cooldown_until < soonest:
                soonest = st.cooldown_until
        if not soonest:
            return floor_s
        dt = (soonest - now).total_seconds()
        if dt <= 0:
            return floor_s
        return min(max(dt, floor_s), ceil_s)

    def cooldown_active_count(self) -> int:
        now = datetime.now(UTC)
        n = 0
        for st in self.domain.values():
            if st.cooldown_until and now < st.cooldown_until:
                n += 1
        return n

    def set_cooldown(self, domain: str, seconds: int, *, reason: str = "") -> None:
        self.cooldown_hits_total += 1
        st = self.domain.setdefault(domain, DomainState())
        st.last_seen = datetime.now(UTC)

        reason_l = (reason or "").lower()
        if "blocked" in reason_l or "403" in reason_l:
            st.recent_block_count += 1
        elif "429" in reason_l:
            st.recent_block_count += 1
        else:
            st.recent_fail_count += 1

        mult = 1 + min(4, st.recent_block_count // 2)
        st.cooldown_until = datetime.now(UTC) + timedelta(seconds=int(seconds * mult))

    def prune(self, *, ttl_s: int = 1800) -> int:
        now = datetime.now(UTC)
        drop = []
        for d, st in self.domain.items():
            if (now - st.last_seen).total_seconds() > ttl_s:
                drop.append(d)
        for d in drop:
            self.domain.pop(d, None)
        return len(drop)


class JobEnvelope(BaseModel):
    job_id: str
    request_id: str
    user_id: Optional[str] = None
    plan: JobPlan
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    attempt_no: int = 0
    last_error: Optional[str] = None

    _future: asyncio.Future | None = PrivateAttr(default=None)

    # request-scoped objects
    _ctx: Context | None = PrivateAttr(default=None)
    _user: UserORM | None = PrivateAttr(default=None)
    _db: AsyncIOMotorDatabase | None = PrivateAttr(default=None)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def future(self) -> asyncio.Future | None:
        return self._future

    @future.setter
    def future(self, value: asyncio.Future | None) -> None:
        self._future = value

    @property
    def ctx(self) -> Context:
        if self._ctx is None:
            raise RuntimeError("job_ctx_missing")
        return self._ctx

    @ctx.setter
    def ctx(self, v: Context) -> None:
        self._ctx = v

    @property
    def user(self) -> UserORM:
        if self._user is None:
            raise RuntimeError("job_user_missing")
        return self._user

    @user.setter
    def user(self, v: UserORM) -> None:
        self._user = v

    @property
    def db(self) -> AsyncIOMotorDatabase:
        if self._db is None:
            raise RuntimeError("job_db_missing")
        return self._db

    @db.setter
    def db(self, v: AsyncIOMotorDatabase) -> None:
        self._db = v