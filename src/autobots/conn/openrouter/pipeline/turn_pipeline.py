from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.conn.openrouter.chat_model import OpenRouterChatReq
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.mcp.mcp_client import MCPToolBinding
from src.autobots.server_sent_events.server_sent_events import Notify


@dataclass
class TurnContext:
    action: Any
    ctx: Context
    app_state: AppState
    action_input: DataBlock
    notify: Notify | None = None

    current_turn: int = 1
    turn_start_index: int = -1
    previous_tool_results: list[dict[str, Any]] = field(default_factory=list)
    tool_defs: list[Any] | None = None
    mcp_tool_bindings: dict[str, MCPToolBinding] = field(default_factory=dict)
    chat_req: OpenRouterChatReq | None = None
    assistant_texts: list[str] = field(default_factory=list)
    data_blocks: DataBlocks = field(default_factory=lambda: DataBlocks(blocks=[]))


class TurnStage(ABC):
    name: ClassVar[str]

    @abstractmethod
    async def run(self, turn: TurnContext) -> None:
        raise NotImplementedError


class OpenRouterTurnPipeline:
    def __init__(self, stages: list[TurnStage] | None = None):
        self.stages = stages if stages is not None else default_stages()

    async def run(self, turn: TurnContext) -> DataBlocks:
        for stage in self.stages:
            await stage.run(turn)
        return turn.data_blocks


def default_stages() -> list[TurnStage]:
    from src.autobots.conn.openrouter.pipeline.stages import (
        BuildRequestStage,
        ExecuteStage,
        HistoryIndexStage,
        McpResolveStage,
        ModelCapabilityStage,
        PersistStage,
        ReasoningStripStage,
        StateInitStage,
        ToolResolutionStage,
        UserInputStage,
        VectorStoreStage,
        WindowRebuildStage,
    )

    return [
        StateInitStage(),
        ModelCapabilityStage(),
        ReasoningStripStage(),
        WindowRebuildStage(),
        UserInputStage(),
        ToolResolutionStage(),
        VectorStoreStage(),
        McpResolveStage(),
        BuildRequestStage(),
        ExecuteStage(),
        PersistStage(),
        HistoryIndexStage(),
    ]
