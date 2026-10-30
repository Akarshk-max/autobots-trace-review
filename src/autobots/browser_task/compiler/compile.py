from typing import List
from urllib.parse import urlparse

from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import HttpUrl

from src.autobots.browser_task.compiler.models import (
    ExecutionPlan,
    Expectations,
    ExtractionPlan,
    JobPlan,
    PaginationPlan,
)
from src.autobots.browser_task.intent.models import ScrapeIntentRequest
from src.autobots.browser_task.policies.base import PolicyRegistry
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM


def _domain_from_url(u: str) -> str:
    return (urlparse(u).hostname or "").lower()


def compile_intent_to_plan( ctx : Context, user: UserORM, db : AsyncIOMotorDatabase,*, request_id: str, job_ids: List[str], intent: ScrapeIntentRequest) -> ExecutionPlan:
    policy = PolicyRegistry.get(intent.policy_id)

    jobs: List[JobPlan] = []
    for idx, tgt in enumerate(intent.targets):
        domain = _domain_from_url(str(tgt.url))

        steps = policy.build_steps(intent)
        budgets = policy.budgets(intent)
        network = policy.network(intent)
        context = policy.context(intent)

        resolved_storage_state_url = (
            str(tgt.storage_state_url)
            if getattr(tgt, "storage_state_url", None)
            else (str(intent.storage_state_url) if intent.storage_state_url else None)
        )


        # Apply target-level header hints safely (e.g., Accept-Language)
        if tgt.headers_hint and tgt.headers_hint.accept_language:
            context.extra_http_headers = {**context.extra_http_headers, "Accept-Language": tgt.headers_hint.accept_language}
            # align locale if user provides something like "en-US" ( will improve further still trying to understand all these things in detail )
            if not context.locale:
                context.locale = tgt.headers_hint.accept_language.split(",")[0].strip()

        # clamp step timeouts to budgets.max_nav_ms
        for s in steps:
            s.nav_timeout_ms = min(int(s.nav_timeout_ms), int(budgets.max_nav_ms))

        tasks = [t.kind.value for t in intent.tasks]
        links_task = next((t for t in intent.tasks if t.kind.value == "links"), None)
        links_limit = int(links_task.limit) if (links_task and links_task.limit) else 200

        extraction = ExtractionPlan(
            tasks=tasks,
            content_selectors=intent.extraction.content.selectors_hint,
            exclude_selectors=intent.extraction.content.exclude_selectors_hint,
            link_scope_selectors=intent.extraction.links.scope_selectors_hint,
            links_limit=links_limit,
            allow_external_links=bool(intent.extraction.links.allow_external),
            dedupe_links=bool(intent.extraction.links.dedupe),
            keep_query_params=bool(intent.extraction.links.keep_query_params),
            image_selectors=intent.extraction.images.selectors_hint,
            image_scope_selectors=intent.extraction.images.scope_selectors_hint,
            images_limit=int(intent.extraction.images.images_limit or 200),
            include_background_images=bool(intent.extraction.images.include_background_images),

            normalize=intent.extraction.normalize.model_dump(),
        )

        expectations = Expectations(
            min_text_length_hint=intent.expectations.min_text_length_hint,
            contains_any_text_hint=intent.expectations.contains_any_text_hint,
            contains_any_selectors_hint=intent.expectations.contains_any_selectors_hint,
            block_signals_hint=intent.expectations.block_signals_hint,
            allow_empty=bool(intent.expectations.allow_empty),
        )

        p = intent.navigation.pagination
        pagination = PaginationPlan(
            mode=p.mode.value,
            max_pages=int(p.max_pages_hint or 1),
            next_selectors=p.next_selectors_hint,
            page_param_name=p.page_param_name,
            stop_when_hint=p.stop_when_hint,
        )

        b = intent.extraction.browser
        def _clamp_list(xs, n): return list((xs or [])[:n])

        browser_plan = {
            "screenshots": {
                "enabled": bool(b.screenshots.enabled),
                "selectors": _clamp_list(b.screenshots.selectors, 10),
                "full_page": bool(b.screenshots.full_page),
                "max_images": max(0, min(int(b.screenshots.max_images or 0), 20)),
            },
            "pdf": {
                "enabled": bool(b.pdf.enabled),
                "page": bool(b.pdf.page),
            },
            "downloads": {
                "enabled": bool(b.downloads.enabled),
                "click_selectors": _clamp_list(b.downloads.click_selectors, 20),
                "max_files": max(0, min(int(b.downloads.max_files or 0), 20)),
                "timeout_ms": max(1000, min(int(b.downloads.timeout_ms or 15000), 120000)),
                "max_bytes": max(0, min(int(b.downloads.max_bytes or 20_000_000), 200_000_000)),
            },
            "network": {
                "enabled": bool(b.network.enabled),
                "resource_types": _clamp_list(b.network.resource_types, 5),
                "url_contains": _clamp_list(b.network.url_contains, 20),
                "max_items": max(0, min(int(b.network.max_items or 0), 200)),
                "capture_bodies": bool(b.network.capture_bodies),
                "max_body_bytes": max(0, min(int(b.network.max_body_bytes or 0), 50_000_000)),
                "upload_large_bodies": bool(b.network.upload_large_bodies),
                "upload_always": bool(b.network.upload_always),
                "settle_ms": max(0, min(int(b.network.settle_ms or 0), 5000)),
            },
            "tables": {
                "enabled": bool(b.tables.enabled),
                "selectors": _clamp_list(b.tables.selectors, 10),
                "max_tables": max(0, min(int(b.tables.max_tables or 100), 500)),
                "max_rows": max(1, min(int(b.tables.max_rows or 200), 5000)),
                "max_scrolls": max(0, min(int(b.tables.max_scrolls or 20), 200)),
                "scroll_delta": max(50, min(int(b.tables.scroll_delta or 800), 5000)),
                "settle_ms": max(0, min(int(b.tables.settle_ms or 250), 2000)),
                "start_count": max(1, int(getattr(b.tables, "start_count", 1) or 1)),
                "end_count": max(1, int(getattr(b.tables, "end_count", getattr(b.tables, "start_count", 1)) or 1)),
            },
            "charts": {
                "enabled": bool(b.charts.enabled),
                "selectors": _clamp_list(b.charts.selectors, 10),
                "max_charts": max(0, min(int(b.charts.max_charts or 0), 30)),
                "screenshot_canvas": bool(b.charts.screenshot_canvas),
            },
            "auth": {
                "enabled": bool(b.auth.enabled),
                "login_url": str(b.auth.login_url) if b.auth.login_url else None,
                "credentials_url": str(b.auth.credentials_url) if b.auth.credentials_url else None,
                "username_selector": b.auth.username_selector,
                "password_selector": b.auth.password_selector,
                "submit_selector": b.auth.submit_selector,
                "success_indicator_selector": b.auth.success_indicator_selector,
                "timeout_ms": max(5000, min(int(b.auth.timeout_ms or 30000), 120000)),
                "extract_storage_state": bool(b.auth.extract_storage_state),
            },
        }


        jobs.append(
            JobPlan(
                job_id=str(job_ids[idx]),
                request_id=str(request_id),
                url=HttpUrl(str(tgt.url)),
                domain=domain,
                policy_id=policy.policy_id,
                storage_state_url=resolved_storage_state_url,
                steps=steps,
                budgets=budgets,
                network=network,
                context=context,
                extraction=extraction,
                expectations=expectations,
                pagination=pagination,
                browser=browser_plan,
                max_attempts=3,
            )
        )

    return ExecutionPlan(request_id=str(request_id), jobs=jobs)