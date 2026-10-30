import asyncio
from collections import deque
from typing import Deque, Dict, Optional, Set

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from src.autobots.browser_task.exception_handler.base_exception import QueueFull
from src.autobots.browser_task.inmem.state import JobEnvelope, SchedulerConfig, SchedulerState
from src.autobots.browser_task.runtime.executor import PlaywrightExecutor
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM


class InMemoryFairQueue(BaseModel):
    max_total_jobs: int = 500

    _by_domain: Dict[str, Deque[JobEnvelope]] = PrivateAttr(default_factory=dict)
    _active_domains: Deque[str] = PrivateAttr(default_factory=deque)
    _active_set: Set[str] = PrivateAttr(default_factory=set)

    _total: int = PrivateAttr(default=0)
    _cv: asyncio.Condition = PrivateAttr(default_factory=asyncio.Condition)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def size(self) -> int:
        return self._total

    def active_domains_count(self) -> int:
        return len(self._active_domains)

    async def active_domains_snapshot(self) -> list[str]:
        async with self._cv:
            return list(self._active_domains)

    async def put(self, domain: str, job: JobEnvelope) -> None:
        async with self._cv:
            if self._total >= self.max_total_jobs:
                raise QueueFull("scrape queue is full")

            q = self._by_domain.setdefault(domain, deque())
            q.append(job)
            self._total += 1

            if domain not in self._active_set:
                self._active_domains.append(domain)
                self._active_set.add(domain)

            self._cv.notify_all()

    async def get_head_domain(self) -> str:
        async with self._cv:
            while not self._active_domains:
                await self._cv.wait()
            return self._active_domains[0]

    async def rotate_active(self) -> None:
        async with self._cv:
            if self._active_domains:
                self._active_domains.rotate(-1)

    async def pop_one(self, domain: str) -> Optional[JobEnvelope]:
        async with self._cv:
            q = self._by_domain.get(domain)
            if not q:
                self._drop_domain(domain)
                return None

            job = q.popleft()
            self._total -= 1

            if not q:
                self._drop_domain(domain)
            else:
                self._active_domains.rotate(-1)

            return job

    def _drop_domain(self, domain: str) -> None:
        if domain in self._active_set:
            self._active_set.remove(domain)
        try:
            self._active_domains.remove(domain)
        except ValueError:
            pass
        self._by_domain.pop(domain, None)


class InMemoryScheduler(BaseModel):
    executor: PlaywrightExecutor
    config: SchedulerConfig

    queue: InMemoryFairQueue = Field(default_factory=InMemoryFairQueue)
    state: SchedulerState = Field(default_factory=SchedulerState)

    _global_sem: asyncio.Semaphore = PrivateAttr()
    _domain_sems: Dict[str, asyncio.Semaphore] = PrivateAttr(default_factory=dict)

    _stop: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)
    _inflight: set[asyncio.Task] = PrivateAttr(default_factory=set)

    # prune cadence (gpt's suggestion)
    _ticks: int = PrivateAttr(default=0)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def __init__(self, **data):
        super().__init__(**data)
        self.queue = InMemoryFairQueue(max_total_jobs=self.config.queue_max_jobs)
        self._global_sem = asyncio.Semaphore(self.config.global_concurrency)

    def stats(self) -> dict:
        global_in_use = self.config.global_concurrency - int(getattr(self._global_sem, "_value", 0))
        return {
            "queue_depth": self.queue.size(),
            "queue_max": self.config.queue_max_jobs,
            "active_domains": self.queue.active_domains_count(),
            "inflight_tasks": len(self._inflight),
            "global_concurrency": self.config.global_concurrency,
            "global_in_use": max(0, global_in_use),
            "per_domain_concurrency": self.config.per_domain_concurrency,
            "cooldown_active": self.state.cooldown_active_count(),
            "cooldown_hits_total": self.state.cooldown_hits_total,
        }

    def _domain_sem(self, domain: str) -> asyncio.Semaphore:
        sem = self._domain_sems.get(domain)
        if not sem:
            sem = asyncio.Semaphore(self.config.per_domain_concurrency)
            self._domain_sems[domain] = sem
        return sem

    def _cleanup_domain(self, domain: str) -> None:
        # do NOT delete cooldown state here; it prevents immediate re-hits.
        self._domain_sems.pop(domain, None)

    async def submit(self,ctx : Context, user : UserORM, db : AsyncIOMotorDatabase, job: JobEnvelope) -> None:
        job.ctx = ctx
        job.user = user
        job.db = db
        # Track domain activity for TTL prune
        self.state.touch(job.plan.domain)
        await self.queue.put(job.plan.domain, job)

    def stop(self) -> None:
        self._stop.set()

    async def shutdown(self) -> None:
        self.stop()

        tasks = list(self._inflight)
        for t in tasks:
            t.cancel()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run_forever(self) -> None:
        logger.info(
            "inmem_scheduler_start global={} per_domain={} queue_max={}",
            self.config.global_concurrency,
            self.config.per_domain_concurrency,
            self.config.queue_max_jobs,
        )

        while not self._stop.is_set():
            try:
                # Periodic prune (cheap)
                self._ticks += 1
                if self._ticks % 500 == 0:
                    dropped = self.state.prune(ttl_s=1800)
                    if dropped:
                        logger.info("scheduler_state_pruned dropped={}", dropped)

                # I want to avoid sleeping just because the current head domain is in cooldown.
                # Strategy:
                # - snapshot active domains
                # - rotate through them until we find one not in cooldown
                # - if all are in cooldown -> sleep until soonest expires

                domains = await self.queue.active_domains_snapshot()
                if not domains:
                    # queue.get_head_domain() will block anyway; but keep loop safe it's simple
                    await asyncio.sleep(0.01)
                    continue

                runnable_domain = None
                # Try at most N rotations (one full cycle)
                for _ in range(len(domains)):
                    domain = await self.queue.get_head_domain()

                    # Touch for TTL tracking
                    self.state.touch(domain)

                    if not self.state.in_cooldown(domain):
                        runnable_domain = domain
                        break

                    # Not runnable; rotate and check next immediately (NO sleep here)
                    await self.queue.rotate_active()

                if runnable_domain is None:
                    # All active domains are cooling down -> sleep until soonest cooldown ends
                    domains2 = await self.queue.active_domains_snapshot()
                    await asyncio.sleep(self.state.next_cooldown_sleep_s(domains2))
                    continue

                # Acquire capacity
                await self._global_sem.acquire()
                dom_sem = self._domain_sem(runnable_domain)
                await dom_sem.acquire()

                job = await self.queue.pop_one(runnable_domain)
                if not job:
                    self._cleanup_domain(runnable_domain)
                    dom_sem.release()
                    self._global_sem.release()
                    await asyncio.sleep(0)
                    continue

                fut = job.future
                if fut and fut.done():
                    dom_sem.release()
                    self._global_sem.release()
                    await asyncio.sleep(0)
                    continue

                t = asyncio.create_task(self._run_one(job, dom_sem))
                self._inflight.add(t)
                t.add_done_callback(lambda _t: self._inflight.discard(_t))

            except Exception as e:
                logger.exception("inmem_scheduler_loop_error {}", e)
                await asyncio.sleep(0.1)

    async def _run_one(self, job: JobEnvelope, dom_sem: asyncio.Semaphore) -> None:
        try:
            result = await self.executor.execute(job, scheduler_state=self.state)
            fut = job._future
            if fut and not fut.done():
                fut.set_result(result)
        except asyncio.CancelledError:
            fut = job._future
            if fut and not fut.done():
                fut.set_exception(asyncio.CancelledError())
            raise
        except Exception as e:
            fut = job._future
            if fut and not fut.done():
                fut.set_exception(e)
        finally:
            dom_sem.release()
            self._global_sem.release()