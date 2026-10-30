# from typing import Any, List, Literal

# import supabase_auth
# from fastapi import APIRouter, Depends, Request
# from motor.motor_asyncio import AsyncIOMotorDatabase

# from src.autobots.action.action.common_action_models import TextObj
# from src.autobots.agent.one_step import AgentData, OneStepAgent
# from src.autobots.agent.reason_act_observe import ReasonActObserve
# from src.autobots.integrations.app_conn.app_conn import AppConn
# from src.autobots.auth.security import get_online_user_from_access_token
# from src.autobots.core.database.mongo_base import get_mongo_db
# from src.autobots.user.user_orm_model import UserORM

# router = APIRouter()


# @router.post("/react")
# async def run_react_agent(
#     request: Request,
#     input: TextObj,
#     openai_provider: Literal["openai", "azure"],
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> List[
#     Any
# ]:  # List[ChatCompletionSystemMessageParam | ChatCompletionUserMessageParam | ChatCompletionAssistantMessageParam | ChatCompletionToolMessageParam | ChatCompletionFunctionMessageParam]:
#     ctx = request.state.context
#     user = UserORM(**user_res.model_dump())
#     client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=openai_provider)
#     messages = await ReasonActObserve(client=client, openai_provider=openai_provider).do_task(
#         ctx, input.text
#     )
#     return messages


# # TODO: Make Selenium work, currently unable to install chromedriver in image
# @router.post("/act")
# async def run_act_agent(
#     request: Request,
#     agent_data: AgentData,
#     # openai_provider: Literal["openai", "azure"],
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> AgentData:
#     ctx = request.state.context
#     user = UserORM(**user_res.model_dump())
#     # client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=openai_provider)
#     await OneStepAgent().run(ctx=ctx, db=db, user=user, agent_data=agent_data, loops_allowed=5)
#     return agent_data
