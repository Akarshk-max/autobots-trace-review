import importlib.metadata
from typing import Optional

from anthropic import AsyncAnthropic
from httpx2 import Timeout
from loguru import logger

from src.autobots.conn.claude.claude_chat import AnthropicChat
from src.autobots.data_model.context import Context


class Claude:
    """
    A client for interacting with the Anthropic Claude API.

    This class wraps the AsyncAnthropic client from Anthropic's SDK
    and provides access to Claude's chat functionality.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        organization: str | None = None,
        timeout: float | Timeout | None = None,
        max_retries: int = 3,
        default_headers: Optional[dict] = None,
    ):
        """
        Initialize a new Claude instance.

        Args:
            api_key: The Anthropic API key for authentication
            organization: The Anthropic organization ID (if applicable)
            timeout: Request timeout in seconds or an httpx.Timeout object
            max_retries: Maximum number of retries for API calls
            default_headers: Additional default headers to include in all requests
        """
        # Create default headers if not provided
        if default_headers is None:
            default_headers = {}

        # Initialize the Anthropic client with all settings
        self.client = AsyncAnthropic(
            api_key=api_key,
            timeout=timeout,
            max_retries=max_retries,
            default_headers=default_headers,
        )
        self.claude_chat = AnthropicChat(self.client)


# @lru_cache
# def get_claude(settings: Settings = SettingsProvider.sget()) -> Claude:
#     """
#     Get a configured Claude client instance.
#
#     This function is cached so the same Claude instance will be returned
#     for the same settings.
#
#     Args:
#         settings: Application settings containing Anthropic API key
#
#     Returns:
#         A configured Claude client instance
#     """
#     # Determine appropriate API version based on installed SDK
#     try:
#         anthropic_version = pkg_resources.get_distribution("anthropic").version
#         logger.info(f"Using Anthropic SDK version: {anthropic_version}")
#
#         # Get major.minor version
#         version_parts = anthropic_version.split('.')
#         major = int(version_parts[0])
#         minor = int(version_parts[1])
#
#         # Default to 2023-06-01 for older versions
#         api_version = "2023-06-01"
#
#         # For newer versions, use newer API versions
#         if major > 0 or (major == 0 and minor >= 30):
#             api_version = "2023-12-01"
#         if major > 0 or (major == 0 and minor >= 40):
#             api_version = "2024-05"
#
#     except Exception as e:
#         logger.warning(f"Could not determine anthropic package version: {str(e)}")
#         api_version = "2023-06-01"
#
#     # Set up additional request headers
#     # default_headers = {
#     #     "anthropic-version": api_version
#     # }
#
#     # Log which API version we're using
#     logger.info(f"Using Anthropic API version: {api_version}")
#
#     return Claude(
#         api_key=settings.CLAUDE_API_KEY,
#         timeout=300,
#         max_retries=3,
#         # default_headers=default_headers
#     )


def get_claude_conn(ctx: Context, api_key: str) -> Claude:
    # Determine appropriate API version based on installed SDK
    try:
        anthropic_version = importlib.metadata.version("anthropic")
        logger.info(f"Using Anthropic SDK version: {anthropic_version}")

        # Get major.minor version
        version_parts = anthropic_version.split(".")
        major = int(version_parts[0])
        minor = int(version_parts[1])

        # Default to 2023-06-01 for older versions
        api_version = "2023-06-01"

        # For newer versions, use newer API versions
        if major > 0 or (major == 0 and minor >= 30):
            api_version = "2023-12-01"
        if major > 0 or (major == 0 and minor >= 40):
            api_version = "2024-05"

    except Exception as e:
        logger.bind(ctx=ctx).warning(f"Could not determine anthropic package version: {str(e)}")
        api_version = "2023-06-01"

    # Set up additional request headers
    # default_headers = {
    #     "anthropic-version": api_version
    # }

    # Log which API version we're using
    logger.bind(ctx=ctx).info(f"Using Anthropic API version: {api_version}")

    return Claude(
        api_key=api_key,
        timeout=3600,
        max_retries=3,
        # default_headers=default_headers
    )