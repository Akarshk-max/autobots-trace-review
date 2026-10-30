import json
from typing import Any

from loguru import logger
from playwright.async_api import Page
from pydantic import BaseModel

from src.autobots.browser_task.inmem.state import JobEnvelope
from src.autobots.browser_task.runtime.artifacts import ArtifactKind
from src.autobots.browser_task.runtime.modules.base import ModuleResult
from src.autobots.browser_task.runtime.modules.utils import log_ctx, upload_bytes_public_s3


class StorageStateModule(BaseModel):
    name: str = "storage_state"
    attach_before_goto: bool = False

    async def run(self, *, job: JobEnvelope, page: Page, plan: Any, tracker: Any) -> ModuleResult:
        logger.bind(ctx=log_ctx(job)).info("storage_state_module_evaluating")
        browser = getattr(plan, "browser", None)
        if not browser:
            logger.bind(ctx=log_ctx(job)).info("storage_state_module_skipping reason=no_browser")
            return ModuleResult(artifacts=[])

        auth_cfg = getattr(browser, "auth", None)
        if not auth_cfg or not getattr(auth_cfg, "enabled", False):
            logger.bind(ctx=log_ctx(job)).info("storage_state_module_skipping reason=auth_not_enabled")
            return ModuleResult(artifacts=[])

        # only extract if requested
        if not getattr(auth_cfg, "extract_storage_state", False):
            logger.bind(ctx=log_ctx(job)).info("storage_state_module_skipping reason=extract_not_requested")
            return ModuleResult(artifacts=[])

        logger.bind(ctx=log_ctx(job)).info("capturing_storage_state")

        try:
            state = await page.context.storage_state()
            state_bytes = json.dumps(state).encode("utf-8")
            
            art = await upload_bytes_public_s3(
                job=job,
                data=state_bytes,
                filename="storage_state.json",
                mime="application/json",
                kind=ArtifactKind.storage_state,
                label="storage_state",
            )
            
            logger.bind(ctx=log_ctx(job)).info("storage_state_captured url={}", art.s3_url)
            
            return ModuleResult(artifacts=[art])

        except Exception as e:
            logger.bind(ctx=log_ctx(job)).error("storage_state_extraction_failed err={}", str(e))
            return ModuleResult(artifacts=[])
