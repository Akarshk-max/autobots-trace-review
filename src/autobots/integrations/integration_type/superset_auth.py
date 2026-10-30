from typing import Dict, Optional, Type

import httpx
from loguru import logger
from pydantic import BaseModel, HttpUrl

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.core.clock.clock import Clock
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.integration_type import (
    AppCredsModelType,
    IntegrationModelType,
    IntegrationTypeABC,
)


class SupersetCredsModel(BaseModel):
    url: str
    username: str
    password: str
    provider: str = "db"
    refresh: bool = True


class SupersetAuthModel(BaseModel):
    access_token: str
    refresh_token: str
    csrf_token: str
    # created_at: str


class SupersetAuth(IntegrationTypeABC):
    login_path: str = "/api/v1/security/login"
    refresh_path: str = "/api/v1/security/refresh"
    csrf_path: str = "/api/v1/security/csrf_token"

    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.superset

    @staticmethod
    async def get_api_domain(app_creds: AppCredsModelType | None) -> str:
        return app_creds.url

    @staticmethod
    def get_creds_model_type() -> Type[AppCredsModelType]:
        return SupersetCredsModel

    @staticmethod
    def get_auth_model_type() -> Type[IntegrationModelType]:
        return SupersetAuthModel

    @staticmethod
    async def create_auth(
        ctx: Context, app_creds: SupersetCredsModel
    ) -> SupersetAuthModel | Exception:
        superset_auth_dict = {}
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response: httpx.Response = await client.post(
                url=f"{app_creds.url}/{SupersetAuth.login_path}",
                json={
                    "username": app_creds.username,
                    "password": app_creds.password,
                    "refresh": True,
                    "provider": app_creds.provider,
                },
            )
            if response.status_code != 200:
                return Exception(
                    f"Failed to login to superset. Status code: {response.status_code}"
                )
            response_dict = response.json()
            if response_dict.get("access_token") and response_dict.get("refresh_token"):
                superset_auth_dict.update(response_dict)

                access_token = superset_auth_dict["access_token"]
                csrf_response: httpx.Response = await client.get(
                    url=f"{app_creds.url}/{SupersetAuth.csrf_path}/",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if csrf_response.status_code != 200:
                    return Exception(
                        f"Failed to get csrf token. Status code: {csrf_response.status_code}"
                    )
                csrf_response_dict = csrf_response.json()
                superset_auth_dict.update({"csrf_token": csrf_response_dict.get("result")})

        superset_auth_model = SupersetAuthModel(**superset_auth_dict)
        return superset_auth_model

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: SupersetCredsModel | None, app_auth: SupersetAuthModel | None
    ) -> SupersetAuthModel | Exception:
        async with httpx.AsyncClient() as client:
            response: httpx.Response = await client.post(
                url=f"{app_creds.url}/{SupersetAuth.refresh_path}",
                headers={"Authorization": f"Bearer {app_auth.refresh_token}"},
            )
            if response.status_code == 200:
                response_dict = response.json()
                app_auth.model_dump().update(response_dict)
                return SupersetAuthModel(**app_auth.model_dump())
            else:
                return Exception(
                    f"Failed to login to superset. Status code: {response.status_code}"
                )

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: SupersetAuthModel) -> bool:
        curr_time = Clock.datetime_now()
        auth_creation_time = app_auth.created_at
        duration_since_creation = curr_time - auth_creation_time
        duration_sec = duration_since_creation.total_seconds()
        is_refresh = duration_sec > app_auth.expires_in - 600
        logger.bind(
            ctx=ctx,
            is_refresh_required=is_refresh,
            expires_in=app_auth.expires_in,
            duration_sec=duration_sec,
            curr_time=str(curr_time),
            auth_creation_time=str(auth_creation_time),
        ).info(f"is_refresh_required: {is_refresh}")
        return is_refresh

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: SupersetAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {app_auth.access_token}",
            "X-CSRFToken": f"{app_auth.csrf_token}",
        }

    @staticmethod
    async def get_updated_url(
        ctx: Context, app_creds: SupersetCredsModel, app_auth: SupersetAuthModel, url: str
    ) -> str:
        http_url = HttpUrl(url)
        superset_url = await SupersetAuth.get_api_domain(app_creds)
        new_url = url.replace(f"{http_url.scheme}://{http_url.host}", superset_url)
        return new_url
