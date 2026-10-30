from typing import Any, Dict

import httpx
from loguru import logger
from pydantic import BaseModel, Field, HttpUrl
from pydantic_core import to_jsonable_python


class Webhook(BaseModel):
    url: HttpUrl
    track_id: str = Field("")

    def target_url(self) -> str:
        # HttpUrl normalises a bare host to a trailing slash, so plain concatenation
        # gave "https://example.com//track_id", or a bare "/" with no track_id.
        base = str(self.url).rstrip("/")
        return f"{base}/{self.track_id}" if self.track_id else base

    async def send(self, data: Dict[str, Any]) -> None:
        try:
            # Call sites pass model_dump(), which leaves datetime as a datetime. httpx
            # serialises json= with the stdlib encoder and raises before sending, so
            # coerce here rather than relying on every call site to use mode="json".
            payload = to_jsonable_python(data, serialize_unknown=True)
            async with httpx.AsyncClient() as client:
                await client.post(self.target_url(), json=payload, timeout=10)
        except Exception as e:
            logger.bind(url=self.url, track_id=self.track_id).exception(str(e))
