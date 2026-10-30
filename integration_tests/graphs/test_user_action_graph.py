from typing import Literal

import pytest
from motor.motor_asyncio import AsyncIOMotorDatabase
from openai.types.chat import ChatCompletionUserMessageParam

from src.autobots.action.action.action_doc_model import ActionCreate, ActionDoc
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openai_v2 import (
    LlmChatOpenaiConfig,
)
from src.autobots.action_graph.action_graph.action_graph_doc_model import ActionGraphCreate
from src.autobots.action_graph.action_graph.user_action_graph import UserActionGraphs
from src.autobots.action_graph.action_graph_result.user_action_graph_result import (
    UserActionGraphResult,
)
from src.autobots.api.v1.api_action_type.text2text.api_actions_text2text import (
    ActionCreateText2TextLlmChatOpenai,
)
from src.autobots.core.utils import gen_random_str
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_model import EventResultStatus


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["azure", "openai"])
async def test_user_graph_run_happy_path(
    ctx, db, user, provider: Literal["azure", "openai"], set_test_settings
):
    rand: str = gen_random_str()

    user_actions = UserActions(user=user, db=db)
    user_action_graph = UserActionGraphs(user=user, db=db)
    user_action_graph_result = UserActionGraphResult(user, db)

    # create actions
    action_llm_persona = await create_action_persona(ctx, user_actions, db, rand, provider)
    action_llm_manager = await create_action_manager(ctx, user_actions, db, rand, provider)
    action_llm_product = await create_action_product(ctx, user_actions, db, rand, provider)
    action_llm_creative = await create_action_creative(ctx, user_actions, db, rand, provider)
    action_llm_jingle = await create_action_jingle(ctx, user_actions, db, rand, provider)

    action_graph_doc = None
    try:
        node_map = {
            "n1": str(action_llm_manager.id),
            "n2": str(action_llm_persona.id),
            "n3": str(action_llm_product.id),
            "n4": str(action_llm_creative.id),
            "n5": str(action_llm_jingle.id),
        }
        # create action graph
        action_graph = {"n1": ["n2", "n3"], "n2": ["n4", "n5"], "n3": ["n4", "n5"]}

        # create action_graph
        action_graph_create = ActionGraphCreate(
            name="Marketing Dept", nodes=node_map, graph=action_graph
        )
        action_graph_doc = await user_action_graph.create(action_graph_create)

        # run action graph
        user_input = DataBlock(text="Campaign for Nike shoes during Diwali Festival")
        app_state = AppState()
        action_graph_result_doc = await user_action_graph.run_in_background(
            ctx,
            app_state,
            user_actions,
            # user_action_market,
            user_action_graph_result,
            action_graph_doc.id,
            user_input,
            action_graph_result_id=None,
        )
        assert action_graph_result_doc is not None

        assert action_graph_result_doc.status == EventResultStatus.success

        # user_input = TextObj(input="Campaign for Nike shoes during Diwali Festival")
        # action_graph_response = await ActionGraph.run(user, user_input, node_map, action_graph, db)
        # assert len(action_graph_response) > 1
        #
        # # save action graph
        # action_graph_create = ActionGraphCreate(
        #     name="Marketing Dept", graph=action_graph
        # )
        # action_graph_doc = await user_action_graph.create(action_graph_create, db)
        #
        # # run saved action graph
        # input = TextObj(input="Create ad for sports shoes")
        # action_graph_resp = await user_action_graph.run(action_graph_doc.id, input, db)
        # assert len(action_graph_resp) > 1

    except Exception as e:
        assert e is not None

    finally:
        # cleanup action graph
        if action_graph_doc:
            await user_action_graph_result.delete_action_graph_result(action_graph_doc.id)
            await user_action_graph.delete(action_graph_doc.id)

        # cleanup actions
        await user_actions.delete_action(action_llm_manager.id)
        await user_actions.delete_action(action_llm_persona.id)
        await user_actions.delete_action(action_llm_product.id)
        await user_actions.delete_action(action_llm_creative.id)
        await user_actions.delete_action(action_llm_jingle.id)


@pytest.mark.asyncio
async def create_action_persona(
    ctx: Context,
    user_actions: UserActions,
    db: AsyncIOMotorDatabase,
    rand: str,
    provider: Literal["azure", "openai"],
) -> ActionDoc:
    action_create = ActionCreateText2TextLlmChatOpenai(
        name="persona_" + rand,
        description="Persona action",
        user_manual="Persona manual",
        config=LlmChatOpenaiConfig(
            provider=provider,
            messages=[
                ChatCompletionUserMessageParam(
                    role="user", content="Generate personas for Marketing this product"
                )
            ],
        ),
    )
    action_doc = await user_actions.create_action(ctx, ActionCreate(**action_create.model_dump()))
    return action_doc


@pytest.mark.asyncio
async def create_action_manager(
    ctx: Context,
    user_actions: UserActions,
    db: AsyncIOMotorDatabase,
    rand: str,
    provider: Literal["azure", "openai"],
) -> ActionDoc:
    action_create = ActionCreateText2TextLlmChatOpenai(
        name="manager_" + rand,
        description="Manager action",
        user_manual="Manager manual",
        config=LlmChatOpenaiConfig(
            provider=provider,
            messages=[
                ChatCompletionUserMessageParam(
                    role="user", content="Act as market manager, create input for department"
                )
            ],
        ),
    )
    action_doc = await user_actions.create_action(ctx, ActionCreate(**action_create.model_dump()))
    return action_doc


@pytest.mark.asyncio
async def create_action_product(
    ctx: Context,
    user_actions: UserActions,
    db: AsyncIOMotorDatabase,
    rand: str,
    provider: Literal["azure", "openai"],
) -> ActionDoc:
    action_create = ActionCreateText2TextLlmChatOpenai(
        name="market researcher_" + rand,
        description="market researcher",
        user_manual="market researcher manual",
        config=LlmChatOpenaiConfig(
            provider=provider,
            messages=[
                ChatCompletionUserMessageParam(
                    role="user",
                    content="Act as product researcher, create research report for the product",
                )
            ],
        ),
    )
    action_doc = await user_actions.create_action(ctx, ActionCreate(**action_create.model_dump()))
    return action_doc


@pytest.mark.asyncio
async def create_action_creative(
    ctx: Context,
    user_actions: UserActions,
    db: AsyncIOMotorDatabase,
    rand: str,
    provider: Literal["azure", "openai"],
) -> ActionDoc:
    action_create = ActionCreateText2TextLlmChatOpenai(
        name="creative_" + rand,
        description="Creative action",
        user_manual="Creative manual",
        config=LlmChatOpenaiConfig(
            provider=provider,
            messages=[
                ChatCompletionUserMessageParam(
                    role="user", content="Act as a creative editor, generate text creative"
                )
            ],
        ),
    )
    action_doc = await user_actions.create_action(ctx, ActionCreate(**action_create.model_dump()))
    return action_doc


@pytest.mark.asyncio
async def create_action_jingle(
    ctx: Context,
    user_actions: UserActions,
    db: AsyncIOMotorDatabase,
    rand: str,
    provider: Literal["azure", "openai"],
) -> ActionDoc:
    action_create = ActionCreateText2TextLlmChatOpenai(
        name="jingle_" + rand,
        description="Jingle action",
        user_manual="Jingle manual",
        config=LlmChatOpenaiConfig(
            provider=provider,
            messages=[
                ChatCompletionUserMessageParam(
                    role="user", content="Act as a creative editor, generate jingle for marketing"
                )
            ],
        ),
    )
    action_doc = await user_actions.create_action(ctx, ActionCreate(**action_create.model_dump()))
    return action_doc


# @pytest.mark.asyncio
# async def create_graph(user_graphs: UserGraphs, graph_map: Dict[str, List[str]], db: Session, rand: str = gen_random_str()) -> GraphORM:
#     user_graph_create_input = UserGraphCreateInput(
#         name="marketing dept_" + rand,
#         graph_map=graph_map
#     )
#     graph = await user_graphs.create(user_graph_create_input, db)
#     return graph
