import os
import uuid
from io import BytesIO
from typing import List

import pandas as pd
import pytest
from motor.motor_asyncio import AsyncIOMotorDatabase
from openai.types.chat import ChatCompletionUserMessageParam

from src.autobots.action.action.action_doc_model import ActionCreate, ActionDoc
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action_graph.action_graph.action_graph_doc_model import ActionGraphCreate
from src.autobots.action_graph.action_graph.user_action_graph import UserActionGraphs
from src.autobots.action_graph.action_graph_result.user_action_graph_result import (
    UserActionGraphResult,
)
from src.autobots.api.v1.api_action_type.text2text.api_actions_text2text import (
    ActionCreateText2TextLlmChatOpenai,
)
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.conn.openai.openai_chat.chat_model import ChatReq
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.settings import SettingsProvider
from src.autobots.core.utils import gen_random_str
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.user.user_orm_model import UserORM


async def create_test_excel_with_image_urls(
    image_campaigns: List[str], settings
) -> tuple[str, str]:
    """Create a test Excel file with image URLs in the first column using Unsplash images"""
    local_excel_path = "test_campaign_images.xlsx"
    try:
        # Use Unsplash URLs for sports/festival related images
        image_urls = [
            "https://images.unsplash.com/photo-1542291026-7eec264c27ff?w=800",  # Nike shoe
            "https://images.unsplash.com/photo-1600185365483-26d7a4cc7519?w=800",  # Festival lights
        ]

        # Create DataFrame with image URLs
        df = pd.DataFrame({"Image URL": image_urls, "Campaign": image_campaigns})

        # Save to Excel file locally first
        df.to_excel(local_excel_path, index=False)

        # Read the file into BytesIO
        with open(local_excel_path, "rb") as f:
            file_data = BytesIO(f.read())

        # Upload to S3 and get public URL
        s3_url = await get_public_s3().put_file_obj(file_data, "test_campaign_images.xlsx")

        return local_excel_path, s3_url
    except Exception:
        if os.path.exists(local_excel_path):
            os.remove(local_excel_path)
        raise  # Re-raise the exception


async def create_test_excel_with_data(image_campaigns: List[str], settings) -> tuple[str, str]:
    """Create a test Excel file with image URLs in the first column using Unsplash images"""
    local_excel_path = "test_campaign_images.xlsx"
    try:
        # Use Unsplash URLs for sports/festival related images
        # image_urls = [
        #     "https://images.unsplash.com/photo-1542291026-7eec264c27ff?w=800",  # Nike shoe
        #     "https://images.unsplash.com/photo-1600185365483-26d7a4cc7519?w=800"  # Festival lights
        # ]
        data_instructions = ["Give 3 suggestions", "Give 5 suggestions"]

        # Create DataFrame with image URLs
        df = pd.DataFrame({"suggestions": data_instructions, "Campaign": image_campaigns})

        # Save to Excel file locally first
        df.to_excel(local_excel_path, index=False)

        # Read the file into BytesIO
        with open(local_excel_path, "rb") as f:
            file_data = BytesIO(f.read())

        # Upload to S3 and get public URL
        s3_url = await get_public_s3().put_file_obj(file_data, "test_campaign_images.xlsx")

        return local_excel_path, s3_url
    except Exception:
        if os.path.exists(local_excel_path):
            os.remove(local_excel_path)
        raise  # Re-raise the exception


@pytest.mark.asyncio
@pytest.mark.skip(reason="Expensive to test everytime")
async def test_user_graph_run_happy_path(set_test_settings):
    ctx = Context()
    rand: str = gen_random_str()
    settings = SettingsProvider.sget()
    local_excel_path = None  # Initialize the variable

    user_id = uuid.UUID("4d5d5063-36fb-422e-a811-cac8c2003d38")
    user = UserORM(id=user_id)

    db = next(get_mongo_db())

    user_actions = UserActions(user=user, db=db)
    user_action_graph = UserActionGraphs(user=user, db=db)
    user_action_graph_result = UserActionGraphResult(user, db)

    # create actions
    action_llm_persona = await create_action_persona(ctx, user_actions, db, rand)
    action_llm_manager = await create_action_manager(ctx, user_actions, db, rand)
    action_llm_product = await create_action_product(ctx, user_actions, db, rand)
    action_llm_creative = await create_action_creative(ctx, user_actions, db, rand)
    action_llm_jingle = await create_action_jingle(ctx, user_actions, db, rand)

    try:
        node_map = {
            "n1": str(action_llm_manager.id),
            "n2": str(action_llm_persona.id),
            "n3": str(action_llm_product.id),
            "n4": str(action_llm_creative.id),
            "n5": str(action_llm_jingle.id),
        }
        node_details = {
            "n1": {
                "id": str(action_llm_manager.id),
                "position": {"x": 280, "y": 113},
                "type": "default",
                "data": {
                    "label": "Manager",
                    "actionId": "",
                    "is_loop": True,
                    "excel_loop": True,
                    "file_column_index": 0,
                    "instruction_column_index": 1,
                    "file_processing": True,
                },
            },
            "n2": {
                "id": str(action_llm_persona.id),
                "position": {"x": 280, "y": 113},
                "type": "default",
                "data": {"label": "Persona", "actionId": "", "is_loop": True},
            },
            "n3": {
                "id": str(action_llm_product.id),
                "position": {"x": 280, "y": 113},
                "type": "default",
                "data": {"label": "Product", "actionId": "", "is_loop": True},
            },
            "n4": {
                "id": str(action_llm_creative.id),
                "position": {"x": 280, "y": 113},
                "type": "default",
                "data": {"label": "Creative", "actionId": "", "is_loop": True},
            },
            "n5": {
                "id": str(action_llm_jingle.id),
                "position": {"x": 280, "y": 113},
                "type": "default",
                "data": {"label": "Jingle", "actionId": "", "is_loop": True},
            },
        }
        # create action graph
        action_graph = {"n1": ["n2"], "n2": ["n3"], "n3": ["n4"], "n4": ["n5"]}

        # create action_graph
        action_graph_create = ActionGraphCreate(
            name="Marketing Dept", nodes=node_map, graph=action_graph, node_details=node_details
        )
        action_graph_doc = await user_action_graph.create(action_graph_create)

        # Create test campaigns
        test_campaigns = [
            "Campaign for Nike shoes during Diwali Festival",
            "Campaign for Nike shoes during Holi Festival",
        ]

        # Create test Excel file and upload to S3
        local_excel_path, s3_url = await create_test_excel_with_image_urls(test_campaigns, settings)

        # Modify the user input to include the S3 URL of the Excel file
        user_input = DataBlock(
            text="Explain the images",
            urls=[str(s3_url)],  # Use S3 URL instead of local path
        )
        app_state = AppState()
        action_graph_result_doc = await user_action_graph.run_in_background(
            ctx, app_state, user_actions, user_action_graph_result, action_graph_doc.id, user_input, None
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

        # cleanup action graph
        await user_action_graph_result.delete_action_graph_result(action_graph_doc.id)
        await user_action_graph.delete(action_graph_doc.id)

    except Exception as e:
        assert e is not None
        raise  # Re-raise the exception to see the actual error

    finally:
        # Cleanup the local test Excel file
        if local_excel_path and os.path.exists(local_excel_path):
            os.remove(local_excel_path)

        # cleanup actions
        await user_actions.delete_action(action_llm_manager.id)
        await user_actions.delete_action(action_llm_persona.id)
        await user_actions.delete_action(action_llm_product.id)
        await user_actions.delete_action(action_llm_creative.id)
        await user_actions.delete_action(action_llm_jingle.id)


@pytest.mark.asyncio
@pytest.mark.skip(reason="Expensive to test everytime")
async def test_user_graph_run_happy_path_v2(set_test_settings):
    ctx = Context()
    rand: str = gen_random_str()
    settings = SettingsProvider.sget()
    local_excel_path = None  # Initialize the variable

    user_id = uuid.UUID("4d5d5063-36fb-422e-a811-cac8c2003d38")
    user = UserORM(id=user_id)

    db = next(get_mongo_db())

    user_actions = UserActions(user=user, db=db)
    user_action_graph = UserActionGraphs(user=user, db=db)
    user_action_graph_result = UserActionGraphResult(user, db)

    # create actions
    action_llm_persona = await create_action_persona(ctx, user_actions, db, rand)
    action_llm_manager = await create_action_manager(ctx, user_actions, db, rand)
    action_llm_product = await create_action_product(ctx, user_actions, db, rand)
    action_llm_creative = await create_action_creative(ctx, user_actions, db, rand)
    action_llm_jingle = await create_action_jingle(ctx, user_actions, db, rand)

    try:
        node_map = {
            "n1": str(action_llm_manager.id),
            "n2": str(action_llm_persona.id),
            "n3": str(action_llm_product.id),
            "n4": str(action_llm_creative.id),
            "n5": str(action_llm_jingle.id),
        }
        node_details = {
            "n1": {
                "id": str(action_llm_manager.id),
                "position": {"x": 280, "y": 113},
                "type": "default",
                "data": {
                    "label": "Manager",
                    "actionId": "",
                    "is_loop": True,
                    "excel_loop": True,
                    "data_processing": True,
                },
            },
            "n2": {
                "id": str(action_llm_persona.id),
                "position": {"x": 280, "y": 113},
                "type": "default",
                "data": {"label": "Persona", "actionId": "", "is_loop": True},
            },
            "n3": {
                "id": str(action_llm_product.id),
                "position": {"x": 280, "y": 113},
                "type": "default",
                "data": {"label": "Product", "actionId": "", "is_loop": True},
            },
            "n4": {
                "id": str(action_llm_creative.id),
                "position": {"x": 280, "y": 113},
                "type": "default",
                "data": {"label": "Creative", "actionId": "", "is_loop": True},
            },
            "n5": {
                "id": str(action_llm_jingle.id),
                "position": {"x": 280, "y": 113},
                "type": "default",
                "data": {"label": "Jingle", "actionId": "", "is_loop": True},
            },
        }
        # create action graph
        action_graph = {"n1": ["n2"], "n2": ["n3"], "n3": ["n4"], "n4": ["n5"]}

        # create action_graph
        action_graph_create = ActionGraphCreate(
            name="Marketing Dept", nodes=node_map, graph=action_graph, node_details=node_details
        )
        action_graph_doc = await user_action_graph.create(action_graph_create)

        # Create test campaigns
        test_campaigns = [
            "Campaign for Nike shoes during Diwali Festival",
            "Campaign for Nike shoes during Holi Festival",
        ]

        # Create test Excel file and upload to S3
        local_excel_path, s3_url = await create_test_excel_with_data(test_campaigns, settings)

        # Modify the user input to include the S3 URL of the Excel file
        user_input = DataBlock(
            text="Explain the images",
            urls=[str(s3_url)],  # Use S3 URL instead of local path
        )
        app_state = AppState()
        action_graph_result_doc = await user_action_graph.run_in_background(
            ctx, app_state, user_actions, user_action_graph_result, action_graph_doc.id, user_input, None
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

        # cleanup action graph
        await user_action_graph_result.delete_action_graph_result(action_graph_doc.id)
        await user_action_graph.delete(action_graph_doc.id)

    except Exception as e:
        assert e is not None
        raise  # Re-raise the exception to see the actual error

    finally:
        # Cleanup the local test Excel file
        if local_excel_path and os.path.exists(local_excel_path):
            os.remove(local_excel_path)

        # cleanup actions
        await user_actions.delete_action(action_llm_manager.id)
        await user_actions.delete_action(action_llm_persona.id)
        await user_actions.delete_action(action_llm_product.id)
        await user_actions.delete_action(action_llm_creative.id)
        await user_actions.delete_action(action_llm_jingle.id)


@pytest.mark.asyncio
async def create_action_persona(
    ctx: Context, user_actions: UserActions, db: AsyncIOMotorDatabase, rand: str
) -> ActionDoc:
    action_create = ActionCreateText2TextLlmChatOpenai(
        name="persona_" + rand,
        config=ChatReq(
            messages=[
                ChatCompletionUserMessageParam(
                    role="user", content="Generate personas for Marketing this product"
                )
            ]
        ),
    )
    action_doc = await user_actions.create_action(ctx, ActionCreate(**action_create.model_dump()))
    return action_doc


@pytest.mark.asyncio
async def create_action_manager(
    ctx: Context, user_actions: UserActions, db: AsyncIOMotorDatabase, rand: str
) -> ActionDoc:
    action_create = ActionCreateText2TextLlmChatOpenai(
        name="manager_" + rand,
        config=ChatReq(
            messages=[
                ChatCompletionUserMessageParam(
                    role="user", content="Act as market manager, create input for department"
                )
            ]
        ),
    )
    action_doc = await user_actions.create_action(ctx, ActionCreate(**action_create.model_dump()))
    return action_doc


@pytest.mark.asyncio
async def create_action_product(
    ctx: Context, user_actions: UserActions, db: AsyncIOMotorDatabase, rand: str
) -> ActionDoc:
    action_create = ActionCreateText2TextLlmChatOpenai(
        name="market researcher_" + rand,
        config=ChatReq(
            messages=[
                ChatCompletionUserMessageParam(
                    role="user",
                    content="Act as product researcher, create research report for the product",
                )
            ]
        ),
    )
    action_doc = await user_actions.create_action(ctx, ActionCreate(**action_create.model_dump()))
    return action_doc


@pytest.mark.asyncio
async def create_action_creative(
    ctx: Context, user_actions: UserActions, db: AsyncIOMotorDatabase, rand: str = gen_random_str()
) -> ActionDoc:
    action_create = ActionCreateText2TextLlmChatOpenai(
        name="creative_" + rand,
        config=ChatReq(
            messages=[
                ChatCompletionUserMessageParam(
                    role="user", content="Act as a creative editor, generate text creative"
                )
            ]
        ),
    )
    action_doc = await user_actions.create_action(ctx, ActionCreate(**action_create.model_dump()))
    return action_doc


@pytest.mark.asyncio
async def create_action_jingle(
    ctx: Context, user_actions: UserActions, db: AsyncIOMotorDatabase, rand: str = gen_random_str()
) -> ActionDoc:
    action_create = ActionCreateText2TextLlmChatOpenai(
        name="jingle_" + rand,
        config=ChatReq(
            messages=[
                ChatCompletionUserMessageParam(
                    role="user", content="Act as a creative editor, generate jingle for marketing"
                )
            ]
        ),
    )
    action_doc = await user_actions.create_action(ctx, ActionCreate(**action_create.model_dump()))
    return action_doc
