from typing import Dict

from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.admin_actions import AdminActions
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action_graph.action_graph.action_graph_doc_model import (
    ActionGraphCreate,
    ActionGraphDoc,
)
from src.autobots.action_graph.action_graph.admin_action_graphs import AdminActionGraphs
from src.autobots.action_graph.action_graph.user_action_graph import UserActionGraphs
from src.autobots.action_team.action_team.action_team_doc_model import (
    ActionTeamCreate,
    ActionTeamDoc,
    TeamMember,
)
from src.autobots.action_team.action_team.admin_action_teams import AdminActionTeams
from src.autobots.action_team.action_team.user_action_team import UserActionTeams
from src.autobots.data_model.context import Context
from src.autobots.share_data.share_data_crud import ShareDataAdminCRUD, ShareDataCRUD
from src.autobots.share_data.share_data_doc_model import (
    RunnableType,
    ShareDataCreate,
    ShareDataDoc,
    ShareDataDocCreate,
    ShareDataDocRead,
    ShareDataDocsPage,
    ShareDataDocUpdate,
    ShareDataRead,
    ShareDataUpdate,
)
from src.autobots.user.user_orm_model import UserORM


class UserShareData:
    @staticmethod
    async def create(
        ctx: Context, user: UserORM, db: AsyncIOMotorDatabase, share_data_create: ShareDataCreate
    ) -> ShareDataDoc:
        share_data_read = ShareDataRead(data_id=share_data_create.data_id)
        page = await UserShareData.read(ctx=ctx, user=user, db=db, share_data_read=share_data_read)
        if page.total_count == 0:
            share_data_crud = ShareDataCRUD(db=db)
            share_data_doc_create = ShareDataDocCreate(
                user_id=str(user.id), **share_data_create.model_dump(exclude_none=True)
            )
            doc = await share_data_crud.insert_one(share_data_doc_create)
            return ShareDataDoc.model_validate(doc)
        else:
            raise HTTPException(status_code=400, detail="Share data already exists")

    @staticmethod
    async def read(
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        share_data_read: ShareDataRead,
        limit: int = 100,
        offset: int = 0,
    ) -> ShareDataDocsPage:
        share_data_crud = ShareDataCRUD(db=db)
        doc_find = ShareDataDocRead(
            user_id=str(user.id), **share_data_read.model_dump(exclude_none=True)
        )
        doc_page = await share_data_crud.find_page(doc_find=doc_find, limit=limit, offset=offset)
        share_data_page = ShareDataDocsPage.model_validate(doc_page.model_dump(exclude_none=True))
        return share_data_page

    @staticmethod
    async def get(
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        id: str,
    ) -> ShareDataDoc | None:
        share_data_crud = ShareDataCRUD(db=db)
        doc_find = ShareDataDocRead(user_id=str(user.id), id=id)
        doc_page = await share_data_crud.find_page(doc_find=doc_find, limit=5, offset=0)
        share_data_page = ShareDataDocsPage.model_validate(doc_page.model_dump(exclude_none=True))
        return share_data_page.docs[0] if len(share_data_page.docs) > 0 else None

    @staticmethod
    async def update(
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        id: str,
        share_data_update: ShareDataUpdate,
    ) -> ShareDataDoc:
        share_data_crud = ShareDataCRUD(db=db)
        share_data_doc_update = ShareDataDocUpdate(
            id=id, user_id=str(user.id), **share_data_update.model_dump(exclude_none=True)
        )
        doc = await share_data_crud.update_one(share_data_doc_update)
        return ShareDataDoc.model_validate(doc)

    @staticmethod
    async def delete(
        ctx: Context, user: UserORM, db: AsyncIOMotorDatabase, share_data_id: str
    ) -> int:
        share_data_crud = ShareDataCRUD(db=db)
        find_doc = ShareDataDocRead(user_id=str(user.id), id=share_data_id)
        deleted = await share_data_crud.delete_many(doc_find=find_doc)
        return deleted.deleted_count

    @staticmethod
    async def clone(
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        share_data_id: str,
    ) -> ActionDoc | ActionGraphDoc | ActionTeamDoc:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        share_data_admin_crud = ShareDataAdminCRUD(db=db)
        find_doc = ShareDataDocRead(id=share_data_id)
        share_data_docs = await share_data_admin_crud.find(doc_find=find_doc)
        if len(share_data_docs) == 0:
            raise HTTPException(status_code=404, detail="Share data not found")
        share_data_doc = share_data_docs[0]
        match share_data_doc.runnable_type:
            case RunnableType.action:
                return await UserShareData.clone_action(
                    ctx=ctx, user=user, db=db, share_data_doc=share_data_doc
                )
            case RunnableType.action_graph:
                return await UserShareData.clone_action_graph(
                    ctx=ctx, user=user, db=db, share_data_doc=share_data_doc
                )
            case RunnableType.action_team:
                return await UserShareData.clone_action_team(
                    ctx=ctx, user=user, db=db, share_data_doc=share_data_doc
                )
            case _:
                raise HTTPException(status_code=400, detail="Invalid runnable type")

    @staticmethod
    async def clone_action(
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        share_data_doc: ShareDataDoc,
    ) -> ActionDoc:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        action_doc = await AdminActions.get_action(
            ctx=ctx, action_id=share_data_doc.data_id, user=user, db=db
        )
        match action_doc:
            case ActionDoc():
                user_actions = UserActions(user=user, db=db)
                # Clone from stored ActionDoc: copy stored config as-is, bypass ActionFactory config re-processing
                new_action_doc = await user_actions.clone_action_from_doc(
                    ctx=ctx,
                    source_action=action_doc,
                    name_override=f"{action_doc.name} (Copy)",
                )
                return new_action_doc
            case _:
                raise HTTPException(status_code=400, detail="Invalid action doc")

    @staticmethod
    async def clone_action_graph(
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        share_data_doc: ShareDataDoc,
    ) -> ActionGraphDoc:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        action_graph_doc = await AdminActionGraphs.get_action_graph(
            ctx=ctx,
            action_graph_id=share_data_doc.data_id,
            user=user,
            db=db,
        )
        if not action_graph_doc:
            raise HTTPException(status_code=404, detail="Action graph not found")

        user_actions = UserActions(user=user, db=db)
        old_action_id_to_new_action_id: Dict[str, str] = {}

        action_ids_to_clone = set()
        if action_graph_doc.nodes:
            for node_id, action_id in action_graph_doc.nodes.items():
                if action_id:
                    action_ids_to_clone.add(action_id)

        for old_action_id in action_ids_to_clone:
            try:
                action_doc = await AdminActions.get_action(
                    ctx=ctx, action_id=old_action_id, user=user, db=db
                )
                if action_doc:
                    # Clone from stored ActionDoc: copy stored config as-is, bypass ActionFactory config re-processing
                    new_action_doc = await user_actions.clone_action_from_doc(
                        ctx=ctx,
                        source_action=action_doc,
                        name_override=f"{action_doc.name} (Copy)",
                    )
                    if new_action_doc:
                        old_action_id_to_new_action_id[old_action_id] = new_action_doc.id
                        logger.bind(
                            ctx=ctx,
                            old_action_id=old_action_id,
                            new_action_id=new_action_doc.id,
                            action_name=action_doc.name,
                        ).info("Cloned action for workflow share clone")
                else:
                    logger.bind(ctx=ctx, action_id=old_action_id).warning(
                        "Action not found for cloning, keeping original reference"
                    )
            except Exception as e:
                logger.bind(ctx=ctx, action_id=old_action_id).exception(
                    f"Error cloning action: {str(e)}, keeping original reference"
                )

        new_graph_data = action_graph_doc.model_dump(
            exclude={"id", "user_id", "created_at", "updated_at", "is_published", "shared_with"}
        )
        new_graph_data["name"] = f"{new_graph_data['name']} (Copy)"
        new_graph_data["is_published"] = False
        new_graph_data["shared_with"] = []

        if new_graph_data.get("nodes"):
            updated_nodes = {}
            for node_id, old_action_id in new_graph_data["nodes"].items():
                if old_action_id and old_action_id in old_action_id_to_new_action_id:
                    updated_nodes[node_id] = old_action_id_to_new_action_id[old_action_id]
                else:
                    updated_nodes[node_id] = old_action_id
            new_graph_data["nodes"] = updated_nodes

        if new_graph_data.get("node_details"):
            for node_id, node_data in new_graph_data["node_details"].items():
                if node_data and isinstance(node_data, dict):
                    data = node_data.get("data", {})
                    if data:
                        old_action_id = data.get("actionId")
                        if old_action_id and old_action_id in old_action_id_to_new_action_id:
                            data["actionId"] = old_action_id_to_new_action_id[old_action_id]
                            if data.get("label"):
                                data["label"] = f"{data['label']} (Copy)"

        action_graph_create = ActionGraphCreate(**new_graph_data)
        user_action_graphs = UserActionGraphs(user=user, db=db)
        return await user_action_graphs.create(action_graph_create)

    @staticmethod
    async def clone_action_team(
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        share_data_doc: ShareDataDoc,
    ) -> ActionTeamDoc:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        action_team_doc = await AdminActionTeams.get_action_team(
            ctx=ctx,
            action_team_id=share_data_doc.data_id,
            user=user,
            db=db,
        )
        if not action_team_doc:
            raise HTTPException(status_code=404, detail="Action team not found")

        user_actions = UserActions(user=user, db=db)
        old_action_id_to_new_action_id: Dict[str, str] = {}

        # Collect unique action IDs from team members
        action_ids_to_clone = set()
        if action_team_doc.members:
            for member in action_team_doc.members:
                if member.action_id:
                    action_ids_to_clone.add(member.action_id)

        # Clone each referenced action
        for old_action_id in action_ids_to_clone:
            try:
                action_doc = await AdminActions.get_action(
                    ctx=ctx, action_id=old_action_id, user=user, db=db
                )
                if action_doc:
                    new_action_doc = await user_actions.clone_action_from_doc(
                        ctx=ctx,
                        source_action=action_doc,
                        name_override=f"{action_doc.name} (Copy)",
                    )
                    if new_action_doc:
                        old_action_id_to_new_action_id[old_action_id] = new_action_doc.id
                        logger.bind(
                            ctx=ctx,
                            old_action_id=old_action_id,
                            new_action_id=new_action_doc.id,
                            action_name=action_doc.name,
                        ).info("Cloned action for team share clone")
                else:
                    logger.bind(ctx=ctx, action_id=old_action_id).warning(
                        "Action not found for cloning, keeping original reference"
                    )
            except Exception as e:
                logger.bind(ctx=ctx, action_id=old_action_id).exception(
                    f"Error cloning action: {str(e)}, keeping original reference"
                )

        # Build new members list with remapped action IDs
        new_members = []
        for member in action_team_doc.members:
            new_action_id = old_action_id_to_new_action_id.get(member.action_id, member.action_id)
            new_members.append(
                TeamMember(
                    action_id=new_action_id,
                    role=member.role,
                    name=f"{member.name} (Copy)"
                    if member.action_id in old_action_id_to_new_action_id
                    else member.name,
                    description=member.description,
                )
            )

        action_team_create = ActionTeamCreate(
            name=f"{action_team_doc.name} (Copy)",
            version=action_team_doc.version,
            description=action_team_doc.description,
            user_manual=action_team_doc.user_manual,
            members=new_members,
            max_iterations=action_team_doc.max_iterations,
            is_saved=action_team_doc.is_saved,
            is_published=False,
            shared_with=[],
        )
        user_action_teams = UserActionTeams(user=user, db=db)
        return await user_action_teams.create(ctx, action_team_create)
