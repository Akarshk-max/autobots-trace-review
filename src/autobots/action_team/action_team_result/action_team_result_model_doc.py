from typing import List, Optional

from src.autobots.action_team.action_team.action_team_doc_model import (
    ActionTeamDoc,
    ActionTeamFind,
    ActionTeamLiteDoc,
)
from src.autobots.core.database.mongo_base_crud import DocFindPage
from src.autobots.event_result.event_result_model import (
    EventResultCreate,
    EventResultDoc,
    EventResultDocSearch,
    EventResultFind,
    EventResultLiteDoc,
    EventResultUpdate,
    EventType,
)


class ActionTeamResultUpdate(EventResultUpdate):
    result: Optional[ActionTeamDoc | ActionTeamLiteDoc] = None


class ActionTeamResultCreate(EventResultCreate):
    type: EventType = EventType.action_team_run
    result: ActionTeamDoc | ActionTeamLiteDoc


class ActionTeamResultFind(EventResultFind):
    result: ActionTeamFind | None = None


class ActionTeamResultDoc(EventResultDoc):
    result: ActionTeamDoc | ActionTeamLiteDoc


class ActionTeamResultLiteDoc(EventResultLiteDoc):
    result: Optional[ActionTeamLiteDoc] = None


class ActionTeamResultDocsFound(DocFindPage):
    docs: List[ActionTeamResultLiteDoc]


class ActionTeamResultDocSearch(EventResultDocSearch):
    type: EventType = EventType.action_team_run
