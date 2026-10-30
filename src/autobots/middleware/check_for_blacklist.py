from fastapi import HTTPException, Request
from loguru import logger
from starlette.responses import JSONResponse

from src.autobots.auth.blacklist import Blacklist


async def check_for_blacklist(request: Request, call_next):
    ctx = request.state.context
    try:
        await Blacklist.assert_auth_checks(request=request)
        response = await call_next(request)
        return response
    except Exception as e:
        if isinstance(e, HTTPException):
            logger.bind(ctx=ctx).exception(str(e))
            return JSONResponse(status_code=e.status_code, content=f"{e.status_code} - {e.detail}")
        else:
            logger.bind(ctx=ctx).exception(str(e))
            return JSONResponse(status_code=500, content={"detail": str(e)})
