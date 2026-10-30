from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, PrivateAttr, computed_field

from src.autobots.core.cancellation.run_cancellation import RunCancellation
from src.autobots.core.logging.app_code import AppCode
from src.autobots.token_usage.token_usage_doc_model import TokenUsageSource

if TYPE_CHECKING:
    from src.autobots.token_usage.token_usage import TokenUsageCollector


class AppState(BaseModel):

    """
    This will contain all the state information that is relevant to current run.
    such as action_id , action_run_id, action_graph_id, action_graph_result_id, action_graph_node_id, action_team_id
    """
    action_id: str | None = None
    action_run_id: str | None = None
    action_name: str | None = None
    action_run_name: str | None = None

    action_graph_id: str | None = None
    action_graph_result_id: str | None = None
    action_graph_node_id: str | None = None

    action_team_id: str | None = None
    action_team_result_id: str | None = None
    action_team_leader_action_id: str | None = None
    action_team_member_id: str | None = None

    parent_action_id: str | None = None
    parent_action_run_id: str | None = None
    parent_action_graph_id: str | None = None
    parent_action_graph_result_id: str | None = None

    ancestors: list[dict[str, str | None]] = []

    source: TokenUsageSource = TokenUsageSource.action
    source_id: str | None = None

    _cancellation: RunCancellation | None = PrivateAttr(default=None)
    _token_usage_collector: "TokenUsageCollector | None" = PrivateAttr(default=None)

    @property
    def cancellation(self) -> RunCancellation | None:
        return self._cancellation

    def set_cancellation(self, cancellation: RunCancellation | None) -> None:
        self._cancellation = cancellation

    @property
    def token_usage_collector(self) -> "TokenUsageCollector | None":
        return self._token_usage_collector

    def set_token_usage_collector(
        self, token_usage_collector: "TokenUsageCollector | None"
    ) -> None:
        self._token_usage_collector = token_usage_collector

    def __setstate__(self, state: dict[str, Any]) -> None:
        super().__setstate__(state)
        for name, field in type(self).model_fields.items():
            if name not in self.__dict__:
                self.__dict__[name] = field.get_default(call_default_factory=True)
        private = self.__pydantic_private__
        if private is None:
            private = {}
            object.__setattr__(self, "__pydantic_private__", private)
        for name, private_attr in type(self).__private_attributes__.items():
            if name not in private:
                private[name] = private_attr.get_default()

    def raise_if_cancelled(self) -> None:
        """Cooperative checkpoint. Safe to call when no token is attached."""
        if self._cancellation is not None:
            self._cancellation.raise_if_cancelled()

    @computed_field
    @property
    def app_type(self) -> AppCode | None:
        if self.action_id or self.action_run_id:
            return AppCode.ACTION
        if self.action_graph_id or self.action_graph_result_id:
            return AppCode.ACTION_GRAPH
        if self.action_team_id or self.action_team_result_id:
            return AppCode.ACTION_TEAM
        return AppCode.UNKNOWN

    def child_context(self, **kwargs: Any) -> "AppState":
        """
        Create a fresh child AppState with the current IDs preserved as parent_*
        and the full ancestor chain carried forward.

        The `ancestors` list captures ALL currently active IDs (action, graph,
        team). Later the child's run_action_in_background / run_in_background
        will set the child's own IDs, which will be captured by the next
        child_context() call.
        """
        child = AppState()
        child.ancestors = list(self.ancestors)
        child._cancellation = self._cancellation
        child.source = self.source
        child.source_id = self.source_id

        if self.action_id:
            child.ancestors.append({
                "type": "action",
                "id": self.action_id,
                "run_id": self.action_run_id,
                "name": self.action_name,
                "run_name": self.action_run_name,
            })

        if self.action_graph_id:
            child.ancestors.append({
                "type": "action_graph",
                "id": self.action_graph_id,
                "result_id": self.action_graph_result_id,
                "node_id": self.action_graph_node_id,
            })

        if self.action_team_id:
            child.ancestors.append({
                "type": "action_team",
                "id": self.action_team_id,
                "result_id": self.action_team_result_id,
                "leader_action_id": self.action_team_leader_action_id,
            })

        child.parent_action_id = self.action_id
        child.parent_action_run_id = self.action_run_id
        child.parent_action_graph_id = self.action_graph_id
        child.parent_action_graph_result_id = self.action_graph_result_id

        # Apply any caller-supplied overrides
        for key, value in kwargs.items():
            if hasattr(child, key):
                setattr(child, key, value)
        return child
