import json
import math
from enum import Enum
from typing import Annotated, Any, Literal, Type

import httpx
import jmespath
from fastapi import HTTPException
from loguru import logger
from pydantic import BaseModel, Field, HttpUrl

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.abc.ActionABC import (
    ActionABC,
    ActionConfigCreateType,
    ActionConfigType,
    ActionConfigUpdateType,
    ActionInputType,
    ActionOutputType,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.aws.aws_s3 import get_s3
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM

MAX_JSON_READER_OUTPUT_CHARS = 50_000


class JSONReadMode(str, Enum):
    auto = "auto"
    full = "full"
    preview = "preview"
    schema = "schema"
    page = "page"
    chunk = "chunk"


class JSONQueryReq(BaseModel):
    json_url: HttpUrl = Field(
        ...,
        description="HTTP/HTTPS URL pointing to a JSON resource.",
    )

    query: str | None = Field(
        default=None,
        max_length=3000,
        description=(
            "Optional JMESPath query to select part of the JSON. "
            "Prefer narrow queries such as `metadata`, `text_content`, "
            "`tables[0]`, `items[*].name`, etc."
        ),
    )

    mode: JSONReadMode = Field(
        default=JSONReadMode.auto,
        description=(
            "Read mode. Use `schema` to inspect structure, `preview` for a small sample, "
            "`page` for list results, `chunk` for long string results, "
            "`full` only for small selected data, and `auto` for safe default behavior."
        ),
    )

    page: Annotated[int, Field(ge=1, le=100_000)] = Field(
        default=1,
        description="1-based page number for list pagination when mode='page'.",
    )

    page_size: Annotated[int, Field(ge=1, le=100)] = Field(
        default=20,
        description="Number of list items returned per page when mode='page'.",
    )

    chunk_index: Annotated[int, Field(ge=1, le=100_000)] = Field(
        default=1,
        description="1-based chunk index for long string results when mode='chunk'.",
    )

    chunk_chars: Annotated[int, Field(ge=500, le=20_000)] = Field(
        default=10_000,
        description="Maximum characters returned for one string chunk when mode='chunk'.",
    )

    max_depth: Annotated[int, Field(ge=1, le=10)] = Field(
        default=4,
        description="Maximum depth for preview/schema generation.",
    )

    max_string_preview_chars: Annotated[int, Field(ge=50, le=5000)] = Field(
        default=1000,
        description="Maximum characters kept per string in preview mode.",
    )

    max_list_preview_items: Annotated[int, Field(ge=1, le=20)] = Field(
        default=5,
        description="Maximum list items shown in preview mode.",
    )

    max_object_preview_keys: Annotated[int, Field(ge=1, le=50)] = Field(
        default=20,
        description="Maximum object keys shown in preview mode.",
    )


class JSONNextRequest(BaseModel):
    json_url: str
    query: str | None = None
    mode: JSONReadMode

    page: int | None = None
    page_size: int | None = None

    chunk_index: int | None = None
    chunk_chars: int | None = None


class JSONQueryRes(BaseModel):
    status: Literal["complete", "partial", "error"]

    data: Any | None = None
    message: str | None = None

    query_used: str | None = None
    mode_used: JSONReadMode

    original_type: str | None = None
    json_shape: Any | None = None

    truncated: bool = False

    total_items: int | None = None
    page: int | None = None
    page_size: int | None = None
    has_next_page: bool = False

    total_chars: int | None = None
    chunk_index: int | None = None
    chunk_chars: int | None = None
    total_chunks: int | None = None
    has_next_chunk: bool = False

    next_request: JSONNextRequest | None = None
    warnings: list[str] = Field(default_factory=list)


class ActionJsonReader(
    ActionABC[JSONQueryReq, JSONQueryReq, JSONQueryReq, DataBlock, DataBlocks]
):
    type = ActionType.action_text2text_json_reader

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return JSONQueryReq

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return JSONQueryReq

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return JSONQueryReq

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    @staticmethod
    def get_description() -> str:
        return """
Read JSON from a URL and optionally apply a JMESPath query.

Use this tool as a JSON browser, not as a raw JSON dump tool.

Input
-----
{
  "json_url": "https://example.com/data.json",
  "query": "metadata",              // optional JMESPath query
  "mode": "auto",                  // auto | full | preview | schema | page | chunk
  "page": 1,                        // for mode="page"
  "page_size": 20,                  // for mode="page"
  "chunk_index": 1,                 // for mode="chunk"
  "chunk_chars": 10000              // for mode="chunk"
}

Behavior
--------
- Prefer narrow JMESPath queries instead of reading full JSON.
- mode="schema" returns shape/type information.
- mode="preview" returns a compact sample.
- mode="page" paginates list results.
- mode="chunk" chunks long string results such as text_content.
- mode="auto" returns full data if small, otherwise safely returns page/chunk/preview.
- mode="full" returns full data only if it fits inside the tool hard limit.

Output
------
Returns:
{
  "status": "complete" | "partial" | "error",
  "data": ...,
  "json_shape": ...,
  "truncated": true | false,
  "next_request": {...} | null
}

Usage rules
-----------
- If status="complete", the returned data is complete.
- If status="partial" and next_request is present, use next_request to continue.
- For large arrays, use mode="page".
- For long strings, use mode="chunk".
- For unknown JSON, start with mode="schema" or mode="preview".
- If you see "Output stored in file url" in output response , then you must use this tool with page or chunk mode otherwise due to large output, it will produce a s3 url of txt file.
"""

    # URL / download / query
    @staticmethod
    def validate_url(url: str) -> None:
        if not url.startswith("http://") and not url.startswith("https://"):
            raise HTTPException(
                status_code=400,
                detail="Invalid URL. Only http and https URLs are allowed.",
            )

    @staticmethod
    async def _download_private_s3_json(url: str, ctx: Context, user: UserORM) -> Any:
        # SECURITY NOTE:
        # This private-S3 fallback is safe only for explicitly supported user-scoped key
        # layouts like <prefix>/<user_id>/<filename> (e.g. action_runs/... or runs/...).
        # The bucket also contains non-user namespaces, so non-internal users should not
        # fall through to raw s3.get_bytes(ctx=ctx, key=key) for arbitrary derived keys.
        # Possible improvement: restrict non-internal access to an allowlist of known
        # user-owned prefixes and reserve raw key reads for internal users only.
        try:
            s3 = get_s3()
            is_internal = SettingsProvider.is_system_email(user.email)
            key = await s3.get_key(url)

            if not key:
                raise HTTPException(
                    status_code=403,
                    detail="HTTP 403 received and private S3 fallback could not derive an S3 key from the URL.",
                )

            key = key.lstrip("/")
            key_parts = key.split("/")

            file_bytes: bytes | None = None

            if len(key_parts) >= 3:
                top_level_folder = key_parts[0]
                user_id = key_parts[1]
                filename = "/".join(key_parts[2:])

                if user_id != user.id and not is_internal:
                    raise HTTPException(
                        status_code=403,
                        detail="Current logged-in user does not have permission to access this S3 file.",
                    )

                if "/" not in filename:
                    user_files = UserFiles(
                        user=UserORM(id=user_id),
                        s3=s3,
                        top_level_folder=top_level_folder,
                    )
                    file_bytes = await user_files.get_bytes(ctx=ctx, filename=filename)

            if not file_bytes:
                file_bytes = await s3.get_bytes(ctx=ctx, key=key)

            if not file_bytes:
                raise HTTPException(
                    status_code=404,
                    detail="Private S3 JSON file could not be read.",
                )

            try:
                return json.loads(file_bytes)
            except Exception as e:
                logger.bind(ctx=ctx, url=url, key=key).exception(
                    "Failed to parse private S3 JSON response"
                )
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to parse private S3 JSON response: {str(e)}",
                )

        except HTTPException:
            raise
        except Exception as e:
            logger.bind(ctx=ctx, url=url).exception("Error reading private S3 JSON")
            raise HTTPException(
                status_code=500,
                detail=f"Error reading private S3 JSON: {str(e)}",
            )

    @staticmethod
    async def download_json(url: str, ctx: Context , user: UserORM) -> Any:
        try:
            async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as client:
                response = await client.get(
                    url,
                    headers={
                        "Accept": "application/json,text/json,*/*",
                    },
                )

            if response.status_code != 200:
                if response.status_code == 403:
                    logger.bind(ctx=ctx, url=url).warning(
                        "HTTP 403 while downloading JSON. Attempting private S3 fallback."
                    )
                    return await ActionJsonReader._download_private_s3_json(url, ctx , user)

                logger.bind(
                    ctx=ctx,
                    url=url,
                    status_code=response.status_code,
                ).error("Failed to download JSON")

                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Failed to download JSON. HTTP Status: {response.status_code}",
                )

            try:
                return response.json()
            except Exception as e:
                logger.bind(ctx=ctx, url=url).exception("Failed to parse JSON response")
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to parse JSON response: {str(e)}",
                )

        except HTTPException:
            raise
        except Exception as e:
            logger.bind(ctx=ctx, url=url).exception("Error downloading JSON")
            raise HTTPException(
                status_code=500,
                detail=f"Error downloading JSON: {e}",
            )
    @staticmethod
    def run_query(data: Any, query: str | None) -> Any:
        if query is None or query.strip() == "":
            return data

        try:
            return jmespath.search(query, data)
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid JMESPath query: {str(e)}",
            )

    # JSON helpers
    @staticmethod
    def _json_dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)

    @staticmethod
    def _type_name(value: Any) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "bool"
        if isinstance(value, int):
            return "int"
        if isinstance(value, float):
            return "float"
        if isinstance(value, str):
            return "str"
        if isinstance(value, list):
            return "list"
        if isinstance(value, dict):
            return "object"
        return type(value).__name__

    @staticmethod
    def _summary(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                "type": "object",
                "key_count": len(value),
                "keys_sample": list(map(str, list(value.keys())[:20])),
            }

        if isinstance(value, list):
            return {
                "type": "list",
                "length": len(value),
            }

        if isinstance(value, str):
            return {
                "type": "str",
                "chars": len(value),
                "sample": value[:150] + ("..." if len(value) > 150 else ""),
            }

        return {
            "type": ActionJsonReader._type_name(value),
            "value_sample": value,
        }

    @staticmethod
    def _shape(
        value: Any,
        *,
        depth: int,
        max_depth: int,
        max_keys: int = 30,
    ) -> Any:
        if depth >= max_depth:
            return ActionJsonReader._summary(value)

        if isinstance(value, dict):
            out: dict[str, Any] = {}
            keys = list(value.keys())

            for key in keys[:max_keys]:
                out[str(key)] = ActionJsonReader._shape(
                    value[key],
                    depth=depth + 1,
                    max_depth=max_depth,
                    max_keys=max_keys,
                )

            if len(keys) > max_keys:
                out["__truncated_keys__"] = f"{len(keys) - max_keys} more keys"

            return {
                "type": "object",
                "key_count": len(keys),
                "keys": out,
            }

        if isinstance(value, list):
            sample_shape = None

            if value:
                sample_shape = ActionJsonReader._shape(
                    value[0],
                    depth=depth + 1,
                    max_depth=max_depth,
                    max_keys=max_keys,
                )

            return {
                "type": "list",
                "length": len(value),
                "item_sample_shape": sample_shape,
            }

        if isinstance(value, str):
            return {
                "type": "str",
                "chars": len(value),
                "sample": value[:150] + ("..." if len(value) > 150 else ""),
            }

        return {
            "type": ActionJsonReader._type_name(value),
            "value_sample": value,
        }

    @staticmethod
    def _preview(
        value: Any,
        *,
        depth: int,
        max_depth: int,
        max_string_chars: int,
        max_list_items: int,
        max_object_keys: int,
    ) -> Any:
        if depth >= max_depth:
            return ActionJsonReader._summary(value)

        if isinstance(value, str):
            if len(value) <= max_string_chars:
                return value

            return (
                value[:max_string_chars]
                + f"... [truncated string: {len(value)} total chars]"
            )

        if isinstance(value, list):
            preview_items = [
                ActionJsonReader._preview(
                    item,
                    depth=depth + 1,
                    max_depth=max_depth,
                    max_string_chars=max_string_chars,
                    max_list_items=max_list_items,
                    max_object_keys=max_object_keys,
                )
                for item in value[:max_list_items]
            ]

            if len(value) > max_list_items:
                preview_items.append(
                    {
                        "__truncated_items__": (
                            f"{len(value) - max_list_items} more items"
                        )
                    }
                )

            return preview_items

        if isinstance(value, dict):
            out: dict[str, Any] = {}
            items = list(value.items())

            for key, item_value in items[:max_object_keys]:
                out[str(key)] = ActionJsonReader._preview(
                    item_value,
                    depth=depth + 1,
                    max_depth=max_depth,
                    max_string_chars=max_string_chars,
                    max_list_items=max_list_items,
                    max_object_keys=max_object_keys,
                )

            if len(items) > max_object_keys:
                out["__truncated_keys__"] = f"{len(items) - max_object_keys} more keys"

            return out

        return value

    @staticmethod
    def _response_len(response: JSONQueryRes) -> int:
        try:
            return len(
                response.model_dump_json(
                    exclude_none=True,
                    ensure_ascii=False,
                )
            )
        except Exception:
            return len(str(response))

    # Next request helpers
    @staticmethod
    def _next_page_request(
        cfg: JSONQueryReq,
        *,
        next_page: int,
    ) -> JSONNextRequest:
        return JSONNextRequest(
            json_url=str(cfg.json_url),
            query=cfg.query,
            mode=JSONReadMode.page,
            page=next_page,
            page_size=cfg.page_size,
        )

    @staticmethod
    def _next_chunk_request(
        cfg: JSONQueryReq,
        *,
        next_chunk_index: int,
    ) -> JSONNextRequest:
        return JSONNextRequest(
            json_url=str(cfg.json_url),
            query=cfg.query,
            mode=JSONReadMode.chunk,
            chunk_index=next_chunk_index,
            chunk_chars=cfg.chunk_chars,
        )

    # Response builders
    @staticmethod
    def _complete_response(selected: Any, cfg: JSONQueryReq) -> JSONQueryRes:
        return JSONQueryRes(
            status="complete",
            data=selected,
            message="Selected JSON result returned completely.",
            query_used=cfg.query,
            mode_used=cfg.mode,
            original_type=ActionJsonReader._type_name(selected),
            json_shape=ActionJsonReader._shape(
                selected,
                depth=0,
                max_depth=cfg.max_depth,
            ),
            truncated=False,
        )

    @staticmethod
    def _schema_response(selected: Any, cfg: JSONQueryReq) -> JSONQueryRes:
        return JSONQueryRes(
            status="complete",
            data=None,
            message="Returned JSON schema/shape only.",
            query_used=cfg.query,
            mode_used=JSONReadMode.schema,
            original_type=ActionJsonReader._type_name(selected),
            json_shape=ActionJsonReader._shape(
                selected,
                depth=0,
                max_depth=cfg.max_depth,
            ),
            truncated=False,
        )

    @staticmethod
    def _preview_response(
        selected: Any,
        cfg: JSONQueryReq,
        *,
        message: str | None = None,
    ) -> JSONQueryRes:
        return JSONQueryRes(
            status="partial",
            data=ActionJsonReader._preview(
                selected,
                depth=0,
                max_depth=cfg.max_depth,
                max_string_chars=cfg.max_string_preview_chars,
                max_list_items=cfg.max_list_preview_items,
                max_object_keys=cfg.max_object_preview_keys,
            ),
            message=message
            or (
                "Selected JSON result was too large. Returned a compact preview. "
                "Use a narrower JMESPath query, mode='page' for lists, or mode='chunk' for strings."
            ),
            query_used=cfg.query,
            mode_used=cfg.mode,
            original_type=ActionJsonReader._type_name(selected),
            json_shape=ActionJsonReader._shape(
                selected,
                depth=0,
                max_depth=cfg.max_depth,
            ),
            truncated=True,
        )

    @staticmethod
    def _page_response(selected: Any, cfg: JSONQueryReq) -> JSONQueryRes:
        if not isinstance(selected, list):
            return JSONQueryRes(
                status="partial",
                data=ActionJsonReader._preview(
                    selected,
                    depth=0,
                    max_depth=cfg.max_depth,
                    max_string_chars=cfg.max_string_preview_chars,
                    max_list_items=cfg.max_list_preview_items,
                    max_object_keys=cfg.max_object_preview_keys,
                ),
                message=(
                    "mode='page' was requested, but the selected JSON result is not a list. "
                    "Returned preview instead."
                ),
                query_used=cfg.query,
                mode_used=JSONReadMode.page,
                original_type=ActionJsonReader._type_name(selected),
                json_shape=ActionJsonReader._shape(
                    selected,
                    depth=0,
                    max_depth=cfg.max_depth,
                ),
                truncated=True,
                warnings=["mode='page' only works directly on list results."],
            )

        total_items = len(selected)
        start = (cfg.page - 1) * cfg.page_size
        end = start + cfg.page_size
        page_items = selected[start:end]
        has_next_page = end < total_items

        response = JSONQueryRes(
            status="partial" if has_next_page else "complete",
            data=page_items,
            message=(
                f"Returned page {cfg.page} with {len(page_items)} item(s) "
                f"from {total_items} total item(s)."
            ),
            query_used=cfg.query,
            mode_used=JSONReadMode.page,
            original_type="list",
            json_shape=ActionJsonReader._shape(
                selected,
                depth=0,
                max_depth=cfg.max_depth,
            ),
            truncated=has_next_page,
            total_items=total_items,
            page=cfg.page,
            page_size=cfg.page_size,
            has_next_page=has_next_page,
            next_request=(
                ActionJsonReader._next_page_request(
                    cfg,
                    next_page=cfg.page + 1,
                )
                if has_next_page
                else None
            ),
        )

        if ActionJsonReader._response_len(response) <= MAX_JSON_READER_OUTPUT_CHARS:
            return response

        # Current page is still too large. Compact the page itself.
        response.data = ActionJsonReader._preview(
            page_items,
            depth=0,
            max_depth=min(cfg.max_depth, 3),
            max_string_chars=min(cfg.max_string_preview_chars, 500),
            max_list_items=min(cfg.max_list_preview_items, 3),
            max_object_keys=min(cfg.max_object_preview_keys, 10),
        )
        response.status = "partial"
        response.truncated = True
        response.message = (
            f"Page {cfg.page} was still too large, so returned a compact preview "
            f"of the page. Reduce page_size or use a narrower JMESPath query."
        )
        response.warnings.append(
            "Page output was compacted to stay under json_reader hard output limit."
        )

        return response

    @staticmethod
    def _chunk_response(selected: Any, cfg: JSONQueryReq) -> JSONQueryRes:
        if not isinstance(selected, str):
            return JSONQueryRes(
                status="partial",
                data=ActionJsonReader._preview(
                    selected,
                    depth=0,
                    max_depth=cfg.max_depth,
                    max_string_chars=cfg.max_string_preview_chars,
                    max_list_items=cfg.max_list_preview_items,
                    max_object_keys=cfg.max_object_preview_keys,
                ),
                message=(
                    "mode='chunk' was requested, but the selected JSON result is not a string. "
                    "Returned preview instead."
                ),
                query_used=cfg.query,
                mode_used=JSONReadMode.chunk,
                original_type=ActionJsonReader._type_name(selected),
                json_shape=ActionJsonReader._shape(
                    selected,
                    depth=0,
                    max_depth=cfg.max_depth,
                ),
                truncated=True,
                warnings=["mode='chunk' only works directly on string results."],
            )

        total_chars = len(selected)
        total_chunks = max(1, math.ceil(total_chars / cfg.chunk_chars))

        start = (cfg.chunk_index - 1) * cfg.chunk_chars
        end = start + cfg.chunk_chars

        chunk = selected[start:end]
        has_next_chunk = cfg.chunk_index < total_chunks

        response = JSONQueryRes(
            status="partial" if has_next_chunk else "complete",
            data=chunk,
            message=(
                f"Returned chunk {cfg.chunk_index} of {total_chunks} "
                f"from selected string result."
            ),
            query_used=cfg.query,
            mode_used=JSONReadMode.chunk,
            original_type="str",
            json_shape=ActionJsonReader._shape(
                selected,
                depth=0,
                max_depth=cfg.max_depth,
            ),
            truncated=has_next_chunk,
            total_chars=total_chars,
            chunk_index=cfg.chunk_index,
            chunk_chars=cfg.chunk_chars,
            total_chunks=total_chunks,
            has_next_chunk=has_next_chunk,
            next_request=(
                ActionJsonReader._next_chunk_request(
                    cfg,
                    next_chunk_index=cfg.chunk_index + 1,
                )
                if has_next_chunk
                else None
            ),
        )

        if ActionJsonReader._response_len(response) <= MAX_JSON_READER_OUTPUT_CHARS:
            return response

        # This should rarely happen because chunk_chars is capped.
        safe_chunk_chars = max(500, cfg.chunk_chars // 2)
        safe_start = (cfg.chunk_index - 1) * safe_chunk_chars
        safe_end = safe_start + safe_chunk_chars
        safe_chunk = selected[safe_start:safe_end]
        safe_total_chunks = max(1, math.ceil(total_chars / safe_chunk_chars))
        safe_has_next_chunk = cfg.chunk_index < safe_total_chunks

        return JSONQueryRes(
            status="partial" if safe_has_next_chunk else "complete",
            data=safe_chunk,
            message=(
                f"Original chunk was too large after JSON serialization. "
                f"Returned safer chunk {cfg.chunk_index} of {safe_total_chunks}."
            ),
            query_used=cfg.query,
            mode_used=JSONReadMode.chunk,
            original_type="str",
            json_shape=ActionJsonReader._shape(
                selected,
                depth=0,
                max_depth=cfg.max_depth,
            ),
            truncated=safe_has_next_chunk,
            total_chars=total_chars,
            chunk_index=cfg.chunk_index,
            chunk_chars=safe_chunk_chars,
            total_chunks=safe_total_chunks,
            has_next_chunk=safe_has_next_chunk,
            next_request=(
                JSONNextRequest(
                    json_url=str(cfg.json_url),
                    query=cfg.query,
                    mode=JSONReadMode.chunk,
                    chunk_index=cfg.chunk_index + 1,
                    chunk_chars=safe_chunk_chars,
                )
                if safe_has_next_chunk
                else None
            ),
            warnings=[
                "Chunk size was reduced to stay under json_reader hard output limit."
            ],
        )

    @staticmethod
    def _force_fit_response(
        response: JSONQueryRes,
        selected: Any,
        cfg: JSONQueryReq,
    ) -> JSONQueryRes:
        """
        Final safety gate.

        This keeps json_reader output under MAX_JSON_READER_OUTPUT_CHARS so ToolFactory
        should not need to offload this tool's output during normal operation.
        """
        if ActionJsonReader._response_len(response) <= MAX_JSON_READER_OUTPUT_CHARS:
            return response

        compact = JSONQueryRes(
            status="partial",
            data=ActionJsonReader._preview(
                selected,
                depth=0,
                max_depth=min(cfg.max_depth, 3),
                max_string_chars=min(cfg.max_string_preview_chars, 500),
                max_list_items=min(cfg.max_list_preview_items, 3),
                max_object_keys=min(cfg.max_object_preview_keys, 10),
            ),
            message=(
                "The selected JSON result exceeded json_reader hard output limit. "
                "Returned compact preview. Use a narrower JMESPath query, mode='page', "
                "or mode='chunk'."
            ),
            query_used=cfg.query,
            mode_used=cfg.mode,
            original_type=ActionJsonReader._type_name(selected),
            json_shape=ActionJsonReader._shape(
                selected,
                depth=0,
                max_depth=min(cfg.max_depth, 3),
            ),
            truncated=True,
            warnings=[
                "Output was compacted inside json_reader to stay under hard output limit."
            ],
        )

        if ActionJsonReader._response_len(compact) <= MAX_JSON_READER_OUTPUT_CHARS:
            return compact

        minimal = JSONQueryRes(
            status="partial",
            data=None,
            message=(
                "The selected JSON result is too large to return directly. "
                "Use mode='schema' to inspect structure, then use a narrower JMESPath query. "
                "For lists use mode='page'. For long strings use mode='chunk'."
            ),
            query_used=cfg.query,
            mode_used=cfg.mode,
            original_type=ActionJsonReader._type_name(selected),
            json_shape=ActionJsonReader._summary(selected),
            truncated=True,
            warnings=[
                "Minimal response returned because compact preview exceeded hard output limit."
            ],
        )

        return minimal

    @staticmethod
    def build_response(selected: Any, cfg: JSONQueryReq) -> JSONQueryRes:
        if cfg.mode == JSONReadMode.schema:
            return ActionJsonReader._force_fit_response(
                ActionJsonReader._schema_response(selected, cfg),
                selected,
                cfg,
            )

        if cfg.mode == JSONReadMode.preview:
            return ActionJsonReader._force_fit_response(
                ActionJsonReader._preview_response(selected, cfg),
                selected,
                cfg,
            )

        if cfg.mode == JSONReadMode.page:
            return ActionJsonReader._force_fit_response(
                ActionJsonReader._page_response(selected, cfg),
                selected,
                cfg,
            )

        if cfg.mode == JSONReadMode.chunk:
            return ActionJsonReader._force_fit_response(
                ActionJsonReader._chunk_response(selected, cfg),
                selected,
                cfg,
            )

        if cfg.mode == JSONReadMode.full:
            full_response = ActionJsonReader._complete_response(selected, cfg)

            if ActionJsonReader._response_len(full_response) <= MAX_JSON_READER_OUTPUT_CHARS:
                return full_response

            return ActionJsonReader._force_fit_response(
                ActionJsonReader._preview_response(
                    selected,
                    cfg,
                    message=(
                        "mode='full' was requested, but the selected JSON result is too large. "
                        "Returned preview instead."
                    ),
                ),
                selected,
                cfg,
            )

        # mode == auto
        full_response = ActionJsonReader._complete_response(selected, cfg)

        if ActionJsonReader._response_len(full_response) <= MAX_JSON_READER_OUTPUT_CHARS:
            return full_response

        if isinstance(selected, list):
            return ActionJsonReader._force_fit_response(
                ActionJsonReader._page_response(selected, cfg),
                selected,
                cfg,
            )

        if isinstance(selected, str):
            return ActionJsonReader._force_fit_response(
                ActionJsonReader._chunk_response(selected, cfg),
                selected,
                cfg,
            )

        return ActionJsonReader._force_fit_response(
            ActionJsonReader._preview_response(
                selected,
                cfg,
                message=(
                    "Selected JSON result is too large for direct return. "
                    "Returned preview automatically. Use mode='schema' to inspect structure "
                    "or a narrower JMESPath query to extract specific fields."
                ),
            ),
            selected,
            cfg,
        )

    async def run_action(
        self,
        ctx: Context,
        app_state: AppState,
        action_input: DataBlock,
        notify: Notify | None = None,
    ) -> DataBlocks | Exception:
        try:
            if not action_input.urls:
                raise HTTPException(
                    status_code=400,
                    detail="No JSON URL provided in action_input.urls.",
                )

            url = action_input.urls[0]
            self.validate_url(url)

            cfg = JSONQueryReq(
                json_url=url,
                query=self.action_config.query,
                mode=self.action_config.mode,
                page=self.action_config.page,
                page_size=self.action_config.page_size,
                chunk_index=self.action_config.chunk_index,
                chunk_chars=self.action_config.chunk_chars,
                max_depth=self.action_config.max_depth,
                max_string_preview_chars=self.action_config.max_string_preview_chars,
                max_list_preview_items=self.action_config.max_list_preview_items,
                max_object_preview_keys=self.action_config.max_object_preview_keys,
            )

            json_data = await ActionJsonReader.download_json(str(cfg.json_url), ctx , self.user)
            selected = ActionJsonReader.run_query(json_data, cfg.query)
            response = ActionJsonReader.build_response(selected, cfg)

            return DataBlocks(
                blocks=[
                    DataBlock(
                        text=response.model_dump_json(
                            exclude_none=True,
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                ]
            )

        except Exception as e:
            logger.bind(ctx=ctx).exception(
                f"Error in running JSON query action: {str(e)}"
            )
            return e

    async def run_tool(
        self,
        action_config: JSONQueryReq,
        ctx: Context,
        app_state: AppState,
        notify: Notify | None = None,
    ) -> JSONQueryRes | Exception:
        try:
            url = str(action_config.json_url)
            self.validate_url(url)

            json_data = await ActionJsonReader.download_json(url, ctx , self.user)
            selected = ActionJsonReader.run_query(json_data, action_config.query)

            return ActionJsonReader.build_response(selected, action_config)

        except HTTPException as e:
            logger.bind(ctx=ctx).exception(
                f"HTTP error in running JSON query tool: {str(e)}"
            )
            return JSONQueryRes(
                status="error",
                data=None,
                message=str(e.detail),
                query_used=action_config.query,
                mode_used=action_config.mode,
                truncated=False,
            )

        except Exception as e:
            logger.bind(ctx=ctx).exception(
                f"Error in running JSON query tool: {str(e)}"
            )
            return JSONQueryRes(
                status="error",
                data=None,
                message=str(e),
                query_used=action_config.query,
                mode_used=action_config.mode,
                truncated=False,
            )
