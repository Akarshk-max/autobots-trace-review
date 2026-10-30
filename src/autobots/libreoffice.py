import os
import sys

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.autobots import SettingsProvider
from src.autobots.api.ui import ui
from src.autobots.api.v1_libreoffice import v1_libreoffice
from src.autobots.core.fastapi_desc_libreoffice import FastAPIDescLibreOffice
from src.autobots.core.lifespan_libreoffice import lifespan_libreoffice
from src.autobots.middleware.api_usage_dispatch import usage_info_dispatch
from src.autobots.middleware.check_for_blacklist import check_for_blacklist
from src.autobots.middleware.dispatch_catch_all_exception import catch_all_exception_dispatch
from src.autobots.middleware.middleware_add_context import add_context_dispatch

# patch(fastapi=True)
app = FastAPI(lifespan=lifespan_libreoffice, **FastAPIDescLibreOffice().model_dump())

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

app.include_router(v1_libreoffice.libreoffice_router)
# app.include_router(router=v1.router_docs)
app.include_router(router=ui.router)


def _is_compiled_runtime() -> bool:
    # Nuitka sets __compiled__ in compiled modules.
    return bool(getattr(sys, "frozen", False) or "__compiled__" in globals())


if __name__ == "__main__":
    SettingsProvider.set()
    settings = SettingsProvider.sget()
    is_compiled = _is_compiled_runtime()
    app_target = app if is_compiled else "libreoffice:app"
    # Run server
    uvicorn.run(
        app_target,
        host="0.0.0.0",
        port=8003,
        reload=not is_compiled,
        log_level="debug",
        workers=1 if is_compiled else 3,
    )
