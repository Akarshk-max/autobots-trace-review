"""
Google Cloud SQL Python Connector integration.

Connects to Cloud SQL for PostgreSQL without IP whitelisting by using the
Cloud SQL Python Connector. Uses IAM/Application Default Credentials for
authentication - no need to configure authorized networks.

Requirements:
- Cloud SQL Admin API enabled
- Service account or user with Cloud SQL Client IAM role
- Application Default Credentials configured (gcloud auth application-default login)
"""

from sqlalchemy import create_engine, event


def create_cloud_sql_engine(
    instance_connection_name: str,
    user: str,
    password: str,
    database: str,
    use_private_ip: bool = False,
    enable_iam_auth: bool = False,
    credentials_info: dict | None = None,
):
    """
    Create a SQLAlchemy engine using the Cloud SQL Python Connector.

    No IP whitelisting required. The connector calls the Cloud SQL Admin API to
    fetch instance metadata, which requires Google credentials:
      - If `credentials_info` (a service-account JSON dict) is provided, those
        credentials are used. This is the multi-tenant path: each saved
        connection brings its own service account (same field BigQuery uses).
      - Otherwise the connector falls back to Application Default Credentials
        (ADC) from the runtime environment.

    When enable_iam_auth=True, password is ignored; user must be the IAM
    principal (e.g. service account email).
    """
    from google.cloud.sql.connector import Connector, IPTypes

    ip_type = IPTypes.PRIVATE if use_private_ip else IPTypes.PUBLIC

    credentials = None
    if credentials_info:
        from google.oauth2 import service_account

        # Scope required by the Cloud SQL Admin API the connector calls.
        credentials = service_account.Credentials.from_service_account_info(
            credentials_info,
            scopes=["https://www.googleapis.com/auth/sqlservice.admin"],
        )

    connector = Connector(refresh_strategy="LAZY", credentials=credentials)

    def getconn():
        kwargs: dict = {
            "user": user or "",
            "db": database or "postgres",
            "ip_type": ip_type,
            "enable_iam_auth": enable_iam_auth,
        }
        if not enable_iam_auth:
            kwargs["password"] = password or ""
        conn = connector.connect(
            instance_connection_name,
            "pg8000",
            **kwargs,
        )
        return conn

    engine = create_engine(
        "postgresql+pg8000://",
        creator=getconn,
        pool_pre_ping=True,
    )

    # The Connector runs background refresh tasks and holds resources that
    # disposing the SQLAlchemy engine does NOT release. Tie the connector's
    # cleanup to engine disposal so callers that already `engine.dispose()`
    # (the SQL runners and /db-auth/test-connection) don't leak connectors in a
    # long-running process.
    def _close_connector(_engine) -> None:
        try:
            connector.close()
        except Exception:
            pass

    event.listen(engine, "engine_disposed", _close_connector)

    return engine
