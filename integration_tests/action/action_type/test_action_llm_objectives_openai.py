from typing import Literal

import pytest

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.action_factory import ActionFactory, RunActionObj
from src.autobots.action.action_type.action_llm.action_llm_objectives.action_llm_objectives_openai import (
    ActionLLMObjectiveConfig,
    ActionLLMObjectivesOpenaiConfig,
)
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openai_v2 import (
    LlmChatOpenaiConfig,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.data_model.app_state import AppState


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["azure", "openai"])
async def test_action_llm_objectives_openai_happy_path(
    ctx, db, user, provider: Literal["azure", "openai"], set_test_settings
):
    action_config = ActionLLMObjectivesOpenaiConfig(
        chat_req=LlmChatOpenaiConfig(
            provider=provider,
            model="o4-mini",
            messages=[
                {
                    "role": "system",
                    "content": "You are an AI assistant that helps users to complete objectives.",
                },
            ],
        ),
        objectives=[
            ActionLLMObjectiveConfig(
                objective_name="first",
                objective=DataBlock(text="Generate a algorithm for large datasets."),
            ),
            ActionLLMObjectiveConfig(
                objective_name="second",
                objective=DataBlock(text="Implement a Python function for that algorithm."),
            ),
            ActionLLMObjectiveConfig(
                objective_name="third",
                objective=DataBlock(text="Optimize the function for efficiency."),
            ),
        ],
    )

    """
    # action_config as json
    {
        "chat_req": {
            "provider": "openai",
            "messages": [
                {
                    "role": "system",
                    "content": "You are an AI assistant that helps users to complete objectives."
                }
            ]
        },
        "objectives": [
            {
                "objective_name": "first",
                "objective": {
                    "text": "Generate a algorithm for large datasets."
                }
            },
            {
                "objective_name": "second",
                "objective": {
                    "text": "Implement a Python function for that algorithm."
                }
            },
            {
                "objective_name": "third",
                "objective": {
                    "text": "Optimize the function for efficiency."
                }
            }
        ]
    }
    """

    action_doc = ActionDoc(
        id="action_doc_1",
        user_id=user.id,
        name="action_1",
        type=ActionType.llm_objectives_openai,
        config=action_config.model_dump(exclude_none=True),
    )
    action_input = DataBlock(text="Generate Sorting algorithm")
    app_state = AppState()
    action_run_obj: RunActionObj | Exception = await ActionFactory.run_action(
        ctx=ctx,
        app_state=app_state,
        action_doc=action_doc,
        action_input_dict=action_input.model_dump(exclude_none=True),
        user=user,
        db=db,
    )

    output = DataBlocks.model_validate(action_run_obj.output_dict)
    assert output.blocks[-1].text.lower().__contains__("sort")

    # action = ActionLLMObjectivesOpenai(action_config=action_config)

    # output = await action.run_action(ctx=Context(), action_input=DataBlock(text="Generate Sorting algorithm"))

    # assert output.blocks[-1].text.lower().__contains__("sort")
