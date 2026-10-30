"""
data_plane_v2 — large-data tool for scalable ingest, query, and inspect (AUT-1378).

Day One modes:  ingest | query | inspect | status | resume | cancel

Design notes (post-review hardening):
  - Ingest streams: URL → temp file (FileDownloader) → lazy row iterator → lazy
    chunk iterator → immediate execute.  Memory is O(chunk), never O(file).
  - Query streams result rows to a temp CSV file and uploads it to S3 as an
    artifact; large results are returned as {artifact_url, row_count, ...}.
  - Query mode is read-only: destructive/DDL statements are rejected up front.
  - All SQL generation and execution routes through a backend adapter seam
    (DataPlaneAdapter protocol) — Postgres gets execute_values + statement
    timeout; other backends get a portable append-only path.
  - All MongoDB job mutations are scoped by {user_id, job_id}; resume uses an
    atomic claim; cancellation is polled at every shard boundary.
"""

import asyncio
import csv
import json
import os
import re
import tempfile
import time
from enum import Enum
from typing import Any, Iterator, Protocol, Type, runtime_checkable

from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.abc.ActionABC import (
    ActionABC,
    ActionConfigCreateType,
    ActionConfigType,
    ActionConfigUpdateType,
    ActionInputType,
    ActionOutputType,
)
from src.autobots.action.action_type.action_text2text.action_data_plane.data_plane_v2_crud import (
    checkpoint_shard,
    claim_job_for_resume,
    create_job,
    finalize_job,
    get_job,
    is_job_cancelled,
    set_status,
)
from src.autobots.action.action_type.action_text2text.action_data_plane.data_plane_v2_doc_model import (
    ChunkMetric,
    DataPlaneError,
    DataPlaneErrorCode,
    DataPlaneJob,
    JobMetrics,
    JobStatus,
)
from src.autobots.action.action_type.action_text2text.action_sql_runner.action_db_sql_runner import (
    ActionDBSqlQueryRunner,
    DatabaseEnum,
    _DBSqlRunnerInternalConfig,
    build_connect_args,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.aws.aws_s3 import get_s3
from src.autobots.data_model.context import Context
from src.autobots.files.file_downloader import FileDownloader
from src.autobots.files.user_files import UserFiles
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM
from src.autobots.core.cancellation.run_cancellation import RunCancellation
from src.autobots.data_model.app_state import AppState

# ---------------------------------------------------------------------------
# Config enums
# ---------------------------------------------------------------------------

class DataPlaneMode(str, Enum):
    ingest = "ingest"
    query = "query"
    inspect = "inspect"
    status = "status"
    resume = "resume"
    cancel = "cancel"


class IngestStrategy(str, Enum):
    append = "append"
    upsert = "upsert"
    # replace (TRUNCATE + INSERT) is defined in Jira §9.1 but is NOT listed in the
    # Day One Must Have requirements (§16). Deferred to a post-MVP iteration.


class QueryOutputMode(str, Enum):
    inline = "inline"   # rows as JSON array — fine for small results
    csv = "csv"         # rows uploaded to S3 as a CSV artifact — use for large results


# ---------------------------------------------------------------------------
# Action config
# ---------------------------------------------------------------------------

class DataPlaneV2Config(BaseModel):
    """
    Configuration for data_plane_v2.  Set `mode` first, then fill the
    fields for that mode.

    ingest  → table, source_csv_urls, strategy, conflict_keys,
               chunk_rows, max_batch_bytes
    query   → sql, params, max_rows, query_output_mode
    inspect → table
    status / resume / cancel → job_id | job_name | idempotency_key
    """

    connection_name: str
    mode: DataPlaneMode

    # job identity — used for tracking; required for status / resume / cancel
    job_name: str | None = None
    idempotency_key: str | None = None
    job_id: str | None = None

    # ---------- ingest ----------
    table: str | None = None
    columns: list[str] | None = None
    source_csv_urls: list[str] | None = None
    strategy: IngestStrategy = IngestStrategy.upsert
    conflict_keys: list[str] | None = None
    chunk_rows: int = Field(default=200, ge=1, le=5000)
    max_batch_bytes: int = Field(default=524288, ge=1)   # 512 KB — chunks stay below this
    retry_batch_halving: bool = True

    # ---------- query ----------
    sql: str | None = None
    params: dict[str, Any] | None = None
    max_rows: int = Field(default=5000, ge=1, le=50000)
    query_output_mode: QueryOutputMode = QueryOutputMode.inline


# ---------------------------------------------------------------------------
# Helpers — engine creation
# ---------------------------------------------------------------------------

def _create_engine(cfg: _DBSqlRunnerInternalConfig):
    if cfg.database_type == DatabaseEnum.bigquery:
        return create_engine(
            cfg.build_connection_string(),
            credentials_info=cfg.credentials_info,
            pool_pre_ping=True,
        )
    if cfg.database_type == DatabaseEnum.postgres and cfg.cloud_sql_instance_connection_name:
        from src.autobots.core.database.cloud_sql import create_cloud_sql_engine
        return create_cloud_sql_engine(
            instance_connection_name=cfg.cloud_sql_instance_connection_name,
            user=cfg.username or "",
            password=cfg.password or "",
            database=cfg.database,
            use_private_ip=cfg.cloud_sql_use_private_ip,
            enable_iam_auth=cfg.cloud_sql_enable_iam_auth,
            credentials_info=cfg.credentials_info,
        )
    return create_engine(
        cfg.build_connection_string(),
        pool_pre_ping=True,
        connect_args=build_connect_args(cfg, timeout_sec=1800),
    )


def _notify(notify: Notify | None, ctx: Context, msg: str) -> None:
    if notify:
        notify(ctx, msg)


def _user_id(user: UserORM | None) -> str:
    return str(getattr(user, "id", "unknown")) if user else "unknown"


# ---------------------------------------------------------------------------
# Helpers — identifier validation
# ---------------------------------------------------------------------------

_SAFE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _validate_identifier(name: str) -> str:
    """Validate that a SQL identifier is safe to embed in non-parameterized SQL.

    Only allows identifiers matching [A-Za-z_][A-Za-z0-9_$]* — sufficient for
    all standard schema/table names. Raises ValueError for names with spaces,
    hyphens, quotes, or other characters that enable SQL injection.
    """
    if not _SAFE_IDENT_RE.match(name):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name


def _quote_table_reference(table: str) -> str:
    """Validate and double-quote a (possibly schema-qualified) table reference.

    Accepts "table" or "schema.table"; every component must pass
    _validate_identifier. Raises ValueError otherwise.
    """
    parts = [p.strip('"') for p in table.split(".")]
    if not parts or len(parts) > 2 or not all(parts):
        raise ValueError(f"Unsafe table reference: {table!r}")
    return ".".join(f'"{_validate_identifier(p)}"' for p in parts)


# ---------------------------------------------------------------------------
# Helpers — query-mode read-only guard
# ---------------------------------------------------------------------------

def _strip_sql_comments_and_literals(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"'([^']|'')*'", "''", sql)
    sql = re.sub(r'"([^"]|"")*"', '""', sql)
    return sql


_QUERY_FORBIDDEN = re.compile(
    r"(?i)\b(drop|truncate|alter|delete|update|insert|create|grant|revoke|exec|execute|into|copy|call|do)\b"
)

_QUERY_ALLOWED_FIRST_TOKENS = {"SELECT", "WITH"}


def _check_query_safe(sql: str) -> str | None:
    """Return a rejection reason string if the SQL is not safe for query mode, else None.

    Checks (in order):
    1. Rejects empty input.
    2. Rejects multi-statement SQL (semicolon-separated).
    3. Requires the first meaningful token to be SELECT or WITH.
    4. Defense-in-depth denylist for destructive/side-effecting keywords.
    """
    cleaned = _strip_sql_comments_and_literals(sql)
    stripped = cleaned.strip()

    if not stripped:
        return "EMPTY_QUERY"

    # Reject multi-statement — split on ; and ignore trailing empty fragment
    statements = [s.strip() for s in stripped.split(";") if s.strip()]
    if len(statements) > 1:
        return "MULTI_STATEMENT"

    # Require SELECT or WITH as first token
    first = re.match(r"(\w+)", stripped)
    if not first or first.group(1).upper() not in _QUERY_ALLOWED_FIRST_TOKENS:
        token = first.group(1).upper() if first else "UNKNOWN"
        return token

    # Defense-in-depth denylist
    m = _QUERY_FORBIDDEN.search(stripped)
    return m.group(0).upper() if m else None


# ---------------------------------------------------------------------------
# Helpers — streaming CSV row iteration + lazy adaptive chunking
# ---------------------------------------------------------------------------

def _iter_csv_rows(file_path: str, columns: list[str] | None) -> Iterator[dict]:
    """Yield rows from a temp CSV file one at a time. O(1) memory per row."""
    with open(file_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if columns:
                yield {c: (None if row.get(c, "") == "" else row.get(c)) for c in columns}
            else:
                yield {k: (None if v == "" else v) for k, v in row.items()}


def _estimate_row_bytes(row: dict) -> int:
    try:
        return len(json.dumps(row, default=str).encode())
    except Exception:
        return 256  # safe fallback


def _adaptive_chunks_iter(
    row_iter: Iterator[dict],
    chunk_rows: int,
    max_batch_bytes: int,
) -> Iterator[list[dict]]:
    """Lazy chunking from a row iterator — never materializes the full row list.

    Yields chunks bounded by both row count and estimated byte size.
    """
    chunk: list[dict] = []
    current_bytes = 0
    for row in row_iter:
        row_bytes = _estimate_row_bytes(row)
        if chunk and (len(chunk) >= chunk_rows or current_bytes + row_bytes > max_batch_bytes):
            yield chunk
            chunk = []
            current_bytes = 0
        chunk.append(row)
        current_bytes += row_bytes
    if chunk:
        yield chunk


# ---------------------------------------------------------------------------
# Helpers — SQL builders (internal; called only via adapters)
# ---------------------------------------------------------------------------

def _build_insert_sql(
    table: str,
    columns: list[str],
    conflict_keys: list[str] | None,
    strategy: IngestStrategy,
) -> str:
    """Named-param INSERT for the generic (non-Postgres) execution path.

    All identifiers are validated before being embedded. The ON CONFLICT clause
    uses PostgreSQL/SQLite syntax and is NOT portable to BigQuery, Snowflake, or
    Databricks. Upsert on non-Postgres backends is rejected in _dispatch (via
    adapter.supports_upsert) before this function is reached.
    """
    safe_table = _quote_table_reference(table)
    cols = [_validate_identifier(c) for c in columns]
    keys = [_validate_identifier(k) for k in (conflict_keys or [])]

    col_list = ", ".join(f'"{c}"' for c in cols)
    placeholders = ", ".join(f":{c}" for c in cols)
    sql = f"INSERT INTO {safe_table} ({col_list}) VALUES ({placeholders})"
    if strategy == IngestStrategy.upsert and keys:
        conflict_cols = ", ".join(f'"{k}"' for k in keys)
        update_cols = [c for c in cols if c not in keys]
        if update_cols:
            set_clause = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in update_cols)
            sql += f" ON CONFLICT ({conflict_cols}) DO UPDATE SET {set_clause}"
        else:
            sql += f" ON CONFLICT ({conflict_cols}) DO NOTHING"
    return sql


def _build_postgres_insert_sql(
    table: str,
    columns: list[str],
    conflict_keys: list[str] | None,
    strategy: IngestStrategy,
) -> str:
    """Postgres execute_values INSERT — uses %s template (not named params).

    All identifiers are validated before being embedded.
    """
    safe_table = _quote_table_reference(table)
    cols = [_validate_identifier(c) for c in columns]
    keys = [_validate_identifier(k) for k in (conflict_keys or [])]

    col_list = ", ".join(f'"{c}"' for c in cols)
    sql = f"INSERT INTO {safe_table} ({col_list}) VALUES %s"
    if strategy == IngestStrategy.upsert and keys:
        conflict_cols = ", ".join(f'"{k}"' for k in keys)
        update_cols = [c for c in cols if c not in keys]
        if update_cols:
            set_clause = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in update_cols)
            sql += f" ON CONFLICT ({conflict_cols}) DO UPDATE SET {set_clause}"
        else:
            sql += f" ON CONFLICT ({conflict_cols}) DO NOTHING"
    return sql


# ---------------------------------------------------------------------------
# Helpers — chunk execution (internal; called only via adapters)
# ---------------------------------------------------------------------------

def _execute_chunk_generic(
    engine,
    sql: str,
    rows: list[dict],
    retry_halving: bool,
    retry_count: int = 0,
) -> tuple[int, str | None, int]:
    """Generic SQLAlchemy executemany — works on all backends."""
    try:
        with engine.begin() as conn:
            conn.execute(text(sql), rows)
        return len(rows), None, retry_count
    except Exception as e:
        if not retry_halving or len(rows) <= 1:
            return 0, str(e), retry_count
        mid = len(rows) // 2
        c1, e1, r1 = _execute_chunk_generic(engine, sql, rows[:mid], retry_halving, retry_count + 1)
        c2, e2, r2 = _execute_chunk_generic(engine, sql, rows[mid:], retry_halving, r1)
        return c1 + c2, "; ".join(filter(None, [e1, e2])) or None, r2


def _execute_chunk_postgres(
    engine,
    sql: str,
    rows: list[dict],
    columns: list[str],
    retry_halving: bool,
    retry_count: int = 0,
) -> tuple[int, str | None, int]:
    """Postgres-optimized path using psycopg2.extras.execute_values."""
    try:
        from psycopg2.extras import execute_values
        values = [tuple(row.get(c) for c in columns) for row in rows]
        raw_conn = engine.raw_connection()
        try:
            with raw_conn.cursor() as cur:
                execute_values(cur, sql, values)
            raw_conn.commit()
        except Exception:
            raw_conn.rollback()
            raise
        finally:
            raw_conn.close()
        return len(rows), None, retry_count
    except Exception as e:
        if not retry_halving or len(rows) <= 1:
            return 0, str(e), retry_count
        mid = len(rows) // 2
        c1, e1, r1 = _execute_chunk_postgres(engine, sql, rows[:mid], columns, retry_halving, retry_count + 1)
        c2, e2, r2 = _execute_chunk_postgres(engine, sql, rows[mid:], columns, retry_halving, r1)
        return c1 + c2, "; ".join(filter(None, [e1, e2])) or None, r2


# ---------------------------------------------------------------------------
# Helpers — pre-ingest schema validation
# ---------------------------------------------------------------------------

def _validate_ingest_schema_sync(
    engine,
    table: str,
    columns: list[str] | None,
    conflict_keys: list[str] | None,
) -> dict | None:
    """Check the target table exists and columns/conflict_keys are valid.

    Returns an error dict {"error", "code"} if invalid, None if OK.
    Fails open (returns None) if information_schema itself is unavailable on
    the backend (e.g. SQLite) so it never blocks otherwise-working ingests.
    """
    parts = table.split(".")
    schema_name = parts[0].strip('"') if len(parts) == 2 else None
    table_name = parts[-1].strip('"')

    col_sql = "SELECT column_name FROM information_schema.columns WHERE table_name = :t"
    col_params: dict[str, Any] = {"t": table_name}
    if schema_name:
        col_sql += " AND table_schema = :s"
        col_params["s"] = schema_name

    try:
        with engine.connect() as conn:
            db_cols = {r[0] for r in conn.execute(text(col_sql), col_params)}
    except Exception as e:
        logger.warning(f"data_plane_v2 schema validation skipped (information_schema unavailable): {e}")
        return None

    if not db_cols:
        return {
            "error": f"Table '{table}' not found",
            "code": DataPlaneErrorCode.TABLE_NOT_FOUND,
        }

    if columns:
        missing = set(columns) - db_cols
        if missing:
            return {
                "error": f"Columns missing from table: {sorted(missing)}",
                "code": DataPlaneErrorCode.SCHEMA_MISMATCH,
            }
        if conflict_keys:
            bad = set(conflict_keys) - set(columns)
            if bad:
                return {
                    "error": f"conflict_keys not in columns: {sorted(bad)}",
                    "code": DataPlaneErrorCode.SCHEMA_MISMATCH,
                }
    elif conflict_keys:
        bad = set(conflict_keys) - db_cols
        if bad:
            return {
                "error": f"conflict_keys not in table columns: {sorted(bad)}",
                "code": DataPlaneErrorCode.SCHEMA_MISMATCH,
            }
    return None


# ---------------------------------------------------------------------------
# Backend adapter seam — all SQL generation & execution routes through here
# ---------------------------------------------------------------------------

@runtime_checkable
class DataPlaneAdapter(Protocol):
    """Backend seam for data_plane_v2. One implementation per backend family."""

    def supports_upsert(self) -> bool: ...

    def quote_identifier(self, name: str) -> str: ...

    def build_insert_sql(
        self,
        table: str,
        columns: list[str],
        conflict_keys: list[str] | None,
        strategy: IngestStrategy,
    ) -> str: ...

    def execute_insert_batch(
        self,
        engine,
        sql: str,
        rows: list[dict],
        columns: list[str],
        retry_halving: bool,
        retry_count: int,
    ) -> tuple[int, str | None, int]: ...

    def set_statement_timeout(self, conn, timeout_seconds: int) -> None: ...

    def validate_schema_sync(
        self,
        engine,
        table: str,
        columns: list[str] | None,
        conflict_keys: list[str] | None,
    ) -> dict | None: ...


class PostgresDataPlaneAdapter:
    """Postgres implementation: execute_values inserts, upsert, statement timeout."""

    backend_strategy = "postgres_execute_values"

    def supports_upsert(self) -> bool:
        return True

    def quote_identifier(self, name: str) -> str:
        return f'"{_validate_identifier(name)}"'

    def build_insert_sql(
        self,
        table: str,
        columns: list[str],
        conflict_keys: list[str] | None,
        strategy: IngestStrategy,
    ) -> str:
        return _build_postgres_insert_sql(table, columns, conflict_keys, strategy)

    def execute_insert_batch(
        self,
        engine,
        sql: str,
        rows: list[dict],
        columns: list[str],
        retry_halving: bool,
        retry_count: int = 0,
    ) -> tuple[int, str | None, int]:
        return _execute_chunk_postgres(engine, sql, rows, columns, retry_halving, retry_count)

    def set_statement_timeout(self, conn, timeout_seconds: int) -> None:
        conn.execute(text(f"SET statement_timeout = '{int(timeout_seconds)}s'"))

    def validate_schema_sync(
        self,
        engine,
        table: str,
        columns: list[str] | None,
        conflict_keys: list[str] | None,
    ) -> dict | None:
        return _validate_ingest_schema_sync(engine, table, columns, conflict_keys)


class Pg8000PostgresDataPlaneAdapter:
    """Postgres via pg8000 (Cloud SQL).

    pg8000 is incompatible with psycopg2.extras.execute_values, so this adapter
    builds named-param SQL and uses SQLAlchemy's connection.execute() instead.
    Full Postgres semantics (upsert, statement timeout) are still supported.
    """

    backend_strategy = "executemany_pg8000"

    def supports_upsert(self) -> bool:
        return True

    def quote_identifier(self, name: str) -> str:
        return f'"{_validate_identifier(name)}"'

    def build_insert_sql(
        self,
        table: str,
        columns: list[str],
        conflict_keys: list[str] | None,
        strategy: IngestStrategy,
    ) -> str:
        return _build_insert_sql(table, columns, conflict_keys, strategy)

    def execute_insert_batch(
        self,
        engine,
        sql: str,
        rows: list[dict],
        columns: list[str],
        retry_halving: bool,
        retry_count: int = 0,
    ) -> tuple[int, str | None, int]:
        return _execute_chunk_generic(engine, sql, rows, retry_halving, retry_count)

    def set_statement_timeout(self, conn, timeout_seconds: int) -> None:
        conn.execute(text(f"SET statement_timeout = '{int(timeout_seconds)}s'"))

    def validate_schema_sync(
        self,
        engine,
        table: str,
        columns: list[str] | None,
        conflict_keys: list[str] | None,
    ) -> dict | None:
        return _validate_ingest_schema_sync(engine, table, columns, conflict_keys)


class GenericDataPlaneAdapter:
    """Portable fallback: append-only, no upsert, no statement timeout."""

    backend_strategy = "executemany_sqlalchemy"

    def supports_upsert(self) -> bool:
        return False

    def quote_identifier(self, name: str) -> str:
        return f'"{_validate_identifier(name)}"'

    def build_insert_sql(
        self,
        table: str,
        columns: list[str],
        conflict_keys: list[str] | None,
        strategy: IngestStrategy,
    ) -> str:
        return _build_insert_sql(table, columns, conflict_keys, strategy)

    def execute_insert_batch(
        self,
        engine,
        sql: str,
        rows: list[dict],
        columns: list[str],
        retry_halving: bool,
        retry_count: int = 0,
    ) -> tuple[int, str | None, int]:
        return _execute_chunk_generic(engine, sql, rows, retry_halving, retry_count)

    def set_statement_timeout(self, conn, timeout_seconds: int) -> None:
        # No portable statement-timeout mechanism across generic backends — no-op.
        return None

    def validate_schema_sync(
        self,
        engine,
        table: str,
        columns: list[str] | None,
        conflict_keys: list[str] | None,
    ) -> dict | None:
        # Same information_schema check as Postgres; fails open if unsupported.
        return _validate_ingest_schema_sync(engine, table, columns, conflict_keys)


def _get_adapter(internal_config: _DBSqlRunnerInternalConfig) -> DataPlaneAdapter:
    if internal_config.database_type == DatabaseEnum.postgres:
        # Cloud SQL uses pg8000; direct Postgres connections use psycopg2.
        # execute_values is psycopg2-specific so gate on the connection type.
        if internal_config.cloud_sql_instance_connection_name:
            return Pg8000PostgresDataPlaneAdapter()
        return PostgresDataPlaneAdapter()
    return GenericDataPlaneAdapter()


# ---------------------------------------------------------------------------
# Shard processor
# ---------------------------------------------------------------------------

def _empty_shard_result(ok: bool, errors: list[DataPlaneError] | None = None) -> dict:
    return {
        "ok": ok,
        "rows_attempted": 0, "rows_committed": 0, "bytes_processed": 0,
        "chunks_total": 0, "chunks_completed": 0, "chunks_failed": 0,
        "retry_count": 0,
        "errors": errors or [],
        "chunk_metrics": [],
    }


def _process_shard_sync(
    engine,
    adapter: DataPlaneAdapter,
    config: DataPlaneV2Config,
    job_id: str,
    user_id: str,
    shard_idx: int,
    tmp_path: str,
    notify_fn,
    cancellation: RunCancellation | None = None,
) -> dict:
    """Insert all chunks of one already-downloaded shard CSV.

    Streams rows lazily from the temp file and chunks lazily — the full row
    list is never materialized, keeping memory O(chunk) not O(file).
    Returns a metrics dict.
    """
    # Read just the header line to resolve the column list without loading rows.
    try:
        with open(tmp_path, "r", encoding="utf-8-sig", newline="") as f:
            header = next(csv.reader(f), None)
    except Exception as e:
        return _empty_shard_result(False, [DataPlaneError(
            code=DataPlaneErrorCode.INVALID_SOURCE_ARTIFACT,
            message=f"failed to read shard CSV: {e}", shard_index=shard_idx,
        )])

    if not header:
        return _empty_shard_result(True)  # empty file — nothing to ingest

    columns = list(config.columns) if config.columns else list(header)

    # Validate every identifier that will be embedded in SQL (table, CSV header
    # columns, conflict keys) and build the INSERT through the adapter.
    try:
        _quote_table_reference(config.table or "")
        for c in columns:
            _validate_identifier(c)
        for k in config.conflict_keys or []:
            _validate_identifier(k)
        sql = adapter.build_insert_sql(
            config.table or "", columns, config.conflict_keys, config.strategy,
        )
    except ValueError as e:
        return _empty_shard_result(False, [DataPlaneError(
            code=DataPlaneErrorCode.SCHEMA_MISMATCH,
            message=str(e), shard_index=shard_idx,
        )])

    backend_strategy = getattr(adapter, "backend_strategy", "unknown")

    rows_attempted = 0
    rows_committed = 0
    bytes_processed = 0
    chunks_total = 0
    chunks_completed = 0
    chunks_failed = 0
    total_retries = 0
    errors: list[DataPlaneError] = []
    chunk_metrics: list[ChunkMetric] = []
    stream_error: str | None = None
    shard_cancelled = False

    try:
        row_iter = _iter_csv_rows(tmp_path, columns)
        for chunk_idx, chunk in enumerate(
            _adaptive_chunks_iter(row_iter, config.chunk_rows, config.max_batch_bytes)
        ):
            if cancellation is not None and cancellation.is_cancelled:
                shard_cancelled = True
                break

            if notify_fn:
                notify_fn(f"Shard {shard_idx + 1} chunk {chunk_idx + 1} ({len(chunk)} rows)")

            est_bytes = sum(_estimate_row_bytes(r) for r in chunk)
            t0 = time.time()

            committed, err, retries = adapter.execute_insert_batch(
                engine, sql, chunk, columns, config.retry_batch_halving, 0,
            )

            elapsed_ms = int((time.time() - t0) * 1000)
            rows_attempted += len(chunk)
            rows_committed += committed
            bytes_processed += est_bytes
            total_retries += retries
            chunks_total += 1

            chunk_metrics.append(ChunkMetric(
                job_id=job_id,
                user_id=user_id,
                shard_index=shard_idx,
                chunk_index=chunk_idx,
                row_count=len(chunk),
                estimated_bytes=est_bytes,
                execution_ms=elapsed_ms,
                backend_strategy=backend_strategy,
                retry_count=retries,
                failure_reason=err,
            ))

            if err:
                chunks_failed += 1
                errors.append(DataPlaneError(
                    code=DataPlaneErrorCode.CHUNK_EXECUTION_FAILED,
                    message=err, shard_index=shard_idx, chunk_index=chunk_idx,
                ))
            else:
                chunks_completed += 1
    except Exception as e:
        # CSV iteration/parse failure mid-stream — record and stop this shard.
        stream_error = f"shard CSV processing failed: {e}"
        errors.append(DataPlaneError(
            code=DataPlaneErrorCode.INVALID_SOURCE_ARTIFACT,
            message=stream_error, shard_index=shard_idx,
        ))

    return {
        "ok": chunks_failed == 0 and stream_error is None and not shard_cancelled,
        "rows_attempted": rows_attempted,
        "rows_committed": rows_committed,
        "bytes_processed": bytes_processed,
        "chunks_total": chunks_total,
        "chunks_completed": chunks_completed,
        "chunks_failed": chunks_failed,
        "retry_count": total_retries,
        "errors": errors,
        "chunk_metrics": chunk_metrics,
    }


# ---------------------------------------------------------------------------
# Mode: ingest
# ---------------------------------------------------------------------------

def _ingest_result(
    job_id: str,
    status: JobStatus,
    metrics: JobMetrics,
    errors: list[DataPlaneError],
) -> dict:
    return {
        "job_id": job_id,
        "mode": "ingest",
        "status": status.value,
        "shards_total": metrics.shards_total,
        "shards_completed": metrics.shards_completed,
        "rows_attempted": metrics.rows_attempted,
        "rows_committed": metrics.rows_committed,
        "bytes_processed": metrics.bytes_processed,
        "chunks_total": metrics.chunks_total,
        "chunks_completed": metrics.chunks_completed,
        "chunks_failed": metrics.chunks_failed,
        "retry_count": metrics.retry_count,
        "elapsed_seconds": metrics.elapsed_seconds,
        "errors": [e.model_dump(mode="json") for e in errors],
    }


async def _run_ingest(
    action: "ActionDataPlaneV2",
    config: DataPlaneV2Config,
    adapter: DataPlaneAdapter,
    internal_config: _DBSqlRunnerInternalConfig,
    job: DataPlaneJob,
    ctx: Context,
    app_state: AppState,
    notify: Notify | None,
) -> dict:
    engine = _create_engine(internal_config)
    uid = job.user_id
    completed_indices = set(job.checkpoint.completed_shard_indices)
    started = time.time()

    metrics = job.metrics.model_copy()
    metrics.shards_total = len(config.source_csv_urls or [])
    metrics.current_phase = "validating"
    errors: list[DataPlaneError] = list(job.errors)

    await set_status(action.db, job.job_id, uid, JobStatus.running)

    # ── pre-ingest schema validation ────────────────────────────────────────
    # When config.columns is explicitly provided we can fully validate column
    # existence against information_schema before any data moves.
    # When config.columns is None (inferred from CSV headers) we can only
    # confirm the table exists and that conflict_keys are valid DB columns —
    # the header columns themselves are unknown until the first shard is
    # downloaded.  Per-shard identifier format validation still runs in
    # _process_shard_sync; a non-existent inferred column surfaces as a
    # DB-side error on the first chunk insert rather than an early
    # SCHEMA_MISMATCH.  Pass explicit columns to get full pre-flight checks.
    try:
        schema_err = await asyncio.to_thread(
            adapter.validate_schema_sync,
            engine, config.table or "", config.columns, config.conflict_keys,
        )
    except Exception as e:
        schema_err = {
            "error": f"Schema validation failed: {e}",
            "code": DataPlaneErrorCode.CONNECTION_ERROR,
        }
    if schema_err:
        engine.dispose()
        code = schema_err.get("code", DataPlaneErrorCode.SCHEMA_MISMATCH)
        metrics.current_phase = "failed"
        metrics.elapsed_seconds = round(time.time() - started, 2)
        errors.append(DataPlaneError(code=code, message=str(schema_err.get("error"))))
        await finalize_job(action.db, job.job_id, uid, JobStatus.failed, metrics, errors)
        result = _ingest_result(job.job_id, JobStatus.failed, metrics, errors)
        result["error"] = schema_err.get("error")
        result["code"] = code
        return result

    metrics.current_phase = "ingesting"

    # Capture any unexpected exception so finalize_job is always reached regardless
    # of whether the loop exits cleanly or crashes mid-shard.
    unexpected_exc: Exception | None = None
    cancelled = False
    try:
        for shard_idx, url in enumerate(config.source_csv_urls or []):
            # Cancellation is polled at every shard boundary so a user-initiated
            # cancel takes effect without waiting for the whole job.
            if await is_job_cancelled(action.db, job.job_id, uid) or (
                app_state.cancellation is not None and app_state.cancellation.is_cancelled
            ):
                cancelled = True
                _notify(notify, ctx, f"Job {job.job_id} cancelled — stopping ingest")
                break

            if shard_idx in completed_indices:
                _notify(notify, ctx, f"Shard {shard_idx + 1}: already completed, skipping")
                continue

            _notify(notify, ctx, f"Shard {shard_idx + 1}/{metrics.shards_total}: {url}")

            # Streaming download to a temp file — never loads the body into memory
            # and goes through FileDownloader (S3-aware, redirect-safe) instead of
            # raw urllib to avoid SSRF-prone direct fetches.
            tmp_path: str | None = None
            try:
                url_to_path = await FileDownloader._download_urls_to_tmpfiles(
                    ctx=ctx, user=action.user, db=action.db, urls=[url],
                )
                tmp_path = url_to_path.get(url)
            except Exception as e:
                logger.bind(ctx=ctx).warning(f"data_plane_v2 shard download raised: {e}")
                tmp_path = None

            if not tmp_path:
                errors.append(DataPlaneError(
                    code=DataPlaneErrorCode.INVALID_SOURCE_ARTIFACT,
                    message=f"failed to download shard source: {url}",
                    shard_index=shard_idx,
                ))
                await checkpoint_shard(
                    action.db, job.job_id, uid, shard_idx, False, metrics, errors, [],
                )
                continue

            try:
                shard = await asyncio.to_thread(
                    _process_shard_sync,
                    engine, adapter, config, job.job_id, uid, shard_idx, tmp_path,
                    lambda msg: _notify(notify, ctx, msg),
                    app_state.cancellation,
                )
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        logger.bind(ctx=ctx).warning(
                            f"data_plane_v2 failed to remove temp file {tmp_path}"
                        )

            metrics.rows_attempted += shard["rows_attempted"]
            metrics.rows_committed += shard["rows_committed"]
            metrics.bytes_processed += shard["bytes_processed"]
            metrics.chunks_total += shard["chunks_total"]
            metrics.chunks_completed += shard["chunks_completed"]
            metrics.chunks_failed += shard["chunks_failed"]
            metrics.retry_count += shard["retry_count"]
            if shard["ok"]:
                metrics.shards_completed += 1
            errors.extend(shard["errors"])

            await checkpoint_shard(
                action.db, job.job_id, uid, shard_idx, shard["ok"],
                metrics, errors, shard["chunk_metrics"],
            )

    except asyncio.CancelledError:
        metrics.current_phase = "cancelled"
        await finalize_job(action.db, job.job_id, uid, JobStatus.cancelled, metrics, errors)
        raise
    except Exception as exc:
        unexpected_exc = exc
    finally:
        engine.dispose()

    metrics.elapsed_seconds = round(time.time() - started, 2)

    if unexpected_exc is not None:
        # An exception escaped the shard loop (e.g. MongoDB down during checkpoint,
        # asyncio.to_thread rejected). Finalize as failed so the job is never left
        # permanently in RUNNING state.
        metrics.current_phase = "failed"
        errors.append(DataPlaneError(
            code=DataPlaneErrorCode.CHUNK_EXECUTION_FAILED,
            message=f"Unexpected error during ingest: {unexpected_exc}",
        ))
        await finalize_job(action.db, job.job_id, uid, JobStatus.failed, metrics, errors)
        raise unexpected_exc

    if cancelled:
        metrics.current_phase = "cancelled"
        await finalize_job(action.db, job.job_id, uid, JobStatus.cancelled, metrics, errors)
        return _ingest_result(job.job_id, JobStatus.cancelled, metrics, errors)

    metrics.current_phase = "done"

    if metrics.shards_completed >= metrics.shards_total and metrics.chunks_failed == 0:
        final_status = JobStatus.completed
    elif metrics.rows_committed > 0:
        final_status = JobStatus.partial
        errors.append(DataPlaneError(
            code=DataPlaneErrorCode.PARTIAL_PROGRESS_RECORDED,
            message=(
                f"{metrics.rows_committed} rows committed; "
                f"{metrics.chunks_failed} chunk(s) failed; "
                f"{metrics.shards_total - metrics.shards_completed} shard(s) incomplete"
            ),
        ))
    else:
        final_status = JobStatus.failed

    await finalize_job(action.db, job.job_id, uid, final_status, metrics, errors)
    return _ingest_result(job.job_id, final_status, metrics, errors)


# ---------------------------------------------------------------------------
# Mode: query  (streams to temp CSV; large results uploaded as S3 artifact)
# ---------------------------------------------------------------------------

_ARTIFACT_ROW_THRESHOLD = 1000     # inline only at/below this; artifact above
_QUERY_STATEMENT_TIMEOUT_SECONDS = 60
_ARTIFACT_MAX_BYTES = 100 * 1024 * 1024  # 100 MB safety cap for full-export artifact mode


def _query_to_tmpfile_sync(
    engine,
    config: DataPlaneV2Config,
    adapter: DataPlaneAdapter,
    tmp_path: str,
) -> tuple[int, bool, list[str] | None]:
    """Execute query, stream rows to a temp CSV file. Returns (row_count, truncated, fieldnames).

    Memory stays O(row): rows are written to disk as they arrive; the statement
    timeout (Postgres) bounds runaway queries.

    Truncation policy:
      inline mode  — capped at config.max_rows (preview semantics)
      csv mode     — full export; capped only by _ARTIFACT_MAX_BYTES safety limit
    """
    is_full_export = config.query_output_mode == QueryOutputMode.csv

    with engine.connect() as conn:
        adapter.set_statement_timeout(conn, _QUERY_STATEMENT_TIMEOUT_SECONDS)
        rs = conn.execution_options(stream_results=True).execute(
            text(config.sql or ""), config.params or {}
        )
        if not rs.returns_rows:
            return 0, False, None

        row_count = 0
        bytes_written = 0
        truncated = False
        fieldnames: list[str] | None = None
        with open(tmp_path, "w", newline="", encoding="utf-8") as f:
            writer = None
            for row in rs:
                d = dict(row._mapping)
                if writer is None:
                    fieldnames = list(d.keys())
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                if not is_full_export and row_count >= config.max_rows:
                    truncated = True
                    break
                if is_full_export and bytes_written >= _ARTIFACT_MAX_BYTES:
                    truncated = True
                    break
                # Estimate bytes for the row before writing so the cap check above
                # is accurate across platforms (f.tell() is opaque in text mode).
                row_line = ",".join("" if v is None else str(v) for v in d.values()) + "\n"
                bytes_written += len(row_line.encode("utf-8"))
                writer.writerow(d)
                row_count += 1
    return row_count, truncated, fieldnames


def _read_rows_from_csv(tmp_path: str) -> list[dict]:
    """Read back a small (threshold-bounded) query result CSV as row dicts."""
    with open(tmp_path, "r", encoding="utf-8", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


# ---------------------------------------------------------------------------
# Mode: inspect  (schema + primary keys + indexes + row count)
# ---------------------------------------------------------------------------

def _inspect_sync(engine, table: str) -> dict:
    parts = table.split(".")
    if len(parts) == 2:
        schema_name, table_name = parts[0].strip('"'), parts[1].strip('"')
    else:
        schema_name, table_name = None, parts[0].strip('"')

    col_sql = (
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_name = :t"
    )
    col_params: dict[str, Any] = {"t": table_name}
    if schema_name:
        col_sql += " AND table_schema = :s"
        col_params["s"] = schema_name
    col_sql += " ORDER BY ordinal_position"

    # Primary key query (information_schema — works on all backends)
    pk_sql = (
        "SELECT kcu.column_name "
        "FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "  ON tc.constraint_name = kcu.constraint_name "
        " AND tc.table_schema = kcu.table_schema "
        " AND tc.table_name = kcu.table_name "
        "WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_name = :t"
    )
    pk_params: dict[str, Any] = {"t": table_name}
    if schema_name:
        pk_sql += " AND tc.table_schema = :s"
        pk_params["s"] = schema_name
    pk_sql += " ORDER BY kcu.ordinal_position"

    with engine.connect() as conn:
        columns = [dict(r._mapping) for r in conn.execute(text(col_sql), col_params)]
        table_exists = len(columns) > 0

        primary_keys: list[str] = []
        try:
            primary_keys = [r[0] for r in conn.execute(text(pk_sql), pk_params)]
        except Exception:
            pass

        # Indexes — pg_indexes is Postgres-specific; skip gracefully on others
        indexes: list[dict] = []
        try:
            idx_sql = "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = :t"
            idx_params: dict[str, Any] = {"t": table_name}
            if schema_name:
                idx_sql += " AND schemaname = :s"
                idx_params["s"] = schema_name
            indexes = [dict(r._mapping) for r in conn.execute(text(idx_sql), idx_params)]
        except Exception:
            pass

        row_count: int | None = None
        if table_exists:
            try:
                # COUNT(*) cannot use bound params for the table reference, so we
                # validate and double-quote each identifier component instead.
                safe_ref = (
                    f'"{_validate_identifier(schema_name)}"."{_validate_identifier(table_name)}"'
                    if schema_name
                    else f'"{_validate_identifier(table_name)}"'
                )
                row_count = conn.execute(text(f"SELECT COUNT(*) FROM {safe_ref}")).scalar()
            except ValueError:
                raise  # identifier validation errors must not be swallowed
            except Exception:
                pass

    return {
        "mode": "inspect",
        "table": table,
        "table_exists": table_exists,
        "columns": columns,
        "primary_keys": primary_keys,
        "indexes": indexes,
        "row_count": row_count,
    }


# ---------------------------------------------------------------------------
# Action class
# ---------------------------------------------------------------------------

class ActionDataPlaneV2(
    ActionABC[
        DataPlaneV2Config,
        DataPlaneV2Config,
        DataPlaneV2Config,
        DataBlock,
        DataBlocks,
    ]
):
    type = ActionType.data_plane_v2

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return DataPlaneV2Config

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return DataPlaneV2Config

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return DataPlaneV2Config

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    @staticmethod
    def get_description() -> str:
        return """
PREREQUISITE: Call get_database_connection_names first to get available connection names.

Description
-----------
Large-data tool for scalable ingest, query, and schema inspection (AUT-1378 MVP).
Designed to replace the `database` / `sql_runner` tools for large-data workflows:
  - Chunked ingest: no row caps, respects both chunk_rows AND max_batch_bytes
  - Streaming: shard CSVs stream to disk and rows are chunked lazily — memory
    stays bounded by one chunk, never the whole file
  - Postgres optimized: uses psycopg2 execute_values instead of row-by-row inserts
  - Upsert-first: INSERT … ON CONFLICT DO UPDATE, no duplicate rows
  - Resumable: each shard checkpointed in MongoDB; failed/partial jobs can be
    resumed (atomic claim — concurrent resumes are rejected)
  - Cancellable: a running ingest stops at the next shard boundary after cancel
  - Artifact-backed query: large results are uploaded to S3 and returned as an
    artifact_url instead of blowing up LLM context
  - Read-only query mode: destructive SQL (DROP/DELETE/UPDATE/INSERT/…) is blocked;
    Postgres queries run under a 60s statement timeout
  - Structured errors: 11 typed error codes (AUT-1378 section 17)

Modes
-----
ingest  — Chunked CSV ingest with per-shard checkpointing and adaptive retry.
query   — Run SELECT; small results inline, large results as an S3 CSV artifact.
inspect — Table schema, primary keys, indexes, row count.
status  — Look up a job by job_id / job_name / idempotency_key.
resume  — Continue a partial/failed ingest from its last checkpoint.
cancel  — Mark a job as cancelled; a running ingest stops at the next shard.

Key config fields
-----------------
ingest:
  connection_name    str         saved DB connection name
  mode               "ingest"
  table              str         target table e.g. "spotify.metric_points"
  source_csv_urls    list[str]   one URL per shard
  columns            list[str]   (optional) column subset; inferred from CSV header if omitted
  strategy           "upsert"|"append"  default "upsert" (upsert requires PostgreSQL)
  conflict_keys      list[str]   required for upsert; the unique key columns
  chunk_rows         int         max rows per INSERT batch (default 200)
  max_batch_bytes    int         max bytes per INSERT batch (default 524288 = 512KB)
  retry_batch_halving bool       on failure split chunk in half and retry (default true)
  job_name           str         (optional) human label for status lookup
  idempotency_key    str         (optional) skip re-ingest if this key already completed

query:
  sql                str         SELECT only; named params :param supported
  params             dict        (optional)
  max_rows           int         default 5000, max 50000
  query_output_mode  "inline"|"csv"  default inline; results above 1000 rows are
                                  always uploaded as a CSV artifact

inspect:
  table              str         e.g. "spotify.metric_points"

status / resume / cancel:
  job_id | job_name | idempotency_key   (use any one to identify the job)

Output
------
One DataBlock containing JSON. Key fields by mode:

ingest:  { job_id, status, shards_total, shards_completed, rows_attempted,
           rows_committed, bytes_processed, chunks_total, chunks_completed,
           chunks_failed, retry_count, elapsed_seconds, errors }

query:   { mode, output_mode, rows OR artifact_url, row_count, truncated, columns }

inspect: { mode, table, table_exists, columns, primary_keys, indexes, row_count }

status:  { job_id, status, metrics, checkpoint, errors, error_summary, updated_at }
"""

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        return await self.run_tool(self.action_config, ctx, app_state, notify)

    async def run_tool(
        self,
        action_config: DataPlaneV2Config,
        ctx: Context,
        app_state: AppState,
        notify: Notify | None = None,
    ) -> DataBlocks | Exception:
        try:
            result = await self._dispatch(action_config, ctx, app_state, notify)
            return DataBlocks(blocks=[DataBlock(text=json.dumps(result, default=str))])
        except Exception as e:
            logger.bind(ctx=ctx).exception("data_plane_v2 error")
            return e

    async def _dispatch(
        self,
        config: DataPlaneV2Config,
        ctx: Context,
        app_state: AppState,
        notify: Notify | None,
    ) -> dict:
        uid = _user_id(self.user)

        # ── status ──────────────────────────────────────────────────────────
        if config.mode == DataPlaneMode.status:
            job = await get_job(self.db, uid, config.job_id, config.job_name, config.idempotency_key)
            if not job:
                return {"error": "Job not found"}
            return {
                "job_id": job.job_id,
                "status": job.status.value,
                "metrics": job.metrics.model_dump(mode="json"),
                "checkpoint": job.checkpoint.model_dump(mode="json"),
                "errors": [e.model_dump(mode="json") for e in job.errors],
                "error_summary": job.error_summary,
                "updated_at": str(job.updated_at),
            }

        # ── cancel ───────────────────────────────────────────────────────────
        if config.mode == DataPlaneMode.cancel:
            job = await get_job(self.db, uid, config.job_id, config.job_name, config.idempotency_key)
            if not job:
                return {"error": "Job not found"}
            await set_status(self.db, job.job_id, uid, JobStatus.cancelled)
            return {"job_id": job.job_id, "status": "cancelled"}

        # ── resume ───────────────────────────────────────────────────────────
        # Resolve the job first (job_id | job_name | idempotency_key), then take
        # an atomic claim so two concurrent resumes can never both run.
        if config.mode == DataPlaneMode.resume:
            tmp_job = await get_job(self.db, uid, config.job_id, config.job_name, config.idempotency_key)
            if not tmp_job:
                return {"error": "Job not found to resume"}

            job = await claim_job_for_resume(self.db, tmp_job.job_id, uid)
            if not job:
                # Already running, cancelled, completed, or otherwise not claimable.
                return {
                    "error": "Job is already running or cannot be resumed",
                    "code": DataPlaneErrorCode.RESUME_CONFLICT,
                    "job_id": tmp_job.job_id,
                }

            try:
                # Restore original ingest config; allow connection_name override
                restored = DataPlaneV2Config(**job.config)
                restored.connection_name = config.connection_name
            except Exception as e:
                # Revert the claim so the job is not stuck in RUNNING forever.
                await set_status(self.db, job.job_id, uid, JobStatus.failed)
                return {"error": f"Saved job config is invalid: {e}", "job_id": job.job_id}

            internal_config = await ActionDBSqlQueryRunner._build_internal_config(
                ctx=ctx, connection_name=restored.connection_name,
                user=self.user, db=self.db,
            )
            if isinstance(internal_config, Exception):
                await set_status(self.db, job.job_id, uid, JobStatus.failed)
                return {
                    "error": str(internal_config),
                    "code": DataPlaneErrorCode.CONNECTION_ERROR,
                    "job_id": job.job_id,
                }

            adapter = _get_adapter(internal_config)
            if restored.strategy == IngestStrategy.upsert and not adapter.supports_upsert():
                await set_status(self.db, job.job_id, uid, JobStatus.failed)
                return {
                    "error": (
                        "Upsert strategy requires PostgreSQL. "
                        "Use strategy='append' for other backends."
                    ),
                    "code": DataPlaneErrorCode.UNSUPPORTED_BACKEND_MODE,
                    "job_id": job.job_id,
                }

            _notify(notify, ctx, f"Resuming job {job.job_id} from shard {job.checkpoint.current_shard_index}")
            return await _run_ingest(self, restored, adapter, internal_config, job, ctx, app_state, notify)

        # ── ingest ───────────────────────────────────────────────────────────
        if config.mode == DataPlaneMode.ingest:
            if not config.table:
                return {"error": "table is required for ingest mode"}
            if not config.source_csv_urls:
                return {"error": "source_csv_urls is required for ingest mode"}
            if config.strategy == IngestStrategy.upsert and not config.conflict_keys:
                return {"error": "conflict_keys is required when strategy=upsert"}

            # Fail fast on unsafe identifiers before any job is created.
            try:
                _quote_table_reference(config.table)
                for c in config.columns or []:
                    _validate_identifier(c)
                for k in config.conflict_keys or []:
                    _validate_identifier(k)
            except ValueError as e:
                return {"error": str(e), "code": DataPlaneErrorCode.SCHEMA_MISMATCH}

            if config.idempotency_key:
                existing = await get_job(self.db, uid, idempotency_key=config.idempotency_key)
                if existing and existing.status == JobStatus.completed:
                    _notify(notify, ctx, f"Idempotent: cached result for {config.idempotency_key}")
                    return {
                        "job_id": existing.job_id, "mode": "ingest",
                        "status": "completed", "cached": True,
                        "metrics": existing.metrics.model_dump(mode="json"),
                    }

            job = await create_job(
                db=self.db, user_id=uid, mode="ingest",
                connection_name=config.connection_name,
                job_name=config.job_name,
                idempotency_key=config.idempotency_key,
                config_dict=config.model_dump(mode="json"),
                table=config.table,
            )

            internal_config = await ActionDBSqlQueryRunner._build_internal_config(
                ctx=ctx, connection_name=config.connection_name,
                user=self.user, db=self.db,
            )
            if isinstance(internal_config, Exception):
                await set_status(self.db, job.job_id, uid, JobStatus.failed)
                return {
                    "error": str(internal_config),
                    "code": DataPlaneErrorCode.CONNECTION_ERROR,
                    "job_id": job.job_id,
                }

            # ON CONFLICT syntax is PostgreSQL/SQLite only. Reject upsert on
            # backends whose adapter does not support it rather than silently
            # generating invalid SQL.
            adapter = _get_adapter(internal_config)
            if config.strategy == IngestStrategy.upsert and not adapter.supports_upsert():
                await set_status(self.db, job.job_id, uid, JobStatus.failed)
                return {
                    "error": (
                        "Upsert strategy requires PostgreSQL. "
                        "Use strategy='append' for other backends."
                    ),
                    "code": DataPlaneErrorCode.UNSUPPORTED_BACKEND_MODE,
                    "job_id": job.job_id,
                }

            return await _run_ingest(self, config, adapter, internal_config, job, ctx, app_state, notify)

        # ── query ────────────────────────────────────────────────────────────
        if config.mode == DataPlaneMode.query:
            if not config.sql:
                return {"error": "sql is required for query mode"}

            # Query mode is strictly read-only.
            forbidden = _check_query_safe(config.sql)
            if forbidden:
                return {
                    "error": (
                        f"Query mode is read-only: statement contains forbidden "
                        f"keyword {forbidden}. Use ingest mode to write data."
                    ),
                    "code": DataPlaneErrorCode.UNSUPPORTED_BACKEND_MODE,
                }

            internal_config = await ActionDBSqlQueryRunner._build_internal_config(
                ctx=ctx, connection_name=config.connection_name,
                user=self.user, db=self.db,
            )
            if isinstance(internal_config, Exception):
                return {"error": str(internal_config), "code": DataPlaneErrorCode.CONNECTION_ERROR}

            adapter = _get_adapter(internal_config)
            _notify(notify, ctx, "Running query …")
            engine = _create_engine(internal_config)

            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
            tmp_path = tmp.name
            tmp.close()
            try:
                row_count, truncated, fieldnames = await asyncio.to_thread(
                    _query_to_tmpfile_sync, engine, config, adapter, tmp_path,
                )

                if row_count == 0:
                    return {
                        "mode": "query",
                        "output_mode": "inline",
                        "rows": [],
                        "row_count": 0,
                        "truncated": truncated,
                        "max_rows_limit": config.max_rows,
                    }

                # Small results requested inline stay inline (bounded read-back).
                if (
                    config.query_output_mode == QueryOutputMode.inline
                    and row_count <= _ARTIFACT_ROW_THRESHOLD
                ):
                    rows = await asyncio.to_thread(_read_rows_from_csv, tmp_path)
                    return {
                        "mode": "query",
                        "output_mode": "inline",
                        "rows": rows,
                        "row_count": row_count,
                        "truncated": truncated,
                        "max_rows_limit": config.max_rows,
                    }

                # Everything else is artifact-backed: upload the CSV to S3 and
                # return a reference instead of inlining the data.
                user_files = UserFiles(user=self.user, s3=get_s3())
                filename = f"data_plane_query_{int(time.time() * 1000)}.csv"
                with open(tmp_path, "rb") as f:
                    file_metas = await user_files.upload_binary(
                        ctx=ctx, file_obj=f, filename=filename,
                    )
                artifact_url = str(file_metas[0].url) if file_metas else None
                if not artifact_url:
                    return {
                        "error": "Failed to upload query result artifact",
                        "code": DataPlaneErrorCode.CONNECTION_ERROR,
                        "row_count": row_count,
                        "truncated": truncated,
                    }
                return {
                    "mode": "query",
                    "output_mode": "artifact",
                    "artifact_url": artifact_url,
                    "row_count": row_count,
                    "truncated": truncated,
                    "columns": fieldnames,
                    "max_rows_limit": config.max_rows,
                }
            finally:
                engine.dispose()
                if os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        logger.bind(ctx=ctx).warning(
                            f"data_plane_v2 failed to remove temp file {tmp_path}"
                        )

        # ── inspect ──────────────────────────────────────────────────────────
        if config.mode == DataPlaneMode.inspect:
            if not config.table:
                return {"error": "table is required for inspect mode"}
            internal_config = await ActionDBSqlQueryRunner._build_internal_config(
                ctx=ctx, connection_name=config.connection_name,
                user=self.user, db=self.db,
            )
            if isinstance(internal_config, Exception):
                return {"error": str(internal_config), "code": DataPlaneErrorCode.CONNECTION_ERROR}
            _notify(notify, ctx, f"Inspecting {config.table} …")
            engine = _create_engine(internal_config)
            try:
                return await asyncio.to_thread(_inspect_sync, engine, config.table)
            finally:
                engine.dispose()

        return {
            "error": f"Unknown mode: {config.mode}",
            "code": DataPlaneErrorCode.UNSUPPORTED_BACKEND_MODE,
        }
