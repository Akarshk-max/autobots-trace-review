import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel
from starlette.requests import Request

from src.autobots.integrations.db_auth.db_auth import DatabaseAuth
from src.autobots.integrations.db_auth.db_auth_model import (
    DatabaseCredsModel,
    DbAuthCreate,
    DbAuthDoc,
    DbAuthLiteDocPage,
    DbAuthUpdate,
)
from src.autobots.integrations.db_auth.user_db_auth import UserDbAuth
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.space.space_auth import get_optional_space_member
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(prefix="/db-auth", tags=["db-auth"])


class Deleted(BaseModel):
    deleted_count: int


class DatabaseConnection(BaseModel):
    """Response model for database connection info (without sensitive data)"""

    id: str
    name: str | None
    database_type: str
    host: str | None = None  # Not used for BigQuery
    port: int | None = None
    database: str | None = None
    username: str | None = None
    # BigQuery specific fields
    project_id: str | None = None
    dataset: str | None = None


@router.post("/", response_model=DbAuthDoc)
async def create_db_auth(
    request: Request,
    db_creds: DatabaseCredsModel,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> DbAuthDoc:
    """
    Create a new database connection auth.
    Allows multiple database connections per user.
    """
    ctx = request.state.context
    user = UserORM(id=user_res.user.id, space_member=space_member)

    # Create auth from credentials
    auth_model = await DatabaseAuth.create_auth(ctx=ctx, app_creds=db_creds)
    if isinstance(auth_model, Exception):
        raise HTTPException(status_code=400, detail=str(auth_model))

    db_auth_create = DbAuthCreate(name=db_creds.name or "Unnamed",auth=auth_model.model_dump(exclude_none=True))
    db_auth_doc = await UserDbAuth.create(
        ctx=ctx,
        db=db,
        user=user,
        db_auth_create=db_auth_create,
    )
    if not db_auth_doc:
        raise HTTPException(status_code=400, detail="Error creating database auth")

    logger.bind(ctx=ctx).info(f"Created database auth: {db_creds.name or 'unnamed'}")
    db_auth_doc.auth = {}
    return db_auth_doc


@router.get("/", response_model=DbAuthLiteDocPage)
async def list_db_auths(
    request: Request,
    limit: int = 100,
    offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> DbAuthLiteDocPage:
    """List all database connection auths for the current user."""
    ctx = request.state.context
    user = UserORM(id=user_res.user.id, space_member=space_member)
    db_auth_docs = await UserDbAuth.list(ctx=ctx, db=db, user=user, limit=limit, offset=offset)
    return db_auth_docs


@router.get("/connections", response_model=list[DatabaseConnection])
async def list_db_connections(
    request: Request,
    limit: int = 100,
    offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> list[DatabaseConnection]:
    """
    List all database connections with details (without password).
    Useful for displaying connection info in UI.
    """
    ctx = request.state.context
    user = UserORM(id=user_res.user.id, space_member=space_member)

    page = await UserDbAuth.list(ctx=ctx, db=db, user=user, limit=limit, offset=offset)
    connections = []

    for doc in page.docs:
        full_doc = await UserDbAuth.get(ctx=ctx, db=db, user=user, db_auth_id=doc.id, decrypt=True)
        if full_doc:
            auth = DatabaseAuth.get_auth_model_type().model_validate(full_doc.auth)

            connections.append(
                DatabaseConnection(
                    id=doc.id,
                    name=auth.name,
                    database_type=auth.database_type.value,
                    host=auth.host,
                    port=auth.port,
                    database=auth.database,
                    username=auth.username,
                    # BigQuery specific fields
                    project_id=auth.project_id,
                    dataset=auth.dataset,
                )
            )

    return connections


@router.get("/{db_auth_id}", response_model=DbAuthDoc)
async def get_db_auth(
    request: Request,
    db_auth_id: str,
    decrypt: bool = False,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> DbAuthDoc:
    """Get a specific database auth by ID."""
    ctx = request.state.context
    user = UserORM(id=user_res.user.id, space_member=space_member)
    db_auth_doc = await UserDbAuth.get(ctx=ctx, db=db, user=user, db_auth_id=db_auth_id, decrypt=decrypt)
    if not db_auth_doc:
        raise HTTPException(status_code=404, detail="Database auth not found")
    db_auth_doc.auth = {}
    return db_auth_doc


@router.get("/by-name/{connection_name}", response_model=DbAuthDoc)
async def get_db_auth_by_name(
    request: Request,
    connection_name: str,
    decrypt: bool = False,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> DbAuthDoc:
    """Get a database auth by its memorable name."""
    ctx = request.state.context
    user = UserORM(id=user_res.user.id, space_member=space_member)
    db_auth_doc = await UserDbAuth.get_by_name(
        ctx=ctx, db=db, user=user, connection_name=connection_name, decrypt=decrypt
    )
    if not db_auth_doc:
        raise HTTPException(
            status_code=404, detail=f"Database auth with name '{connection_name}' not found"
        )
    db_auth_doc.auth = {}
    return db_auth_doc


@router.put("/{db_auth_id}", response_model=DbAuthDoc)
async def update_db_auth(
    request: Request,
    db_auth_id: str,
    db_creds: DatabaseCredsModel,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> DbAuthDoc:
    """Update an existing database auth."""
    ctx = request.state.context
    user = UserORM(id=user_res.user.id, space_member=space_member)

    # Verify the auth exists
    existing = await UserDbAuth.get(ctx=ctx, db=db, user=user, db_auth_id=db_auth_id, decrypt=True)
    if not existing:
        raise HTTPException(status_code=404, detail="Database auth not found")

    # --- FIX: merge update payload into existing auth so UI edits don't wipe secrets/fields ---
    try:
        existing_auth_model = DatabaseAuth.get_auth_model_type().model_validate(existing.auth)

        # Only fields explicitly provided by the client
        patch = db_creds.model_dump(exclude_unset=True)

        merged = existing_auth_model.model_dump()
        merged.update(patch)
        merged.pop("connection_string", None)  # force rebuild from current fields

        # Re-validate merged auth and keep connection_string in sync
        auth_model = DatabaseAuth.get_auth_model_type().model_validate(merged)
        auth_model.connection_string = auth_model.build_connection_string()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Optional safety: prevent saving broken BigQuery configs
    from src.autobots.integrations.db_auth.db_auth_model import DatabaseEnum as SharedDatabaseEnum

    if auth_model.database_type == SharedDatabaseEnum.bigquery and not auth_model.credentials_info:
        raise HTTPException(
            status_code=400,
            detail="credentials_info (service account JSON) is required for BigQuery",
        )

    db_auth_update = DbAuthUpdate(name=db_creds.name or auth_model.name or "Unnamed",auth=auth_model.model_dump(exclude_none=True))
    db_auth_doc = await UserDbAuth.update(
        ctx=ctx,
        db=db,
        user=user,
        id=db_auth_id,
        db_auth_update=db_auth_update,
    )
    if not db_auth_doc:
        raise HTTPException(status_code=400, detail="Error updating database auth")

    logger.bind(ctx=ctx).info(f"Updated database auth: {db_creds.name or db_auth_id}")
    db_auth_doc.auth = {}
    return db_auth_doc


@router.delete("/{db_auth_id}", response_model=Deleted)
async def delete_db_auth(
    request: Request,
    db_auth_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> Deleted:
    """Delete a database auth by ID."""
    ctx = request.state.context
    user = UserORM(id=user_res.user.id, space_member=space_member)
    deleted = await UserDbAuth.delete(ctx=ctx, db=db, user=user, db_auth_id=db_auth_id)
    if deleted is None:
        raise HTTPException(status_code=404, detail="Could not delete database auth")
    logger.bind(ctx=ctx).info(f"Deleted database auth: {db_auth_id}")
    return Deleted(deleted_count=deleted)


@router.post("/test-connection/{db_auth_id}")
async def test_db_connection(
    request: Request,
    db_auth_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> dict:
    """Test a database connection."""
    from src.autobots.integrations.db_auth.db_auth_model import DatabaseEnum

    ctx = request.state.context
    user = UserORM(id=user_res.user.id, space_member=space_member)

    db_auth_model = await UserDbAuth.get_database_auth_model(
        ctx=ctx, db=db, user=user, db_auth_id=db_auth_id
    )
    if not db_auth_model:
        raise HTTPException(status_code=404, detail="Database auth not found")

    engine = None
    try:
        from sqlalchemy import create_engine, text

        # BigQuery requires credentials_info to be passed to create_engine
        if db_auth_model.database_type == DatabaseEnum.bigquery:
            if not db_auth_model.credentials_info:
                raise HTTPException(
                    status_code=400,
                    detail="credentials_info (service account JSON) is required for BigQuery",
                )
            engine = create_engine(
                db_auth_model.build_connection_string(),
                credentials_info=db_auth_model.credentials_info,
            )
        elif (
            db_auth_model.database_type == DatabaseEnum.postgres
            and db_auth_model.cloud_sql_instance_connection_name
        ):
            # Cloud SQL Postgres connects via the Cloud SQL Python Connector, not a
            # plain connection string. A bare create_engine() on the pg8000 URL would
            # try to TCP-connect to a host named "cloudsql" with no user.
            from src.autobots.core.database.cloud_sql import create_cloud_sql_engine

            engine = create_cloud_sql_engine(
                instance_connection_name=db_auth_model.cloud_sql_instance_connection_name,
                user=db_auth_model.username or "",
                password=db_auth_model.password or "",
                database=db_auth_model.database or "postgres",
                use_private_ip=db_auth_model.cloud_sql_use_private_ip,
                enable_iam_auth=db_auth_model.cloud_sql_enable_iam_auth,
                credentials_info=db_auth_model.credentials_info,
            )
        else:
            engine = create_engine(db_auth_model.build_connection_string())

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

        return {"status": "success", "message": "Connection successful"}
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Database connection test failed: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Connection failed: {str(e)}")
    finally:
        # Dispose even if connect()/execute() raised, so a failed test never
        # leaks the engine (and, for Cloud SQL, the underlying connector).
        if engine is not None:
            engine.dispose()
