from typing import Any, Dict, Optional

from pydantic import BaseModel


class ActionWorkflowInput(BaseModel):
    action_id: str
    action_run_id: str
    #: The run's owner. Everything the activity reads or writes is scoped to it.
    user_id: str
    space_id: str | None
    #: Who asked for this turn. Equal to `user_id` until the ingress accepts
    #: participants; carried now so that opening it is not also a change of scope.
    actor_user_id: Optional[str] = None
    trace_id: Optional[str] = None
    consumer_id: Optional[str] = None
    #: The actor's token, not the owner's. Roughly ten action types call back
    #: into the API with it and error outright without one, as scheduled runs
    #: already do. So once actor and owner differ this cannot simply be
    #: dropped: an owner token has to be minted instead.
    access_token: Optional[str] = None
    input: Dict[str, Any] = {}
    webhook: Optional[Dict[str, Any]] = None
