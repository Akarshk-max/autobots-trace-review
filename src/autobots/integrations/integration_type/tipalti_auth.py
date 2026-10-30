"""
Tipalti API authentication using HMAC-SHA256 signature.

Tipalti uses encryption key-based authentication (not OAuth):
- auth_str = PayerName + timestamp [+ payee_id for payee functions] [+ EAT params]
- signature = HMAC-SHA256(api_key, auth_str).hexdigest()

Ref: https://soap-support.tipalti.com/Content/Topics/Development/APIs/UseEncryptionKey.htm
"""

import hashlib
import hmac
import math
import time
from typing import Dict, Literal, Optional, Type

from pydantic import BaseModel

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC


class TipaltiCreds(BaseModel):
    """Credentials for Tipalti API. Payer name and API key from Tipalti dashboard (Administration > API integration)."""

    payer_name: str
    api_key: str
    environment: Literal["sandbox", "production"] = "sandbox"


class TipaltiAuthModel(BaseModel):
    """Auth model mirrors creds - Tipalti uses request-specific HMAC, no stored tokens."""

    payer_name: str
    api_key: str
    environment: Literal["sandbox", "production"] = "sandbox"


def _compute_tipalti_signature(
    payer_name: str,
    api_key: str,
    payee_id: Optional[str] = None,
    eat_param: Optional[str] = None,
) -> tuple[str, str]:
    """
    Compute Tipalti HMAC-SHA256 signature.
    Returns (signature_hex, timestamp_str).
    """
    now = str(math.floor(time.time()))
    auth_str = payer_name + now
    if payee_id:
        auth_str += payee_id
    if eat_param is not None:
        auth_str += eat_param

    signature = hmac.new(
        api_key.encode("utf-8"),
        auth_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return signature, now


class TipaltiAuth(IntegrationTypeABC):
    """Tipalti app auth - HMAC-SHA256 per request, no OAuth."""

    PAYER_API_BASE_SANDBOX = "https://api.sandbox.tipalti.com"
    PAYER_API_BASE_PRODUCTION = "https://api.tipalti.com"

    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.tipalti

    @staticmethod
    async def get_api_domain(app_creds: TipaltiCreds | None) -> str | None:
        if not app_creds:
            return None
        return (
            TipaltiAuth.PAYER_API_BASE_SANDBOX
            if app_creds.environment == "sandbox"
            else TipaltiAuth.PAYER_API_BASE_PRODUCTION
        )

    @staticmethod
    def get_creds_model_type() -> Type[TipaltiCreds]:
        return TipaltiCreds

    @staticmethod
    def get_auth_model_type() -> Type[TipaltiAuthModel]:
        return TipaltiAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: TipaltiCreds) -> TipaltiAuthModel | Exception:
        return TipaltiAuthModel(
            payer_name=app_creds.payer_name,
            api_key=app_creds.api_key,
            environment=app_creds.environment,
        )

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: TipaltiCreds, app_auth: TipaltiAuthModel
    ) -> TipaltiAuthModel | Exception:
        return app_auth

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: TipaltiAuthModel) -> bool:
        return False

    @staticmethod
    async def get_auth_header(
        ctx: Context,
        app_auth: TipaltiAuthModel,
        config: Optional[APIRequest] = None,
    ) -> Dict[str, str]:
        """
        Compute Tipalti HMAC signature and return headers.
        For payer functions: auth_str = PayerName + timestamp.
        Config body/params can optionally provide payee_id or eat_param for payee/EAT flows.
        """
        payee_id = None
        eat_param = None
        if config:
            if config.body:
                payee_id = config.body.get("idap") or config.body.get("payee_id")
                eat_param = config.body.get("eat_param")
            if config.params:
                payee_id = payee_id or config.params.get("idap") or config.params.get("payee_id")

        signature, timestamp = _compute_tipalti_signature(
            payer_name=app_auth.payer_name,
            api_key=app_auth.api_key,
            payee_id=payee_id,
            eat_param=str(eat_param) if eat_param is not None else None,
        )

        return {
            "X-Tipalti-Key": signature,
            "X-Tipalti-Timestamp": timestamp,
            "X-Tipalti-PayerName": app_auth.payer_name,
        }
