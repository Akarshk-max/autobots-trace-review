from typing import Optional

import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel
from starlette.requests import Request

from src.autobots import SettingsProvider
from src.autobots.integrations.integration_model import IntegrationCreate, OAuthModel
from src.autobots.integrations.integration_type.adp_auth import ADPAuth, ADPCreds
from src.autobots.integrations.user_integration import UserIntegration
from src.autobots.integrations.app_types import AppTypes
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(prefix=SettingsProvider.sget().API_AUTH, tags=[SettingsProvider.sget().API_TAG_EXTERNAL_APP_AUTH])


class ADPClientCredentialsRequest(BaseModel):
    """
    Request model for ADP client credentials authentication.
    
    ADP requires Mutual SSL (mTLS) Authentication:
    - client_id: Your ADP client ID from ADP Developer Portal
    - client_secret: Your ADP client secret
    - ssl_cert_path: Path to SSL certificate file (PEM or PFX format)
    - ssl_key_path: Path to private key file (optional if embedded in cert)
    
    Supported path formats for certificates:
    - Local file path: /path/to/cert.pem
    - S3 URI: s3://bucket-name/path/to/cert.pem
    - S3 HTTPS URL: https://bucket-name.s3.region.amazonaws.com/path/to/cert.pem
    
    Certificate Setup:
    1. Obtain SSL certificate and private key from:
       - ADP Partners: Developer Self-Service Portal (automated)
       - ADP Clients: ADP Certificate Signing Tool (manual CSR)
       - API Central Users: API Central (automated)
    
    2. Upload certificates to S3 (recommended) or provide local paths:
       s3://your-bucket/adp/cert.pem
       s3://your-bucket/adp/key.key
    
    3. Optionally combine into single PFX file:
       openssl pkcs12 -export -out adp.pfx -name "OrgName Mutual SSL" \\
           -inkey private-key.key -in public-certificate.pem
    
    Note: Certificates are valid for 2 years. ADP sends renewal notices
    60 days before expiration.
    """

    client_id: str
    client_secret: str
    ssl_cert_path: str  # Required - local path or S3 URL
    ssl_key_path: Optional[str] = None  # Optional if key is combined with cert


@router.post("/adp/client-credentials")
async def adp_client_credentials(
    request: Request,
    adp_request: ADPClientCredentialsRequest,
    name: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    """
    Authenticate using ADP client credentials flow with Mutual SSL.

    ADP mandates mTLS where both client and server verify each other's
    identity through SSL certificates.

    Certificate paths can be:
    - Local file paths: /path/to/cert.pem
    - S3 URLs: s3://bucket/path/cert.pem or https://bucket.s3.region.amazonaws.com/path/cert.pem

    S3 certificates will be automatically downloaded to temporary files,
    used for authentication, then securely deleted.
    """
    ctx = Context(user_id=str(user_res.user.id))
    try:
        user = UserORM(id=user_res.user.id)

        adp_creds = ADPCreds(
            client_id=adp_request.client_id,
            client_secret=adp_request.client_secret,
            ssl_cert_path=adp_request.ssl_cert_path,
            ssl_key_path=adp_request.ssl_key_path,
        )

        oauth_rs = await ADPAuth.create_auth(ctx=ctx, app_creds=adp_creds)

        if isinstance(oauth_rs, Exception):
            logger.bind(ctx=ctx).error(f"Failed to create ADP auth: {str(oauth_rs)}")
            raise HTTPException(status_code=400, detail=str(oauth_rs))

        assert isinstance(oauth_rs, OAuthModel)
        integration_create = IntegrationCreate(
            app_name=AppTypes.adp, name=name, auth=oauth_rs.model_dump(exclude_none=True)
        )

        created_auth = await UserIntegration.create(
            ctx=ctx, db=db, user=user, integration_create=integration_create, decrypt=True
        )
        logger.bind(ctx=ctx).info("ADP Client Credentials Auth Created")

        return {"message": "ADP authentication successful", "auth_id": str(created_auth.id)}

    except HTTPException:
        raise
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Error in ADP client credentials auth: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
