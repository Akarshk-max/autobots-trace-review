# src/autobots/action/action_type/action_text2text/daytona_conn_forward.py
"""
Materialization of a user's stored connections into a Daytona sandbox process
environment (AUT-1593).

Two buckets are materialized here:

- App connections (`resolve_forwardable_env`): an app connection resolves to static
  request headers (or, for AWS, to a key pair SigV4 needs in-process), so it can be
  handed to the sandbox directly. Connections are addressed by their saved *name*,
  not by app type: this codebase lets a user save several connections of the same
  app type (two Workday tenants, a prod and a sandbox Salesforce), so a type alone
  does not identify a credential. Env var names are therefore derived from the
  connection name, matching how `resolve_direct_database_env` below names its keys.
- Direct database connections (`resolve_direct_database_env`): the SQLAlchemy
  connection string is built server-side and handed to the sandbox so it can do real
  read/write and bulk work with its own DB client on its own compute.

Read-only database access (`forward_databases`) is NOT materialized; it goes through
the connections bridge (the `/v1/daytona/connections/...` endpoints in `api_daytona.py`)
instead. The sandbox-side half of that bridge lands with this change -- the job doc's
grant list, the connections token, and the AUTOBOTS_CONNECTIONS_* env vars -- while the
serving endpoints themselves are ported separately, so `forward_databases` is not usable
end to end until those land. `forward_connections` and `forward_databases_direct` are
self-contained and work now.

Callers must inject the returned mapping into the per-command environment
(`start_background(env=...)`), never into `create_sandbox(env_vars=...)`, which is
sandbox-wide and persistent.
"""

import json
import re
from typing import Dict, List

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.integrations.integration_type.aws_auth import AwsModel
from src.autobots.integrations.user_integration import UserIntegration
from src.autobots.integrations.app_creds.app_creds_model import AppCredsDoc
from src.autobots.integrations.app_creds.user_app_creds import UserAppCreds
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.db_auth.db_auth_model import DatabaseEnum
from src.autobots.integrations.db_auth.user_db_auth import UserDbAuth
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM

# Headers that carry no credential and are not useful inside the sandbox.
_SKIPPED_HEADERS = {"content-type"}


def _sanitize_env_fragment(value: str) -> str:
    """Uppercase and replace every non-alphanumeric character with '_' for env var names."""
    return re.sub(r"[^A-Z0-9]", "_", value.upper())


def forwarded_authorization_env_key(connection_name: str) -> str:
    """Env var name a sandbox reads to get a connection's Authorization header value."""
    return f"AUTOBOTS_CONN_{_sanitize_env_fragment(connection_name)}_AUTHORIZATION"


def forwarded_header_env_key(connection_name: str, header_name: str) -> str:
    """
    Env var name a sandbox reads to get one forwarded header value.

    Routed through `forwarded_authorization_env_key` for the Authorization header so
    that name stays byte-stable for anything already relying on it.
    """
    if header_name.lower() == "authorization":
        return forwarded_authorization_env_key(connection_name)
    return (
        f"AUTOBOTS_CONN_{_sanitize_env_fragment(connection_name)}"
        f"_{_sanitize_env_fragment(header_name)}"
    )


def _aws_env_from_creds(app_creds_doc: AppCredsDoc | None) -> Dict[str, str]:
    """
    AWS is special-cased: SigV4 signs each request, so there is no static header to
    forward. We forward the stored key pair under the standard boto3 env var names
    so `boto3.client("s3")` inside the sandbox works with no sandbox-side code.

    The doc must already have been fetched with decrypt=True; credentials are
    encrypted at rest and a ciphertext key pair would silently fail every AWS call
    made inside the sandbox.
    """
    if not app_creds_doc:
        return {}
    creds = AwsModel.model_validate(app_creds_doc.creds)
    env = {
        "AWS_ACCESS_KEY_ID": creds.aws_access_key_id,
        "AWS_SECRET_ACCESS_KEY": creds.aws_secret_access_key,
    }
    if creds.region_name:
        env["AWS_DEFAULT_REGION"] = creds.region_name
    return env


async def resolve_forwardable_env(
    ctx: Context,
    db: AsyncIOMotorDatabase,
    user: UserORM,
    connection_names: List[str],
) -> Dict[str, str]:
    """
    Resolve stored app credentials for `user` into environment variables.

    Connections are named, and a user may hold several of the same app type, so each
    entry of `connection_names` is a saved connection name (the `name` on AppCreds /
    AppAuth), not an app type. The app type is read back off the stored document and
    is used only to decide whether the AWS special case applies.

    Every header `UserIntegration.auth_header` returns is forwarded, not just
    Authorization: roughly a sixth of the app types authenticate with a differently
    named header (Plaid's PLAID-CLIENT-ID/PLAID-SECRET, Datadog's DD-API-KEY, Azure
    OpenAI's api-key, ...) and several more need a second header alongside
    Authorization (Google Ads' developer-token, Notion's Notion-Version, Superset's
    X-CSRFToken).

    Credentials are resolved fresh on every call (UserIntegration.auth_header refreshes
    expiring OAuth tokens internally), so nothing stale is ever forwarded. Every
    lookup passes decrypt=True: credentials are encrypted at rest here, and headers
    built from ciphertext would be accepted silently and then fail every call the
    sandbox makes.

    Best-effort by design: a connection that is missing or cannot be resolved is
    skipped with a warning instead of failing the whole sandbox run. Some apps
    legitimately resolve to no headers at all (exa, gemini, vertex_ai authenticate
    via query params or SDK-side config), and those are skipped the same way.
    """
    env: Dict[str, str] = {}
    # Two saved connection names can sanitize to the same env var prefix (e.g.
    # "prod-workday" and "prod_workday"). Keep the first and warn, rather than
    # silently overwriting one credential with another.
    used_key_fragments: Dict[str, str] = {}
    for connection_name in connection_names or []:
        try:
            key_fragment = _sanitize_env_fragment(connection_name)
            kept_connection_name = used_key_fragments.get(key_fragment)
            if kept_connection_name is not None:
                logger.bind(ctx=ctx, connection_name=connection_name).warning(
                    f"Daytona connection forwarding: connection '{connection_name}' maps to the "
                    f"same env var prefix AUTOBOTS_CONN_{key_fragment}_ as connection "
                    f"'{kept_connection_name}', which was already forwarded. Keeping "
                    f"'{kept_connection_name}' and skipping '{connection_name}'; rename one of "
                    "the connections to forward both"
                )
                continue

            app_creds_doc = await UserAppCreds.get_app_creds_by_name(
                ctx=ctx, db=db, user=user, name=connection_name, decrypt=True
            )
            app_name = app_creds_doc.app_name if app_creds_doc else None
            if app_name is None:
                # OAuth-only connections have no creds document of their own.
                integration_doc = await UserIntegration.get_by_name(
                    ctx=ctx, db=db, user=user, name=connection_name, decrypt=True
                )
                app_name = integration_doc.app_name if integration_doc else None
            if app_name is None:
                logger.bind(ctx=ctx, connection_name=connection_name).warning(
                    "Daytona connection forwarding: connection not found, skipping"
                )
                continue
            app_name = AppTypes(app_name)

            if app_name == AppTypes.aws:
                resolved = _aws_env_from_creds(app_creds_doc)
            else:
                headers = await UserIntegration.auth_header(
                    ctx=ctx,
                    db=db,
                    user=user,
                    name=connection_name,
                    app_name=app_name,
                    decrypt=True,
                )
                resolved = {
                    forwarded_header_env_key(connection_name, header_name): header_value
                    for header_name, header_value in (headers or {}).items()
                    if header_name
                    and header_value
                    and header_name.lower() not in _SKIPPED_HEADERS
                }
            if not resolved:
                logger.bind(
                    ctx=ctx, connection_name=connection_name, app_name=app_name.value
                ).warning(
                    "Daytona connection forwarding: no credentials resolved, skipping connection"
                )
                continue
            used_key_fragments[key_fragment] = connection_name
            env.update(resolved)
        except Exception as e:
            logger.bind(ctx=ctx, connection_name=connection_name).warning(
                f"Daytona connection forwarding: failed to resolve app credentials: {e}"
            )
    return env


async def resolve_direct_database_env(
    ctx: Context,
    db: AsyncIOMotorDatabase,
    user: UserORM,
    connection_names: List[str],
) -> Dict[str, str]:
    """
    Materialize saved database connections into environment variables so sandbox code
    can connect directly with its own DB client (sqlalchemy, psycopg2, ...).

    This is the write/bulk path. Unlike the read-only bridge behind
    `forward_databases`, it hands the sandbox a real, usable credential carrying
    whatever permissions the saved connection has, and the work runs on the sandbox's
    own compute rather than being proxied through the API server.

    Per connection:
      AUTOBOTS_DB_<NAME>_URL              SQLAlchemy connection string
      AUTOBOTS_DB_<NAME>_CREDENTIALS_JSON BigQuery service-account JSON (BigQuery only,
                                          since its credentials cannot be embedded in a
                                          connection string)

    Only plain, network-reachable database URLs can be forwarded. Cloud SQL connections
    (which need the server-side Cloud SQL Python Connector) and SQLite connections
    (which name a path on the API server's filesystem) are skipped: their connection
    strings are meaningless inside the sandbox.

    Best-effort by design: one connection that is missing, unsupported, or cannot be
    built is skipped with a warning instead of failing the whole sandbox run.
    """
    env: Dict[str, str] = {}
    # Two saved connection names can sanitize to the same env var name (e.g. "prod-db"
    # and "prod_db" both -> PROD_DB). Keep the first and warn, rather than silently
    # overwriting one credential with another.
    used_key_fragments: Dict[str, str] = {}
    for connection_name in connection_names or []:
        try:
            auth_model = await UserDbAuth.get_database_auth_model_by_name(
                ctx=ctx, db=db, user=user, connection_name=connection_name
            )
            if not auth_model:
                logger.bind(ctx=ctx, connection_name=connection_name).warning(
                    "Daytona direct database forwarding: connection not found, skipping"
                )
                continue
            if auth_model.cloud_sql_instance_connection_name:
                logger.bind(ctx=ctx, connection_name=connection_name).warning(
                    f"Daytona direct database forwarding: connection '{connection_name}' uses the "
                    "Cloud SQL Python Connector, which does not run inside the Daytona sandbox. "
                    "forward_databases_direct only supports plain network-reachable database URLs; "
                    "skipping"
                )
                continue
            if auth_model.database_type == DatabaseEnum.sqlite:
                logger.bind(ctx=ctx, connection_name=connection_name).warning(
                    f"Daytona direct database forwarding: connection '{connection_name}' is a local "
                    "SQLite file on the API server, not reachable from the Daytona sandbox. "
                    "forward_databases_direct only supports plain network-reachable database URLs; "
                    "skipping"
                )
                continue
            connection_string = auth_model.build_connection_string()
            if not connection_string:
                logger.bind(ctx=ctx, connection_name=connection_name).warning(
                    "Daytona direct database forwarding: empty connection string, skipping"
                )
                continue
            key_fragment = _sanitize_env_fragment(connection_name)
            kept_connection_name = used_key_fragments.get(key_fragment)
            if kept_connection_name is not None:
                logger.bind(ctx=ctx, connection_name=connection_name).warning(
                    f"Daytona direct database forwarding: connection '{connection_name}' maps to the "
                    f"same env var name AUTOBOTS_DB_{key_fragment}_URL as connection "
                    f"'{kept_connection_name}', which was already forwarded. Keeping "
                    f"'{kept_connection_name}' and skipping '{connection_name}'; rename one of the "
                    "connections to forward both"
                )
                continue
            used_key_fragments[key_fragment] = connection_name
            env[f"AUTOBOTS_DB_{key_fragment}_URL"] = connection_string
            # BigQuery's service-account credentials cannot be embedded in the
            # connection string, so they ride alongside it as JSON.
            if auth_model.credentials_info:
                env[f"AUTOBOTS_DB_{key_fragment}_CREDENTIALS_JSON"] = json.dumps(
                    auth_model.credentials_info
                )
        except Exception as e:
            logger.bind(ctx=ctx, connection_name=connection_name).warning(
                f"Daytona direct database forwarding: failed to resolve connection: {e}"
            )
    return env
