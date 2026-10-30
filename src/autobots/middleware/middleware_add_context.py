from src.autobots.data_model.app_state import AppState
from fastapi import Request
from loguru import logger
from starlette.responses import JSONResponse

from src.autobots.data_model.context import Context


async def add_context_dispatch(request: Request, call_next):
    ctx = Context()
    app_state:AppState = AppState()
    try:
        request.state.context = ctx
        request.state.app_state = app_state
        response = await call_next(request)
        return response
    except Exception as e:
        logger.bind(ctx=ctx).exception(str(e))
        return JSONResponse(status_code=500, content={"detail": str(e)})
