import json
from enum import Enum
from typing import Type
from urllib.parse import quote_plus

import sqlalchemy
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel
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
from src.autobots.action.action_type.action_text2text.action_sql_runner.action_db_sql_runner import (
    register_query_cancel,
)
from src.autobots.integrations.db_auth.db_auth import DatabaseAuth
from src.autobots.integrations.db_auth.user_db_auth import UserDbAuth
from src.autobots.core.cancellation.blocking_work import run_blocking
from src.autobots.core.cancellation.run_cancellation import RunCancellation
from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM

"""
Note:Connection String, also SqlAlchemy engine is of format:
dialect+driver://username:password@host:port/database
"""


class DatabaseEnum(str, Enum):
    sqlite = "sqlite"
    mysql = "mysql"
    postgres = "postgres"
    bigquery = "bigquery"
    oracle = "oracle"


SqlDialectPlusDriver = {
    DatabaseEnum.sqlite: "sqlite",
    DatabaseEnum.mysql: "mysql+pymysql",
    DatabaseEnum.postgres: "postgresql+psycopg2",
    DatabaseEnum.bigquery: "bigquery",
    DatabaseEnum.oracle: "oracle+oracledb",
}


class ActionSqlRunnerConfig(BaseModel):
    """
    Configuration for SQL runner action.

    Uses a saved database connection (looked up by connection_name) to execute SQL queries.
    Database credentials must be pre-configured via the database auth system.
    """

    connection_name: str  # Name of the saved database connection to use
    sql_query: str | None = None  # SQL query to execute (SELECT only)


class _SqlRunnerInternalConfig(BaseModel):
    """Internal config with all database connection details for query execution."""

    database_type: DatabaseEnum
    host: str | None = None
    port: int | None = None
    database: str
    username: str | None = None
    password: str | None = None
    sql_query: str
    # BigQuery specific fields
    project_id: str | None = None  # GCP project ID for BigQuery
    dataset: str | None = None  # BigQuery dataset name
    credentials_info: dict | None = None  # Service account JSON for BigQuery (user-provided)
    # Cloud SQL (Postgres) - alternative to IP whitelisting
    cloud_sql_instance_connection_name: str | None = None  # project:region:instance
    cloud_sql_use_private_ip: bool = False
    cloud_sql_enable_iam_auth: bool = False  # IAM DB auth (no password; user = IAM principal)
    # Optional hardening knobs. Both default to None, which preserves the historical
    # behaviour for every caller that does not opt in (e.g. the SQL-runner LLM tool).
    # Only the Daytona connections DB bridge sets them today.
    statement_timeout_ms: int | None = None  # DB-side statement timeout (plain Postgres only)
    max_response_bytes: int | None = None  # cap on cumulative serialized row bytes

    def build_connection_string(self) -> str:
        dialect_driver = SqlDialectPlusDriver.get(self.database_type)

        # BigQuery uses a different connection format: bigquery://project-id/dataset
        # Credentials are passed separately via create_engine() credentials_info parameter
        if self.database_type == DatabaseEnum.bigquery:
            project = self.project_id or self.database
            if self.dataset:
                return f"{dialect_driver}://{project}/{self.dataset}"
            return f"{dialect_driver}://{project}"

        # Cloud SQL Postgres connects via the Cloud SQL Python Connector (see
        # _run_select_sync), so this connection string is never handed to a bare
        # create_engine(); it's a placeholder only.
        if (
            self.database_type == DatabaseEnum.postgres
            and self.cloud_sql_instance_connection_name
        ):
            return f"postgresql+pg8000://cloudsql/{self.database}"

        # SQLite has no host/port/credentials
        if self.database_type == DatabaseEnum.sqlite:
            return f"{dialect_driver}:///{self.database}"

        # Oracle and other traditional databases. Quote credentials so special
        # chars (@ : / #) don't corrupt the URL, and only append ":port" when a
        # port is set (otherwise SQLAlchemy parses the literal "None" as the port
        # and raises invalid literal for int()).
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
        return f"{dialect_driver}://{host_port}/{self.database}"


class ActionSqlRunner(
    ActionABC[
        ActionSqlRunnerConfig, ActionSqlRunnerConfig, ActionSqlRunnerConfig, DataBlock, DataBlocks
    ]
):
    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return ActionSqlRunnerConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return ActionSqlRunnerConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return ActionSqlRunnerConfig

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_description() -> str:
        return """
        PREREQUISITE: Before calling this tool, call get_database_connection_names to get the database connection names the user is connected to and has access to.

        Description
        -----------
        Executes a SQL SELECT query against a relational database and returns the
        results as a sequence of `DataBlock` entries (each row serialized to a
        JSON string). Database credentials are looked up by connection_name from
        the user's saved database connections. Only SELECT queries are permitted;
        non-SELECT queries will cause an error. A hard row limit of 1000 results
        is enforced to prevent excessive memory usage.

        Input
        -----
        config : dict
            Expected structure:
                {
                    "connection_name": "<name of saved database connection>",
                    "sql_query": "<SELECT ... query>"
                }

            connection_name : str
                The name of a pre-configured database connection saved in the
                user's database auth settings. This name is used to look up
                the database credentials (type, host, port, database, username,
                password).

            sql_query : str
                The SQL to execute. Must be a SELECT statement. If runtime
                input text is provided to the action, it will replace or
                override this value.

        Output
        ------
        DataBlocks | Exception
            Expected structure on success:
                {
                    "blocks": [
                        { "text": "<json-serialized-row-1>" },
                        { "text": "<json-serialized-row-2>" },
                        ...
                    ]
                }

            Each `DataBlock.text` contains a single row serialized as JSON.
            Behavior and guarantees:
              - Only SELECT queries are allowed; an assertion enforces this.
              - Results are iterated and converted to JSON; a hard limit of 1000
                rows is applied.
              - On network, driver, SQL syntax, or other unexpected errors the
                exception is logged and returned.
        """

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        # Use action_input text as sql_query if provided, otherwise use config sql_query
        sql_query = (
            action_input.text.strip() if action_input.text.strip() else self.action_config.sql_query
        )
        if not sql_query:
            return Exception("sql_query is required")

        # Fetch database auth from user credentials by connection_name
        internal_config = await ActionSqlRunner._build_internal_config(
            ctx=ctx,
            connection_name=self.action_config.connection_name,
            sql_query=sql_query,
            user=self.user,
            db=self.db,
        )
        if isinstance(internal_config, Exception):
            return internal_config

        res = await ActionSqlRunner.run_select_query(
            ctx=ctx, internal_config=internal_config, app_state=app_state
        )
        return res

    async def run_tool(
        self, action_config: ActionSqlRunnerConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        if not action_config.sql_query:
            return Exception("sql_query is required")

        # Fetch database auth from user credentials by connection_name
        internal_config = await ActionSqlRunner._build_internal_config(
            ctx=ctx,
            connection_name=action_config.connection_name,
            sql_query=action_config.sql_query,
            user=self.user,
            db=self.db,
        )
        if isinstance(internal_config, Exception):
            return internal_config

        res = await ActionSqlRunner.run_select_query(
            ctx=ctx, internal_config=internal_config, app_state=app_state
        )
        return res

    @staticmethod
    async def _build_internal_config(
        ctx: Context,
        connection_name: str,
        sql_query: str,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        statement_timeout_ms: int | None = None,
        max_response_bytes: int | None = None,
    ) -> _SqlRunnerInternalConfig | Exception:
        """
        Fetch database auth from user credentials by connection_name and build internal config.
        """
        try:
            db_auth_doc = await UserDbAuth.get_by_name(
                ctx=ctx, db=db, user=user, connection_name=connection_name, decrypt=True
            )
            if not db_auth_doc:
                return Exception(f"No database auth found for connection_name: {connection_name}")

            # Validate auth using DatabaseAuth model
            database_auth = DatabaseAuth.get_auth_model_type().model_validate(db_auth_doc.auth)

            # Get auth config dict
            auth_config = await DatabaseAuth.get_auth_config(ctx=ctx, app_auth=database_auth)

            # Ensure required fields are present
            if not auth_config.get("database_type"):
                return Exception("database_type is required but not found in auth")

            database_type = auth_config["database_type"]

            # BigQuery has different requirements - needs project_id and credentials_info
            if database_type == DatabaseEnum.bigquery or database_type == "bigquery":
                if not auth_config.get("project_id") and not auth_config.get("database"):
                    return Exception("project_id or database is required for BigQuery")
                if not auth_config.get("credentials_info"):
                    return Exception(
                        "credentials_info (service account JSON) is required for BigQuery"
                    )

                return _SqlRunnerInternalConfig(
                    database_type=DatabaseEnum.bigquery,
                    database=auth_config.get("database", auth_config.get("project_id", "")),
                    project_id=auth_config.get("project_id"),
                    dataset=auth_config.get("dataset"),
                    credentials_info=auth_config.get("credentials_info"),
                    sql_query=sql_query,
                    statement_timeout_ms=statement_timeout_ms,
                    max_response_bytes=max_response_bytes,
                )

            # Traditional databases require host and database. Cloud SQL Postgres
            # has no host (it connects via the Cloud SQL connector), so host is
            # only required when no Cloud SQL instance is configured.
            use_cloud_sql = bool(auth_config.get("cloud_sql_instance_connection_name"))
            if not use_cloud_sql and not auth_config.get("host"):
                return Exception(
                    "host is required but not found in auth (or use cloud_sql_instance_connection_name for Cloud SQL)"
                )
            if not auth_config.get("database"):
                return Exception("database is required but not found in auth")

            return _SqlRunnerInternalConfig(
                database_type=auth_config["database_type"],
                host=auth_config.get("host"),
                port=auth_config.get("port"),
                database=auth_config["database"],
                username=auth_config.get("username"),
                password=auth_config.get("password"),
                cloud_sql_instance_connection_name=auth_config.get(
                    "cloud_sql_instance_connection_name"
                ),
                cloud_sql_use_private_ip=auth_config.get("cloud_sql_use_private_ip", False),
                cloud_sql_enable_iam_auth=auth_config.get("cloud_sql_enable_iam_auth", False),
                credentials_info=auth_config.get("credentials_info"),
                sql_query=sql_query,
                statement_timeout_ms=statement_timeout_ms,
                max_response_bytes=max_response_bytes,
            )

        except Exception as e:
            logger.bind(
                ctx=ctx,
                code=AppCode.SQL_RUNNER,
                connection_name=connection_name,
            ).exception("Error fetching database auth from user credentials")
            return e

    @staticmethod
    async def run_select_query(
        ctx: Context,
        internal_config: _SqlRunnerInternalConfig,
        app_state: AppState | None = None,
    ) -> DataBlocks | Exception:
        logger.bind(ctx=ctx, code=AppCode.SQL_RUNNER, query=repr(internal_config.sql_query)[:20]).info("Starting run_select_query")
        if not internal_config.sql_query.strip().lower().startswith("select"):
            return Exception("Only SELECT queries are allowed")
        try:
            return await run_blocking(
                app_state,
                ActionSqlRunner._execute_select_query,
                ctx,
                internal_config,
                app_state.cancellation if app_state is not None else None,
            )
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.SQL_RUNNER, query=repr(internal_config.sql_query)[:100]).exception(
                "Error in run_select_query"
            )
            return e

    @staticmethod
    def _execute_select_query(
        ctx: Context,
        internal_config: _SqlRunnerInternalConfig,
        cancellation: RunCancellation | None,
    ) -> DataBlocks:
        engine = None
        try:
            # BigQuery requires credentials_info to be passed to create_engine
            if internal_config.database_type == DatabaseEnum.bigquery:
                engine = create_engine(
                    internal_config.build_connection_string(),
                    credentials_info=internal_config.credentials_info,
                )
            elif (
                internal_config.database_type == DatabaseEnum.postgres
                and internal_config.cloud_sql_instance_connection_name
            ):
                # Cloud SQL Postgres connects via the Cloud SQL Python Connector,
                # not a plain connection string. A bare create_engine() on the
                # pg8000 URL would try to TCP-connect to a host named "cloudsql".
                from src.autobots.core.database.cloud_sql import create_cloud_sql_engine

                engine = create_cloud_sql_engine(
                    instance_connection_name=internal_config.cloud_sql_instance_connection_name,
                    user=internal_config.username or "",
                    password=internal_config.password or "",
                    database=internal_config.database or "postgres",
                    use_private_ip=internal_config.cloud_sql_use_private_ip,
                    enable_iam_auth=internal_config.cloud_sql_enable_iam_auth,
                    credentials_info=internal_config.credentials_info,
                )
            else:
                # DB-side statement timeout is currently only enforced for plain
                # (non-Cloud-SQL) Postgres connections; other dialects/paths rely on the
                # caller's outer wall-clock timeout only.
                connect_args: dict = {}
                if (
                    internal_config.statement_timeout_ms is not None
                    and internal_config.database_type == DatabaseEnum.postgres
                ):
                    connect_args["options"] = (
                        f"-c statement_timeout={int(internal_config.statement_timeout_ms)}"
                    )
                if connect_args:
                    engine = create_engine(
                        internal_config.build_connection_string(),
                        connect_args=connect_args,
                    )
                else:
                    engine = create_engine(internal_config.build_connection_string())

            session_factory = sessionmaker(bind=engine)
            blocks = DataBlocks()
            with session_factory() as session:
                remove_driver_cancel = register_query_cancel(
                    cancellation, session, internal_config
                )
                try:
                    statement = sqlalchemy.text(text=internal_config.sql_query)
                    rs = session.execute(statement)
                    hard_limit = 1000
                    max_bytes = internal_config.max_response_bytes
                    curr = 0
                    cumulative_bytes = 0
                    for row in rs:
                        curr += 1
                        if curr > hard_limit:
                            break
                        row_dict = dict(row._mapping)
                        row_str = json.dumps(row_dict)
                        if max_bytes is not None:
                            # Same semantics as the row cap above: the row that crosses
                            # the limit is not appended, so the response stays within
                            # the cap.
                            cumulative_bytes += len(row_str.encode("utf-8"))
                            if cumulative_bytes > max_bytes:
                                break
                        block = DataBlock(text=row_str)
                        blocks.blocks.append(block)
                finally:
                    remove_driver_cancel()
            return blocks

        finally:
            # Always dispose the engine (and, for Cloud SQL, close the connector
            # via the engine_disposed hook) so connections aren't leaked per call.
            if engine is not None:
                try:
                    engine.dispose()
                except Exception:
                    logger.bind(ctx=ctx).exception("Error disposing engine in run_select_query")
