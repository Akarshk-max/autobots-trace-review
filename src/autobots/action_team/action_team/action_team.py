import asyncio
import json
import re
import time
from io import BytesIO
from typing import Any, Dict, List, Optional

from fastapi import BackgroundTasks
from loguru import logger
from pydantic import BaseModel
from pydantic_extra_types.pendulum_dt import DateTime
from tenacity import retry, stop_after_attempt, wait_fixed

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import DataBlock, TextObj
from src.autobots.action.action.space_actions import SpaceActions
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action.action_result.action_result_doc_model import ActionResultDoc
from src.autobots.action.action_result.user_action_result import UserActionResult
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action_graph.action_graph.action_graph import ActionGraph
from src.autobots.action_team.action_team.action_team_doc_model import (
    ActionTeamDoc,
    ActionTeamResult,
    TeamMember,
    TeamMemberRole,
    ThreadEntry,
    ThreadEntryRole,
)
from src.autobots.action_team.action_team_result.action_team_result_model_doc import (
    ActionTeamResultDoc,
    ActionTeamResultUpdate,
)
from src.autobots.action_team.action_team_result.user_action_team_result import UserActionTeamResult
from src.autobots.api.webhook import Webhook
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.core.clock.clock import Clock
from src.autobots.core.heartbeat.running_executions import HeartBeatRunRegistry
from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.run_metrics.run_metrics_writer import RunMetricsWriter
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.space.space_auth import is_space_member_a_admin


class Delegation(BaseModel):
    member_id: str
    member_action_id: str
    member_action_run_id : str | None = None
    task: str


class LeaderDecision(BaseModel):
    is_complete: bool = False
    summary: str = ""
    delegations: List[Delegation] = []


class ActionTeam:
    @staticmethod
    async def run_in_background(
        ctx: Context,
        app_state: AppState,
        action_team_doc: ActionTeamDoc,
        input_dict: Dict[str, Any],
        user_actions: UserActions,
        user_action_team_result: UserActionTeamResult,
        action_team_result_id: str = None,
        background_tasks: Optional[BackgroundTasks] = None,
        webhook: Optional[Webhook] = None,
        fire_and_forget: bool = None,
        batch_id: str | None = None,
    ) -> Optional[ActionTeamResultDoc]:
        logger.bind(
            ctx=ctx,
            user_id=action_team_doc.user_id,
            action_team_id=action_team_doc.id,
            action_team_name=action_team_doc.name,
        ).info("Action Team Run started")
        
        try:
            action_team_result_doc = await user_action_team_result.init(
                app_state=app_state,
                action_team_result_id=action_team_result_id,
                action_team_doc=action_team_doc,
                input_dict=input_dict,
                db=user_actions.db,
                user=user_actions.user,
                ctx=ctx,
                batch_id=batch_id,
            )
            # Adding input to the result object!
            action_team_result_doc.result.results.append(
                ActionTeamResult(
                    input=input_dict
                )
            )
            action_team_result_update: ActionTeamResultUpdate = ActionTeamResultUpdate(
                result=action_team_result_doc.result
            )
            action_team_result_doc: ActionTeamResultDoc = (
                await user_action_team_result.update_action_team_result(
                    ctx=ctx,
                    action_team_result_id=action_team_result_doc.id,
                    action_team_update=action_team_result_update,
                )
            )
            if webhook:
                await webhook.send(action_team_result_doc.model_dump())

            app_state.action_team_id = action_team_doc.id
            app_state.action_team_result_id = action_team_result_doc.id

            HeartBeatRunRegistry.get_instance().register(ctx=ctx , app_state=app_state, runnable_id=action_team_result_doc.id, runnable_type=AppCode.ACTION_TEAM.value)

            notify: Notify = Notify(topic=action_team_result_doc.id)

            if background_tasks:
                background_tasks.add_task(
                    ActionTeam._run_as_background_task,
                    ctx=ctx,
                    app_state=app_state,
                    user_actions=user_actions,
                    input_dict=input_dict,
                    action_team_result_doc=action_team_result_doc,
                    user_action_team_result=user_action_team_result,
                    notify=notify,
                    webhook=webhook,
                )
            elif fire_and_forget:
                asyncio.create_task(
                        ActionTeam._run_as_background_task(
                        ctx=ctx,
                        app_state=app_state,
                        user_actions=user_actions,
                        input_dict=input_dict,
                        action_team_result_doc=action_team_result_doc,
                        user_action_team_result=user_action_team_result,
                        notify=notify,
                        webhook=webhook,
                    )
                )
            else:
                action_team_result_doc: Optional[
                    ActionTeamResultDoc
                ] = await ActionTeam._run_as_background_task(
                    ctx=ctx,
                    app_state=app_state,
                    user_actions=user_actions,
                    input_dict=input_dict,
                    action_team_result_doc=action_team_result_doc,
                    user_action_team_result=user_action_team_result,
                    notify=notify,
                    webhook=webhook,
                )

            return action_team_result_doc
        except Exception as e:
            logger.bind(ctx=ctx, action_team_id=action_team_doc.id).exception(
                f"ActionTeam run failed: {e}"
            )
            raise

    @staticmethod
    async def _run_as_background_task(
        ctx: Context,
        app_state: AppState,
        user_actions: UserActions,
        input_dict: Dict[str, Any],
        action_team_result_doc: ActionTeamResultDoc,
        user_action_team_result: UserActionTeamResult,
        notify: Notify,
        webhook: Optional[Webhook] = None,
    ) -> Optional[ActionTeamResultDoc]:
        start_time: DateTime = Clock.get_date_time()
        try:
            logger.bind(
                ctx=ctx,
                action_team_id=action_team_result_doc.result.id,
                action_team_run_id=action_team_result_doc.id,
            ).info("Action Team Run Start")
            action_team_result_doc: ActionTeamResultDoc = (
                await ActionTeam._run_retry_as_background_task(
                    ctx=ctx,
                    app_state=app_state,
                    user_actions=user_actions,
                    input_dict=input_dict,
                    action_team_result_doc=action_team_result_doc,
                    user_action_team_result=user_action_team_result,
                    notify=notify,
                    webhook=webhook,
                )
            )
            return action_team_result_doc
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in running action team: {str(e)}")
            try:
                action_team_result_update: ActionTeamResultUpdate = ActionTeamResultUpdate(
                    status=EventResultStatus.error,
                    error_message=TextObj(text=f"{type(e).__name__}: {e}"),
                )
                action_team_result_doc: ActionTeamResultDoc = (
                    await user_action_team_result.update_action_team_result(
                        ctx=ctx,
                        action_team_result_id=action_team_result_doc.id,
                        action_team_update=action_team_result_update,
                    )
                )
                await RunMetricsWriter(user_actions.db).record_action_team_run(
                    ctx,
                    action_team_result_doc,
                    duration_seconds=Clock.get_date_time().diff(start_time).in_seconds(),
                )
                if webhook:
                    await webhook.send(action_team_result_doc.model_dump())
                return action_team_result_doc
            except Exception:
                logger.bind(ctx=ctx).exception("Error updating result to error status")
                return None
        finally:
            HeartBeatRunRegistry.get_instance().deregister(ctx=ctx, app_state=app_state, runnable_id=action_team_result_doc.id)

            end_time: DateTime = Clock.get_date_time()
            logger.bind(
                ctx=ctx,
                action_team_id=action_team_result_doc.result.id,
                action_team_run_id=action_team_result_doc.id,
                start_time=start_time,
                end_time=end_time,
                duration=str(end_time - start_time),
            ).info("Action Team Run End")

    @staticmethod
    @retry(stop=stop_after_attempt(3), wait=wait_fixed(1))
    async def _run_retry_as_background_task(
        ctx: Context,
        app_state: AppState,
        user_actions: UserActions,
        input_dict: Dict[str, Any],
        action_team_result_doc: ActionTeamResultDoc,
        user_action_team_result: UserActionTeamResult,
        notify: Notify,
        webhook: Optional[Webhook] = None,
    ) -> ActionTeamResultDoc:
        return await ActionTeam._execute_team_loop(
            ctx=ctx,
            app_state=app_state,
            user_actions=user_actions,
            input_dict=input_dict,
            action_team_result_doc=action_team_result_doc,
            user_action_team_result=user_action_team_result,
            notify=notify,
            webhook=webhook,
        )

    @staticmethod
    async def _execute_team_loop(
        ctx: Context,
        app_state: AppState,
        user_actions: UserActions,
        input_dict: Dict[str, Any],
        action_team_result_doc: ActionTeamResultDoc,
        user_action_team_result: UserActionTeamResult,
        notify: Notify,
        webhook: Optional[Webhook] = None,
    ) -> ActionTeamResultDoc:
        team_run_started_at = time.monotonic()
        logger.bind(ctx=ctx).info(f"Action Team Doc members : {action_team_result_doc.result.members}")
        leader: TeamMember = ActionTeam._get_leader(members=action_team_result_doc.result.members)
        members: Dict[str, TeamMember] = {
            m.member_id: m for m in action_team_result_doc.result.members if m.role == TeamMemberRole.member
        }
        logger.bind(ctx=ctx).info(f"Leader : {leader} and Members : {members}")
        output: Dict[str, Any] = {}
        # thread: List[ThreadEntry] = [
        #     ThreadEntry(name="me", role=ThreadEntryRole.user, input=input_dict, output=input_dict),
        # ]
        thread : List[ThreadEntry] = action_team_result_doc.result.thread
        
        action_team_result_doc.result.thread = thread
        parsed: LeaderDecision = LeaderDecision()
        for iteration in range(action_team_result_doc.result.max_iterations+1):
            logger.bind(ctx=ctx).info(f"Iteration {iteration} of Action Team Run")
            logger.bind(
                ctx=ctx,
                action_team_id=action_team_result_doc.result.id,
                iteration=iteration,
            ).info("Action Team iteration start")

            # 1. Build leader input
            leader_input: Dict[str, Any] = await ActionTeam._build_leader_input(
                ctx=ctx,
                original_input=input_dict,
                thread=thread,
                members=list(members.values()),
                iteration=iteration,
                max_iterations=action_team_result_doc.result.max_iterations,
                user_actions=user_actions,
                action_team_result_id=action_team_result_doc.id,
            )

            # 2. Run leader action
            app_state.action_team_leader_action_id = leader.action_id
            leader_result: ActionDoc | Exception = await ActionGraph.run_action(
                ctx=ctx,
                app_state=app_state,
                user_actions=user_actions,
                action_id=leader.action_id,
                action_graph_input_dict=leader_input,
                notify=notify,
            )
            if isinstance(leader_result, Exception):
                logger.bind(ctx=ctx).exception(f"Leader action failed: {leader_result}")
                raise leader_result
            
            leader_output: Dict[str, Any] = leader_result.output if leader_result.output else {}
            logger.bind(ctx=ctx, iteration=iteration).info(f"Leader output : {leader_output}")
            # Append leader to thread
            thread.append(
                ThreadEntry(
                    member_id=leader.member_id,
                    action_id=leader.action_id,
                    name=leader.name,
                    role=ThreadEntryRole.leader,
                    input=leader_input,
                    output=leader_output,
                )
            )
            output = leader_output
            # Persist after leader run
            action_team_result_doc: ActionTeamResultDoc = await ActionTeam._persist_intermediate(
                ctx=ctx,
                output=output,
                thread=thread,
                action_team_result_doc=action_team_result_doc,
                user_action_team_result=user_action_team_result,
                webhook=webhook,
            )

            # 3. Parse leader response
            output_text: str = ActionTeam._extract_text_from_output(output=leader_output)
            parsed: LeaderDecision = ActionTeam._parse_leader_response(output_text=output_text)
            logger.bind(ctx=ctx, iteration=iteration).info(
                f"output_text : {output_text} and parsed : {parsed.model_dump()}"
            )
            if parsed.is_complete:
                # Append completion to thread
                thread.append(
                    ThreadEntry(
                        member_id=leader.member_id,
                        action_id=leader.action_id,
                        name=leader.name,
                        role=ThreadEntryRole.leader,
                        output={"final_summary": parsed.summary},
                    )
                )
                logger.bind(ctx=ctx, iteration=iteration).info(
                    "Action Team: leader declared task complete"
                )
                break

            # 4. Execute delegated member tasks
            for delegation in parsed.delegations:
                member: Optional[TeamMember] = members.get(delegation.member_id)
                logger.bind(ctx=ctx,iteration=iteration).info(f"member : {member} and delegation : {delegation.model_dump()}")
                if not member:
                    logger.bind(ctx=ctx, member_action_id=delegation.member_action_id).warning(
                        "Leader delegated to unknown member, skipping"
                    )
                    continue

                member_input: Dict[str, Any] = await ActionTeam._build_member_input(
                    ctx=ctx,
                    delegation_task=delegation.task,
                    original_input=input_dict,
                    thread=thread,
                    user_actions=user_actions,
                    action_team_result_id=action_team_result_doc.id,
                )
                logger.bind(ctx=ctx, iteration=iteration).info(f"member_input : {member_input}")
                # member_result: ActionDoc | Exception = await ActionGraph.run_action(
                #     ctx=ctx,
                #     user_actions=user_actions,
                #     action_id=member.action_id,
                #     action_graph_input_dict=member_input,
                #     notify=notify,
                # )
                member_action_doc = await ActionTeam.get_member_action_doc(
                    ctx=ctx,
                    user_actions=user_actions,
                    action_id=member.action_id,
                    action_result_id=delegation.member_action_run_id,
                )
                member_app_state = app_state.child_context(
                    action_team_id=app_state.action_team_id,
                    action_team_result_id=app_state.action_team_result_id,
                    action_team_leader_action_id=app_state.action_team_leader_action_id,
                )
                if not delegation.member_action_run_id:
                    member_app_state.action_team_member_id = delegation.member_id
                else:
                    logger.bind(ctx=ctx).info("Current Active Members Updating")
                    action_team_result_doc.result.current_active_members = [
                        {
                            delegation.member_id : delegation.member_action_run_id
                        }
                    ]
                    action_team_result_update = ActionTeamResultUpdate(
                        result=action_team_result_doc.result
                    )
                    action_team_result_doc: ActionTeamResultDoc = (
                        await user_action_team_result.update_action_team_result(
                            ctx=ctx,
                            action_team_result_id=action_team_result_doc.id,
                            action_team_update=action_team_result_update,
                        )
                    )
                    logger.bind(ctx=ctx).info(f"Updated Current Active Team Members list to {action_team_result_doc.result.current_active_members}")
                    
                member_result : ActionResultDoc | None = await ActionFactory.run_action_in_background(
                    ctx=ctx,
                    app_state=member_app_state,
                    action_doc=member_action_doc,
                    action_input_dict=member_input,
                    user_action_result=UserActionResult(user_actions.user, user_actions.db),
                    db=user_actions.db,
                    action_result_id=delegation.member_action_run_id,
                    background_tasks= None,
                    webhook=webhook,
                    notify=notify,
                )

                logger.bind(ctx=ctx).info(f"Member Result ID : {member_result.id if member_result and not isinstance(member_result, Exception) else None}")
                member_doc: Dict[str, Any]
                member_output: Dict[str, Any]
                if isinstance(member_result, Exception):
                    logger.bind(ctx=ctx).exception(
                        "Member action failed"
                    )
                    member_doc = {"error": str(member_result)}
                    member_output = member_doc
                else:
                    member_doc = member_result.model_dump()
                    member_output = member_result.result.output if member_result.result.output else {}
                    logger.bind(ctx=ctx).info(f"member_output : {member_output}")
                # Update member's action_run_ids
                logger.bind(ctx=ctx).info("Updating member's action_run_ids")
                for member in action_team_result_doc.result.members:
                    if member.member_id == delegation.member_id:
                        if member.action_run_ids is None:
                            member.action_run_ids = []
                        if member_result and not isinstance(member_result, Exception):
                            member.action_run_ids.append(member_result.id)
                # Store latest ActionDoc per action_id
                # output = member_output
                # output[member.action_id] = member_doc

                # Append member to thread
                thread.append(
                    ThreadEntry(
                        member_id=member.member_id,
                        action_id=member.action_id,
                        action_run_id=member_result.id if member_result and not isinstance(member_result, Exception) else None,
                        name=member.name,
                        role=ThreadEntryRole.member,
                        input=member_input,
                        output=member_output,
                    )
                )
                action_team_result_doc.result.current_active_members = []
                # Persist after each member run
                action_team_result_doc: ActionTeamResultDoc = (
                    await ActionTeam._persist_intermediate(
                        ctx=ctx,
                        output=output,
                        thread=thread,
                        action_team_result_doc=action_team_result_doc,
                        user_action_team_result=user_action_team_result,
                        webhook=webhook,
                    )
                )
                
            if iteration == action_team_result_doc.result.max_iterations-1:
                logger.bind(ctx=ctx, iteration=iteration).info("Injecting the final leader text into threads!. Reached Last Iteration Already!!")
                final_leader_text = """"
                    Now, You have reached max iterations without completion, do the final wrap-up without delegating anything, and then tell the user of the status of what has been done until now regarding the task.
                """
                final_leader_text_dict = {
                    "text": final_leader_text
                }
                thread.append(
                    ThreadEntry(name="me", role=ThreadEntryRole.user, input=final_leader_text_dict, output={})
                )
                logger.bind(ctx=ctx).info("Injected the final leader text into threads!. Reached Last Iteration Already!!")
            logger.bind(ctx=ctx, iteration=iteration).info("Action Team iteration end")
        # else:
        #     # Loop exhausted without leader declaring completion
        #     logger.bind(
        #         ctx=ctx,
        #         action_team_id=action_team_result_doc.result.id,
        #         max_iterations=action_team_result_doc.result.max_iterations,
        #     ).info("Action Team: max iterations reached without leader completion")

        # Final update
        action_team_result_doc.result.output = output
        ## Adding Output to action Result results 
        if len(action_team_result_doc.result.results) == 0 or action_team_result_doc.result.results[-1].output:
            action_team_result_doc.result.results.append(
                ActionTeamResult(
                    input=input_dict,
                    output=output
                )
            )
        else:
            action_team_result_doc.result.results[-1].output = output
        action_team_result_doc.result.thread = thread
        final_status: EventResultStatus = (
            EventResultStatus.success if parsed.is_complete else EventResultStatus.error
        )
        error_msg: Optional[TextObj] = (
            TextObj(text="Max iterations reached without leader completion")
            if not parsed.is_complete
            else None
        )
        action_team_result_update: ActionTeamResultUpdate = ActionTeamResultUpdate(
            status=final_status,
            result=action_team_result_doc.result,
            error_message=error_msg,
        )
        action_team_result_doc: ActionTeamResultDoc = (
            await user_action_team_result.update_action_team_result(
                ctx=ctx,
                action_team_result_id=action_team_result_doc.id,
                action_team_update=action_team_result_update,
            )
        )
        await RunMetricsWriter(user_actions.db).record_action_team_run(
            ctx, action_team_result_doc, duration_seconds=time.monotonic() - team_run_started_at
        )
        if webhook:
            await webhook.send(action_team_result_doc.model_dump())

        return action_team_result_doc

    @staticmethod
    async def get_member_action_doc(
        ctx: Context,
        user_actions:  UserActions,
        action_id: str,
        action_result_id : str | None = None,
    ) -> ActionDoc | None:
        action_doc = None
        is_space_admin: bool = is_space_member_a_admin(user_actions.space_member)
        if is_space_admin:
            action_doc = await SpaceActions(space_member=user_actions.space_member, db=user_actions.db).get_action(ctx, action_id)
        if action_doc is None:
            user_actions = UserActions(user_actions.user, user_actions.db)
            action_doc = await user_actions.get_owned_or_shared_or_system_actions(
                ctx=ctx,
                action_id=action_id,
                empty_shared_action_config=False,
            )
        if action_doc:
            return action_doc
        if action_result_id:
            user_action_result = UserActionResult(user_actions.user, user_actions.db)
            existing_result_doc: ActionResultDoc | None = await user_action_result.get_action_result(
                action_result_id=action_result_id,
            )
            if existing_result_doc is not None:
                if action_doc is None:
                    action_doc = existing_result_doc.result
        return action_doc
        

        
        

    @staticmethod
    async def _persist_intermediate(
        ctx: Context,
        output: Dict[str, Any],
        thread: List[ThreadEntry],
        action_team_result_doc: ActionTeamResultDoc,
        user_action_team_result: UserActionTeamResult,
        webhook: Optional[Webhook] = None,
    ) -> ActionTeamResultDoc:
        action_team_result_doc.result.output = output
        action_team_result_doc.result.thread = thread
        action_team_result_update: ActionTeamResultUpdate = ActionTeamResultUpdate(
            status=EventResultStatus.processing,
            result=action_team_result_doc.result,
        )
        action_team_result_doc: ActionTeamResultDoc = (
            await user_action_team_result.update_action_team_result(
                ctx=ctx,
                action_team_result_id=action_team_result_doc.id,
                action_team_update=action_team_result_update,
            )
        )
        if webhook:
            await webhook.send(action_team_result_doc.model_dump())
        return action_team_result_doc

    @staticmethod
    def _get_leader(members: List[TeamMember]) -> TeamMember:
        for m in members:
            if m.role == TeamMemberRole.leader:
                return m
        raise ValueError("No leader found in team members")




    @staticmethod
    async def _build_thread_str(
        ctx: Context,
        thread: List[ThreadEntry],
        user_actions: UserActions,
        action_team_result_id: str,
        max_chars: int = 75000,
    ) -> str:
        thread_entries: List[Dict[str, Any]] = []
        for entry in thread:
            entry_summary: Dict[str, Any] = {
                "role": entry.role.value,
                "member_id": entry.member_id,
                "action_id": entry.action_id,
            }
            if entry.input:
                entry_summary["input"] = ActionTeam._extract_text_from_output(output=entry.input)
            if entry.output:
                entry_summary["output"] = ActionTeam._extract_text_from_output(output=entry.output)
            thread_entries.append(entry_summary)
        thread_str: str = json.dumps(thread_entries, indent=2, default=str)
        if len(thread_str) > max_chars:
            s3 = get_public_s3()
            s3.set_object_prefix(f"action_teams/{user_actions.user.id}/{action_team_result_id}/")
            url = await s3.upload_fileobj(
                ctx,
                BytesIO(thread_str.encode("utf-8")),
                "thread.json",
            )
            return (
                f"Full thread available at: {url}]"
            )
        return thread_str

    @staticmethod
    async def _build_leader_input(
        ctx: Context,
        original_input: Dict[str, Any],
        thread: List[ThreadEntry],
        members: List[TeamMember],
        iteration: int,
        max_iterations: int,
        user_actions: UserActions,
        action_team_result_id: str
    ) -> Dict[str, Any]:
        input_block: DataBlock = DataBlock.model_validate(original_input)

        team_members_str: str = ""
        for m in members:
            team_members_str += (
                f"- Name: {m.name or m.member_id} | "
                f"ID: {m.member_id} | "
                f"Action ID: {m.action_id} | "
                f"Specialization: {m.description or 'General'}\n"
                "RUN IDS : No RUN IDS, Not a single time this member has been invoked." if not m.action_run_ids else f"RUN IDS : {",".join(m.action_run_ids)}"
            )

        thread_str: str = await ActionTeam._build_thread_str(
            ctx=ctx, thread=thread, user_actions=user_actions, action_team_result_id=action_team_result_id
        )
        logger.bind(ctx=ctx, iteration=iteration).info(f"Thread so far : {thread_str}")
        team_context: str = (
            f"\n"
            f"----"
            f"\n"
            f"You are the team leader coordinating members to complete a task.\n\n"
            f"## Team Members\n{team_members_str}\n"
            f"## Thread So Far\n{thread_str} (Always read the thread)\n\n"
            f"## Iteration {iteration + 1} of {max_iterations}\n\n"
            f"## Response Format\n"
            f"Analyze the task and thread. Then respond with ONE of:\n\n"
            f"1. To delegate work to a member with fresh conversation, include this JSON block in your response:\n"
            f"```json\n"
            f'{{"action": "delegate", "delegations": ['
            f'{{"member_id": "<id>", "member_action_id": "<action_id>", "task": "<specific instructions>"}}'
            f"]}}\n```\n\n"
            f"2. To delegate work with continued conversation to a member on a specific run\n"
            f"```json\n"
            f'{{"action": "delegate", "delegations": ['
            f'{{"member_id": "<id>", "member_action_id": "<action_id>", "member_action_run_id": "<run_id>", "task": "<continued instructions>"}}'
            f"]}}\n```\n\n"
            f"3. To declare task complete, include this JSON block in your response:\n"
            f"```json\n"
            f'{{"action": "complete", "summary": "<final synthesized result>"}}\n'
            f"```\n"
            
            f"Always include one of these JSON blocks in your response."
        )

        input_block.text = f"Original Input : {input_block.text}\n{team_context}"
        return input_block.model_dump()

    @staticmethod
    async def _build_member_input(
        ctx: Context,
        delegation_task: str,
        original_input: Dict[str, Any],
        thread: List[ThreadEntry],
        user_actions: UserActions,
        action_team_result_id: str
    ) -> Dict[str, Any]:
        thread_str: str = await ActionTeam._build_thread_str(
            ctx=ctx, thread=thread, user_actions=user_actions, action_team_result_id=action_team_result_id
        )
        original_text: str = original_input.get("text", "")
        text: str = f"Task assigned by team leader:\n{delegation_task}"
        if original_text:
            text += f"\n\nOriginal team input:\n{original_text}"
        text += f"\n\n## Thread So Far\n{thread_str}"
        input_block: DataBlock = DataBlock(text=text)
        # Carry over URLs and files from original input
        if original_input.get("urls"):
            input_block.urls = original_input["urls"]
        return input_block.model_dump()

    @staticmethod
    def _extract_text_from_output(output: Dict[str, Any]) -> str:
        if not output:
            return ""
        # Try to get text from DataBlocks-style output
        blocks: list = output.get("blocks", [])
        if blocks:
            texts: List[str] = []
            for block in blocks:
                if isinstance(block, dict) and block.get("text"):
                    texts.append(block["text"])
            if texts:
                return "\n".join(texts)
        # Try direct text field
        if output.get("text"):
            return output["text"]
        # Fallback to JSON string
        return json.dumps(output, default=str)

    @staticmethod
    def _parse_leader_response(output_text: str) -> LeaderDecision:
        if not output_text:
            return LeaderDecision()

        # Try to find JSON blocks in the text
        json_blocks: List[str] = ActionTeam._extract_json_blocks(text=output_text)

        for block in json_blocks:
            try:
                data: dict = json.loads(block)
                if isinstance(data, dict):
                    action: str = data.get("action", "")
                    if action == "complete":
                        return LeaderDecision(
                            is_complete=True,
                            summary=data.get("summary", output_text),
                        )
                    if action == "delegate":
                        delegations: List[Delegation] = []
                        for d in data.get("delegations", []):
                            if d.get("member_id") and d.get("member_action_id") and d.get("task"):
                                cleaned_d = {
                                    key: value
                                    for key, value in d.items()
                                    if value is not None
                                }
                                delegations.append(Delegation(**cleaned_d))
                        if delegations:
                            return LeaderDecision(delegations=delegations)
            except (json.JSONDecodeError, TypeError, KeyError):
                continue

        # No valid commands found — return empty decision (will loop again)
        return LeaderDecision()

    @staticmethod
    def _extract_json_blocks(text: str) -> List[str]:
        blocks: List[str] = []
        # Match JSON in code fences
        fence_pattern: re.Pattern = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)
        for match in fence_pattern.finditer(text):
            blocks.append(match.group(1).strip())
        # Match standalone JSON objects
        brace_pattern: re.Pattern = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", re.DOTALL)
        for match in brace_pattern.finditer(text):
            candidate: str = match.group(0).strip()
            if candidate not in blocks:
                blocks.append(candidate)
        return blocks
