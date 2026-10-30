import uvicorn
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from src.autobots.api.v1.api_hello import api_hello
from src.autobots.core.lifespan_runner import lifespan_runner
from src.autobots.core.settings import SettingsProvider
from src.autobots.core.tracing import enable_ddtrace_web_tracing
from src.autobots.middleware.middleware_add_context import add_context_dispatch

enable_ddtrace_web_tracing()

app = FastAPI(
    title="Autobots Runner",
    description="Temporal worker polling for action, action graph and action team tasks.",
    version="0.1.0",
    lifespan=lifespan_runner,
)

app.add_middleware(BaseHTTPMiddleware, dispatch=add_context_dispatch)

app.include_router(api_hello.router, prefix=SettingsProvider.sget().API_v1)


@app.get("/health", tags=["Health"])
async def health_check():
    """Unhealthy if the worker's poll loop died — the process would otherwise
    keep serving 200s with nothing consuming the task queue."""
    worker_task = getattr(app.state, "worker_task", None)
    polling = worker_task is not None and not worker_task.done()
    if polling:
        return {"status": "ok", "service": "autobots-runner", "polling": True}
    return JSONResponse(
        status_code=503,
        content={"status": "error", "service": "autobots-runner", "polling": False},
    )


if __name__ == "__main__":
    SettingsProvider.set()
    uvicorn.run(app, host="0.0.0.0", port=8005, workers=1)
