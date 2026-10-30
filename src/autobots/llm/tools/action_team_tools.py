from typing import Any, Dict, List
import uuid

from fastapi import HTTPException

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action_team.action_team.space_action_teams import SpaceActionTeams
from src.autobots.action_team.action_team.user_action_team import UserActionTeams
from src.autobots.action_team.action_team_result.action_team_result_model_doc import ActionTeamResultDoc
from src.autobots.action_team.action_team_result.space_action_team_result import SpaceActionTeamResults
from src.autobots.action_team.action_team_result.user_action_team_result import UserActionTeamResult
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel
from src.autobots.action_team.action_team.action_team_doc_model import ActionTeamCreate, ActionTeamDoc, ActionTeamDocsFound, ActionTeamFind, ActionTeamUpdate
from loguru import logger 
from src.autobots.space.space_auth import is_space_member_a_admin
from src.autobots.data_model.app_state import AppState


class ActionTeamListConfig(BaseModel):
    action_team_id: str | None = None
    action_team_name: str | None = None
    is_saved: bool | None = None
    is_published: bool | None = None
    is_shared: bool | None = None
    limit: int = 100
    offset: int = 0
    
class ActionTeamID(BaseModel):
    action_team_id: str

class ActionTeamRunID(BaseModel):
    action_team_run_id: str
class ActionTeamRunConfig(ActionTeamID):
    input: DataBlock
    action_team_run_id: str | None = None
class ActionTeamUpdateConfig(BaseModel):
    action_team_id : str
    action_team_update : ActionTeamUpdate

class ActionTeamTools:
    
    @staticmethod
    async def create_action_team(
        config: ActionTeamCreate, 
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
        space_member: SpaceMemberDoc | None = None) -> ActionTeamDoc:
        """
        Description
        -----------
        Creates a new action team with the specified members. Exactly one member
        must have role 'leader'; the rest are 'member'. Each member references an
        action by its action_id. Returns the persisted ActionTeamDoc on success.

        Input
        -----
        config : ActionTeamCreate
            Expected structure:
                {
                    "name": "<team name>",
                    "description": "<optional description>",
                    "members": [
                        {"action_id": "<action_id>", "role": "leader", "name": "<name>", "description": "<specialization>"},
                        {"action_id": "<action_id>", "role": "member", "name": "<name>", "description": "<specialization>"}
                    ],
                    "max_iterations": 50,
                    "is_saved": false,
                    "is_published": false
                }

            name : str
                Human-readable name for the team.
            members : list[TeamMember]
                List of team members. Exactly one must have role='leader'.
                Each member requires action_id (the action they execute).
            max_iterations : int (default 50)
                Maximum leader-member iteration cycles before forced completion.
        """
        try:
            for member in config.members:
                member.member_id = str(gen_uuid())
            logger.bind(ctx=ctx).info(f"Creating action team with members: {config.members}")
            resp = await UserActionTeams(user=user, db=db).create(
                ctx,
                config,
            )
            return resp
        except Exception as e:
            logger.error(str(e))
            raise HTTPException(500, detail=str(e))
        
    @staticmethod 
    async def update_action_team(
        config: ActionTeamUpdateConfig, 
        ctx: Context, 
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
        space_member: SpaceMemberDoc | None = None) -> ActionTeamDoc:
        """
        Description
        -----------
        Updates an existing action team by its ID. Can modify name, description,
        members, max_iterations, and sharing settings. New members without a
        member_id get one auto-generated; existing member_ids are validated as
        UUIDs. Space admins update via SpaceActionTeams; regular users update
        their own teams.

        Input
        -----
        config : ActionTeamUpdateConfig
            Expected structure:
                {
                    "action_team_id": "<team id>",
                    "action_team_update": {
                        "name": "<new name>",
                        "description": "<new description>",
                        "members": [{"action_id": "<id>", "role": "leader", ...}],
                        "max_iterations": 30,
                        "is_saved": true,
                        "is_published": true,
                        "shared_with": ["<email or space_id>"]
                    }
                }

            action_team_id : str
                The unique identifier of the team to update.
            action_team_update : ActionTeamUpdate
                Partial update payload. All fields are optional. If members is
                provided, it replaces the full member list. New members without
                member_id get one auto-assigned; existing member_ids are preserved.
        """
        is_space_admin: bool = is_space_member_a_admin(space_member)
        for members in config.action_team_update.members or []:
            if not members.member_id:
                members.member_id = str(gen_uuid())
            else:
                # ensure that the member id is unique and valid UUID
                try:
                    uuid_obj = uuid.UUID(members.member_id)
                    members.member_id = str(uuid_obj)
                except ValueError:
                    raise HTTPException(400, detail=f"Invalid member_id: {members.member_id}")
                
        if is_space_admin:
            action_team_doc = await SpaceActionTeams(
                space_member=space_member, db=db
            ).update_action_team(ctx, config.action_team_id, config.action_team_update)
        else:
            action_team_doc = await UserActionTeams(user=user, db=db).update(
                ctx, config.action_team_id, config.action_team_update
            )
        return action_team_doc

    @staticmethod
    async def list_action_teams(
        config: ActionTeamListConfig,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
        space_member: SpaceMemberDoc | None = None) -> ActionTeamDocsFound:
        """
        Description
        -----------
        Lists action teams accessible to the user. Can filter by id, name,
        saved/published status. Set is_shared=true to include teams shared
        with the user. Space admins see all space teams; regular users see
        owned and shared teams.

        Input
        -----
        config : ActionTeamListConfig
            Expected structure:
                {
                    "action_team_id": "<optional id filter>",
                    "action_team_name": "<optional name filter>",
                    "limit": 100,
                    "offset": 0
                }
            limit : int (default 100)
                Max results per page.
            offset : int (default 0)
                Page offset for pagination.
        """
        is_space_admin: bool = is_space_member_a_admin(space_member)
        find = ActionTeamFind(id=config.action_team_id, name=config.action_team_name, is_saved=config.is_saved, is_published=config.is_published)
        if is_space_admin:
            action_team_docs = await SpaceActionTeams(
                space_member=space_member, db=db
            ).list_action_teams(ctx, find, config.limit, config.offset)
        else:
            if config.is_shared:
                find.shared_with = user.email
            action_team_docs = await UserActionTeams(user=user, db=db).list_owned_or_shared(
                ctx,
                find,
                config.limit,
                config.offset,
            )
        return action_team_docs

    @staticmethod
    async def get_action_team(
        config: ActionTeamID,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
        space_member: SpaceMemberDoc | None = None) -> ActionTeamDoc:
        """
        Description
        -----------
        Retrieves a single action team by its ID. Space admins can fetch any
        space team; regular users can only fetch teams they own or that are
        shared with them. Returns 404 if not found.

        Input
        -----
        config : ActionTeamID
            Expected structure:
                {"action_team_id": "<team id>"}

            action_team_id : str
                The unique identifier of the action team to retrieve.
        """
        is_space_admin: bool = is_space_member_a_admin(space_member)
        if is_space_admin:
            action_team_doc = await SpaceActionTeams(
                space_member=space_member, db=db
            ).get_action_team(ctx, config.action_team_id)
        else:
            action_team_doc = await UserActionTeams(user=user, db=db).get_owned_or_shared(ctx, config.action_team_id)
        if not action_team_doc:
            raise HTTPException(404, detail="Action team not found")
        return action_team_doc
    
    @staticmethod
    async def delete_action_team(
        config: ActionTeamID,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
        space_member: SpaceMemberDoc | None = None) -> ActionTeamDoc:
        """
        Description
        -----------
        Deletes an action team by its ID. Space admins can delete any space
        team. Regular users can only delete teams they own. Returns the
        deleted team doc on success, 404 if not found.

        Input
        -----
        config : ActionTeamID
            Expected structure:
                {"action_team_id": "<team id>"}

            action_team_id : str
                The unique identifier of the action team to delete.
        """
        is_space_admin: bool = is_space_member_a_admin(space_member)
        if is_space_admin:
            action_team_doc = await SpaceActionTeams(
                space_member=space_member, db=db
            ).delete_action_team(ctx, config.action_team_id)
            return action_team_doc
        user_action_teams = UserActionTeams(user=user, db=db)
        action_team_doc = await user_action_teams.get_owned(ctx, config.action_team_id)
        if action_team_doc is None:
            raise HTTPException(404, "Action team not found")
        deleted_count = await user_action_teams.delete(ctx, config.action_team_id)
        if deleted_count != 1:
            raise HTTPException(500, "Error in deleting action team")
        return action_team_doc
        
        
    @staticmethod
    async def run_action_team(
        config: ActionTeamRunConfig,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
        space_member: SpaceMemberDoc | None = None
    ) -> ActionTeamResultDoc | None:
        """
        Description
        -----------
        Executes an action team run. The leader action coordinates member
        actions across multiple iterations until the task is complete or
        max_iterations is reached. Runs fire-and-forget in the background.
        Provide action_team_run_id to continue an existing run and continue the corresponding conversation of the run.

        Input
        -----
        config : ActionTeamRunConfig
            Expected structure:
                {
                    "action_team_id": "<team id>",
                    "input": {"text": "<task description>", "urls": [...], "files": [...]},
                    "action_team_run_id": "<optional existing run id to continue>"
                }

            action_team_id : str
                The action team to execute.
            input : DataBlock
                The task input (text, urls, files) for the team.
            action_team_run_id : str | None
                If provided, continues an existing run instead of starting new.
        """
        is_space_admin: bool = is_space_member_a_admin(space_member)
        action_team_doc = None
        if is_space_admin:
            action_team_doc = await SpaceActionTeams(
                space_member=space_member, db=db
            ).get_action_team(ctx, config.action_team_id)
        user_actions = UserActions(user, db)
        user_action_team_result = UserActionTeamResult(user, db)
        # Use child context so the launched team gets its own scope with parent IDs preserved
        child_app_state = app_state.child_context(
            action_graph_id=app_state.action_graph_id,
            action_graph_result_id=app_state.action_graph_result_id,
            action_graph_node_id=app_state.action_graph_node_id,
            action_team_id=app_state.action_team_id,
            action_team_result_id=app_state.action_team_result_id,
        )
        return await UserActionTeams(user=user, db=db).run_in_background(
            ctx=ctx,
            app_state=child_app_state,
            user_actions=user_actions,
            user_action_team_result=user_action_team_result,
            action_team_id=config.action_team_id,
            input=config.input,
            action_team_result_id=config.action_team_run_id,
            background_tasks=None,
            action_team_doc=action_team_doc,
            fire_and_forget=True,
        )
        
    @staticmethod
    async def get_action_team_run_status(
        config: ActionTeamRunID,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
        space_member: SpaceMemberDoc | None = None
    ) -> str | None:
        """
        Description
        -----------
        Checks the status of a previously started action team run. Returns a
        status string (e.g. "processing", "success", "error - <message>") or
        the full ActionTeamResultDoc. Returns 404 if the run is not found.

        Input
        -----
        config : ActionTeamRunID
            Expected structure:
                {"action_team_run_id": "<run id>"}

            action_team_run_id : str
                The unique identifier of the action team run to check.
        """
        is_space_admin: bool = is_space_member_a_admin(space_member)
        if is_space_admin:
            return await SpaceActionTeamResults(
                space_member=space_member, db=db
            ).get_action_team_result(ctx, config.action_team_run_id)
        result_doc = await UserActionTeamResult(user, db).get_action_team_result(
            ctx,
            config.action_team_run_id,
        )
        
        if result_doc is None:
            raise HTTPException(404, "Action team run not found")
        if isinstance(result_doc, ActionTeamResultDoc):
            status = result_doc.status
            if result_doc.error_message:
                status = f"{status} - {result_doc.error_message}"
            return status
        else:
            return result_doc
        
    @staticmethod 
    async def get_action_team_run_result(
        config: ActionTeamRunID,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
        space_member: SpaceMemberDoc | None = None
    ) -> Dict[str, Any]:
        """
        Description
        -----------
        Retrieves the full result output of a completed action team run.
        Returns the output dict from the run's result. Returns 404 if the
        run is not found.

        Input
        -----
        config : ActionTeamRunID
            Expected structure:
                {"action_team_run_id": "<run id>"}

            action_team_run_id : str
                The unique identifier of the action team run.
        """
        is_space_admin: bool = is_space_member_a_admin(space_member)
        if is_space_admin:
            return await SpaceActionTeamResults(
                space_member=space_member, db=db
            ).get_action_team_result(ctx, config.action_team_run_id)
        result_doc = await UserActionTeamResult(user, db).get_action_team_result(
            ctx,
            config.action_team_run_id,
        )
        if result_doc is None:
            raise HTTPException(404, "Action team run not found")
        if result_doc.result.output:
            return result_doc.result.output
        return {}
