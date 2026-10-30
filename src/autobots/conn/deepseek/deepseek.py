from dataclasses import dataclass
from typing import Optional

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM

try:
    from openai import AsyncOpenAI
except Exception as e:
    raise RuntimeError(
        "OpenAI SDK not installed or incompatible. Install with: pip install openai"
    ) from e


@dataclass
class DeepSeekConnOptions:
    api_key: str
    base_url: str = "https://api.deepseek.com"
    beta_base_url: str = "https://api.deepseek.com/beta"  # for strict tools
    timeout_sec: float = 420.0

    # OpenAI SDK has its own retries; we also do app-level retry in common
    max_sdk_retries: int = 2

    # Optional: custom headers for gateway/proxy
    default_headers: Optional[dict] = None


class DeepSeekConnection:
    """
    Holds:
      - normal client: base_url=https://api.deepseek.com
      - beta client:   base_url=https://api.deepseek.com/beta   (strict tools)
    """

    def __init__(self, opts: DeepSeekConnOptions) -> None:
        if not opts.api_key:
            raise ValueError("DeepSeek api_key is required")

        self.opts = opts
        self._client = AsyncOpenAI(
            api_key=opts.api_key,
            base_url=opts.base_url,
            timeout=opts.timeout_sec,
            max_retries=opts.max_sdk_retries,
            default_headers=opts.default_headers,
        )
        self._beta_client = AsyncOpenAI(
            api_key=opts.api_key,
            base_url=opts.beta_base_url,
            timeout=opts.timeout_sec,
            max_retries=opts.max_sdk_retries,
            default_headers=opts.default_headers,
        )

        logger.info(
            "DeepSeek clients created (base_url={}, beta_base_url={})",
            opts.base_url,
            opts.beta_base_url,
        )

    def client(self, *, beta: bool = False) -> AsyncOpenAI:
        return self._beta_client if beta else self._client

    async def close(self) -> None:
        # OpenAI SDK v1 has close() on client.http_client; best-effort
        for c in (self._client, self._beta_client):
            try:
                http = getattr(c, "http_client", None)
                if http and hasattr(http, "aclose"):
                    await http.aclose()
            except Exception:
                pass


def get_deepseek_conn(ctx : Context , user: UserORM ,db: AsyncIOMotorDatabase, api_key: str, base_url: str = "https://api.deepseek.com") -> DeepSeekConnection:
    return DeepSeekConnection(DeepSeekConnOptions(api_key=api_key, base_url=base_url))
