import asyncio
import json
import re
import time
from contextlib import nullcontext
from enum import Enum
from typing import Any, Callable, Iterable, Literal, Type
from urllib.parse import quote_plus

import sqlalchemy
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field, field_validator
from realtime import dataclass
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

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
from src.autobots.integrations.db_auth.db_auth import DatabaseAuth
from src.autobots.integrations.db_auth.user_db_auth import UserDbAuth
from src.autobots.core.cancellation.run_cancellation import RunCancellation
from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState

"""
Note: Connection String, also SqlAlchemy engine is of format:
dialect+driver://username:password@host:port/database
"""
PARALLEL_SELECT_LIMIT = 8
PARALLEL_SELECT_CHUNK_SIZE = max(1, PARALLEL_SELECT_LIMIT // 2)
_parallel_select_sem = asyncio.Semaphore(PARALLEL_SELECT_LIMIT) # preventing thread explosion and starvation of main event loop

def _chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]

def _notify(notify:Notify, ctx:Context, msg: str) -> None:
    if notify:
        notify(ctx, msg)

# Enum for databases
class DatabaseEnum(str, Enum):
    sqlite = "sqlite"
    mysql = "mysql"
    postgres = "postgres"
    bigquery = "bigquery"
    oracle = "oracle"
    snowflake = "snowflake"
    databricks = "databricks"


# mapping for enum values
SqlDialectPlusDriver = {
    DatabaseEnum.sqlite: "sqlite",
    DatabaseEnum.mysql: "mysql+pymysql",
    DatabaseEnum.postgres: "postgresql+psycopg2",
    DatabaseEnum.bigquery: "bigquery",
    DatabaseEnum.oracle: "oracle+oracledb",
    DatabaseEnum.snowflake: "snowflake",
    DatabaseEnum.databricks: "databricks",
}


# it's necessary to avoid uncessary failure at runtime for db_type evaluation
def _normalize_database_type(db_type_val: Any) -> DatabaseEnum:
    """
    Normalize database_type from auth_config which may be:
    - our DatabaseEnum
    - some other Enum type (from another module)
    - string like "postgres"
    - string like "DatabaseEnum.postgres"
    """
    if db_type_val is None:
        raise ValueError("database_type is required but not found in auth")

    if isinstance(db_type_val, Enum):
        raw = getattr(db_type_val, "value", str(db_type_val))
    else:
        raw = db_type_val

    s = str(raw).strip()
    if "." in s:
        s = s.split(".")[-1]
    s = s.lower()
    aliases = {
        "postgresql": "postgres",
        "pg": "postgres",
        "databricks-spark": "databricks",
        "spark": "databricks",
    }
    s = aliases.get(s, s)
    return DatabaseEnum(s)


class ActionDBSqlRunnerConfig(BaseModel):
    """
    Configuration for SQL runner action.

    Uses a saved database connection (looked up by connection_name) to execute SQL queries.
    Database credentials must be pre-configured via the database auth system.

    `sql_query` can be:
      - plain SQL string (single or multi-statement separated by ';')
      - OR JSON batch payload (recommended) for multi-statement with params.
    """

    connection_name: str
    sql_query: str | None = None


def _coerce_text_to_str(v: Any) -> str:
    """
    Accept dict/list payloads in action_input.text and convert them into a JSON string.
    This allows the action to receive:
      - {"text": {...}}  (dict)
      - {"text": [...]}  (list)
      - {"text": "..."}  (string)
    while still feeding a string into _parse_sql_input.
    """
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8", errors="replace")
        except Exception:
            return str(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    return str(v)


class SqlInputBlock(BaseModel):
    """
    Action input model for SQL runner.
    Allows `text` to be a string OR dict/list (auto JSON-dumped to string).
    """

    text: str | dict[str, Any] | list[Any] | None = None

    @field_validator("text", mode="before")
    @classmethod
    def _normalize_text(cls, v: Any) -> str | None:
        if v is None:
            return None
        return _coerce_text_to_str(v)


class SqlStatement(BaseModel):
    """
    One statement in a batch.
    - params: dict for single execute, OR list[dict] for executemany
    - fetch:
        auto: fetch rows only if result returns rows (SELECT / RETURNING)
        none: never fetch rows
        one: fetch one row
        all: fetch up to max_rows rows
    """

    name: str | None = None
    sql: str
    params: dict[str, Any] | list[dict[str, Any]] | None = None

    fetch: Literal["auto", "none", "one", "all"] = "auto"
    max_rows: int = Field(default=1000, ge=1, le=5000)


class SqlBatchRequest(BaseModel):
    """
    Batch request to run multiple statements.
    - transaction:
        true  -> run all statements in a single transaction (rollback all on error if stop_on_error=true)
        false -> commit per statement
    - stop_on_error:
        true  -> stop immediately on error
        false -> continue; when transaction=true, uses SAVEPOINT per statement (begin_nested) where supported
    """

    transaction: bool = True
    stop_on_error: bool = True
    statements: list[SqlStatement]


class _DBSqlRunnerInternalConfig(BaseModel):
    """Internal config with all database connection details for query execution."""

    database_type: DatabaseEnum
    host: str | None = None
    port: int | None = None
    database: str
    username: str | None = None
    password: str | None = None

    # BigQuery specific fields
    project_id: str | None = None
    dataset: str | None = None
    credentials_info: dict | None = None  # Service account JSON for BigQuery (user-provided)

    # Cloud SQL (Postgres) - alternative to IP whitelisting
    cloud_sql_instance_connection_name: str | None = None  # project:region:instance
    cloud_sql_use_private_ip: bool = False
    cloud_sql_enable_iam_auth: bool = False  # IAM DB auth (no password; user = IAM principal)

    # Snowflake specific fields
    account: str | None = None  # Snowflake account identifier (e.g. xy12345.us-east-1)
    warehouse: str | None = None  # Snowflake virtual warehouse
    db_schema: str | None = None  # Snowflake/Databricks schema name
    role: str | None = None  # Snowflake role

    # Databricks specific fields
    http_path: str | None = None  # Databricks SQL warehouse or cluster HTTP path
    access_token: str | None = None  # Databricks personal access token
    catalog: str | None = None  # Databricks Unity Catalog name

    def build_connection_string(self) -> str:
        dialect_driver = SqlDialectPlusDriver.get(self.database_type)
        if not dialect_driver:
            raise ValueError(f"Unsupported database_type: {self.database_type}")

        # SQLite special handling
        if self.database_type == DatabaseEnum.sqlite:
            db = (self.database or "").strip()
            if not db:
                raise ValueError("database is required for sqlite")
            if db == ":memory:":
                return f"{dialect_driver}:///:memory:"
            return f"{dialect_driver}:///{db}"

        # BigQuery: bigquery://project-id/dataset (credentials passed separately)
        if self.database_type == DatabaseEnum.bigquery:
            project = self.project_id or self.database
            if not project:
                raise ValueError("project_id or database is required for BigQuery")
            if self.dataset:
                return f"{dialect_driver}://{project}/{self.dataset}"
            return f"{dialect_driver}://{project}"

        # Snowflake: snowflake://user:password@account/database/schema?warehouse=X&role=Y
        if self.database_type == DatabaseEnum.snowflake:
            if not self.account:
                raise ValueError("account is required for Snowflake")
            user_q = quote_plus(self.username or "")
            pwd_q = quote_plus((self.password or "").strip())
            db = self.database or ""
            schema = self.db_schema or ""
            path = f"/{db}/{schema}" if (db and schema) else f"/{db}" if db else ""
            query_parts = []
            if self.warehouse:
                query_parts.append(f"warehouse={quote_plus(self.warehouse)}")
            if self.role:
                query_parts.append(f"role={quote_plus(self.role)}")
            query = f"?{'&'.join(query_parts)}" if query_parts else ""
            if user_q and pwd_q:
                return f"{dialect_driver}://{user_q}:{pwd_q}@{self.account}{path}{query}"
            return f"{dialect_driver}://{user_q}@{self.account}{path}{query}"

        # Databricks: databricks://token:<PAT>@<host>/<catalog>?http_path=X&schema=Y
        # Catalog is placed as the URL path segment (parsed as `database` by the dialect).
        # http_path and schema are query params. Verified against databricks-sqlalchemy==2.0.9.
        if self.database_type == DatabaseEnum.databricks:
            if not self.access_token:
                raise ValueError("access_token is required for Databricks")
            if not self.host:
                raise ValueError("host (server hostname) is required for Databricks")
            if not self.http_path:
                raise ValueError("http_path is required for Databricks")
            token_q = quote_plus(self.access_token)
            catalog = self.catalog or self.database or ""
            path = f"/{catalog}" if catalog else ""
            query_parts = [f"http_path={quote_plus(self.http_path)}"]
            if self.db_schema:
                query_parts.append(f"schema={quote_plus(self.db_schema)}")
            query = f"?{'&'.join(query_parts)}"
            return f"{dialect_driver}://token:{token_q}@{self.host}{path}{query}"

        # Traditional DBs (host not required for Cloud SQL Postgres)
        if not self.cloud_sql_instance_connection_name and not self.host:
            raise ValueError("host is required (or use cloud_sql_instance_connection_name for Cloud SQL)")
        if not self.database:
            raise ValueError("database is required")

        # Cloud SQL Postgres connects via the Cloud SQL Python Connector (see
        # run_sql_batch), so this connection string is never handed to a bare
        # create_engine(); it's a placeholder only.
        if (
            self.database_type == DatabaseEnum.postgres
            and self.cloud_sql_instance_connection_name
        ):
            return f"postgresql+pg8000://cloudsql/{self.database}"

        # Quote creds to handle special chars (@ : / #) safely, and only append
        # ":port" when a port is set (otherwise the dangling colon corrupts the URL
        # and SQLAlchemy parses the literal "None" as the port).
        user_q = quote_plus(self.username) if self.username else ""
        pwd_q = (
            quote_plus(self.password.strip())
            if (self.password and self.password.strip())
            else ""
        )
        host_port = f"{self.host}:{self.port}" if self.port is not None else (self.host or "")

        if user_q and pwd_q:
            return f"{dialect_driver}://{user_q}:{pwd_q}@{host_port}/{self.database}"
        if user_q:
            return f"{dialect_driver}://{user_q}@{host_port}/{self.database}"
        # If no username, still allow (some DSNs / auth)
        return f"{dialect_driver}://{host_port}/{self.database}"


def _json_dumps_safe(obj: Any) -> str:
    return json.dumps(obj, default=str)


def _looks_like_json(text: str) -> bool:
    t = (text or "").lstrip()
    return t.startswith("{") or t.startswith("[")


def _split_sql_statements(raw_sql: str) -> list[str]:
    """
    Minimal splitter for fallback raw SQL multi-statement.
    This does NOT handle semicolons inside strings perfectly.
    For robust multi-statement + params, use JSON batch payload.
    But it works better for now
    """
    parts = [p.strip() for p in (raw_sql or "").split(";")]
    return [p for p in parts if p]


def _default_fetch_mode(sql: str) -> Literal["auto", "none", "one", "all"]:
    s = (sql or "").strip().lower()
    # For raw SQL fallback, default to fetching rows for select/with
    if s.startswith("select") or s.startswith("with"):
        return "all"
    return "auto"


def _parse_sql_input(text: Any, fallback_sql: str | None) -> SqlBatchRequest:
    """
    Preferred: JSON batch payload in DataBlock.text (or config.sql_query).
    Fallback: raw SQL string (single or multi statement separated by ';'), no params.
    """
    t = _coerce_text_to_str(text).strip()
    if not t:
        t = (fallback_sql or "").strip()

    if not t:
        raise ValueError("sql_query is required (either action_input.text or config.sql_query)")

    # JSON batch payload
    if _looks_like_json(t):
        # Allow:
        #  - full batch object
        #  - or a list of statements
        try:
            obj = json.loads(t)
        except Exception as e:
            raise ValueError(f"Invalid JSON payload: {e}") from e

        if isinstance(obj, list):
            # list[SqlStatement]
            return SqlBatchRequest(
                transaction=True,
                stop_on_error=True,
                statements=[SqlStatement.model_validate(x) for x in obj],
            )
        return SqlBatchRequest.model_validate(obj)

    # Raw SQL fallback (single or multiple); no params
    statements = _split_sql_statements(t)
    if not statements:
        raise ValueError("No SQL statements found")

    return SqlBatchRequest(
        transaction=True,
        stop_on_error=True,
        statements=[SqlStatement(sql=s, fetch=_default_fetch_mode(s)) for s in statements],
    )


_FORBIDDEN_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("delete", re.compile(r"(?i)\bdelete\b")),
    ("drop", re.compile(r"(?i)\bdrop\b")),
    ("truncate", re.compile(r"(?i)\btruncate\b")),
    ("alter_drop", re.compile(r"(?i)\balter\b.*\bdrop\b", re.DOTALL)),
    # ("update", re.compile(r"(?i)\bupdate\b")),
    # ("insert", re.compile(r"(?i)\binsert\b")),
]


@dataclass
class SqlSafetyViolation:
    reason: str
    statement_index: int | None = None
    statement_name: str | None = None
    sql_preview: str | None = None


def _strip_sql_comments_and_literals(sql: str) -> str:
    if not sql:
        return ""

    # Remove block comments first
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    # Remove line comments
    sql = re.sub(r"--[^\n]*", " ", sql)
    # Remove single quoted strings: '...'
    sql = re.sub(r"'([^']|'')*'", "''", sql)
    # Remove double quoted strings: "..."
    sql = re.sub(r'"([^"]|"")*"', '""', sql)

    return sql


def _detect_forbidden_sql(sql: str) -> str | None:
    cleaned = _strip_sql_comments_and_literals(sql)
    for label, pat in _FORBIDDEN_PATTERNS:
        if pat.search(cleaned):
            return label
    return None


def _iter_sql_from_batch(batch: SqlBatchRequest) -> Iterable[tuple[int, str | None, str]]:
    for idx, st in enumerate(batch.statements):
        yield idx, st.name, st.sql


def validate_no_deletions_from_text_or_config(
    input_text: Any,
    fallback_sql: Any,
) -> SqlSafetyViolation | None:
    batch = _parse_sql_input(input_text, fallback_sql)
    for idx, name, sql in _iter_sql_from_batch(batch):
        hit = _detect_forbidden_sql(sql)
        if hit:
            return SqlSafetyViolation(
                reason=f"Forbidden SQL operation detected: {hit}",
                statement_index=idx,
                statement_name=name,
                sql_preview=(sql or "")[:2000],
            )
    return None

def build_connect_args(
    cfg: _DBSqlRunnerInternalConfig,
    timeout_sec: int = 1800,
) -> dict[str, Any]:
    if cfg.database_type == DatabaseEnum.postgres:
        return {"connect_timeout": timeout_sec}

    if cfg.database_type == DatabaseEnum.mysql:
        return {
            "connect_timeout": timeout_sec,
            "read_timeout": timeout_sec,
            "write_timeout": timeout_sec,
        }

    if cfg.database_type == DatabaseEnum.oracle:
        return {"timeout": timeout_sec}

    if cfg.database_type == DatabaseEnum.snowflake:
        return {"login_timeout": timeout_sec, "network_timeout": timeout_sec}

    return {}

def is_read_only_sql(sql: str) -> bool:
    s = (sql or "").strip().lower()
    return s.startswith("select") or s.startswith("with")


def can_run_parallel(batch: SqlBatchRequest) -> bool:
    return (
        batch.transaction is False
        and bool(batch.statements)
        and all(is_read_only_sql(st.sql) for st in batch.statements)
    )

def apply_statement_timeout(session, ms: int) -> None:
    session.execute(
        sqlalchemy.text("SET LOCAL statement_timeout = :ms"),
        {"ms": ms},
    )


def _no_driver_cancel() -> None:
    return None


def _dbapi_connection(session) -> Any | None:
    try:
        return session.connection().connection.dbapi_connection
    except Exception:
        return None


def _build_driver_cancel(
    internal_config: _DBSqlRunnerInternalConfig,
    dbapi_connection: Any,
) -> Callable[[], None] | None:
    database_type = internal_config.database_type

    if database_type == DatabaseEnum.postgres:
        postgres_cancel = getattr(dbapi_connection, "cancel", None)
        if not callable(postgres_cancel):
            return None

        def cancel_postgres_query() -> None:
            try:
                postgres_cancel()
            except Exception as e:
                logger.bind(code=AppCode.SQL_RUNNER).warning(f"Postgres query cancel failed: {e}")

        return cancel_postgres_query

    if database_type == DatabaseEnum.sqlite:
        sqlite_interrupt = getattr(dbapi_connection, "interrupt", None)
        if not callable(sqlite_interrupt):
            return None

        def cancel_sqlite_query() -> None:
            try:
                sqlite_interrupt()
            except Exception as e:
                logger.bind(code=AppCode.SQL_RUNNER).warning(f"SQLite query interrupt failed: {e}")

        return cancel_sqlite_query

    if database_type == DatabaseEnum.mysql:
        if not internal_config.host or not internal_config.username:
            return None
        try:
            mysql_thread_id = int(dbapi_connection.thread_id())
        except Exception:
            return None

        def cancel_mysql_query() -> None:
            killer_connection = None
            try:
                import pymysql

                killer_connection = pymysql.connect(
                    host=internal_config.host,
                    port=internal_config.port or 3306,
                    user=internal_config.username,
                    password=internal_config.password or "",
                    database=internal_config.database or None,
                    connect_timeout=10,
                )
                with killer_connection.cursor() as cursor:
                    cursor.execute("KILL QUERY %s", (mysql_thread_id,))
            except Exception as e:
                logger.bind(code=AppCode.SQL_RUNNER).warning(f"MySQL query kill failed: {e}")
            finally:
                if killer_connection is not None:
                    try:
                        killer_connection.close()
                    except Exception:
                        logger.bind(code=AppCode.SQL_RUNNER).warning(
                            "Closing MySQL kill connection failed"
                        )

        return cancel_mysql_query

    return None


def register_query_cancel(
    cancellation: RunCancellation | None,
    session,
    internal_config: _DBSqlRunnerInternalConfig,
) -> Callable[[], None]:
    if cancellation is None:
        return _no_driver_cancel

    dbapi_connection = _dbapi_connection(session)
    if dbapi_connection is None:
        return _no_driver_cancel

    driver_cancel = _build_driver_cancel(internal_config, dbapi_connection)
    if driver_cancel is None:
        return _no_driver_cancel

    return cancellation.register_driver_cancel(driver_cancel)


class ActionDBSqlQueryRunner(
    ActionABC[
        ActionDBSqlRunnerConfig,
        ActionDBSqlRunnerConfig,
        ActionDBSqlRunnerConfig,
        DataBlock,
        DataBlocks,
    ]
):
    type = ActionType.database

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return ActionDBSqlRunnerConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return ActionDBSqlRunnerConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return ActionDBSqlRunnerConfig

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    @staticmethod
    def get_description() -> str:
        return """
PREREQUISITE: Before calling this tool, call get_database_connection_names to get the database connection names the user is connected to and has access to.

Description
---------------
Executes SQL queries on a saved database connection (read/write).  
Supports single or multi-statement execution, parameters, executemany batch inserts/updates, and transactional execution.
It does not supports any kind of deletion in saved database.

Input Structure
---------------
Configuration (ActionDBSqlRunnerConfig):
{
  "connection_name": "<registered DB connection>",
  "sql_query": "<SQL string or JSON batch payload>"
}

Configuration.sql_query:
- Must be a string.
- The string may contain either:
    1. Raw SQL (single or multi-statement separated by ';')
    2. A JSON batch payload encoded as a string (recommended for multi-statement with params)

Example Input
-------------
{
  "connection_name": "postgres",
  "sql_query": "{\"transaction\": true, \"stop_on_error\": true, \"statements\": [{\"name\": \"create_table\", \"sql\": \"CREATE TABLE IF NOT EXISTS users_demo (id INT PRIMARY KEY, name VARCHAR(100));\", \"fetch\": \"none\"}, {\"name\": \"insert_users\", \"sql\": \"INSERT INTO users_demo (id, name) VALUES (:id, :name)\", \"params\": [{\"id\": 1, \"name\": \"Alice\"}, {\"id\": 2, \"name\": \"Bob\"}, {\"id\": 3, \"name\": \"Charlie\"}], \"fetch\": \"none\"}, {\"name\": \"read_all_users\", \"sql\": \"SELECT * FROM users_demo\", \"fetch\": \"all\", \"max_rows\": 100}]}"
}

Execution Behavior
------------------
- Parameters bindings support expanding lists for IN queries
- executemany triggered when params is a list of dicts
- In transaction mode:
    stop_on_error=true → stop & rollback all
    stop_on_error=false → use nested savepoints when supported
- Per-statement execution time recorded


Output Format
-------------
Returns DataBlocks where:
- Each SQL statement produces one DataBlock with:
    { index, name, ok, rowcount, rows?, truncated?, error?, execution_ms, sql_preview }
- Final summary DataBlock:
    {
      "summary": {
        "statements": <count>,
        "ok": <success count>,
        "failed": <failure count>,
        "transaction": <bool>,
        "stop_on_error": <bool>,
        "total_ms": <duration>
      }
    }

        """

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        try:
            violation = validate_no_deletions_from_text_or_config(
                input_text=action_input.text,
                fallback_sql=self.action_config.sql_query,
            )
            if violation:
                return Exception(
                    _json_dumps_safe(
                        {
                            "error": {
                                "message": violation.reason,
                                "blocked": True,
                                "failed_index": violation.statement_index,
                                "failed_name": violation.statement_name,
                                "failed_sql_preview": violation.sql_preview,
                            }
                        }
                    )
                )
            else:
                try:
                    _notify(notify, ctx, f"Parsing the sql batch {action_input.text} ")
                    batch = _parse_sql_input(action_input.text, self.action_config.sql_query)
                except Exception as e:
                    return e
                
                _notify(notify, ctx, "Building the config for database connection ")
                internal_config = await ActionDBSqlQueryRunner._build_internal_config(
                    ctx=ctx,
                    connection_name=self.action_config.connection_name,
                    user=self.user,
                    db=self.db,
                )
                if isinstance(internal_config, Exception):
                    return internal_config

                return await ActionDBSqlQueryRunner.run_sql_batch(
                    ctx=ctx,
                    internal_config=internal_config,
                    batch=batch,
                    notify = notify,
                    cancellation=app_state.cancellation,
                )
        except Exception as e:
            return e

    async def run_tool(
        self, action_config: ActionDBSqlRunnerConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        try:
            violation = validate_no_deletions_from_text_or_config(
                input_text=action_config.sql_query or "",
                fallback_sql=None,
            )
            if violation:
                return Exception(
                    _json_dumps_safe(
                        {
                            "error": {
                                "message": violation.reason,
                                "blocked": True,
                                "failed_index": violation.statement_index,
                                "failed_name": violation.statement_name,
                                "failed_sql_preview": violation.sql_preview,
                            }
                        }
                    )
                )
            else:
                try:
                    batch = _parse_sql_input(action_config.sql_query or "", None)
                except Exception as e:
                    return e

                internal_config = await ActionDBSqlQueryRunner._build_internal_config(
                    ctx=ctx,
                    connection_name=action_config.connection_name,
                    user=self.user,
                    db=self.db,
                )
                if isinstance(internal_config, Exception):
                    return internal_config

                return await ActionDBSqlQueryRunner.run_sql_batch(
                    ctx=ctx,
                    internal_config=internal_config,
                    batch=batch,
                    notify=notify,
                    cancellation=app_state.cancellation,
                )
        except Exception as e:
            return e

    @staticmethod
    async def _build_internal_config(
        ctx: Context,
        connection_name: str,
        user: UserORM,
        db: AsyncIOMotorDatabase,
    ) -> _DBSqlRunnerInternalConfig | Exception:
        """
        Fetch database auth from user credentials by connection_name and build internal config.
        """
        try:
            db_auth_doc = await UserDbAuth.get_by_name(
                ctx=ctx, db=db, user=user, connection_name=connection_name, decrypt=True
            )
            if not db_auth_doc:
                return Exception(f"No database auth found for connection_name: {connection_name}")

            database_auth = DatabaseAuth.get_auth_model_type().model_validate(db_auth_doc.auth)
            auth_config = await DatabaseAuth.get_auth_config(ctx=ctx, app_auth=database_auth)

            try:
                database_type = _normalize_database_type(auth_config.get("database_type"))
            except Exception as e:
                return Exception(str(e))

            # BigQuery
            if database_type == DatabaseEnum.bigquery:
                if not auth_config.get("project_id") and not auth_config.get("database"):
                    return Exception("project_id or database is required for BigQuery")
                if not auth_config.get("credentials_info"):
                    return Exception(
                        "credentials_info (service account JSON) is required for BigQuery"
                    )

                return _DBSqlRunnerInternalConfig(
                    database_type=DatabaseEnum.bigquery,
                    database=auth_config.get("database", auth_config.get("project_id", "")) or "",
                    project_id=auth_config.get("project_id"),
                    dataset=auth_config.get("dataset"),
                    credentials_info=auth_config.get("credentials_info"),
                )

            # SQLite
            if database_type == DatabaseEnum.sqlite:
                if not auth_config.get("database"):
                    return Exception(
                        "database (sqlite file path or :memory:) is required but not found in auth"
                    )
                return _DBSqlRunnerInternalConfig(
                    database_type=DatabaseEnum.sqlite,
                    database=auth_config["database"],
                )

            # Snowflake
            if database_type == DatabaseEnum.snowflake:
                if not auth_config.get("account"):
                    return Exception("account is required for Snowflake")
                if not auth_config.get("username"):
                    return Exception("username is required for Snowflake")
                return _DBSqlRunnerInternalConfig(
                    database_type=DatabaseEnum.snowflake,
                    database=auth_config.get("database", ""),
                    account=auth_config.get("account"),
                    username=auth_config.get("username"),
                    password=auth_config.get("password"),
                    warehouse=auth_config.get("warehouse"),
                    db_schema=auth_config.get("schema"),
                    role=auth_config.get("role"),
                )

            # Databricks
            if database_type == DatabaseEnum.databricks:
                if not auth_config.get("host"):
                    return Exception("host (server hostname) is required for Databricks")
                if not auth_config.get("access_token"):
                    return Exception("access_token is required for Databricks")
                if not auth_config.get("http_path"):
                    return Exception("http_path is required for Databricks")
                return _DBSqlRunnerInternalConfig(
                    database_type=DatabaseEnum.databricks,
                    host=auth_config.get("host"),
                    database=auth_config.get("catalog") or auth_config.get("database") or "",
                    access_token=auth_config.get("access_token"),
                    http_path=auth_config.get("http_path"),
                    catalog=auth_config.get("catalog"),
                    db_schema=auth_config.get("schema"),
                )

            # Traditional DBs (host not required for Cloud SQL Postgres)
            use_cloud_sql = bool(auth_config.get("cloud_sql_instance_connection_name"))
            if not use_cloud_sql and not auth_config.get("host"):
                return Exception("host is required but not found in auth (or use cloud_sql_instance_connection_name for Cloud SQL)")
            if not auth_config.get("database"):
                return Exception("database is required but not found in auth")

            return _DBSqlRunnerInternalConfig(
                database_type=database_type,
                host=auth_config.get("host"),
                port=auth_config.get("port"),
                database=auth_config["database"],
                username=auth_config.get("username"),
                password=auth_config.get("password"),
                cloud_sql_instance_connection_name=auth_config.get("cloud_sql_instance_connection_name"),
                cloud_sql_use_private_ip=auth_config.get("cloud_sql_use_private_ip", False),
                cloud_sql_enable_iam_auth=auth_config.get("cloud_sql_enable_iam_auth", False),
                credentials_info=auth_config.get("credentials_info"),
            )

        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.DB_RUNNER, connection_name=connection_name).exception(
                "Error fetching database auth from user credentials"
            )
            return e

    @staticmethod
    def execute_single_statement(
        SessionLocal,
        internal_config : _DBSqlRunnerInternalConfig,
        idx: int,
        st: SqlStatement,
        cancellation: RunCancellation | None = None,
    ) -> dict[str, Any]:
        started = time.time()
        sql_preview = (st.sql or "")[:2000]

        if cancellation is not None and cancellation.is_cancelled:
            return {
                "index": idx,
                "name": st.name,
                "ok": False,
                "rowcount": None,
                "rows": None,
                "truncated": None,
                "error": "cancelled before execution",
                "sql_preview": sql_preview,
                "elapsed_ms": 0,
            }

        out = {
            "index": idx,
            "name": st.name,
            "ok": False,
            "rowcount": None,
            "rows": None,
            "truncated": None,
            "error": None,
            "execution_ms": None,
            "sql_preview": sql_preview,
        }

        logger.bind(index=idx, code=AppCode.SQL_RUNNER, name=st.name).info("Executing SQL statement")

        with SessionLocal() as session:
            with session.begin():
                # Apply Postgres query timeout only we have to apply for this.
                if internal_config.database_type == DatabaseEnum.postgres:
                    apply_statement_timeout(session, 30 * 60 * 1000)

                remove_driver_cancel = register_query_cancel(
                    cancellation, session, internal_config
                )

                try:
                    if isinstance(st.params, list):
                        # executemany: list[dict] → pass directly, no expanding bindparams
                        clause = sqlalchemy.text(st.sql)
                        rs = session.execute(clause, st.params)
                    else:
                        params = st.params or {}
                        clause = sqlalchemy.text(st.sql)
                        if params:
                            for k, v in params.items():
                                if isinstance(v, (list, tuple, set)):
                                    clause = clause.bindparams(
                                        sqlalchemy.bindparam(k, expanding=True)
                                    )
                        rs = session.execute(clause, params)

                    out["rowcount"] = getattr(rs, "rowcount", None)

                    fetch_mode = st.fetch
                    if fetch_mode == "auto":
                        fetch_mode = "all" if rs.returns_rows else "none"

                    if fetch_mode != "none" and rs.returns_rows:
                        m = rs.mappings()
                        fetched = m.fetchmany(st.max_rows + 1)
                        out["truncated"] = len(fetched) > st.max_rows
                        out["rows"] = [dict(r) for r in fetched[: st.max_rows]]

                    out["ok"] = True

                except Exception as e:
                    out["error"] = str(e)
                    logger.bind(index=idx, code=AppCode.SQL_RUNNER, name=st.name).exception("SQL execution failed")

                finally:
                    remove_driver_cancel()
                    out["execution_ms"] = int((time.time() - started) * 1000)
        return out

    # this static method handles executing batch of sql query with params also
    @staticmethod
    async def run_sql_batch(
        ctx: Context,
        internal_config: _DBSqlRunnerInternalConfig,
        batch: SqlBatchRequest,
        notify: Notify | None = None,
        cancellation: RunCancellation | None = None,
    ) -> DataBlocks | Exception:
        logger.bind(ctx=ctx, code=AppCode.SQL_RUNNER).info(
            f"Starting run_sql_batch statements={len(batch.statements)} "
            f"transaction={batch.transaction} stop_on_error={batch.stop_on_error}"
        )

        # preventing unbounded exception
        engine = None
        try:
            # Create engine (BigQuery special handling)

            _notify(notify, ctx, "Creating db connection engine ")
            # Only build the connection string in the branches that actually use
            # it. Cloud SQL connects via the connector (no connection string), so
            # building it eagerly would needlessly exercise the host/port path.
            if internal_config.database_type == DatabaseEnum.bigquery:
                engine = create_engine(
                    internal_config.build_connection_string(),
                    credentials_info=internal_config.credentials_info,
                    pool_pre_ping=True,
                )
            elif (
                internal_config.database_type == DatabaseEnum.postgres
                and internal_config.cloud_sql_instance_connection_name
            ):
                from src.autobots.core.database.cloud_sql import create_cloud_sql_engine

                engine = create_cloud_sql_engine(
                    instance_connection_name=internal_config.cloud_sql_instance_connection_name,
                    user=internal_config.username or "",
                    password=internal_config.password or "",
                    database=internal_config.database,
                    use_private_ip=internal_config.cloud_sql_use_private_ip,
                    enable_iam_auth=internal_config.cloud_sql_enable_iam_auth,
                    credentials_info=internal_config.credentials_info,
                )
            else:
                connect_args = build_connect_args(internal_config, timeout_sec=1800)
                engine = create_engine(
                    internal_config.build_connection_string(),
                    pool_pre_ping=True,
                    connect_args=connect_args,
                )

            SessionLocal = sessionmaker(bind=engine)

            # Parallel SELECT-only execution
            if can_run_parallel(batch):
                logger.bind(ctx=ctx, code=AppCode.DB_RUNNER).info(
                    "Parallel SELECT execution enabled",
                    statements=len(batch.statements),
                )
                _notify(notify, ctx, "Batch is started running in parallel ")
                started = time.time()

                async def _run_with_semaphore(fn, *args):
                    async with _parallel_select_sem:
                        return await asyncio.to_thread(fn, *args)
                
                # running parallel and gather as it completed , no guarante for order but we have already index
                # and so we will able to still preserve the ordering 
                blocks = DataBlocks()
                started = time.time()

                tasks = [
                    _run_with_semaphore(
                        ActionDBSqlQueryRunner.execute_single_statement,
                        SessionLocal,
                        internal_config,
                        idx,
                        st,
                        cancellation,
                    )
                    for idx, st in enumerate(batch.statements)
                ]

                all_results: list[dict[str, Any]] = []

                for chunk in _chunked(tasks, PARALLEL_SELECT_CHUNK_SIZE):
                    chunk_results = await asyncio.gather(*chunk)

                    # preserve ordering using index
                    chunk_results.sort(key=lambda r: r["index"])

                    for r in chunk_results:
                        blocks.blocks.append(DataBlock(text=_json_dumps_safe(r)))
                        all_results.append(r)

                    _notify(
                        notify,
                        ctx,
                        f"Completed {len(all_results)}/{len(batch.statements)} SELECT statements",
                    )

                total_ms = int((time.time() - started) * 1000)

                ok_count = sum(1 for r in all_results if r.get("ok"))

                blocks.blocks.append(
                    DataBlock(
                        text=_json_dumps_safe(
                            {
                                "summary": {
                                    "statements": len(all_results),
                                    "ok": ok_count,
                                    "failed": len(all_results) - ok_count,
                                    "transaction": False,
                                    "stop_on_error": batch.stop_on_error,
                                    "rolled_back": False,
                                    "fatal_error": None,
                                    "total_ms": total_ms,
                                }
                            }
                        )
                    )
                )

                return blocks
            
            # here ordering matters so no parallel but yeah it will be parallel computed at request level not single statement level.
            blocks = DataBlocks()
            started = time.time()

            def _prepare_text_clause(
                sql: str, params: dict[str, Any] | None
            ) -> sqlalchemy.sql.elements.TextClause:
                clause = sqlalchemy.text(sql)
                # Auto-enable "expanding" for list/tuple/set params (use `IN :ids` style)
                if params:
                    for k, v in params.items():
                        if isinstance(v, (list, tuple, set)):
                            clause = clause.bindparams(sqlalchemy.bindparam(k, expanding=True))
                return clause

            def exec_batch_sync() -> dict[str, Any]:
                """
                Always returns partial results even if stop_on_error=True triggers a rollback.
                It will trigger to previous state of table and also give output with success query and at last
                append the failed error caused due to wrong sql query and for if llm will use it as tool so that i
                added few new things like failed_sql_preview so that llm can understand which sql query failed and why.
                Returns:
                  {
                    "results": [...],
                    "fatal_error": str|None,
                    "rolled_back": bool,
                    "failed_index": int|None,
                    "failed_name": str|None,
                    "failed_sql_preview": str|None,
                  }
                """
                results: list[dict[str, Any]] = []
                fatal_error: str | None = None
                rolled_back: bool = False

                failed_index: int | None = None
                failed_name: str | None = None
                failed_sql_preview: str | None = None

                with SessionLocal() as session:
                    outer_ctx = session.begin() if batch.transaction else nullcontext()

                    try:
                        # If transaction=True and stop_on_error=True, we MUST allow an exception
                        # to escape this `with outer_ctx:` so SQLAlchemy rolls back automatically.
                        # it's simple as described earlier
                        with outer_ctx:
                            for idx, st in enumerate(batch.statements):
                                if cancellation is not None and cancellation.is_cancelled:
                                    break

                                st_started = time.time()

                                # Put sql_preview early so it exists even if the statement fails
                                sql_preview = (st.sql or "")[:2000]

                                logger.bind(ctx = ctx, code=AppCode.SQL_RUNNER).debug(f"sql preview at index {idx} is {sql_preview[:50]}")

                                out: dict[str, Any] = {
                                    "index": idx,
                                    "name": st.name,
                                    "ok": False,
                                    "rowcount": None,
                                    "rows": None,
                                    "truncated": None,
                                    "error": None,
                                    "execution_ms": None,
                                    "sql_preview": sql_preview,
                                }

                                stop_after_this = False

                                def _run_one_statement() -> None:
                                    if isinstance(st.params, list):
                                        # executemany
                                        clause = sqlalchemy.text(st.sql)
                                        rs = session.execute(clause, st.params)
                                    else:
                                        params = st.params or {}
                                        clause = _prepare_text_clause(st.sql, params)
                                        rs = session.execute(clause, params)

                                    out["rowcount"] = getattr(rs, "rowcount", None)

                                    fetch_mode = st.fetch
                                    if fetch_mode == "auto":
                                        fetch_mode = (
                                            "all" if getattr(rs, "returns_rows", False) else "none"
                                        )

                                    if fetch_mode != "none" and getattr(rs, "returns_rows", False):
                                        m = rs.mappings()
                                        if fetch_mode == "one":
                                            row = m.first()
                                            out["rows"] = [] if row is None else [dict(row)]
                                            out["truncated"] = False
                                        else:
                                            fetched = m.fetchmany(st.max_rows + 1)
                                            truncated = len(fetched) > st.max_rows
                                            if truncated:
                                                fetched = fetched[: st.max_rows]
                                            out["rows"] = [dict(r) for r in fetched]
                                            out["truncated"] = truncated
                                    else:
                                        out["rows"] = None
                                        out["truncated"] = None

                                remove_driver_cancel = register_query_cancel(
                                    cancellation, session, internal_config
                                )

                                try:
                                    if batch.transaction:
                                        if batch.stop_on_error:
                                            _run_one_statement()
                                        else:
                                            try:
                                                with session.begin_nested():
                                                    _run_one_statement()
                                            except Exception as e_nested:
                                                out["error"] = str(e_nested)
                                    else:
                                        # No outer transaction: commit per statement
                                        if batch.stop_on_error:
                                            _run_one_statement()
                                            session.commit()
                                        else:
                                            try:
                                                _run_one_statement()
                                                session.commit()
                                            except Exception as e_nonstop:
                                                session.rollback()
                                                out["error"] = str(e_nonstop)

                                except Exception as e:
                                    # Statement failed
                                    out["error"] = str(e)
                                    fatal_error = fatal_error or str(e)

                                    # Capture first failure details
                                    if failed_index is None:
                                        failed_index = out["index"]
                                        failed_name = out.get("name")
                                        failed_sql_preview = out.get("sql_preview")

                                    if not batch.transaction:
                                        # per-statement cleanup it's necessary
                                        try:
                                            # here we will rollback to previous state of table as usual
                                            session.rollback()
                                        except Exception:
                                            pass
                                        if batch.stop_on_error:
                                            stop_after_this = True
                                    else:
                                        # now it will trigger full rollback
                                        if batch.stop_on_error:
                                            raise

                                finally:
                                    remove_driver_cancel()
                                    out["ok"] = out["error"] is None
                                    out["execution_ms"] = int((time.time() - st_started) * 1000)
                                    results.append(out)

                                if stop_after_this:
                                    break

                    except Exception as e_outer:
                        # We reach here when transaction=True and stop_on_error=True caused a raise.
                        # The transaction has been rolled back automatically by SQLAlchemy.
                        fatal_error = fatal_error or str(e_outer)
                        if batch.transaction and batch.stop_on_error:
                            rolled_back = True

                        # Extra safety rollback it's added optionaly for safely handling it.
                        try:
                            session.rollback()
                        except Exception:
                            pass

                return {
                    "results": results,
                    "fatal_error": fatal_error,
                    "rolled_back": rolled_back,
                    "failed_index": failed_index,
                    "failed_name": failed_name,
                    "failed_sql_preview": failed_sql_preview,
                }

            # Run blocking SQLAlchemy work in a worker thread
            payload = await asyncio.to_thread(exec_batch_sync)

            batch_results = payload["results"]
            fatal_error = payload["fatal_error"]
            rolled_back = payload["rolled_back"]
            failed_index = payload.get("failed_index")
            failed_name = payload.get("failed_name")
            failed_sql_preview = payload.get("failed_sql_preview")

            total_ms = int((time.time() - started) * 1000)

            # One DataBlock per statement result
            for r in batch_results:
                blocks.blocks.append(DataBlock(text=_json_dumps_safe(r)))

            # Append fatal error info - it make sense
            if fatal_error:
                blocks.blocks.append(
                    DataBlock(
                        text=_json_dumps_safe(
                            {
                                "error": {
                                    "message": fatal_error,
                                    "rolled_back": rolled_back,
                                    "transaction": batch.transaction,
                                    "stop_on_error": batch.stop_on_error,
                                    "failed_index": failed_index,
                                    "failed_name": failed_name,
                                    "failed_sql_preview": failed_sql_preview,
                                }
                            }
                        )
                    )
                )

            # Final summary
            ok_count = sum(1 for r in batch_results if r.get("ok"))
            blocks.blocks.append(
                DataBlock(
                    text=_json_dumps_safe(
                        {
                            "summary": {
                                "statements": len(batch_results),
                                "ok": ok_count,
                                "failed": len(batch_results) - ok_count,
                                "transaction": batch.transaction,
                                "stop_on_error": batch.stop_on_error,
                                "rolled_back": rolled_back,
                                "fatal_error": fatal_error,
                                "failed_index": failed_index,
                                "failed_name": failed_name,
                                "total_ms": total_ms,
                            }
                        }
                    )
                )
            )

            return blocks

        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.SQL_RUNNER).exception("Error in run_sql_batch")
            return e

        finally:
            if engine is not None:
                try:
                    engine.dispose()
                    logger.bind(ctx=ctx, code=AppCode.DB_RUNNER).info("Disposed DB engine")
                    _notify(notify, ctx, "Disposed DB engine ")
                except Exception:
                    logger.bind(ctx=ctx, code=AppCode.DB_RUNNER).exception("Error disposing engine in run_sql_batch")
