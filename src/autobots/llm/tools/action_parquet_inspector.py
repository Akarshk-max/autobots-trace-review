import asyncio
import base64
import json
import math
import re
import socket
import threading
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, List, Literal, Type
from urllib.parse import urljoin, urlparse

import duckdb
import httpx
import pandas as pd
from fastapi import HTTPException
from loguru import logger
from pydantic import BaseModel, Field, field_validator, model_validator

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
from src.autobots.core.cancellation.blocking_work import run_blocking
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify

MAX_LIMIT = 1500
DEFAULT_LIMIT = 50
HTTP_TIMEOUT_SECONDS = 30
MAX_REDIRECTS = 5
ALLOW_PRIVATE_NETWORKS = False


class ParquetInspectorReq(BaseModel):
    parquet_url: str = Field(..., description="HTTP/HTTPS URL to the parquet file")
    operation: Literal["schema", "preview", "query"] = "schema"
    query: str | None = None
    columns: List[str] | None = None
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)

    @field_validator("parquet_url")
    @classmethod
    def validate_parquet_url(cls, value: str) -> str:
        value = value.strip()
        parsed = urlparse(value)

        if parsed.scheme not in {"http", "https"}:
            raise ValueError("Only http:// and https:// parquet URLs are allowed.")

        if not parsed.netloc or not parsed.hostname:
            raise ValueError("Invalid parquet URL.")

        return value

    @field_validator("columns")
    @classmethod
    def validate_columns(cls, value: List[str] | None) -> List[str] | None:
        if value is None:
            return None

        cleaned: list[str] = []
        seen: set[str] = set()

        for col in value:
            if col is None:
                raise ValueError("Column names cannot be null.")

            col = col.strip()
            if not col:
                raise ValueError("Column names cannot be empty.")

            if col not in seen:
                cleaned.append(col)
                seen.add(col)

        return cleaned

    @model_validator(mode="after")
    def validate_query_requirements(self) -> "ParquetInspectorReq":
        if self.operation == "query" and not self.query:
            raise ValueError("The 'query' field is required when operation='query'.")
        return self


class ParquetInspectorRes(BaseModel):
    result: Any


class ActionParquetInspector(
    ActionABC[
        ParquetInspectorReq,
        ParquetInspectorReq,
        ParquetInspectorReq,
        DataBlock,
        DataBlocks,
    ]
):
    type = ActionType.action_text2text_parquet_inspector

    _duckdb_extension_lock = threading.Lock()

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return ParquetInspectorReq

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return ParquetInspectorReq

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return ParquetInspectorReq

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    @staticmethod
    def get_description() -> str:
        return """
Inspect, preview, and safely query Parquet files over HTTP/HTTPS.

Description
-----------
This tool allows structured inspection and read-only querying of Parquet datasets.

Capabilities
------------
- Inspect schema
- Preview rows
- Select specific columns
- Execute read-only SQL queries against a temporary table called `parquet_file`

Input
-----
{
    "parquet_url": "https://example.com/data.parquet",
    "operation": "schema | preview | query",
    "columns": ["col1", "col2"],
    "limit": 50,
    "query": "SELECT col1, col2 FROM parquet_file WHERE col3 IS NOT NULL LIMIT 10"
}

Fields
------
parquet_url
HTTP/HTTPS URL pointing to a parquet file.

operation
Operation mode:
- schema
- preview
- query

columns
Optional list of columns to return for preview.

limit
Maximum number of rows returned.
Hard-capped at 1500.

query
Read-only SQL query.
The query must reference the temporary table `parquet_file`.

Output
------
{
  "result": <query result>
}
"""

    async def run_action(
        self,
        ctx: Context,
        app_state: AppState,
        action_input: DataBlock,
        notify: Notify | None = None,
    ) -> DataBlocks | Exception:
        try:
            parquet_url = self._resolve_action_url(action_input)
            operation = self.action_config.operation
            columns = self.action_config.columns
            limit = self.action_config.limit
            query = self.action_config.query

            if notify:
                notify(ctx, f"parquet_inspector:operation={operation}")

            result = await self._run_parquet_operation(
                ctx=ctx,
                app_state=app_state,
                parquet_url=parquet_url,
                operation=operation,
                columns=columns,
                limit=limit,
                query=query,
            )

            if notify:
                notify(ctx, f"parquet_inspector:completed rows={len(result) if isinstance(result, list) else 1}")

            return DataBlocks(
                blocks=[
                    DataBlock(
                        text=json.dumps(
                            result,
                            indent=2,
                            ensure_ascii=False,
                            allow_nan=False,
                        )
                    )
                ]
            )

        except Exception as e:
            logger.bind(ctx=ctx).exception(
                f"Error running parquet inspector action: {str(e)}"
            )
            return e

    async def run_tool(
        self,
        action_config: ParquetInspectorReq,
        ctx: Context,
        app_state: AppState,
        notify: Notify | None = None,
    ) -> ParquetInspectorRes | Exception:
        try:
            if notify:
                notify(ctx, f"parquet_inspector:operation={action_config.operation}")

            result = await self._run_parquet_operation(
                ctx=ctx,
                app_state=app_state,
                parquet_url=action_config.parquet_url,
                operation=action_config.operation,
                columns=action_config.columns,
                limit=action_config.limit,
                query=action_config.query,
            )

            if notify:
                notify(ctx, f"parquet_inspector:completed rows={len(result) if isinstance(result, list) else 1}")

            return ParquetInspectorRes(result=result)

        except Exception as e:
            logger.bind(ctx=ctx).exception(
                f"Error running parquet inspector tool: {str(e)}"
            )
            return e

    async def _run_parquet_operation(
        self,
        ctx: Context,
        app_state: AppState,
        parquet_url: str,
        operation: Literal["schema", "preview", "query"],
        columns: list[str] | None,
        limit: int,
        query: str | None,
    ) -> list[dict[str, Any]]:
        self._validate_url_shape(parquet_url)
        await self._validate_remote_access(parquet_url, ctx)

        return await run_blocking(
            app_state,
            self._run_parquet_operation_sync,
            ctx,
            parquet_url,
            operation,
            columns,
            limit,
            query,
        )

    def _run_parquet_operation_sync(
        self,
        ctx: Context,
        parquet_url: str,
        operation: Literal["schema", "preview", "query"],
        columns: list[str] | None,
        limit: int,
        query: str | None,
    ) -> list[dict[str, Any]]:
        conn = None
        try:
            conn = self._open_duckdb_connection()

            if operation == "schema":
                result = self._execute_schema_query(conn, parquet_url)

            elif operation == "preview":
                result = self._execute_preview_query(
                    conn=conn,
                    url=parquet_url,
                    columns=columns,
                    limit=limit,
                )

            elif operation == "query":
                result = self._execute_custom_query(
                    conn=conn,
                    url=parquet_url,
                    query=query,
                    limit=limit,
                )

            else:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid operation. Use schema, preview, or query.",
                )

            return result

        except HTTPException:
            raise
        except Exception as e:
            logger.bind(ctx=ctx).exception(
                f"Unexpected parquet execution failure: {str(e)}"
            )
            raise HTTPException(
                status_code=500,
                detail="Failed to inspect or query parquet file.",
            ) from e
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    logger.bind(ctx=ctx).warning("Failed to close DuckDB connection cleanly.")

    @staticmethod
    def _validate_url_shape(url: str) -> None:
        parsed = urlparse(url)

        if parsed.scheme not in {"http", "https"}:
            raise HTTPException(
                status_code=400,
                detail="Invalid URL. Only http and https URLs are allowed.",
            )

        if not parsed.netloc or not parsed.hostname:
            raise HTTPException(
                status_code=400,
                detail="Invalid URL.",
            )

    @classmethod
    async def _validate_remote_access(cls, url: str, ctx: Context) -> None:
        try:
            timeout = httpx.Timeout(
                connect=10.0,
                read=20.0,
                write=10.0,
                pool=10.0,
            )

            current_url = url
            redirects_followed = 0

            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                while True:
                    cls._validate_url_shape(current_url)
                    await cls._assert_public_url(current_url)

                    # First try HEAD
                    head_response = await client.head(current_url)

                    if cls._is_redirect(head_response.status_code):
                        location = head_response.headers.get("location")
                        if not location:
                            raise HTTPException(
                                status_code=502,
                                detail="Redirect response missing Location header.",
                            )

                        redirects_followed += 1
                        if redirects_followed > MAX_REDIRECTS:
                            raise HTTPException(
                                status_code=502,
                                detail="Too many redirects while validating parquet URL.",
                            )

                        current_url = urljoin(current_url, location)
                        continue

                    if head_response.status_code < 400:
                        return

                    get_response = await client.get(
                        current_url,
                        headers={"Range": "bytes=0-0"},
                    )

                    if cls._is_redirect(get_response.status_code):
                        location = get_response.headers.get("location")
                        if not location:
                            raise HTTPException(
                                status_code=502,
                                detail="Redirect response missing Location header.",
                            )

                        redirects_followed += 1
                        if redirects_followed > MAX_REDIRECTS:
                            raise HTTPException(
                                status_code=502,
                                detail="Too many redirects while validating parquet URL.",
                            )

                        current_url = urljoin(current_url, location)
                        continue

                    if get_response.status_code >= 400:
                        raise HTTPException(
                            status_code=get_response.status_code,
                            detail="Unable to access parquet file.",
                        )

                    return

        except HTTPException:
            raise
        except httpx.HTTPError as e:
            logger.bind(ctx=ctx).exception("Parquet URL validation failed due to HTTP error")
            raise HTTPException(
                status_code=502,
                detail=f"Failed to reach parquet URL: {str(e)}",
            ) from e
        except Exception as e:
            logger.bind(ctx=ctx).exception("Parquet URL validation failed")
            raise HTTPException(
                status_code=500,
                detail="Parquet URL validation failed.",
            ) from e

    @staticmethod
    def _is_redirect(status_code: int) -> bool:
        return status_code in {301, 302, 303, 307, 308}

    @classmethod
    async def _assert_public_url(cls, url: str) -> None:
        if ALLOW_PRIVATE_NETWORKS:
            return

        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            raise HTTPException(status_code=400, detail="Invalid URL hostname.")

        await asyncio.to_thread(cls._assert_public_hostname_sync, hostname)

    @staticmethod
    def _assert_public_hostname_sync(hostname: str) -> None:
        lowered = hostname.lower()
        if lowered in {"localhost", "localhost.localdomain"}:
            raise HTTPException(
                status_code=400,
                detail="Private/local URLs are not allowed.",
            )

        try:
            addrinfos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
        except socket.gaierror as e:
            raise HTTPException(
                status_code=400,
                detail=f"Unable to resolve hostname: {hostname}",
            ) from e

        if not addrinfos:
            raise HTTPException(
                status_code=400,
                detail=f"Unable to resolve hostname: {hostname}",
            )

        # for addrinfo in addrinfos:
        #     sockaddr = addrinfo[4]
        #     ip_str = sockaddr[0]
        #     ip_obj = ip_address(ip_str)

        #     if (
        #         ip_obj.is_private
        #         or ip_obj.is_loopback
        #         or ip_obj.is_link_local
        #         or ip_obj.is_multicast
        #         or ip_obj.is_reserved
        #         or ip_obj.is_unspecified
        #     ):
        #         raise HTTPException(
        #             status_code=400,
        #             detail="Private/local URLs are not allowed.",
        #         )

    @classmethod
    def _open_duckdb_connection(cls) -> duckdb.DuckDBPyConnection:
        conn = duckdb.connect(database=":memory:")
        cls._ensure_httpfs_loaded(conn)

        # Conservative session settings.
        conn.execute("SET enable_progress_bar = false")
        conn.execute("PRAGMA disable_progress_bar")
        return conn

    @classmethod
    def _ensure_httpfs_loaded(cls, conn: duckdb.DuckDBPyConnection) -> None:
        try:
            conn.execute("LOAD httpfs")
            return
        except Exception:
            pass

        with cls._duckdb_extension_lock:
            try:
                conn.execute("INSTALL httpfs")
            except Exception:
                pass

        conn.execute("LOAD httpfs")

    @staticmethod
    def _sql_string_literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    @staticmethod
    def _quote_identifier(identifier: str) -> str:
        if not identifier or not identifier.strip():
            raise HTTPException(status_code=400, detail="Invalid column name.")
        return '"' + identifier.replace('"', '""') + '"'

    @staticmethod
    def _enforce_limit(limit: int) -> int:
        if limit <= 0:
            return DEFAULT_LIMIT
        return min(limit, MAX_LIMIT)

    @classmethod
    def _normalize_read_only_query(cls, query: str) -> str:
        if not query or not query.strip():
            raise HTTPException(
                status_code=400,
                detail="Query operation requires a SQL query.",
            )

        q = query.strip().rstrip(";").strip()

        if ";" in q:
            raise HTTPException(
                status_code=400,
                detail="Multiple SQL statements are not allowed.",
            )

        scrubbed = re.sub(r"/\*.*?\*/", " ", q, flags=re.DOTALL)
        scrubbed = re.sub(r"--.*?$", " ", scrubbed, flags=re.MULTILINE)
        lowered = scrubbed.strip().lower()

        if not lowered.startswith(("select", "with")):
            raise HTTPException(
                status_code=400,
                detail="Only read-only SELECT queries are allowed.",
            )

        if not re.search(r"\bparquet_file\b", lowered):
            raise HTTPException(
                status_code=400,
                detail="Query must reference the temporary table 'parquet_file'.",
            )

        denied_keywords = {
            "insert",
            "update",
            "delete",
            "create",
            "replace",
            "drop",
            "alter",
            "attach",
            "detach",
            "copy",
            "pragma",
            "install",
            "load",
            "call",
            "export",
            "import",
            "vacuum",
            "checkpoint",
            "set",
        }

        for keyword in denied_keywords:
            if re.search(rf"\b{re.escape(keyword)}\b", lowered):
                raise HTTPException(
                    status_code=400,
                    detail=f"Forbidden SQL keyword detected: {keyword}",
                )

        return q

    @classmethod
    def _build_parquet_source(cls, url: str) -> str:
        return f"read_parquet({cls._sql_string_literal(url)})"

    @classmethod
    def _execute_schema_query(
        cls,
        conn: duckdb.DuckDBPyConnection,
        url: str,
    ) -> list[dict[str, Any]]:
        source = cls._build_parquet_source(url)
        query = f"DESCRIBE SELECT * FROM {source}"
        df = conn.execute(query).fetchdf()
        return cls._dataframe_to_jsonable_records(df)

    @classmethod
    def _get_schema_column_names(
        cls,
        conn: duckdb.DuckDBPyConnection,
        url: str,
    ) -> set[str]:
        rows = cls._execute_schema_query(conn, url)
        names = set()

        for row in rows:
            # DuckDB DESCRIBE commonly returns "column_name"
            name = row.get("column_name")
            if isinstance(name, str):
                names.add(name)

        return names

    @classmethod
    def _execute_preview_query(
        cls,
        conn: duckdb.DuckDBPyConnection,
        url: str,
        columns: list[str] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        limit = cls._enforce_limit(limit)
        source = cls._build_parquet_source(url)

        if columns:
            available_columns = cls._get_schema_column_names(conn, url)
            missing = [c for c in columns if c not in available_columns]
            if missing:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown columns requested: {missing}",
                )

            cols_sql = ", ".join(cls._quote_identifier(c) for c in columns)
        else:
            cols_sql = "*"

        query = f"SELECT {cols_sql} FROM {source} LIMIT {limit}"
        df = conn.execute(query).fetchdf()
        return cls._dataframe_to_jsonable_records(df)

    @classmethod
    def _execute_custom_query(
        cls,
        conn: duckdb.DuckDBPyConnection,
        url: str,
        query: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        safe_query = cls._normalize_read_only_query(query or "")
        limit = cls._enforce_limit(limit)

        source = cls._build_parquet_source(url)
        conn.execute(
            f"CREATE OR REPLACE TEMP VIEW parquet_file AS SELECT * FROM {source}"
        )
        wrapped_query = f"""
            SELECT *
            FROM ({safe_query}) AS parquet_query_result
            LIMIT {limit}
        """

        df = conn.execute(wrapped_query).fetchdf()
        return cls._dataframe_to_jsonable_records(df)

    @classmethod
    def _dataframe_to_jsonable_records(cls, df: pd.DataFrame) -> list[dict[str, Any]]:
        records = df.to_dict(orient="records")
        return [cls._normalize_jsonable(record) for record in records]

    @classmethod
    def _normalize_jsonable(cls, value: Any) -> Any:
        if value is None:
            return None

        # pandas / numpy missing values
        try:
            if pd.isna(value):
                return None
        except Exception:
            pass

        # Native time/date types
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()

        # Decimal
        if isinstance(value, Decimal):
            return str(value)

        # bytes / binary
        if isinstance(value, (bytes, bytearray, memoryview)):
            raw = bytes(value)
            return {
                "encoding": "base64",
                "data": base64.b64encode(raw).decode("ascii"),
            }

        # Float edge cases
        if isinstance(value, float):
            if math.isnan(value) or math.isinf(value):
                return None
            return value

        # dict
        if isinstance(value, dict):
            return {str(k): cls._normalize_jsonable(v) for k, v in value.items()}

        # list / tuple / set
        if isinstance(value, (list, tuple, set)):
            return [cls._normalize_jsonable(v) for v in value]

        # numpy scalar-like objects
        if hasattr(value, "item") and callable(value.item):
            try:
                return cls._normalize_jsonable(value.item())
            except Exception:
                pass

        return value

    def _resolve_action_url(self, action_input: DataBlock) -> str:
        urls = getattr(action_input, "urls", None) or []
        if urls and isinstance(urls, list) and urls[0]:
            parquet_url = str(urls[0]).strip()
            self._validate_url_shape(parquet_url)
            return parquet_url

        config_url = getattr(self.action_config, "parquet_url", None)
        if config_url:
            parquet_url = str(config_url).strip()
            self._validate_url_shape(parquet_url)
            return parquet_url

        raise HTTPException(
            status_code=400,
            detail="A parquet URL is required either in action_input.urls[0] or action_config.parquet_url.",
        )