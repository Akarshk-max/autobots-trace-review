import copy
from typing import Any, Dict, List

from loguru import logger

from src.autobots.action.action.action_doc_model import ActionDoc, ActionFind, ActionUpdate
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action_graph.action_graph.action_graph_prev import ActionGraph
from src.autobots.action_graph.action_graph_result.action_graph_result_model_doc import (
    ActionGraphResultDoc,
    ActionGraphResultUpdate,
)
from src.autobots.action_graph.action_graph_result.user_action_graph_result_prev import (
    UserActionGraphResult,
)
from src.autobots.api.webhook import Webhook
from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_model import EventResultStatus, EventType
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState


class ActionGroup:
    # roles: 1 Director, n managers, m functional

    @staticmethod
    async def run_action_group(
        ctx: Context,
        app_state: AppState,
        action_graph_input_dict: Dict[str, Any],
        action_graph_result_doc: ActionGraphResultDoc,
        user_actions: UserActions,
        user_action_graph_result: UserActionGraphResult,
        webhook: Webhook | None = None,
    ) -> ActionGraphResultDoc:
        node_ids_to_group_prompts = await ActionGroup._build_node_ids_to_group_prompts(
            ctx=ctx,
            action_graph_result_doc=action_graph_result_doc,
            user_actions=user_actions,
            user_action_graph_result=user_action_graph_result,
        )
        assert node_ids_to_group_prompts
        # TODO: Continue
        director_node_id = await ActionGroup.get_director_node(
            ctx=ctx, action_graph_result_doc=action_graph_result_doc
        )
        assert director_node_id
        directior_action_id = action_graph_result_doc.result.nodes.get(director_node_id)
        await ActionGroup.add_delegate_tool_to_director_node(
            ctx=ctx, app_state=app_state, user_actions=user_actions, director_id=directior_action_id
        )

        director_action_doc = await user_actions.get_owned_or_shared_or_system_actions(
            ctx=ctx, action_id=directior_action_id
        )
        director_action_doc.input = action_graph_input_dict
        action_graph_result_doc.result.output[director_node_id] = director_action_doc
        action_graph_result_doc = await ActionGroup.save_result_field_of_action_graph_result_doc(
            ctx=ctx,
            action_graph_result_doc=action_graph_result_doc,
            user_action_graph_result=user_action_graph_result,
        )
        # action_graph_result_update = ActionGraphResultUpdate(
        #     type=EventType.action_graph_run,
        #     result=action_graph_result_doc.result,
        #     status=EventResultStatus.success,
        # )
        # action_graph_result_doc = await user_action_graph_result.update_action_graph_result(
        #     ctx=ctx,
        #     action_graph_result_id=action_graph_result_doc.id,
        #     action_graph_update=action_graph_result_update,
        # )

        action_graph_input_dict["text"] += f"\n{node_ids_to_group_prompts.get(director_node_id)}"
        node_action_map = action_graph_result_doc.result.nodes
        notify = Notify(topic=action_graph_result_doc.id)
        action_result: ActionDoc = await ActionGraph.run_action(
            ctx,
            app_state,
            user_actions,
            node_action_map.get(director_node_id),
            action_graph_input_dict,
            notify,
        )

        if action_graph_result_doc.result.output is None:
            action_graph_result_doc.result.output = {}
        action_graph_result_doc.result.output[director_node_id] = action_result
        action_graph_result_doc = await ActionGroup.save_result_field_of_action_graph_result_doc(
            ctx=ctx,
            action_graph_result_doc=action_graph_result_doc,
            user_action_graph_result=user_action_graph_result,
        )
        # action_graph_result_update = ActionGraphResultUpdate(
        #     type=EventType.action_graph_run,
        #     result=action_graph_result_doc.result,
        #     status=EventResultStatus.success,
        # )
        # action_graph_result_doc = await user_action_graph_result.update_action_graph_result(
        #     ctx=ctx,
        #     action_graph_result_id=action_graph_result_doc.id,
        #     action_graph_update=action_graph_result_update,
        # )

        return action_graph_result_doc

    @staticmethod
    async def _build_node_ids_to_group_prompts(
        ctx: Context,
        action_graph_result_doc: ActionGraphResultDoc,
        user_actions: UserActions,
        user_action_graph_result: UserActionGraphResult,
    ) -> Dict[str, str]:
        node_group_prompt = {}
        how_to_use_group_context = (
            "\n\nYou are agent in this organizational structure."
            "\nFunction: Your job is to break work into smaller task and delegate work to immediate org members."
            "\nRole: You can only delegate work to Agents you directly manage"
            "\nRole: Evaluate the work you have delegated."
            "\nUse organizational structure to determine delegation of work."
            "\nBreak work in sequential manner if necessary."
            "\nUse `delegate` tool to assign work to team members."
            "\nRemember to give them information the organization tree they own"
            # "\nReturn with delegate tool output\n\n"
            # "\nYour delegate output should be in just a JSON with following keys:"
            # "\nnode_id: str,"
            # "\nagent_id: str,"
            # "\nmessage: str,"
            # "\n\nExample Output:"
            # "\n[{key1: value1, key2: value2}, {key1: value1, key2: value2}, {key1: value1, key2: value2}]"
        )
        for node_id in action_graph_result_doc.result.nodes.keys():
            graph_copy = copy.deepcopy(action_graph_result_doc)
            group_hierarchy_prompt = await ActionGroup.build_node_group_hierarchy_prompt(
                ctx=ctx, action_graph_result_doc=graph_copy, node_id=node_id
            )
            graph_copy = copy.deepcopy(action_graph_result_doc)
            group_profile_prompt = await ActionGroup.build_node_group_profile_prompt(
                ctx=ctx,
                user_actions=user_actions,
                action_graph_result_doc=graph_copy,
                node_id=node_id,
            )
            group_prompt = ""
            if group_hierarchy_prompt:
                group_prompt += how_to_use_group_context + group_hierarchy_prompt
            if group_profile_prompt:
                group_prompt += group_profile_prompt
            node_group_prompt[node_id] = group_prompt
        logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).info(f"build_node_ids_to_group_prompts: {repr(node_group_prompt)[:100]}")
        return node_group_prompt

    @staticmethod
    async def build_node_group_hierarchy_prompt(
        ctx: Context, action_graph_result_doc: ActionGraphResultDoc, node_id: str
    ):
        group_hierarchy_prompt = ""
        inverted_graph: Dict[str, List[str]] = await ActionGraph.invert_map(
            action_graph_result_doc.result.graph
        )
        child_nodes: List[str] = action_graph_result_doc.result.graph.get(node_id)

        visited_nodes = []
        to_visit_nodes = copy.deepcopy(child_nodes) if child_nodes else []
        while len(to_visit_nodes) > 0:
            visiting_node = to_visit_nodes[0]
            children_of_visiting_nodes = action_graph_result_doc.result.graph.get(visiting_node)
            to_visit_nodes += children_of_visiting_nodes if children_of_visiting_nodes else []
            try:
                if visiting_node in action_graph_result_doc.result.graph.get(node_id):
                    group_hierarchy_prompt += f"You manage Agent {visiting_node}.\n"
                else:
                    managers = inverted_graph.get(visiting_node)
                    for manager in managers:
                        group_hierarchy_prompt += f"Agent {manager} manage {visiting_node}.\n"
            except Exception as e:
                logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).exception(
                    f"Error in build_node_group_hierarchy_prompt: {repr(e)}"
                )
            finally:
                to_visit_nodes.remove(visiting_node)
                visited_nodes.append(visiting_node)
        logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).info(f"build_node_group_hierarchy_prompt: {repr(group_hierarchy_prompt)[:100]}")
        return group_hierarchy_prompt

    @staticmethod
    async def build_node_group_profile_prompt(
        ctx: Context,
        user_actions: UserActions,
        action_graph_result_doc: ActionGraphResultDoc,
        node_id: str,
    ):
        group_profile_prompt = ""
        group_profile_prompt_header = "\nAgent profiles:\n"
        child_nodes: List[str] = action_graph_result_doc.result.graph.get(node_id)

        visited_nodes = []
        to_visit_nodes = child_nodes if child_nodes else []
        while len(to_visit_nodes) > 0:
            visiting_node = to_visit_nodes[0]
            children_of_visiting_nodes = action_graph_result_doc.result.graph.get(visiting_node)
            to_visit_nodes += children_of_visiting_nodes if children_of_visiting_nodes else []
            # to_visit_nodes += action_graph_result_doc.result.graph.get(visiting_node)
            try:
                action_id = action_graph_result_doc.result.nodes.get(visiting_node)
                action_find = ActionFind(id=action_id)
                action_doc_page = await user_actions.list_owned_or_shared_with(
                    action_find=action_find
                )
                assert action_doc_page.total_count == 1
                action_doc = action_doc_page.docs.pop()
                group_profile_prompt += (
                    f"Profile for node_id: {visiting_node}\n"
                    f"agent_id: {action_doc.id}\n"
                    f"agent_name: {action_doc.name}\n"
                    f"agent_description: {action_doc.description}\n"
                    f"agent_manual: {action_doc.user_manual}\n\n"
                )
            except Exception as e:
                logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).exception(
                    f"Error in build_node_group_profile_prompt: {str(e)}"
                )
            finally:
                to_visit_nodes.remove(visiting_node)
                visited_nodes.append(visiting_node)
        logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).info(f"build_node_group_profile_prompt: {repr(group_profile_prompt)[:100]}")
        group_profile_prompt = (
            group_profile_prompt_header + group_profile_prompt if group_profile_prompt else ""
        )
        return group_profile_prompt

    @staticmethod
    async def get_director_node(
        ctx: Context, action_graph_result_doc: ActionGraphResultDoc
    ) -> str | None:
        inverted_graph: Dict[str, List[str]] = await ActionGraph.invert_map(
            action_graph_result_doc.result.graph
        )
        # TODO: remove start node
        for node_id, children_ids in inverted_graph.items():
            if (
                action_graph_result_doc.result.nodes.__contains__(node_id)
                and len(children_ids) == 0
            ):
                return node_id
        logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).error("Could not get director node")
        return None

    @staticmethod
    async def add_delegate_tool_to_director_node(
        ctx: Context, app_state: AppState, user_actions: UserActions, director_id: str
    ):
        try:
            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).info(f"Adding delegate tool to director action {director_id}")
            action_doc = await user_actions.get_owned_or_shared_or_system_actions(
                ctx=ctx, action_id=director_id, empty_shared_action_config=True
            )
            assert action_doc
            # logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).debug(f"config : {action_doc.config}")
            tools = action_doc.config.get("tools") if action_doc.config.get("tools") else []
            is_delegate_tool_added = False
            for tool in tools:
                if tool == "delegate":
                    is_delegate_tool_added = True
                    break
            if not is_delegate_tool_added:
                tools.append("delegate")
                action_doc.config["tools"] = tools
                action_update = ActionUpdate(config=action_doc.config)
                logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).info(
                    f"Adding delegate tool to director action {director_id}"
                )
                action_doc = await user_actions.update_action(
                    ctx=ctx, app_state=app_state, action_id=action_doc.id, action_update=action_update
                )
                logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).info(
                    f"Added delegate tool to director action {director_id}"
                )
                assert action_doc
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).exception(f"Error in add_delegate_tool_to_director_node: {str(e)}")
            raise e

    @staticmethod
    async def save_result_field_of_action_graph_result_doc(
        ctx: Context,
        action_graph_result_doc: ActionGraphResultDoc,
        user_action_graph_result: UserActionGraphResult,
    ) -> ActionGraphResultDoc:
        action_graph_result_update = ActionGraphResultUpdate(
            type=EventType.action_graph_run,
            result=action_graph_result_doc.result,
            status=EventResultStatus.success,
        )
        action_graph_result_doc = await user_action_graph_result.update_action_graph_result(
            ctx=ctx,
            action_graph_result_id=action_graph_result_doc.id,
            action_graph_update=action_graph_result_update,
        )
        return action_graph_result_doc