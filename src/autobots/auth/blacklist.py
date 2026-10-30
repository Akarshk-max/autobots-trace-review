from fastapi import HTTPException, Request
from loguru import logger


class Blacklist:
    BLACKLISTED_HOSTS = ["44.240.109.143", "0.0.0.0"]

    @staticmethod
    async def assert_auth_checks(request: Request):
        ctx = request.state.context
        try:
            hostname = request.base_url.hostname
            assert hostname, "URL not set"

            assert request.base_url.hostname.lower() not in Blacklist.BLACKLISTED_HOSTS, (
                f"Blacklisted Host: {hostname}"
            )
        except Exception as e:
            logger.bind(ctx=ctx, hostname=request.base_url.hostname, url=request.url).exception(
                f"Blacklist error: {str(e)}"
            )
            raise HTTPException(status_code=404, detail="Page not found")
