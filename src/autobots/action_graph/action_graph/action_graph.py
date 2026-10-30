import asyncio
import copy
import json
import random
from collections import deque
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Set, Tuple

from fastapi import BackgroundTasks, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from openai.types.chat import ChatCompletionUserMessageParam
from openai.types.shared_params import ResponseFormatJSONObject
from tenacity import retry, stop_after_attempt, wait_fixed

from src.autobots import SettingsProvider
from src.autobots.action.action.action_doc_model import ActionDoc, ActionResult
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks, Metadata, TextObj
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action.action_type.action_map import ACTION_MAP
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openai_v2 import (
    ActionText2TextLlmChatOpenai,
    LlmChatOpenaiConfig,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.action_graph.action_graph.action_graph_doc_model import ActionGraphDoc, Node
from src.autobots.action_graph.action_graph.action_graph_input_creator import (
    ActionGraphInputCreator,
)
from src.autobots.action_graph.action_graph.coffman_graham import coffman_graham
from src.autobots.action_graph.action_graph.context_input import (
    contextual_input,
    contextual_input_dict,
)
from src.autobots.action_graph.action_graph.context_vector import BASELINE_SLOT
from src.autobots.action_graph.action_graph.edge_decision import contributing_parents
from src.autobots.action_graph.action_graph.frontier import FrontierScheduler, ScheduledNode
from src.autobots.action_graph.action_graph.graph_topology import GraphTopology
from src.autobots.action_graph.action_graph.node_result_writer import (
    NodeRunContext,
    assert_baseline_mirror,
    assert_upstream_turn_recorded,
    assert_vector_covers_output,
    forget_all,
    forget_node,
    is_node_done,
    node_results,
    record_node_result,
    seed_baseline_from_output,
)
from src.autobots.action_graph.action_graph.node_units_recorder import NodeUnitsRecorder
from src.autobots.action_graph.action_graph.redirect_budget import (
    initial_budgets,
    is_disabled,
    may_fire,
    record_firing,
)
from src.autobots.action_graph.action_graph.redirect_stack import RedirectStack
from src.autobots.action_graph.action_graph.turn_counter import (
    close_open_turns,
    continue_run_key,
    next_turn,
    record_open_turn,
    split_continue_run_key,
    split_open_turn_key,
)
from src.autobots.action_graph.action_graph_result.action_graph_result_model_doc import (
    ActionGraphResultDoc,
    ActionGraphResultUpdate,
)
from src.autobots.action_graph.action_graph_result.user_action_graph_result import (
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


class RedirectOutcome(NamedTuple):
    """What one node's redirect evaluation did.

    `fired` is not cosmetic: a fired redirect advances a durable budget counter, so the caller has
    to know whether there is anything new to persist.
    """

    exhausted: List[Tuple[str, str]]
    fired: int


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
            # Anything reaching here escaped all three retry attempts. Without this the run keeps
            # whatever status it had - `processing` - and sits there until the 720-minute stuck
            # monitor, indistinguishable from a run that is still working. `run_action_graph_v2`
            # marks its own failures; this covers the ones raised before or around it, such as
            # `empty_downstream_nodes` refusing to half-empty a graph.
            try:
                action_graph_result_doc = await user_action_graph_result.update_action_graph_result(
                    ctx,
                    action_graph_result_doc.id,
                    ActionGraphResultUpdate(
                        status=EventResultStatus.error, error_message=TextObj(text=str(e))
                    ),
                )
                if webhook:
                    await webhook.send(action_graph_result_doc.model_dump())
            except Exception:
                logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).exception(
                    "Error marking action graph run as failed"
                )
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

        # Tenacity retries with the same document, which still carries the failed attempt's
        # terminal status. Re-arm it before any save, or this attempt's first save is read as
        # a completion: it writes a premature metric and its duration is never counted.
        action_graph_result_doc.status = EventResultStatus.processing
        action_graph_result_doc.error_message = None

        # Empty Downstream Nodes if necessary
        if action_graph_node_id and is_rerun_graph:
            logger.bind(ctx=ctx ,code=AppCode.ACTION_GRAPH).info("Empty Downstream Nodes")
            emptied = await ActionGraph.empty_downstream_nodes(
                ctx=ctx,
                action_graph_node_id=action_graph_node_id,
                action_graph_result_doc=action_graph_result_doc,
                is_empty_downstream_only=is_empty_downstream_only,
            )
            # Exactly the nodes that were emptied, which is exactly the nodes about to re-execute.
            # Note this includes the root on a full rerun and excludes it on a downstream-only
            # one - `empty_downstream_nodes` has already made that distinction, so reusing its
            # answer means the two cannot disagree.
            ActionGraph.withdraw_approvals(
                ctx,
                action_graph_result_doc=action_graph_result_doc,
                nodes=emptied,
                reason="rerun",
            )
        elif not action_graph_node_id and is_rerun_graph:
            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).info("Empty all Nodes")
            forget_all(action_graph_result_doc)
            # `forget_all` empties every node, so this re-executes the whole graph from the start.
            # It is not a resume - a run parked on a gate is resumed by an ordinary run, which
            # empties nothing and lets `skip_done` step over what already finished.
            #
            # **No approvals are withdrawn here, deliberately.** The human's verdicts are the one
            # thing worth carrying across a full re-execution: clearing them means approve the
            # gate, re-run, the approval is gone, and the run parks at the same gate - the same
            # way a continue-run was unfinishable before the resume check below.
            #
            # The cost is that every node does produce new output, so an approval granted for the
            # old output now stands over output nobody has seen. Accepted, because the alternative
            # is an operation that cannot get past its own gates.
        elif action_graph_node_id and not is_rerun_graph and is_continue_run_graph:
            # Only for a NEW continue-run. A resume must not withdraw the approvals again: they
            # were withdrawn once, when the operation started, and the human has since granted
            # the one that stopped it. Clearing on every attempt is what would make a gated
            # continue-run unfinishable - approve, resume, cleared again, blocked again.
            if getattr(action_graph_result_doc.result, "open_continue_run", None):
                logger.bind(
                    ctx=ctx,
                    code=AppCode.ACTION_GRAPH,
                    open_continue_run=action_graph_result_doc.result.open_continue_run,
                ).info(
                    "Continue Run Graph - resuming an outstanding one, leaving user gates as they "
                    "stand"
                )
            else:
                # The subtree MINUS the root. The root is skipped by a continue-run, so its
                # output does not change and its approval still describes what its children are
                # about to read. Clearing it blocks the very subtree the operation exists to
                # re-run - the children check their *parent's* review, and the root is that
                # parent.
                ActionGraph.withdraw_approvals(
                    ctx,
                    action_graph_result_doc=action_graph_result_doc,
                    nodes=ActionGraph._subtree_of(
                        action_graph_node_id, action_graph_result_doc.result.graph or {}
                    )
                    - {action_graph_node_id},
                    reason="continue-run",
                )
    
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

        await user_action_graph_result.save_run_doc(
            ctx, action_graph_result_doc, bump_search_cache=True
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
        action_response: Dict[str, ActionDoc] = node_results(action_graph_result_doc)
        # action_graph_input = TextObj.model_validate(action_graph_input_dict)

        total_nodes: Set[str] = await ActionGraph.get_nodes(graph_map)
        logger.bind(ctx=ctx,**bind_dict).info(f"Total Nodes : {len(total_nodes)}")
        
        to_wait = False
        try:
            # Inside the try: a GraphCycleError here must mark the run as errored like any other
            # failure. Raised above it, the run would sit in `processing` until the stuck-graph
            # monitor noticed it hours later.
            # Warn-only until validation is enforced: see coffman_graham's docstring.
            nodes_order: List[List[str]] = coffman_graham(
                inverted_map_subgraph if inverted_map_subgraph else inverted_map,
                len(total_nodes),
                raise_on_cycle=SettingsProvider.sget().ACTION_GRAPH_VALIDATION_ENFORCED,
            )
            logger.bind(
                ctx=ctx,**bind_dict
            ).info(f"Nodes Order by COFFMAN GRAHAM : {nodes_order}")
            # The frontier replaces the layer walk. Seeded in Coffman-Graham order, so turn 0
            # drains in exactly the sequence the nested for-loops produced; `rearm` is what a
            # redirect will call, and a graph with no redirect edges never reaches a second turn.
            frontier = FrontierScheduler(nodes_order)
            # Every structural fact resolved once: K, the R.N. set, the context index map and the
            # precomputed R.S.(i, j). `build` never raises - validation ships warn-only, so a graph
            # that fails it still has to run.
            topology = GraphTopology.build(action_graph_result_doc.result)
            cyclic = topology.K > 1
            all_slots = tuple(str(index) for index in range(topology.K))
            # Whether this run keeps a context vector at all. **Not the same question as
            # `cyclic`.** A DAG now records its transcript too, so the two shapes share one code
            # path rather than a vector-writing engine beside a vector-less one - at K == 1 that
            # is a single slot, slot 0, mirroring `ActionDoc.results`.
            #
            # The condition is a start node, not a redirect edge, because every entry carries a
            # provenance tag and the start node is what tags the baseline. A graph with no single
            # root - one isolated node, or a pure data cycle - has nothing to tag with, so it
            # keeps no vector and behaves exactly as it did before. Those shapes are legal today
            # (validation is warn-only) and refusing them here would break runs that work.
            writes_vectors = bool(topology.start_node_id)
            if cyclic and not topology.start_node_id:
                # A cyclic run tags its baseline with the start node, so without one every entry
                # case (i) writes would be tagged "" - which Part 1's guard refuses, correctly,
                # but only after the run has begun and with a message written for whoever is
                # building a `NodeRunContext`. Refuse it here instead, where the reason is still
                # about the graph the user drew.
                #
                # This is a precondition of `K > 1`, not a general validation rule: a DAG writes
                # no vector, needs no tag, and is unaffected. Validation already reports the
                # condition (D7) but is warn-only, so on its own it lets the run proceed to the
                # crash.
                raise ValueError(
                    "A cyclic graph needs exactly one start node - the single root of its data "
                    "edges - to tag the baseline every context slot starts from. This graph has "
                    f"{len(topology.roots)}: {list(topology.roots)}. Give it a single root, or "
                    "remove the redirect edges to run it as a DAG."
                )
            start_node_id = topology.start_node_id or ""
            if cyclic and action_graph_result_doc.result.redirect_budgets is None:
                # One zeroed counter per redirect edge, on the document. A resumed run keeps the
                # counts it already has rather than starting the cycle over.
                action_graph_result_doc.result.redirect_budgets = initial_budgets(
                    topology.redirect_edges,
                    default_limit=SettingsProvider.sget().ACTION_GRAPH_REDIRECT_BUDGET_DEFAULT,
                )
            if cyclic:
                logger.bind(
                    ctx=ctx,
                    **bind_dict,
                    K=topology.K,
                    redirecting_nodes=list(topology.redirecting_nodes),
                ).info("Cyclic graph: scheduling with redirect stacks")
            if writes_vectors:
                # A run written before vectors existed has `output` and no `node_vectors`, and
                # `is_node_done` reads that combination as drift - "not done" - so every completed
                # node would be re-executed and re-billed on this first touch. Reconstruct what
                # the run would have recorded, from the only record it has. Lazy on purpose: no
                # migration job, no S3 sweep, no race, and untouched history costs nothing.
                seeded = seed_baseline_from_output(
                    action_graph_result_doc,
                    start_node_id=start_node_id,
                    all_slots=all_slots,
                )
                if seeded:
                    logger.bind(
                        ctx=ctx, **bind_dict, count_seeded=len(seeded)
                    ).warning(
                        "Seeded context vectors from `output` for a run that predates them; "
                        "without this its completed nodes would read as never having run"
                    )
            if cyclic:
                # Before any new work: finish what a previous attempt started. A turn that was
                # handed out and never completed leaves nodes owing it, and without this the
                # forward pass reads as done, the redirecting node fires again, and the
                # interrupted turn is never recorded by the nodes that missed it (RISK-12).
                ActionGraph.resume_outstanding_turns(
                    ctx,
                    bind_dict=bind_dict,
                    action_graph_result_doc=action_graph_result_doc,
                    topology=topology,
                    frontier=frontier,
                )
            # Fixed once, before any node runs, and held for every node in this continue-run - see
            # `next_user_turn`. None for a plain run, a rerun, and every DAG (K == 1 keeps no
            # vector, so there is no baseline slot to append to).
            continue_run_tag: Optional[str] = None
            continue_run_turn_no: Optional[int] = None
            continue_run_participants: Set[str] = set()
            # An unfinished continue-run is resumed before any new one is opened, and on **any**
            # run - the same way `resume_outstanding_turns` above works, and for the same reason:
            # the operation is recorded on the document, so it does not depend on the next request
            # asking for it. It cannot depend on that here, because the API refuses a continue-run
            # on a `waiting` run (405) - the run that resumes one is an ordinary run, issued after
            # the human approved whatever gate stopped it.
            #
            # Without this a human gate did not pause a continue-run, it cancelled it: the root
            # and the turn lived in these three locals, died with the function, and the next run
            # asked every node about turn 0, found them all done, and reported success having
            # executed nothing.
            outstanding = (
                ActionGraph.outstanding_continue_run(action_graph_result_doc, graph_map)
                if writes_vectors
                else None
            )
            if outstanding and outstanding[2]:
                continue_run_tag, continue_run_turn_no, owing = outstanding
                continue_run_participants = (
                    ActionGraph._subtree_of(continue_run_tag, graph_map) - {continue_run_tag}
                )
                logger.bind(
                    ctx=ctx,
                    **bind_dict,
                    action_graph_node_id=continue_run_tag,
                    turn_no=continue_run_turn_no,
                    still_owing=sorted(owing),
                ).warning(
                    "Resuming a continue-run that was started and never finished - the nodes "
                    "that still owe its turn are re-armed under the original turn rather than a "
                    "new one being opened"
                )
            elif writes_vectors and action_graph_node_id and is_continue_run_graph:
                continue_run_tag = action_graph_node_id
                continue_run_turn_no = ActionGraph.next_user_turn(action_graph_result_doc)
                # The nodes that will actually re-execute. Everything else - the root, which is
                # skipped, and any parent outside the subtree - takes no part in this turn and is
                # asked the loose readiness question instead of being made to fake an entry.
                continue_run_participants = set(total_nodes) - {action_graph_node_id}
                # Durable from the moment it opens, so a gate can pause it rather than end it.
                action_graph_result_doc.result.open_continue_run = continue_run_key(
                    continue_run_tag, continue_run_turn_no
                )
                logger.bind(
                    ctx=ctx,
                    **bind_dict,
                    action_graph_node_id=action_graph_node_id,
                    turn_no=continue_run_turn_no,
                ).info("Continue-run: opening a fresh turn on the baseline column")

            current_layer: int | None = None
            # Deliberately not `to_wait`. That flag is sticky - once any node sets it every later
            # node is skipped - which is right for a node that cannot run, but wrong here: an
            # exhausted redirect does not stop the rest of the graph from finishing the work it
            # already has. The run drains, then reports `waiting` so a human can raise the limit.
            exhausted_pairs_hit: Set[Tuple[str, str]] = set()
            while (scheduled := frontier.pop()) is not None:
                node = scheduled.node
                if scheduled.layer != current_layer:
                    current_layer = scheduled.layer
                    logger.bind(
                        ctx=ctx,
                        **bind_dict,
                        count_total_nodes=len(total_nodes),
                    ).info(
                        f"Running Action Graph on nodes : {str(nodes_order[current_layer])}"
                    )
                upstream_nodes = inverted_map.get(node)
                logger.bind(
                    ctx=ctx,
                    **bind_dict,
                    count_total_nodes=len(total_nodes),
                ).info(f"Running Action Graph on node : {node} and its upstream nodes : {upstream_nodes}")
                run_ctx = ActionGraph.resolve_run_context(
                    action_graph_result_doc=action_graph_result_doc,
                    scheduled=scheduled,
                    writes_vectors=writes_vectors,
                    all_slots=all_slots,
                    start_node_id=start_node_id,
                    data_parents=upstream_nodes,
                    topology=topology,
                    # Participants only. On a resume the frontier holds the whole graph, not
                    # the subtree, so a node outside the operation would otherwise be asked
                    # "are you done at (root, N)?", answer no, and re-run for nothing.
                    continue_run_tag=(
                        continue_run_tag if node in continue_run_participants else None
                    ),
                    continue_run_turn_no=(
                        continue_run_turn_no if node in continue_run_participants else None
                    ),
                )
                # A DAG asks "has this node run at all"; a cyclic graph asks "has *this execution*
                # been recorded", which is what lets a re-armed node run again instead of reading
                # as finished. `resolve_run_context` supplies the identity either way.
                done_identity = (
                    dict(
                        context_index=scheduled.context_index,
                        tag=run_ctx.provenance_tag,
                        turn_no=run_ctx.turn_no,
                    )
                    if writes_vectors
                    else {}
                )
                node_already_done = await ActionGraph.is_work_done(
                    [node], action_graph_result_doc, **done_identity
                )
                # On the baseline column readiness is a turn comparison, so a parent that is
                # taking part is asked about *this* turn rather than about having recorded
                # anything at all. That is what makes a continue-run's ordering real: a child
                # waits for its parent's new output instead of reading the last run's.
                #
                # But only a parent that is taking part. The continue-run root is deliberately
                # skipped, and a subtree node can have a parent outside the subtree entirely
                # (`upstream_nodes` comes from the full inverted map) - neither will ever record
                # this turn. Asking them the strict question stalls the whole continue-run, and
                # making them write an entry to answer it would file a value they did not
                # produce. They get the loose question: "have you got anything for me at all",
                # which is SS1.4's case (ii) - contributing the value they already hold.
                #
                # Inside a redirect turn the loose question is the right one for everybody: a
                # parent outside `R.S.` legitimately holds nothing new, and the precise per-turn
                # check there is `assert_upstream_turn_recorded`, a canary rather than a gate.
                parents = list(upstream_nodes or ())
                if writes_vectors and scheduled.context_index == BASELINE_SLOT:
                    taking_part = (
                        [p for p in parents if p in continue_run_participants]
                        if continue_run_participants
                        else parents
                    )
                    standing_by = [p for p in parents if p not in taking_part]
                else:
                    taking_part, standing_by = [], parents

                upstream_ready: bool = await ActionGraph.is_work_done(
                    taking_part,
                    action_graph_result_doc,
                    context_index=scheduled.context_index,
                    tag=run_ctx.provenance_tag,
                    turn_no=run_ctx.turn_no,
                ) and await ActionGraph.is_work_done(
                    standing_by,
                    action_graph_result_doc,
                    context_index=scheduled.context_index,
                )
                # A DAG keeps no vector, so it has no turn to compare and `node_already_done` is
                # simply "is it in `output`" - permanently true once it has run. A continue-run
                # there still has to ignore it. At K > 1 the fresh baseline turn already makes it
                # false, so no bypass is needed and the check does its job.
                #
                # `upstream_ready` is never bypassed. Ignoring it is what let a node run whose
                # parent had produced nothing, and `run_node_action_with_existing_doc` then raises
                # a bare KeyError on `output[node]`.
                skip_done = node_already_done and not (
                    action_graph_node_id and is_continue_run_graph and not writes_vectors
                )
                if (action_graph_node_id and is_continue_run_graph) and node == action_graph_node_id:
                    logger.bind(
                        ctx=ctx, **bind_dict, action_graph_node_id=action_graph_node_id
                    ).info(f"Skipping the Root Node {node} of the Subtree for continue run.")
                    continue
                elif skip_done or not upstream_ready:
                    if cyclic and node_already_done:
                        # Work that is already recorded still advances the wave. A resumed run
                        # finds its forward pass complete and would otherwise never reach the node
                        # that fires the redirect, so a raised budget would buy nothing - the run
                        # would come straight back as `waiting` having done nothing. Whether a
                        # redirect fires is a question about the edge's budget, not about whether
                        # this particular result had to be recomputed.
                        outcome = ActionGraph.fire_redirects(
                            ctx,
                            bind_dict=bind_dict,
                            action_graph_result_doc=action_graph_result_doc,
                            scheduled=scheduled,
                            topology=topology,
                            frontier=frontier,
                        )
                        exhausted_pairs_hit.update(outcome.exhausted)
                        if outcome.fired:
                            # This path runs no node, so no save follows it. Without this the
                            # increment would sit only in memory until some later node happened
                            # to save - and on a resumed run, where every node is skipped, that
                            # may never happen at all.
                            await user_action_graph_result.save_run_doc(
                                ctx, action_graph_result_doc
                            )
                    logger.bind(ctx=ctx, **bind_dict, action_name=node_action_map.get(node)).info("Continuing to next Node as this Node's result is calculated or Upstream dependencies are not met")
                    continue
                else:
                    logger.bind(ctx=ctx, **bind_dict,action_id=node_action_map.get(node)).info("Continuing exec as Node's result not yet calculated and Upstream dependencies are met")
                # The human gates are a forward-pass concept, so they apply to the baseline
                # context only. `user_input_required` / `user_review_required` and their `_done`
                # counterparts live on `node_details` - graph-definition state with no per-turn
                # dimension - so a re-entry can only read a verdict that was given for the
                # baseline. Applying them again means one of two wrong things: the flag is still
                # `done`, so the gate is satisfied by a stale approval and adds nothing; or it is
                # not, and the sticky `to_wait` kills turn work that redirects already queued.
                # A DAG has only the baseline context, so nothing about it changes.
                on_baseline = scheduled.context_index == BASELINE_SLOT

                # Check if user input required
                if (on_baseline
                    and node_details_map
                    and node_details_map.get(node)
                    and node_details_map.get(node).data.user_input_required
                    and not node_details_map.get(node).data.user_input_done):
                    logger.bind(
                        ctx=ctx,
                        **bind_dict
                    ).info("This node requires User Input, not executing it.")
                    to_wait = True
                    continue 
                
                if on_baseline:
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
                # The precise upstream check (§2.5), as a canary. Only inside a turn: on the forward
                # pass there is no frame, and `upstream_ready` above is already the right question.
                # Expressible only now that the frame names its edge, because `R.S.` is keyed by
                # the pair - which is also what makes it precise: a parent incomparable to the
                # redirecting node takes no part in the turn and correctly holds nothing new.
                frame = scheduled.stack.top
                if cyclic and frame is not None:
                    taking_part = topology.relevant_subgraph.get(
                        (frame.target, frame.node), frozenset()
                    )
                    assert_upstream_turn_recorded(
                        ctx,
                        action_graph_result_doc,
                        node,
                        parents=[
                            parent
                            for parent in (upstream_nodes or ())
                            if parent in taking_part
                        ],
                        context_index=scheduled.context_index,
                        tag=frame.node,
                        turn_no=frame.turn_no,
                    )
                node_app_state = app_state.child_context(action_graph_id=app_state.action_graph_id, action_graph_result_id=app_state.action_graph_result_id, action_graph_node_id=node,)
                # A node with no data parents never reaches `to_input`, so if it is a redirect
                # target - the root of the graph, sent work back to it - its own history has to
                # arrive through the graph input instead. Composed once here, so all four
                # zero-upstream paths get it without knowing about contexts. Identity for every
                # other node. See `context_input`.
                node_input_dict = contextual_input_dict(
                    action_graph_result_doc, node, action_graph_input_dict, run_ctx=run_ctx
                )
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
                            action_graph_result_doc=action_graph_result_doc,
                            run_ctx=run_ctx,
                            action_graph_input_dict=node_input_dict,
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
                            action_graph_result_doc=action_graph_result_doc,
                            run_ctx=run_ctx,
                            action_graph_input_dict=node_input_dict,
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
                            action_graph_result_doc=action_graph_result_doc,
                            run_ctx=run_ctx,
                            action_graph_input_dict=node_input_dict,
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
                            action_graph_result_doc=action_graph_result_doc,
                            run_ctx=run_ctx,
                            action_graph_input_dict=node_input_dict,
                            user_actions=user_actions,
                            user_action_graph_result=user_action_graph_result,
                            notify=notify,
                        )
                else:
                    # Which execution path this node takes is a question about **this node**, not
                    # about the request. `run_action` fetches the action fresh and replaces
                    # `results`; `run_node_action_with_existing_doc` appends to the stored one.
                    # A resumed continue-run arrives as an ordinary run - the API refuses a
                    # continue-run on a `waiting` run - so keying on the request sent its
                    # participants down the replacing path, and their `results` stopped matching
                    # the turns their slot recorded.
                    in_continue_run = bool(continue_run_tag) and node in continue_run_participants
                    node_id_for_run = continue_run_tag if in_continue_run else action_graph_node_id
                    continue_for_run = True if in_continue_run else is_continue_run_graph

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
                            action_graph_result_doc=action_graph_result_doc,
                            run_ctx=run_ctx,
                            action_graph_input_dict=action_graph_input_dict,
                            action_graph_node_id=node_id_for_run,
                            is_continue_run_graph=continue_for_run,
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
                            action_graph_result_doc=action_graph_result_doc,
                            run_ctx=run_ctx,
                            action_graph_input_dict=action_graph_input_dict,
                            action_graph_node_id=node_id_for_run,
                            is_continue_run_graph=continue_for_run,
                            user_actions=user_actions,
                            user_action_graph_result=user_action_graph_result,
                            notify=notify,
                        )

                # Redirects are evaluated *before* the save, not after, so that a fired
                # redirect's budget increment lands in the same write as the result that
                # triggered it. Firing afterwards left a window where a crash lost the increment
                # while keeping the work, and a retry would then re-fire a redirect that had
                # already fired - the exact failure RISK-4 is about. Nothing here needs the save
                # to have happened: the turn counter and the budgets both live on the in-memory
                # document, and this call updates them in the same pass.
                if writes_vectors:
                    # Once per node, after its final write for this execution - never per loop
                    # iteration, where the deep payload comparison would be paid N times to check
                    # a value only the last write settles.
                    assert_baseline_mirror(
                        ctx, action_graph_result_doc, node, start_node_id=start_node_id
                    )
                    exhausted_pairs_hit.update(
                        ActionGraph.fire_redirects(
                            ctx,
                            bind_dict=bind_dict,
                            action_graph_result_doc=action_graph_result_doc,
                            scheduled=scheduled,
                            topology=topology,
                            frontier=frontier,
                        ).exhausted
                    )

                await user_action_graph_result.save_run_doc(ctx, action_graph_result_doc)
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

            # O(nodes), no payload comparison, so it is cheap enough to leave on. This is the
            # half that catches a deletion applied to one structure and not the other - drift no
            # per-node check can see, because there is nothing left to compare.
            assert_vector_covers_output(ctx, action_graph_result_doc)

            if writes_vectors and getattr(
                action_graph_result_doc.result, "open_continue_run", None
            ):
                # The operation is finished when every participant holds its turn. Back to None
                # rather than an empty value, the same discipline the other run-state fields
                # follow, so an ordinary run's document never carries the key.
                remaining = ActionGraph.outstanding_continue_run(
                    action_graph_result_doc, graph_map
                )
                if remaining is None or not remaining[2]:
                    action_graph_result_doc.result.open_continue_run = None
                    logger.bind(ctx=ctx, **bind_dict).info(
                        "Continue-run complete: every participant recorded its turn"
                    )
                else:
                    logger.bind(
                        ctx=ctx, **bind_dict, still_owing=sorted(remaining[2])
                    ).info("Continue-run still outstanding; it will resume on the next run")

            if cyclic:
                # Keep the outstanding-turn record to what is genuinely outstanding, so a resumed
                # run re-arms only real work and the map does not grow with every turn the run
                # completed normally.
                still_open = ActionGraph.outstanding_turns(action_graph_result_doc, topology)
                action_graph_result_doc.result.open_turns = close_open_turns(
                    action_graph_result_doc.result.open_turns,
                    [
                        key
                        for key in (action_graph_result_doc.result.open_turns or {})
                        if key not in still_open
                    ],
                )

            # Update action result graph as success or waiting
            logger.bind(ctx=ctx,**bind_dict).info(f"To wait : {to_wait}")
            if exhausted_pairs_hit:
                logger.bind(
                    ctx=ctx,
                    **bind_dict,
                    exhausted_pairs=[f"{i}|{j}" for i, j in sorted(exhausted_pairs_hit)],
                ).warning(
                    "Action Graph Status : Waiting - redirect budget exhausted. Raise the pair's "
                    "limit on the run and re-run against the same result id to continue."
                )
                action_graph_result_doc.status = EventResultStatus.waiting
            elif not to_wait:
                logger.bind(ctx=ctx,**bind_dict).info("Action Graph Status : Success")
                action_graph_result_doc.status = EventResultStatus.success
            else:
                logger.bind(ctx=ctx, **bind_dict).info("Action Graph Status : Waiting")
                action_graph_result_doc.status = EventResultStatus.waiting

            await user_action_graph_result.save_run_doc(
                ctx, action_graph_result_doc, bump_search_cache=True
            )
            if webhook:
                    await webhook.send(action_graph_result_doc.model_dump())
        
        except asyncio.CancelledError:
            logger.bind(ctx=ctx, **bind_dict).info("Action Graph Run Cancelled")
            action_graph_result_update: ActionGraphResultUpdate = ActionGraphResultUpdate(
                status=EventResultStatus.cancelled, error_message=TextObj(text="Cancelled")
            )
            await user_action_graph_result.update_action_graph_result(
                ctx, action_graph_result_doc.id, action_graph_result_update
            )
            raise

        except Exception as e:
            logger.bind(ctx=ctx, **bind_dict).exception(f"Error while graph run, {str(e)}")

            # Update action result graph as error
            action_graph_result_doc.status = EventResultStatus.error
            action_graph_result_doc.error_message = TextObj(text=f"{type(e).__name__}: {e}")
            await user_action_graph_result.save_run_doc(
                ctx, action_graph_result_doc, bump_search_cache=True
            )
            if webhook:
                await webhook.send(action_graph_result_doc.model_dump())

        logger.bind(ctx=ctx, **bind_dict).info("Action Graph Run Completed")
        return action_graph_result_doc



    
    @staticmethod
    def single_node_run_context(
        action_graph_result_doc: ActionGraphResultDoc, node: str
    ) -> NodeRunContext:
        """The context for a single-node rerun - "chat with this node" and the rerun button.

        Any graph with a start node gets a real turn on the baseline column - a DAG included,
        since it records a transcript now too. The reason is the same one a continue-run has: the
        node produces a new result, and a result that reaches `output[node]` without reaching
        slot 0 breaks the mirror `assert_baseline_mirror` exists to protect.

        Tagged with the node itself. It is the node the operation was aimed at, exactly as a
        continue-run is tagged with the node it was started from, so the two read the same way in
        a transcript.

        `run_node_action_with_existing_doc` records twice - once before the run with an empty
        output so the UI has something to show, once after with the real one. Both carry this
        context, so they share an identity and `upsert_entry` replaces rather than appending. That
        is the same progressive-write behaviour a loop node relies on, and it is why the empty
        first write does not leave a hole in the transcript.
        """
        graph = action_graph_result_doc.result
        topology = GraphTopology.build(graph)
        if not topology.start_node_id:
            # Nothing to tag the entry with. Part 1's guard would refuse the write; refuse the
            # context instead and let the rerun behave as it did before vectors existed.
            return NodeRunContext.for_dag(node)
        all_slots = tuple(str(index) for index in range(topology.K))
        # A run written before vectors existed reaches this path too, and `run_node` has no
        # scheduler to have seeded it. Without this the chat writes the only vector on the
        # document and every node that finished earlier reads as never having run.
        seed_baseline_from_output(
            action_graph_result_doc,
            start_node_id=topology.start_node_id,
            all_slots=all_slots,
        )
        return NodeRunContext(
            node=node,
            start_node_id=topology.start_node_id,
            tag=node,
            turn_no=ActionGraph.next_user_turn(action_graph_result_doc),
            # EVERY slot, not just the baseline. A redirect target reads its **own slot at the
            # redirect's context index** (`context_input._target_input`), never slot 0 - so a chat
            # written to the baseline alone is invisible the moment anything redirects back into
            # this node. The node would be told to try again while reading a transcript that does
            # not contain what the user just told it.
            #
            # Same shape as case (i): one node writing its own result across its own dimensions,
            # which is the row rule, not the column one. One turn number for all of them, so one
            # chat reads as one event whichever dimension you look at. At K == 1 that is slot 0
            # alone, and the entry mirrors the `ActionResult` the same execution appends.
            target_slots=all_slots,
            context_index=BASELINE_SLOT,
        )

    @staticmethod
    def next_user_turn(action_graph_result_doc: ActionGraphResultDoc) -> int:
        """The turn number a user operation takes - a continue-run, or a chat with one node.

        Highest turn recorded anywhere in the run, plus one.

        **Computed once for the whole run, never per node.** Each node owns its own vector, so
        "the highest turn in the baseline slot" is a per-node number, and per-node numbers drift
        apart the moment one node's history differs from another's - a node that never ran has an
        empty slot and would take turn 0 while its sibling takes turn 3. Reading a slot survives
        that (identity is exact-match within one node's slot, and turn numbers are allowed to be
        sparse), but "turn N of this run" stops being a thing that can be compared across nodes.
        Fixed once, the way a redirect turn is fixed when the turn is opened rather than when each
        node runs.

        The maximum is taken across **every slot of every node**, not just the baseline. These
        operations write into every dimension (see `single_node_run_context`), and a number drawn
        from the baseline alone lands mid-transcript in the others: on a graph whose slot 1 is
        already at turn 2, a chat numbered from the baseline's max of 0 takes turn 1 and sorts
        *between* two earlier redirect turns, reading as though the user had spoken in the middle
        of the loop. Scanning every slot makes the entry sort last wherever it is written.

        Turn numbers need not be dense, so the gaps this leaves in the baseline column are
        harmless - identity is exact-match and ordering only needs them to sort.

        Deliberately derived from the transcript rather than from `turn_counters`, which is the
        opposite of the rule for redirect turns - and safe here for the reason that rule exists.
        Part 3.5's bug was several turns in flight at once, each reading a count that had not yet
        been written. A continue-run opens exactly one turn, before any node has run.
        """
        vectors = getattr(action_graph_result_doc.result, "node_vectors", None) or {}
        highest = -1
        for vector in vectors.values():
            for slot_key in vector.slots or {}:
                for entry in vector.slot(slot_key).entries:
                    highest = max(highest, entry.turn_no)
        return highest + 1

    @staticmethod
    def resolve_run_context(
        *,
        action_graph_result_doc: ActionGraphResultDoc,
        scheduled: ScheduledNode,
        writes_vectors: bool,
        all_slots: Tuple[str, ...],
        start_node_id: str,
        data_parents: Optional[List[str]] = None,
        topology: Optional[GraphTopology] = None,
        continue_run_tag: Optional[str] = None,
        continue_run_turn_no: Optional[int] = None,
    ) -> NodeRunContext:
        """Which slots this execution writes, under whose provenance, at which turn (§1.4).

        **Case (i)** - every slot still empty. Nothing has redirected, so there is nothing to
        disambiguate: run normally and write the same result into *every* slot. Not waste - it
        establishes the baseline, and "reset to the start node" is only coherent if the baseline is
        uniform (OQ-2).

        **Case (ii)** - the slots are populated. Somebody has already redirected, so write only the
        slot the responsible R.N. owns. Case (i) writes broadly because there is no ambiguity;
        case (ii) writes narrowly because there is.

        Both cases apply whatever `K` is. A DAG has one slot, so case (i) seeds that one and
        case (ii) writes it - the transcript ends up mirroring `ActionDoc.results`, which is what
        makes the acyclic and cyclic paths the same code rather than two engines.

        `for_dag` - no vector at all - is now reserved for the one graph that cannot have one: no
        single root, so no start node, so nothing to tag an entry with. One isolated node and a
        pure data cycle are both that shape, both legal while validation is warn-only, and both
        keep behaving exactly as they did.
        """
        node = scheduled.node
        if not writes_vectors:
            return NodeRunContext.for_dag(node)

        frame = scheduled.stack.top
        # The target of the redirect that opened this turn reads differently from everyone else.
        # `frame.target` is why Part 3.5 put the target on the frame at all.
        is_target = frame is not None and frame.target == node

        vectors = getattr(action_graph_result_doc.result, "node_vectors", None) or {}
        vector = vectors.get(node)
        nothing_recorded_yet = vector is None or not any(
            vector.slot(slot).entries for slot in all_slots
        )

        if nothing_recorded_yet:
            return NodeRunContext(
                node=node,
                start_node_id=start_node_id,
                tag=start_node_id,
                turn_no=0,
                target_slots=all_slots,
                context_index=scheduled.context_index,
                data_parents=tuple(data_parents or ()),
            )

        # A continue-run: a fresh turn on the baseline column, tagged with the node it was started
        # from. Without it every re-execution took `(start, 0)` again, which is the identity the
        # forward pass already holds, so `upsert_entry` replaced that cell and the earlier output
        # was gone - the slot kept one entry while `ActionDoc.results` grew one per execution.
        # Appending under a fresh turn makes the baseline slot mirror that list, which is what it
        # is supposed to be.
        #
        # Only where there is no frame. A continue-run on a run with outstanding turns re-arms
        # those nodes with their original frame (RISK-12), and they belong to their turn, not to
        # this one.
        if frame is None and continue_run_tag and continue_run_turn_no is not None:
            return NodeRunContext(
                node=node,
                start_node_id=start_node_id,
                tag=continue_run_tag,
                turn_no=continue_run_turn_no,
                # Every slot, for the same reason a chat writes every slot: a redirect target
                # reads its own slot at the redirect's context index, so a continue-run recorded
                # on the baseline alone is invisible to any later turn that re-enters this node.
                target_slots=all_slots,
                context_index=scheduled.context_index,
                data_parents=tuple(data_parents or ()),
            )

        # Which dimensions this turn disturbs: the slots of the R.N.s inside R.S.(i, j),
        # precomputed on the topology. Every node executing in the turn writes its own result into
        # all of them - a row update. Falls back to the reading slot alone if the frame is missing,
        # which is the old single-slot behaviour and never wrong, only narrow.
        write_slots: Tuple[str, ...] = (scheduled.context_index,)
        if frame is not None and topology is not None:
            write_slots = topology.propagation_slots.get(
                (frame.target, frame.node), write_slots
            )

        return NodeRunContext(
            node=node,
            start_node_id=start_node_id,
            tag=scheduled.tag or start_node_id,
            turn_no=scheduled.turn_no,
            target_slots=write_slots,
            context_index=scheduled.context_index,
            is_redirect_target=is_target,
            data_parents=tuple(data_parents or ()),
        )

    @staticmethod
    def fire_redirects(
        ctx: Context,
        *,
        bind_dict: Dict[str, str],
        action_graph_result_doc: ActionGraphResultDoc,
        scheduled: ScheduledNode,
        topology: GraphTopology,
        frontier: FrontierScheduler,
    ) -> "RedirectOutcome":
        """Close the turn this node ends, then open one per redirect edge leaving it (§1.8).

        Order matters. The node that fires a redirect is also the node whose arrival closes the
        previous turn, so the pop has to happen first - otherwise the stack grows by one on every
        turn and the depth never comes back down.

        The re-run set is the precomputed `R.S.(i, j)`: ancestors of *j* clipped to subgraph(*i*)
        re-run because *i* re-runs and feeds down the loop, descendants of *j* re-run because *j*
        recomputes and feeds them. A branch incomparable to *j* takes no part - it is not queued,
        delivers no queue, and the node reads its stored value instead.
        """
        node = scheduled.node
        stack = scheduled.stack.close_if_complete(node, scheduled.context_index)
        exhausted: List[Tuple[str, str]] = []
        fired = 0

        for edge in topology.redirect_edges:
            if edge.source != node:
                continue
            target = edge.target
            # Not `.get(...)`: `context_index` is keyed by exactly the set of `e.source` over
            # `redirect_edges`, and we only get here when `node == edge.source` for an edge in that
            # same tuple - so a miss is impossible, and both come from one `GraphTopology.build`.
            # A silent `continue` here would mean the cycle never runs and the run still reports
            # success; if the invariant is ever broken, fail loudly instead.
            context_index = topology.context_index[node]

            graph = action_graph_result_doc.result
            if is_disabled(graph.redirect_budgets, target, node):
                # Switched off on purpose. Skip it quietly - reporting it as exhausted would park
                # the run and ask a human to raise a limit they deliberately set to zero.
                logger.bind(
                    ctx=ctx, **bind_dict, node_id=node, redirect_target=target
                ).debug("Redirect edge is disabled (limit 0); not firing")
                continue
            if not may_fire(graph.redirect_budgets, target, node):
                # D5: the run parks and asks a human rather than silently truncating the loop.
                # Until Part 8's inbox exists nobody is notified, so this logs loudly and the pair
                # is left readable on the document (RISK-3).
                exhausted.append((target, node))
                logger.bind(
                    ctx=ctx,
                    **bind_dict,
                    node_id=node,
                    redirect_target=target,
                    exhausted_pair=f"{target}|{node}",
                ).warning(
                    "Redirect budget exhausted; run will park as `waiting` for a human to raise "
                    "the limit or stop it"
                )
                continue
            graph.redirect_budgets = record_firing(
                graph.redirect_budgets,
                target,
                node,
                default_limit=SettingsProvider.sget().ACTION_GRAPH_REDIRECT_BUDGET_DEFAULT,
            )
            fired += 1

            # `turn_no` is fixed here rather than when the node runs: this is the m-th turn this
            # tag has opened, and it stays that whoever asks and however often. Derived at run time
            # it would have to be "the next turn", which is fresh by construction, so a resumed run
            # would redo work it had already recorded.
            #
            # The number comes from a counter that advances when a turn is **opened**, never from a
            # count of turns already recorded. A queued turn has written nothing, so with two edges
            # leaving this node - every one of them writing this same column, because a slot
            # belongs to the node and not to the edge (§1.3) - a count of the transcript is stale
            # for exactly the turns that are still in flight, and hands the same `(turn_no, tag)` to
            # two of them. They then land in one cell and the later erases the earlier.
            graph.turn_counters, turn_no = next_turn(
                graph.turn_counters,
                node,
                context_index,
                # Only consulted when this column has no counter yet, which is what a run started
                # before `turn_counters` existed looks like: its transcript is the only record of
                # how many turns it opened, so it seeds the machine rather than authoring it.
                # Run-wide, not per-column. The per-column count could not see a number taken
                # by a user operation, which writes into every slot: a chat numbered 3 sat beside
                # a redirect turn also numbered 3. This floor subsumes the old per-column seed -
                # it is always at least as large - so a run begun before `turn_counters` existed
                # still continues its numbering rather than restarting it.
                seed=ActionGraph.next_user_turn(action_graph_result_doc),
            )
            # RISK-12: remember that this turn was handed out and which edge opened it, so a
            # crash between here and the turn finishing leaves something that can name the nodes
            # that still owe it. The counter alone cannot: it says a number was issued, not who
            # owed work for it.
            graph.open_turns = record_open_turn(
                graph.open_turns, node, context_index, turn_no, target=target
            )
            opened = stack.push(node, context_index, target=target, turn_no=turn_no)
            rerun_set = topology.relevant_subgraph.get((target, node), frozenset())
            queued = frontier.rearm(rerun_set, lap=scheduled.lap + 1, stack=opened)
            logger.bind(
                ctx=ctx,
                **bind_dict,
                node_id=node,
                redirect_target=target,
                context_index=str(context_index),
                turn_no=turn_no,
                lap=scheduled.lap + 1,
                rerun_set=list(queued),
            ).info("Redirect fired: re-arming the relevant subgraph")

        return RedirectOutcome(exhausted=exhausted, fired=fired)

    @staticmethod
    def outstanding_turns(
        action_graph_result_doc: ActionGraphResultDoc, topology: GraphTopology
    ) -> Dict[str, Tuple[str, str, str, int, Tuple[str, ...]]]:
        """Which handed-out turns are not fully recorded, and who still owes them (RISK-12).

        A turn is complete when every node in its `R.S.(i, j)` **that has run at all** holds an
        entry `(tag=j, turn=t)` in the turn's column. The has-run clause matters: `R.S.` includes
        descendants of *j* that a crash may have stopped before they ever executed, and a node
        with no result owes nothing - it will pick the context up when it runs.

        Returns `key -> (source, target, context_index, turn_no, nodes still owing)`.
        """
        graph = action_graph_result_doc.result
        open_turns = getattr(graph, "open_turns", None) or {}
        output = getattr(graph, "output", None) or {}
        outstanding: Dict[str, Tuple[str, str, str, int, Tuple[str, ...]]] = {}
        for key, target in open_turns.items():
            try:
                source, context_index, turn_no = split_open_turn_key(key)
            except ValueError:
                continue  # an unreadable key is not worth failing a run over
            taking_part = topology.relevant_subgraph.get((target, source), frozenset())
            owing = tuple(
                sorted(
                    n
                    for n in taking_part
                    if n in output
                    and not is_node_done(
                        action_graph_result_doc,
                        n,
                        context_index=context_index,
                        tag=source,
                        turn_no=turn_no,
                    )
                )
            )
            if owing:
                outstanding[key] = (source, target, context_index, turn_no, owing)
        return outstanding

    @staticmethod
    def outstanding_continue_run(
        action_graph_result_doc: ActionGraphResultDoc,
        graph: Dict[str, List[str]],
    ) -> Optional[Tuple[str, int, Set[str]]]:
        """The continue-run that was started and has not finished, and who still owes it.

        Returns `(root, turn_no, nodes still owing)`, or None when nothing is outstanding - which
        is every ordinary run.

        **Read on every run, not only when a continue-run is requested.** That is deliberate and
        it mirrors `resume_outstanding_turns`: the operation lives on the document, so whatever
        the next request happens to be picks it up. It has to work that way here, because the API
        refuses a continue-run on a `waiting` run (405) - so the run that resumes one is a plain
        run, issued after the human approved whatever gate stopped it.

        A participant that has recorded the turn is finished with it; one that has not still owes
        it, whether it was blocked by a gate, never reached, or the process died. `owing` empty
        means the operation is complete and the record can go.
        """
        key = getattr(action_graph_result_doc.result, "open_continue_run", None)
        if not key:
            return None
        try:
            root, turn_no = split_continue_run_key(key)
        except ValueError:
            return None  # an unreadable key is not worth failing a run over

        subtree = ActionGraph._subtree_of(root, graph)
        participants = subtree - {root}
        owing = {
            node
            for node in participants
            if not is_node_done(
                action_graph_result_doc,
                node,
                context_index=BASELINE_SLOT,
                tag=root,
                turn_no=turn_no,
            )
        }
        return root, turn_no, owing

    @staticmethod
    def _subtree_of(root: str, graph: Dict[str, List[str]]) -> Set[str]:
        """`root` and everything below it on the data edges. Cycle-safe."""
        seen: Set[str] = {root}
        stack = [root]
        while stack:
            for child in (graph or {}).get(stack.pop(), ()) or ():
                if child not in seen:
                    seen.add(child)
                    stack.append(child)
        return seen

    @staticmethod
    def resume_outstanding_turns(
        ctx: Context,
        *,
        bind_dict: Dict[str, str],
        action_graph_result_doc: ActionGraphResultDoc,
        topology: GraphTopology,
        frontier: FrontierScheduler,
    ) -> int:
        """Re-arm the nodes that still owe an outstanding turn, rather than opening a new one.

        **Complete the turn; do not roll it back.** The nodes that did record it really executed
        and really cost money, and `merge_slots` is explicitly "pure addition - nothing is ever
        overwritten and nothing is dropped". Rollback would also have to know which nodes owed the
        turn, which needs the edge - the thing that was not saved until now.

        The budget is **not** re-charged. The turn was charged when it was opened; finishing it is
        the work that charge already paid for. Charging again is precisely the cost RISK-12
        describes - a crash permanently costing the user one of their turns.

        The frame is reconstructed rather than restored: everything it holds is either in the key
        or was recorded beside it, which is why persisting `turn -> edge` is enough and a
        serialised frame is not needed.
        """
        outstanding = ActionGraph.outstanding_turns(action_graph_result_doc, topology)
        for key, (source, target, context_index, turn_no, owing) in sorted(outstanding.items()):
            frame = RedirectStack().push(
                source, context_index, target=target, turn_no=turn_no
            )
            queued = frontier.rearm(owing, lap=0, stack=frame)
            logger.bind(
                ctx=ctx,
                **bind_dict,
                node_id=source,
                redirect_target=target,
                context_index=context_index,
                turn_no=turn_no,
                re_armed=list(queued),
            ).warning(
                "Resuming an outstanding turn: it was handed out and never completed, so the "
                "nodes that still owe it are re-armed with the original frame rather than a new "
                "turn being opened (RISK-12). The budget is not charged again."
            )
        return len(outstanding)

    @staticmethod
    def contributing_upstream(
        action_graph_result_doc: ActionGraphResultDoc,
        node: str,
        upstream_nodes: List[str],
        *,
        run_ctx: NodeRunContext,
    ) -> List[str]:
        """`upstream_nodes` minus the parents that declined to feed `node` on this turn.

        The consumer OQ-9's store was built for and deliberately left without: Part 3.5 recorded
        the verdicts' shape, Part 4 subtracts them, Part 5 writes them.

        **Not a readiness question**, which is the trap the plan warns about twice. A parent that
        declines still ran and still produced output, so `is_work_done` passes and nothing blocks -
        this engine cannot deadlock on a declined edge. It is input composition: the parent must
        disappear from the prompt it would otherwise contribute to.

        **Absence means the parent contributes.** Nothing writes a verdict until Part 5, so every
        graph running today has `edge_decisions is None` and gets its parent list back unchanged -
        which is what makes this safe to land now rather than under pressure later. Absence also
        covers a parent outside `R.S.` that did not re-run this turn: it is not declining, it is
        contributing the value it already has (case (ii)).

        The parent and child of an edge always run in the **same** turn - `rearm` hands one stack
        to every node in `R.S.` and the frame fixes the turn - so the child looks this up with its
        own run context and nothing is threaded from the parent's execution.
        """
        decisions = getattr(action_graph_result_doc.result, "edge_decisions", None)
        if not decisions:
            return upstream_nodes
        return contributing_parents(
            decisions,
            node,
            upstream_nodes,
            context_index=run_ctx.context_index,
            turn_no=run_ctx.turn_no,
        )

    @staticmethod
    def _turns_recorded(
        action_graph_result_doc: ActionGraphResultDoc, node: str, context_index: str
    ) -> int:
        """How many turns this tag has already **recorded** in its own slot.

        This used to be the authority on the next turn number, and could not be: it sees only the
        turns that have finished writing, so while several are in flight it returns the same value
        to each of them. `turn_counters` is the authority now.

        It survives as the **seed** - the value the counter starts from when a column has none.
        That is exactly a run begun before `turn_counters` existed: the transcript is the only
        record of how many turns it opened, so it continues the numbering rather than restarting it
        and overwriting what it already wrote.
        """
        vectors = getattr(action_graph_result_doc.result, "node_vectors", None) or {}
        vector = vectors.get(node)
        if vector is None:
            return 0
        return len(vector.slot(context_index).tagged(node))

    @staticmethod 
    def get_subgraph_map(ctx: Context,node_id: str, graph: Optional[Dict[str, List[str]]] = None):
        if not graph:
            return graph
        subgraph = {}
        visited: Set[str] = set()
        q = deque()
        q.append(node_id)
        while q:
            fst = q.popleft()
            if fst in visited:
                continue
            visited.add(fst)
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
        action_response: Dict[str, ActionDoc] = node_results(action_graph_result_doc)

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
            action_graph_result_doc.status = EventResultStatus.processing
            # **No approvals are withdrawn here, deliberately.** A chat is an iteration on a node
            # the human is already working on, not a re-run of the graph, and clearing the flags
            # would re-block the node's children on every message.
            await user_action_graph_result.save_run_doc(
                ctx, action_graph_result_doc, bump_search_cache=True
            )
            await ActionGraph.run_node_action_with_existing_doc(
                ctx=ctx,
                app_state=app_state,
                node=action_graph_node_id,
                action_graph_input_dict=action_graph_input_dict,
                action_graph_result_doc=action_graph_result_doc,
                # The single-node rerun path has no scheduler, so no redirect context to inherit
                # - but it still produces a new result, and on a cyclic graph that result has to
                # land in the baseline slot or `output[node]` and slot 0 drift apart. `for_dag`
                # writes no vector at all, which is why chatting with a node left the slot holding
                # the previous output while `results` grew.
                #
                # A single-node rerun is a continue-run whose subtree is one node - except that
                # node *does* re-run rather than being skipped - so it takes the same shape: its
                # own id as the tag, the next baseline turn, the baseline slot alone.
                run_ctx=ActionGraph.single_node_run_context(
                    action_graph_result_doc, action_graph_node_id
                ),
                user_actions=user_actions,
                user_action_graph_result=user_action_graph_result,
                notify=notify,
                webhook=webhook,
                run_single_node=True,
            )
            action_graph_result_doc.status = EventResultStatus.success
            await user_action_graph_result.save_run_doc(
                ctx, action_graph_result_doc, bump_search_cache=True
            )
        except asyncio.CancelledError:
            logger.bind(
                ctx=ctx,
                code=AppCode.ACTION_GRAPH_RUN,
                graph_id=action_graph_result_doc.result.id,
                graph_run_id=action_graph_result_doc.id,
                action_id=node_action_doc.id,
            ).info("Action Graph Node Run Cancelled")
            action_graph_result_doc.status = EventResultStatus.cancelled
            action_graph_result_doc.error_message = TextObj(text="Cancelled")
            await user_action_graph_result.save_run_doc(
                ctx, action_graph_result_doc, bump_search_cache=True
            )
            raise

        except Exception as e:
            logger.bind(
                ctx=ctx,
                code=AppCode.ACTION_GRAPH_RUN,
                graph_id=action_graph_result_doc.result.id,
                graph_run_id=action_graph_result_doc.id,
                action_id=node_action_doc.id,
            ).exception(f"Error in rerun node of Action Graph: {str(e)}")
            # Update action result graph as error
            action_graph_result_doc.status = EventResultStatus.error
            action_graph_result_doc.error_message = TextObj(text=f"{type(e).__name__}: {e}")
            await user_action_graph_result.save_run_doc(
                ctx, action_graph_result_doc, bump_search_cache=True
            )
            if webhook:
                await webhook.send(action_graph_result_doc.model_dump())

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
    async def is_work_done(
        values: List[str],
        action_graph_result_doc: ActionGraphResultDoc,
        *,
        context_index: int | str = BASELINE_SLOT,
        tag: Optional[str] = None,
        turn_no: Optional[int] = None,
    ) -> bool:
        """Are all of `values` done for the context being asked about?

        Takes the run document rather than the output map, because "done" is no longer a question
        `output` alone can answer once a node can be entered under more than one context. At
        `K == 1` this is still exactly "is every key present in `output`" - see `is_node_done`.
        """
        for value in values or ():
            if not is_node_done(
                action_graph_result_doc,
                value,
                context_index=context_index,
                tag=tag,
                turn_no=turn_no,
            ):
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
    def withdraw_approvals(
        ctx: Context,
        *,
        action_graph_result_doc: ActionGraphResultDoc,
        nodes: Iterable[str],
        reason: str,
    ) -> List[str]:
        """Take back the human approvals held by nodes whose output is about to be replaced.

        **An approval belongs to the output it was given for.** A reviewer approved `d#21`; once
        `d` re-runs and produces `d#23`, that approval describes nothing. Left standing it lets
        the new output through unreviewed - which is the gate quietly not working rather than the
        gate being satisfied.

        Called by each operation with **its own** set, because which nodes re-execute differs and
        the difference is the whole point. Only two operations withdraw at all:

          - **rerun from a node** - exactly the nodes that were emptied, root included when the
            root itself was emptied and excluded on a downstream-only rerun.
          - **continue-run (new only)** - the subtree minus the root. The root is deliberately
            skipped, so its output does not change and its approval still describes what the
            children read. Clearing it instead blocks the very subtree the operation exists to
            re-run.

        The other three deliberately do **not**, and each for its own reason - see the note at the
        branch itself: a **resume** of an outstanding continue-run, a **rerun with no node id**
        (which drives an unfinished run to completion rather than starting it over), and a
        **chat**, which is iteration on a node the human is already working on.

        Returns the nodes whose approval actually changed, so the log says what was withdrawn
        rather than what was walked.
        """
        details = (action_graph_result_doc.result.node_details or {})
        withdrawn: List[str] = []
        for node in sorted(set(nodes)):
            entry = details.get(node)
            if entry is None:
                continue
            data = entry.data
            if data.user_review_done or data.user_input_done:
                data.user_review_done = False
                data.user_input_done = False
                withdrawn.append(node)
        if withdrawn:
            logger.bind(
                ctx=ctx,
                code=AppCode.ACTION_GRAPH,
                reason=reason,
                withdrawn=withdrawn,
            ).info("Withdrew user approvals for nodes whose output is being replaced")
        return withdrawn

    @staticmethod
    async def empty_downstream_nodes(
        ctx: Context,
        action_graph_node_id: str,
        action_graph_result_doc: ActionGraphResultDoc,
        is_empty_downstream_only: bool = False,
    ) -> Set[str]:
        """Drop the stored results for a node and everything downstream, so a rerun recomputes them.

        **Results only.** Withdrawing the human approvals of the nodes being replaced is
        `withdraw_approvals`, called by each operation with its own set - the sets differ, and a
        continue-run does not empty anything at all, so folding the two jobs together meant one
        of them was always being asked the wrong question.

        Errors propagate. This used to catch `Exception` around the whole per-node body, which had
        two costs: a real error (a lite result doc reaching `forget_node`, say) was logged as
        "emptied nothing" and the rerun then ran against stale results and still reported success;
        and because the `to_visit` extension sat inside the same `try`, one bad node silently
        pruned its entire subtree from the walk. A half-emptied graph is worse than an unemptied
        one - it looks like it worked.

        The one data gap that is genuinely expected - an edge naming a node with no `node_details`
        entry - is handled explicitly and keeps the walk going.
        """
        logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH_RUN).info("Emptying downstream nodes")
        graph: Dict[str, List[str]] = action_graph_result_doc.result.graph or {}
        node_details_map: Dict[str, Node] = action_graph_result_doc.result.node_details or {}

        to_visit = [action_graph_node_id]
        # A node reachable by several paths would otherwise be re-expanded once per path,
        # which is exponential in the number of diamonds in the graph.
        visited: Set[str] = set()
        emptied: Set[str] = set()
        without_details: Set[str] = set()

        while to_visit:
            node = to_visit.pop(0)
            if node in visited:
                continue
            visited.add(node)

            node_details = node_details_map.get(node)
            if node_details is None:
                # An edge can name a node the details map has no entry for. Not a reason to stop:
                # whatever sits below it still holds stale results.
                without_details.add(node)
            elif node_details.data.actionId and not (
                # `is_empty_downstream_only` keeps the node the rerun starts from. Compared by
                # value - the seed is the same object on the first pop, so `is not` happened to
                # work, but only by that accident.
                is_empty_downstream_only and node == action_graph_node_id
            ):
                forget_node(action_graph_result_doc, node)
                emptied.add(node)
            to_visit += graph.get(node, [])

        if without_details:
            logger.bind(
                ctx=ctx,
                code=AppCode.ACTION_GRAPH_RUN,
                action_graph_node_id=action_graph_node_id,
                nodes_without_details=sorted(without_details),
            ).warning("Nodes reachable in the graph have no node_details entry; walked past them")

        logger.bind(
            ctx=ctx,
            code=AppCode.ACTION_GRAPH_RUN,
            action_graph_node_id=action_graph_node_id,
            count_visited=len(visited),
            count_emptied=len(emptied),
        ).info("Emptied downstream nodes")
        return emptied

    @staticmethod
    async def run_action_with_text_split_and_loop_concurrently(
        ctx: Context,
        app_state: AppState,
        bind_dict: Dict[str, str],
        node: str,
        node_details_map: Dict[str, Node],
        node_action_map: Dict[str, str],
        action_graph_result_doc: ActionGraphResultDoc,
        action_graph_input_dict: Dict[str, Any],
        user_actions: UserActions,
        user_action_graph_result: UserActionGraphResult,
        notify: Notify,
        # The execution this node result belongs to: which slots it writes, under whose
        # provenance, at which turn. `for_dag` when the graph has no redirect edges.
        run_ctx: NodeRunContext,
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
            action_graph_result_doc=action_graph_result_doc,
            run_ctx=run_ctx,
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
        action_graph_result_doc: ActionGraphResultDoc,
        action_graph_input_dict: Dict[str, Any],
        user_actions: UserActions,
        user_action_graph_result: UserActionGraphResult,
        notify: Notify,
        # The execution this node result belongs to: which slots it writes, under whose
        # provenance, at which turn. `for_dag` when the graph has no redirect edges.
        run_ctx: NodeRunContext,
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

                iter_app_state = app_state.child_context(
                    action_graph_id=app_state.action_graph_id,
                    action_graph_result_id=app_state.action_graph_result_id,
                    action_graph_node_id=app_state.action_graph_node_id,
                )
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
                        record_node_result(
                            action_graph_result_doc, action_result, run_ctx
                        )
                        # Throttled: this exists so the UI can show rows landing, and the
                        # unconditional node-level save in `run_action_graph_v2` follows the loop.
                        await user_action_graph_result.save_run_doc(
                            ctx, action_graph_result_doc, throttle=True
                        )
            except Exception:
                logger.bind(ctx=ctx, **bind_dict).exception(
                    f"Error in processing batch of excel row {idx + 1} run action"
                )

        NodeUnitsRecorder.record(
            run_result=action_graph_result_doc.result,
            node_id=node,
            node_data=node_details_map.get(node).data,
            total=len(batches),
            succeeded=len(inter_action_result),
        )

    @staticmethod
    async def run_action_with_file_split_and_loop_concurrently(
        ctx: Context,
        app_state: AppState,
        bind_dict: Dict[str, str],
        node: str,
        node_details_map: Dict[str, Node],
        node_action_map: Dict[str, str],
        action_graph_result_doc: ActionGraphResultDoc,
        action_graph_input_dict: Dict[str, Any],
        user_actions: UserActions,
        user_action_graph_result: UserActionGraphResult,
        notify: Notify,
        # The execution this node result belongs to: which slots it writes, under whose
        # provenance, at which turn. `for_dag` when the graph has no redirect edges.
        run_ctx: NodeRunContext,
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
            action_graph_result_doc=action_graph_result_doc,
            run_ctx=run_ctx,
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
        action_graph_result_doc: ActionGraphResultDoc,
        action_graph_input_dict: Dict[str, Any],
        user_actions: UserActions,
        user_action_graph_result: UserActionGraphResult,
        notify: Notify,
        # The execution this node result belongs to: which slots it writes, under whose
        # provenance, at which turn. `for_dag` when the graph has no redirect edges.
        run_ctx: NodeRunContext,
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
                record_node_result(
                    action_graph_result_doc, action_result, run_ctx
                )

    @staticmethod
    async def run_action_with_loop(
        ctx: Context,
        app_state: AppState,
        bind_dict: Dict[str, str],
        node: str,
        upstream_nodes: List[str],
        node_details_map: Dict[str, Node],
        node_action_map: Dict[str, str],
        action_graph_result_doc: ActionGraphResultDoc,
        action_graph_input_dict: Dict[str, Any],
        action_graph_node_id: str | None,
        is_continue_run_graph: bool,
        user_actions: UserActions,
        user_action_graph_result: UserActionGraphResult,
        notify: Notify,
        # The execution this node result belongs to: which slots it writes, under whose
        # provenance, at which turn. `for_dag` when the graph has no redirect edges.
        run_ctx: NodeRunContext,
    ):
        # A parent that declined its edge on this turn is left out of the input it would otherwise
        # contribute to (OQ-9). Not a readiness question - it ran and produced output, so nothing
        # blocks; it is input composition. Absence of a verdict means the parent contributes, and
        # nothing writes a verdict until Part 5, so today this returns `upstream_nodes` unchanged.
        #
        # Before the compose below, not after: the target's read replaces the parent list outright,
        # and a declined parent must disappear from the list the ordinary path is about to read.
        contributing = ActionGraph.contributing_upstream(
            action_graph_result_doc, node, upstream_nodes, run_ctx=run_ctx
        )
        # What this node reads, composed for the context it is running under: the redirect target
        # reads its own slot plus the redirector's handback, every other participant reads its
        # data parents' slots. Returns the caller's own list and the live `node_results` map by
        # identity for a DAG and for the baseline. See `context_input`.
        #
        # A separate name on purpose. `upstream_nodes` means "this node's data parents" everywhere
        # else in the engine, and for a redirect target this list is neither its parents nor even
        # only other nodes - it holds the node **itself**, which is how its own transcript becomes
        # a section of its own prompt. Rebinding `upstream_nodes` to that would leave a parameter
        # whose name lies to everything downstream of here.
        input_sources, action_response = contextual_input(
            action_graph_result_doc, node, contributing, run_ctx=run_ctx
        )
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
            upstream_nodes=input_sources,
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
            action_graph_result_doc=action_graph_result_doc,
            run_ctx=run_ctx,
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
        action_graph_result_doc: ActionGraphResultDoc,
        action_graph_input_dict: Dict[str, Any],
        action_graph_node_id: str | None,
        is_continue_run_graph: bool,
        user_actions: UserActions,
        user_action_graph_result: UserActionGraphResult,
        notify: Notify,
        # The execution this node result belongs to: which slots it writes, under whose
        # provenance, at which turn. `for_dag` when the graph has no redirect edges.
        run_ctx: NodeRunContext,
    ):
        # Declined parents dropped, then composed by role - see the notes in
        # `run_action_with_loop` above, `edge_decision` and `context_input`, including why the
        # composed list gets a name of its own rather than rebinding `upstream_nodes`.
        contributing = ActionGraph.contributing_upstream(
            action_graph_result_doc, node, upstream_nodes, run_ctx=run_ctx
        )
        input_sources, action_response = contextual_input(
            action_graph_result_doc, node, contributing, run_ctx=run_ctx
        )
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
            upstream_nodes=input_sources,
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
                node=node,
                action_graph_input_dict=action_input.model_dump(),
                action_graph_result_doc=action_graph_result_doc,
                run_ctx=run_ctx,
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
                record_node_result(
                    action_graph_result_doc, action_result, run_ctx
                )

        memory_profile = await Profiler.do_memory_profile(ctx)
        logger.bind(ctx=ctx, **bind_dict, memory_profile=memory_profile).info(
            f"Memory profile for Action run {action_result.name}:{action_result.version}"
        )

    @staticmethod
    async def run_node_action_with_existing_doc(
        ctx: Context,
        app_state: AppState,
        node: str,
        action_graph_input_dict: Dict[str, Any],
        action_graph_result_doc: ActionGraphResultDoc,
        user_actions: UserActions,
        user_action_graph_result: UserActionGraphResult,
        # The execution this node result belongs to: which slots it writes, under whose
        # provenance, at which turn. `for_dag` when the graph has no redirect edges.
        run_ctx: NodeRunContext,
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
            record_node_result(
                action_graph_result_doc, node_action_doc, run_ctx
            )
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
        # `run_node` discards this return value, so today the stored result is correct only by
        # aliasing: `output[node]` IS `node_action_doc`, which `run_action_doc` mutates in place.
        # That holds because pydantic's `revalidate_instances` defaults to "never", so storing an
        # ActionDoc keeps the same object rather than copying it. Recording the result explicitly
        # costs nothing and means a rerun no longer depends on that default holding.
        if run_single_node:
            match action_result:
                case ActionDoc():
                    record_node_result(
                        action_graph_result_doc, action_result, run_ctx
                    )

        return action_result
    
    
    @staticmethod
    async def run_action_with_loop_concurrently(
        ctx: Context,
        app_state: AppState,
        bind_dict: Dict[str, str],
        node: str,
        upstream_nodes: List[str],
        node_details_map: Dict[str, Node],
        node_action_map: Dict[str, str],
        action_graph_result_doc: ActionGraphResultDoc,
        action_graph_input_dict: Dict[str, Any],
        action_graph_node_id: str | None,
        is_continue_run_graph: bool,
        user_actions: UserActions,
        user_action_graph_result: UserActionGraphResult,
        action_inputs: List[DataBlock],
        notify: Notify,
        # The execution this node result belongs to: which slots it writes, under whose
        # provenance, at which turn. `for_dag` when the graph has no redirect edges.
        run_ctx: NodeRunContext,
    ):
        # The live per-node map, read through the document rather than held as a
        # binding. See `node_results`.
        action_response: Dict[str, ActionDoc] = node_results(action_graph_result_doc)
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
                    task_app_state = app_state.child_context(
                        action_graph_id=app_state.action_graph_id,
                        action_graph_result_id=app_state.action_graph_result_id,
                        action_graph_node_id=app_state.action_graph_node_id,
                    )
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
                            node=node,
                            action_graph_input_dict=action_input.model_dump(),
                            action_graph_result_doc=action_graph_result_doc,
                            run_ctx=run_ctx,
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
                            record_node_result(
                                action_graph_result_doc, collated, run_ctx
                            )
                            # Throttled: the collation grows with every iteration, so writing it
                            # each time costs n^2/2 bytes to persist n results. The final
                            # collation below and the node-level save both write unconditionally.
                            await user_action_graph_result.save_run_doc(
                                ctx, action_graph_result_doc, throttle=True
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
        NodeUnitsRecorder.record(
            run_result=action_graph_result_doc.result,
            node_id=node,
            node_data=node_details_map.get(node).data,
            total=len(action_inputs),
            succeeded=sum(1 for r in inter_action_results if isinstance(r, ActionDoc)),
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
        record_node_result(
            action_graph_result_doc, collated, run_ctx
        )
        await user_action_graph_result.save_run_doc(ctx, action_graph_result_doc)
        logger.bind(
            ctx=ctx, **bind_dict, action_graph_result_doc_id=action_graph_result_doc.id
        ).info("run_action_with_loop_concurrently complete")