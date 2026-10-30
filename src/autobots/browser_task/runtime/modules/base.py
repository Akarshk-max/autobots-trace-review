from typing import List, Protocol

from playwright.async_api import Page
from pydantic import BaseModel, Field

from src.autobots.browser_task.inmem.state import JobEnvelope
from src.autobots.browser_task.runtime.artifacts import Artifact
from src.autobots.browser_task.runtime.helpers import BudgetTracker


class ModuleResult(BaseModel):
    artifacts: List[Artifact] = Field(default_factory=list)


class BrowserModule(Protocol):
    name: str

    async def run(
        self,
        *,
        job: JobEnvelope,
        page: Page,
        plan: object,
        tracker: BudgetTracker,
    ) -> ModuleResult: ...