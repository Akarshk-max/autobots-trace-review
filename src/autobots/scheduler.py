import sys

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware

from src.autobots import SettingsProvider
from src.autobots.api.ui import ui
from src.autobots.api.v1_scheduler import v1_scheduler
from src.autobots.core.fastapi_desc_scheduler import FastAPIDescScheduler
from src.autobots.core.lifespan_scheduler import lifespan_scheduler
from src.autobots.core.tracing import enable_ddtrace_web_tracing
from src.autobots.middleware.api_usage_dispatch import usage_info_dispatch
from src.autobots.middleware.dispatch_catch_all_exception import catch_all_exception_dispatch
from src.autobots.middleware.middleware_add_context import add_context_dispatch

enable_ddtrace_web_tracing()
app = FastAPI(lifespan=lifespan_scheduler, **FastAPIDescScheduler().model_dump())

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
app.add_middleware(BaseHTTPMiddleware, dispatch=add_context_dispatch)

app.include_router(v1_scheduler.scheduler_router)
# app.include_router(router=v1.router_docs)
app.include_router(router=ui.router)


def _is_compiled_runtime() -> bool:
    # Nuitka sets __compiled__ in compiled modules.
    return bool(getattr(sys, "frozen", False) or "__compiled__" in globals())


if __name__ == "__main__":
    SettingsProvider.set()
    settings = SettingsProvider.sget()
    is_compiled = _is_compiled_runtime()
    app_target = app if is_compiled else "scheduler:app"
    # settings.APP = "scheduler"
    # Run server
    uvicorn.run(
        app_target,
        host="0.0.0.0",
        port=8001,
        reload=not is_compiled,
        log_level="debug",
        workers=1 if is_compiled else 1,
    )
