from typing import Any, Dict, Type

from src.autobots.data_model.context import Context
from src.autobots.integrations.db_auth.db_auth_model import DatabaseAuthModel, DatabaseCredsModel


class DatabaseAuth:
    """
    Database authentication class for SQL runner tool.

    Unlike API-based auth classes (like AirtableAuth), this doesn't inherit from IntegrationTypeABC
    since it's not API-based and doesn't need auth headers. Instead, it provides:
    - Connection string building for SQLAlchemy
    - Configuration dict compatible with ActionSqlRunnerConfig
    - Database credential management

    This class is designed to work with the SQL runner tool (ActionSqlRunner) to provide
    authenticated database connections.
    """

    @staticmethod
    def get_creds_model_type() -> Type[DatabaseCredsModel]:
        return DatabaseCredsModel

    @staticmethod
    def get_auth_model_type() -> Type[DatabaseAuthModel]:
        return DatabaseAuthModel

    @staticmethod
    async def create_auth(
        ctx: Context, app_creds: DatabaseCredsModel
    ) -> DatabaseAuthModel | Exception:
        """Create database auth from credentials"""
        try:
            auth_model = DatabaseAuthModel(
                database_type=app_creds.database_type,
                host=app_creds.host,
                port=app_creds.port,
                database=app_creds.database,
                username=app_creds.username,
                password=app_creds.password,
                name=app_creds.name,
                # BigQuery specific fields
                project_id=app_creds.project_id,
                dataset=app_creds.dataset,
                credentials_info=app_creds.credentials_info,
                # Cloud SQL (Postgres)
                cloud_sql_instance_connection_name=app_creds.cloud_sql_instance_connection_name,
                cloud_sql_use_private_ip=app_creds.cloud_sql_use_private_ip,
                cloud_sql_enable_iam_auth=app_creds.cloud_sql_enable_iam_auth,
                # Snowflake specific fields
                account=app_creds.account,
                warehouse=app_creds.warehouse,
                schema=app_creds.schema,
                role=app_creds.role,
                # Databricks specific fields
                http_path=app_creds.http_path,
                access_token=app_creds.access_token,
                catalog=app_creds.catalog,
            )
            # Pre-build connection string for convenience
            auth_model.connection_string = auth_model.build_connection_string()
            return auth_model
        except Exception as e:
            return e

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: DatabaseCredsModel | None, app_auth: DatabaseAuthModel | None
    ) -> DatabaseAuthModel | Exception:
        """Refresh database auth - for databases, this is typically a no-op"""
        if app_auth:
            return app_auth
        if app_creds:
            return await DatabaseAuth.create_auth(ctx, app_creds)
        return Exception("No credentials or auth provided")

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: DatabaseAuthModel) -> bool:
        """Check if database auth needs refresh - typically False for databases"""
        return False

    @staticmethod
    async def get_connection_string(ctx: Context, app_auth: DatabaseAuthModel) -> str:
        """Get the connection string for the database"""
        return app_auth.build_connection_string()

    @staticmethod
    async def get_auth_config(ctx: Context, app_auth: DatabaseAuthModel) -> Dict[str, Any]:
        """Get database configuration dict compatible with ActionSqlRunnerConfig"""
        return {
            "database_type": app_auth.database_type,
            "host": app_auth.host,
            "port": app_auth.port,
            "database": app_auth.database,
            "username": app_auth.username,
            "password": app_auth.password,
            # BigQuery specific fields
            "project_id": app_auth.project_id,
            "dataset": app_auth.dataset,
            "credentials_info": app_auth.credentials_info,
            # Cloud SQL (Postgres)
            "cloud_sql_instance_connection_name": app_auth.cloud_sql_instance_connection_name,
            "cloud_sql_use_private_ip": app_auth.cloud_sql_use_private_ip,
            "cloud_sql_enable_iam_auth": app_auth.cloud_sql_enable_iam_auth,
            # Snowflake specific fields
            "account": app_auth.account,
            "warehouse": app_auth.warehouse,
            "schema": app_auth.schema,
            "role": app_auth.role,
            # Databricks specific fields
            "http_path": app_auth.http_path,
            "access_token": app_auth.access_token,
            "catalog": app_auth.catalog,
        }
