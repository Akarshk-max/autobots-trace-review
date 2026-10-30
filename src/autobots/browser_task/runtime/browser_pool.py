import asyncio
from datetime import UTC, datetime
from typing import Optional

from loguru import logger
from playwright.async_api import Browser, BrowserContext, async_playwright

from src.autobots.browser_task.runtime.helpers import get_rss_bytes


class BrowserPool:
    def __init__(
        self,
        *,
        max_jobs_per_browser: int = 200,
        idle_restart_seconds: int = 900,
        max_rss_bytes: int = 0,
    ):
        self.max_jobs_per_browser = max_jobs_per_browser
        self.idle_restart_seconds = idle_restart_seconds
        self.max_rss_bytes = max_rss_bytes

        self._pw = None
        self._browser: Browser | None = None

        self._jobs_done = 0
        self._last_used = datetime.now(UTC)
        self._active_contexts = 0

        self._recycles = 0  # NEW: counter

        self._lock = asyncio.Lock()

    def stats(self) -> dict:
        idle_s = int((datetime.now(UTC) - self._last_used).total_seconds())
        return {
            "active_contexts": self._active_contexts,
            "jobs_done": self._jobs_done,
            "idle_seconds": idle_s,
            "recycles_total": self._recycles,
        }

    async def start(self) -> None:
        async with self._lock:
            if self._browser:
                return
            self._pw = await async_playwright().start()

            self._browser = await self._pw.chromium.launch(
                headless=True , 
                args=[
                    "--no-sandbox",
                    "--disable-gpu",
                    "--no-zygote",
                    "--disable-background-networking",
                    "--disable-background-timer-throttling",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-breakpad",
                    "--disable-component-update",
                    "--disable-default-apps",
                    "--disable-domain-reliability",
                    "--disable-features=Translate,MediaRouter,OptimizationHints,site-per-process",
                    "--disable-hang-monitor",
                    "--disable-ipc-flooding-protection",
                    "--disable-popup-blocking",
                    "--disable-prompt-on-repost",
                    "--disable-renderer-backgrounding",
                    "--disable-sync",
                    "--metrics-recording-only",
                    "--no-first-run",
                    "--password-store=basic",
                    "--use-mock-keychain",
                    # "--disable-dev-shm-usage",  # enable only if you cannot set shm-size
                ],
            )

            self._jobs_done = 0
            self._active_contexts = 0
            self._last_used = datetime.now(UTC)
            logger.info("browser_pool_started")

    async def stop(self) -> None:
        async with self._lock:
            if self._browser:
                await self._browser.close()
                self._browser = None
            if self._pw:
                await self._pw.stop()
                self._pw = None
            logger.info("browser_pool_stopped")

    async def _relaunch_browser_locked(self, *, reason: str, extra: dict | None = None) -> None:
        extra = extra or {}
        logger.warning("browser_recycle_reason={} {}", reason, extra)

        await self._browser.close()
        self._browser = await self._pw.chromium.launch(
            headless=True,
            args=[
                    "--no-sandbox",
                    "--disable-gpu",
                    "--no-zygote",
                    "--disable-background-networking",
                    "--disable-background-timer-throttling",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-breakpad",
                    "--disable-component-update",
                    "--disable-default-apps",
                    "--disable-domain-reliability",
                    "--disable-features=Translate,MediaRouter,OptimizationHints,site-per-process",
                    "--disable-hang-monitor",
                    "--disable-ipc-flooding-protection",
                    "--disable-popup-blocking",
                    "--disable-prompt-on-repost",
                    "--disable-renderer-backgrounding",
                    "--disable-sync",
                    "--metrics-recording-only",
                    "--no-first-run",
                    "--password-store=basic",
                    "--use-mock-keychain",
                    # "--disable-dev-shm-usage",  # enable only if you cannot set shm-size
                ],
            )
        self._jobs_done = 0
        self._recycles += 1
        self._last_used = datetime.now(UTC)

    async def maybe_recycle(self) -> None:
        async with self._lock:
            if not self._browser:
                return

            # Do not recycle while contexts are in-flight
            if self._active_contexts > 0:
                return

            now = datetime.now(UTC)
            idle = (now - self._last_used).total_seconds()

            # NEW: RSS-based recycle (safe only when no active contexts)
            if self.max_rss_bytes:
                rss = get_rss_bytes()
                if rss and rss > self.max_rss_bytes:
                    await self._relaunch_browser_locked(
                        reason="rss",
                        extra={"rss_mb": int(rss / (1024 * 1024)), "threshold_mb": int(self.max_rss_bytes / (1024 * 1024))},
                    )
                    return

            if self._jobs_done >= self.max_jobs_per_browser:
                await self._relaunch_browser_locked(reason="max_jobs", extra={"jobs_done": self._jobs_done})
                return

            if idle > self.idle_restart_seconds:
                await self._relaunch_browser_locked(reason="idle", extra={"idle_s": int(idle)})
                return

            self._last_used = datetime.now(UTC)

    async def new_context(self, *, context_kwargs: Optional[dict] = None) -> BrowserContext:
        context_kwargs = context_kwargs or {}

        async with self._lock:
            if not self._browser:
                await self.start()
            assert self._browser is not None
            browser = self._browser
            self._last_used = datetime.now(UTC)
            self._active_contexts += 1

        try:
            return await browser.new_context(**context_kwargs)
        except Exception:
            await self.context_closed()
            raise

    async def context_closed(self) -> None:
        async with self._lock:
            self._active_contexts = max(0, self._active_contexts - 1)
            self._last_used = datetime.now(UTC)

    async def mark_job_done(self) -> None:
        async with self._lock:
            self._jobs_done += 1
            self._last_used = datetime.now(UTC)