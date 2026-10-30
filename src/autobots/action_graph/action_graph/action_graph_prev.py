import asyncio
import copy
import json
import random
from collections import deque
from typing import Any, Dict, List, Optional, Set

from fastapi import BackgroundTasks, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from openai.types.chat import ChatCompletionUserMessageParam
from openai.types.shared_params import ResponseFormatJSONObject
from tenacity import retry, stop_after_attempt, wait_fixed

from src.autobots.action.action.action_doc_model import ActionDoc, ActionResult
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks, Metadata, TextObj
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action.action_type.action_map import ACTION_MAP
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openai_v2 import (
    ActionText2TextLlmChatOpenai,
    LlmChatOpenaiConfig,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.action_graph.action_graph.action_graph_doc_model_prev import ActionGraphDoc, Node
from src.autobots.action_graph.action_graph.action_graph_input_creator import (
    ActionGraphInputCreator,
)
from src.autobots.action_graph.action_graph.coffman_graham_prev import coffman_graham
from src.autobots.action_graph.action_graph_result.action_graph_result_model_doc import (
    ActionGraphResultDoc,
    ActionGraphResultUpdate,
)
from src.autobots.action_graph.action_graph_result.user_action_graph_result_prev import (
    UserActionGraphResult,
)
from src.autobots.api.webhook import Webhook
from src.autobots.conn.excel.read_and_split import pick_spreadsheet_url, split_excel_into_batches
from src.autobots.core.clock.clock import Clock
from src.autobots.core.heartbeat.running_executions import HeartBeatRunRegistry
from src.autobots.core.logging.app_code import AppCode
from src.autobots.core.profiler.profiler import Profiler
from src.autobots.core.semaphore.semaphore import get_semaphore
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM


class ActionGraph:
    @staticmethod
    async def run_in_background(
        ctx: Context,
        app_state: AppState,
        action_graph_doc: ActionGraphDoc,
        action_graph_input_dict: Dict[str, Any],
        user_actions: UserActions,
        user_action_graph_result: UserActionGraphResult,
        action_graph_result_id: Optional[str] = None,
        action_graph_node_id: Optional[str] = None,
        is_rerun_graph: bool = False,
        is_continue_run_graph: bool = False,
        is_empty_downstream_only: bool = False,
        title_override: str | None = None,
        background_tasks: BackgroundTasks = None,
        webhook: Webhook | None = None,
        notify: Notify | None = None,
        fire_and_forget: bool = False,
        batch_id: str | None = None,
    ) -> ActionGraphResultDoc | None:
        logger.bind(
            ctx=ctx,
            code=AppCode.ACTION_GRAPH,
            user_id=action_graph_doc.user_id,
            action_graph_id=action_graph_doc.id,
            action_graph_name=action_graph_doc.name,
            action_graph_version=action_graph_doc.version,
        ).info("Action Graph Run started")
        try:
            action_graph_result_doc = await user_action_graph_result.init(
                app_state=app_state,
                action_graph_result_id=action_graph_result_id,
                action_graph_doc=action_graph_doc,
                action_graph_input_dict=action_graph_input_dict,
                title_override=title_override,
                db=user_actions.db,
                user=user_actions.user,
                webhook=webhook,
                ctx=ctx,
                batch_id=batch_id,
            )

            app_state.action_graph_id = action_graph_doc.id
            app_state.action_graph_result_id = action_graph_result_doc.id
            app_state.action_graph_node_id = action_graph_node_id

            HeartBeatRunRegistry.get_instance().register(ctx=ctx , app_state=app_state, runnable_id=action_graph_result_doc.id, runnable_type=AppCode.ACTION_GRAPH.value)


            if notify is None:
                notify = Notify(topic=action_graph_result_doc.id)

            if background_tasks:
                logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).info("Starting background tasks")
                background_tasks.add_task(
                    ActionGraph._run_as_background_task,
                    ctx,
                    app_state,
                    user_actions,
                    action_graph_input_dict,
                    action_graph_result_doc,
                    user_action_graph_result,
                    notify,
                    action_graph_node_id,
                    is_rerun_graph,
                    is_continue_run_graph,
                    is_empty_downstream_only,
                    webhook,
                )
            elif fire_and_forget:
                logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).info("Starting fire-and-forget task with asyncio.create_task")
                asyncio.create_task(
                    ActionGraph._run_as_background_task(
                        ctx,
                        app_state,
                        user_actions,
                        action_graph_input_dict,
                        action_graph_result_doc,
                        user_action_graph_result,
                        notify,
                        action_graph_node_id,
                        is_rerun_graph,
                        is_continue_run_graph,
                        is_empty_downstream_only,
                        webhook,
                    )
                )
            else:
                logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).info("Running background tasks as method")
                action_graph_result_doc = await ActionGraph._run_as_background_task(
                    ctx,
                    app_state,
                    user_actions,
                    action_graph_input_dict,
                    action_graph_result_doc,
                    user_action_graph_result,
                    notify,
                    action_graph_node_id,
                    is_rerun_graph,
                    is_continue_run_graph,
                    is_empty_downstream_only,
                    webhook,
                )

            return action_graph_result_doc
        except Exception as e:
            bind_dict = {
                "code": AppCode.ACTION_GRAPH_RUN,
                "action_graph_id": action_graph_doc.id,
                "action_graph_run_id": action_graph_result_doc.id,
            }
            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH, **bind_dict).error(f"ActionGraph run failed due to {e}")
            raise

    @staticmethod
    async def _run_as_background_task(
        ctx: Context,
        app_state: AppState,
        user_actions: UserActions,
        action_graph_input_dict: Dict[str, Any],
        action_graph_result_doc: ActionGraphResultDoc,
        user_action_graph_result: UserActionGraphResult,
        notify: Notify,
        action_graph_node_id: Optional[str] = None,
        is_rerun_graph: bool = False,
        is_continue_run_graph: bool = False,
        is_empty_downstream_only: bool = False,
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
                code=AppCode.ACTION_GRAPH_RUN.value,
                user_id=action_graph_result_doc.user_id,
                action_graph_id=action_graph_result_doc.result.id,
                action_graph_name=action_graph_result_doc.result.name,
                action_graph_version=action_graph_result_doc.result.version,
                action_graph_run_id=action_graph_result_doc.id,
            ).info("Action Graph Run Start")
            action_graph_result_doc = await ActionGraph._run_retry_as_background_task(
                ctx=ctx,
                app_state=app_state,
                user_actions=user_actions,
                action_graph_input_dict=action_graph_input_dict,
                action_graph_result_doc=action_graph_result_doc,
                user_action_graph_result=user_action_graph_result,
                notify=notify,
                action_graph_node_id=action_graph_node_id,
                is_rerun_graph=is_rerun_graph,
                is_continue_run_graph=is_continue_run_graph,
                is_empty_downstream_only=is_empty_downstream_only,
                webhook=webhook,
            )
            action_graph_result_doc = ActionGraphResultDoc.model_validate(action_graph_result_doc)
            action_graph_result_doc.result = ActionGraphDoc.model_validate(
                action_graph_result_doc.result
            )
            return action_graph_result_doc
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).exception(f"Error in running action graph several times {str(e)}")
        finally:
            HeartBeatRunRegistry.get_instance().deregister(ctx=ctx, app_state=app_state, runnable_id=action_graph_result_doc.id)

            end_time = Clock.get_date_time()
            logger.bind(
                ctx=ctx,
                code=AppCode.ACTION_GRAPH_RUN.value,
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
        app_state: AppState,
        user_actions: UserActions,
        action_graph_input_dict: Dict[str, Any],
        action_graph_result_doc: ActionGraphResultDoc,
        user_action_graph_result: UserActionGraphResult,
        notify: Notify,
        action_graph_node_id: Optional[str] = None,
        is_rerun_graph: bool = False,
        is_continue_run_graph: bool = False,
        is_empty_downstream_only: bool = False,
        webhook: Webhook | None = None,
    ) -> ActionGraphResultDoc | None:
        # TODO: check if action_graph_result_doc status is success, if success then return.
        # TODO: This behaviour should be accompanied by status change on action_graph_result_doc update

        # Empty Downstream Nodes if necessary
        if action_graph_node_id and is_rerun_graph:
            logger.bind(ctx=ctx ,code=AppCode.ACTION_GRAPH).info("Empty Downstream Nodes")
            await ActionGraph.empty_downstream_nodes(
                ctx=ctx,
                action_graph_node_id=action_graph_node_id,
                action_graph_result_doc=action_graph_result_doc,
                is_empty_downstream_only=is_empty_downstream_only,
            )
        elif not action_graph_node_id and is_rerun_graph:
            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).info("Empty all Nodes")
            action_graph_result_doc.result.output = {}

        # ACTION GRAPH RESULT - NODE RERUN
        if action_graph_node_id and not is_rerun_graph and not is_continue_run_graph:
            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH, action_graph_node_id=action_graph_node_id).info(
                "Action Graph Node Rerun start"
            )
            action_graph_result_doc = await ActionGraph.run_node(
                ctx=ctx,
                app_state=app_state,
                user_actions=user_actions,
                action_graph_input_dict=action_graph_input_dict,
                action_graph_result_doc=action_graph_result_doc,
                action_graph_node_id=action_graph_node_id,
                user_action_graph_result=user_action_graph_result,
                notify=notify,
                webhook=webhook,
            )
            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH, action_graph_node_id=action_graph_node_id).info(
                "Action Graph Node Rerun complete"
            )
        # ACTION GRAPH RESULT - GRAPH RUN / RERUN
        else:
            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).info("Action Graph Run/Rerun Start")
            action_graph_result_doc = await ActionGraph.run_action_graph_v2(
                ctx,
                app_state,
                user_actions,
                action_graph_input_dict,
                action_graph_result_doc,
                action_graph_node_id,
                is_continue_run_graph,
                user_action_graph_result,
                notify=notify,
                webhook=webhook,
            )
            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).info("Action Graph Run/Rerun Complete")

        if action_graph_result_doc.status == EventResultStatus.error:
            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).info("Retrying ActionGraph run")
            raise HTTPException(status_code=500, detail="Action Graph run failed")

        return action_graph_result_doc


    @staticmethod
    async def run_action_graph_v2(
        ctx: Context, 
        app_state: AppState,
        user_actions: UserActions,
        action_graph_input_dict: Dict[str, Any],
        action_graph_result_doc: ActionGraphResultDoc,
        action_graph_node_id: str | None,
        is_continue_run_graph: bool,
        user_action_graph_result: UserActionGraphResult,
        notify: Notify,
        webhook: Webhook | None = None,
    ) -> ActionGraphResultDoc:
        bind_dict = {
            "code": AppCode.ACTION_GRAPH_RUN,
            "action_graph_id": action_graph_result_doc.result.id,
            "action_graph_run_id": action_graph_result_doc.id,
        }
        logger.bind(ctx=ctx, **bind_dict).info("Action Graph Run Start")

        action_graph_result_update: ActionGraphResultUpdate = ActionGraphResultUpdate(
            result=action_graph_result_doc.result
        )
        action_graph_result_doc = await user_action_graph_result.update_action_graph_result(
            ctx=ctx,
            action_graph_result_id=action_graph_result_doc.id,
            action_graph_update=action_graph_result_update,
        )
        if webhook:
            await webhook.send(action_graph_result_doc.model_dump())

        graph_map: Optional[Dict[str, List[str]]] = action_graph_result_doc.result.graph
        inverted_map: Dict[str, List[str]] = await ActionGraph.invert_map(graph_map)
        inverted_map_subgraph = None
        logger.bind(ctx=ctx,**bind_dict).info(f"Inverted Map : {inverted_map}")
        if action_graph_node_id and is_continue_run_graph:
            graph_map = ActionGraph.get_subgraph_map(ctx=ctx,node_id=action_graph_node_id,graph=graph_map)
            logger.bind(ctx=ctx,**bind_dict).info(f"SubGraph received : {graph_map}")
            inverted_map_subgraph = await ActionGraph.invert_map(graph_map)
            logger.bind(ctx=ctx,**bind_dict).info(f"Inverted Map Subgraph : {inverted_map_subgraph}")
        node_action_map: Optional[Dict[str, str]] = action_graph_result_doc.result.nodes
        node_details_map: Dict[str, Node] = action_graph_result_doc.result.node_details
        action_response: Dict[str, ActionDoc] = action_graph_result_doc.result.output
        # action_graph_input = TextObj.model_validate(action_graph_input_dict)

        total_nodes: Set[str] = await ActionGraph.get_nodes(graph_map)
        logger.bind(ctx=ctx,**bind_dict).info(f"Total Nodes : {len(total_nodes)}")
        
        nodes_order: List[str] = coffman_graham(inverted_map_subgraph if inverted_map_subgraph else inverted_map,len(total_nodes))
        logger.bind(
            ctx=ctx,**bind_dict
        ).info(f"Nodes Order by COFFMAN GRAHAM : {nodes_order}")
        to_wait = False
        try:
            for nodes in nodes_order:
                logger.bind(
                    ctx=ctx,
                    **bind_dict,
                    count_total_nodes=len(total_nodes),
                ).info(f"Running Action Graph on nodes : {str(nodes)}")
                for node in nodes:
                    upstream_nodes = inverted_map.get(node)
                    logger.bind(
                        ctx=ctx,
                        **bind_dict,
                        count_total_nodes=len(total_nodes),
                    ).info(f"Running Action Graph on node : {node} and its upstream nodes : {upstream_nodes}")
                    if not (action_graph_node_id and is_continue_run_graph) and (await ActionGraph.is_work_done([node], action_response) or not await ActionGraph.is_work_done(upstream_nodes, action_response)):
                        logger.bind(ctx=ctx, **bind_dict, action_name=node_action_map.get(node)).info("Continuing to next Node as this Node's result is calculated or Upstream dependencies are not met")
                        continue
                    elif (action_graph_node_id and is_continue_run_graph) and node == action_graph_node_id:
                        logger.bind(ctx=ctx,**bind_dict,action_graph_node_id=action_graph_node_id).info(f"Skipping the Root Node {node} of the Subtree for continue run.")
                        continue
                    else:
                        logger.bind(ctx=ctx, **bind_dict,action_id=node_action_map.get(node)).info("Continuing exec as Node's result not yet calculated and Upstream dependencies are met")
                    # Check if user input required
                    if (node_details_map 
                        and node_details_map.get(node)
                        and node_details_map.get(node).data.user_input_required
                        and not node_details_map.get(node).data.user_input_done):
                        logger.bind(
                            ctx=ctx,
                            **bind_dict
                        ).info("This node requires User Input, not executing it.")
                        to_wait = True
                        continue 
                    
                    for upstream_node in upstream_nodes:
                        if (node_details_map
                            and node_details_map.get(upstream_node)
                            and node_details_map.get(upstream_node).data.user_review_required
                            and not node_details_map.get(upstream_node).data.user_review_done):
                            logger.bind(
                                ctx=ctx,
                                **bind_dict).info(f"Node {upstream_node} requires User Review. Since it is the parent of Node {node}, can't execute the current node.")
                            to_wait = True
                            break
                    if to_wait:
                        continue
                    node_app_state = app_state.child_context(action_graph_id=app_state.action_graph_id, action_graph_result_id=app_state.action_graph_result_id, action_graph_node_id=node,)
                    if len(upstream_nodes) == 0:
                        if node_details_map and (node_details_map.get(node).data.is_loop
                        and not node_details_map.get(node).data.file_split
                        and not node_details_map.get(node).data.excel_loop):
                            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH_RUN).info(
    "run_action_graph v2: run_action_with_text_split_and_loop_concurrently")
                            await ActionGraph.run_action_with_text_split_and_loop_concurrently(
                                ctx=ctx,
                                app_state=node_app_state,
                                bind_dict=bind_dict,
                                node=node,
                                node_details_map=node_details_map,
                                node_action_map=node_action_map,
                                action_response=action_response,
                                action_graph_result_doc=action_graph_result_doc,
                                action_graph_input_dict=action_graph_input_dict,
                                user_actions=user_actions,
                                user_action_graph_result=user_action_graph_result,
                                notify=notify,
                            )
                        elif (
                            node_details_map.get(node)
                            and node_details_map.get(node).data.excel_loop
                        ):
                            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH_RUN).info(
                                "run_action_graph v2: run_action_with_excel_loop"
                            )
                            await ActionGraph.run_action_with_excel_loop(
                                ctx=ctx,
                                app_state=node_app_state,
                                bind_dict=bind_dict,
                                node=node,
                                node_details_map=node_details_map,
                                node_action_map=node_action_map,
                                action_response=action_response,
                                action_graph_result_doc=action_graph_result_doc,
                                action_graph_input_dict=action_graph_input_dict,
                                user_actions=user_actions,
                                user_action_graph_result=user_action_graph_result,
                                notify=notify,
                            )
                        elif (
                            node_details_map.get(node)
                            and node_details_map.get(node).data.is_loop
                            and node_details_map.get(node).data.file_split
                        ):
                            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH_RUN).info(
                                "run_action_graph v2: run_action_with_file_split_and_loop_concurrently"
                            )
                            await ActionGraph.run_action_with_file_split_and_loop_concurrently(
                                ctx=ctx,
                                app_state=node_app_state,
                                bind_dict=bind_dict,
                                node=node,
                                node_details_map=node_details_map,
                                node_action_map=node_action_map,
                                action_response=action_response,
                                action_graph_result_doc=action_graph_result_doc,
                                action_graph_input_dict=action_graph_input_dict,
                                user_actions=user_actions,
                                user_action_graph_result=user_action_graph_result,
                                notify=notify,
                            )
                        else:
                            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH_RUN).info("run_action_graph v2: run_action_simple")
                            await ActionGraph.run_action_simple(
                                ctx=ctx,
                                app_state=node_app_state,
                                bind_dict=bind_dict,
                                node=node,
                                node_details_map=node_details_map,
                                node_action_map=node_action_map,
                                action_response=action_response,
                                action_graph_result_doc=action_graph_result_doc,
                                action_graph_input_dict=action_graph_input_dict,
                                user_actions=user_actions,
                                user_action_graph_result=user_action_graph_result,
                                notify=notify,
                            )
                    else:
                        # Run action with at least 1 dependency #TODO: error handling
                        if node_details_map and node_details_map.get(node).data.is_loop:
                            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH_RUN).info("run_action_graph v2: run_action_with_loop.")
                            await ActionGraph.run_action_with_loop(
                                ctx=ctx,
                                app_state=node_app_state,
                                bind_dict=bind_dict,
                                node=node,
                                upstream_nodes=upstream_nodes,
                                node_details_map=node_details_map,
                                node_action_map=node_action_map,
                                action_response=action_response,
                                action_graph_result_doc=action_graph_result_doc,
                                action_graph_input_dict=action_graph_input_dict,
                                action_graph_node_id=action_graph_node_id,
                                is_continue_run_graph=is_continue_run_graph,
                                user_actions=user_actions,
                                user_action_graph_result=user_action_graph_result,
                                notify=notify,
                            )
                        else:
                            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH_RUN).info("run_action_graph v2: run_action_with_merge")
                            await ActionGraph.run_action_with_merge(
                                ctx=ctx,
                                app_state=node_app_state,
                                bind_dict=bind_dict,
                                node=node,
                                upstream_nodes=upstream_nodes,
                                node_details_map=node_details_map,
                                node_action_map=node_action_map,
                                action_response=action_response,
                                action_graph_result_doc=action_graph_result_doc,
                                action_graph_input_dict=action_graph_input_dict,
                                action_graph_node_id=action_graph_node_id,
                                is_continue_run_graph=is_continue_run_graph,
                                user_actions=user_actions,
                                user_action_graph_result=user_action_graph_result,
                                notify=notify,
                            )

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
                count_total_nodes=len(total_nodes),
                **bind_dict,
            ).info(
                "Exited Action Graph Run for Loop"
            )

            # Update action result graph as success or waiting
            action_graph_result_update: ActionGraphResultUpdate | None
            logger.bind(ctx=ctx,**bind_dict).info(f"To wait : {to_wait}")
            if not to_wait:
                logger.bind(ctx=ctx,**bind_dict).info("Action Graph Status : Success")
                action_graph_result_update: ActionGraphResultUpdate = ActionGraphResultUpdate(status=EventResultStatus.success)
            else:
                logger.bind(ctx=ctx, **bind_dict).info("Action Graph Status : Waiting")
                action_graph_result_update: ActionGraphResultUpdate = ActionGraphResultUpdate(status=EventResultStatus.waiting)
            
            action_graph_result_doc = await user_action_graph_result.update_action_graph_result(ctx, action_graph_result_doc.id, action_graph_result_update)
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
    def get_subgraph_map(ctx: Context,node_id: str, graph: Optional[Dict[str, List[str]]] = None):
        if not graph:
            return graph
        subgraph = {}
        q = deque()
        q.append(node_id)
        while q:
            fst = q.popleft()
            children = graph.get(fst)
            if not children:
                continue
            subgraph[fst]= children
            for child in children:
                q.append(child)
        logger.bind(ctx=ctx).info(f"Subgraph : {subgraph}")
        return subgraph
        
        

    @staticmethod
    async def run_node(
        ctx: Context,
        app_state: AppState,
        user_actions: UserActions,
        action_graph_input_dict: Dict[str, Any],
        action_graph_result_doc: ActionGraphResultDoc,
        action_graph_node_id: str,
        user_action_graph_result: UserActionGraphResult,
        notify: Notify,
        webhook: Webhook | None = None,
    ) -> ActionGraphResultDoc:
        logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH_RUN).info("Starting Run Node")
        action_response: Dict[str, ActionDoc] = action_graph_result_doc.result.output

        # Run action with no dependency
        sleep_time = ActionGraph.get_node_sleep_time(
            action_graph_node_id, action_graph_result_doc.result.node_details
        )
        if sleep_time > 0:
            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH_RUN, node_id=action_graph_node_id, sleep_time=sleep_time).info(
                "Sleeping before node rerun"
            )
            await asyncio.sleep(sleep_time)

        node_action_doc = action_response.get(action_graph_node_id)

        logger.bind(
            ctx=ctx,
            code=AppCode.ACTION_GRAPH_RUN,
            graph_id=action_graph_result_doc.result.id,
            graph_run_id=action_graph_result_doc.id,
            action_id=node_action_doc.id,
        ).info(f"Started Run Node: {action_graph_node_id}")
        try:
            action_graph_result_update: ActionGraphResultUpdate = ActionGraphResultUpdate(
                        result=action_graph_result_doc.result, status=EventResultStatus.processing
                    )
            action_graph_result_doc = await user_action_graph_result.update_action_graph_result(
                ctx, action_graph_result_doc.id, action_graph_result_update
            )
            await ActionGraph.run_node_action_with_existing_doc(
                ctx=ctx,
                app_state=app_state,
                action_response=action_response,
                node=action_graph_node_id,
                action_graph_input_dict=action_graph_input_dict,
                action_graph_result_doc=action_graph_result_doc,
                user_actions=user_actions,
                user_action_graph_result=user_action_graph_result,
                notify=notify,
                webhook=webhook,
                run_single_node=True,
            )
            action_graph_result_update: ActionGraphResultUpdate = ActionGraphResultUpdate(
                        result=action_graph_result_doc.result, status=EventResultStatus.success
                    )
            action_graph_result_doc = await user_action_graph_result.update_action_graph_result(
                        ctx, action_graph_result_doc.id, action_graph_result_update
                    )
        except Exception as e:
            logger.bind(
                ctx=ctx,
                code=AppCode.ACTION_GRAPH_RUN,
                graph_id=action_graph_result_doc.result.id,
                graph_run_id=action_graph_result_doc.id,
                action_id=node_action_doc.id,
            ).exception(f"Error in rerun node of Action Graph: {str(e)}")
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
                code=AppCode.ACTION_GRAPH_RUN,
                graph_id=action_graph_result_doc.result.id,
                graph_run_id=action_graph_result_doc.id,
                action_id=node_action_doc.id,
            ).info("Action Graph Node Run Completed")
            return action_graph_result_doc

    @staticmethod
    async def run_action(
        ctx: Context,
        app_state: AppState,
        user_actions: UserActions,
        action_id: str,
        action_graph_input_dict: Dict[str, Any],
        notify: Notify,
    ) -> ActionDoc | Exception:
        logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH_RUN, action_id=action_id).info("ActionGraph Run action")
        # TODO: update code to Find owned action of shared action. Then call UserActions.run_action_doc()
        # NOTE: await UserActions.run_action_doc() does not persist state like run_action_v1, so
        # DO: user_actions.update_action()
        max_attachment_retries = 3
        last_llm_output: str = ""
        for attempt in range(1, max_attachment_retries + 1):
            try:
                action = await user_actions.get_action(action_id=action_id)
                is_action_found = True if action else False
                if action_id == "" or is_action_found:
                    notify(ctx, f"Running action: {action.name}") if notify and action else None
                    action_result = await user_actions.run_action_v1(
                        ctx,
                        app_state,
                        action_id,
                        action_graph_input_dict,
                        notify=notify,
                    )
                    logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH_RUN, action_id=action_id).info("Ran User Action")
                    return action_result

                shared_action = await user_actions.get_shared_action(
                    id=action_id, empty_shared_action_config=False
                )
                is_shared_found = True if shared_action else False
                if is_shared_found:
                    notify(
                        ctx, f"Running action: {shared_action.name}"
                    ) if notify and shared_action else None
                    action_result = await user_actions.run_action_doc(
                        ctx=ctx,
                        app_state=app_state,
                        action_doc=shared_action,
                        input=action_graph_input_dict,
                        user=user_actions.user,
                        db=user_actions.db,
                        notify=notify,
                    )
                    logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH_RUN, action_id=action_id).info("Ran Shared Action")
                    return action_result

                system_action = await user_actions.get_system_action(ctx=ctx, id=action_id, empty_shared_action_config=False)
                if system_action:
                    notify(
                        ctx, f"Running action: {system_action.name}"
                    ) if notify and system_action else None
                    action_result = await user_actions.run_action_doc(
                        ctx=ctx,
                        app_state=app_state,
                        action_doc=system_action,
                        input=action_graph_input_dict,
                        user=user_actions.user,
                        db=user_actions.db,
                        notify=notify,
                    )
                    logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH_RUN, action_id=action_id).info("Ran System Action")
                    return action_result

                if not is_action_found and not is_shared_found:
                    logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH_RUN, action_id=action_id).error(f"Action {action_id} not found")
                    return HTTPException(404, "Action not found")

            except HTTPException as e:
                detail = e.detail
                if isinstance(detail, dict):
                    is_attachment_failure = detail.get("message") == "Attachments not matched"
                    last_llm_output = detail.get("llm_output") or ""
                else:
                    is_attachment_failure = "Attachments not matched" in str(detail)
                if is_attachment_failure and attempt < max_attachment_retries:
                    logger.bind(
                        ctx=ctx,
                        code=AppCode.ACTION_GRAPH_RUN,
                        action_id=action_id,
                        attempt=attempt,
                        max_attachment_retries=max_attachment_retries,
                    ).info("Attachment not matched, retrying")
                    await asyncio.sleep(1)
                    continue
                if is_attachment_failure:
                    logger.bind(
                        ctx=ctx,
                        code=AppCode.ACTION_GRAPH_RUN,
                        action_id=action_id,
                        max_attachment_retries=max_attachment_retries,
                    ).error("Attachment not matched after max retries, giving up")
                    user_message = (
                        f"The AI model did not generate the expected file(s) after "
                        f"{max_attachment_retries} attempts. This can happen when the model "
                        "skips file generation. Please try running this action again."
                    )
                    if last_llm_output:
                        user_message += f"\n\nLast model output:\n{last_llm_output}"
                    return HTTPException(500, user_message)
                else:
                    logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH_RUN, action_id=action_id).exception(
                        f"Error in Run Action: {str(e)}"
                    )
                return HTTPException(500, f"Error in Run Action: {str(e)}")
        return HTTPException(500, "Error in Run Action: unexpected exit from retry loop")

    @staticmethod
    async def get_action(
        user_actions: UserActions,
        action_id: str,
        ctx: Context | None = None,
    ) -> ActionDoc:
        action = await user_actions.get_action(action_id)
        if action:
            return action
        action = await user_actions.get_shared_action(
            id=action_id, empty_shared_action_config=False
        )
        if action:
            return action
        if ctx:
            action = await user_actions.get_system_action(
                ctx=ctx, id=action_id, empty_shared_action_config=False
            )
            if action:
                return action
        raise HTTPException(404, "Action not found")

    @staticmethod
    def get_node_sleep_time(node: str, node_details_map: Dict[str, Node] | None) -> float:
        if not node_details_map:
            return 0.0
        node_details = node_details_map.get(node)
        if not node_details or not node_details.data:
            return 0.0
        return float(node_details.data.sleep_time or 0.0)

    @staticmethod
    async def sleep_for_node(
        ctx: Context,
        bind_dict: Dict[str, str],
        node: str,
        node_details_map: Dict[str, Node] | None,
    ) -> None:
        if not node_details_map:
            return
        node_details = node_details_map.get(node)
        if not node_details or not node_details.data:
            return
        sleep_time = node_details.data.sleep_time or 0
        if sleep_time <= 0:
            return
        logger.bind(ctx=ctx, **bind_dict, sleep_time=sleep_time).info(
            "Sleeping before node execution"
        )
        await asyncio.sleep(sleep_time)

    @staticmethod
    async def run_action_doc(
        ctx: Context,
        app_state:AppState,
        user_actions: UserActions,
        action_doc: ActionDoc,
        action_graph_input_dict: Dict[str, Any],
        user: UserORM,
        notify: Notify,
    ) -> ActionDoc:
        try:
            action_doc = await user_actions.run_action_doc(
                ctx=ctx,
                app_state=app_state,
                action_doc=action_doc,
                input=action_graph_input_dict,
                user=user,
                db=user_actions.db,
                notify=notify,
            )
            return action_doc
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH_RUN).exception(f"Error in run_action_doc {str(e)}")
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
    async def collate_action_results(
        ctx: Context, action_results: List[ActionDoc | Exception], node: str | None = None, action_graph_node_id: str | None = None, action_response : Dict[str, ActionDoc] | None = None, is_continue_run_graph: bool | None = None,
    ) -> ActionDoc:
        assert len(action_results) > 0
        results = copy.deepcopy(action_results[0])
        results.results = copy.deepcopy(action_results[0].results)
        logger.bind(ctx=ctx).info(f"Length of action results : {len(action_results)}")
        for i, action_result in enumerate(action_results[1:]):
            match action_result:
                case ActionDoc():
                    results.input[f"text_{i}"] = action_result.input["text"]
                    # logger.bind(ctx=ctx).info(f"Results output before : {results.output}")
                    results.output["blocks"] += action_result.output["blocks"]
                    # logger.bind(ctx=ctx).info(f"Results output after : {results.output}")
                    if not (action_graph_node_id and is_continue_run_graph):
                        results.results += action_result.results
                    else:
                        results.results.append(
                            ActionResult(
                                input=action_result.input,
                                output=action_result.output,
                            )
                        )
                case Exception():
                    logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH_RUN).error(
                        f"Exception object found in collate_action_results: {repr(action_result)[:100]}"
                    )
                    results.input[f"text_{i}"] = str(action_result)
                    results.output["blocks"] += [DataBlock(text=str(action_result))]
                    # results.results += action_result.results
                case None:
                    logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH_RUN).error(
                        f"None value found in collate_action_results: {repr(action_result)[:100]}"
                    )
                    results.input[f"text_{i}"] = "No Response"
                    results.output["blocks"] += [DataBlock(text="No Response")]
                    # results.results += action_result.results
        return results

    @staticmethod
    async def gen_input(
        ctx: Context,
        app_state: AppState,
        db: AsyncIOMotorDatabase,
        user: UserORM,
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
                response_format=ResponseFormatJSONObject(type="json_object"),
            )
            run_input = DataBlock(text=llm_input)
            data_blocks: DataBlocks = await ActionText2TextLlmChatOpenai(
                action_config=chat_req, db=db, user=user
            ).run_action(ctx, app_state, run_input)
            output_json = data_blocks.blocks[0].text
            try:
                json_dict = json.loads(output_json)
            except Exception as e:
                logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH_RUN, current_node_action_type=current_node_action_type).exception(
                    f"Cannot convert to Input: {str(e)}"
                )
                json_dict = output_json
            curr_action_input_obj = curr_action_input_type.model_validate(json_dict)
            logger.bind(
                ctx=ctx,
                code=AppCode.ACTION_GRAPH_RUN,
                current_node_action_type=current_node_action_type,
                action_input=curr_action_input_obj,
            ).info("Used Llm to generate input by using outputs of upstream nodes")
            return curr_action_input_obj
            # return json_dict
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH_RUN).error(f"Cannot generate Input: {str(e)}")
            return {}

    @staticmethod
    async def empty_downstream_nodes(
        ctx: Context,
        action_graph_node_id: str,
        action_graph_result_doc: ActionGraphResultDoc,
        is_empty_downstream_only: bool = False,
    ):
        logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH_RUN).info("Emptying downstream nodes")
        to_visit = [action_graph_node_id]
        while to_visit:
            try:
                node = to_visit.pop(0)
                node_details = action_graph_result_doc.result.node_details.get(node)
                # Empty downstream nodes only
                # if node_details.data.actionId and node is not action_graph_node_id:
                #     action_graph_result_doc.result.output.pop(node)
                # Empty current node as well as downstream nodes
                if is_empty_downstream_only:
                    if node_details.data.actionId and node is not action_graph_node_id:
                        if action_graph_result_doc.result.output.__contains__(node):
                            action_graph_result_doc.result.output.pop(node)
                else:
                    if node_details.data.actionId:
                        if action_graph_result_doc.result.output.__contains__(node):
                            action_graph_result_doc.result.output.pop(node)

                downstream_nodes = action_graph_result_doc.result.graph.get(node, [])
                to_visit += downstream_nodes
            except Exception as e:
                logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH_RUN, current_node_action_type=action_graph_node_id).exception(
                    f"Error in emptying downstream nodes{str(e)}"
                )

    @staticmethod
    async def run_action_with_text_split_and_loop(
        ctx: Context,
        app_state: AppState,
        bind_dict: Dict[str, str],
        node: str,
        node_details_map: Dict[str, Node],
        node_action_map: Dict[str, str],
        action_response: Dict[str, ActionDoc],
        action_graph_result_doc: ActionGraphResultDoc,
        action_graph_input_dict: Dict[str, Any],
        user_actions: UserActions,
        user_action_graph_result: UserActionGraphResult,
        notify: Notify,
    ):
        # run action in loop
        inter_action_result = []
        multiple_objs = action_graph_input_dict["text"].split(
            node_details_map.get(node).data.seperator
        )

        sleep_time = ActionGraph.get_node_sleep_time(node, node_details_map)
        for idx, multiple in enumerate(multiple_objs):
            if idx > 0 and sleep_time > 0:
                await asyncio.sleep(sleep_time)
            data_block = DataBlock(
                text=multiple,
                metadata=Metadata(
                    process_url_in_text=node_details_map.get(node).data.process_url_in_text,
                    process_attachment_url=node_details_map.get(node).data.process_attachment_url,
                    attachment_requested=node_details_map.get(node).data.attachment_requested,
                ),
            )

            iter_app_state = app_state.child_context()
            _action_result: ActionDoc = await ActionGraph.run_action(
                ctx,
                iter_app_state,
                user_actions,
                node_action_map.get(node),
                data_block.model_dump(),
                notify=notify,
            )
            match _action_result:
                case Exception():
                    logger.bind(ctx=ctx, **bind_dict).exception(
                        f"Error in loop run action {str(_action_result)}"
                    )
                case ActionDoc():
                    inter_action_result.append(_action_result)
                    action_result = await ActionGraph.collate_action_results(
                        ctx=ctx, action_results=inter_action_result
                    )
                    action_response[node] = ActionDoc.model_validate(action_result)
                    action_graph_result_doc.result.output[node] = action_response[node]
                    action_graph_result_update: ActionGraphResultUpdate = ActionGraphResultUpdate(
                        result=action_graph_result_doc.result
                    )
                    action_graph_result_doc = (
                        await user_action_graph_result.update_action_graph_result(
                            ctx, action_graph_result_doc.id, action_graph_result_update
                        )
                    )

    @staticmethod
    async def run_action_with_text_split_and_loop_concurrently(
        ctx: Context,
        app_state: AppState,
        bind_dict: Dict[str, str],
        node: str,
        node_details_map: Dict[str, Node],
        node_action_map: Dict[str, str],
        action_response: Dict[str, ActionDoc],
        action_graph_result_doc: ActionGraphResultDoc,
        action_graph_input_dict: Dict[str, Any],
        user_actions: UserActions,
        user_action_graph_result: UserActionGraphResult,
        notify: Notify,
    ):
        # run action in loop
        action_inputs: List[DataBlock] = []
        separator = "|"
        if (
            node_details_map.get(node)
            and node_details_map.get(node).data
            and node_details_map.get(node).data.seperator
        ):
            separator = node_details_map.get(node).data.seperator
        multiple_objs = action_graph_input_dict["text"].split(separator)
        for multiple in multiple_objs:
            data_block = DataBlock(
                text=multiple,
                metadata=Metadata(
                    process_url_in_text=node_details_map.get(node).data.process_url_in_text,
                    process_attachment_url=node_details_map.get(node).data.process_attachment_url,
                    attachment_requested=node_details_map.get(node).data.attachment_requested,
                ),
            )
            action_inputs.append(data_block)

        await ActionGraph.run_action_with_loop_concurrently(
            ctx=ctx,
            app_state=app_state,
            bind_dict=bind_dict,
            node=node,
            upstream_nodes=[],
            node_details_map=node_details_map,
            node_action_map=node_action_map,
            action_response=action_response,
            action_graph_result_doc=action_graph_result_doc,
            action_graph_input_dict=action_graph_input_dict,
            action_graph_node_id=None,
            is_continue_run_graph=False,
            user_actions=user_actions,
            user_action_graph_result=user_action_graph_result,
            action_inputs=action_inputs,
            notify=notify,
        )

    @staticmethod
    async def run_action_with_excel_loop(
        ctx: Context,
        app_state: AppState,
        bind_dict: Dict[str, str],
        node: str,
        node_details_map: Dict[str, Node],
        node_action_map: Dict[str, str],
        action_response: Dict[str, ActionDoc],
        action_graph_result_doc: ActionGraphResultDoc,
        action_graph_input_dict: Dict[str, Any],
        user_actions: UserActions,
        user_action_graph_result: UserActionGraphResult,
        notify: Notify,
    ):
        logger.bind(ctx=ctx, **bind_dict).info("Running action with excel loop")
        # run action in loop
        inter_action_result = []
        urls = list(action_graph_input_dict.get("urls") or [])
        text = action_graph_input_dict.get("text") or ""
        if not urls and not text:
            raise HTTPException(
                status_code=400,
                detail="excel_loop requires urls or text containing spreadsheet URL(s) in input",
            )
        spreadsheet_url = pick_spreadsheet_url(urls, text=text)
        batches = split_excel_into_batches(
            spreadsheet_url, 1, header=node_details_map.get(node).data.header
        )

        sleep_time = ActionGraph.get_node_sleep_time(node, node_details_map)
        for idx, batch in enumerate(batches):
            if idx > 0 and sleep_time > 0:
                await asyncio.sleep(sleep_time)
            logger.bind(ctx=ctx, **bind_dict).info(f"Excel loop: Batch {idx + 1} of {len(batches)}")
            try:
                if node_details_map.get(node).data.data_processing:
                    logger.bind(ctx=ctx, **bind_dict).info("Running data processing")
                    # Process all columns from the row, not just first column
                    row_data = batch.to_dict(orient="records")[0]
                    data_block = DataBlock(
                        text=f"{action_graph_input_dict['text']}\n\nData: {json.dumps(row_data)}",
                        metadata=Metadata(
                            process_url_in_text=node_details_map.get(node).data.process_url_in_text,
                            process_attachment_url=node_details_map.get(
                                node
                            ).data.process_attachment_url,
                            attachment_requested=node_details_map.get(
                                node
                            ).data.attachment_requested,
                        ),
                    )
                elif node_details_map.get(node).data.file_processing:
                    logger.bind(ctx=ctx, **bind_dict).info("Running file processing")
                    # Process files with instructions
                    instruction = batch.iloc[0][
                        node_details_map.get(node).data.instruction_column_index
                    ]
                    data_block = DataBlock(
                        text=f"{action_graph_input_dict['text']}\n\nInstruction: {instruction}",
                        urls=[batch.iloc[0][node_details_map.get(node).data.file_column_index]],
                        metadata=Metadata(
                            process_url_in_text=node_details_map.get(node).data.process_url_in_text,
                            process_attachment_url=node_details_map.get(
                                node
                            ).data.process_attachment_url,
                            attachment_requested=node_details_map.get(
                                node
                            ).data.attachment_requested,
                        ),
                    )
                else:
                    logger.bind(ctx=ctx, **bind_dict).info("Running default processing")
                    # Default behavior
                    data_block_text = action_graph_input_dict["text"]
                    data_block_text = f"{data_block_text}"
                    data_block_urls = [
                        batch.iloc[0][node_details_map.get(node).data.file_column_index]
                    ]
                    data_block = DataBlock(
                        text=data_block_text,
                        urls=data_block_urls,
                        metadata=Metadata(
                            process_url_in_text=node_details_map.get(node).data.process_url_in_text,
                            process_attachment_url=node_details_map.get(
                                node
                            ).data.process_attachment_url,
                            attachment_requested=node_details_map.get(
                                node
                            ).data.attachment_requested,
                        ),
                    )

                iter_app_state = app_state.child_context()
                _action_result: ActionDoc = await ActionGraph.run_action(
                    ctx,
                    iter_app_state,
                    user_actions,
                    node_action_map.get(node),
                    data_block.model_dump(),
                    notify=notify,
                )
                match _action_result:
                    case Exception():
                        logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH_RUN).exception(f"Error in row {idx + 1} run action")
                    case ActionDoc():
                        inter_action_result.append(_action_result)
                        action_result = await ActionGraph.collate_action_results(
                            ctx=ctx, action_results=inter_action_result
                        )
                        action_response[node] = ActionDoc.model_validate(action_result)
                        action_graph_result_doc.result.output[node] = action_response[node]
                        action_graph_result_update: ActionGraphResultUpdate = (
                            ActionGraphResultUpdate(result=action_graph_result_doc.result)
                        )
                        action_graph_result_doc = (
                            await user_action_graph_result.update_action_graph_result(
                                ctx, action_graph_result_doc.id, action_graph_result_update
                            )
                        )
            except Exception:
                logger.bind(ctx=ctx, **bind_dict).exception(
                    f"Error in processing batch of excel row {idx + 1} run action"
                )

    @staticmethod
    async def run_action_with_file_split_and_loop(
        ctx: Context,
        app_state: AppState,
        bind_dict: Dict[str, str],
        node: str,
        node_details_map: Dict[str, Node],
        node_action_map: Dict[str, str],
        action_response: Dict[str, ActionDoc],
        action_graph_result_doc: ActionGraphResultDoc,
        action_graph_input_dict: Dict[str, Any],
        user_actions: UserActions,
        user_action_graph_result: UserActionGraphResult,
        notify: Notify,
    ):
        inter_action_result = []
        multiple_objs = action_graph_input_dict["urls"]

        sleep_time = ActionGraph.get_node_sleep_time(node, node_details_map)
        for idx, multiple in enumerate(multiple_objs):
            if idx > 0 and sleep_time > 0:
                await asyncio.sleep(sleep_time)
            data_block = DataBlock(
                text=action_graph_input_dict["text"],
                urls=[multiple],
                metadata=Metadata(
                    process_url_in_text=node_details_map.get(node).data.process_url_in_text,
                    process_attachment_url=node_details_map.get(node).data.process_attachment_url,
                    attachment_requested=node_details_map.get(node).data.attachment_requested,
                ),
            )
            iter_app_state = app_state.child_context()
            _action_result: ActionDoc = await ActionGraph.run_action(
                ctx, iter_app_state, user_actions, node_action_map.get(node), data_block.model_dump(), notify=notify
            )
            match _action_result:
                case Exception():
                    logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH_RUN).exception("Error in loop run action")
                case ActionDoc():
                    inter_action_result.append(_action_result)
                    action_result = await ActionGraph.collate_action_results(
                        ctx=ctx, action_results=inter_action_result
                    )
                    action_response[node] = ActionDoc.model_validate(action_result)
                    action_graph_result_doc.result.output[node] = action_response[node]
                    action_graph_result_update: ActionGraphResultUpdate = ActionGraphResultUpdate(
                        result=action_graph_result_doc.result
                    )
                    action_graph_result_doc = (
                        await user_action_graph_result.update_action_graph_result(
                            ctx, action_graph_result_doc.id, action_graph_result_update
                        )
                    )

    @staticmethod
    async def run_action_with_file_split_and_loop_concurrently(
        ctx: Context,
        app_state: AppState,
        bind_dict: Dict[str, str],
        node: str,
        node_details_map: Dict[str, Node],
        node_action_map: Dict[str, str],
        action_response: Dict[str, ActionDoc],
        action_graph_result_doc: ActionGraphResultDoc,
        action_graph_input_dict: Dict[str, Any],
        user_actions: UserActions,
        user_action_graph_result: UserActionGraphResult,
        notify: Notify,
    ):
        action_inputs: List[DataBlock] = []
        urls = action_graph_input_dict["urls"]
        if urls:
            for url in urls:
                data_block = DataBlock(
                    text=action_graph_input_dict["text"],
                    urls=[url],
                    metadata=Metadata(
                        process_url_in_text=node_details_map.get(node).data.process_url_in_text,
                        process_attachment_url=node_details_map.get(
                            node
                        ).data.process_attachment_url,
                        attachment_requested=node_details_map.get(node).data.attachment_requested,
                    ),
                )
                action_inputs.append(data_block)
        else:
            raise HTTPException(404, "No urls with File loop")

        await ActionGraph.run_action_with_loop_concurrently(
            ctx=ctx,
            app_state=app_state,
            bind_dict=bind_dict,
            node=node,
            upstream_nodes=[],
            node_details_map=node_details_map,
            node_action_map=node_action_map,
            action_response=action_response,
            action_graph_result_doc=action_graph_result_doc,
            action_graph_input_dict=action_graph_input_dict,
            action_graph_node_id=None,
            is_continue_run_graph=False,
            user_actions=user_actions,
            user_action_graph_result=user_action_graph_result,
            action_inputs=action_inputs,
            notify=notify,
        )

    @staticmethod
    async def run_action_simple(
        ctx: Context,
        app_state: AppState,
        bind_dict: Dict[str, str],
        node: str,
        node_details_map: Dict[str, Node],
        node_action_map: Dict[str, str],
        action_response: Dict[str, ActionDoc],
        action_graph_result_doc: ActionGraphResultDoc,
        action_graph_input_dict: Dict[str, Any],
        user_actions: UserActions,
        user_action_graph_result: UserActionGraphResult,
        notify: Notify,
    ):
        await ActionGraph.sleep_for_node(ctx, bind_dict, node, node_details_map)
        action_result: ActionDoc = await ActionGraph.run_action(
            ctx, app_state, user_actions, node_action_map.get(node), action_graph_input_dict, notify=notify
        )
        # action_result = await user_actions.run_action_v1(node_action_map.get(node), action_graph_input.model_dump())
        match action_result:
            case Exception():
                logger.bind(ctx=ctx, **bind_dict).exception(
                    f"Error in ActionGraph run_action: {repr(action_result)[:100]}"
                )
                raise action_result
            case ActionDoc():
                action_response[node] = ActionDoc.model_validate(action_result)
        action_response[node] = ActionDoc.model_validate(action_result)

    @staticmethod
    async def run_action_with_loop(
        ctx: Context,
        app_state: AppState,
        bind_dict: Dict[str, str],
        node: str,
        upstream_nodes: List[str],
        node_details_map: Dict[str, Node],
        node_action_map: Dict[str, str],
        action_response: Dict[str, ActionDoc],
        action_graph_result_doc: ActionGraphResultDoc,
        action_graph_input_dict: Dict[str, Any],
        action_graph_node_id: str | None,
        is_continue_run_graph: bool,
        user_actions: UserActions,
        user_action_graph_result: UserActionGraphResult,
        notify: Notify,
    ):
        curr_action = await ActionGraph.get_action(user_actions, node_action_map.get(node), ctx=ctx)
        separator = "|"
        if (
            node_details_map.get(node)
            and node_details_map.get(node).data
            and node_details_map.get(node).data.seperator
        ):
            separator = node_details_map.get(node).data.seperator
        action_inputs = await ActionGraphInputCreator.to_input(
            ctx,
            app_state=app_state,
            db=user_actions.db,
            user=user_actions.user,
            upstream_nodes=upstream_nodes,
            current_node_action=curr_action,
            action_response=action_response,
            separator=separator,
            data_flow_type=node_details_map.get(node).data.data_flow_type,
            is_loop=node_details_map.get(node).data.is_loop,
            api_loop=node_details_map.get(node).data.api_loop,
            batches=node_details_map.get(node).data.batches,
            excel_loop=node_details_map.get(node).data.excel_loop,
            file_column_index=node_details_map.get(node).data.file_column_index,
            is_split=node_details_map.get(node).data.is_split,
            file_split=node_details_map.get(node).data.file_split,
            header=node_details_map.get(node).data.header,
            data_processing=node_details_map.get(node).data.data_processing,
            instruction_column_index=node_details_map.get(node).data.instruction_column_index,
            file_processing=node_details_map.get(node).data.file_processing,
            process_url_in_text=node_details_map.get(node).data.process_url_in_text,
            process_attachment_url=node_details_map.get(node).data.process_attachment_url,
            attachment_requested=node_details_map.get(node).data.attachment_requested,
        )

        # await ActionGraph.run_action_with_loop_synchronously(
        await ActionGraph.run_action_with_loop_concurrently(
            ctx=ctx,
            app_state=app_state,
            bind_dict=bind_dict,
            node=node,
            upstream_nodes=upstream_nodes,
            node_details_map=node_details_map,
            node_action_map=node_action_map,
            action_response=action_response,
            action_graph_result_doc=action_graph_result_doc,
            action_graph_input_dict=action_graph_input_dict,
            action_graph_node_id=action_graph_node_id,
            is_continue_run_graph=is_continue_run_graph,
            user_actions=user_actions,
            user_action_graph_result=user_action_graph_result,
            action_inputs=action_inputs,
            notify=notify,
        )

    @staticmethod
    async def run_action_with_merge(
        ctx: Context,
        app_state: AppState,
        bind_dict: Dict[str, str],
        node: str,
        upstream_nodes: List[str],
        node_details_map: Dict[str, Node],
        node_action_map: Dict[str, str],
        action_response: Dict[str, ActionDoc],
        action_graph_result_doc: ActionGraphResultDoc,
        action_graph_input_dict: Dict[str, Any],
        action_graph_node_id: str | None,
        is_continue_run_graph: bool,
        user_actions: UserActions,
        user_action_graph_result: UserActionGraphResult,
        notify: Notify,
    ):
        # run action in merge
        await ActionGraph.sleep_for_node(ctx, bind_dict, node, node_details_map)
        curr_action = await ActionGraph.get_action(user_actions, node_action_map.get(node), ctx=ctx)
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

        meta_data = Metadata()
        if node_details_map.get(node) and node_details_map.get(node).data:
            node_data = node_details_map.get(node).data
            meta_data.process_url_in_text = node_data.process_url_in_text
            meta_data.process_attachment_url = node_data.process_attachment_url
            meta_data.attachment_requested = node_data.attachment_requested

        action_input = await ActionGraphInputCreator.to_input(
            ctx=ctx,
            app_state=app_state,
            db=user_actions.db,
            user=user_actions.user,
            upstream_nodes=upstream_nodes,
            current_node_action=curr_action,
            action_response=action_response,
            separator=separator,
            data_flow_type=node_details_map.get(node).data.data_flow_type,
            process_url_in_text=meta_data.process_url_in_text,
            process_attachment_url=meta_data.process_attachment_url,
            attachment_requested=meta_data.attachment_requested,
        )
        if not (action_graph_node_id and is_continue_run_graph):    
            action_result: ActionDoc = await ActionGraph.run_action(
                ctx, app_state, user_actions, node_action_map.get(node), action_input.model_dump(), notify=notify
            )
        else:
            action_result = await ActionGraph.run_node_action_with_existing_doc(
                ctx=ctx,
                app_state=app_state,
                action_response=action_response,
                node=node,
                action_graph_input_dict=action_input.model_dump(),
                action_graph_result_doc=action_graph_result_doc,
                user_actions=user_actions,
                user_action_graph_result=user_action_graph_result,
                notify=notify,
            )
        # action_result = await user_actions.run_action_v1(node_action_map.get(node), action_input.model_dump())
        match action_result:
            case Exception():
                logger.bind(ctx=ctx, **bind_dict).exception(
                    f"Error in ActionGraph run_action: {repr(action_result)[:100]}"
                )
                raise action_result
            case ActionDoc():
                action_response[node] = ActionDoc.model_validate(action_result)

        memory_profile = await Profiler.do_memory_profile(ctx)
        logger.bind(ctx=ctx, **bind_dict, memory_profile=memory_profile).info(
            f"Memory profile for Action run {action_result.name}:{action_result.version}"
        )

    @staticmethod
    async def run_node_action_with_existing_doc(
        ctx: Context,
        app_state: AppState,
        action_response: Dict[str, ActionDoc],
        node: str,
        action_graph_input_dict: Dict[str, Any],
        action_graph_result_doc: ActionGraphResultDoc,
        user_actions: UserActions,
        user_action_graph_result: UserActionGraphResult,
        notify: Notify | None = None,
        webhook: Webhook| None = None,
        run_single_node: bool | None = None,
    ) -> ActionDoc:
        # node_action_doc = action_response.get(node)
        node_action_doc = copy.deepcopy(action_graph_result_doc.result.output[node])
        node_action_doc.input = action_graph_input_dict
        node_action_doc.output = {}  # Removing output from previous run
        # logger.bind(ctx=ctx).info(f"Action Graph Input Dict : {action_graph_input_dict}")
        logger.bind(ctx=ctx).info(f"Node Action Doc Name : {node_action_doc.name}")
        node_action_doc.results.append(
            ActionResult(input=action_graph_input_dict)
        )  # updating node_action_doc for initial save, could have created and used a deepcopy of node_action_doc
        if run_single_node:
            action_graph_result_doc.result.output[node] = node_action_doc
        # save
        
        if webhook:
            await webhook.send(action_graph_result_doc.model_dump())

        node_action_doc.results.pop(-1)  # bringing node_action_doc to previous state
        # dont care about node_action_doc.input and node_action_doc.output yet as it is not used for running an action
        action_result: ActionDoc = await ActionGraph.run_action_doc(  # noqa F841
            ctx=ctx,
            app_state=app_state,
            user_actions=user_actions,
            action_doc=node_action_doc,
            action_graph_input_dict=action_graph_input_dict,
            user=user_actions.user,
            notify=notify,
        )
        # logger.bind(ctx=ctx).info(f"Action Result : {action_result}")
        # Update action result graph result
        
        # save
        
        return action_result
    
    
    @staticmethod
    async def run_action_with_loop_synchronously(
        ctx: Context,
        app_state: AppState,
        bind_dict: Dict[str, str],
        node: str,
        upstream_nodes: List[str],
        node_details_map: Dict[str, Node],
        node_action_map: Dict[str, str],
        action_response: Dict[str, ActionDoc],
        action_graph_result_doc: ActionGraphResultDoc,
        action_graph_input_dict: Dict[str, Any],
        user_actions: UserActions,
        user_action_graph_result: UserActionGraphResult,
        action_inputs: List[DataBlock],
        notify: Notify,
    ):
        inter_action_result = []

        sleep_time = ActionGraph.get_node_sleep_time(node, node_details_map)
        for idx, action_input in enumerate(action_inputs):
            if idx > 0 and sleep_time > 0:
                await asyncio.sleep(sleep_time)
            try:
                action_input = DataBlock.model_validate(action_input)
                iter_app_state = app_state.child_context()
                action_result: ActionDoc | Exception = await ActionGraph.run_action(
                    ctx,
                    iter_app_state,
                    user_actions,
                    node_action_map.get(node),
                    action_input.model_dump(),
                    notify=notify,
                )
                match action_result:
                    case Exception():
                        logger.bind(ctx=ctx, **bind_dict).exception(
                            f"Error in loop run action {repr(action_result)[:100]}"
                        )
                    case ActionDoc():
                        inter_action_result.append(action_result)
                        action_result = await ActionGraph.collate_action_results(
                            ctx=ctx, action_results=inter_action_result
                        )
                        action_response[node] = ActionDoc.model_validate(action_result)
                        action_graph_result_doc.result.output[node] = action_response[node]
                        action_graph_result_update: ActionGraphResultUpdate = (
                            ActionGraphResultUpdate(result=action_graph_result_doc.result)
                        )
                        action_graph_result_doc = (
                            await user_action_graph_result.update_action_graph_result(
                                ctx, action_graph_result_doc.id, action_graph_result_update
                            )
                        )
            except Exception as e:
                logger.bind(ctx=ctx, **bind_dict).exception(f"Error in loop run action: {str(e)}")

    @staticmethod
    async def run_action_with_loop_concurrently(
        ctx: Context,
        app_state: AppState,
        bind_dict: Dict[str, str],
        node: str,
        upstream_nodes: List[str],
        node_details_map: Dict[str, Node],
        node_action_map: Dict[str, str],
        action_response: Dict[str, ActionDoc],
        action_graph_result_doc: ActionGraphResultDoc,
        action_graph_input_dict: Dict[str, Any],
        action_graph_node_id: str | None,
        is_continue_run_graph: bool,
        user_actions: UserActions,
        user_action_graph_result: UserActionGraphResult,
        action_inputs: List[DataBlock],
        notify: Notify,
    ):
        logger.bind(ctx=ctx, **bind_dict).info(f"Running action with loop concurrently for node : {node}")
        semaphore: asyncio.Semaphore = get_semaphore()
        lock = asyncio.Lock()
        inter_action_results: List[ActionDoc | Exception] = [Exception("Empty")] * len(
            action_inputs
        )

        sleep_time = ActionGraph.get_node_sleep_time(node, node_details_map)

        async def process_action_input(idx: int, action_input: DataBlock, notify: Notify):
            # Stagger start times to prevent burst
            if sleep_time > 0:
                # Cap at 30s + jitter to prevent secondary bursts for tail items
                delay = min(idx * sleep_time, 30.0)
                delay += random.uniform(0, min(sleep_time, 1.0))
                await asyncio.sleep(delay)

            logger.bind(ctx=ctx, idx=idx, action_input=repr(action_input)[:100], **bind_dict).info(
                "Processing action input"
            )
            nonlocal action_graph_result_doc
            logger.bind(ctx=ctx, idx=idx, action_input=repr(action_input)[:100], **bind_dict).info(
                "Trying to acquire semaphore"
            )
            async with semaphore:
                logger.bind(ctx=ctx, idx=idx, action_input=repr(action_input)[:100], **bind_dict).info(
                    "Semaphore acquired, processing action input"
                )
                try:
                    action_input = DataBlock.model_validate(action_input)
                    task_app_state = app_state.child_context()
                    if not (action_graph_node_id and is_continue_run_graph):
                        action_result: ActionDoc | Exception = await ActionGraph.run_action(
                            ctx,
                            task_app_state,
                            user_actions,
                            node_action_map.get(node),
                            action_input.model_dump(),
                            notify=notify,
                        )
                    else:
                        logger.bind(ctx=ctx).info("Running node action with existing doc!!")
                        action_result = await ActionGraph.run_node_action_with_existing_doc(
                            ctx=ctx,
                            app_state=app_state,
                            action_response=action_response,
                            node=node,
                            action_graph_input_dict=action_input.model_dump(),
                            action_graph_result_doc=action_graph_result_doc,
                            user_actions=user_actions,
                            user_action_graph_result=user_action_graph_result,
                            notify=notify,
                        )
                    match action_result:
                        case Exception():
                            logger.bind(
                                ctx=ctx, idx=idx, action_input=repr(action_input)[:100], **bind_dict
                            ).exception(f"Error in loop run action {repr(action_result)[:100]}")
                        case ActionDoc():
                            logger.bind(
                                ctx=ctx, idx=idx, action_input=repr(action_input)[:100], **bind_dict
                            ).info("Success, adding action result to inter_action_results")

                    logger.bind(ctx=ctx, idx=idx, action_input=repr(action_input)[:100], **bind_dict).info(
                        "Storing action result into inter_action_results"
                    )
                    inter_action_results[idx] = action_result

                    async with lock:
                        logger.bind(ctx=ctx).info("Acquired Lock!!")
                        # Intermediate result update (safe collation)
                        # Find a seed ActionDoc to use as base for collation
                        seed = next(
                            (r for r in inter_action_results if isinstance(r, ActionDoc)), None
                        )
                        
                        if seed:
                            # Preserve original order: [seed] + [others minus seed]
                            
                            ordered_results = [seed] + [
                                r for r in inter_action_results if r is not seed
                            ]
                        
                            logger.bind(ctx=ctx).info("Collating Action Results")
                            collated: ActionDoc = await ActionGraph.collate_action_results(
                                ctx=ctx, action_results=ordered_results, node=node, action_graph_node_id=action_graph_node_id, action_response=action_response if action_graph_node_id else None, is_continue_run_graph=True if is_continue_run_graph else None
                            )
                            # logger.bind(ctx=ctx).info(f"Output of Collated : {collated.output}")
                            action_response[node] = ActionDoc.model_validate(collated)
                            action_graph_result_doc.result.output[node] = action_response[node]
                            action_graph_result_update = ActionGraphResultUpdate(
                                result=action_graph_result_doc.result
                            )
                            action_graph_result_doc = (
                                await user_action_graph_result.update_action_graph_result(
                                    ctx, action_graph_result_doc.id, action_graph_result_update
                                )
                            )
                            logger.bind(
                                ctx=ctx,
                                **bind_dict
                            ).info("run_action_with_loop_concurrently intermediate results")

                except Exception as e:
                    logger.bind(ctx=ctx, idx=idx, action_input=repr(action_input)[:100], **bind_dict).exception(
                        f"Error in loop run action: {repr(e)}"
                    )
            logger.bind(ctx=ctx, idx=idx, action_input=repr(action_input)[:100], **bind_dict).info(
                "Semaphore released, processed action input"
            )
            logger.bind(ctx=ctx, idx=idx, action_input=repr(action_input)[:100], **bind_dict).info(
                "Processing process_action_input complete"
            )

        await asyncio.gather(
            *[process_action_input(idx, ai, notify=notify) for idx, ai in enumerate(action_inputs)]
        )
        # Complete result update
        seed = next((r for r in inter_action_results if isinstance(r, ActionDoc)), None)
        if seed:
            logger.bind(ctx=ctx).info("Found Seed!!")
            # Standard collation if we have at least one success (preserving order)
            ordered_results = [seed] + [r for r in inter_action_results if r is not seed]
            collated: ActionDoc = await ActionGraph.collate_action_results(
                ctx=ctx, action_results=ordered_results, node=node, action_graph_node_id=action_graph_node_id, action_response=action_response if action_graph_node_id else None, is_continue_run_graph=True if is_continue_run_graph else None
            )
        else:
            # All failed: manually construct synthetic ActionDoc to preserve failure info safely
            logger.bind(ctx=ctx).info("All failed: manually construct synthetic ActionDoc to preserve failure info safely. Running Loop Failed!!")
            error_text = "\n".join([str(e) for e in inter_action_results])
            collated = ActionDoc(
                id="",
                user_id=action_graph_result_doc.user_id,
                name="Loop Failed",
                type=ActionType.mock_action,
                input=action_graph_input_dict,
                output=DataBlocks(blocks=[DataBlock(text=error_text)]).model_dump(),
                results=[],
            )
        action_response[node] = ActionDoc.model_validate(collated)
        action_graph_result_doc.result.output[node] = action_response[node]
        action_graph_result_update = ActionGraphResultUpdate(result=action_graph_result_doc.result)
        action_graph_result_doc = await user_action_graph_result.update_action_graph_result(
            ctx, action_graph_result_doc.id, action_graph_result_update
        )
        logger.bind(
            ctx=ctx, **bind_dict, action_graph_result_doc_id=action_graph_result_doc.id
        ).info("run_action_with_loop_concurrently complete")