from typing import Literal

import pytest
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from openai.types.chat import ChatCompletionUserMessageParam

from src.autobots.action.action.action_doc_model import ActionCreate, ActionDoc
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openai_v2 import (
    LlmChatOpenaiConfig,
)
from src.autobots.action_graph.action_graph.action_graph_doc_model import ActionGraphCreate
from src.autobots.action_graph.action_graph.action_group import ActionGroup
from src.autobots.action_graph.action_graph.user_action_graph import UserActionGraphs
from src.autobots.action_graph.action_graph_result.action_graph_result_model_doc import (
    ActionGraphResultDoc,
)
from src.autobots.action_graph.action_graph_result.user_action_graph_result import (
    UserActionGraphResult,
)
from src.autobots.api.v1.api_action_type.text2text.api_actions_text2text import (
    ActionCreateText2TextLlmChatOpenai,
)
from src.autobots.core.utils import gen_random_str
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_model import EventResultStatus, EventType


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["azure", "openai"])
async def test_user_action_group_run_happy_path(
    ctx, db, user, provider: Literal["azure", "openai"], set_test_settings
):
    rand: str = gen_random_str()

    user_actions = UserActions(user=user, db=db)
    user_action_graph = UserActionGraphs(user=user, db=db)
    user_action_graph_result = UserActionGraphResult(user, db)

    # create actions
    action_llm_director = await create_action_director(ctx, user_actions, db, rand, provider)
    # action_llm_marketing_manager = await create_action_marketing(ctx, user_actions, db, rand)
    action_llm_research_manager = await create_action_research(
        ctx, user_actions, db, rand, provider
    )
    action_llm_creative_manager = await create_action_creative(
        ctx, user_actions, db, rand, provider
    )
    # action_llm_twitter = await create_action_persona(ctx, user_actions, db, rand)
    # action_llm_slack = await create_action_persona(ctx, user_actions, db, rand)
    action_llm_web_search = await create_action_web_search(ctx, user_actions, db, rand, provider)
    action_llm_persona = await create_action_persona(ctx, user_actions, db, rand, provider)
    action_llm_creative = await create_action_creative(ctx, user_actions, db, rand, provider)
    action_llm_jingle = await create_action_jingle(ctx, user_actions, db, rand, provider)

    node_map = {
        "n1": str(action_llm_director.id),
        # "n2": str(action_llm_marketing_manager.id),
        "n3": str(action_llm_research_manager.id),
        "n4": str(action_llm_creative_manager.id),
        # "n5": str(action_llm_twitter.id), #
        # "n6": str(action_llm_slack.id),
        "n7": str(action_llm_web_search.id),  # n3
        "n9": str(action_llm_persona.id),  # n3
        "n10": str(action_llm_creative.id),  # n4
        "n11": str(action_llm_jingle.id),  # n4
    }

    action_graph_doc = None
    try:
        # create action graph
        action_graph = {
            "n1": ["n3", "n4"],
            "n3": ["n7", "n9"],
            "n4": ["n10", "n11"],
            # "n3": ["n4", "n5"]
        }

        # create action_graph
        action_graph_create = ActionGraphCreate(
            name="Marketing Dept", nodes=node_map, graph=action_graph
        )
        action_graph_doc = await user_action_graph.create(action_graph_create)

        # run action graph
        user_input = DataBlock(text="Campaign for Nike shoes during Diwali Festival")
        app_state = AppState()
        action_graph_result_doc = await ActionGroup.run_action_group(
            ctx=ctx,
            app_state=app_state,
            action_graph_input_dict=user_input.model_dump(),
            action_graph_result_doc=ActionGraphResultDoc(
                id="1",
                result=action_graph_doc,
                type=EventType.action_graph_run,
                status=EventResultStatus.processing,
                user_id=user.id,
            ),
            user_actions=user_actions,
            user_action_graph_result=user_action_graph_result,
        )
        logger.bind(ctx=ctx).info(f"Action graph result doc: {action_graph_result_doc}")
        assert action_graph_result_doc is not None
        logger.bind(ctx=ctx).info(
            f"Action graph result doc status: {action_graph_result_doc.status}"
        )
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
        logger.bind(ctx=ctx).error(f"Error running action graph: {str(e)}")
        raise e

    finally:
        # cleanup action graph
        if action_graph_doc:
            await user_action_graph_result.delete_action_graph_result(action_graph_doc.id)
            await user_action_graph.delete(action_graph_doc.id)

        # cleanup actions
        for node_id, action_id in node_map.items():
            await user_actions.delete_action(action_id)
        # await user_actions.delete_action(action_llm_director.id)
        # await user_actions.delete_action(action_llm_persona.id)
        # await user_actions.delete_action(action_llm_product.id)
        # await user_actions.delete_action(action_llm_creative.id)
        # await user_actions.delete_action(action_llm_jingle.id)


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
    app_state = AppState()
    action_doc = await user_actions.create_action(ctx, app_state, ActionCreate(**action_create.model_dump()))
    return action_doc


@pytest.mark.asyncio
async def create_action_web_search(
    ctx: Context,
    user_actions: UserActions,
    db: AsyncIOMotorDatabase,
    rand: str,
    provider: Literal["azure", "openai"],
) -> ActionDoc:
    action_create = ActionCreateText2TextLlmChatOpenai(
        name="persona_" + rand,
        description="Web search action",
        user_manual="Web search manual",
        config=LlmChatOpenaiConfig(
            provider=provider,
            messages=[ChatCompletionUserMessageParam(role="user", content="Web searcher")],
        ),
    )
    app_state = AppState()
    action_doc = await user_actions.create_action(ctx, app_state, ActionCreate(**action_create.model_dump()))
    return action_doc


@pytest.mark.asyncio
async def create_action_director(
    ctx: Context,
    user_actions: UserActions,
    db: AsyncIOMotorDatabase,
    rand: str,
    provider: Literal["azure", "openai"],
) -> ActionDoc:
    action_create = ActionCreateText2TextLlmChatOpenai(
        name="director_" + rand,
        description="Director action",
        user_manual="Director manual",
        config=LlmChatOpenaiConfig(
            provider=provider,
            messages=[
                ChatCompletionUserMessageParam(
                    role="user", content="You are director of the group."
                )
            ],
            # tools=["delegate"]
        ),
    )
    app_state = AppState()
    action_doc = await user_actions.create_action(ctx, app_state, ActionCreate(**action_create.model_dump()))
    return action_doc


@pytest.mark.asyncio
async def create_action_marketing(
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
    app_state = AppState()
    action_doc = await user_actions.create_action(ctx, app_state, ActionCreate(**action_create.model_dump()))
    return action_doc


@pytest.mark.asyncio
async def create_action_research(
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
    app_state = AppState()
    action_doc = await user_actions.create_action(ctx, app_state, ActionCreate(**action_create.model_dump()))
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
    app_state = AppState()
    action_doc = await user_actions.create_action(ctx, app_state, ActionCreate(**action_create.model_dump()))
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
    app_state = AppState()
    action_doc = await user_actions.create_action(ctx, app_state, ActionCreate(**action_create.model_dump()))
    return action_doc


# @pytest.mark.asyncio
# async def create_graph(user_graphs: UserGraphs, graph_map: Dict[str, List[str]], db: Session, rand: str = gen_random_str()) -> GraphORM:
#     user_graph_create_input = UserGraphCreateInput(
#         name="marketing dept_" + rand,
#         graph_map=graph_map
#     )
#     graph = await user_graphs.create(user_graph_create_input, db)
#     return graph
