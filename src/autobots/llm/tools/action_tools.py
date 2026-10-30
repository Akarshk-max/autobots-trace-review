from datetime import datetime
from typing import Any, Dict, List

from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from src.autobots.action.action.action_doc_model import (
    ActionCreate,
    ActionDoc,
    ActionDocsFound,
    ActionFind,
    ActionUpdate,
)
from src.autobots.action.action.common_action_models import DataBlock, TextObj
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action.action_result.action_result_doc_model import (
    ActionResultDoc,
    ActionResultLiteDoc,
)
from src.autobots.action.action_result.user_action_result import UserActionResult
from src.autobots.action.action_type.action_factory import ActionDataTypes, ActionFactory
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.integrations.integration_model import IntegrationRead
from src.autobots.integrations.user_integration import UserIntegration
from src.autobots.integrations.db_auth.user_db_auth import UserDbAuth
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.llm.tools.tool_factory import ToolFactory
from src.autobots.mcp.mcp_doc_model import MCPAddInput, MCPCreate, MCPFind
from src.autobots.mcp.user_mcp import UserMCP
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM


class ActionTools:
    # Guidance for LLM about Google family apps and parent/child app relationships
    GOOGLE_FAMILY_INFO = (
        "IMPORTANT: If 'google' or 'google_read_only' is shown as connected but a subsidiary "
        "Google app (like google_gmail, google_drive, google_analytics, youtube, "
        "google_cloud) is NOT connected, you can still reach the subsidiary service "
        "through the parent connection, which often holds the necessary scopes. Do it by "
        "passing that connection's own name AND app_name — both from the row you saw — "
        "not the subsidiary's app_name. For example:\n"
        "- Gmail through a google connection named 'work': name='work', app_name='google'.\n"
        "- BigQuery/GCS through the same: name='work', app_name='google'.\n"
        "Passing app_name='google_gmail' when the connection is stored under 'google' "
        "matches nothing, and the request is sent unauthenticated."
    )

    @staticmethod
    async def get_action_types(
        config: TextObj, ctx: Context, app_state: AppState, user: UserORM, notify: Notify | None = None
    ) -> List[str]:
        """
        Description
        -----------
        Returns the list of available action type identifiers supported by the
        system. This is a simple helper that exposes the registry of action
        types so callers can discover what kinds of actions can be created or
        executed.

        Input
        -----
        config : dict
            Expected structure:
                { "text": "<optional freeform query or context (ignored)>" }

            text : str
                Optional freeform text; not used to determine available action
                types but accepted for interface symmetry.

        Output
        ------
        list[str]
            A list of action type strings, for example:
                ["text2img_flux", "audio2text_translation_openai", ...]
            Always returns the full set of registered action type identifiers.
        """
        return ActionFactory.get_action_types()

    class GetActionDataTypesConfig(BaseModel):
        action_type: ActionType
        is_get_config_create: bool = True
        is_get_config_update: bool = True
        is_get_config: bool = True
        is_get_input: bool = True
        is_get_output: bool = True

    @staticmethod
    async def get_action_data_types(
        config: GetActionDataTypesConfig, ctx: Context, app_state: AppState, user: UserORM, notify: Notify | None = None
    ) -> ActionDataTypes:
        """
        Description
        -----------
        Retrieves the input/output and config type metadata for a specific action
        type. This enables clients to programmatically discover the expected
        configuration, create/update payload shapes, and input/output types for
        a given action.

        Input
        -----
        config : dict
            Expected structure:
                {
                    "action_type": "<action type identifier>",
                    "is_get_config_create": true|false,
                    "is_get_config_update": true|false,
                    "is_get_config": true|false,
                    "is_get_input": true|false,
                    "is_get_output": true|false
                }

            action_type : str
                The action type identifier to inspect.

            is_get_* : bool
                Flags controlling which pieces of metadata to return. Set the
                relevant flags to false to omit those parts from the result.

        Output
        ------
        ActionDataTypes
            A structured object describing the requested type metadata (config
            create/update/current, input type, and output type). The exact
            shape is the project's ActionDataTypes model; clients can use this
            to validate or build payloads for the chosen action type.
        """
        action_data_types = await ActionFactory.get_data_types(**config.model_dump())
        return action_data_types

    @staticmethod
    async def get_tools(
        config: TextObj, ctx: Context, app_state: AppState, user: UserORM, notify: Notify | None = None
    ) -> List[str]:
        """
        Description
        -----------
        Returns the list of available tooling integrations that the system can
        surface to LLMs or use within actions (for example: "code_interpreter",
        "file_search", etc.). Useful for UIs that present tool selections or
        for validation before invoking an action that requires specific tools.

        Input
        -----
        config : dict
            Expected structure:
                { "text": "<optional freeform query or context>" }

            text : str
                Optional freeform text; accepted for interface symmetry but not
                required for retrieving the tool list.

        Output
        ------
        list[str]
            A list of tool identifier strings available in the runtime, for
            example: ["coding_assistant", "read_url", "web_search"].
        """
        tools = await ToolFactory.get_tools()
        return tools

    @staticmethod
    async def get_database_connection_names(
        config: TextObj,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
    ) -> List[str]:
        """
        Description
        -----------
        Returns the names of database connections configured by the current user.
        This is useful before running SQL/database tools so callers can select a
        valid `connection_name`.

        Input
        -----
        config : dict
            Expected structure:
                { "text": "<optional freeform query or context>" }

            text : str
                Optional freeform text; not used to compute the output.

        Output
        ------
        list[str]
            A de-duplicated list of connection names. If a connection has no
            explicit name, its database name is returned as a fallback when
            available.
        """
        db_auth_models = await UserDbAuth.list_database_auth_models(
            ctx=ctx, db=db, user=user, limit=200, offset=0
        )
        connection_names: List[str] = []
        for auth_model in db_auth_models:
            raw_name = (auth_model.name or "").strip()
            fallback_database_name = (auth_model.database or "").strip()
            candidate_name = raw_name or fallback_database_name
            if candidate_name and candidate_name not in connection_names:
                connection_names.append(candidate_name)
        return connection_names

    @staticmethod
    async def get_mcp_server_names(
        config: TextObj,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
    ) -> List[str]:
        """
        Description
        -----------
        Returns MCP server names configured by the current user. Useful for
        selecting a valid MCP target before calling MCP-related tools.

        Input
        -----
        config : dict
            Expected structure:
                { "text": "<optional freeform query or context>" }

            text : str
                Optional freeform text; not used to compute the output.

        Output
        ------
        list[str]
            A de-duplicated list of MCP server names.
        """
        mcp_docs = await UserMCP(user=user, db=db).list_mcps(
            ctx=ctx, mcp_find=MCPFind(), limit=200, offset=0
        )
        server_names: List[str] = []
        for mcp_doc in mcp_docs.docs:
            server_name = (mcp_doc.server_name or "").strip()
            if server_name and server_name not in server_names:
                server_names.append(server_name)
        return server_names

    @staticmethod
    async def enable_mcp(
        config: MCPAddInput,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
    ) -> Dict[str, Any]:
        """
        Description
        -----------
        Creates or reuses an MCP (Model Context Protocol) server connection that
        will be dynamically added to the current run's tool configuration. After
        successful execution, the MCP tools become immediately available for use
        by the LLM in the current conversation.

        If an MCP with the same server_name already exists for this user, it will
        be reused (idempotent behavior) rather than failing with a duplicate error.

        For security, users should pre-register MCP servers with authentication
        tokens via the UI, then reference them here by server_name only. This
        avoids exposing sensitive tokens in the chat conversation.

        Input
        -----
        config : MCPAddInput
            For referencing an existing MCP (recommended for authenticated servers):
                {
                    "server_name": "<name of pre-registered MCP server>"
                }

            For creating a new MCP:
                {
                    "server_name": "<unique name for the MCP server>",
                    "server_type": "SSE" | "StreamableHttp",
                    "server_url": "<URL of the MCP server>",
                    "description": "<optional description>",
                    "allowed_tools": ["<optional list of allowed tool names>"],
                    "request_headers": {"<optional headers dict>"},
                    "oauth_token": "<optional OAuth token>"
                }

            server_name : str (required)
                A unique identifier for this MCP server. Must start with a
                letter and contain only letters, digits, underscores, or
                hyphens. If the user provides an invalid name, ask them for a
                valid one before calling this tool. If the MCP was pre-registered
                via UI, only server_name is needed.

            server_type : str (required for new MCPs)
                Transport protocol: "SSE" or "StreamableHttp".

            server_url : str (required for new MCPs)
                The URL of the MCP server endpoint.

            description : str (optional)
                Human-readable description of what this MCP server provides.

            allowed_tools : list[str] (optional)
                List of specific tool names to allow from this MCP server.

            request_headers : dict (optional)
                HTTP headers to include in requests to the MCP server.

            oauth_token : str (optional)
                OAuth access token for authenticating with the MCP server.
                WARNING: If provided via chat, this token will be visible in
                the conversation history. For sensitive tokens, pre-register
                the MCP via the UI instead.

        Output
        ------
        dict
            {
                "success": true|false,
                "server_name": "<server name>",
                "message": "<status message>",
                "created": true|false  # true if newly created, false if reused
            }
        """
        if isinstance(config, dict):
            try:
                config = MCPAddInput(**config)
            except Exception as e:
                detail = getattr(e, "detail", str(e))
                return {
                    "success": False,
                    "server_name": config.get("server_name") if isinstance(config, dict) else None,
                    "message": f"Invalid MCP config: {detail}",
                    "created": False,
                }

        server_name = config.server_name
        user_mcp = UserMCP(user=user, db=db)

        # Check if MCP already exists (allows server_name-only lookups)
        existing_mcp = await user_mcp.get_mcp_by_server_name(
            ctx=ctx, mcp_server_name=server_name, decrypt=True
        )

        if existing_mcp:
            logger.bind(ctx=ctx, server_name=server_name).info(
                "MCP already exists, reusing for current run"
            )
            return {
                "success": True,
                "server_name": existing_mcp.server_name,
                "message": f"MCP '{existing_mcp.server_name}' already exists. It has been added to the current run's config and you can now call its tools.",
                "created": False,
            }

        # MCP doesn't exist - validate required fields for creation
        if not config.server_type or not config.server_url:
            return {
                "success": False,
                "server_name": server_name,
                "message": f"MCP '{server_name}' not found. To create a new MCP, provide server_type and server_url. Or pre-register via the UI.",
                "created": False,
            }

        # Build MCPCreate from validated input
        mcp_create = MCPCreate(
            server_name=config.server_name,
            server_type=config.server_type,
            server_url=config.server_url,
            description=config.description,
            allowed_tools=config.allowed_tools,
            request_headers=config.request_headers,
            oauth_token=config.oauth_token,
        )

        # Create new MCP
        try:
            mcp_doc = await user_mcp.create_mcp(ctx=ctx, mcp_create=mcp_create, decrypt=True)
            if mcp_doc:
                logger.bind(ctx=ctx, server_name=mcp_doc.server_name).info(
                    "MCP created successfully"
                )
                return {
                    "success": True,
                    "server_name": mcp_doc.server_name,
                    "message": f"MCP '{mcp_doc.server_name}' created and added to the current run's config. You can now call its tools.",
                    "created": True,
                }
            else:
                return {
                    "success": False,
                    "server_name": server_name,
                    "message": "Failed to create MCP: unknown error",
                    "created": False,
                }
        except Exception as e:
            detail = getattr(e, "detail", str(e))
            logger.bind(ctx=ctx, server_name=server_name, error=str(e)).exception(
                "Error creating MCP"
            )
            return {
                "success": False,
                "server_name": server_name,
                "message": f"Failed to create MCP: {detail}",
                "created": False,
            }

    @staticmethod
    async def get_connected_apps(
        config: TextObj,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
    ) -> Dict[str, Any]:
        """
        Description
        -----------
        Returns the set of supported apps split into those the user has
        connected via App Auth and those that are still available to connect.

        Input
        -----
        config : dict
            Expected structure:
                { "text": "<optional freeform query or context>" }

            text : str
                Optional freeform text; not used to compute the output.

        Output
        ------
        dict[str, Any]
            {
                "connected": [{"app_name": "...", "name": "..."}, ...],
                "important_info": {"google_family": "<guidance about Google parent/child apps>"},
            }
        """
        app_auth_page = await UserIntegration.list(
            ctx=ctx, db=db, user=user, integration_list=IntegrationRead(), limit=200, offset=0
        )
        connected_apps: List[dict] = []
        for integration_doc in app_auth_page.docs:
            app_name = str(getattr(integration_doc.app_name, "value", integration_doc.app_name)).strip()
            name = integration_doc.name.strip() if integration_doc.name else ""
            if app_name or name:
                connected_apps.append({"app_name": app_name, "name": name})
        return {
            "connected": connected_apps,
            "important_info": {"google_family": ActionTools.GOOGLE_FAMILY_INFO},
        }

    # Keys that must never appear in tool output regardless of app or nesting depth.
    # Allowlisted non-sensitive examples that survive: api_domain, login_customer_id,
    # account_id, realm_id, org_id, scope, token_type, expires_in, team, app_id, etc.
    _SENSITIVE_AUTH_KEYS: frozenset = frozenset(
        {
            "access_token",
            "refresh_token",
            "code",
            "id_token",
            "basic_token",
            "api_key",
            "management_api_key",
            "consumer_key",
            "consumer_secret",
            "token_secret",
            "client_secret",
            "password",
            "private_key",
            "ssl_cert_path",
            "ssl_key_path",
            "fb_exchange_token",
        }
    )

    @classmethod
    def _filter_auth(cls, obj: Any) -> Any:
        """Recursively strip sensitive keys from an auth dict."""
        if isinstance(obj, dict):
            return {
                k: cls._filter_auth(v) for k, v in obj.items() if k not in cls._SENSITIVE_AUTH_KEYS
            }
        if isinstance(obj, list):
            return [cls._filter_auth(v) for v in obj]
        if isinstance(obj, datetime):
            return obj.isoformat()
        return obj

    @staticmethod
    async def get_connected_app_details(
        config: TextObj,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
    ) -> List[Dict[str, Any]]:
        """
        Description
        -----------
        Returns non-sensitive metadata for each connected app integration,
        including safe fields stored inside the auth record (e.g. api_domain,
        login_customer_id, account_id, realm_id, org_id, team workspace info,
        scope, token_type, expires_in).  All known-sensitive keys (tokens,
        secrets, keys, passwords) are stripped before returning.

        Input
        -----
        config : dict
            Expected structure:
                { "text": "<optional freeform query or context>" }

            text : str
                Optional freeform text; not used to compute the output.

        Output
        ------
        list[dict]
            Each item contains:
              - id            : str   — unique connection identifier
              - name          : str   — the connection's name, e.g. "googleapitest"
              - app_name      : str   — e.g. "slack", "openai", "netsuite"

            To make an authenticated call, pass BOTH of a row's `name` and `app_name`
            to api_call, exactly as they appear here. They are one identity: the same
            name can exist under several apps, so neither alone identifies a
            connection. api_call's `app_name` defaults to "anon", which matches no
            connection — a call that gives `name` and omits `app_name` is sent with no
            Authorization header, and the provider answers 401 as if the credentials
            were missing or the scopes wrong. They are not.
              - created_at    : str   — ISO 8601 creation timestamp
              - updated_at    : str   — ISO 8601 last-updated timestamp
              - auth_metadata : dict  — non-sensitive fields from the stored
                                        auth record, varies by app. Examples:
                  jira/zendesk      → api_domain
                  google_ads        → login_customer_id, scope, expires_in
                  openai            → org_id
                  slack             → app_id, scope, token_type, bot_user_id,
                                      team.{id,name}, enterprise,
                                      is_enterprise_install,
                                      authed_user.{id,scope,token_type}
                  intuit            → realm_id, scope, expires_in
                  netsuite          → account_id, signature_method, version
                  zoho              → api_domain, token_type, expires_in
                  salesforce/others → scope, token_type, expires_in
        """
        app_auth_page = await UserIntegration.list(
            ctx=ctx, db=db, user=user, integration_list=IntegrationRead(), limit=200, offset=0
        )
        details: List[Dict[str, Any]] = []
        for lite_doc in app_auth_page.docs:
            app_name = str(getattr(lite_doc.app_name, "value", lite_doc.app_name)).strip()
            auth_metadata: Dict[str, Any] = {}
            if lite_doc.id:
                full_doc = await UserIntegration.get(ctx=ctx, db=db, user=user, integration_id=lite_doc.id, decrypt=True)
                if full_doc and full_doc.auth:
                    auth_metadata = ActionTools._filter_auth(full_doc.auth)
            details.append(
                {
                    "id": str(lite_doc.id) if lite_doc.id else None,
                    "name": lite_doc.name,
                    "app_name": app_name,
                    "created_at": lite_doc.created_at.isoformat() if lite_doc.created_at else None,
                    "updated_at": lite_doc.updated_at.isoformat() if lite_doc.updated_at else None,
                    "auth_metadata": auth_metadata,
                }
            )
        return details

    @staticmethod
    async def create_action(
        config: ActionCreate,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
        space_member: SpaceMemberDoc | None = None,
    ) -> ActionDoc | HTTPException:
        """
        Description
        -----------
        Creates and persists a new user-scoped action based on the provided
        creation payload. The action is stored and returned as an ActionDoc,
        which contains the persisted representation including identifiers and
        metadata. If creation fails, an HTTPException is returned indicating
        the error.

        Input
        -----
        config : dict
            Expected structure:
                {
                    "name": "<action name>",
                    "type": "<action type identifier>",
                    "config": { ... action-specific config ... },
                    "metadata": { ... optional metadata ... }
                }

            name : str
                Human-readable name for the action.

            type : str
                Action type identifier that determines behavior and config shape.

            config : dict
                Action-specific configuration payload required by the chosen
                action type.

            metadata : dict | None
                Optional metadata stored with the action.

        Output
        ------
        dict | HTTPException
            On success returns the persisted ActionDoc (including id and stored
            config). On failure returns an HTTPException describing the error.
        """
        user_actions = UserActions(user=user, db=db)
        action_doc = await user_actions.create_action(ctx=ctx, app_state=app_state, action_create=config)
        match action_doc:
            case None:
                return HTTPException(status_code=500, detail="Action creation failed")
            case _:
                return action_doc

    class ActionId(BaseModel):
        action_id: str

    @staticmethod
    async def get_action(
        config: ActionId,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
        space_member: SpaceMemberDoc | None = None,
    ) -> ActionDoc | HTTPException:
        """
        Description
        -----------
        Retrieves a single persisted action by its identifier. Returns the full
        action document if found; if not found or retrieval fails, an
        HTTPException is returned. The action may be owned, shared, or a system
        action. If the returned action has an empty config ({}), it means the
        action is a shared or system action and its configuration is not exposed.
        A non-empty config means the action is owned by the user.

        Input
        -----
        config : dict
            Expected structure:
                { "action_id": "<id of the action to retrieve>" }

            action_id : str
                The unique identifier of the action to fetch.

        Output
        ------
        dict | HTTPException
            On success returns the ActionDoc containing the stored action.
            If the action cannot be retrieved or does not exist, returns an
            HTTPException with an appropriate status code and detail.
        """
        user_actions = UserActions(user=user, db=db)
        action_doc = await user_actions.get_owned_or_shared_or_system_actions(
            ctx=ctx, action_id=config.action_id
        )
        match action_doc:
            case None:
                return HTTPException(status_code=500, detail="Action get failed")
            case _:
                return action_doc

    @staticmethod
    async def find_action(
        config: ActionFind,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
        space_member: SpaceMemberDoc | None = None,
    ):
        """
        Description
        -----------
        Searches for actions matching the supplied query filters and returns a
        paginated list of action documents. This searches owned actions, actions
        shared with the user, and system actions. If no actions match the
        criteria, an HTTPException with a 404 status is returned.

        Input
        -----
        config : dict
            Expected structure (example):
                {
                    "name": "<optional name filter>",
                    "type": "<optional action type filter>",
                    "limit": <max results>,
                    "offset": <pagination offset>,
                    "id": "<unique identifier for any action>"
                }

            Typical filter fields include name, type, owner id, and pagination
            parameters.

        Output
        ------
        dict | HTTPException
            On success returns a paginated container of ActionDoc entries. If
            no actions are found the function returns an HTTPException with
            status_code 404. Other errors produce an HTTPException describing
            the failure.
        """
        user_actions = UserActions(user=user, db=db)

        # Search owned + shared actions
        config_with_shared = config.model_copy()
        config_with_shared.shared_with = user.email
        owned_shared_docs = await user_actions.list_owned_or_shared_with(
            action_find=config_with_shared
        )

        # Search system actions
        system_docs = await user_actions.list_system_actions(ctx=ctx, action_find=config)

        # Combine results
        all_docs = owned_shared_docs.docs + system_docs.docs
        total_count = owned_shared_docs.total_count + system_docs.total_count

        if len(all_docs) == 0:
            return HTTPException(status_code=404, detail="No Actions found")

        # TODO: This combines two already-paginated result sets, so limit/offset are not
        # strict for the merged response. This is acceptable for now because the system
        # action set is very small, but should be replaced with a single combined query
        # if the number of published system actions grows

        return ActionDocsFound(
            docs=all_docs,
            total_count=total_count,
            limit=owned_shared_docs.limit,
            offset=owned_shared_docs.offset,
        )

    class ActionUpdateConfig(BaseModel):
        action_id: str
        action_update: ActionUpdate

    # TODO: search action

    @staticmethod
    async def update_action(
        config: ActionUpdateConfig,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
        space_member: SpaceMemberDoc | None = None,
    ):
        """
        Description
        -----------
        Updates an existing action with the provided patch. The function looks
        up the action by id and applies the supplied update payload, returning
        the updated action document. If the update fails, an HTTPException is
        returned.

        Input
        -----
        config : dict
            Expected structure:
                {
                    "action_id": "<id of the action to update>",
                    "action_update": {
                        "name": "<optional new name>",
                        "config": { ... partial or full config ... },
                        "metadata": { ... updated metadata ... }
                    }
                }

            action_id : str
                Identifier of the action to update.

            action_update : dict
                Partial or full update payload to apply to the stored action.

        Output
        ------
        dict | HTTPException
            On success returns the updated ActionDoc. On failure returns an
            HTTPException describing the error.
        """
        user_actions = UserActions(user=user, db=db)
        action_doc = await user_actions.update_action(
            ctx=ctx, app_state=app_state, action_id=config.action_id, action_update=config.action_update,
        )
        match action_doc:
            case None:
                return HTTPException(status_code=500, detail="Action update failed")
            case _:
                return action_doc

    @staticmethod
    async def delete_action(
        config: ActionId,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
        space_member: SpaceMemberDoc | None = None,
    ):
        pass

    class ActionRunConfig(BaseModel):
        action_id: str
        action_run_id: str | None = None
        input: DataBlock

    @staticmethod
    async def run_action(
        config: ActionRunConfig,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
        space_member: SpaceMemberDoc | None = None,
    ):
        """
        Description
        -----------
        Executes a persisted action by id using the provided input payload.
        The action is launched in the background and this method returns
        immediately with an ActionResultLiteDoc containing the run id and
        status=processing. The actual execution continues in the background
        and results are persisted to MongoDB.

        Input
        -----
        config : dict
            Expected structure:
                1. A new conversation of an action
                    {
                        "action_id": "<id of the action to run>",

                        "input": {
                            "text": "<optional text>",
                            "urls": [ "<optional list of urls>" ],
                            ...
                        }
                    }
                2. Continue an existing action run conversation.
                    {
                        "action_id": "<id of the action to run>",
                        "action_run_id" : "<id of the action run to continue>",
                        "input": {
                            "text": "<optional text>",
                            "urls": [ "<optional list of urls>" ],
                            ...
                        }
                    }



            action_id : str
                The identifier of the action to execute.

            action_run_id : str | None
                The identifier of the action run to continue a conversation.

            input : dict
                The runtime input for the action. Shape depends on the action's
                declared input type (commonly includes "text" and/or "urls").

        Output
        ------
        dict | Exception
            On success returns an ActionResultLiteDoc summarizing the run
            (ids, status, timestamps). The action execution runs in the background
            and this method returns immediately with status=processing. If the
            action is not found or the run fails to start, an HTTPException is
            returned.
        """
        user_actions = UserActions(user=user, db=db)
        user_action_result = UserActionResult(user=user, db=db)

        # Get the action doc first (owned, shared, or system actions)
        action_doc = await user_actions.get_owned_or_shared_or_system_actions(
            ctx=ctx, action_id=config.action_id, empty_shared_action_config=False,
        )

        if action_doc is None and config.action_run_id:
            action_result_doc: ActionResultDoc | None = await user_action_result.get_action_result(
                action_result_id=config.action_run_id
            )
            if action_result_doc is not None:
                action_doc = action_result_doc.result

        if not action_doc:
            return HTTPException(status_code=404, detail="Action not found")
        if config.action_run_id:
            try:
                action_result_doc = await user_action_result.get_action_result(
                    action_result_id=config.action_run_id
                )
                logger.bind(ctx=ctx).info(f"Current Action Run status : {action_result_doc.status}")
                if action_result_doc.status != "success":
                    return HTTPException(
                        status_code=500,
                        detail=f"Action Run Status is not success. Current Action Run status : {action_result_doc.status}, so u are not in a position to run this action run at the moment. You may try later.",
                    )
            except Exception as e:
                logger.bind(ctx=ctx).info(
                    f"Received an error while running action run tool -- {str(e)}"
                )
                return e

        # Run action in background (fire and forget)
        # Don't pass notify - let it create its own with the new action's ID as topic
        child_app_state = app_state.child_context(
            action_graph_id=app_state.action_graph_id,
            action_graph_result_id=app_state.action_graph_result_id,
            action_graph_node_id=app_state.action_graph_node_id,
            action_team_id=app_state.action_team_id,
            action_team_result_id=app_state.action_team_result_id,
        )
        action_result_doc: ActionResultDoc | None = await ActionFactory.run_action_in_background(
            ctx=ctx,
            app_state=child_app_state,
            action_doc=action_doc,
            action_input_dict=config.input.model_dump(exclude_none=True),
            user_action_result=user_action_result,
            action_result_id=config.action_run_id,
            db=db,
            fire_and_forget=True,
        )

        match action_result_doc:
            case ActionResultDoc():
                action_result_lite_doc = ActionResultLiteDoc.model_validate(
                    action_result_doc.model_dump()
                )
                return action_result_lite_doc
            case None:
                return HTTPException(status_code=500, detail="Action run failed")
            case _:
                return HTTPException(
                    status_code=500,
                    detail="Action run returned unexpected response type, cannot process result",
                )
