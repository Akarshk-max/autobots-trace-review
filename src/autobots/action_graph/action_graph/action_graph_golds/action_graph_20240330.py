import copy
import json
from typing import Any, Dict, List, Optional, Set

from fastapi import BackgroundTasks, HTTPException
from loguru import logger
from openai.types.chat import ChatCompletionUserMessageParam
from pydantic import HttpUrl
from tenacity import retry, stop_after_attempt, wait_fixed

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks, TextObj
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action.action_type.action_map import ACTION_MAP
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openai_v2 import (
    ActionText2TextLlmChatOpenai,
    LlmChatOpenaiConfig,
)
from src.autobots.action_graph.action_graph.action_graph_doc_model import ActionGraphDoc
from src.autobots.action_graph.action_graph_result.action_graph_result_model_doc import (
    ActionGraphResultCreate,
    ActionGraphResultDoc,
    ActionGraphResultUpdate,
)
from src.autobots.action_graph.action_graph_result.user_action_graph_result import (
    UserActionGraphResult,
)
from src.autobots.api.webhook import Webhook
from src.autobots.conn.excel.read_and_split import split_excel_into_batches
from src.autobots.core.clock.clock import Clock
from src.autobots.core.logging.app_code import AppCode
from src.autobots.core.profiler.profiler import Profiler
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.user.user_orm_model import UserORM


class ActionGraph:
    @staticmethod
    async def run_in_background(
        ctx: Context,
        action_graph_doc: ActionGraphDoc,
        action_graph_input_dict: Dict[str, Any],
        user_actions: UserActions,
        user_action_graph_result: UserActionGraphResult,
        action_graph_result_id: Optional[str] = None,
        action_graph_node_id: Optional[str] = None,
        is_rerun_graph: bool = False,
        background_tasks: BackgroundTasks = None,
        webhook: Webhook | None = None,
    ) -> ActionGraphResultDoc | None:
        logger.bind(
            ctx=ctx,
            user_id=action_graph_doc.user_id,
            action_graph_id=action_graph_doc.id,
            action_graph_name=action_graph_doc.name,
            action_graph_version=action_graph_doc.version,
        ).info("Action Graph Run started")
        action_graph_result_doc: ActionGraphResultDoc | None = None
        try:
            if not action_graph_result_id:
                logger.bind(ctx=ctx).info("Creating Action Graph Run")
                # Create initial Action Graph Result if not provided
                action_graph_doc.input = action_graph_input_dict
                action_graph_doc.output = {}
                action_graph_result_create: ActionGraphResultCreate = ActionGraphResultCreate(
                    status=EventResultStatus.processing, result=action_graph_doc, is_saved=False
                )
                action_graph_result_doc = await user_action_graph_result.create_action_graph_result(
                    ctx=ctx, action_graph_result=action_graph_result_create
                )
                logger.bind(ctx=ctx, action_graph_result_doc=action_graph_result_doc).info(
                    "Created Action Graph Run"
                )
                if webhook:
                    await webhook.send(action_graph_result_doc.model_dump())
            else:
                logger.bind(ctx=ctx).info("Find and use Action Graph Result")
                # find and use Action Graph Result
                action_graph_result_doc = await user_action_graph_result.get_action_graph_result(
                    action_graph_result_id
                )
                # Change status back to processing
                logger.bind(ctx=ctx).info("Updating status of Action Graph Result to processing")
                action_graph_result_update: ActionGraphResultUpdate = ActionGraphResultUpdate(
                    status=EventResultStatus.processing
                )
                action_graph_result_doc = await user_action_graph_result.update_action_graph_result(
                    ctx, action_graph_result_id, action_graph_result_update
                )

            if background_tasks:
                logger.bind(ctx=ctx).info("Starting background tasks")
                background_tasks.add_task(
                    ActionGraph._run_as_background_task,
                    ctx,
                    user_actions,
                    action_graph_input_dict,
                    action_graph_result_doc,
                    user_action_graph_result,
                    action_graph_node_id,
                    is_rerun_graph,
                    webhook,
                )
            else:
                logger.bind(ctx=ctx).info("Running background tasks as method")
                action_graph_result_doc = await ActionGraph._run_as_background_task(
                    ctx,
                    user_actions,
                    action_graph_input_dict,
                    action_graph_result_doc,
                    user_action_graph_result,
                    action_graph_node_id,
                    is_rerun_graph,
                    webhook,
                )

            return action_graph_result_doc
        except Exception as e:
            bind_dict = {
                "app_code": AppCode.ACTION_GRAPH_RUN,
                "action_graph_id": action_graph_doc.id,
                "action_graph_run_id": action_graph_result_doc.id,
            }
            logger.bind(ctx=ctx, **bind_dict).error(f"ActionGraph run failed due to {e}")
            raise

    @staticmethod
    async def _run_as_background_task(
        ctx: Context,
        user_actions: UserActions,
        action_graph_input_dict: Dict[str, Any],
        action_graph_result_doc: ActionGraphResultDoc,
        user_action_graph_result: UserActionGraphResult,
        action_graph_node_id: Optional[str] = None,
        is_rerun_graph: bool = False,
        webhook: Webhook | None = None,
    ):
        """
        We are using indirection. This is because Background task doesn't work if the called function has a decorator.
        In our case `retry` decorator.
        """
        start_time = Clock.get_date_time()
        try:
            logger.bind(
                ctx=ctx,
                app_code=AppCode.ACTION_GRAPH_RUN.value,
                user_id=action_graph_result_doc.user_id,
                action_graph_id=action_graph_result_doc.result.id,
                action_graph_name=action_graph_result_doc.result.name,
                action_graph_version=action_graph_result_doc.result.version,
                action_graph_run_id=action_graph_result_doc.id,
            ).info("Action Graph Run Start")
            action_graph_result_doc = await ActionGraph._run_retry_as_background_task(
                ctx=ctx,
                user_actions=user_actions,
                action_graph_input_dict=action_graph_input_dict,
                action_graph_result_doc=action_graph_result_doc,
                user_action_graph_result=user_action_graph_result,
                action_graph_node_id=action_graph_node_id,
                is_rerun_graph=is_rerun_graph,
                webhook=webhook,
            )
            return action_graph_result_doc
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in running action graph several times {str(e)}")
        finally:
            end_time = Clock.get_date_time()
            logger.bind(
                ctx=ctx,
                app_code=AppCode.ACTION_GRAPH_RUN.value,
                user_id=action_graph_result_doc.user_id,
                action_graph_id=action_graph_result_doc.result.id,
                action_graph_name=action_graph_result_doc.result.name,
                action_graph_version=action_graph_result_doc.result.version,
                action_graph_run_id=action_graph_result_doc.id,
                action_graph_run_status=action_graph_result_doc.status.value,
                duration=end_time.diff(start_time).in_seconds(),
            ).info("Action Graph Run Complete")

    @staticmethod
    @retry(stop=stop_after_attempt(3), wait=wait_fixed(1), reraise=True)
    async def _run_retry_as_background_task(
        ctx: Context,
        user_actions: UserActions,
        action_graph_input_dict: Dict[str, Any],
        action_graph_result_doc: ActionGraphResultDoc,
        user_action_graph_result: UserActionGraphResult,
        action_graph_node_id: Optional[str] = None,
        is_rerun_graph: bool = False,
        webhook: Webhook | None = None,
    ) -> ActionGraphResultDoc | None:
        # TODO: check if action_graph_result_doc status is success, if success then return.
        # TODO: This behaviour should be accompanied by status change on action_graph_result_doc update

        # Empty Downstream Nodes if necessary
        if action_graph_node_id and is_rerun_graph:
            logger.bind(ctx=ctx).info("Empty Downstream Nodes")
            await ActionGraph.empty_downstream_nodes(
                ctx=ctx,
                action_graph_node_id=action_graph_node_id,
                action_graph_result_doc=action_graph_result_doc,
            )
        elif not action_graph_node_id and is_rerun_graph:
            logger.bind(ctx=ctx).info("Empty all Nodes")
            action_graph_result_doc.result.output = {}

        # ACTION GRAPH RESULT - NODE RERUN
        if action_graph_node_id and not is_rerun_graph:
            logger.bind(ctx=ctx, action_graph_node_id=action_graph_node_id).info(
                "Action Graph Node Rerun start"
            )
            action_graph_result_doc = await ActionGraph.run_node(
                ctx=ctx,
                user_actions=user_actions,
                action_graph_input_dict=action_graph_input_dict,
                action_graph_result_doc=action_graph_result_doc,
                action_graph_node_id=action_graph_node_id,
                user_action_graph_result=user_action_graph_result,
                webhook=webhook,
            )
            logger.bind(ctx=ctx, action_graph_node_id=action_graph_node_id).info(
                "Action Graph Node Rerun complete"
            )
        # ACTION GRAPH RESULT - GRAPH RUN / RERUN
        else:
            logger.bind(ctx=ctx).info("Action Graph Run/Rerun Start")
            action_graph_result_doc = await ActionGraph.run_action_graph(
                ctx,
                user_actions,
                action_graph_input_dict,
                action_graph_result_doc,
                user_action_graph_result,
                webhook,
            )
            logger.bind(ctx=ctx).info("Action Graph Run/Rerun Complete")

        if action_graph_result_doc.status == EventResultStatus.error:
            logger.bind(ctx=ctx).info("Retrying ActionGraph run")
            raise HTTPException(status_code=500, detail="Action Graph run failed")

        return action_graph_result_doc

    @staticmethod
    async def run_action_graph(
        ctx: Context,
        user_actions: UserActions,
        action_graph_input_dict: Dict[str, Any],
        action_graph_result_doc: ActionGraphResultDoc,
        user_action_graph_result: UserActionGraphResult,
        webhook: Webhook | None = None,
    ) -> ActionGraphResultDoc:
        bind_dict = {
            "app_code": AppCode.ACTION_GRAPH_RUN,
            "action_graph_id": action_graph_result_doc.result.id,
            "action_graph_run_id": action_graph_result_doc.id,
        }
        logger.bind(ctx=ctx, **bind_dict).info("Action Graph Run Start")
        graph_map = action_graph_result_doc.result.graph
        node_action_map = action_graph_result_doc.result.nodes
        node_details_map = action_graph_result_doc.result.node_details
        action_response: Dict[str, ActionDoc] = action_graph_result_doc.result.output
        # action_graph_input = TextObj.model_validate(action_graph_input_dict)

        total_nodes = await ActionGraph.get_nodes(graph_map)
        inverted_map = await ActionGraph.invert_map(graph_map)

        review_required_nodes: List[str] = []
        unreachable_nodes: List[str] = []

        count = 0
        try:
            while len(action_response) + len(unreachable_nodes) < len(total_nodes):
                count += 1
                if count > 100:
                    logger.bind(**bind_dict).error("Exiting Action Graph Run as total loops > 100")
                    raise StopIteration
                logger.bind(
                    ctx=ctx,
                    **bind_dict,
                    count_action_response=len(action_response),
                    count_unreachable_nodes=len(unreachable_nodes),
                    count_total_nodes=len(total_nodes),
                ).info("Running Action Graph as action_response + unreachable_nodes < total_nodes")
                for node, upstream_nodes in inverted_map.items():
                    if await ActionGraph.is_work_done(
                        [node], action_response
                    ) or not await ActionGraph.is_work_done(upstream_nodes, action_response):
                        logger.bind(
                            ctx=ctx, **bind_dict, action_name=node_action_map.get(node)
                        ).info(
                            "Continuing to next None as this Node's result is calculated or Upstream dependencies are not met"
                        )
                        continue
                    else:
                        logger.bind(ctx=ctx, **bind_dict, action_id=node_action_map.get(node)).info(
                            "Continuing exec as Node's result not yet calculated and Upstream dependencies are met"
                        )
                    # Check if user review required
                    is_any_dependent_require_review = False
                    for upstream_node in upstream_nodes:
                        if (
                            node_details_map
                            and node_details_map.get(upstream_node)
                            and node_details_map.get(upstream_node).data.user_review_required
                            and not node_details_map.get(upstream_node).data.user_review_done
                        ):
                            if upstream_node not in review_required_nodes:
                                review_required_nodes.append(upstream_node)

                                def find_downstream_nodes(node, graph):
                                    downstream = set()
                                    for dependent, dependencies in graph.items():
                                        if node in dependencies:
                                            downstream.add(dependent)
                                            downstream.update(
                                                find_downstream_nodes(dependent, graph)
                                            )
                                    return downstream

                                dependent_nodes = list(
                                    find_downstream_nodes(upstream_node, inverted_map)
                                )
                                new_unreachable_nodes = [
                                    dependent_node
                                    for dependent_node in dependent_nodes
                                    if dependent_node not in unreachable_nodes
                                ]
                                unreachable_nodes += new_unreachable_nodes
                                logger.bind(**bind_dict).info(
                                    f"Unreachable nodes: {unreachable_nodes}"
                                )
                            is_any_dependent_require_review = True
                            logger.bind(
                                ctx=ctx, node_id=node, upstream_node_id=upstream_node, **bind_dict
                            ).info("Upstream node requires User review")
                        elif (
                            node_details_map
                            and node_details_map.get(upstream_node)
                            and node_details_map.get(upstream_node).data.user_review_required
                            and node_details_map.get(upstream_node).data.user_review_done
                        ):
                            dependent_nodes = graph_map.get(upstream_node)
                            for dependent_node in dependent_nodes:
                                if dependent_node in unreachable_nodes:
                                    unreachable_nodes.remove(dependent_node)
                                    logger.bind(
                                        ctx=ctx,
                                        node_id=dependent_node,
                                        upstream_node_id=upstream_node,
                                        **bind_dict,
                                    ).info("Removed node from unreachable list")
                    if is_any_dependent_require_review:
                        logger.bind(ctx=ctx, node_id=node, **bind_dict).info(
                            "Continuing to run next node in graph as this node requires review"
                        )
                        continue
                    else:
                        logger.bind(ctx=ctx, **bind_dict, action_id=node_action_map.get(node)).info(
                            "Continuing exec as this Node's result is not yet calculated are review requirement are met"
                        )

                    #  TODO: REFACTOR: RUN CODE START
                    if len(upstream_nodes) == 0:
                        # Run action with no dependency
                        # check if node is loop
                        if node_details_map and (
                            node_details_map.get(node).data.is_loop
                            and not node_details_map.get(node).data.file_split
                            and not node_details_map.get(node).data.excel_loop
                        ):
                            # run action in loop
                            inter_action_result = []
                            multiple_objs = action_graph_input_dict["text"].split(
                                node_details_map.get(node).data.seperator
                            )
                            for multiple in multiple_objs:
                                data_block = DataBlock(text=multiple)
                                _action_result: ActionDoc = await ActionGraph.run_action(
                                    ctx,
                                    user_actions,
                                    node_action_map.get(node),
                                    data_block.model_dump(),
                                )
                                match _action_result:
                                    case Exception():
                                        logger.bind(ctx=ctx, **bind_dict).exception(
                                            f"Error in loop run action {str(_action_result)}"
                                        )
                                    case ActionDoc():
                                        inter_action_result.append(_action_result)
                                        action_result = await ActionGraph.collate_action_results(
                                            inter_action_result
                                        )
                                        action_response[node] = ActionDoc.model_validate(
                                            action_result
                                        )
                                        action_graph_result_doc.result.output[node] = (
                                            action_response[node]
                                        )
                                        action_graph_result_update: ActionGraphResultUpdate = (
                                            ActionGraphResultUpdate(
                                                result=action_graph_result_doc.result
                                            )
                                        )
                                        action_graph_result_doc = await user_action_graph_result.update_action_graph_result(
                                            ctx,
                                            action_graph_result_doc.id,
                                            action_graph_result_update,
                                        )
                        elif (
                            node_details_map.get(node)
                            and node_details_map.get(node).data.excel_loop
                        ):
                            # run action in loop
                            inter_action_result = []
                            batches = split_excel_into_batches(
                                action_graph_input_dict["urls"][0],
                                1,
                                header=node_details_map.get(node).data.header,
                            )
                            for batch in batches:
                                if node_details_map.get(node).data.data_processing:
                                    # Process all columns from the row, not just first column
                                    row_data = batch.to_dict(orient="records")[0]
                                    data_block = DataBlock(
                                        text=f"{action_graph_input_dict['text']}\n\nData: {json.dumps(row_data)}"
                                    )
                                elif node_details_map.get(node).data.file_processing:
                                    # Process files with instructions
                                    instruction = batch.iloc[0][
                                        node_details_map.get(node).data.instruction_column_index
                                    ]
                                    data_block = DataBlock(
                                        text=f"{action_graph_input_dict['text']}\n\nInstruction: {instruction}",
                                        urls=[
                                            batch.iloc[0][
                                                node_details_map.get(node).data.file_column_index
                                            ]
                                        ],
                                    )
                                else:
                                    # Default behavior
                                    data_block = DataBlock(
                                        text=action_graph_input_dict["text"],
                                        urls=[
                                            batch.iloc[0][
                                                node_details_map.get(node).data.file_column_index
                                            ]
                                        ],
                                    )
                                _action_result: ActionDoc = await ActionGraph.run_action(
                                    ctx,
                                    user_actions,
                                    node_action_map.get(node),
                                    data_block.model_dump(),
                                )
                                match _action_result:
                                    case Exception():
                                        logger.bind(ctx=ctx).exception(
                                            f"Error in loop run action {str(_action_result)}"
                                        )
                                    case ActionDoc():
                                        inter_action_result.append(_action_result)
                                        action_result = await ActionGraph.collate_action_results(
                                            inter_action_result
                                        )
                                        action_response[node] = ActionDoc.model_validate(
                                            action_result
                                        )
                                        action_graph_result_doc.result.output[node] = (
                                            action_response[node]
                                        )
                                        action_graph_result_update: ActionGraphResultUpdate = (
                                            ActionGraphResultUpdate(
                                                result=action_graph_result_doc.result
                                            )
                                        )
                                        action_graph_result_doc = await user_action_graph_result.update_action_graph_result(
                                            ctx,
                                            action_graph_result_doc.id,
                                            action_graph_result_update,
                                        )
                        elif (
                            node_details_map.get(node)
                            and node_details_map.get(node).data.is_loop
                            and node_details_map.get(node).data.file_split
                        ):
                            inter_action_result = []
                            multiple_objs = action_graph_input_dict["urls"]
                            for multiple in multiple_objs:
                                data_block = DataBlock(
                                    text=action_graph_input_dict["text"], urls=[multiple]
                                )
                                _action_result: ActionDoc = await ActionGraph.run_action(
                                    ctx,
                                    user_actions,
                                    node_action_map.get(node),
                                    data_block.model_dump(),
                                )
                                match _action_result:
                                    case Exception():
                                        logger.bind(ctx=ctx).exception(
                                            f"Error in loop run action {str(_action_result)}"
                                        )
                                    case ActionDoc():
                                        inter_action_result.append(_action_result)
                                        action_result = await ActionGraph.collate_action_results(
                                            inter_action_result
                                        )
                                        action_response[node] = ActionDoc.model_validate(
                                            action_result
                                        )
                                        action_graph_result_doc.result.output[node] = (
                                            action_response[node]
                                        )
                                        action_graph_result_update: ActionGraphResultUpdate = (
                                            ActionGraphResultUpdate(
                                                result=action_graph_result_doc.result
                                            )
                                        )
                                        action_graph_result_doc = await user_action_graph_result.update_action_graph_result(
                                            ctx,
                                            action_graph_result_doc.id,
                                            action_graph_result_update,
                                        )
                        else:
                            action_result: ActionDoc = await ActionGraph.run_action(
                                ctx,
                                user_actions,
                                node_action_map.get(node),
                                action_graph_input_dict,
                            )
                            # action_result = await user_actions.run_action_v1(node_action_map.get(node), action_graph_input.model_dump())
                            match action_result:
                                case Exception():
                                    logger.bind(ctx=ctx, **bind_dict).exception(
                                        f"Error in ActionGraph run_action: {str(action_result)}"
                                    )
                                    raise action_result
                                case ActionDoc():
                                    action_response[node] = ActionDoc.model_validate(action_result)
                            action_response[node] = ActionDoc.model_validate(action_result)
                    else:
                        # Run action with at least 1 dependency #TODO: error handling
                        if node_details_map and node_details_map.get(node).data.is_loop:
                            inter_action_result = []
                            curr_action = await ActionGraph.get_action(
                                user_actions, node_action_map.get(node)
                            )
                            separator = "|"
                            if (
                                node_details_map.get(node)
                                and node_details_map.get(node).data
                                and node_details_map.get(node).data.seperator
                            ):
                                separator = node_details_map.get(node).data.seperator
                            action_input = await ActionGraph.to_input(
                                ctx,
                                upstream_nodes,
                                curr_action,
                                action_response,
                                separator=separator,
                                is_loop=node_details_map.get(node).data.is_loop,
                                api_loop=node_details_map.get(node).data.api_loop,
                                batches=node_details_map.get(node).data.batches,
                                excel_loop=node_details_map.get(node).data.excel_loop,
                                file_column_index=node_details_map.get(node).data.file_column_index,
                                is_split=node_details_map.get(node).data.is_split,
                                file_split=node_details_map.get(node).data.file_split,
                                header=node_details_map.get(node).data.header,
                                data_processing=node_details_map.get(node).data.data_processing,
                                instruction_column_index=node_details_map.get(
                                    node
                                ).data.instruction_column_index,
                                file_processing=node_details_map.get(node).data.file_processing,
                            )
                            for _action in action_input:
                                try:
                                    _action_result: ActionDoc = await ActionGraph.run_action(
                                        ctx,
                                        user_actions,
                                        node_action_map.get(node),
                                        _action.model_dump(),
                                    )
                                    match _action_result:
                                        case Exception():
                                            logger.bind(ctx=ctx, **bind_dict).exception(
                                                f"Error in loop run action {str(_action_result)}"
                                            )
                                        case ActionDoc():
                                            inter_action_result.append(_action_result)
                                            action_result = (
                                                await ActionGraph.collate_action_results(
                                                    inter_action_result
                                                )
                                            )
                                            action_response[node] = ActionDoc.model_validate(
                                                action_result
                                            )
                                            action_graph_result_doc.result.output[node] = (
                                                action_response[node]
                                            )
                                            action_graph_result_update: ActionGraphResultUpdate = (
                                                ActionGraphResultUpdate(
                                                    result=action_graph_result_doc.result
                                                )
                                            )
                                            action_graph_result_doc = await user_action_graph_result.update_action_graph_result(
                                                ctx,
                                                action_graph_result_doc.id,
                                                action_graph_result_update,
                                            )
                                except Exception as e:
                                    logger.bind(ctx=ctx, **bind_dict).exception(
                                        f"Error in loop run action: {str(e)}"
                                    )
                        else:
                            # run action in merge
                            curr_action = await ActionGraph.get_action(
                                user_actions, node_action_map.get(node)
                            )
                            logger.bind(ctx=ctx, **bind_dict, action_name=curr_action.name).info(
                                "Getting input for current action"
                            )
                            separator = "|"
                            if (
                                node_details_map.get(node)
                                and node_details_map.get(node).data
                                and node_details_map.get(node).data.seperator
                            ):
                                separator = node_details_map.get(node).data.seperator
                            action_input = await ActionGraph.to_input(
                                ctx,
                                upstream_nodes,
                                curr_action,
                                action_response,
                                separator=separator,
                            )
                            action_result: ActionDoc = await ActionGraph.run_action(
                                ctx,
                                user_actions,
                                node_action_map.get(node),
                                action_input.model_dump(),
                            )
                            # action_result = await user_actions.run_action_v1(node_action_map.get(node), action_input.model_dump())
                            match action_result:
                                case Exception():
                                    logger.bind(ctx=ctx, **bind_dict).exception(
                                        f"Error in ActionGraph run_action: {str(action_result)}"
                                    )
                                    raise action_result
                                case ActionDoc():
                                    action_response[node] = ActionDoc.model_validate(action_result)

                            memory_profile = await Profiler.do_memory_profile(ctx)
                            logger.bind(ctx=ctx, **bind_dict, memory_profile=memory_profile).info(
                                f"Memory profile for Action run {action_result.name}:{action_result.version}"
                            )
                    # TODO: REFACTOR: RUN CODE END

                    # Update action result graph
                    action_graph_result_doc.result.output[node] = action_response[node]
                    action_graph_result_update: ActionGraphResultUpdate = ActionGraphResultUpdate(
                        result=action_graph_result_doc.result
                    )
                    action_graph_result_doc = (
                        await user_action_graph_result.update_action_graph_result(
                            ctx, action_graph_result_doc.id, action_graph_result_update
                        )
                    )
                    if webhook:
                        await webhook.send(action_graph_result_doc.model_dump())
            logger.bind(
                ctx=ctx,
                count_action_response=len(action_response),
                count_unreachable_nodes=len(unreachable_nodes),
                count_total_nodes=len(total_nodes),
                **bind_dict,
            ).info(
                "Exited Action Graph Run While loop len(action_response) + len(unreachable_nodes) >= len(total_nodes)"
            )

            # Update action result graph as success or waiting
            action_graph_result_update: ActionGraphResultUpdate | None
            if len(review_required_nodes) == 0:
                action_graph_result_update: ActionGraphResultUpdate = ActionGraphResultUpdate(
                    status=EventResultStatus.success
                )
            else:
                action_graph_result_update: ActionGraphResultUpdate = ActionGraphResultUpdate(
                    status=EventResultStatus.waiting
                )

            action_graph_result_doc = await user_action_graph_result.update_action_graph_result(
                ctx, action_graph_result_doc.id, action_graph_result_update
            )
            if webhook:
                await webhook.send(action_graph_result_doc.model_dump())
        except Exception as e:
            logger.bind(ctx=ctx, **bind_dict).exception(f"Error while graph run, {str(e)}")

            # Update action result graph as error
            action_graph_result_update: ActionGraphResultUpdate = ActionGraphResultUpdate(
                status=EventResultStatus.error, error_message=TextObj(text=str(e))
            )
            action_graph_result_doc = await user_action_graph_result.update_action_graph_result(
                ctx, action_graph_result_doc.id, action_graph_result_update
            )
            if webhook:
                await webhook.send(action_graph_result_doc.model_dump())
        finally:
            logger.bind(ctx=ctx, **bind_dict).info("Action Graph Run Completed")
            return action_graph_result_doc

    @staticmethod
    async def run_node(
        ctx: Context,
        user_actions: UserActions,
        action_graph_input_dict: Dict[str, Any],
        action_graph_result_doc: ActionGraphResultDoc,
        action_graph_node_id: str,
        user_action_graph_result: UserActionGraphResult,
        webhook: Webhook | None = None,
    ) -> ActionGraphResultDoc:
        logger.bind(ctx=ctx).info("Starting Run Node")
        action_response: Dict[str, ActionDoc] = action_graph_result_doc.result.output

        # Run action with no dependency
        node_action_doc = action_response.get(action_graph_node_id)

        logger.bind(
            ctx=ctx,
            app_code=AppCode.ACTION_GRAPH_RUN,
            graph_id=action_graph_result_doc.result.id,
            graph_run_id=action_graph_result_doc.id,
            action_id=node_action_doc.id,
        ).info(f"Started Run Node: {action_graph_node_id}")
        try:
            # # Update action result graph result with input
            # action_graph_result_doc.result.output.get(action_graph_node_id).input = action_graph_input_dict
            # action_graph_result_update: ActionGraphResultUpdate = ActionGraphResultUpdate(
            #     result=action_graph_result_doc.result, status=EventResultStatus.processing
            # )
            # action_graph_result_doc = await user_action_graph_result.update_action_graph_result(
            #     action_graph_result_doc.id,
            #     action_graph_result_update
            # )
            # if webhook:
            #     await webhook.send(action_graph_result_doc.model_dump())

            action_result: ActionDoc = await ActionGraph.run_action_doc(  # noqa F841
                ctx=ctx,
                user_actions=user_actions,
                action_doc=node_action_doc,
                action_graph_input_dict=action_graph_input_dict,
                user=user_actions.user,
            )
            # Update action result graph result
            action_graph_result_doc.result.output[action_graph_node_id] = action_result
            # save
            action_graph_result_update: ActionGraphResultUpdate = ActionGraphResultUpdate(
                result=action_graph_result_doc.result, status=EventResultStatus.success
            )
            action_graph_result_doc = await user_action_graph_result.update_action_graph_result(
                ctx, action_graph_result_doc.id, action_graph_result_update
            )
            if webhook:
                await webhook.send(action_graph_result_doc.model_dump())
        except Exception as e:
            logger.bind(
                ctx=ctx,
                app_code=AppCode.ACTION_GRAPH_RUN,
                graph_id=action_graph_result_doc.result.id,
                graph_run_id=action_graph_result_doc.id,
                action_id=node_action_doc.id,
            ).info(f"Error in rerun node of Action Graph: {str(e)}")
            # Update action result graph as error
            action_graph_result_update: ActionGraphResultUpdate = ActionGraphResultUpdate(
                status=EventResultStatus.error, error_message=TextObj(text=str(e))
            )
            action_graph_result_doc = await user_action_graph_result.update_action_graph_result(
                ctx, action_graph_result_doc.id, action_graph_result_update
            )
            if webhook:
                await webhook.send(action_graph_result_doc.model_dump())
        finally:
            logger.bind(
                ctx=ctx,
                app_code=AppCode.ACTION_GRAPH_RUN,
                graph_id=action_graph_result_doc.result.id,
                graph_run_id=action_graph_result_doc.id,
                action_id=node_action_doc.id,
            ).info("Action Graph Node Run Completed")
            return action_graph_result_doc

    @staticmethod
    async def run_action(
        ctx: Context,
        user_actions: UserActions,
        action_id: str,
        action_graph_input_dict: Dict[str, Any],
    ) -> ActionDoc | Exception:
        logger.bind(ctx=ctx, action_id=action_id).info("ActionGraph Run action")
        # TODO: update code to Find owned action of published action. Then call UserActions.run_action_doc()
        # NOTE: await UserActions.run_action_doc() does not persist state like run_action_v1, so
        # DO: user_actions.update_action()
        try:
            action = await user_actions.get_action(action_id=action_id)
            is_action_found = True if action else False
            if action_id == "" or is_action_found:
                action_result = await user_actions.run_action_v1(
                    ctx, action_id, action_graph_input_dict
                )
                logger.bind(ctx=ctx, action_id=action_id).info("Ran User Action")
                return action_result

            published_action = await user_actions.get_published_action(
                id=action_id, empty_published_action_config=False
            )
            is_market_found = True if published_action else False
            if is_market_found:
                action_result = await user_actions.run_action_doc(
                    ctx=ctx,
                    action_doc=published_action,
                    input=action_graph_input_dict,
                    user=user_actions.user,
                    db=user_actions.db,
                )
                logger.bind(ctx=ctx, action_id=action_id).info("Ran Published Action")
                return action_result

            if not is_action_found and not is_market_found:
                logger.bind(ctx=ctx, action_id=action_id).error(f"Action {action_id} not found")
                return HTTPException(404, "Action not found")

        except HTTPException as e:
            logger.bind(ctx=ctx, action_id=action_id).exception(f"Error in Run Action: {str(e)}")
            return HTTPException(500, f"Error in Run Action: {str(e)}")

    @staticmethod
    async def get_action(
        user_actions: UserActions,
        action_id: str,
    ) -> ActionDoc:
        exception = None
        try:
            action = await user_actions.get_action(action_id)
            if action is None:
                raise HTTPException(404, "Action not found")
            return action
        except HTTPException as e:
            exception = e
        try:
            action = await user_actions.get_published_action(
                id=action_id, empty_published_action_config=False
            )
            return action
        except Exception as e:
            raise HTTPException(405, f"{exception.detail} and {e}")

    @staticmethod
    async def run_action_doc(
        ctx: Context,
        user_actions: UserActions,
        action_doc: ActionDoc,
        action_graph_input_dict: Dict[str, Any],
        user: UserORM,
    ) -> ActionDoc:
        try:
            action_doc = await user_actions.run_action_doc(
                ctx=ctx,
                action_doc=action_doc,
                input=action_graph_input_dict,
                user=user,
                db=user_actions.db,
            )
            return action_doc
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in run_action_doc {str(e)}")
            raise

    @staticmethod
    async def get_nodes(graph: Dict[str, List[str]]) -> Set[str]:
        nodes: Set[str] = set()
        for key, values in graph.items():
            nodes.add(key)
            for val in values:
                nodes.add(val)
        return nodes

    @staticmethod
    async def invert_map(graph: Dict[str, List[str]]) -> Dict[str, List[str]]:
        inverted_map: Dict[str, List[str]] = {}

        nodes = await ActionGraph.get_nodes(graph)
        for node in nodes:
            inverted_map[node] = []

        for key, values in graph.items():
            for value in values:
                inverted_map[value] = inverted_map.get(value) + [key]

        return inverted_map

    @staticmethod
    async def is_work_done(values: List[str], action_response: Dict[str, Any]) -> bool:
        for value in values:
            if value not in action_response:
                return False
        return True

    @staticmethod
    async def to_input(
        ctx: Context,
        upstream_nodes: List[str],
        current_node_action: ActionDoc,
        action_response: Dict[str, ActionDoc],
        separator,
        is_loop: bool = False,
        excel_loop: bool = False,
        file_column_index: int = 0,
        api_loop: bool = False,
        batches: int = 0,
        is_split: bool = False,
        file_split: bool = False,
        header: bool = False,
        data_processing: bool = False,
        instruction_column_index: int = 0,
        file_processing: bool = False,
    ) -> DataBlock | Any:
        input_msg = ""
        urls = []
        current_node_action_type = current_node_action.type

        # curr_action_input = await ActionGraph.gen_input(upstream_nodes, current_node_action_type, action_response)
        # return curr_action_input
        # Use llm to build input obj
        upstream_node_names = []
        for upstream_node in upstream_nodes:
            upstream_action_doc = action_response.get(upstream_node)
            upstream_node_names.append(upstream_action_doc.name)
            upstream_action_output_type = ACTION_MAP.get(upstream_action_doc.type).get_output_type()
            if (
                upstream_action_output_type != DataBlocks
                and upstream_action_output_type != DataBlock
            ):
                logger.bind(
                    ctx=ctx,
                    current_node_name=current_node_action.name,
                    upstream_node_name=upstream_action_doc.name,
                ).info("Choosing llm to generate input for Node")
                curr_action_input = await ActionGraph.gen_input(
                    ctx, upstream_nodes, current_node_action_type, action_response
                )
                logger.bind(
                    ctx=ctx,
                    current_node_name=current_node_action.name,
                    upstream_node_name=upstream_action_doc.name,
                ).info("Used llm to generate input for Node")
                return curr_action_input
        # Use manual code to build input obj
        logger.bind(ctx=ctx).info("Choosing manual path to generate input for Node")
        if api_loop:
            blocks = []
            for upstream_node in upstream_nodes:
                action_doc = action_response.get(upstream_node)
                action_outputs = action_doc.output
                action_outputs = DataBlocks.model_validate(action_outputs)
                for action_output in action_outputs.blocks:
                    for batch in range(batches):
                        data_block = DataBlock(
                            text=action_output.text
                            + f"Get google sheets data for batch_id: {batch} using api_call"
                        )
                        blocks.append(data_block)

        if excel_loop:
            blocks = []
            for upstream_node in upstream_nodes:
                action_doc = action_response.get(upstream_node)
                action_outputs = action_doc.output
                action_outputs = DataBlocks.model_validate(action_outputs)
                for action_output in action_outputs.blocks:
                    batches = split_excel_into_batches(action_output.urls[0], 1, header=header)
                    for batch in batches:
                        if data_processing:
                            # Process all columns from the row, not just first column
                            row_data = batch.to_dict(orient="records")[0]
                            data_block = DataBlock(
                                text=f"{action_output.text}\n\nData: {json.dumps(row_data)}"
                            )
                        elif file_processing:
                            instruction = batch.iloc[0][instruction_column_index]
                            data_block = DataBlock(
                                text=f"{action_output.text}\n\nInstruction: {instruction}",
                                urls=[batch.iloc[0][file_column_index]],
                            )
                        else:
                            data_block = DataBlock(
                                text=action_output.text, urls=[batch.iloc[0][file_column_index]]
                            )
                        blocks.append(data_block)
            return blocks

        if is_loop:
            blocks = []
            temp_blocks = []
            for upstream_node in upstream_nodes:
                action_doc = action_response.get(upstream_node)
                try:
                    action_outputs = action_doc.output
                    action_outputs = DataBlocks.model_validate(action_outputs)
                    upstream_blocks = []
                    for action_output in action_outputs.blocks:
                        # check for urls first TODO: Need for better solution
                        if is_split and not file_split:
                            multiple_objs = action_output.text.split(separator)
                            for multiple in multiple_objs:
                                data_block = DataBlock(text=multiple)
                                blocks.append(data_block)
                        elif file_split and len(action_output.urls) > 0:
                            for i in range(len(action_output.urls)):
                                data_block = DataBlock(
                                    text=action_output.text, urls=[action_output.urls[i]]
                                )
                                blocks.append(data_block)
                        else:
                            data_block = DataBlock(text=action_output.text)
                            upstream_blocks.append(data_block)
                    if not is_split and not file_split:
                        temp_blocks.append(upstream_blocks)
                except Exception as e:
                    logger.bind(ctx=ctx, current_node_name=current_node_action.name).exception(
                        f"Error in to_input: {str(e)}"
                    )

            # Only combine blocks if we're not splitting
            if not is_split and not file_split and temp_blocks:
                # Find the minimum length among all upstream blocks to avoid index errors
                min_length = min(len(blocks) for blocks in temp_blocks)

                # Combine blocks from different upstreams by index
                for i in range(min_length):
                    combined_text = ""
                    for upstream_blocks in temp_blocks:
                        if combined_text:
                            combined_text += "\n"
                        combined_text += upstream_blocks[i].text
                    blocks.append(DataBlock(text=combined_text))

            return blocks
        else:
            for upstream_node in upstream_nodes:
                action_doc = action_response.get(upstream_node)
                logger.bind(
                    ctx=ctx,
                    upstream_action_name=action_doc.name,
                    curr_action=current_node_action.name,
                ).info(
                    f"Creating input for node: {current_node_action.name} from upstream node: {action_doc.name}"
                )
                action_outputs = action_doc.output
                try:
                    # check if action_output is DataBlocks
                    action_outputs = DataBlocks.model_validate(action_outputs)
                    for action_output in action_outputs.blocks:
                        urls += action_output.urls
                        # check for urls first TODO: Need for better solution
                        try:
                            potential_urls = action_output.text.split(",")
                            for potential_url in potential_urls:
                                url = HttpUrl(potential_url)
                                input_msg += f"{url.unicode_string()},"
                                logger.bind(ctx=ctx).info("Added url to input_msg")
                        except Exception as e:
                            data_block = DataBlock.model_validate(action_output)
                            if action_doc.name:
                                if input_msg:
                                    input_msg += f"\n## {action_doc.name}:\n"
                                else:
                                    input_msg += f"## {action_doc.name}:\n"
                            input_msg += f"{data_block.text}\n"
                            logger.bind(ctx=ctx).info(f"Added DataBlock to input_msg {str(e)}")
                except Exception as e:
                    logger.bind(
                        ctx=ctx,
                        curr_action_name=current_node_action.name,
                        upstream_action_name=action_doc.name,
                    ).exception(f"Error while creating input from upstream node: {str(e)}")
            data_block = DataBlock(text=input_msg, urls=urls)
            return data_block
            # if isinstance(action_outputs, TextObjs):
            #     for action_output in action_outputs.texts:
            #         if isinstance(action_output, TextObj):
            #             text_obj = TextObj.model_validate(action_output)
            #             input_msg = f"{input_msg}\n{text_obj.text}"
            # else:
            #     logger.warning("Cannot convert to Input")

    @staticmethod
    async def collate_action_results(action_results: List[ActionDoc]) -> ActionDoc:
        assert len(action_results) > 0
        results = copy.deepcopy(action_results[0])
        results.results = copy.deepcopy(action_results[0].results)
        for i, action_result in enumerate(action_results[1:]):
            results.input[f"text_{i}"] = action_result.input["text"]
            results.output["blocks"] += action_result.output["blocks"]
            results.results += action_result.results

        return results

    @staticmethod
    async def gen_input(
        ctx: Context,
        upstream_nodes: List[str],
        current_node_action_type: str,
        action_response: Dict[str, ActionDoc],
    ) -> Dict[str, Any]:
        try:
            llm_input = (
                "You are an expert programmer and logician."
                "Create result json for a given model json schema for result, "
                "from the input(s) data and its/their json model schema\n\n"
            )
            for upstream_node in upstream_nodes:
                upstream_action_doc = action_response.get(upstream_node)
                upstream_action_output_type = ACTION_MAP.get(
                    upstream_action_doc.type
                ).get_output_type()
                upstream_action_output_model_schema = (
                    upstream_action_output_type.model_json_schema()
                )
                upstream_action_output = upstream_action_doc.output
                llm_input += f"#####\nInput model schema:\n{upstream_action_output_model_schema}\n\nInput data:\n{upstream_action_output}\n\n"

            curr_action_input_type = ACTION_MAP.get(current_node_action_type).get_input_type()
            curr_action_input_model_schema = curr_action_input_type.model_json_schema()
            llm_input += f"#####\nOutput model schema:\n{curr_action_input_model_schema}\n\n"
            llm_input += (
                "Add all inputs to give me the output data that adheres to Output model schema. You have to provide only the output in JSON. "
                "Start straight with { and end with }."
            )

            chat_req = LlmChatOpenaiConfig(
                provider="azure",
                model="gpt-4.1-mini",
                max_tokens=4096,
                messages=[ChatCompletionUserMessageParam(role="user", content=llm_input)],
                response_format={"type": "json_object"},
            )
            run_input = DataBlock(text=llm_input)
            data_blocks: DataBlocks = await ActionText2TextLlmChatOpenai(chat_req).run_action(
                ctx, run_input
            )
            output_json = data_blocks.blocks[0].text
            try:
                json_dict = json.loads(output_json)
            except Exception as e:
                logger.bind(ctx=ctx, current_node_action_type=current_node_action_type).exception(
                    f"Cannot convert to Input: {str(e)}"
                )
                json_dict = output_json
            curr_action_input_obj = curr_action_input_type.model_validate(json_dict)
            logger.bind(
                ctx=ctx,
                current_node_action_type=current_node_action_type,
                action_input=curr_action_input_obj,
            ).info("Used Llm to generate input by using outputs of upstream nodes")
            return curr_action_input_obj
            # return json_dict
        except Exception as e:
            logger.bind(ctx=ctx).error(f"Cannot generate Input: {str(e)}")
            return {}

    @staticmethod
    async def empty_downstream_nodes(
        ctx: Context, action_graph_node_id: str, action_graph_result_doc: ActionGraphResultDoc
    ):
        logger.bind(ctx=ctx).info("Emptying downstream nodes")
        to_visit = [action_graph_node_id]
        while to_visit:
            try:
                node = to_visit.pop(0)
                node_details = action_graph_result_doc.result.node_details.get(node)
                if node_details.data.actionId and node is not action_graph_node_id:
                    action_graph_result_doc.result.output.pop(node)

                downstream_nodes = action_graph_result_doc.result.graph.get(node, [])
                to_visit += downstream_nodes
            except Exception as e:
                logger.bind(ctx=ctx, current_node_action_type=action_graph_node_id).exception(
                    f"Error in emptying downstream nodes{str(e)}"
                )
