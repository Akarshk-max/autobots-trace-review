import json
from typing import List

from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action.user_actions import UserActions
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState

class AgentChatConfig(BaseModel):
    node_id: str | None = None
    agent_id: str
    message: str


class AgentChatsConfig(BaseModel):
    delegates: List[AgentChatConfig]


class AgentsChat:
    @staticmethod
    async def delegate(
        config: AgentChatConfig,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
    ) -> str:
        """
        Description
        -----------
        Executes a single delegated agent interaction. The function takes a
        target agent identifier and a user message, converts the message into a
        `DataBlock`, and invokes `run_action_v1` on the corresponding agent
        action. The raw action output is serialized to JSON and returned as a
        string. If the agent run fails, the exception or error bubble-up occurs
        through the underlying action runner.

        Input
        -----
        config : dict
            Expected structure:
                {
                    "node_id": "<optional graph node id>",
                    "agent_id": "<id of the agent action to run>",
                    "message": "<message to send to the agent>"
                }

            node_id : str | None
                Optional node identifier if the agent call is part of a graph
                or larger workflow. Not required for standalone delegation.

            agent_id : str
                Identifier of the agent action to invoke.

            message : str
                User-provided text message that becomes the `DataBlock.text`
                passed to the agent.

        Output
        ------
        str
            JSON-encoded representation of the agent action's output. The
            structure depends on the action's own return format, but is
            typically equivalent to:
                {
                    "output": ...,
                    "status": ...,
                    ...
                }

            Any exceptions or failure states are surfaced via the underlying
            user action runner before JSON serialization.
        """
        # user_actions = UserActions(user=user, db=next(get_mongo_db()))
        user_actions = UserActions(user=user, db=db)
        input = DataBlock(text=config.message)
        action_doc = await user_actions.run_action_v1(
            ctx=ctx,
            app_state=app_state,
            action_id=config.agent_id,
            input=input.model_dump(exclude_none=True),
            notify=notify,
        )
        output = action_doc.output
        out_str = json.dumps(output)
        return out_str
        #
        # config_str = config.model_dump_json(exclude_none=True)
        # return config_str
