from typing import Optional

from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action_graph.action_graph.action_graph_doc_model import (
    ActionGraphCreate,
    ActionGraphDoc,
    ActionGraphDocsFound,
    ActionGraphFind,
    ActionGraphUpdate,
)
from src.autobots.action_graph.action_graph.user_action_graph import UserActionGraphs
from src.autobots.action_graph.action_graph_result.action_graph_result_model_doc import (
    ActionGraphResultDoc,
    ActionGraphResultLiteDoc,
)
from src.autobots.action_graph.action_graph_result.user_action_graph_result import (
    UserActionGraphResult,
)
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState

class ActionGraphTools:
    @staticmethod
    async def create_action_graph(
        config: ActionGraphCreate,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
        space_member: SpaceMemberDoc | None = None,
    ) -> ActionGraphDoc | HTTPException:
        """
        Description
        -----------
        Persists a new action graph for the current user. The creation payload
        describes nodes, node metadata, edges, and runtime defaults. On success
        the fully persisted ActionGraph document is returned. Unexpected errors
        are logged and returned as an HTTPException.

        Input
        -----
        config : dict
            Expected structure:
                {
                    "name": "<graph name>",
                    "version": <float>,
                    "description": "<optional description>",
                    "user_manual": "<optional user manual text>",
                    "nodes": { "<node_id>": "<action_id>", ... } | None,
                    "node_details": {
                        "<node_id>": {
                            "id": "<node_id>",
                            "position": {"x": <int>, "y": <int>},
                            "type": "<node type>",
                            "data": { "label": "<label>", "actionId": "<action_id>" , ...}
                        },
                        ...
                    },
                    "graph": { "<node_id>": ["<node_id>", ...], ... } | None,
                    "start_node_id": "<node_id>" | None,
                    "input": { ... runtime input defaults ... } | None,
                    "output": { "<node_id>": <ActionDoc>, ... } | None,
                    "is_saved": true|false,
                    "is_published": true|false,
                    "shared_with": ["<user_id>", ...],
                    "time_saved_per_file": <float>,
                    "dollar_cost_per_hour": <float>
                }

            Important fields:
              - name (str): Graph display name.
              - nodes (map): mapping from node_id to action_id (optional at create).
              - node_details (map): complete per-node metadata used by the UI and runner.
              - graph (map): adjacency list mapping node to downstream node ids.
              - start_node_id (str): optional entry node for execution.
              - input/output: runtime defaults and prior outputs (optional).
              - is_saved / is_published / shared_with: management flags.

        Output
        ------
        dict | HTTPException
            On success returns the persisted ActionGraphDoc containing identifiers,
            stored node/edge structures, and metadata. On failure returns an
            HTTPException with an explanatory message.
        """
        try:
            user_action_graphs = UserActionGraphs(user=user, db=db)
            action_graph_doc = await user_action_graphs.create(action_graph_create=config)
            return action_graph_doc
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in create_action_graph: {str(e)}")
            return HTTPException(status_code=500, detail=f"Action Graph creation failed: {str(e)}")

    class ActionGraphId(BaseModel):
        action_graph_id: str

    @staticmethod
    async def get_action_graph(
        config: ActionGraphId,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
        space_member: SpaceMemberDoc | None = None,
    ) -> ActionGraphDoc | HTTPException:
        """
        Description
        -----------
        Retrieves an action graph by its identifier. The action graph may be
        owned by the user or shared with them. The full stored ActionGraph
        document is returned when found. If the graph cannot be retrieved or
        does not exist, an HTTPException is returned. If the returned graph
        has an empty config, it means the graph is shared and its configuration
        is not exposed. A non-empty config means the graph is owned by the user.

        Input
        -----
        config : dict
            Expected structure:
                { "action_graph_id": "<id of the action graph>" }

            action_graph_id : str
                Identifier of the action graph to fetch.

        Output
        ------
        dict | HTTPException
            On success returns the ActionGraphDoc with nodes, edges, config,
            and metadata. If the graph is not found or retrieval fails, an
            HTTPException with an appropriate status and detail is returned.
        """
        user_action_graphs = UserActionGraphs(user=user, db=db)
        action_graph_doc = await user_action_graphs.get_owned_or_shared(
            ctx=ctx, action_graph_id=config.action_graph_id
        )
        match action_graph_doc:
            case None:
                return HTTPException(status_code=500, detail="Action Graph get failed")
            case _:
                return action_graph_doc

    @staticmethod
    async def find_action_graph(
        config: ActionGraphFind,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
        space_member: SpaceMemberDoc | None = None,
    ) -> ActionGraphDocsFound | HTTPException:
        """
        Description
        -----------
        Searches for action graphs owned by or shared with the user according to
        the provided filter criteria. Returns a paginated list of matching
        ActionGraph documents. If no graphs match the query a 404-style
        HTTPException is returned.

        Input
        -----
        config : dict
            Expected structure (example):
                {
                    "name": "<optional name filter>",
                    "id": "<optional owner id>",
                    "shared_with": "<optional user id>",
                    "limit": <max results>,
                    "offset": <pagination offset>
                }

            Typical fields include name filters, owner or sharing filters, and
            pagination controls.

        Output
        ------
        dict | HTTPException
            On success returns a paginated container (ActionGraphDocsFound)
            with matching ActionGraphDocs. If no graphs match, returns an
            HTTPException with status_code 404. Other failures return an
            HTTPException describing the error.
        """
        user_action_graphs = UserActionGraphs(user=user, db=db)
        action_graph_docs_found = await user_action_graphs.list_owned_or_shared(
            action_graph_find=config
        )
        if len(action_graph_docs_found.docs) == 0:
            return HTTPException(status_code=404, detail="No Action Graph found")
        else:
            return action_graph_docs_found

    class ActionGraphUpdateConfig(BaseModel):
        action_graph_id: str
        action_graph_update: ActionGraphUpdate

    @staticmethod
    async def update_action_graph(
        config: ActionGraphUpdateConfig,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
        space_member: SpaceMemberDoc | None = None,
    ) -> ActionGraphDoc | HTTPException:
        """
        Description
        -----------
        Applies a partial update to an existing action graph. Any provided
        fields are merged/overwritten into the stored graph document (for
        example: nodes mapping, node_details, graph topology, flags, and
        metadata). The updated ActionGraphDoc is returned on success. If the
        update fails, an HTTPException is returned.

        Input
        -----
        config : dict
            Expected structure:
                {
                    "action_graph_id": "<id of the graph to update>",
                    "action_graph_update": {
                        "name": "<optional new name>",
                        "version": <optional float>,
                        "description": "<optional>",
                        "user_manual": "<optional>",
                        "nodes": { "<node_id>": "<action_id>", ... } | None,
                        "node_details": { "<node_id>": { ... Node ... }, ... } | None,
                        "graph": { "<node_id>": ["<node_id>", ...], ... } | None,
                        "start_node_id": "<node_id>" | None,
                        "input": { ... } | None,
                        "output": { "<node_id>": <ActionDoc>, ... } | None,
                        "is_saved": true|false | None,
                        "is_published": true|false | None,
                        "shared_with": ["<user_id>", ...] | None,
                        "time_saved_per_file": <float> | None,
                        "dollar_cost_per_hour": <float> | None
                    }
                }

            action_graph_update : dict
                Partial or full update payload to apply to the stored action
                graph. Only supplied fields are changed.

        Output
        ------
        dict | HTTPException
            On success returns the updated ActionGraphDoc. On failure returns an
            HTTPException explaining why the update failed.
        """
        user_action_graphs = UserActionGraphs(user=user, db=db)
        action_graph_doc = await user_action_graphs.update(
            app_state=app_state, action_graph_id=config.action_graph_id, action_graph_update=config.action_graph_update
        )
        match action_graph_doc:
            case None:
                return HTTPException(status_code=500, detail="Action graph update failed")
            case _:
                return action_graph_doc

    @staticmethod
    async def delete_action_graph(
        config: ActionGraphId,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
        space_member: SpaceMemberDoc | None = None,
    ):
        pass

    class ActionGraphRunConfig(BaseModel):
        action_graph_id: str
        input: DataBlock
        action_graph_result_id: Optional[str] = None
        action_graph_node_id: Optional[str] = None
        is_rerun_graph: bool = False
        is_empty_downstream_only: bool = False

    @staticmethod
    async def run_action_graph(
        config: ActionGraphRunConfig,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
        space_member: SpaceMemberDoc | None = None,
    ) -> ActionGraphResultLiteDoc | HTTPException:
        """
        Description
        -----------
        Executes a defined action graph for the user. The graph runner will
        schedule or run the graph (background invocation) and return a lightweight
        result document summarizing the run. Optional fields control rerun
        behavior and whether to execute only nodes with empty downstream
        results. On success a compact ActionGraphResultLiteDoc is returned;
        failures are returned as HTTPException or the raised exception.

        Input
        -----
        config : dict
            Expected structure:
                {
                    "action_graph_id": "<id of the graph to run>",
                    "input": { "text": "<optional text>", "urls": ["..."] },
                    "action_graph_result_id": "<optional existing run id>",
                    "action_graph_node_id": "<optional node id to start from>",
                    "is_rerun_graph": true|false,
                    "is_empty_downstream_only": true|false
                }

            action_graph_id : str
                Identifier of the graph to execute.

            input : dict
                Runtime input passed to the graph entry node(s). Shape depends on
                node input types (commonly includes "text" and/or "urls").

            action_graph_result_id : str | None
                Optional id to attach or resume an existing run.

            action_graph_node_id : str | None
                Optional node id to start or focus execution from.

            is_rerun_graph : bool
                If true, forces rerunning nodes even if prior results exist.

            is_empty_downstream_only : bool
                If true, only executes nodes whose downstream results are empty.

        Output
        ------
        dict | HTTPException
            On success returns an ActionGraphResultLiteDoc summarizing the run
            (ids, status, timestamps). The graph execution runs in the background
            and this method returns immediately with status=processing. If the
            run fails to start or an unexpected error occurs, an HTTPException
            or exception object is returned instead.
        """
        user_action_graphs = UserActionGraphs(user=user, db=db)
        user_actions = UserActions(user=user, db=db)
        user_action_graph_result = UserActionGraphResult(user=user, db=db)
        child_app_state = app_state.child_context(
            action_graph_id=app_state.action_graph_id,
            action_graph_result_id=app_state.action_graph_result_id,
            action_graph_node_id=app_state.action_graph_node_id,
            action_team_id=app_state.action_team_id,
            action_team_result_id=app_state.action_team_result_id,
        )
        action_graph_result_doc: (
            ActionGraphResultDoc | None
        ) = await user_action_graphs.run_in_background(
            ctx=ctx,
            app_state=child_app_state,
            user_actions=user_actions,
            user_action_graph_result=user_action_graph_result,
            action_graph_id=config.action_graph_id,
            action_graph_result_id=config.action_graph_result_id,
            input=config.input,
            is_rerun_graph=config.is_rerun_graph,
            is_empty_downstream_only=config.is_empty_downstream_only,
            fire_and_forget=True,
        )
        match action_graph_result_doc:
            case ActionGraphResultDoc():
                action_graph_result_lite_doc = ActionGraphResultLiteDoc.model_validate(
                    action_graph_result_doc.model_dump()
                )
                return action_graph_result_lite_doc
            case None:
                return HTTPException(status_code=500, detail="Action Graph run failed")
            case _:
                return HTTPException(status_code=500, detail="Action graph run returned unexpected response type, cannot process result")
