from typing import Literal, Type

from loguru import logger
from pydantic import BaseModel

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.abc.ActionABC import ActionABC
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.skills.skill_doc_model import (
    SkillCreate,
    SkillDoc,
    SkillLiteDocPage,
    SkillRead,
    SkillUpdate,
)
from src.autobots.skills.user_skills import UserSkills
from src.autobots.data_model.app_state import AppState

class ActionSkillsConfig(BaseModel):
    """
Manage persisted "Skills" (DataBlocks) stored in MongoDB.

operation:
- create: create a new skill
- read: get a skill by id or name
- update: update an existing skill by id
- delete: delete a skill by id
- list: list skills (optional filters + pagination)
    """

    operation: Literal["create", "read", "update", "delete", "list"] = "list"
    id: str | None = None
    name: str | None = None
    blocks: DataBlocks | None = None
    limit: int = 100
    offset: int = 0


class ActionSkillsOutput(BaseModel):
    doc: SkillDoc | None = None
    page: SkillLiteDocPage | None = None
    deleted: bool | None = None


class ActionSkills(
    ActionABC[ActionSkillsConfig, ActionSkillsConfig, ActionSkillsConfig, DataBlock, ActionSkillsOutput]
):
    type = ActionType.action_skills

    @staticmethod
    def get_config_create_type() -> Type[ActionSkillsConfig]:
        return ActionSkillsConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionSkillsConfig]:
        return ActionSkillsConfig

    @staticmethod
    def get_config_type() -> Type[ActionSkillsConfig]:
        return ActionSkillsConfig

    @staticmethod
    def get_input_type() -> Type[DataBlock]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionSkillsOutput]:
        return ActionSkillsOutput

    @staticmethod
    def get_description() -> str:
        return """
CRUD skills stored in MongoDB (collection: "Skills"). Skills store reusable DataBlocks.

Operations:
- create: {name, blocks}
- read: {id} or {name}
- update: {id, name?, blocks?}
- delete: {id}
- list: {id?, name?, limit?, offset?}
"""

    async def _execute(self, ctx: Context, config: ActionSkillsConfig) -> ActionSkillsOutput:
        op = config.operation

        if op == "create":
            if not config.name or config.blocks is None:
                raise ValueError("create requires name and blocks")
            doc = await UserSkills.create(
                ctx=ctx,
                db=self.db,
                user=self.user,
                skill_create=SkillCreate(name=config.name, blocks=config.blocks),
            )
            return ActionSkillsOutput(doc=doc)

        if op == "read":
            if config.id:
                doc = await UserSkills.get(ctx=ctx, db=self.db, user=self.user, skill_id=config.id)
            elif config.name:
                doc = await UserSkills.get_by_name(ctx=ctx, db=self.db, user=self.user, name=config.name)
            else:
                raise ValueError("read requires id or name")

            if not doc:
                raise ValueError("Skill not found")
            return ActionSkillsOutput(doc=doc)

        if op == "update":
            if not config.id:
                raise ValueError("update requires id")
            update = SkillUpdate(name=config.name, blocks=config.blocks)
            doc = await UserSkills.update(ctx=ctx, db=self.db, user=self.user, id=config.id, skill_update=update)
            if not doc:
                raise ValueError("Skill not found")
            return ActionSkillsOutput(doc=doc)

        if op == "delete":
            if not config.id:
                raise ValueError("delete requires id")
            deleted = await UserSkills.delete(
                ctx=ctx, db=self.db, user=self.user, skill_delete=SkillRead(id=config.id)
            )
            if deleted != 1:
                raise RuntimeError("Error deleting skill")
            return ActionSkillsOutput(deleted=True)

        # list
        page = await UserSkills.list(
            ctx=ctx,
            db=self.db,
            user=self.user,
            skill_read=SkillRead(id=config.id, name=config.name),
            limit=config.limit,
            offset=config.offset,
        )
        return ActionSkillsOutput(page=page)

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> ActionSkillsOutput | Exception:
        try:
            notify(ctx, "Running skills operation") if notify else None
            logger.bind(ctx=ctx, config=self.action_config).info("ActionSkills.run_action")
            return await self._execute(ctx=ctx, config=self.action_config)
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in ActionSkills.run_action: {str(e)}")
            return e

    async def run_tool(
        self, action_config: ActionSkillsConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> ActionSkillsOutput | Exception:
        try:
            notify(ctx, "Running skills tool") if notify else None
            return await self._execute(ctx=ctx, config=action_config)
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in ActionSkills.run_tool: {str(e)}")
            return e
