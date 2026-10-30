from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic_extra_types.pendulum_dt import DateTime

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import TextObj
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.action_graph.action_graph.context_vector import NodeContextVector
from src.autobots.action_graph.action_graph.edge_decision import EdgeDecision
from src.autobots.action_graph.action_graph.redirect_budget import RedirectBudget
from src.autobots.core.clock.clock import Clock
from src.autobots.core.database.mongo_base_crud import DocFindPage
from src.autobots.data_model.space_id import OptionalSpaceId, SpaceId
from src.autobots.data_model.updated_at import UpdatedAt
from src.autobots.run_metrics.run_metrics_doc_model import NodeUnits


class Position(BaseModel):
    x: int
    y: int


class DataFlowType(str, Enum):
    only_output = "only_output"
    both_input_output = "both_input_output"
    as_is_last_output = "as_is_last_output"


class EdgeSpec(BaseModel):
    """One non-data edge.

    Deliberately carries no `type`, and there is deliberately no EdgeType enum: the list an edge
    lives in is what types it, so there is no second source of truth that could disagree with the
    first. `graph` holds the data edges; `redirect_edges` and `conditional_edges` hold the rest.
    """

    source: str = Field(description="node_id the edge leaves from")
    target: str = Field(description="node_id the edge arrives at")
    id: Optional[str] = Field(default=None, description="frontend (React Flow) edge id")


class NodeData(BaseModel):
    label: str
    actionId: str
    type: ActionType | None = None
    updated_at: datetime | None = None
    characterId: str | None = None
    data_flow_type: DataFlowType = DataFlowType.only_output
    user_review_required: bool = False
    user_review_done: bool = False
    user_input_required: bool = False
    user_input_done: bool = False
    is_loop: bool = False
    seperator: str = "|"
    is_split: bool = False
    file_split: bool = False
    api_loop: bool = False
    excel_loop: bool = False
    header: bool = False
    data_processing: bool = False
    file_processing: bool = False
    file_column_index: int = 0
    instruction_column_index: int = 0
    batches: int = 0
    process_url_in_text: bool = True
    process_attachment_url: bool = True
    attachment_requested: int = 0
    sleep_time: float = Field(
        default=0,
        ge=0,
        description="Seconds to sleep before executing the node action.",
    )
    time_saved_per_unit: Optional[float] = Field(
        default=None,
        description=(
            "Hours a person would spend on one unit this node processes - one file, row, text "
            "item or API batch, whichever this node loops over. Lives here rather than on the "
            "graph so the rate and the count it multiplies describe the same thing. Falls back "
            "to the graph's `time_saved_per_file` when unset."
        ),
    )


class Node(BaseModel):
    id: str
    position: Optional[Position] = None
    type: Optional[str] = None
    data: NodeData


class ActionGraphFind(BaseModel):
    """
    Input from User to find action_graph
    """

    id: Optional[str] = Field(default=None)  # , alias='_id')
    name: Optional[str] = None
    version: Optional[float] = None
    description: Optional[str] = None
    user_manual: Optional[str] = None
    is_saved: Optional[bool] = None
    is_published: Optional[bool] = None
    shared_with: Optional[SpaceId | str] = None


class ActionGraphDocFind(ActionGraphFind, OptionalSpaceId):
    """
    Add in user id to enforce multi-tenancy
    """

    user_id: str | None = None


# class ActionGraphPublishedDocFind(ActionGraphDocFind):
#     user_id: str | None = None
#     is_published: bool = True


class ActionGraphSharedWithDocFind(ActionGraphDocFind):
    user_id: str | None = None


class ActionGraphUpdate(BaseModel):
    name: Optional[str] = None
    version: Optional[float] = None
    description: Optional[str] = None
    user_manual: Optional[str] = None
    nodes: Optional[Dict[str, str]] = Field(
        default=None,
        description="map of node to action",
        examples=[{"node1": "action1", "node2": "action2", "node3": "action3"}],
    )
    node_details: Dict[str, Node] | None = Field(
        default=None,
        description="map of nodeId to Node",
        examples=[
            {
                "node1": {
                    "id": "node1",
                    "position": {"x": 288, "y": 203},
                    "type": "default",
                    "data": {"label": "action1_name_version", "actionId": "action1"},
                }
            }
        ],
    )
    graph: Optional[Dict[str, List[str]]] = Field(
        default=None,
        description="map of node to nodes. Data edges only; must stay acyclic.",
        examples=[{"node1": ["node2", "node3"], "node2": ["node3"]}],
    )
    # These default to None, not []. CRUDBase.update_one builds its $set from model_dump() and
    # skips only None, so a [] default would be written on every save - silently wiping a graph's
    # edges whenever the frontend saved an unrelated field. Every field here is Optional for this
    # same reason.
    redirect_edges: Optional[List[EdgeSpec]] = Field(
        default=None,
        description="Back edges. Omit to leave the stored value untouched.",
        examples=[[{"source": "node3", "target": "node1"}]],
    )
    conditional_edges: Optional[List[EdgeSpec]] = Field(
        default=None,
        description="Conditional markers on edges in `graph`. Omit to leave the stored value untouched.",
        examples=[[{"source": "node1", "target": "node2"}]],
    )
    start_node_id: str | None = None
    input: Optional[TextObj] = None
    output: Optional[Dict[str, ActionDoc]] = None
    node_units: Optional[Dict[str, NodeUnits]] = Field(
        default=None,
        description=(
            "Run state: node_id -> what that node processed, and the hours it saved. Only loop "
            "nodes appear; a run with none did one unit of work. Durable on the document for the "
            "same reason as `redirect_budgets` - a resumed run continues in a fresh AppState, so "
            "an in-memory tally would lose everything the first segment did."
        ),
    )
    execution_seconds: Optional[float] = Field(
        default=None,
        description=(
            "Run state: seconds actually spent executing, accumulated across segments. Excludes "
            "time spent waiting, and cannot be derived from the timestamps - `updated_at` moves "
            "on every later save."
        ),
    )
    node_vectors: Optional[Dict[str, NodeContextVector]] = Field(
        default=None,
        description=(
            "Run state: node_id -> context vector. Absent means a plain DAG run, where slot 0 is "
            "the only dimension and `output` says everything."
        ),
    )
    redirect_budgets: Optional[Dict[str, RedirectBudget]] = Field(
        default=None,
        description=(
            "Run state: '<target>|<source>' -> how many times that redirect edge has fired and its "
            "limit. Absent means a plain DAG run with no redirect edges to bound. Durable on the "
            "document on purpose - an in-memory counter would reset on each of the three stacked "
            "retry layers and stop bounding anything."
        ),
    )
    turn_counters: Optional[Dict[str, int]] = Field(
        default=None,
        description=(
            "Run state: '<node>|<context_index>' -> how many turns that column has handed out, "
            "which is the turn the next one takes. Absent means a plain DAG run, which opens no "
            "turns. Counted when a turn is *opened*, not when one is recorded: a queued turn has "
            "written nothing, so counting the transcript hands the same number to two turns that "
            "are in flight together. Durable for the same reason as `redirect_budgets`."
        ),
    )
    open_turns: Optional[Dict[str, str]] = Field(
        default=None,
        description=(
            "Run state: '<node>|<context_index>|<turn_no>' -> the redirect target that opened "
            "that turn. A turn is removed once every node in its relevant subgraph has recorded "
            "it, so what remains is what is genuinely outstanding. Absent means a plain DAG run, "
            "which opens no turns. Exists because the turn counter was durable while the open "
            "frames were not: a crash mid-turn left nodes owing a turn nobody could name, and the "
            "retry opened the next one instead of completing it (RISK-12). The target is the "
            "missing piece - R.S.(i, j) is keyed by the pair."
        ),
    )
    open_continue_run: Optional[str] = Field(
        default=None,
        description=(
            "Run state: '<root>|<turn_no>' - a continue-run that was started and has not "
            "finished. A continue-run's identity lived only in the request parameters, so a "
            "human gate mid-operation did not pause it, it cancelled it: the root and the turn "
            "died with the function, and the next run asked every node about turn 0 instead, "
            "found them all done, and reported success having executed nothing. Same shape as "
            "`open_turns` and for the same reason - the work was durable, the frame was not. "
            "Cleared once every participant has recorded the turn. Absent means no continue-run "
            "is outstanding, which is every ordinary run."
        ),
    )
    edge_decisions: Optional[Dict[str, EdgeDecision]] = Field(
        default=None,
        description=(
            "Run state: '<target>|<source>|<context_index>|<turn_no>' -> what that edge answered "
            "on that turn. Keyed by edge because one node runs once but can answer differently for "
            "each edge leaving it, and by turn because it can answer differently on each. Absent "
            "means nothing has been asked, and an edge never asked contributes as it always has - "
            "which is every graph today, since the decider arrives in Part 5."
        ),
    )
    is_saved: Optional[bool] = None
    is_published: Optional[bool] = None
    shared_with: Optional[List[SpaceId | str]] = None
    # None, not 0.1/50: a non-None default here is written on every save (see the note on
    # redirect_edges above), so a rename would reset whatever the user had configured.
    # ActionGraphCreate still supplies the real defaults at creation time.
    time_saved_per_file: Optional[float] = Field(
        default=None,
        description="Time saved per file in hours for dashboard workflow calculations",
    )
    dollar_cost_per_hour: Optional[float] = Field(
        default=None,
        description="Dollar cost per hour for dashboard workflow calculations",
    )


class ActionGraphDocUpdate(ActionGraphUpdate, UpdatedAt):
    id: str
    user_id: str
    # updated_at: datetime = datetime.now()


class ActionGraphCreate(BaseModel):
    """
    Input from User to create action_graph
    """

    name: str
    version: float = 0
    description: str = ""
    user_manual: str = ""
    nodes: Optional[Dict[str, str]] = Field(
        None,
        description="map of node_id to action_id",
        examples=[{"node1": "action1", "node2": "action2", "node3": "action3"}],
    )
    node_details: Dict[str, Node] = Field(
        default={},
        description="map of node_id to Node",
        examples=[
            {
                "node1": {
                    "id": "node1",
                    "position": {"x": 288, "y": 203},
                    "type": "default",
                    "data": {"label": "action1_name_version", "actionId": "action1"},
                }
            }
        ],
    )
    graph: Optional[Dict[str, List[str]]] = Field(
        None,
        description="map of node_id to list of node_ids. Data edges only; must stay acyclic.",
        examples=[{"node1": ["node2", "node3"], "node2": ["node3"]}],
    )
    redirect_edges: List[EdgeSpec] = Field(
        default=[],
        description=(
            "Back edges. `source` must be a descendant of `target` in `graph`. Empty means the "
            "graph is a plain DAG and behaves exactly as before."
        ),
        examples=[[{"source": "node3", "target": "node1"}]],
    )
    conditional_edges: List[EdgeSpec] = Field(
        default=[],
        description=(
            "Marks edges already present in `graph` as conditional - the caller decides at "
            "runtime whether to fire them."
        ),
        examples=[[{"source": "node1", "target": "node2"}]],
    )
    start_node_id: str | None = None
    input: Optional[Dict[str, Any]] = None
    output: Optional[Dict[str, ActionDoc]] = None
    node_units: Optional[Dict[str, NodeUnits]] = Field(
        default=None,
        description=(
            "Run state: node_id -> what that node processed, and the hours it saved. Only loop "
            "nodes appear; a run with none did one unit of work. Durable on the document for the "
            "same reason as `redirect_budgets` - a resumed run continues in a fresh AppState, so "
            "an in-memory tally would lose everything the first segment did."
        ),
    )
    execution_seconds: Optional[float] = Field(
        default=None,
        description=(
            "Run state: seconds actually spent executing, accumulated across segments. Excludes "
            "time spent waiting, and cannot be derived from the timestamps - `updated_at` moves "
            "on every later save."
        ),
    )
    node_vectors: Optional[Dict[str, NodeContextVector]] = Field(
        default=None,
        description=(
            "Run state: node_id -> context vector. Absent means a plain DAG run, where slot 0 is "
            "the only dimension and `output` says everything."
        ),
    )
    redirect_budgets: Optional[Dict[str, RedirectBudget]] = Field(
        default=None,
        description=(
            "Run state: '<target>|<source>' -> how many times that redirect edge has fired and its "
            "limit. Absent means a plain DAG run with no redirect edges to bound. Durable on the "
            "document on purpose - an in-memory counter would reset on each of the three stacked "
            "retry layers and stop bounding anything."
        ),
    )
    turn_counters: Optional[Dict[str, int]] = Field(
        default=None,
        description=(
            "Run state: '<node>|<context_index>' -> how many turns that column has handed out, "
            "which is the turn the next one takes. Absent means a plain DAG run, which opens no "
            "turns. Counted when a turn is *opened*, not when one is recorded: a queued turn has "
            "written nothing, so counting the transcript hands the same number to two turns that "
            "are in flight together. Durable for the same reason as `redirect_budgets`."
        ),
    )
    open_turns: Optional[Dict[str, str]] = Field(
        default=None,
        description=(
            "Run state: '<node>|<context_index>|<turn_no>' -> the redirect target that opened "
            "that turn. A turn is removed once every node in its relevant subgraph has recorded "
            "it, so what remains is what is genuinely outstanding. Absent means a plain DAG run, "
            "which opens no turns. Exists because the turn counter was durable while the open "
            "frames were not: a crash mid-turn left nodes owing a turn nobody could name, and the "
            "retry opened the next one instead of completing it (RISK-12). The target is the "
            "missing piece - R.S.(i, j) is keyed by the pair."
        ),
    )
    open_continue_run: Optional[str] = Field(
        default=None,
        description=(
            "Run state: '<root>|<turn_no>' - a continue-run that was started and has not "
            "finished. A continue-run's identity lived only in the request parameters, so a "
            "human gate mid-operation did not pause it, it cancelled it: the root and the turn "
            "died with the function, and the next run asked every node about turn 0 instead, "
            "found them all done, and reported success having executed nothing. Same shape as "
            "`open_turns` and for the same reason - the work was durable, the frame was not. "
            "Cleared once every participant has recorded the turn. Absent means no continue-run "
            "is outstanding, which is every ordinary run."
        ),
    )
    edge_decisions: Optional[Dict[str, EdgeDecision]] = Field(
        default=None,
        description=(
            "Run state: '<target>|<source>|<context_index>|<turn_no>' -> what that edge answered "
            "on that turn. Keyed by edge because one node runs once but can answer differently for "
            "each edge leaving it, and by turn because it can answer differently on each. Absent "
            "means nothing has been asked, and an edge never asked contributes as it always has - "
            "which is every graph today, since the decider arrives in Part 5."
        ),
    )
    is_saved: bool = False
    is_published: bool = False
    shared_with: List[SpaceId | str] = []
    time_saved_per_file: Optional[float] = Field(
        default=0.1,
        description="Time saved per file in hours for dashboard workflow calculations",
    )
    dollar_cost_per_hour: Optional[float] = Field(
        default=50,
        description="Dollar cost per hour for dashboard workflow calculations",
    )


class ActionGraphDocCreate(OptionalSpaceId, ActionGraphCreate, UpdatedAt):
    """
    Add in user id to enforce multi-tenancy
    """

    user_id: str
    created_at: datetime = Clock.datetime_now()
    # updated_at: datetime = datetime.now()


class ActionGraphDoc(ActionGraphDocCreate):
    __collection__ = "ActionGraphs"

    id: str = Field(..., alias="_id")

    model_config = ConfigDict(populate_by_name=True)


class ActionGraphLiteDoc(OptionalSpaceId, BaseModel):
    id: str = Field(..., alias="_id")
    name: str
    version: float = 0
    description: str = ""
    user_manual: str = ""
    is_saved: bool = False
    is_published: bool = False
    shared_with: List[SpaceId | str] = []
    user_id: str
    created_at: datetime
    updated_at: DateTime
    time_saved_per_file: Optional[float] = 0.1
    dollar_cost_per_hour: Optional[float] = 50

    model_config = ConfigDict(populate_by_name=True)


class ActionGraphDocsFound(DocFindPage):
    docs: List[ActionGraphLiteDoc]
