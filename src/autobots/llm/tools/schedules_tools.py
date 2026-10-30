from typing import List

import httpx
from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from src.autobots import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.schedule.schedule_doc_model import (
    ScheduleCreate,
    ScheduleDoc,
    ScheduleFind,
    ScheduleUpdate,
)
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState

class SchedulerTools:
    class SchedulerId(BaseModel):
        schedule_id: str

    class ScheduleUpdateConfig(BaseModel):
        schedule_id: str
        schedule_update: ScheduleUpdate

    class ScheduleFindConfig(BaseModel):
        find: ScheduleFind
        limit: int = 100
        offset: int = 0

    @staticmethod
    def get_schedule_api_url(id: str = None) -> str:
        """
        Description:
        - Build the scheduler API URL for schedule operations.
        - If an id is provided, returns the URL for a specific schedule resource.
        - If no id is provided, returns the collection URL used for create/list.

        Input Config:
        - id (string, required):
            - When provided, appended to the base schedules URL,
              representing /schedules/{id}.
        """
        settings = SettingsProvider.sget()
        url = f"{settings.SCHEDULER_URL}{settings.API_v1}{settings.API_SCHEDULES}/"
        if id:
            url = f"{url}{id}"
        return url

    @staticmethod
    async def create_schedule(
        config: ScheduleCreate,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
    ) -> ScheduleDoc | HTTPException:
        """
        Description:
        - Create a new schedule in the scheduler service.
        - Sends a POST request with the schedule definition and returns the created schedule document.

        When to use:
        - Use to register a new runnable to be executed on a specified cron or interval trigger.
        - Use when a user wants to schedule repeated or timed executions of a runnable with a given input.

        Input config:
        {
          "name": "<human readable name>",
          "trigger": { ...cron_or_interval_trigger... },
          "runnable_id": "<id of the runnable to execute>",
          "input": {
            "text": "<input payload or parameters>",
          }
        }

        Fields:
        - name (string, required):
            - Descriptive name of the schedule.
        - trigger (object, required):
            - CronSchedule or IntervalTriggerModel-structured object describing when the job runs.
        - runnable_id (string, required):
            - Identifier of the runnable that should be executed on schedule.
        - input (DataBlock, required):
            - Payload forwarded to the runnable when the schedule triggers.
        """
        try:
            url = SchedulerTools.get_schedule_api_url()
            token = user.access_token
            json = config.model_dump(exclude_none=True)
            auth_header = {"authorization": f"Bearer {token}"}
            async with httpx.AsyncClient() as client:
                resp = await client.post(url=url, json=json, headers=auth_header)
                logger.bind(ctx=ctx, status_code=resp.status_code).info(
                    f"Schedule created for runnable {config.runnable_id} by user {user.id}"
                )
                match resp.status_code:
                    case 200:
                        schedule_doc = ScheduleDoc.model_validate(resp.json())
                        return schedule_doc
                    case _:
                        return HTTPException(resp.status_code, resp.text)
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            match e:
                case HTTPException():
                    return e
                case _:
                    return HTTPException(status_code=500, detail=str(e))

    @staticmethod
    async def get_schedule(
        config: SchedulerId,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
    ) -> ScheduleDoc | HTTPException:
        """
        Description:
        - Retrieve a single schedule document by its id from the scheduler service.
        - Sends a GET request to the schedule detail endpoint.

        When to use:
        - Use when you need to inspect a specific schedule"s configuration and metadata.
        - Use to verify that a schedule exists or to read its current trigger, runnable, and input.

        Input config:
        {
          "schedule_id": "<schedule_id>"
        }

        Fields:
        - schedule_id (string, required):
            - Unique identifier of the schedule to fetch.
        """
        try:
            url = SchedulerTools.get_schedule_api_url(id=config.schedule_id)
            token = user.access_token
            auth_header = {"authorization": f"Bearer {token}"}
            async with httpx.AsyncClient() as client:
                resp = await client.get(url=url, headers=auth_header)
                logger.bind(ctx=ctx, status_code=resp.status_code).info(
                    f"Schedule get for id: {config.schedule_id} by user {user.id}"
                )
                match resp.status_code:
                    case 200:
                        schedule_doc_str = resp.json()
                        schedule_doc = ScheduleDoc.model_validate(schedule_doc_str)
                        return schedule_doc
                    case _:
                        return HTTPException(resp.status_code, resp.text)
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            match e:
                case HTTPException():
                    return e
                case _:
                    return HTTPException(status_code=500, detail=str(e))

    @staticmethod
    async def update_schedule(
        config: ScheduleUpdateConfig,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
    ) -> ScheduleDoc | HTTPException:
        """
        Description:
        - Update fields of an existing schedule in the scheduler service.
        - Sends a PUT request with partial fields to modify name, trigger, runnable, or input.

        When to use:
        - Use when a schedule already exists and you want to modify its timing or payload.
        - Use to rename a schedule, change the runnable, or update the input data.

        Input config:
        {
          "schedule_id": "<schedule_id>",
          "schedule_update": {
            "name": "<new_name>",
            "trigger": { ...new_trigger... },
            "runnable_id": "<new_runnable_id>",
            "input": {
              "text": "<new_input_payload>"
            }
          }
        }

        Fields:
        - schedule_id (string, required):
            - Identifier of the schedule to update.
        - schedule_update (object, required):
            - Partial schedule definition with any fields that should be changed:
                - name (string, optional)
                - trigger (object, optional)
                - runnable_id (string, optional)
                - input (DataBlock, optional)
        """
        try:
            url = SchedulerTools.get_schedule_api_url(id=config.schedule_id)
            json = config.schedule_update.model_dump(exclude_none=True)
            token = user.access_token
            auth_header = {"authorization": f"Bearer {token}"}
            async with httpx.AsyncClient() as client:
                resp = await client.put(url=url, json=json, headers=auth_header)
                logger.bind(ctx=ctx, status_code=resp.status_code).info(
                    f"Schedule updated for id {config.schedule_id} by user {user.id}"
                )
                match resp.status_code:
                    case 200:
                        schedule_doc = ScheduleDoc.model_validate(resp.json())
                        return schedule_doc
                    case _:
                        return HTTPException(resp.status_code, resp.text)
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            match e:
                case HTTPException():
                    return e
                case _:
                    return HTTPException(status_code=500, detail=str(e))

    @staticmethod
    async def delete_schedule(
        config: SchedulerId,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
    ) -> ScheduleDoc | HTTPException:
        """
        Description:
        - Delete an existing schedule from the scheduler service.
        - Sends a DELETE request to the schedule detail endpoint.

        When to use:
        - Use when a schedule is no longer needed and should be removed.
        - Use to stop future executions of a scheduled runnable.

        Input config:
        {
          "schedule_id": "<schedule_id>"
        }

        Fields:
        - schedule_id (string, required):
            - Identifier of the schedule to delete.
        """
        try:
            url = SchedulerTools.get_schedule_api_url(id=config.schedule_id)
            token = user.access_token
            auth_header = {"authorization": f"Bearer {token}"}
            async with httpx.AsyncClient() as client:
                resp = await client.delete(url=url, headers=auth_header)
                logger.bind(ctx=ctx, status_code=resp.status_code).info(
                    f"Schedule deleted for id {config.schedule_id} by user {user.id}"
                )
                match resp.status_code:
                    case 200:
                        schedule_doc = ScheduleDoc.model_validate(resp.json())
                        return schedule_doc
                    case _:
                        return HTTPException(resp.status_code, resp.text)
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            match e:
                case HTTPException():
                    return e
                case _:
                    return HTTPException(status_code=500, detail=str(e))

    @staticmethod
    async def find_schedule(
        config: ScheduleFindConfig,
        ctx: Context,
        app_state: AppState,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
    ) -> List[ScheduleDoc] | HTTPException:
        """
        Description:
        - Find schedules matching filter criteria by querying the scheduler service.
        - Sends a GET request to the schedules collection with query parameters built from the filter.

        When to use:
        - Use to list schedules for a user or runnable, or to filter by name or other identifying fields.
        - Use to search for schedules before performing bulk updates or deletes.

        Input config:
        {
          "find": {
            "id": "<schedule_id>",              // required
            "name": "<schedule_name>",          // optional
            "runnable_id": "<runnable_id>",     // required
            "job_id": "<job_id>",               // required
            "input": {                          // optional
              "text": "<matching_input_text>"
              // any other DataBlock fields if used server-side
            }
          },
          "limit": 100,                         // optional, currently not passed as query param
          "offset": 0                           // optional, currently not passed as query param
        }

        Fields:
        - find (ScheduleFind, required):
            - Filter object with optional fields:
                - id (string, required)
                - name (string, optional)
                - runnable_id (string, required)
                - job_id (string, required)
                - input (DataBlock, optional)
        - limit (int, optional, default 100):
            - Logical limit for maximum number of results (not currently applied as a query param).
        - offset (int, optional, default 0):
            - Logical offset for paginated results (not currently applied as a query param).
        """
        try:
            url = SchedulerTools.get_schedule_api_url()
            params = config.find.model_dump(exclude_none=True)
            token = user.access_token
            auth_header = {"authorization": f"Bearer {token}"}
            async with httpx.AsyncClient() as client:
                resp = await client.get(url=url, params=params, headers=auth_header)
                logger.bind(ctx=ctx, find=config.find, status_code=resp.status_code).info(
                    f"Schedule listing for user {user.id}"
                )
            match resp.status_code:
                case 200:
                    schedule_docs = resp.json()
                    return schedule_docs
                case _:
                    return HTTPException(resp.status_code, resp.text)
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            match e:
                case HTTPException():
                    return e
                case _:
                    return HTTPException(status_code=500, detail=str(e))
