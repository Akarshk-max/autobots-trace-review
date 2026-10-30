import typing
from typing import Literal, Type

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, ConfigDict, ValidationError

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.abc.ActionABC import ActionABC
from src.autobots.action.action_type.action_text2text.action_api import ActionAPI
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM

from src.autobots.data_model.app_state import AppState

class APIInput(APIRequest):
    name: str | Literal["anon"] = "anon"
    method: typing.Optional[str] = None
    url: typing.Optional[str] = None


class APIResponse(BaseModel):
    status_code: int
    headers: typing.Optional[typing.Dict[str, str]] = None
    content: typing.Optional[str] = None

    # text: typing.Optional[str] = None
    # stream: typing.Union[SyncByteStream, AsyncByteStream, None] = None
    # request: typing.Optional[Request] = None
    # extensions: typing.Optional[typing.MutableMapping[str, typing.Any]] = None
    # history: typing.Optional[typing.List["Response"]] = None
    # default_encoding: str = "utf-8"

    model_config = ConfigDict(arbitrary_types_allowed=True)


class ActionText2textAPI(ActionABC[APIRequest, APIInput, APIRequest, DataBlock, DataBlocks]):
    type = ActionType.text2text_api_call

    @staticmethod
    def get_config_create_type() -> Type[APIRequest]:
        return APIRequest

    @staticmethod
    def get_config_update_type() -> Type[APIInput]:
        return APIInput

    @staticmethod
    def get_config_type() -> Type[APIRequest]:
        return APIRequest

    @staticmethod
    def get_input_type() -> Type[DataBlock]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[DataBlocks]:
        return DataBlocks

    @staticmethod
    def get_description() -> str:
        return """
PREREQUISITE: Before calling this tool, call get_connected_apps to check what apps the user has authorised connections to and has access to.

Description:
- Send an HTTP request to any API endpoint and return the response.
- Supports authenticated calls via configured apps, query params, JSON body, custom headers, and file uploads.
- Uses the provided configuration then executes the request and returns status code and body text.

When to use this tool:
- Use when you need to call an external HTTP API (REST/JSON, etc.) from within a workflow.
- Use when the user wants to integrate with third-party services (e.g., Slack, GitHub, custom APIs).
- Use when you already know the HTTP method, URL, headers, params, and body to send.

Input config :
{
  "name": "anon" | "grok" | "openai" | "<other like that>",
  "method": "GET" | "POST" | "PUT" | "DELETE",
  "url": "https://api.example.com/resource",
  "headers": { "Header-Name": "value", ... },
  "params": { "query": "value", ... },
  "files": { "field_name": <bytes> },
  "body": { "json_key": "json_value", ... },
  "follow_redirects": false,
  "timeout": 10.0
}

Fields:
- name (string, required):
    - The name of the user's connection (app_auth/app_creds) for the target app.
    - "anon" = no auth, URL used as-is.
    - Any other name: the tool injects authorization headers and may rewrite the URL
      via UserIntegration before calling the API.
- method (string, required):
    - HTTP method (e.g., "GET", "POST", "PUT", "DELETE", "PATCH").
- url (string, required):
    - Target endpoint URL. May be updated when name != "anon".
- headers (object, optional):
    - Additional HTTP headers.
    - If no "Content-Type" header is set, "application/json" is added by default
      when a body is present.
- params (object, optional):
    - Query parameters appended to the URL.
- files (object, optional):
    - Mapping of form field name → raw bytes for file upload style requests.
- body (object, optional):
    - JSON-serializable body. It is automatically JSON-encoded and sent as request content.
- follow_redirects (bool, optional):
    - Whether the client should follow redirects. Defaults to false in config,
      but the underlying client is created with redirect support enabled.
- timeout (float, optional):
    - Request timeout in seconds.
- to_download (bool, optional, default false):
    - When true, any string value in the body that looks like an HTTP URL is
      downloaded server-side and replaced with the downloaded content before
      sending the API request.
    - Use this when passing output URLs from base64_encoder_extended or
      base64_decoder_extended tools. Instead of passing large base64 strings
      in the body, pass the S3 URL and set to_download=true. The tool will
      fetch the full content from the URL and include it in the request.
- download_fields (array of strings, optional):
    - Only used when to_download=true.
    - Specifies which body keys should have their URL values downloaded.
    - If not provided or empty, ALL URL values in the body are downloaded.
    - If provided, only the specified keys are processed; other URL values
      remain unchanged (useful when body has URLs like callback_url or
      webhook_url that should NOT be downloaded).
    - Example: ["content", "file_data"] will only download URLs in those fields.
- raw_body_url (string, optional):
    - When set, the tool downloads raw bytes from this URL and sends them as the
      HTTP request body directly (no JSON serialization).
    - Use this for APIs that expect raw file bytes instead of a JSON body, such as:
      - Google Drive simple upload (uploadType=media)
      - Any API that accepts raw binary content (PDF, image, etc.)
    - When raw_body_url is set, the "body" field is ignored.
    - You MUST set the Content-Type header manually (e.g., "application/pdf",
      "image/png", "application/octet-stream").
    - The URL can be an S3 URL from base64_encoder_extended, a direct file URL,
      or any publicly accessible URL.
    - Example (Google Drive simple upload):
      {
        "name": "google_drive",
        "method": "POST",
        "url": "https://www.googleapis.com/upload/drive/v3/files?uploadType=media",
        "headers": {"Content-Type": "application/pdf"},
        "raw_body_url": "https://...s3.../file.pdf"
      }

USING URLs FROM base64_encoder_extended:

The encoder returns a "url" field with an S3 link to the full base64 string.
The "base64" field is TRUNCATED - do NOT use it directly.

To send encoded content to an API:
1. Take the "url" from encoder output
2. Put it as a value in body (e.g., "content": "https://...s3.../file.txt")
3. Set to_download=true and download_fields to the key name (e.g., ["content"])

Example:
{
  "method": "PUT",
  "url": "https://api.github.com/repos/user/repo/contents/file.pdf",
  "body": {"message": "Add file", "content": "https://...s3.../encoded.txt"},
  "to_download": true,
  "download_fields": ["content"]
}

CRITICAL NOTES (IMPORTANT):
Consideration for Netsuite:
Note that you do not need to ask for NetSuite account ID, that is automatically handled, just proceed with the connection using the URL with placeholder ,those placeholder will be normalized with correct value internally.

Output:
- An APIResponse object:
  {
    "status_code": <int>,
    "headers": { "Header-Name": "value", ... },
    "content": "<response body as UTF-8 string or None>"
  }

- On success:
    - status_code is the HTTP status code returned by the API.
    - content is the decoded response body (UTF-8).
    - If the response contains base64 strings, they are automatically saved
      to S3 and replaced with URLs. Use these URLs with base64_decoder_extended
      (pass as "url" input) to decode the content.
- On failure:
    - An exception object is returned instead of APIResponse.
"""

    @staticmethod
    async def update_config(
        ctx: Context,
        app_state: AppState,
        config: APIRequest,
        config_update: APIInput,
        user: UserORM,
        db: AsyncIOMotorDatabase,
    ) -> APIRequest:
        update_dict = config_update.model_dump(exclude_unset=True, exclude_none=True)
        try:
            # Full valid config => replacement (deletes omitted empty objects)
            return APIRequest.model_validate(update_dict)
        except ValidationError:
            # Partial update => merge
            base_dict = config.model_dump(exclude_none=True)
            return APIRequest.model_validate(base_dict | update_dict)

    # def __init__(self, action_config: APIRequest, user: UserORM | None = None):
    #     super().__init__(action_config=action_config, user=user)

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        try:
            logger.bind(ctx=ctx, code=AppCode.ACTION_API).info(
                f"Running ActionText2textAPI with input: {action_input}. Delegating to ActionAPI."
            )
            action_api = ActionAPI(action_config=self.action_config, user=self.user, db=self.db)
            return await action_api.run_action(ctx=ctx, app_state=app_state, action_input=action_input, notify=notify)
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.ACTION_API).exception(str(e))
            return e

    async def run_tool(
        self, action_config: APIRequest, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        try:
            logger.bind(ctx=ctx, code=AppCode.ACTION_API).info(
                "Running ActionText2textAPI run_tool. Delegating to ActionAPI."
            )
            action_api = ActionAPI(action_config=self.action_config, user=self.user, db=self.db)
            return await action_api.run_tool(action_config=action_config, ctx=ctx, app_state=app_state, notify=notify)
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.ACTION_API).exception(f"Error in run tool: {str(e)}")
            return e

    # async def run_or_err(self, ctx: Context, action_config: APIRequest) -> APIResponse:
    #     logger.bind(
    #         ctx=ctx,
    #         app_name=action_config.app_name,
    #         method=action_config.method,
    #         url=action_config.url,
    #     ).info("ActionText2textAPI.run_or_err: Starting")
    #     config = action_config
    #     if config.app_name != "anon":
    #         logger.bind(ctx=ctx, app_name=config.app_name).info("ActionText2textAPI: Updating URL")
    #         config = await self.update_url(ctx=ctx, config=config)
    #         logger.bind(ctx=ctx, updated_url=config.url).info("ActionText2textAPI: URL updated")
    #
    #     config = await self.build_headers(ctx=ctx, config=config)
    #     logger.bind(ctx=ctx, headers=config.headers).info("ActionText2textAPI: Headers built")
    #
    #     # Get SSL context for mTLS if required (e.g., ADP)
    #     logger.bind(ctx=ctx, app_name=config.app_name).info(
    #         "ActionText2textAPI: Getting SSL context"
    #     )
    #     ssl_context = await self.get_ssl_context(ctx=ctx, config=config)
    #
    #     # When to_download is True, resolve URLs in body values server-side
    #     if config.to_download and config.body:
    #         logger.bind(ctx=ctx, download_fields=config.download_fields).info(
    #             "to_download: starting URL resolution in body"
    #         )
    #         config.body = await self._resolve_body_urls(
    #             ctx=ctx, body=config.body, download_fields=config.download_fields
    #         )
    #         logger.bind(ctx=ctx).info("to_download: URL resolution complete")
    #
    #     # Build config_dict for httpx
    #     config_dict = config.model_dump(exclude_none=True)
    #
    #     follow_redirects = bool(config_dict.pop("follow_redirects", False))
    #
    #     # Pop fields that are not httpx params
    #     config_dict.pop("app_name", None)
    #     config_dict.pop("to_download", None)
    #     config_dict.pop("download_fields", None)
    #     config_dict.pop("raw_body_url", None)
    #
    #     if config.raw_body_url:
    #         # Raw binary body mode: download bytes from URL and send as-is
    #         config_dict.pop("body", None)
    #         raw_bytes = await self._download_raw_body_bytes(ctx, config.raw_body_url)
    #         config_dict["content"] = raw_bytes
    #     elif config.body:
    #         config_dict.pop("body")
    #         config_dict["content"] = json.dumps(config.body)
    #
    #     # Create client with SSL context if available (for mTLS)
    #     logger.bind(ctx=ctx).info(f"Config Dict before first AsyncClient request call: {config_dict}")
    #     if ssl_context:
    #         async with httpx.AsyncClient(follow_redirects=follow_redirects, verify=ssl_context) as client:
    #             logger.bind(ctx=ctx, app_name=action_config.app_name).info(
    #                 "ActionText2textAPI: Using mTLS SSL context for API call"
    #             )
    #             logger.bind(
    #                 ctx=ctx,
    #                 method=config_dict.get("method"),
    #                 url=config_dict.get("url"),
    #             ).info("ActionText2textAPI: Executing HTTP request")
    #             resp = await client.request(**config_dict)
    #     else:
    #         async with httpx.AsyncClient(follow_redirects=follow_redirects) as client:
    #             logger.bind(ctx=ctx, app_name=action_config.app_name).info(
    #                 "ActionText2textAPI: No mTLS, using standard client"
    #             )
    #             logger.bind(
    #                 ctx=ctx,
    #                 method=config_dict.get("method"),
    #                 url=config_dict.get("url"),
    #             ).info("ActionText2textAPI: Executing HTTP request")
    #             resp = await client.request(**config_dict)
    #
    #     logger.bind(
    #         ctx=ctx,
    #         status_code=resp.status_code,
    #         content_length=len(resp.content) if resp.content else 0,
    #     ).info("ActionText2textAPI: Response received")
    #
    #     # Capture headers (needed for protocols like YouTube resumable uploads -> Location header)
    #     response_headers = dict(resp.headers)
    #     # HTTP/2 commonly uses lowercase header names; make Location easy to access.
    #     if "location" in response_headers and "Location" not in response_headers:
    #         response_headers["Location"] = response_headers["location"]
    #
    #     response_content = resp.content.decode("utf-8", errors="replace")
    #
    #     # Replace base64 strings in response with S3 URLs
    #     logger.bind(ctx=ctx).debug("base64_response: checking for base64 in response")
    #     response_content = await self._replace_base64_in_response(ctx, response_content)
    #
    #     api_response = APIResponse(
    #         status_code=resp.status_code,
    #         headers=response_headers,
    #         content=response_content,
    #     )
    #
    #     if resp.status_code >= 400:
    #         logger.bind(
    #             ctx=ctx,
    #             status_code=resp.status_code,
    #             response_content=resp.content.decode(encoding="utf-8")[:1000]
    #             if resp.content
    #             else "",
    #         ).error("ActionText2textAPI: API returned error status")
    #     else:
    #         logger.bind(ctx=ctx, status_code=resp.status_code).info(
    #             "ActionText2textAPI: API call successful"
    #         )
    #
    #     return api_response

    # async def get_ssl_context(self, ctx: Context, config: APIRequest):
    #     """Get SSL context for mTLS if required by the app (e.g., ADP)"""
    #     logger.bind(ctx=ctx, app_name=config.app_name).info(
    #         "ActionText2textAPI.get_ssl_context: Starting"
    #     )
    #
    #     if config.app_name == "anon" or config.app_name is None:
    #         logger.bind(ctx=ctx).info(
    #             "ActionText2textAPI.get_ssl_context: Anonymous/None app, skipping SSL"
    #         )
    #         return None
    #
    #     logger.bind(ctx=ctx, app_name=config.app_name).info(
    #         "ActionText2textAPI.get_ssl_context: Calling UserIntegration.get_ssl_context"
    #     )
    #     ssl_context = await UserIntegration.get_ssl_context(
    #         ctx=ctx, db=self.db, user=self.user, app_name=config.app_name
    #     )
    #
    #     logger.bind(
    #         ctx=ctx,
    #         app_name=config.app_name,
    #         has_ssl_context=ssl_context is not None,
    #         ssl_context_type=type(ssl_context).__name__ if ssl_context else None,
    #     ).info("ActionText2textAPI.get_ssl_context: Result")
    #
    #     return ssl_context

    # async def build_headers(self, ctx: Context, config: APIRequest) -> APIRequest:
    #     if config.headers is None:
    #         config.headers = {}
    #     content_type_header = await self.get_content_type_header_if_not_set(ctx, config)
    #     authorization_header = await self.get_authorization_header(ctx, config)
    #     config_dict = config.model_dump(exclude_none=True)
    #     config_dict["headers"] = config_dict["headers"] | authorization_header | content_type_header
    #     return APIRequest.model_validate(config_dict)

    # async def get_authorization_header(
    #     self, ctx: Context, config: APIRequest
    # ) -> typing.Dict[str, str]:
    #     # db = next(get_mongo_db())
    #     if config.app_name == "anon":
    #         return {}
    #
    #     header = await UserIntegration.auth_header(
    #         ctx=ctx, db=self.db, user=self.user, app_name=config.app_name, config=config
    #     )
    #     return header

    # async def update_url(self, ctx: Context, config: APIRequest) -> APIRequest:
    #     # db = next(get_mongo_db())
    #     url = await UserIntegration.get_updated_url(
    #         ctx=ctx, db=self.db, user=self.user, app_name=config.app_name, url=config.url
    #     )
    #     if url:
    #         config.url = url
    #     return config

    # async def _resolve_body_urls(
    #     self,
    #     ctx: Context,
    #     body: typing.Dict[str, typing.Any],
    #     download_fields: typing.Optional[typing.List[str]] = None,
    # ) -> typing.Dict[str, typing.Any]:
    #     """Walk body dict and download content from URL string values.
    #
    #     Args:
    #         ctx: Request context for logging.
    #         body: The body dictionary to process.
    #         download_fields: If provided, only download URLs for these keys.
    #                          If None or empty, download URLs for ALL keys.
    #     """
    #     resolved = {}
    #     for key, value in body.items():
    #         # Check if this key should have its URL downloaded
    #         should_download = (
    #             download_fields is None or len(download_fields) == 0 or key in download_fields
    #         )
    #
    #         if (
    #             should_download
    #             and isinstance(value, str)
    #             and (value.startswith("https://") or value.startswith("http://"))
    #         ):
    #             logger.bind(ctx=ctx, key=key, url=value).info(
    #                 "to_download: downloading content from URL"
    #             )
    #             try:
    #                 async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
    #                     r = await client.get(value)
    #                     r.raise_for_status()
    #                     resolved[key] = r.text
    #                     logger.bind(ctx=ctx, key=key, size=len(r.text)).info(
    #                         "to_download: download successful"
    #                     )
    #             except Exception as e:
    #                 logger.bind(ctx=ctx, key=key, url=value, error=str(e)).warning(
    #                     "to_download: download failed"
    #                 )
    #                 resolved[key] = value  # keep original on failure
    #         elif isinstance(value, dict):
    #             resolved[key] = await self._resolve_body_urls(
    #                 ctx=ctx, body=value, download_fields=download_fields
    #             )
    #         else:
    #             resolved[key] = value
    #     return resolved

    # async def _download_raw_body_bytes(self, ctx: Context, url: str) -> bytes:
    #     """Download raw bytes from a URL for use as the HTTP request body."""
    #     logger.bind(ctx=ctx, url=url).info("raw_body_url: downloading bytes")
    #     async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
    #         r = await client.get(url)
    #         r.raise_for_status()
    #         logger.bind(ctx=ctx, size=len(r.content)).info("raw_body_url: download complete")
    #         return r.content

    # async def _replace_base64_in_response(self, ctx: Context, content: str) -> str:
    #     """Detect base64 strings in response and replace with S3 URLs.
    #
    #     Parses response as JSON, finds string values that look like base64
    #     (length > threshold, valid base64 chars), uploads them to S3,
    #     and replaces with the URL.
    #     """
    #     try:
    #         data = json.loads(content)
    #     except (json.JSONDecodeError, TypeError):
    #         return content  # Not JSON, return as-is
    #
    #     modified, data = await self._process_value_for_base64(ctx, data)
    #     if modified:
    #         logger.bind(ctx=ctx).info("base64_response: replaced base64 strings with S3 URLs")
    #         return json.dumps(data)
    #     return content

    # async def _process_value_for_base64(
    #     self, ctx: Context, value: typing.Any
    # ) -> tuple[bool, typing.Any]:
    #     """Recursively process a value, replacing base64 strings with S3 URLs."""
    #     if isinstance(value, str):
    #         if self._looks_like_base64(value):
    #             logger.bind(ctx=ctx, size=len(value)).info(
    #                 "base64_response: detected base64 string, uploading to S3"
    #             )
    #             try:
    #                 url = await self._upload_base64_to_s3(ctx, value)
    #                 logger.bind(ctx=ctx, url=url).info("base64_response: upload successful")
    #                 return True, url
    #             except Exception as e:
    #                 logger.bind(ctx=ctx, error=str(e)).warning("base64_response: upload failed")
    #                 return False, value
    #         return False, value
    #     elif isinstance(value, dict):
    #         modified = False
    #         new_dict = {}
    #         for k, v in value.items():
    #             m, new_v = await self._process_value_for_base64(ctx, v)
    #             modified = modified or m
    #             new_dict[k] = new_v
    #         return modified, new_dict
    #     elif isinstance(value, list):
    #         modified = False
    #         new_list = []
    #         for item in value:
    #             m, new_item = await self._process_value_for_base64(ctx, item)
    #             modified = modified or m
    #             new_list.append(new_item)
    #         return modified, new_list
    #     else:
    #         return False, value

    # def _looks_like_base64(self, s: str) -> bool:
    #     """Check if string looks like base64 (alphanumeric + /+=, no spaces)."""
    #     if len(s) < 100:  # Too short to be meaningful base64
    #         return False
    #     # Base64 pattern: alphanumeric, +, /, = padding, no whitespace
    #     pattern = r"^[A-Za-z0-9+/]+=*$"
    #     return bool(re.match(pattern, s))

    # async def _upload_base64_to_s3(self, ctx: Context, base64_str: str) -> str:
    #     """Upload base64 string to S3 and return URL."""
    #     timestamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
    #     filename = f"api_response_base64_{timestamp}.txt"
    #     s3 = get_public_s3(object_prefix="api-response")
    #     url = await s3.put_file_obj(io.BytesIO(base64_str.encode("utf-8")), filename)
    #     return str(url)

    # async def get_content_type_header_if_not_set(
    #     self, ctx: Context, config: APIRequest, default: str = "application/json"
    # ) -> typing.Dict[str, str]:
    #     # Content-type
    #     config_dict = config.model_dump(exclude_none=True)
    #     is_content_type_set = False
    #     if config_dict.__contains__("headers"):
    #         for key in config_dict["headers"]:
    #             if isinstance(key, str):
    #                 if "content-type" == key.lower():
    #                     is_content_type_set = True
    #                     break
    #         if not is_content_type_set:
    #             return {"Content-Type": default}
    #     return {}
