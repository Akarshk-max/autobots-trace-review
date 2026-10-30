import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from redis_fastapi import FastAPIRedis
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware

from src.autobots import SettingsProvider
from src.autobots.api.ui import ui
from src.autobots.api.v1 import v1
from src.autobots.core.fastapi_desc import FastAPIDesc
from src.autobots.core.lifespan import lifespan
from src.autobots.core.tracing import enable_ddtrace_web_tracing
from src.autobots.mcp_server import create_mcp_server
from src.autobots.middleware.api_usage_dispatch import usage_info_dispatch
from src.autobots.middleware.check_for_blacklist import check_for_blacklist
from src.autobots.middleware.dispatch_catch_all_exception import catch_all_exception_dispatch
from src.autobots.middleware.middleware_add_context import add_context_dispatch

enable_ddtrace_web_tracing()

SettingsProvider.set()

_mcp_server = create_mcp_server()


@asynccontextmanager
async def _combined_lifespan(app_instance: FastAPI):
    async with lifespan(app_instance):
        # Starlette does not propagate lifespan into mounted apps, so the MCP app's
        # own lifespan has to be run here — it is what starts FastMCP's
        # StreamableHTTPSessionManager.
        #
        # Failing loudly is deliberate. Yielding past a missing lifespan boots a
        # process that looks perfectly healthy and then fails *every* /v1/mcp
        # request with "Task group is not initialized" — a silent break that only
        # shows up in production traffic, long after startup looked fine.
        mcp_app = _mcp_server.app
        mcp_lifespan = getattr(mcp_app, "lifespan", None)
        if mcp_lifespan is None:
            raise RuntimeError(
                f"Mounted MCP app ({type(mcp_app).__name__}) exposes no lifespan; "
                "its session manager would never start and every /v1/mcp request would fail."
            )
        # Starlette lifespan callables take the app they belong to.
        async with mcp_lifespan(mcp_app):
            yield


app = FastAPI(lifespan=_combined_lifespan, **FastAPIDesc().model_dump())
FastAPIRedis(app).caching().rate_limiting()

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

app.include_router(v1.router)
app.include_router(router=ui.router)

app.mount("/v1/mcp", _mcp_server)


def _is_compiled_runtime() -> bool:
    return bool(getattr(sys, "frozen", False) or "__compiled__" in globals())


if __name__ == "__main__":
    settings = SettingsProvider.sget()
    is_compiled = _is_compiled_runtime()
    app_target = app if is_compiled else "main:app"
    uvicorn.run(
        app_target,
        host="0.0.0.0",
        port=8000,
        reload=not is_compiled,
        log_level="debug",
        workers=1 if is_compiled else 3,
    )
