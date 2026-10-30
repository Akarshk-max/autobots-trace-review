# NOTE: This module provides product/subscription based authorization dependencies for FastAPI.
from __future__ import annotations

from enum import Enum
from typing import Literal

import supabase_auth
from fastapi import Depends, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel
from starlette.requests import Request

from src.autobots.access.access_doc_model import AccessDocsFound, AccessFind
from src.autobots.access.user_access import UserAccess
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.logging.app_code import AppCode
from src.autobots.space.space_auth import get_optional_space_member
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM


class Product(str, Enum):
    PLATFORM = "platform"
    KIWICODE = "kiwicode"


class ProductResource(str, Enum):
    Agents = "agents"
    Workflows = "workflows"
    Teams = "teams"
    AppConn = "app_connection"
    Schedules = "schedules"


class ResourceOperation(str, Enum):
    Create = "create"
    Read = "read"
    Update = "update"
    Delete = "delete"
    Execute = "execute"
    Share = "share"
    Publish = "publish"


class ResourcePermission(BaseModel):
    resource: ProductResource
    operation: ResourceOperation
    resource_id: str | Literal["all"]


# Hardcoded permissions per product (placeholder until you store these in DB).
PRODUCT_PERMISSIONS: dict[Product, list[ResourcePermission]] = {
    Product.KIWICODE: [
        ResourcePermission(resource=ProductResource.Agents, operation=ResourceOperation.Read, resource_id="all"),
        
        # Execute permissions scoped to specific action ids
        ResourcePermission(
            resource=ProductResource.Agents,
            operation=ResourceOperation.Execute,
            resource_id="6a4be8fbc469a7afb3757403",  # Autocode
        ),
        ResourcePermission(
            resource=ProductResource.Agents,
            operation=ResourceOperation.Execute,
            resource_id="6a4cdb3c15f87a77ba650f0d",  # Autocode GROK
        ),
        ResourcePermission(
            resource=ProductResource.Agents,
            operation=ResourceOperation.Execute,
            resource_id="6a4cd992e0e2efd4b2655256",  # Autocode QWEN
        ),
        
        ResourcePermission(
            resource=ProductResource.Agents,
            operation=ResourceOperation.Execute,
            resource_id="6a4cd1d2c469a7afb3757405",  # Autocode OPENAI
        ),
        ResourcePermission(
            resource=ProductResource.Agents,
            operation=ResourceOperation.Execute,
            resource_id="6a4cd4b6c469a7afb3757406",  # Autocode Claude
        ),
        ResourcePermission(
            resource=ProductResource.Agents,
            operation=ResourceOperation.Execute,
            resource_id="6a4cd4e0bc3d3cfea1172619",  # Autocode Claude Deep Thinker
        ),
        ResourcePermission(
            resource=ProductResource.Agents,
            operation=ResourceOperation.Execute,
            resource_id="6a4cd19215f87a77ba650f0c",  # Autocode OpenAI DeepThinker
        ),
        ResourcePermission(
            resource=ProductResource.Agents,
            operation=ResourceOperation.Execute,
            resource_id="6a4cd65e0fae4e6de5147a78",  # Autocode Gemini
        ),
        
        ## Scheduler Permissions --> Read
        
        ResourcePermission(resource=ProductResource.Schedules, operation=ResourceOperation.Read, resource_id="all"),
        
        ## Scheduler Permissions --> Create 
        ResourcePermission(
            resource=ProductResource.Schedules,
            operation=ResourceOperation.Create,
            resource_id="6a4be8fbc469a7afb3757403",  # Autocode Schedules
        ),
        ResourcePermission(
            resource=ProductResource.Schedules,
            operation=ResourceOperation.Create,
            resource_id="6a4cdb3c15f87a77ba650f0d",  # Autocode GROK Schedules
        ),
        ResourcePermission(
            resource=ProductResource.Schedules,
            operation=ResourceOperation.Create,
            resource_id="6a4cd992e0e2efd4b2655256",  # Autocode QWEN Schedules
        ),
        ResourcePermission(
            resource=ProductResource.Schedules,
            operation=ResourceOperation.Create,
            resource_id="6a4cd313e0e2efd4b2655254",  # Autocode OPENAI V2 Schedules
        ),
        ResourcePermission(
            resource=ProductResource.Schedules,
            operation=ResourceOperation.Create,
            resource_id="6a4cd4b6c469a7afb3757406",  # Autocode Claude Schedules
        ),
        ResourcePermission(
            resource=ProductResource.Schedules,
            operation=ResourceOperation.Create,
            resource_id="6a4cd4e0bc3d3cfea1172619",  # Autocode Claude Deep Thinker Schedules
        ),
        ResourcePermission(
            resource=ProductResource.Schedules,
            operation=ResourceOperation.Create,
            resource_id="6a4cd19215f87a77ba650f0c",  # Autocode OpenAI Schedules
        ),
        ResourcePermission(
            resource=ProductResource.Schedules,
            operation=ResourceOperation.Create,
            resource_id="6a4cd65e0fae4e6de5147a78",  # Autocode Gemini Schedules 
        ),
        
        
        ## Scheduler Permissions --> Update
        
        ResourcePermission(resource=ProductResource.Schedules, operation=ResourceOperation.Update, resource_id="all"),
        
        ## Scheduler Permissions --> Delete
        
        ResourcePermission(resource=ProductResource.Schedules, operation=ResourceOperation.Delete, resource_id="all"),
        # App connections / credentials management (needed to run tools inside Kiwi Code)
        ResourcePermission(resource=ProductResource.AppConn, operation=ResourceOperation.Read, resource_id="all"),
        ResourcePermission(resource=ProductResource.AppConn, operation=ResourceOperation.Create, resource_id="all"),
        ResourcePermission(resource=ProductResource.AppConn, operation=ResourceOperation.Update, resource_id="all"),
        ResourcePermission(resource=ProductResource.AppConn, operation=ResourceOperation.Delete, resource_id="all"),
        
    ],
}




def require_permission(resource: ProductResource, operation: ResourceOperation):
    """FastAPI dependency factory that checks product permissions for a given resource/operation.

    Current rules:
    - If the user has a valid "Platform" subscription, treat them as Product.PLATFORM and allow.
      (Tighten later if PLATFORM also needs granular permissions.)
    - If the user has a valid "Kiwi-Code" subscription, apply PRODUCT_PERMISSIONS[Product.KIWICODE].
    """

    async def _check(
        request: Request,
        user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
        db: AsyncIOMotorDatabase = Depends(get_mongo_db),
        space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    ) -> bool:
        ctx = request.state.context

        user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)

        access_find = AccessFind(status="valid")
        access_docs_found: AccessDocsFound = await UserAccess(user=user_orm, db=db).list_user_access(
            ctx=ctx,
            access_find=access_find,
            limit=100,
            offset=0,
        )

        products: list[Product] = []
        for doc in access_docs_found.docs:
            if doc.subscription == "Kiwi-Code":
                products.append(Product.KIWICODE)
            elif doc.subscription == "Platform":
                products.append(Product.PLATFORM)

        # logger.bind(ctx=ctx).info({"subscribed_products": [p.value for p in products]})

        # PLATFORM: allow (current behavior)
        if Product.PLATFORM in products:
            return True

        # KIWICODE: enforce hardcoded permissions
        if Product.KIWICODE in products:
            permissions = PRODUCT_PERMISSIONS.get(Product.KIWICODE, [])

            resource_id = (
                request.path_params.get("id")
                or request.path_params.get("action_id")
            )
            
            logger.bind(ctx=ctx, code=AppCode.PRODUCT).info(
                f"resource : {resource} and operation : {operation}"
            )
            
            for permission in permissions:
                ## Support for Scheduler Create --> 
                if permission.resource == resource and permission.operation == operation:
                    if resource == ProductResource.Schedules and operation == ResourceOperation.Create:
                        
                        payload = await request.json()
                        # logger.info(f"Payload : {payload}")
                        if payload.get("runnable_id") == permission.resource_id:
                            return True
                    else:
                        if permission.resource_id == "all" or permission.resource_id == resource_id:
                            return True

            ## Processing For Autodata
            
            payload = await request.json()
            try:
                action_payload = payload.get("action")
                if action_payload:
                    if action_payload.get("name") == "Knowledge graph query":
                        return True
            except Exception:
                pass
            
            ## Processing For Schedules Create --> 
            
            

        logger.bind(
            ctx=ctx,
            code=AppCode.PRODUCT,
            resource=resource.value,
            operation=operation.value,
        ).warning("Product permission denied")
        raise HTTPException(status_code=403, detail="Insufficient product permissions")

    return _check
