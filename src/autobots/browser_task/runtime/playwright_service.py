import asyncio

from loguru import logger

from src.autobots.browser_task.inmem.scheduler import InMemoryScheduler
from src.autobots.browser_task.inmem.state import SchedulerConfig
from src.autobots.browser_task.runtime.executor import PlaywrightExecutor, PlaywrightExecutorConfig
from src.autobots.core.settings import Settings, SettingsProvider


class PlaywrightScrapeService:
    def __init__(self):
        self.executor: PlaywrightExecutor | None = None
        self.scheduler: InMemoryScheduler | None = None

        self._task: asyncio.Task | None = None

        # asyncio locks created lazily (inside running loop)
        self._start_lock: asyncio.Lock | None = None

        self._started_at = None

    def _ensure_lock(self) -> asyncio.Lock:
        if self._start_lock is None:
            # Only safe to call when an event loop is running
            self._start_lock = asyncio.Lock()
        return self._start_lock

    def is_running(self) -> bool:
        return bool(self._task and not self._task.done())

    async def start(self , settings: Settings = SettingsProvider.sget()) -> None:
        lock = self._ensure_lock()
        async with lock:
            if self.is_running():
                return

            # Lazy-create core objects inside running event loop
            if self.executor is None:
                self.executor = PlaywrightExecutor(
                    PlaywrightExecutorConfig(
                        max_jobs_per_browser=settings.MAX_JOBS_PER_BROWSER,
                        idle_restart_seconds=settings.BROWSER_IDLE_RESTART_SECONDS,
                        max_rss_mb=settings.PLAYWRIGHT_MAX_RSS_MB,  # disable by default , turn on when ready
                    )
                )

            if self.scheduler is None:
                self.scheduler = InMemoryScheduler(
                    executor=self.executor,
                    config=SchedulerConfig(queue_max_jobs=500),
                )

            logger.info("playwright_scrape_service_start")
            self._task = asyncio.create_task(self.scheduler.run_forever())

    async def stop(self) -> None:
        logger.info("playwright_scrape_service_stop")

        # Stop scheduler loop
        if self.scheduler is not None:
            await self.scheduler.shutdown()

        # Cancel scheduler task
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
            self._task = None

        # Stop browser pool
        if self.executor is not None:
            await self.executor.pool.stop()

        # Keep objects around or reset
        self.scheduler = None
        self.executor = None

    def stats(self) -> dict:
        running = self.is_running()
        sched_stats = self.scheduler.stats() if self.scheduler else {}
        pool_stats = self.executor.pool.stats() if (self.executor and self.executor.pool) else {}
        return {
            "running": running,
            "scheduler": sched_stats,
            "pool": pool_stats,
        }


playwright_scrape_service = PlaywrightScrapeService()