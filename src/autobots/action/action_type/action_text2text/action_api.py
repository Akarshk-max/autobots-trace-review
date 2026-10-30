import asyncio
import io
import json
import mimetypes
import os
import re
import tempfile
import typing
from typing import Literal, Type

import httpx
import pandas as pd
from httpx._types import PrimitiveData
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, ConfigDict, ValidationError

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.abc.ActionABC import ActionABC
from src.autobots.action.action_type.action_text2text.response_format_utils import (
    build_text_artifact_summary,
    infer_text_artifact_extension,
    is_ndjson_content_type,
    is_text_content_type,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.integrations.user_integration import UserIntegration
from src.autobots.integrations.app_types import AppTypes
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState

class APIInput(APIRequest):
    name: str | Literal["anon"] = "anon"
    app_name: AppTypes = AppTypes.anon
    method: typing.Optional[str] = None
    url: typing.Optional[str] = None
    headers: typing.Optional[typing.Mapping[str, str]] = None
    params: typing.Optional[typing.Mapping[str, typing.Union[PrimitiveData]]] = None
    files: typing.Optional[typing.Mapping[str, bytes]] = None
    body: typing.Optional[typing.Dict[str, typing.Any]] = None
    follow_redirects: typing.Optional[bool] = None
    timeout: typing.Optional[float] = None

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="ignore")


class APIResponse(BaseModel):
    status_code: int
    headers: typing.Optional[typing.Dict[str, str]] = None
    # NOTE: content can be either decoded text OR raw bytes.
    # In this codebase, binary HTTP responses and large structured text streams
    # (for example NDJSON) may be uploaded to S3 and returned as a URL/summary
    # pair via `content` and `content_url`.
    content: str | bytes | None = None
    # Content-Type (sans charset) if available.
    content_type: str | None = None
    # For binary responses and uploaded structured-text artifacts we return the URL here.
    content_url: str | None = None
    is_binary: bool = False
    # Suggested next steps for the LLM when certain errors occur (e.g., 401).
    llm_suggested_next_steps: str | None = None
    # text: typing.Optional[str] = None
    # stream: typing.Union[SyncByteStream, AsyncByteStream, None] = None
    # request: typing.Optional[Request] = None
    # extensions: typing.Optional[typing.MutableMapping[str, typing.Any]] = None
    # history: typing.Optional[typing.List["Response"]] = None
    # default_encoding: str = "utf-8"

    # If bytes ever do make it into APIResponse.content, ensure JSON-mode
    # serialization does not crash on non-UTF8 bytes.
    model_config = ConfigDict(arbitrary_types_allowed=True, ser_json_bytes="base64")


class ActionAPI(ActionABC[APIRequest, APIInput, APIRequest, DataBlock, DataBlocks]):
    type = ActionType.api

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
  "app_name": "anon" | "slack" | "slack_user" | "<other app type>",
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
- app_name (string, optional, defaults to "anon"):
    - The app type of the connection, as returned by the connected apps tool.
    - Include app_name in the config when calling an API through a named connection.
      It ensures the correct connection is selected, especially when the same connection name
      exists across multiple app types. Failing to send app_name may result in auth lookup
      failures or selecting the wrong connection.
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
    - content is the decoded response body (UTF-8) for text-like responses; for binary responses,
      the bytes are uploaded to S3 and `content`/`content_url` contain the resulting URL.
    - If the response contains base64 strings, they are automatically saved
      to S3 and replaced with URLs. Use these URLs with base64_decoder_extended
      (pass as "url" input) to decode the content.
- On failure:
    - An exception object is returned instead of APIResponse.
        """

    @staticmethod
    async def update_config(
        ctx: Context,
        config: APIRequest,
        config_update: APIInput,
        user: UserORM,
        db: AsyncIOMotorDatabase,
    ) -> APIRequest:
        # Only include fields actually provided by the caller.
        # This prevents default values from the update model overwriting existing config.
        update_dict = config_update.model_dump(exclude_unset=True, exclude_none=True)

        # If update_dict is a complete valid APIRequest => treat as replacement.
        # This fixes "deleted field comes back" when frontend omits empty objects (e.g., params).
        try:
            return APIRequest.model_validate(update_dict)
        except ValidationError:
            # Otherwise partial update (runtime-safe) => merge.
            base_dict = config.model_dump(exclude_none=True)
            return APIRequest.model_validate(base_dict | update_dict)

    # def __init__(self, action_config: APIRequest, user: UserORM | None = None):
    #     super().__init__(action_config=action_config, user=user)

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        try:
            # convert action_input.text to APIInput
            json_dict = json.loads(action_input.text)
            action_input = APIInput(**json_dict)
            config = await ActionAPI.update_config(
                ctx, self.action_config, action_input, user=self.user, db=self.db
            )
            api_response: APIResponse = await self.run_or_err(ctx, config)

            block_urls: list[str] = []
            if api_response.content_url:
                block_urls.append(api_response.content_url)

            block_text = ""
            if isinstance(api_response.content, str):
                block_text = api_response.content
            elif api_response.content_url:
                block_text = api_response.content_url
            else:
                # Fallback: bytes without URL (should not happen; binary responses are uploaded to S3).
                block_text = "<binary response>"

            datablocks = DataBlocks(blocks=[DataBlock(text=block_text, urls=block_urls)])
            return datablocks
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.ACTION_API).exception(str(e))
            return e

    async def run_tool(
        self, action_config: APIRequest, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> APIResponse | Exception:
        try:
            config = await ActionAPI.create_config(
                ctx=ctx, app_state=app_state, config_create=action_config, user=self.user, db=self.db
            )
            api_response = await self.run_or_err(ctx=ctx, action_config=config)
            # Add suggestion for 401 errors to guide LLM
            if api_response.status_code == 401:
                api_response.llm_suggested_next_steps = (
                    "This 401 Unauthorized error indicates authentication issues. "
                    "If you have not already called the 'get_connected_apps' tool, please call it "
                    "to verify which apps are connected and available for API calls."
                )
            return api_response
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.ACTION_API).exception(f"Error in run tool: {str(e)}")
            return e

    # async def run_or_err(self, ctx: Context, action_config: APIRequest) -> APIResponse:
    #     config = await self.build_headers(ctx=ctx, config=action_config)
    #     if config.name != "anon":
    #         config = await self.update_url(ctx=ctx, config=config)
    #     logger.bind(ctx=ctx, name=config.name).info("ActionAPI.get_ssl_context: Starting")
    #     # Get SSL context for mTLS if required (e.g., ADP)
    #     ssl_context = await self.get_ssl_context(ctx=ctx, config=config)

    #     # From body to content field
    #     config_dict = config.model_dump(exclude_none=True)
    #     follow_redirects = bool(config_dict.pop("follow_redirects", False))

    #     if config.body:
    #         config_dict.pop("body")  # removing
    #         config_dict["content"] = json.dumps(config.body)  # moving
    #     if config.name:
    #         config_dict.pop("name")

    #     # Create client with SSL context if available (for mTLS)
    #     if ssl_context:
    #         async with httpx.AsyncClient(follow_redirects=follow_redirects, verify=ssl_context) as client:
    #             logger.bind(ctx=ctx).info("Using mTLS for API call")
    #             logger.bind(ctx=ctx, api_request=config_dict).info("Calling API action")
    #             resp = await client.request(**config_dict)
    #     else:
    #         async with httpx.AsyncClient(follow_redirects=follow_redirects) as client:
    #             logger.bind(ctx=ctx, api_request=config_dict).info("Calling API action")
    #             resp = await client.request(**config_dict)

    #     response_headers = dict(resp.headers)
    #     # Make Location easy to access after converting to a case-sensitive dict.
    #     if "location" in response_headers and "Location" not in response_headers:
    #         response_headers["Location"] = response_headers["location"]

    #     api_response = APIResponse(
    #         status_code=resp.status_code,
    #         headers=response_headers,
    #         content=resp.content.decode("utf-8", errors="replace"),
    #         # text=resp.text,
    #         # extensions=resp.extensions,
    #         # default_encoding=resp.default_encoding
    #     )
    #     logger.bind(ctx=ctx, api_request=config_dict, api_response=api_response).info(
    #         "Action API called"
    #     )
    #     return api_response

    # async def get_ssl_context(self, ctx: Context, config: APIRequest):
    #     """Get SSL context for mTLS if required by the app"""
    #     if config.name == "anon" or config.name is None:
    #         logger.bind(ctx=ctx).info("ActionAPI.get_ssl_context: Anonymous app, skipping SSL")
    #         return None
    #     logger.bind(ctx=ctx, name=config.name).info(
    #         "ActionAPI.get_ssl_context: Calling UserIntegration.get_ssl_context"
    #     )
    #     ssl_context = await UserIntegration.get_ssl_context(
    #         ctx=ctx, db=self.db, user=self.user, name=config.name
    #     )
    #     logger.bind(ctx=ctx, ssl_context=ssl_context is not None).info("Got SSL context for mTLS")
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
    #     if config.name == "anon":
    #         return {}
    #     header = await UserIntegration.auth_header(
    #         ctx=ctx, db=self.db, user=self.user, name=config.name, config=config
    #     )
    #     return header

    # async def update_url(self, ctx: Context, config: APIRequest) -> APIRequest:
    #     # db = next(get_mongo_db())
    #     url = await UserIntegration.get_updated_url(
    #         ctx=ctx, db=self.db, user=self.user, name=config.name, url=config.url
    #     )
    #     if url:
    #         config.url = url
    #     return config

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

    async def run_or_err(self, ctx: Context, action_config: APIRequest) -> APIResponse:
        logger.bind(
            ctx=ctx,
            code=AppCode.ACTION_API,
            name=action_config.name,
            method=action_config.method,
            url=action_config.url,
        ).info("ActionAPI.run_or_err: Starting")
        config = action_config
        if config.name != "anon":
            config = await self.update_url(ctx=ctx, config=config)
            logger.bind(ctx=ctx, code=AppCode.ACTION_API, updated_url=config.url).info("ActionAPI: URL updated")

        # When to_download is True, resolve URLs in body values server-side
        if config.to_download and config.body:
            
            config.body = await self._resolve_body_urls(
                ctx=ctx, body=config.body, download_fields=config.download_fields
            )
            logger.bind(ctx=ctx, code=AppCode.ACTION_API, download_fields=config.download_fields).info(
                            "to_download: downloaded body URLs to content before sending API request"
                        )
            
        config = await self.build_headers(ctx=ctx, config=config)
        header_keys = sorted(list((config.headers or {}).keys()))
        has_authorization = any(k.lower() == "authorization" for k in header_keys)
        logger.bind(ctx=ctx, code=AppCode.ACTION_API, header_keys=header_keys, has_authorization=has_authorization).info(
            "ActionAPI: Headers built"
        )

        if config.name != "anon":
            config = await self.update_body(ctx=ctx, config=config)

        # Get SSL context for mTLS if required (e.g., ADP)
        logger.bind(ctx=ctx, code=AppCode.ACTION_API, name=config.name).info(
            "ActionAPI: Getting SSL context"
        )
        ssl_context = await self.get_ssl_context(ctx=ctx, config=config)

        # Build config_dict for httpx
        config_dict = config.model_dump(exclude_none=True)

        follow_redirects = bool(config_dict.pop("follow_redirects", False))

        # Pop fields that are not httpx params
        config_dict.pop("name", None)
        config_dict.pop("app_name", None)
        config_dict.pop("to_download", None)
        config_dict.pop("download_fields", None)
        config_dict.pop("raw_body_url", None)

        raw_body_tmp_path: str | None = None
        raw_body_size: int | None = None
        raw_body_content_type: str | None = None

        if config.raw_body_url:
            config_dict.pop("body", None)
            raw_body_tmp_path, raw_body_size, raw_body_content_type = (
                await self._download_raw_body_to_tempfile(ctx, config.raw_body_url)
            )

            headers = dict(config_dict.get("headers") or {})
            # Setting Content-Length is safer for some endpoints (avoids chunked transfer surprises).
            if raw_body_size is not None:
                headers.setdefault("Content-Length", str(raw_body_size))

            # If the downloaded content has a Content-Type and we don't already set it, use it.
            if raw_body_content_type and not any(
                k.lower() == "content-type" for k in headers.keys()
            ):
                headers["Content-Type"] = raw_body_content_type

            config_dict["headers"] = headers
            config_dict["content"] = self._iter_file_chunks(raw_body_tmp_path)
        elif config.body:
            config_dict.pop("body")
            config_dict["content"] = json.dumps(config.body)

        logger.bind(
            ctx=ctx,
            code=AppCode.ACTION_API,
            method=config_dict.get("method"),
            url=config_dict.get("url"),
            has_raw_body_url=bool(config.raw_body_url),
            raw_body_size=raw_body_size,
            has_content="content" in config_dict,
        ).info("ActionAPI: Request prepared")

        client_kwargs: dict[str, typing.Any] = {"follow_redirects": follow_redirects}
        if ssl_context:
            logger.bind(ctx=ctx, code=AppCode.ACTION_API, name=action_config.name).info(
                "ActionAPI: Using mTLS SSL context for API call"
            )
            client_kwargs["verify"] = ssl_context
        else:
            logger.bind(ctx=ctx, code=AppCode.ACTION_API, name=action_config.name).info(
                "ActionAPI: No mTLS, using standard client"
            )

        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                logger.bind(
                    ctx=ctx,
                    code=AppCode.ACTION_API,
                    method=config_dict.get("method"),
                    url=config_dict.get("url"),
                ).info("ActionAPI: Executing HTTP request")
                resp = await client.request(**config_dict)
        finally:
            if raw_body_tmp_path:
                try:
                    os.remove(raw_body_tmp_path)
                except Exception as e:
                    logger.bind(ctx=ctx, code=AppCode.ACTION_API, path=raw_body_tmp_path, error=str(e)).warning(
                        "raw_body_url: failed to delete temp file"
                    )

        logger.bind(
            ctx=ctx,
            code=AppCode.ACTION_API,
            status_code=resp.status_code,
            content_length=len(resp.content) if resp.content else 0,
        ).info("ActionAPI: Response received")

        # Capture headers (needed for protocols like YouTube resumable uploads -> Location header)
        response_headers = dict(resp.headers)
        # HTTP/2 commonly uses lowercase header names; make Location easy to access.
        if "location" in response_headers and "Location" not in response_headers:
            response_headers["Location"] = response_headers["location"]

        raw_content_type = resp.headers.get("content-type") or resp.headers.get("Content-Type") or ""
        content_type = raw_content_type.split(";", 1)[0].strip().lower() or None


        response_content: str | bytes = ""
        content_url: str | None = None
        is_binary = False

        if resp.content:
            if content_type and is_ndjson_content_type(content_type):
                try:
                    decoded_text = resp.content.decode("utf-8")
                    ext = infer_text_artifact_extension(content_type, decoded_text)
                    timestamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
                    filename = f"api_response_{timestamp}{ext}"
                    s3 = get_public_s3(object_prefix="api-response")
                    url = await s3.put_file_obj(io.BytesIO(resp.content), filename)
                    content_url = str(url)
                    response_content = build_text_artifact_summary(content_type, decoded_text)
                    logger.bind(
                        ctx=ctx,
                        code=AppCode.ACTION_API,
                        content_type=content_type,
                        artifact_url=content_url,
                        artifact_extension=ext,
                    ).info("ActionAPI: uploaded NDJSON/text artifact")
                except UnicodeDecodeError:
                    is_binary = True
            elif content_type and is_text_content_type(content_type):
                response_content = resp.text
                # Replace base64 strings in response with S3 URLs (text/JSON responses only).
                logger.bind(ctx=ctx, code=AppCode.ACTION_API).debug("base64_response: checking for base64 in response")
                response_content = await self._replace_base64_in_response(ctx, response_content)
            else:
                # If Content-Type is missing, attempt strict UTF-8; otherwise treat as binary.
                if not content_type:
                    try:
                        response_content = resp.content.decode("utf-8")
                        logger.bind(ctx=ctx, code=AppCode.ACTION_API).debug("base64_response: checking for base64 in response")
                        response_content = await self._replace_base64_in_response(ctx, response_content)
                    except UnicodeDecodeError:
                        is_binary = True
                else:
                    is_binary = True

                if is_binary:
                    ext = mimetypes.guess_extension(content_type or "") or ".bin"
                    # Normalize common awkward extensions from mimetypes.
                    if ext == ".jpe":
                        ext = ".jpg"
                    timestamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
                    filename = f"api_response_{timestamp}{ext}"
                    s3 = get_public_s3(object_prefix="api-response")
                    url = await s3.put_file_obj(io.BytesIO(resp.content), filename)
                    content_url = str(url)
                    response_content = content_url

        api_response = APIResponse(
            status_code=resp.status_code,
            headers=response_headers,
            content=response_content,
            content_type=content_type,
            content_url=content_url,
            is_binary=is_binary,
        )

        if resp.status_code >= 400:
            preview = response_content
            if isinstance(preview, bytes):
                preview = "<bytes>"
            logger.bind(
                ctx=ctx,
                code=AppCode.ACTION_API,
                status_code=resp.status_code,
                response_content=preview[:1000] if preview else "",
            ).error("ActionAPI: API returned error status")
        else:
            logger.bind(ctx=ctx, code=AppCode.ACTION_API, status_code=resp.status_code).info(
                "ActionAPI: API call successful"
            )

        return api_response

    async def get_ssl_context(self, ctx: Context, config: APIRequest):
        """Get SSL context for mTLS if required by the app (e.g., ADP)"""
        logger.bind(ctx=ctx, code=AppCode.ACTION_API, name=config.name).info(
            "ActionAPI.get_ssl_context: Starting"
        )

        if config.name == "anon" or config.name is None:
            logger.bind(ctx=ctx, code=AppCode.ACTION_API).info(
                "ActionAPI.get_ssl_context: Anonymous/None app, skipping SSL"
            )
            return None

        logger.bind(ctx=ctx, code=AppCode.ACTION_API, name=config.name).info(
            "ActionAPI.get_ssl_context: Calling UserIntegration.get_ssl_context"
        )
        ssl_context = await UserIntegration.get_ssl_context(
            ctx=ctx, db=self.db, user=self.user, name=config.name,
            app_name=config.app_name, decrypt=True
        )

        logger.bind(
            ctx=ctx,
            code=AppCode.ACTION_API,
            name=config.name,
            has_ssl_context=ssl_context is not None,
            ssl_context_type=type(ssl_context).__name__ if ssl_context else None,
        ).info("ActionAPI.get_ssl_context: Result")

        return ssl_context

    async def build_headers(self, ctx: Context, config: APIRequest) -> APIRequest:
        if config.headers is None:
            config.headers = {}
        content_type_header = await self.get_content_type_header_if_not_set(ctx, config)
        authorization_header = await self.get_authorization_header(ctx, config)
        config_dict = config.model_dump(exclude_none=True)
        config_dict["headers"] = config_dict["headers"] | authorization_header | content_type_header
        return APIRequest.model_validate(config_dict)

    async def get_authorization_header(
        self, ctx: Context, config: APIRequest
    ) -> typing.Dict[str, str]:
        # db = next(get_mongo_db())
        if config.name == "anon":
            return {}

        header = await UserIntegration.auth_header(
            ctx=ctx, db=self.db, user=self.user, name=config.name, config=config,
            app_name=config.app_name, decrypt=True
        )
        return header

    async def update_body(self, ctx: Context, config: APIRequest) -> APIRequest:
        if not config.body:
            return config
        updated_body = await UserIntegration.get_updated_body(
            ctx=ctx, db=self.db, user=self.user,
            name=config.name, body=config.body, app_name=config.app_name, decrypt=True
        )
        config_dict = config.model_dump(exclude_none=True)
        config_dict["body"] = updated_body
        return APIRequest.model_validate(config_dict)

    async def update_url(self, ctx: Context, config: APIRequest) -> APIRequest:
        # db = next(get_mongo_db())
        url = await UserIntegration.get_updated_url(
            ctx=ctx, db=self.db, user=self.user, name=config.name, url=config.url,
            app_name=config.app_name, decrypt=True
        )
        if url:
            config.url = url
        return config

    async def _resolve_body_urls(
        self,
        ctx: Context,
        body: typing.Dict[str, typing.Any],
        download_fields: typing.Optional[typing.List[str]] = None,
    ) -> typing.Dict[str, typing.Any]:
        """Walk body dict and download content from URL string values.

        Args:
            ctx: Request context for logging.
            body: The body dictionary to process.
            download_fields: If provided, only download URLs for these keys.
                             If None or empty, download URLs for ALL keys.
        """
        resolved = {}
        for key, value in body.items():
            # Check if this key should have its URL downloaded
            should_download = (
                download_fields is None or len(download_fields) == 0 or key in download_fields
            )

            if (
                should_download
                and isinstance(value, str)
                and (value.startswith("https://") or value.startswith("http://"))
            ):
                logger.bind(ctx=ctx, code=AppCode.ACTION_API, key=key, url=value).info(
                    "to_download: downloading content from URL"
                )
                try:
                    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                        r = await client.get(value)
                        r.raise_for_status()
                        resolved[key] = r.text
                        logger.bind(ctx=ctx, code=AppCode.ACTION_API, key=key, size=len(r.text)).info(
                            "to_download: download successful"
                        )
                except Exception as e:
                    logger.bind(ctx=ctx, code=AppCode.ACTION_API, key=key, url=value, error=str(e)).warning(
                        "to_download: download failed"
                    )
                    resolved[key] = value  # keep original on failure
            elif isinstance(value, dict):
                resolved[key] = await self._resolve_body_urls(
                    ctx=ctx, body=value, download_fields=download_fields
                )
            else:
                resolved[key] = value
        return resolved

    async def _download_raw_body_bytes(self, ctx: Context, url: str) -> bytes:
        """Download raw bytes from a URL for use as the HTTP request body."""
        logger.bind(ctx=ctx, code=AppCode.ACTION_API, url=url).info("raw_body_url: downloading bytes")
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            logger.bind(ctx=ctx, code=AppCode.ACTION_API, size=len(r.content)).info("raw_body_url: download complete")
            return r.content

    async def _download_raw_body_to_tempfile(
        self, ctx: Context, url: str, chunk_size: int = 1024 * 1024
    ) -> tuple[str, int, str | None]:
        logger.bind(ctx=ctx, code=AppCode.ACTION_API, url=url).info("raw_body_url: streaming download starting")

        fd, tmp_path = tempfile.mkstemp(prefix="raw_body_", suffix=".bin")
        os.close(fd)

        size = 0
        content_type: str | None = None
        try:
            # Longer read timeout because artifacts can be large.
            timeout = httpx.Timeout(60.0, read=300.0)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                async with client.stream("GET", url) as r:
                    r.raise_for_status()
                    content_type = r.headers.get("Content-Type")
                    with open(tmp_path, "wb") as f:
                        async for chunk in r.aiter_bytes(chunk_size=chunk_size):
                            if not chunk:
                                continue
                            f.write(chunk)
                            size += len(chunk)

            # logger.bind(ctx=ctx, code=AppCode.ACTION_API, size=size, path=tmp_path).info(
            #     "raw_body_url: streaming download complete"
            # )
            return tmp_path, size, content_type
        except Exception:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            raise

    async def _iter_file_chunks(
        self, path: str, chunk_size: int = 1024 * 1024
    ) -> typing.AsyncIterator[bytes]:
        """Async-iterate a file in chunks.

        Uses asyncio.to_thread to avoid blocking the event loop on file reads.
        """
        f = open(path, "rb")
        try:
            while True:
                chunk = await asyncio.to_thread(f.read, chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                f.close()
            except Exception:
                pass


    async def _replace_base64_in_response(self, ctx: Context, content: str) -> str:
        """Detect base64 strings in response and replace with S3 URLs.

        Parses response as JSON, finds string values that look like base64
        (length > threshold, valid base64 chars), uploads them to S3,
        and replaces with the URL.
        """
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return content  # Not JSON, return as-is

        modified, data = await self._process_value_for_base64(ctx, data)
        if modified:
            logger.bind(ctx=ctx, code=AppCode.ACTION_API).info("base64_response: replaced base64 strings with S3 URLs")
            return json.dumps(data)
        return content

    async def _process_value_for_base64(
        self, ctx: Context, value: typing.Any
    ) -> tuple[bool, typing.Any]:
        """Recursively process a value, replacing base64 strings with S3 URLs."""
        if isinstance(value, str):
            if self._looks_like_base64(value):
                logger.bind(ctx=ctx, code=AppCode.ACTION_API, size=len(value)).info(
                    "base64_response: detected base64 string, uploading to S3"
                )
                try:
                    url = await self._upload_base64_to_s3(ctx, value)
                    logger.bind(ctx=ctx, code=AppCode.ACTION_API, url=url).info("base64_response: upload successful")
                    return True, url
                except Exception as e:
                    logger.bind(ctx=ctx, code=AppCode.ACTION_API, error=str(e)).warning("base64_response: upload failed")
                    return False, value
            return False, value
        elif isinstance(value, dict):
            modified = False
            new_dict = {}
            for k, v in value.items():
                m, new_v = await self._process_value_for_base64(ctx, v)
                modified = modified or m
                new_dict[k] = new_v
            return modified, new_dict
        elif isinstance(value, list):
            modified = False
            new_list = []
            for item in value:
                m, new_item = await self._process_value_for_base64(ctx, item)
                modified = modified or m
                new_list.append(new_item)
            return modified, new_list
        else:
            return False, value

    def _looks_like_base64(self, s: str) -> bool:
        """Check if string looks like base64 (alphanumeric + /+=, no spaces)."""
        if len(s) < 100:  # Too short to be meaningful base64
            return False
        # Base64 pattern: alphanumeric, +, /, = padding, no whitespace
        pattern = r"^[A-Za-z0-9+/]+=*$"
        return bool(re.match(pattern, s))

    async def _upload_base64_to_s3(self, ctx: Context, base64_str: str) -> str:
        """Upload base64 string to S3 and return URL."""
        timestamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
        filename = f"api_response_base64_{timestamp}.txt"
        s3 = get_public_s3(object_prefix="api-response")
        url = await s3.put_file_obj(io.BytesIO(base64_str.encode("utf-8")), filename)
        return str(url)

    async def get_content_type_header_if_not_set(
        self, ctx: Context, config: APIRequest, default: str = "application/json"
    ) -> typing.Dict[str, str]:
        headers = dict(config.headers or {})
        has_content_type = any(
            isinstance(k, str) and k.lower() == "content-type" for k in headers.keys()
        )
        if has_content_type:
            return {}
        # Only default to JSON when actually sending a JSON body — not for raw bytes or multipart.
        if config.body is not None and not config.raw_body_url and not config.files:
            return {"Content-Type": default}
        return {}
