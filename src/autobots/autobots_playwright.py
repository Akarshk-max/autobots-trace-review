import asyncio
import sys

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import os

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.autobots import SettingsProvider
from src.autobots.api.ui import ui
from src.autobots.api.v1_playwright import v1_playwright
from src.autobots.core.fastapi_desc_playwright import FastAPIDescPlaywright
from src.autobots.core.lifespan_playwright import lifespan_playwright
from src.autobots.middleware.api_usage_dispatch import usage_info_dispatch
from src.autobots.middleware.check_for_blacklist import check_for_blacklist
from src.autobots.middleware.dispatch_catch_all_exception import catch_all_exception_dispatch
from src.autobots.middleware.middleware_add_context import add_context_dispatch

app = FastAPI(lifespan=lifespan_playwright, **FastAPIDescPlaywright().model_dump())

app.mount("/static", StaticFiles(directory="src/autobots/ui/static"), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(BaseHTTPMiddleware, dispatch=usage_info_dispatch)
app.add_middleware(BaseHTTPMiddleware, dispatch=catch_all_exception_dispatch)
app.add_middleware(BaseHTTPMiddleware, dispatch=check_for_blacklist)
app.add_middleware(BaseHTTPMiddleware, dispatch=add_context_dispatch)

app.include_router(v1_playwright.playwight_router)
app.include_router(router=ui.router)


def _is_compiled_runtime() -> bool:
    # Nuitka sets __compiled__ in compiled modules.
    return bool(getattr(sys, "frozen", False) or "__compiled__" in globals())


if __name__ == "__main__":
    SettingsProvider.set()
    is_compiled = _is_compiled_runtime()
    app_target = app if is_compiled else "autobots_playwright:app"

    uvicorn.run(
        app_target,
        host="0.0.0.0",
        port=8010,
        reload=False,  # enable later after it's stable
        workers=1,  # must be 1 for in-process Playwright pool only on window on linux any number of worker could be there
        log_level="debug",
        loop="asyncio",  # key on Windows (avoid Selector loop)
    )
