from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware

from src.autobots.api.v1.api_hello import api_hello
from src.autobots.api.v1_ingestion.v1_ingestion import router as v1_ingestion_router
from src.autobots.core.lifespan_ingestion import lifespan_ingestion
from src.autobots.core.settings import SettingsProvider
from src.autobots.middleware.middleware_add_context import add_context_dispatch

# Initialize the FastAPI application for the worker service
app = FastAPI(
    title="Autobots Ingestion & RAG Service",
    description="A standalone service for background data ingestion and real-time RAG queries.",
    version="0.1.0",
    lifespan=lifespan_ingestion,
)

# Add the middleware that injects the Context object into request.state.context
app.add_middleware(BaseHTTPMiddleware, dispatch=add_context_dispatch)

app.include_router(api_hello.router, prefix=SettingsProvider.sget().API_v1)

app.include_router(v1_ingestion_router, prefix="/api")


@app.get("/health", tags=["Health"])
async def health_check():
    """A simple health check endpoint."""
    return {"status": "ok", "service": "ingestion-worker"}
