import asyncio
from typing import Dict, List, Type

from loguru import logger
from openai.types.chat import ChatCompletionAssistantMessageParam, ChatCompletionUserMessageParam
from pydantic import BaseModel

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.abc.ActionABC import (
    ActionABC,
    ActionConfigCreateType,
    ActionConfigType,
    ActionConfigUpdateType,
    ActionInputType,
    ActionOutputType,
)
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openai_v2 import (
    ActionText2TextLlmChatOpenai,
    LlmChatOpenaiConfig,
)
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

class ActionLLMObjectiveConfig(BaseModel):
    objective_name: str
    objective: DataBlock


class ActionLLMObjectivesOpenaiConfig(BaseModel):
    chat_req: LlmChatOpenaiConfig
    objectives: List[ActionLLMObjectiveConfig]


class ActionLLMObjectivesOpenai(
    ActionABC[
        ActionLLMObjectivesOpenaiConfig,
        ActionLLMObjectivesOpenaiConfig,
        ActionLLMObjectivesOpenaiConfig,
        DataBlock,
        DataBlocks,
    ]
):
    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return ActionLLMObjectivesOpenaiConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return ActionLLMObjectivesOpenaiConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return ActionLLMObjectivesOpenaiConfig

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    async def ask_relevant_questions(self, ctx: Context, app_state: AppState, action_input: DataBlock) -> Dict[str, str]:
        questions = [
            "What are the constraints for achieving these objectives?",
            "Are there any specific tools or methods required?",
            "What criteria define success for each objective?",
            "Are there any known risks or challenges?",
        ]
        knowledge_base: Dict[str, str] = {}
        try:
            chat_req_copy = self.action_config.chat_req.model_copy(deep=True)

            # Add input context if available
            if action_input.get_llm_prompt(ctx):
                chat_req_copy.messages.append(
                    ChatCompletionUserMessageParam(
                        role="user", content=action_input.get_llm_prompt(ctx)
                    )
                )

            # Add objectives context
            objectives_context = "Objectives:\n" + "\n".join(
                [
                    f"- {obj.objective_name}: {obj.objective.get_llm_prompt(ctx)}"
                    for obj in self.action_config.objectives
                ]
            )
            chat_req_copy.messages.append(
                ChatCompletionUserMessageParam(role="user", content=objectives_context)
            )

            for question in questions:
                try:
                    chat_req_copy.messages.append(
                        ChatCompletionUserMessageParam(role="user", content=question)
                    )

                    llm_chat_action = ActionText2TextLlmChatOpenai(
                        action_config=chat_req_copy, user=self.user, db=self.db
                    )
                    results: DataBlocks = await llm_chat_action.run_action(ctx, app_state, DataBlock(text=""))
                    knowledge = ""
                    for block in results.blocks:
                        llm_prompt = block.get_llm_prompt(ctx)
                        knowledge += f"{llm_prompt}\n"

                    knowledge_base[question] = knowledge
                except Exception as e:
                    logger.bind(ctx=ctx, question=question, error=e).exception(
                        "Error in ask_relevant_questions"
                    )
        except Exception as e:
            logger.bind(ctx=ctx, error=e).exception("Error in ask_relevant_questions")

        return knowledge_base

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        # First, ask relevant questions
        # logger.bind(ctx=ctx).info("Asking relevant questions about objectives...")
        knowledge_base: Dict[str, str] = await self.ask_relevant_questions(ctx, app_state, action_input)

        remaining_objectives = self.action_config.objectives.copy()
        max_attempt_per_objective = 3
        results: List[DataBlock] = []

        # Add knowledge base insights to results
        knowledge_base_text = "Preliminary Analysis:\n" + "\n".join(
            [f"Q: {q}\nA: {a}" for q, a in knowledge_base.items()]
        )
        results.append(DataBlock(text=knowledge_base_text))

        while len(remaining_objectives) > 0:
            for objective in remaining_objectives[:]:
                evaluation = False
                for i in range(max_attempt_per_objective):
                    result: DataBlock = await self.reach_objective(
                        ctx=ctx,
                        app_state=app_state,
                        action_input=action_input,
                        objective=objective,
                        prev_results=results,
                    )

                    evaluation = await self.evaluate_objective(
                        ctx=ctx, app_state=app_state, result=result, objective=objective
                    )
                    if evaluation:
                        result.text = "".join(
                            [
                                f"Objective {objective.objective_name} reached successfully\n",
                                result.text,
                            ]
                        )
                        results.append(result)
                        remaining_objectives.remove(objective)
                        break
                    else:
                        logger.bind(ctx=ctx).info(
                            f"Objective {objective.objective_name} not reached yet. Attempt {i + 1}/{max_attempt_per_objective}"
                        )
                        await asyncio.sleep(10)
                if not evaluation:
                    logger.bind(ctx=ctx).error(
                        "Failed to reach objectives"
                    )
                    results.append(
                        DataBlock(text=f"Failed to reach objective {objective.objective_name}")
                    )
                    remaining_objectives.remove(objective)
        return DataBlocks(blocks=results)

    async def reach_objective(
        self,
        ctx: Context,
        app_state: AppState,
        action_input: DataBlock,
        objective: ActionLLMObjectiveConfig,
        prev_results: List[DataBlock] = [],
    ) -> DataBlock:
        chat_req_copy = self.action_config.chat_req.model_copy(deep=True)
        for result in prev_results:
            chat_req_copy.messages.append(
                ChatCompletionAssistantMessageParam(
                    role="assistant", content=result.get_llm_prompt(ctx)
                )
            )

        objective_prompt = "".join(["\nObjective:\n", objective.objective.get_llm_prompt(ctx)])
        chat_req_copy.messages.append(
            ChatCompletionUserMessageParam(role="user", content=objective_prompt)
        )

        llm_chat_action = ActionText2TextLlmChatOpenai(
            action_config=chat_req_copy, user=self.user, db=self.db
        )
        output: DataBlocks = await llm_chat_action.run_action(ctx=ctx, app_state=app_state, action_input=action_input)
        result = DataBlock()
        for block in output.blocks:
            result.text += block.text + "\n"
            result.urls += block.urls
        return result

    async def evaluate_objective(
        self, ctx: Context, app_state: AppState, result: DataBlock, objective: ActionLLMObjectiveConfig
    ) -> bool:
        eval_chat_req = self.action_config.chat_req.model_copy(deep=True)

        eval_prompt = (
            f"Evaluate if the following result satisfies the objective. Answer with 'yes' or 'no' and explain why.\n\n"
            f"Objective: {objective.objective.get_llm_prompt(ctx)}\n"
            f"Result: {result.get_llm_prompt(ctx)}\n"
        )

        eval_chat_req.messages.append(
            ChatCompletionUserMessageParam(role="user", content=eval_prompt)
        )

        llm_chat_action = ActionText2TextLlmChatOpenai(
            action_config=eval_chat_req, user=self.user, db=self.db
        )
        evaluations = await llm_chat_action.run_action(ctx, app_state, DataBlock(text=""))
        evaluation = evaluations.blocks[0]
        return "yes" in evaluation.text.lower()

    async def run_tool(
        self, action_config: ActionConfigType, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> ActionOutputType | Exception:
        pass
