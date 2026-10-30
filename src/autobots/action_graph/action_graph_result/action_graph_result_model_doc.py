from typing import List, Optional

from src.autobots.action_graph.action_graph.action_graph_doc_model import (
    ActionGraphDoc,
    ActionGraphFind,
    ActionGraphLiteDoc,
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


class ActionGraphResultUpdate(EventResultUpdate):
    """
    Input from User to update Action Result
    """

    result: Optional[ActionGraphDoc | ActionGraphLiteDoc] = None


class ActionGraphResultCreate(EventResultCreate):
    type: EventType = EventType.action_graph_run
    result: ActionGraphDoc | ActionGraphLiteDoc


class ActionGraphResultFind(EventResultFind):
    result: ActionGraphFind | None = None


class ActionGraphResultDoc(EventResultDoc):
    result: ActionGraphDoc | ActionGraphLiteDoc


class ActionGraphResultLiteDoc(EventResultLiteDoc):
    result: Optional[ActionGraphLiteDoc] = None


class ActionGraphResultDocsFound(DocFindPage):
    docs: List[ActionGraphResultLiteDoc]


class ActionGraphResultDocSearch(EventResultDocSearch):
    type: EventType = EventType.action_graph_run
