# Context (Ctx)
Ctx is user for observability. It provides a trace_id and user_id to logs. This when used to filter logs, gives us all logs for a request.

Ctx is added to every request coming in through middleware.

Ctx can be got from http request method you have defined by:
```python
from fastapi import APIRouter, Request
from loguru import logger

from src.autobots import SettingsProvider

router = APIRouter(prefix=SettingsProvider.sget().API_Hello, tags=[SettingsProvider.sget().API_Hello])


@router.get("/")
async def hello(request: Request):
    ctx = request.state.context  # NOTE: ctx is present in state field of request
    logger.bind(ctx=ctx).info("Hello request")  # Note: ctx is bound to log
    return {"hello": "world"}
```

### How to use it:
You will receive Ctx in every request. After ctx should be passed as arg to every method. 
Make ctx should be first arg of every method. And every log should have a ctx bound to it.