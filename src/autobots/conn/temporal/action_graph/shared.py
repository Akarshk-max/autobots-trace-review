from typing import Any, Dict, Optional

from pydantic import BaseModel


class ActionGraphWorkflowInput(BaseModel):
    action_graph_id: str
    action_graph_result_id: str
    #: The run's owner. Everything the activity reads or writes is scoped to it.
    user_id: str
    space_id: str | None
    #: Who asked for this turn. Equal to `user_id` until the ingress accepts
    #: participants; carried now so that opening it is not also a change of scope.
    actor_user_id: Optional[str] = None
    trace_id: Optional[str] = None
    #: The actor's token, not the owner's. Roughly ten action types call back
    #: into the API with it and error outright without one, as scheduled runs
    #: already do. So once actor and owner differ this cannot simply be
    #: dropped: an owner token has to be minted instead.
    access_token: Optional[str] = None
    input: Dict[str, Any] = {}
    webhook: Optional[Dict[str, Any]] = None
    action_graph_node_id: Optional[str] = None
    is_rerun_graph: bool = False
    is_continue_run_graph: bool = False
    is_empty_downstream_only: bool = False
    batch_id: Optional[str] = None
    start_to_close_timeout_minutes: int = 1200
