import asyncio
import hashlib
import json
import random
import time
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from loguru import logger
from playwright.async_api import Locator, Page, Route
from playwright.async_api import Request as PWRequest
from pydantic import BaseModel, ConfigDict, Field

from src.autobots.browser_task.inmem.state import JobEnvelope, SchedulerState
from src.autobots.browser_task.policies.base import (
    _JS_HEAVY_NAV_MS,
    _JS_HEAVY_TOTAL_MS,
    _STEALTH_NAV_MS,
    _STEALTH_TOTAL_MS,
)
from src.autobots.browser_task.runtime.artifacts import Artifact, ArtifactKind
from src.autobots.browser_task.runtime.browser_pool import BrowserPool
from src.autobots.browser_task.runtime.helpers import (
    STEALTH_UA_PROFILES,
    BudgetTracker,
    classify_exception,
    detect_soft_block_html,
    extract_all,
    extract_images_http,
    fetch_html,
)
from src.autobots.browser_task.runtime.modules.auth_handler import (
    AuthModule,
    dismiss_post_login_popups,
)
from src.autobots.browser_task.runtime.modules.base import BrowserModule
from src.autobots.browser_task.runtime.modules.storage_state_handler import StorageStateModule
from src.autobots.browser_task.runtime.modules.table_page_handler import TableExtractionModule
from src.autobots.browser_task.runtime.modules.task_handler import (
    ChartExtractionModule,
    DefaultTableExtractionModule,
    DownloadsModule,
    NetworkJsonModule,
    ScreenshotPdfModule,
)
from src.autobots.browser_task.runtime.modules.utils import log_ctx, upload_bytes_public_s3
from src.autobots.core.settings import Settings, SettingsProvider

_MAX_TITLE = 200
_MAX_HASH_TEXT = 10_000
_MAX_BODY_SCAN_CHARS = 20_000

class AdaptiveNetworkIdle:
    def __init__(self, quiet_ms: int = 500):
        self.quiet_ms = quiet_ms
        self.inflight = 0
        self._last_activity = time.time()

    def _touch(self):
        self._last_activity = time.time()

    def attach(self, page: Page):
        page.on("request", self._on_request)
        page.on("requestfinished", self._on_done)
        page.on("requestfailed", self._on_done)

    def _on_request(self, _):
        self.inflight += 1
        self._touch()

    def _on_done(self, _):
        self.inflight = max(0, self.inflight - 1)
        self._touch()

    def is_quiet(self) -> bool:
        return self.inflight == 0 and (time.time() - self._last_activity) * 1000 >= self.quiet_ms


def _locator_for(page: Page, sel: str) -> Locator:
    """
    Supports:
      - "xpath=//..."
      - raw XPath: starts with // or (//
      - "css=..." (optional)
      - otherwise CSS
    """
    s = (sel or "").strip()
    if not s:
        return page.locator("css=__never__")  # empty -> no matches
    if s.startswith("xpath="):
        return page.locator(s)
    if s.startswith("css="):
        return page.locator(s[len("css="):])
    if s.startswith("//") or s.startswith("(//"):
        return page.locator("xpath=" + s)
    return page.locator(s)


def _sha256_text(s: str) -> str:
    b = (s or "").encode("utf-8", errors="ignore")[:_MAX_HASH_TEXT]
    return hashlib.sha256(b).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


async def _apply_network_rules(
    page: Page,
    *,
    tracker: BudgetTracker,
    block_types: set[str],
    allow_hosts: Optional[set[str]] = None,
    block_hosts: Optional[set[str]] = None,
) -> None:
    allow_hosts_l = {h.lower() for h in (allow_hosts or set())} if allow_hosts else None
    block_hosts_l = {h.lower() for h in (block_hosts or set())} if block_hosts else None

    async def _route(route: Route, req: PWRequest):
        try:
            tracker.inc_request()
        except Exception:
            try:
                await route.abort()
            except Exception:
                pass
            return

        host = (urlparse(req.url).hostname or "").lower()
        if allow_hosts_l is not None and host and host not in allow_hosts_l:
            await route.abort()
            return
        if block_hosts_l and host and host in block_hosts_l:
            await route.abort()
            return

        rtype = req.resource_type
        if rtype in block_types:
            await route.abort()
            return
        await route.continue_()

    await page.route("**/*", _route)


def _detect_blocked(hay: str, signals: list[str]) -> Optional[str]:
    h = (hay or "").lower()
    # here if length is more than 1000 it means we get good enough result , no need to check for blocking.
    if len(h) >= 1000:
        return None
    for s in signals or []:
        if s and s.lower() in h:
            return s
    return None

async def dom_is_stable(page: Page, quiet_ms: int) -> bool:
    try:
        return await page.evaluate(
            "(ms) => Date.now() - (window.__domStableAt || 0) >= ms",
            quiet_ms,
        )
    except Exception:
        return False
    
async def install_dom_stability_probe(page: Page):
    await page.add_init_script("""
    (() => {
    window.__domStableAt = Date.now();
    const obs = new MutationObserver(() => {
        window.__domStableAt = Date.now();
    });
    obs.observe(document.documentElement, {
        childList: true,
        subtree: true,
        attributes: true,
    });
    })();
    """)

async def wait_for_adaptive_idle(
    *,
    page: Page,
    tracker: BudgetTracker,
    timeout_ms: int,
    net_idle: AdaptiveNetworkIdle,
    selectors_any: list[str] | None = None,
    selectors_all: list[str] | None = None,
    quiet_ms: int = 500,
    poll_ms: int = 50,
    ):
    t0 = time.time()
    selectors_any = selectors_any or []
    selectors_all = selectors_all or []

    while True:
        tracker.check_time()

        elapsed = (time.time() - t0) * 1000
        if elapsed > timeout_ms:
            raise TimeoutError("adaptive_networkidle_timeout")

        if selectors_all:
            ok = True
            for sel in selectors_all:
                if await _locator_for(page, sel).count() == 0:
                    ok = False
                    break
            if ok:
                return

        if selectors_any:
            for sel in selectors_any:
                if await _locator_for(page, sel).count() > 0:
                    return

        if net_idle.is_quiet() and await dom_is_stable(page, quiet_ms):
            return

        await asyncio.sleep(poll_ms / 1000)

 
def _deterministic_backoff_s(attempt_no: int) -> float:
    return [0.2, 0.5, 1.0, 2.0][min(attempt_no - 1, 3)]


def _url_with_page_param(url: str, param: str, page_no: int) -> str:
    try:
        p = urlparse(url)
        q = dict(parse_qsl(p.query, keep_blank_values=True))
        q[param] = str(page_no)
        new_q = urlencode(q, doseq=True)
        return urlunparse((p.scheme, p.netloc, p.path, p.params, new_q, p.fragment))
    except Exception:
        return url


class PlaywrightExecutorConfig(BaseModel):
    max_jobs_per_browser: int = Field(200, ge=1)
    idle_restart_seconds: int = Field(900, ge=1)

    cooldown_on_block_seconds: int = Field(90, ge=1)
    cooldown_on_429_seconds: int = Field(120, ge=1)

    max_rss_mb: int = Field(0, ge=0)
    model_config = ConfigDict(extra="forbid")


class PlaywrightExecutor:
    """
    Updated:
    - Supports browser modules (`self._modules`) that can run in rendered mode.
    - Runs modules AFTER readiness + expectations checks, and BEFORE page.content()/extract_all.
    - Optional: if a module declares `attach_before_goto=True`, it will be invoked once
      immediately after page creation (so it can register event listeners before navigation).
    """

    def __init__(
        self,
        config: PlaywrightExecutorConfig | None = None,
        settings: Settings = SettingsProvider.sget(),
    ):
        max_jobs = config.max_jobs_per_browser if config else settings.MAX_JOBS_PER_BROWSER
        idle_restart = config.idle_restart_seconds if config else settings.BROWSER_IDLE_RESTART_SECONDS
        max_rss_mb = config.max_rss_mb if config else settings.PLAYWRIGHT_MAX_RSS_MB
        self._storage_state_cache: dict[str, tuple[float, dict]] = {}
        self._storage_state_lock = asyncio.Lock()
        self._stealth_ua_cache: dict[str, dict] = {}

        self.config = PlaywrightExecutorConfig(
            max_jobs_per_browser=max_jobs,
            idle_restart_seconds=idle_restart,
            max_rss_mb=max_rss_mb,
        )

        self.pool = BrowserPool(
            max_jobs_per_browser=self.config.max_jobs_per_browser,
            idle_restart_seconds=self.config.idle_restart_seconds,
            max_rss_bytes=(self.config.max_rss_mb * 1024 * 1024) if self.config.max_rss_mb else 0,
        )

        # modules
        self._modules : List[BrowserModule] = [
            NetworkJsonModule(),
            ScreenshotPdfModule(),
            DownloadsModule(),
            TableExtractionModule(),
            DefaultTableExtractionModule(),
            ChartExtractionModule(),
            StorageStateModule(),
        ]

    def _get_stealth_ua(self, domain: str) -> dict:
        if domain in self._stealth_ua_cache:
            return self._stealth_ua_cache[domain]

        ua = random.choice(STEALTH_UA_PROFILES)
        self._stealth_ua_cache[domain] = ua
        return ua

    # Policy fallback (selective)
    def _policy_chain_for(self, plan) -> list[str]:
        if (plan.policy_id or "default") == "default":
            return ["default", "js-heavy", "stealth-lite"]
        return [plan.policy_id]

    def _select_next_policies(self, chain: list[str], failure_type: str) -> list[str]:
        """
        Avoid wasting attempts:
        - blocked/403 => jump to stealth-lite
        - timeouts/expectation/unknown => js-heavy then stealth-lite
        - 404/429 => handled earlier (no switch)
        """
        failure_type = (failure_type or "").lower()

        if len(chain) <= 1:
            return chain

        if failure_type in ("http_403", "blocked_signal"):
            out = ["default", "stealth-lite"]
            return [p for p in out if p in chain]

        return chain
    
    async def _extract_images_rendered(self, page: Page, plan) -> list[dict]:
        limit = max(0, min(int(getattr(plan.extraction, "images_limit", 200) or 200), 5000))
        include_bg = bool(getattr(plan.extraction, "include_background_images", False))
        sels = list(getattr(plan.extraction, "image_selectors", []) or [])
        scopes = list(getattr(plan.extraction, "image_scope_selectors", []) or [])

        logger.info("image_scope_selectors={}", scopes)

        if limit <= 0:
            return []

        roots: list[Locator] = []
        if scopes:
            for sc in scopes:
                try:
                    loc = _locator_for(page, sc)
                    roots.append(loc)
                except Exception:
                    continue

        if not roots:
            roots = [page.locator("css=html")]

        async def _extract_from_locator(root: Locator) -> list[dict]:
            # NOTE: packed arg: {"rootEl": ElementHandle, "p": {...}}
            js = r"""
            async (arg) => {
            const rootEl = arg && arg.rootEl ? arg.rootEl : null;
            const p = (arg && arg.p) ? arg.p : {};

            function abs(u) {
                try { return new URL(u, location.href).toString(); } catch(e) { return ""; }
            }

            function pickFromSrcset(srcset) {
                if (!srcset) return "";
                const parts = srcset.split(",").map(x => x.trim()).filter(Boolean);

                let best = "";
                let bestScore = -1;

                for (const part of parts) {
                const toks = part.split(/\s+/).filter(Boolean);
                if (!toks.length) continue;

                const u = toks[0];
                let score = 0;

                if (toks.length > 1) {
                    const d = toks[1];
                    if (d.endsWith("w")) {
                    const n = parseInt(d.slice(0, -1), 10);
                    if (!Number.isNaN(n)) score = n;
                    } else if (d.endsWith("x")) {
                    const n = parseFloat(d.slice(0, -1));
                    if (!Number.isNaN(n)) score = n * 10000; // prioritize density
                    }
                }

                if (score > bestScore) {
                    bestScore = score;
                    best = u;
                }
                }
                return best;
            }

            function pickSrc(el) {
                // Prefer currentSrc for Next/Image, <picture>, responsive images
                const current = (el.currentSrc || "").trim();

                const src = (el.getAttribute("src") || "").trim();
                const ds  =
                (el.getAttribute("data-src") ||
                el.getAttribute("data-original") ||
                el.getAttribute("data-lazy-src") ||
                "").trim();
                const ss  =
                (el.getAttribute("srcset") ||
                el.getAttribute("data-srcset") ||
                "").trim();

                let chosen = "";
                if (current) chosen = current;
                else if (ss) chosen = pickFromSrcset(ss);
                else if (ds) chosen = ds;
                else chosen = src;

                return { chosen, srcset: ss };
            }

            function isVisibleEnough(el) {
                const r = el.getBoundingClientRect();
                return r.width >= 16 && r.height >= 16;
            }

            function imageType(u) {
                const m = u.match(/\.([a-z0-9]+)(?:\?|#|$)/i);
                return m ? m[1].toLowerCase() : "";
            }

            const limit = Number(p.limit || 200);
            const includeBg = !!p.includeBg;
            const out = [];
            const seen = new Set();

            if (!rootEl || !rootEl.querySelectorAll) return out;

            /* -------- IMG TAGS -------- */
            const imgs = Array.from(rootEl.querySelectorAll("img"));
            for (const img of imgs) {
                if (out.length >= limit) break;
                if (!isVisibleEnough(img)) continue;

                const alt = (img.getAttribute("alt") || "").trim();
                const { chosen, srcset } = pickSrc(img);
                const u = abs(chosen);
                if (!u || seen.has(u)) continue;

                const ext = imageType(u);
                const allowed = ["jpg","jpeg","png","webp","avif","gif","svg"];
                // allow unknown ext too (some CDNs use no extension)
                if (ext && !allowed.includes(ext)) continue;

                seen.add(u);
                out.push({
                url: u,
                alt,
                source: "img",
                meta: {
                    srcset: srcset || undefined,
                    ext,
                },
                });
            }

            /* -------- <PICTURE><SOURCE> -------- */
            const sources = Array.from(rootEl.querySelectorAll("picture source[srcset]"));
            for (const s of sources) {
                if (out.length >= limit) break;
                const ss = (s.getAttribute("srcset") || "").trim();
                if (!ss) continue;

                const best = pickFromSrcset(ss);
                const u = abs(best);
                if (!u || seen.has(u)) continue;

                const ext = imageType(u);
                const allowed = ["jpg","jpeg","png","webp","avif","gif","svg"];
                if (ext && !allowed.includes(ext)) continue;

                seen.add(u);
                out.push({
                url: u,
                alt: "",
                source: "picture_source",
                meta: { srcset: ss, ext },
                });
            }

            /* -------- BACKGROUND IMAGES -------- */
            if (includeBg) {
                const els = Array.from(rootEl.querySelectorAll("[style], div, section"));
                for (const el of els) {
                if (out.length >= limit) break;

                const st = window.getComputedStyle(el);
                const bg = st ? st.backgroundImage : "";
                if (!bg || bg === "none") continue;

                for (const m of bg.matchAll(/url\(([^)]+)\)/g)) {
                    if (out.length >= limit) break;
                    const raw = m[1].replace(/['"]/g, "").trim();
                    const u = abs(raw);
                    if (!u || seen.has(u)) continue;

                    const ext = imageType(u);
                    if (ext && !["jpg","jpeg","png","webp","avif"].includes(ext)) continue;

                    seen.add(u);
                    out.push({
                    url: u,
                    alt: "",
                    source: "background_computed",
                    meta: { ext },
                    });
                }
                }
            }

            return out;
            }
            """

            h = await root.first.element_handle()
            if not h:
                return []
            try:
                res = await page.evaluate(
                    js,
                    {"rootEl": h, "p": {"limit": limit, "includeBg": include_bg}},
                )
                return res if isinstance(res, list) else []
            finally:
                try:
                    await h.dispose()
                except Exception:
                    pass

        results: list[dict] = []
        seen_urls: set[str] = set()
        inline_seen = 0

        # If selectors provided, treat them as either direct image nodes OR containers
        if sels:
            for sel in sels:
                if len(results) >= limit:
                    break
                try:
                    loc = _locator_for(page, sel)
                    n = min(await loc.count(), 20)
                    for i in range(n):
                        if len(results) >= limit:
                            break
                        chunk = await _extract_from_locator(loc.nth(i))
                        for item in chunk:
                            u = item.get("url", "") or ""
                            if u:
                                if u in seen_urls:
                                    continue
                                seen_urls.add(u)
                                results.append(item)
                            else:
                                # keep inline SVG entries too (no url)
                                inline_seen += 1
                                item["_inline_id"] = inline_seen
                                results.append(item)

                            if len(results) >= limit:
                                break
                except Exception:
                    continue
            return results[:limit]

        for root in roots:
            if len(results) >= limit:
                break
            try:
                chunk = await _extract_from_locator(root)
                for item in chunk:
                    u = item.get("url", "") or ""
                    if u:
                        if u in seen_urls:
                            continue
                        seen_urls.add(u)
                        results.append(item)
                    else:
                        inline_seen += 1
                        item["_inline_id"] = inline_seen
                        results.append(item)

                    if len(results) >= limit:
                        break
            except Exception:
                continue

        return results[:limit]
                    


    def _make_policy_view(self, plan, policy_id: str):
        base_ready = "auto"
        base_any: list[str] = []
        base_all: list[str] = []

        for s in reversed(plan.steps or []):
            if getattr(s, "mode", None) == "rendered":
                base_ready = getattr(s, "ready", "auto") or "auto"
                base_any = list(getattr(s, "selectors_any", []) or [])
                base_all = list(getattr(s, "selectors_all", []) or [])
                break

        budgets = plan.budgets.model_copy(deep=True)
        network = plan.network.model_copy(deep=True)
        context = plan.context.model_copy(deep=True)

        StepCls = type(plan.steps[0]) if plan.steps else None

        if policy_id == "js-heavy":
            budgets.max_total_ms = max(int(budgets.max_total_ms), _JS_HEAVY_TOTAL_MS)
            budgets.max_nav_ms = max(int(budgets.max_nav_ms), _JS_HEAVY_NAV_MS)
            budgets.max_requests = max(int(budgets.max_requests), 180)
            if "images" in (plan.extraction.tasks or []):
                network.block_resource_types = ["font"]
            else:
                network.block_resource_types = ["font", "media"]


            assert StepCls is not None
            steps = [
                StepCls.model_validate(
                    {
                        "mode": "rendered",
                        "nav_timeout_ms": _JS_HEAVY_NAV_MS,
                        "ready": base_ready,
                        "selectors_any": base_any,
                        "selectors_all": base_all,
                    }
                ),
                StepCls.model_validate(
                    {
                        "mode": "rendered",
                        "nav_timeout_ms": _JS_HEAVY_NAV_MS,
                        "ready": "networkidle",
                        "selectors_any": [],
                        "selectors_all": [],
                    }
                ),
            ]

        elif policy_id == "stealth-lite":
            budgets.max_total_ms = max(int(budgets.max_total_ms), _STEALTH_TOTAL_MS)
            budgets.max_nav_ms = max(int(budgets.max_nav_ms), _STEALTH_NAV_MS)
            budgets.max_requests = max(int(budgets.max_requests), 180)
            if "images" in (plan.extraction.tasks or []):
                network.block_resource_types = ["font"]
            else:
                network.block_resource_types = ["font", "media"]

            if not context.locale:
                context.locale = "en-IN"
            if not context.timezone_id:
                context.timezone_id = "Asia/Kolkata"

            extra = dict(context.extra_http_headers or {})
            if "Accept-Language" not in extra:
                extra["Accept-Language"] = "en-IN,en;q=0.9"
            context.extra_http_headers = extra

            ua_cfg = self._get_stealth_ua(plan.domain)

            context.user_agent = ua_cfg["ua"]

            extra = dict(context.extra_http_headers or {})
            extra.update({
                "Accept-Language": "en-IN,en;q=0.9",
                "Sec-CH-UA": ua_cfg["sec_ch_ua"],
                "Sec-CH-UA-Mobile": "?0",
                "Sec-CH-UA-Platform": f'"{ua_cfg["sec_ch_platform"]}"',
            })
            context.extra_http_headers = extra

            assert StepCls is not None
            steps = [
                StepCls.model_validate(
                    {
                        "mode": "rendered",
                        "nav_timeout_ms": _STEALTH_NAV_MS,
                        "ready": base_ready,
                        "selectors_any": base_any,
                        "selectors_all": base_all,
                    }
                ),
                StepCls.model_validate(
                    {
                        "mode": "rendered",
                        "nav_timeout_ms": _STEALTH_NAV_MS,
                        "ready": "networkidle",
                        "selectors_any": [],
                        "selectors_all": [],
                    }
                ),
            ]

        else:
            steps = [s.model_copy(deep=True) for s in plan.steps]

        for s in steps:
            s.nav_timeout_ms = min(int(s.nav_timeout_ms), int(budgets.max_nav_ms))

        p2 = plan.model_copy(deep=True)
        p2.storage_state_url = getattr(plan, "storage_state_url", None)
        p2.policy_id = policy_id
        p2.steps = steps
        p2.budgets = budgets
        p2.network = network
        p2.context = context
        return p2

    # Storage state helpers
    def _normalize_cookie_for_playwright(self, c: dict) -> dict:
        if not isinstance(c, dict):
            return {}

        name = c.get("name")
        value = c.get("value")
        domain = c.get("domain")
        path = c.get("path", "/")

        if not name or value is None or not domain:
            return {}

        expires = c.get("expires", None)
        if expires is None:
            expires = c.get("expirationDate", None)

        session = c.get("session", None)
        if session is True:
            expires = -1
        if expires is None:
            expires = -1

        ss = c.get("sameSite", None)
        ss_out = None
        if isinstance(ss, str):
            s = ss.strip().lower()
            if s == "lax":
                ss_out = "Lax"
            elif s == "strict":
                ss_out = "Strict"
            elif s in ("none", "no_restriction", "no-restriction"):
                ss_out = "None"
            elif s in ("unspecified", ""):
                ss_out = None
            else:
                ss_out = None

        out = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path or "/",
            "expires": float(expires) if isinstance(expires, (int, float)) else -1,
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", False)),
        }

        if ss_out:
            out["sameSite"] = ss_out

        return out

    def _normalize_storage_state(self, data: Any) -> dict:
        if isinstance(data, dict):
            cookies = data.get("cookies", [])
            origins = data.get("origins", []) or []
            if isinstance(cookies, list):
                norm: List[dict] = []
                for c in cookies:
                    cc = self._normalize_cookie_for_playwright(c)
                    if cc:
                        norm.append(cc)
                return {"cookies": norm, "origins": origins}
            return {"cookies": [], "origins": origins}

        if isinstance(data, list):
            norm: List[dict] = []
            for c in data:
                cc = self._normalize_cookie_for_playwright(c)
                if cc:
                    norm.append(cc)
            return {"cookies": norm, "origins": []}

        raise RuntimeError("storage_state_invalid_json_shape")

    async def _fetch_storage_state_json(self, url: str) -> dict:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            raise RuntimeError("storage_state_url_invalid_scheme")

        timeout = httpx.Timeout(10.0, connect=5.0)
        limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)

        async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()

            content = r.content
            if content and len(content) > 1_000_000:
                raise RuntimeError("storage_state_too_large")

            raw = json.loads(content.decode("utf-8", errors="strict"))

        return self._normalize_storage_state(raw)

    async def _get_storage_state_cached(self, url: str, *, ttl_s: int = 300) -> dict:
        now = time.time()
        async with self._storage_state_lock:
            hit = self._storage_state_cache.get(url)
            if hit:
                ts, val = hit
                if (now - ts) <= ttl_s:
                    return val

        data = await self._fetch_storage_state_json(url)

        async with self._storage_state_lock:
            self._storage_state_cache[url] = (now, data)

        return data

    async def _bounded_body_text(self, page: Page, *, max_chars: int) -> str:
        try:
            t = await page.evaluate(
                """(maxLen) => {
                  const el = document.body;
                  const s = el ? (el.innerText || "") : "";
                  return s.slice(0, maxLen);
                }""",
                int(max_chars),
            )
            return (t or "") if isinstance(t, str) else ""
        except Exception:
            return ""


    async def _run_modules(
        self,
        *,
        job: JobEnvelope,
        page: Page,
        plan,
        tracker: BudgetTracker,
        when: str,
        artifacts: list[Any],
    ) -> None:
        """
        Run all modules, or only those that want to run at a specific time.
        `when` values:
          - "before_goto": only modules with attach_before_goto=True
          - "after_ready": modules without attach_before_goto (default place)
        """
        browser = getattr(plan, "browser", None)
        if not browser:
            return

        def _enabled(x) -> bool:
            return bool(x and getattr(x, "enabled", False))

        shots = getattr(browser, "screenshots", None)
        pdfp  = getattr(browser, "pdf", None)
        dl    = getattr(browser, "downloads", None)
        net   = getattr(browser, "network", None)
        tabs  = getattr(browser, "tables", None)
        ch    = getattr(browser, "charts", None)
        auth  = getattr(browser, "auth", None)

        logger.bind(ctx=log_ctx(job)).info(
            "run_modules_check when={} shots={} pdf={} dl={} net={} tabs={} ch={} auth={}",
            when, _enabled(shots), _enabled(pdfp), _enabled(dl), _enabled(net), _enabled(tabs), _enabled(ch), _enabled(auth)
        )

        if not any([
            _enabled(shots),
            _enabled(pdfp),
            _enabled(dl),
            _enabled(net),
            _enabled(tabs),
            _enabled(ch),
            _enabled(auth),
        ]):
            logger.bind(ctx=log_ctx(job)).info("run_modules_skipping_no_features_enabled when={}", when)
            return

        if not self._modules:
            logger.bind(ctx=log_ctx(job)).warning("run_modules_skipping_no_modules_registered")
            return

        logger.bind(ctx=log_ctx(job)).info(
            "run_modules_starting count={} when={}",
            len(self._modules), when
        )


        target_url = str(getattr(plan, "url", "") or "")
        hostname = (urlparse(target_url).hostname or "").lower()
        is_soundon = "soundon.global" in hostname

        logger.bind(ctx=log_ctx(job)).info(
            "run_modules_starting count={} when={} hostname={} is_soundon={}",
            len(self._modules), when, hostname, is_soundon
        )

        for m in self._modules:
            try:
                attach_first = bool(getattr(m, "attach_before_goto", False))
                if when == "before_goto" and not attach_first:
                    continue
                if when == "after_ready" and attach_first:
                    continue

                if isinstance(m, TableExtractionModule) and not is_soundon:
                    continue

                if isinstance(m, DefaultTableExtractionModule) and is_soundon:
                    continue

                mr = await m.run(job=job, page=page, plan=plan, tracker=tracker)
                if mr and getattr(mr, "artifacts", None):
                    artifacts.extend(mr.artifacts or [])
            except Exception as e:
                logger.bind(ctx=job.ctx).warning(
                    "module_failed name={} err={}",
                    getattr(m, "name", "unknown"),
                    str(e),
                )
        # valid for s3 uploader feature   
        # if when == "after_ready":
        #     finalizers = getattr(job, "_network_finalizers", None)

        #     if finalizers:
        #         for recorder, uploader in finalizers:
        #             try:
        #                 recorder.detach(page)
        #                 captures = await recorder.snapshot()

        #                 logger.bind(
        #                     ctx=job.ctx,
        #                     attempt=job.attempt_no,
        #                     policy=plan.policy_id,
        #                 ).info(
        #                     "network_capture_finalizing count={}",
        #                     len(captures),
        #                 )

        #                 for cap in captures:
        #                     await uploader.enqueue(cap)

        #                     artifacts.append(
        #                         Artifact(
        #                             kind=ArtifactKind.network_json,
        #                             label="network_call",
        #                             data={
        #                                 "endpoint": cap.url,
        #                                 "method": cap.method,
        #                                 "status": cap.status,
        #                                 "elapsed_ms": cap.elapsed_ms,
        #                                 "attempt": cap.attempt_no,
        #                                 "policy": cap.policy_id,
        #                                 "graphql_operation": cap.graphql_operation,
        #                             },
        #                         )
        #                     )
        #                 await uploader.shutdown()


        #             except Exception as e:
        #                 logger.bind(ctx=job.ctx).warning(
        #                     "network_finalize_failed err={}",
        #                     str(e),
        #                 )

        #         # cleanup to avoid retry contamination
        #         job._network_finalizers = []

        if when == "after_ready":
            recorders = getattr(job, "_network_recorders", None)

            if recorders:
                for recorder in recorders:
                    try:
                        recorder.detach(page)
                        captures = await recorder.snapshot()

                        logger.bind(
                            ctx=log_ctx(job),
                            attempt=job.attempt_no,
                            policy=plan.policy_id,
                        ).info(
                            "network_capture_finalizing count={}",
                            len(captures),
                        )

                        for cap in captures:
                            artifacts.append(
                                Artifact(
                                    kind=ArtifactKind.network_json
                                    if "application/json" in cap.content_type
                                    else ArtifactKind.binary,
                                    label="network_capture",
                                    source_url=cap.url,
                                    data={
                                        "endpoint": cap.url,
                                        "method": cap.method,
                                        "status": cap.status,
                                        "elapsed_ms": cap.elapsed_ms,
                                        "graphql_operation": cap.graphql_operation,
                                        "graphql_variables": cap.graphql_variables,
                                        "graphql_query": cap.graphql_query,
                                        "parsed_json": cap.parsed_json,
                                    },
                                    meta={
                                        "attempt": cap.attempt_no,
                                        "policy": cap.policy_id,
                                        "response_size": cap.response_size,
                                    },
                                    blob=cap.body,
                                )
                            )

                    except Exception as e:
                        # it will come only when clean up will happen and inside playwright something got crack
                        logger.bind(ctx=job.ctx).debug(
                            "network_finalize_failed err={}",
                            str(e),
                        )

                # cleanup to avoid retry contamination
                job._network_recorders = []


    async def _extract_rendered_fast(self, page: Page, plan) -> dict[str, Any]:
        tasks = set(plan.extraction.tasks or [])
        params = {
            "wantMain": "main_content" in tasks,
            "wantMeta": "metadata" in tasks,
            "wantLinks": "links" in tasks,
            "linksLimit": int(plan.extraction.links_limit or 200),
        }

        js = """
        (p) => {
        const out = {};
        const wantMain = !!p.wantMain;
        const wantMeta = !!p.wantMeta;
        const wantLinks = !!p.wantLinks;
        const linksLimit = Number(p.linksLimit || 200);

        if (wantMeta) {
            const title = document.title || "";
            const md = document.querySelector('meta[name="description"]');
            const desc = md ? (md.getAttribute("content") || "") : "";
            const canonEl = document.querySelector('link[rel="canonical"]');
            const canon = canonEl ? (canonEl.getAttribute("href") || "") : "";
            out.metadata = { title, description: desc, canonical: canon };
        }

        if (wantMain) {
            const t = document.body ? (document.body.innerText || "") : "";
            out.main_content = t;
        }

        if (wantLinks) {
            const as = Array.from(document.querySelectorAll("a[href]"));
            const links = [];
            for (const a of as) {
            if (links.length >= linksLimit) break;
            const href = a.getAttribute("href") || "";
            const text = (a.innerText || "").trim();
            links.push({ href, text });
            }
            out.links = links;
        }

        return out;
        }
        """
        raw = await page.evaluate(js, params)
        return raw if isinstance(raw, dict) else {}
    
    
    async def execute(self, job: JobEnvelope, scheduler_state: SchedulerState) -> Dict[str, Any]:
        base_plan = job.plan

        await self.pool.start()
        await self.pool.maybe_recycle()

        logger.info(
            "scrape_job_start job_id={} request_id={} domain={} url={} policy_id={}",
            job.job_id,
            job.request_id,
            base_plan.domain,
            str(base_plan.url),
            base_plan.policy_id,
        )

        raw_chain = self._policy_chain_for(base_plan)
        policy_chain_tried: list[str] = []
        last_failure: Optional[Dict[str, Any]] = None

        chain = list(raw_chain)

        for idx, pol in enumerate(chain):
            plan = self._make_policy_view(base_plan, pol)
            policy_chain_tried.append(pol)

            logger.info(
                "scrape_policy_try job_id={} domain={} policy_id={}",
                job.job_id,
                plan.domain,
                plan.policy_id,
            )

            r = await self._execute_with_plan(job, plan, scheduler_state)
            r["policy_chain_tried"] = policy_chain_tried
            r["final_policy"] = pol

            if r.get("ok"):
                return r

            last_failure = r

            et = (r.get("error") or {}).get("type", "")
            if et in ("http_404", "http_429"):
                return r

            if idx == 0 and raw_chain == ["default", "js-heavy", "stealth-lite"]:
                chain2 = self._select_next_policies(raw_chain, et)
                if chain2 != chain:
                    chain = [p for p in chain2 if p not in policy_chain_tried] + [
                        p for p in chain if p in policy_chain_tried
                    ]
                    for pol2 in [p for p in chain2 if p not in policy_chain_tried]:
                        plan2 = self._make_policy_view(base_plan, pol2)
                        policy_chain_tried.append(pol2)
                        rr = await self._execute_with_plan(job, plan2, scheduler_state)
                        rr["policy_chain_tried"] = policy_chain_tried
                        rr["final_policy"] = pol2
                        if rr.get("ok"):
                            return rr
                        last_failure = rr
                        et2 = (rr.get("error") or {}).get("type", "")
                        if et2 in ("http_404", "http_429"):
                            return rr
                    return last_failure or r

        return last_failure or {
            "ok": False,
            "status": "failed",
            "job_id": job.job_id,
            "request_id": job.request_id,
            "url": str(base_plan.url),
            "policy_id": base_plan.policy_id,
            "attempt": job.attempt_no,
            "mode": "unknown",
            "error": {"type": "unknown", "message": "failed"},
            "policy_chain_tried": policy_chain_tried,
            "final_policy": (policy_chain_tried[-1] if policy_chain_tried else base_plan.policy_id),
        }
    
    async def log_image_probe(self, page: Page, logger, job, label: str):
        try:
            stats = await page.evaluate("""
            () => {
            const imgs = Array.from(document.images);
            let withSrc = 0;
            let withData = 0;
            let visible = 0;

            for (const img of imgs) {
                const r = img.getBoundingClientRect();
                if (r.width >= 16 && r.height >= 16) visible++;
                if (img.getAttribute("src")) withSrc++;
                if (
                img.getAttribute("data-src") ||
                img.getAttribute("data-original") ||
                img.getAttribute("data-lazy-src")
                ) withData++;
            }

            return {
                total: imgs.length,
                with_src: withSrc,
                with_lazy_attr: withData,
                visible
            };
            }
            """)

            logger.bind(ctx=job.ctx).info(
                "image_probe stage={} total={} with_src={} lazy_attr={} visible={}",
                label,
                stats["total"],
                stats["with_src"],
                stats["with_lazy_attr"],
                stats["visible"],
            )
        except Exception as e:
            logger.bind(ctx=job.ctx).warning(
                "image_probe_failed stage={} err={}",
                label,
                str(e),
            )

    
    async def _execute_with_plan(self, job: JobEnvelope, plan, scheduler_state: SchedulerState) -> Dict[str, Any]:
        for attempt_no in range(1, plan.max_attempts + 1):
            job.attempt_no = attempt_no

            step_index = min(attempt_no - 1, len(plan.steps) - 1)
            step = plan.steps[step_index]

            # Calculate effective budget - add extra time when auth is enabled
            effective_max_total_ms = plan.budgets.max_total_ms
            browser_cfg = getattr(plan, "browser", None)
            auth_cfg = getattr(browser_cfg, "auth", None) if browser_cfg else None
            if auth_cfg and getattr(auth_cfg, "enabled", False):
                # Add 30 seconds for auth flow (login + navigation overhead)
                effective_max_total_ms += 30000
                logger.bind(ctx=log_ctx(job)).info(
                    "auth_budget_extended original_ms={} extended_ms={}",
                    plan.budgets.max_total_ms,
                    effective_max_total_ms,
                )

            tracker = BudgetTracker(
                max_total_ms=effective_max_total_ms,
                max_requests=plan.budgets.max_requests,
                max_chars=plan.budgets.max_chars,
            )

            ctx = None
            page: Page | None = None

            try:
                # http mode ( it's simple just request and got html )
                if step.mode == "http":
                    tracker.inc_request()
                    status, final_url, html = await fetch_html(
                        str(plan.url),
                        timeout_s=max(1.0, float(step.nav_timeout_ms) / 1000.0),
                        max_redirects=int(plan.budgets.max_redirects),
                        headers=(plan.context.extra_http_headers or None),
                    )


                    soft_block = detect_soft_block_html(html)
                    if soft_block:
                        scheduler_state.set_cooldown(plan.domain, self.config.cooldown_on_block_seconds, reason="blocked_signal_http")
                        return {
                            "ok": False,
                            "status": "blocked",
                            "job_id": job.job_id,
                            "request_id": job.request_id,
                            "url": str(plan.url),
                            "final_url": final_url,
                            "policy_id": plan.policy_id,
                            "attempt": attempt_no,
                            "mode": step.mode,
                            "error": {"type": "blocked_signal", "message": str(soft_block)},
                        }

                    if status in (401, 403):
                        scheduler_state.set_cooldown(plan.domain, self.config.cooldown_on_block_seconds, reason=f"http_{status}")
                        return {
                            "ok": False,
                            "status": "failed",
                            "job_id": job.job_id,
                            "request_id": job.request_id,
                            "url": str(plan.url),
                            "final_url": final_url,
                            "policy_id": plan.policy_id,
                            "attempt": attempt_no,
                            "mode": step.mode,
                            "error": {"type": "http_403", "message": f"http_{status}"},
                        }

                    if status == 429:
                        scheduler_state.set_cooldown(plan.domain, self.config.cooldown_on_429_seconds, reason="http_429")
                        return {
                            "ok": False,
                            "status": "failed",
                            "job_id": job.job_id,
                            "request_id": job.request_id,
                            "url": str(plan.url),
                            "final_url": final_url,
                            "policy_id": plan.policy_id,
                            "attempt": attempt_no,
                            "mode": step.mode,
                            "error": {"type": "http_429", "message": "http_429"},
                        }

                    if status == 404:
                        return {
                            "ok": False,
                            "status": "failed",
                            "job_id": job.job_id,
                            "request_id": job.request_id,
                            "url": str(plan.url),
                            "final_url": final_url,
                            "policy_id": plan.policy_id,
                            "attempt": attempt_no,
                            "mode": step.mode,
                            "error": {"type": "http_404", "message": "http_404"},
                        }

                    if 500 <= status <= 599:
                        raise RuntimeError(f"http_{status}")

                    tracker.check_time()

                    out: Dict[str, Any] = {
                        "ok": True,
                        "status": "succeeded",
                        "job_id": job.job_id,
                        "request_id": job.request_id,
                        "url": str(plan.url),
                        "final_url": final_url,
                        "policy_id": plan.policy_id,
                        "attempt": attempt_no,
                        "mode": step.mode,
                        "timings": {"elapsed_ms": tracker.elapsed_ms()},
                        "budgets_used": {"requests": tracker.requests},
                    }

                    extracted = extract_all(
                        html,
                        base_url=final_url,
                        tasks=plan.extraction.tasks,
                        scope_selectors=plan.extraction.link_scope_selectors,
                        links_limit=plan.extraction.links_limit,
                        allow_external_links=plan.extraction.allow_external_links,
                        dedupe_links=plan.extraction.dedupe_links,
                        keep_query_params=plan.extraction.keep_query_params,
                        content_selectors=plan.extraction.content_selectors,
                        exclude_selectors=plan.extraction.exclude_selectors,
                        normalize_cfg=plan.extraction.normalize.model_dump(),
                    )

                    if "images" in (plan.extraction.tasks or []):
                        try:
                            extracted["images"] = extract_images_http(
                                html,
                                base_url=final_url,
                                limit=int(getattr(plan.extraction, "images_limit", 200) or 200),
                                include_background_images=bool(getattr(plan.extraction, "include_background_images", False)),
                            )
                        except Exception:
                            extracted["images"] = []


                    if "main_content" in extracted:
                        main = tracker.clamp_text(extracted["main_content"])
                        extracted["main_content"] = main

                        try:
                            self._enforce_expectations_text(main, plan)
                        except Exception as _:
                            return {
                                "ok": False,
                                "status": "failed",
                                "job_id": job.job_id,
                                "request_id": job.request_id,
                                "url": str(plan.url),
                                "final_url": final_url,
                                "policy_id": plan.policy_id,
                                "attempt": attempt_no,
                                "mode": step.mode,
                                "error": {"type": "expectation_failed", "message": "http_content_insufficient"},
                            }

                    out.update(extracted)

                    await self.pool.mark_job_done()
                    return out

                # rendered mode
                ctx_kwargs: Dict[str, Any] = {}
                if plan.context.locale:
                    ctx_kwargs["locale"] = plan.context.locale
                if plan.context.timezone_id:
                    ctx_kwargs["timezone_id"] = plan.context.timezone_id
                if plan.context.user_agent:
                    ctx_kwargs["user_agent"] = plan.context.user_agent
                if plan.context.extra_http_headers:
                    ctx_kwargs["extra_http_headers"] = plan.context.extra_http_headers

                if getattr(plan, "storage_state_url", None) and not (auth_cfg and getattr(auth_cfg, "enabled", False)):
                    try:
                        storage_state = await self._get_storage_state_cached(plan.storage_state_url)
                        ctx_kwargs["storage_state"] = storage_state
                    except Exception as e:
                        raise RuntimeError(f"storage_state_load_failed:{str(e)}") from e
                elif auth_cfg and getattr(auth_cfg, "enabled", False):
                    logger.bind(ctx=log_ctx(job)).info(
                        "skipping_cached_storage_state auth_enabled=true fresh_login=true"
                    )
                    
                dl_cfg = getattr(getattr(plan, "browser", None), "downloads", None)
                if dl_cfg and getattr(dl_cfg, "enabled", False):
                    ctx_kwargs["accept_downloads"] = True

                ctx = await self.pool.new_context(context_kwargs=ctx_kwargs)
                page = await ctx.new_page()
                net_idle = AdaptiveNetworkIdle(quiet_ms=500)
                ua_cfg = self._get_stealth_ua(plan.domain)
                await page.add_init_script(f"""
                Object.defineProperty(navigator, 'platform', {{
                get: () => "{ua_cfg["platform"]}"
                }});
                Object.defineProperty(navigator, 'webdriver', {{
                get: () => undefined
                }});
                """)

                net_idle.attach(page)
                await install_dom_stability_probe(page)

                await _apply_network_rules(
                    page,
                    tracker=tracker,
                    block_types=set(plan.network.block_resource_types),
                    allow_hosts=set(plan.network.allow_hosts) if plan.network.allow_hosts else None,
                    block_hosts=set(plan.network.block_hosts) if plan.network.block_hosts else None,
                )

                if auth_cfg and getattr(auth_cfg, "enabled", False):
                    logger.bind(ctx=log_ctx(job)).info("auth_flow_starting")
                    auth_module = AuthModule()
                    await auth_module.run(job=job, page=page, plan=plan, tracker=tracker)
                    logger.bind(ctx=log_ctx(job)).info(
                        "auth_flow_completed current_url={} target_url={}",
                        page.url,
                        str(plan.url),
                    )

                artifacts: list[Any] = []
                await self._run_modules(
                    job=job,
                    page=page,
                    plan=plan,
                    tracker=tracker,
                    when="before_goto",
                    artifacts=artifacts,
                )

                dl_cfg = getattr(getattr(plan, "browser", None), "downloads", None)
                downloads_enabled = bool(dl_cfg and getattr(dl_cfg, "enabled", False))
                dl_timeout_ms = int(getattr(dl_cfg, "timeout_ms", 15_000) or 15_000)
                dl_max_bytes = int(getattr(dl_cfg, "max_bytes", 20_000_000) or 20_000_000)

                resp = None
                status = None
                goto_err: Exception | None = None
                download_obj = None

                current_url = page.url
                target_url = str(plan.url)
                url_path_match = current_url.split('?')[0] == target_url.split('?')[0]
                
                
                if url_path_match:
                    logger.bind(ctx=log_ctx(job)).info(
                        "already_at_target_url current_url={} target_url={} skipping_navigation",
                        current_url,
                        target_url,
                    )
                    resp = None
                    status = 200
                    
                    if auth_cfg and getattr(auth_cfg, "enabled", False):
                        await dismiss_post_login_popups(page, job)
                else:
                    if auth_cfg and getattr(auth_cfg, "enabled", False):
                        logger.bind(ctx=log_ctx(job)).info(
                            "attempting_client_side_navigation url={}",
                            target_url,
                        )
                        try:
                            # Use history.pushState to change URL without page reload
                            # this is where real magic happen if site clears the cookie automatically 
                            # we will do this use evaluate method and do whatever i want.
                            await page.evaluate(
                                "(url) => { window.history.pushState({}, '', url); }",
                                target_url,
                            )
                            # Trigger a popstate event so SPA routers detect the change
                            await page.evaluate("() => { window.dispatchEvent(new PopStateEvent('popstate')); }")
                            # Wait for SPA to load content
                            await asyncio.sleep(2)
                            
                            logger.bind(ctx=log_ctx(job)).info(
                                "client_side_navigation_complete final_url={}",
                                page.url,
                            )
                            
                            # Dismiss cookie banner after navigation
                            await dismiss_post_login_popups(page, job)
                            
                            resp = None
                            status = 200
                        except Exception as e:
                            logger.bind(ctx=log_ctx(job)).warning(
                                "client_side_navigation_failed err={} falling_back_to_goto",
                                str(e),
                            )
                            # Fall back to regular navigation
                            resp = await page.goto(target_url, timeout=step.nav_timeout_ms, wait_until="domcontentloaded")
                            status = resp.status if resp else None
                            
                            # Dismiss cookie banner after fallback navigation
                            await dismiss_post_login_popups(page, job)
                    else:
                        logger.bind(ctx=log_ctx(job)).info(
                            "navigating_to_target_url url={} wait_until=domcontentloaded timeout_ms={}",
                            target_url,
                            step.nav_timeout_ms,
                        )
                        
                        if downloads_enabled:
                            # If the URL itself starts a download, goto will throw "Download is starting".
                            # expect_download will still capture it.
                            async with page.expect_download(timeout=dl_timeout_ms) as di:
                                try:
                                    resp = await page.goto(target_url, timeout=step.nav_timeout_ms, wait_until="domcontentloaded")
                                except Exception as e:
                                    goto_err = e
                            try:
                                download_obj = await di.value
                            except Exception:
                                download_obj = None
                        else:
                            resp = await page.goto(target_url, timeout=step.nav_timeout_ms, wait_until="domcontentloaded")
                        
                        logger.bind(ctx=log_ctx(job)).info(
                            "target_url_navigation_complete status={} final_url={}",
                            resp.status if resp else None,
                            page.url,
                        )
                        
                        # Dismiss cookie banner after navigation (non-auth flows may also have cookies)
                        await dismiss_post_login_popups(page, job)

                status = resp.status if resp else None

                # If navigation "failed" because it was actually a download, treat that as success path.
                if goto_err and download_obj and "Download is starting" in str(goto_err):

                    dm = DownloadsModule()
                    raw, suggested, mime, truncated = await dm._save_download_to_bytes(download_obj, max_bytes=dl_max_bytes)

                    art = await upload_bytes_public_s3(
                        job=job,
                        data=raw,
                        filename=f"download_1_{suggested}",
                        mime=mime,
                        kind=ArtifactKind.download,
                        label="download",
                        selector=None,
                        meta={
                            "suggested_filename": suggested,
                            "truncated": truncated,
                            "download_url": getattr(download_obj, "url", None),
                            "trigger": "goto",
                        },
                    )

                    # Return success immediately (this job is “download-only”)
                    out: Dict[str, Any] = {
                        "ok": True,
                        "status": "succeeded",
                        "job_id": job.job_id,
                        "request_id": job.request_id,
                        "url": str(plan.url),
                        "final_url": page.url,
                        "policy_id": plan.policy_id,
                        "attempt": attempt_no,
                        "mode": step.mode,
                        "timings": {"elapsed_ms": tracker.elapsed_ms()},
                        "budgets_used": {"requests": tracker.requests},
                        "artifacts": [art.model_dump(mode="json", exclude_none=True)],
                    }
                    await self.pool.mark_job_done()
                    return out

                # If goto_err happened but it wasn't a download case, re-raise so your retry logic works
                if goto_err:
                    raise goto_err


                if status in (401, 403):
                    scheduler_state.set_cooldown(plan.domain, self.config.cooldown_on_block_seconds, reason=f"http_{status}")
                    return {
                        "ok": False,
                        "status": "failed",
                        "job_id": job.job_id,
                        "request_id": job.request_id,
                        "url": str(plan.url),
                        "final_url": page.url,
                        "policy_id": plan.policy_id,
                        "attempt": attempt_no,
                        "mode": step.mode,
                        "error": {"type": "http_403", "message": f"http_{status}"},
                    }

                if status == 429:
                    scheduler_state.set_cooldown(plan.domain, self.config.cooldown_on_429_seconds, reason="http_429")
                    return {
                        "ok": False,
                        "status": "failed",
                        "job_id": job.job_id,
                        "request_id": job.request_id,
                        "url": str(plan.url),
                        "final_url": page.url,
                        "policy_id": plan.policy_id,
                        "attempt": attempt_no,
                        "mode": step.mode,
                        "error": {"type": "http_429", "message": "http_429"},
                    }

                if status == 404:
                    return {
                        "ok": False,
                        "status": "failed",
                        "job_id": job.job_id,
                        "request_id": job.request_id,
                        "url": str(plan.url),
                        "final_url": page.url,
                        "policy_id": plan.policy_id,
                        "attempt": attempt_no,
                        "mode": step.mode,
                        "error": {"type": "http_404", "message": "http_404"},
                    }

                if status and 500 <= status <= 599:
                    raise RuntimeError(f"http_{status}")

                if step.ready == "networkidle":
                    logger.bind(ctx=log_ctx(job)).info(
                        "waiting_for_network_idle timeout_ms={} selectors_any={} selectors_all={}",
                        step.nav_timeout_ms,
                        step.selectors_any,
                        step.selectors_all,
                    )
                    try:
                        await wait_for_adaptive_idle(
                            page=page,
                            tracker=tracker,
                            timeout_ms=step.nav_timeout_ms,
                            net_idle=net_idle,
                            selectors_any=step.selectors_any,
                            selectors_all=step.selectors_all,
                            quiet_ms=500,
                        )
                    except TimeoutError as e:
                        logger.bind(ctx=log_ctx(job)).warning(
                            "network_idle_timeout_continuing err={}",
                            str(e),
                        )

                elif step.ready == "selector":
                    if step.selectors_all:
                        for sel in step.selectors_all:
                            loc = _locator_for(page, sel).first
                            await loc.wait_for(state="attached", timeout=step.nav_timeout_ms)
                    elif step.selectors_any:
                        slice_ms = max(600, step.nav_timeout_ms // max(1, len(step.selectors_any)))
                        last_err = None
                        for sel in step.selectors_any:
                            try:
                                loc = _locator_for(page, sel).first
                                await loc.wait_for(state="attached", timeout=slice_ms)
                                last_err = None
                                break
                            except Exception as e:
                                last_err = e
                        if last_err:
                            raise last_err

                # Detect login/auth redirects — return auth_expired instead of
                # scraping the login page and returning ok=true with garbage.
                has_storage_state = bool(getattr(plan, "storage_state_url", None))
                if has_storage_state:
                    current_path = urlparse(page.url).path.lower()
                    target_path = urlparse(str(plan.url)).path.lower()
                    _login_segments = (
                        "/login", "/signin", "/sign-in", "/sign_in",
                        "/auth", "/sso", "/oauth", "/cas/login",
                    )
                    redirected_to_login = (
                        current_path != target_path
                        and any(seg in current_path for seg in _login_segments)
                    )
                    if redirected_to_login:
                        logger.bind(ctx=log_ctx(job)).warning(
                            "auth_redirect_detected target={} redirected_to={} "
                            "storage_state_likely_expired",
                            str(plan.url),
                            page.url,
                        )
                        return {
                            "ok": False,
                            "status": "auth_expired",
                            "job_id": job.job_id,
                            "request_id": job.request_id,
                            "url": str(plan.url),
                            "final_url": page.url,
                            "policy_id": plan.policy_id,
                            "attempt": attempt_no,
                            "mode": step.mode,
                            "error": {
                                "type": "auth_expired",
                                "message": (
                                    "Page redirected to login. The storage state "
                                    "(session cookies) may have expired. Please "
                                    "re-login via /login to get a fresh session."
                                ),
                            },
                        }

                tracker.check_time()
                await self._enforce_expectations_selectors(page, plan)

                title = (await page.title())[:_MAX_TITLE]
                final_url = page.url

                body_head = await self._bounded_body_text(page, max_chars=_MAX_BODY_SCAN_CHARS)
                snippet = (body_head or "").strip().replace("\x00", "")[: plan.budgets.max_snippet_chars]
                content_hash = _sha256_text(body_head or "")
                content_len = len(body_head or "")

                block_signal = _detect_blocked(title + "\n" + (body_head or ""), plan.expectations.block_signals_hint)
                if block_signal:
                    scheduler_state.set_cooldown(plan.domain, self.config.cooldown_on_block_seconds, reason="blocked_signal")
                    return {
                        "ok": False,
                        "status": "blocked",
                        "job_id": job.job_id,
                        "request_id": job.request_id,
                        "url": str(plan.url),
                        "final_url": final_url,
                        "policy_id": plan.policy_id,
                        "attempt": attempt_no,
                        "mode": step.mode,
                        "error": {"type": "blocked_signal", "message": str(block_signal)},
                        "diagnostics": {
                            "title": title,
                            "snippet": snippet,
                            "content_len": content_len,
                            "content_hash": content_hash,
                            "requests_used": tracker.requests,
                            "elapsed_ms": tracker.elapsed_ms(),
                        },
                    }

                if plan.pagination.mode != "none":
                    paged = await self._run_pagination(page,job, plan, tracker, step)
                    await self.pool.mark_job_done()
                    return paged

                # run post-ready modules BEFORE we snapshot HTML and extract
                await self._run_modules(
                    job=job,
                    page=page,
                    plan=plan,
                    tracker=tracker,
                    when="after_ready",
                    artifacts=artifacts,
                )

                tracker.check_time()

                out: Dict[str, Any] = {
                    "ok": True,
                    "status": "succeeded",
                    "job_id": job.job_id,
                    "request_id": job.request_id,
                    "url": str(plan.url),
                    "final_url": final_url,
                    "policy_id": plan.policy_id,
                    "attempt": attempt_no,
                    "mode": step.mode,
                    "timings": {"elapsed_ms": tracker.elapsed_ms()},
                    "budgets_used": {"requests": tracker.requests},
                }

                tasks = set(plan.extraction.tasks or [])
                
                safe_fast = (
                    not (plan.extraction.content_selectors or [])
                    and not (plan.extraction.exclude_selectors or [])
                    and not (plan.extraction.link_scope_selectors or [])
                    and ("main_content" in tasks or "metadata" in tasks)
                )

                if safe_fast:
                    extracted = await self._extract_rendered_fast(page, plan)
                else:
                    html = await page.content()
                    extracted = extract_all(
                        html,
                        base_url=final_url,
                        tasks=plan.extraction.tasks,
                        scope_selectors=plan.extraction.link_scope_selectors,
                        links_limit=plan.extraction.links_limit,
                        allow_external_links=plan.extraction.allow_external_links,
                        dedupe_links=plan.extraction.dedupe_links,
                        keep_query_params=plan.extraction.keep_query_params,
                        content_selectors=plan.extraction.content_selectors,
                        exclude_selectors=plan.extraction.exclude_selectors,
                        normalize_cfg=plan.extraction.normalize.model_dump(),
                    )
                
                if "main_content" in extracted:
                    main = tracker.clamp_text(extracted["main_content"])
                    extracted["main_content"] = main
                    self._enforce_expectations_text(main, plan)

                out.update(extracted)

                if "images" in tasks:
                    await self.log_image_probe(page, logger, job, "before_lazy_trigger")

                    try:
                        await page.evaluate("""
                        () => {
                        for (const img of document.images) {
                            img.loading = "eager";
                        }
                        window.scrollBy(0, window.innerHeight * 2);
                        }
                        """)
                        await asyncio.sleep(0.4)
                    except Exception:
                        pass

                    await self.log_image_probe(page, logger, job, "after_lazy_trigger")

                    try:
                        out["images"] = await self._extract_images_rendered(page, plan)
                        logger.bind(ctx=job.ctx).info(
                            "image_extract_result count={}",
                            len(out["images"]),
                        )
                    except Exception as e:
                        logger.bind(ctx=job.ctx).warning(
                            "image_extract_failed err={}",
                            str(e),
                        )
                        out["images"] = []


                xpaths = getattr(plan.extraction, "xpath_queries", None) or getattr(plan.extraction, "xpaths", None)
                if xpaths:
                    try:
                        out["xpath"] = await self._extract_xpath_rendered(page, xpaths)
                    except Exception:
                        out["xpath"] = {}

                # Refresh storage state after successful scrape so that
                # rotated tokens (e.g. TikTok msToken) stay fresh for
                # subsequent requests using this session.
                if has_storage_state and page and ctx:
                    try:
                        refreshed_state = await ctx.storage_state()
                        
                        refreshed_state = await ctx.storage_state()
                        refreshed_bytes = json.dumps(refreshed_state).encode("utf-8")
                        refreshed_art = await upload_bytes_public_s3(
                            job=job,
                            data=refreshed_bytes,
                            filename="storage_state.json",
                            mime="application/json",
                            kind=ArtifactKind.storage_state,
                            label="storage_state_refreshed",
                            meta={"source": "post_scrape_refresh"},
                        )
                        out["storage_state_url"] = refreshed_art.s3_url
                        logger.bind(ctx=log_ctx(job)).info(
                            "storage_state_refreshed url={}",
                            refreshed_art.s3_url,
                        )
                    except Exception as e:
                        logger.bind(ctx=log_ctx(job)).warning(
                            "storage_state_refresh_failed err={}", str(e),
                        )
                        out["storage_state_url"] = getattr(plan, "storage_state_url", None)
                elif getattr(plan, "storage_state_url", None):
                    out["storage_state_url"] = getattr(plan, "storage_state_url", None)


                if artifacts:
                    # normalize to JSON-friendly dicts
                    norm_artifacts: list[dict] = []
                    for a in artifacts:
                        try:
                            if isinstance(a, BaseModel):
                                norm_artifacts.append(a.model_dump(mode="json", exclude_none=True))
                            elif isinstance(a, dict):
                                norm_artifacts.append(a)
                            else:
                                norm_artifacts.append({"type": "unknown", "value": str(a)})
                        except Exception:
                            norm_artifacts.append({"type": "unknown", "value": str(a)})
                    out["artifacts"] = norm_artifacts

                await self.pool.mark_job_done()
                return out

            except Exception as e:
                msg = str(e)
                code, retryable = classify_exception(msg)
                backoff = _deterministic_backoff_s(attempt_no)

                logger.warning(
                    "scrape_job_error job_id={} domain={} attempt={} mode={} policy_id={} retryable={} type={} err={}",
                    job.job_id,
                    plan.domain,
                    attempt_no,
                    step.mode,
                    plan.policy_id,
                    retryable,
                    code,
                    msg[:100],
                )

                if not retryable or attempt_no >= plan.max_attempts:

                    return {
                        "ok": False,
                        "status": "failed",
                        "job_id": job.job_id,
                        "request_id": job.request_id,
                        "url": str(plan.url),
                        "policy_id": plan.policy_id,
                        "attempt": attempt_no,
                        "mode": step.mode,
                        "error": {
                            "code": code,
                            "message": msg,
                            "retryable": retryable,
                        },
                    }

                await asyncio.sleep(backoff)

            finally:
                try:
                    if page:
                        await page.close()
                except Exception:
                    pass
                try:
                    if ctx:
                        await ctx.close()
                except Exception:
                    pass
                if ctx:
                    try:
                        await self.pool.context_closed()
                    except Exception:
                        pass

        return {
            "ok": False,
            "status": "failed",
            "job_id": job.job_id,
            "request_id": job.request_id,
            "url": str(plan.url),
            "policy_id": plan.policy_id,
            "attempt": job.attempt_no,
            "mode": "unknown",
            "error": {"type": "unknown", "message": "failed"},
        }

    def _enforce_expectations_text(self, main: str, plan) -> None:
        if plan.expectations.allow_empty:
            return

        min_len = plan.expectations.min_text_length_hint or 0
        if min_len and len(main or "") < min_len:
            raise RuntimeError(f"expectation_failed_min_text_len:{min_len}")

        hints = plan.expectations.contains_any_text_hint or []
        if hints:
            hay = (main or "").lower()
            ok = any(h.lower() in hay for h in hints if h)
            if not ok:
                raise RuntimeError("expectation_failed_contains_any_text")

    async def _enforce_expectations_selectors(self, page: Page, plan) -> None:
        sels = plan.expectations.contains_any_selectors_hint or []
        if not sels:
            return
        for sel in sels:
            try:
                loc = _locator_for(page, sel)
                if await loc.count() > 0:
                    return
            except Exception:
                continue
        raise RuntimeError("expectation_failed_contains_any_selectors")

    async def _run_pagination(self, page: Page,job:JobEnvelope, plan, tracker: BudgetTracker, step) -> Dict[str, Any]:
        max_pages = max(1, min(int(plan.pagination.max_pages or 1), 20))
        pages: list[dict] = []
        merged_links: list[dict] = []
        seen_links = set()

        for page_no in range(1, max_pages + 1):
            tracker.check_time()

            if plan.pagination.mode == "page_param" and plan.pagination.page_param_name:
                if page_no > 1:
                    url2 = _url_with_page_param(str(plan.url), plan.pagination.page_param_name, page_no)
                    resp = await page.goto(url2, timeout=step.nav_timeout_ms, wait_until="domcontentloaded")
                    status = resp.status if resp else None
                    if status in (401, 403, 404, 429) or (status and 500 <= status <= 599):
                        break

            html = await page.content()
            page_result: dict[str, Any] = {"page_no": page_no, "url": page.url}

            extracted = extract_all(
                html,
                base_url=page.url,
                tasks=plan.extraction.tasks,
                scope_selectors=plan.extraction.link_scope_selectors,
                links_limit=plan.extraction.links_limit,
                allow_external_links=plan.extraction.allow_external_links,
                dedupe_links=plan.extraction.dedupe_links,
                keep_query_params=plan.extraction.keep_query_params,
                content_selectors=plan.extraction.content_selectors,
                exclude_selectors=plan.extraction.exclude_selectors,
                normalize_cfg=plan.extraction.normalize.model_dump(),
            )

            if "main_content" in extracted:
                extracted["main_content"] = tracker.clamp_text(extracted["main_content"])

            page_result.update(extracted)

            if "links" in extracted:
                for link in extracted["links"] or []:
                    k = link.get("href", "")
                    if not k:
                        continue
                    if k in seen_links:
                        continue
                    seen_links.add(k)
                    merged_links.append(link)

            pages.append(page_result)

            if plan.pagination.mode == "next_button":
                clicked = False
                for sel in plan.pagination.next_selectors or []:
                    try:
                        loc = _locator_for(page, sel).first
                        if await loc.count() > 0:
                            await loc.click()
                            await page.wait_for_load_state("domcontentloaded", timeout=step.nav_timeout_ms)
                            clicked = True
                            break
                    except Exception:
                        continue
                if not clicked:
                    break

            if plan.pagination.stop_when_hint == "same_content" and len(pages) >= 2:
                prev = pages[-2].get("main_content", "") or ""
                cur = pages[-1].get("main_content", "") or ""
                if prev and cur and prev[:2000] == cur[:2000]:
                    break

        out: Dict[str, Any] = {
            "ok": True,
            "status": "succeeded",
            "job_id": job.job_id,
            "request_id": job.request_id,
            "url": str(plan.url),
            "final_url": page.url,
            "policy_id": plan.policy_id,
            "attempt": job.attempt_no,
            "mode": "rendered",
            "timings": {"elapsed_ms": tracker.elapsed_ms()},
            "budgets_used": {"requests": tracker.requests},
            "pages": pages,
        }

        if "links" in plan.extraction.tasks:
            out["links_merged"] = merged_links

        return out