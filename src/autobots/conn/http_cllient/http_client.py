import httpx
from pydantic import HttpUrl


class HttpClient:
    @staticmethod
    async def download_from_url(url: HttpUrl):
        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.get(str(url))
            response.raise_for_status()
            return response.content
