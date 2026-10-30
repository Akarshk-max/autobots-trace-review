from enum import Enum
from typing import Any, Dict, List, Literal
from urllib.parse import quote_plus

from pydantic import BaseModel, ConfigDict, model_validator

from src.autobots.core.database.mongo_base_crud import DocFindPage
from src.autobots.data_model.created_at import CreatedAt
from src.autobots.data_model.id import ID
from src.autobots.data_model.MongoId import MongoID
from src.autobots.data_model.space_id import OptionalSpaceId
from src.autobots.data_model.updated_at import UpdatedAt
from src.autobots.data_model.user_id import UserId

DB_AUTH_APP_NAME = "database"


class DatabaseEnum(str, Enum):
    sqlite = "sqlite"
    mysql = "mysql"
    postgres = "postgres"
    bigquery = "bigquery"
    oracle = "oracle"
    snowflake = "snowflake"
    databricks = "databricks"


SqlDialectPlusDriver = {
    DatabaseEnum.sqlite: "sqlite",
    DatabaseEnum.mysql: "mysql+pymysql",
    DatabaseEnum.postgres: "postgresql+psycopg2",
    DatabaseEnum.bigquery: "bigquery",
    DatabaseEnum.oracle: "oracle+oracledb",
    DatabaseEnum.snowflake: "snowflake",
    DatabaseEnum.databricks: "databricks",
}


class DatabaseCredsModel(BaseModel):
    """Credentials model for database connections"""

    database_type: DatabaseEnum
    host: str | None = None  # Not required for BigQuery or Cloud SQL Postgres
    port: int | None = None  # Not required for BigQuery or Cloud SQL Postgres
    database: str | None = None  # Database name or Oracle SID
    username: str | None = None
    password: str | None = None
    name: str | None = None  # Required for Snowflake/Databricks (LLM connection identifier)
    # BigQuery specific fields
    project_id: str | None = None  # GCP project ID for BigQuery
    dataset: str | None = None  # BigQuery dataset name
    credentials_info: dict | None = None  # Service account JSON for BigQuery
    # Cloud SQL (Postgres) - alternative to IP whitelisting
    cloud_sql_instance_connection_name: str | None = None  # project:region:instance
    cloud_sql_use_private_ip: bool = False
    cloud_sql_enable_iam_auth: bool = False  # IAM DB auth (no password; user = IAM principal)
    # Snowflake specific fields
    account: str | None = None  # Snowflake account identifier (e.g. xy12345.us-east-1)
    warehouse: str | None = None  # Snowflake virtual warehouse
    schema: str | None = None  # Snowflake schema (also used for Databricks schema)
    role: str | None = None  # Snowflake role
    # Databricks specific fields
    http_path: str | None = None  # Databricks SQL warehouse or cluster HTTP path
    access_token: str | None = None  # Databricks personal access token
    catalog: str | None = None  # Databricks Unity Catalog name

    @model_validator(mode="after")
    def _validate_required_fields(self) -> "DatabaseCredsModel":
        if self.database_type == DatabaseEnum.snowflake:
            missing = [f for f, v in [("name", self.name), ("account", self.account), ("username", self.username)] if not v]
            if missing:
                raise ValueError(f"Snowflake connection missing required fields: {', '.join(missing)}")
        if self.database_type == DatabaseEnum.databricks:
            missing = [f for f, v in [("name", self.name), ("host", self.host), ("access_token", self.access_token), ("http_path", self.http_path)] if not v]
            if missing:
                raise ValueError(f"Databricks connection missing required fields: {', '.join(missing)}")
        return self


class DatabaseAuthModel(BaseModel):
    """Auth model for database connections - stores connection string"""

    database_type: DatabaseEnum
    host: str | None = None  # Not required for BigQuery or Cloud SQL Postgres
    port: int | None = None
    database: str | None = None
    username: str | None = None
    password: str | None = None
    connection_string: str | None = None
    name: str | None = None  # Optional memorable name for the connection
    # BigQuery specific fields
    project_id: str | None = None  # GCP project ID for BigQuery
    dataset: str | None = None  # BigQuery dataset name
    credentials_info: dict | None = None  # Service account JSON for BigQuery
    # Cloud SQL (Postgres) - alternative to IP whitelisting
    cloud_sql_instance_connection_name: str | None = None  # project:region:instance
    cloud_sql_use_private_ip: bool = False
    cloud_sql_enable_iam_auth: bool = False  # IAM DB auth (no password; user = IAM principal)
    # Snowflake specific fields
    account: str | None = None  # Snowflake account identifier (e.g. xy12345.us-east-1)
    warehouse: str | None = None  # Snowflake virtual warehouse
    schema: str | None = None  # Snowflake schema (also used for Databricks schema)
    role: str | None = None  # Snowflake role
    # Databricks specific fields
    http_path: str | None = None  # Databricks SQL warehouse or cluster HTTP path
    access_token: str | None = None  # Databricks personal access token
    catalog: str | None = None  # Databricks Unity Catalog name

    def build_connection_string(self) -> str:
        """Build SQLAlchemy connection string"""
        if self.connection_string:
            return self.connection_string

        dialect_driver = SqlDialectPlusDriver.get(self.database_type)

        # BigQuery uses a different connection format: bigquery://project-id/dataset
        # Credentials are passed separately via create_engine() credentials_info parameter
        if self.database_type == DatabaseEnum.bigquery:
            project = self.project_id or self.database
            if self.dataset:
                return f"{dialect_driver}://{project}/{self.dataset}"
            return f"{dialect_driver}://{project}"

        # Cloud SQL Postgres uses connector, not direct connection string
        if (
            self.database_type == DatabaseEnum.postgres
            and self.cloud_sql_instance_connection_name
        ):
            return f"postgresql+pg8000://cloudsql/{self.database}"

        # Snowflake: snowflake://user:password@account/database/schema?warehouse=X&role=Y
        if self.database_type == DatabaseEnum.snowflake:
            if not self.account:
                raise ValueError("account is required for Snowflake connection string")
            if not self.username:
                raise ValueError("username is required for Snowflake connection string")
            user_q = quote_plus(self.username)
            pwd_q = quote_plus((self.password or "").strip())
            db = self.database or ""
            schema = self.schema or ""
            path = f"/{db}/{schema}" if (db and schema) else f"/{db}" if db else ""
            query_parts = []
            if self.warehouse:
                query_parts.append(f"warehouse={quote_plus(self.warehouse)}")
            if self.role:
                query_parts.append(f"role={quote_plus(self.role)}")
            query = f"?{'&'.join(query_parts)}" if query_parts else ""
            if pwd_q:
                return f"{dialect_driver}://{user_q}:{pwd_q}@{self.account}{path}{query}"
            return f"{dialect_driver}://{user_q}@{self.account}{path}{query}"

        # Databricks: databricks://token:<PAT>@<host>/<catalog>?http_path=X&schema=Y
        # Catalog is placed as the URL path segment (parsed as `database` by the dialect).
        # http_path and schema are query params. Verified against databricks-sqlalchemy==2.0.9.
        if self.database_type == DatabaseEnum.databricks:
            if not self.host:
                raise ValueError("host is required for Databricks connection string")
            if not self.access_token:
                raise ValueError("access_token is required for Databricks connection string")
            if not self.http_path:
                raise ValueError("http_path is required for Databricks connection string")
            token_q = quote_plus(self.access_token)
            catalog = self.catalog or self.database or ""
            path = f"/{catalog}" if catalog else ""
            query_parts = [f"http_path={quote_plus(self.http_path)}"]
            if self.schema:
                query_parts.append(f"schema={quote_plus(self.schema)}")
            query = f"?{'&'.join(query_parts)}"
            return f"{dialect_driver}://token:{token_q}@{self.host}{path}{query}"

        # SQLite has no host/port/credentials
        if self.database_type == DatabaseEnum.sqlite:
            return f"{dialect_driver}:///{self.database}"

        # Traditional databases (MySQL, PostgreSQL, Oracle).
        # Quote credentials so special chars (@ : / #) don't corrupt the URL, and
        # only append ":port" when a port is set (otherwise SQLAlchemy parses the
        # literal "None" as the port and raises invalid literal for int()).
        user_q = quote_plus(self.username) if self.username else ""
        pwd_q = quote_plus(self.password.strip()) if (self.password and self.password.strip()) else ""
        host_port = f"{self.host}:{self.port}" if self.port is not None else (self.host or "")

        if user_q and pwd_q:
            return f"{dialect_driver}://{user_q}:{pwd_q}@{host_port}/{self.database}"
        if user_q:
            return f"{dialect_driver}://{user_q}@{host_port}/{self.database}"
        return f"{dialect_driver}://{host_port}/{self.database}"


# CRUD Models
class DbAuthCreate(BaseModel):
    app_name: Literal["database"] = DB_AUTH_APP_NAME
    name: str
    default: bool = True
    auth: Dict[str, Any]


class DbAuthDocCreate(UserId, OptionalSpaceId, DbAuthCreate, CreatedAt, UpdatedAt):
    pass


class DbAuthRead(ID):
    id: str | None = None
    app_name: Literal["database"] | None = DB_AUTH_APP_NAME
    name: str | None = None
    default: bool | None = None


class DbAuthDocRead(UserId, OptionalSpaceId, DbAuthRead):
    pass


class DbAuthUpdate(DbAuthCreate):
    default: bool | None = None


class DbAuthDocUpdate(ID, UserId, DbAuthUpdate):
    pass


class DbAuthDeleteDoc(DbAuthDocRead):
    pass


class DbAuthLiteDoc(MongoID, UserId, CreatedAt, UpdatedAt):
    app_name: Literal["database"] = DB_AUTH_APP_NAME
    name: str | None = None
    default: bool | None = None
    model_config = ConfigDict(populate_by_name=True)


class DbAuthLiteDocPage(DocFindPage):
    docs: List[DbAuthLiteDoc]


class DbAuthDoc(MongoID, DbAuthDocCreate):
    __collection__ = "AppAuth"  # Share the same collection as AppAuth

    model_config = ConfigDict(populate_by_name=True)
