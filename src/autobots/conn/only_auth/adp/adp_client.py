import os
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime
from typing import Optional

import httpx
from fastapi import HTTPException
from loguru import logger
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential

from src.autobots.conn.object_storage.storage_file_resolver import (
    download_storage_file_to_temp,
    resolve_local_or_storage_file,
)
from src.autobots.core.clock.clock import Clock
from src.autobots.data_model.context import Context


def _validate_pem_file(file_path: str, file_type: str = "certificate") -> bool:
    """Validate that a file looks like a valid PEM file"""
    try:
        with open(file_path, "r") as f:
            content = f.read()
            if file_type == "certificate":
                if "-----BEGIN CERTIFICATE-----" not in content:
                    logger.warning(
                        f"Certificate file doesn't contain BEGIN CERTIFICATE: {file_path}"
                    )
                    return False
            elif file_type == "key":
                if "-----BEGIN" not in content or "PRIVATE KEY-----" not in content:
                    logger.warning(f"Key file doesn't contain PRIVATE KEY: {file_path}")
                    return False
                if "ENCRYPTED" in content:
                    logger.error(
                        f"Private key appears to be encrypted (password-protected). ADP requires unencrypted keys: {file_path}"
                    )
                    return False
        return True
    except Exception as e:
        logger.error(f"Error validating PEM file {file_path}: {e}")
        return False


class ADPAccessTokenRq(BaseModel):
    """
    ADP OAuth2 Client Credentials Request.

    ADP requires Mutual SSL (mTLS) Authentication:
    - client_id: Your ADP client ID
    - client_secret: Your ADP client secret
    - ssl_cert_path: Path to certificate file (local path or S3 URL)
    - ssl_key_path: Path to private key file (local path or S3 URL)

    Supported path formats:
    - Local file: /path/to/cert.pem
    - S3 URL: s3://bucket-name/path/to/cert.pem
    - S3 HTTPS: https://bucket-name.s3.region.amazonaws.com/path/to/cert.pem

    Certificates are issued by ADP and valid for 2 years.
    """

    client_id: str
    client_secret: str
    ssl_cert_path: str  # Required - local path or S3 URL
    ssl_key_path: Optional[str] = None  # Optional if key is combined with cert


class ADPAccessToken(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int = 3600  # Default 1 hour
    scope: Optional[str] = None
    created_at: datetime = Field(default_factory=Clock.datetime_now)


class ADPRefreshedAccessToken(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int = 3600
    scope: Optional[str] = None


def _is_s3_url(path: str) -> bool:
    """
    Legacy compatibility helper.

    New production resolution is provider-neutral and handled by
    resolve_local_or_storage_file().
    """
    return path.startswith("s3://") or ".s3." in path


async def _download_s3_to_temp(
    ctx: Context,
    s3_url: str,
    suffix: str = ".pem",
) -> str:
    """
    Legacy compatibility wrapper.

    Despite the historical name, deployment-owned Azure and S3 URLs are
    resolved through the provider-neutral storage resolver.
    """
    return await download_storage_file_to_temp(
        ctx=ctx,
        path_or_url=s3_url,
        suffix=suffix,
    )


@asynccontextmanager
async def _get_cert_paths(ctx: Context, rq: ADPAccessTokenRq):
    """
    Resolve ADP certificate/key paths for the duration of an mTLS operation.

    Local paths remain unchanged. Deployment-owned storage URLs are downloaded
    to secure temporary files and cleaned automatically.
    """
    async with AsyncExitStack() as stack:
        cert_path = await stack.enter_async_context(
            resolve_local_or_storage_file(
                ctx=ctx,
                path_or_url=rq.ssl_cert_path,
                suffix=".pem",
            )
        )

        key_path = None
        if rq.ssl_key_path:
            key_path = await stack.enter_async_context(
                resolve_local_or_storage_file(
                    ctx=ctx,
                    path_or_url=rq.ssl_key_path,
                    suffix=".key",
                )
            )

        yield cert_path, key_path


class ADP:
    # ADP OAuth token endpoint
    TOKEN_URL = "https://accounts.adp.com/auth/oauth/v2/token"

    @staticmethod
    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True
    )
    async def access_token(ctx: Context, rq: ADPAccessTokenRq) -> ADPAccessToken | Exception:
        """
        Get access token using client credentials flow with Mutual SSL.

        Supports S3 URLs for certificate files - they will be downloaded
        to temporary files automatically.
        """
        try:
            payload = {
                "grant_type": "client_credentials",
                "client_id": rq.client_id,
                "client_secret": rq.client_secret,
            }

            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
            }

            logger.bind(ctx=ctx).info(f"Attempting to get ADP access token from {ADP.TOKEN_URL}")

            async with _get_cert_paths(ctx, rq) as (cert_path, key_path):
                # Validate certificate files exist
                if not os.path.exists(cert_path):
                    raise FileNotFoundError(f"SSL certificate file not found: {cert_path}")

                cert_size = os.path.getsize(cert_path)
                logger.bind(ctx=ctx, cert_path=cert_path, cert_size=cert_size).info(
                    "Certificate file found"
                )

                # Validate PEM format
                _validate_pem_file(cert_path, "certificate")

                if key_path:
                    if not os.path.exists(key_path):
                        raise FileNotFoundError(f"SSL key file not found: {key_path}")
                    key_size = os.path.getsize(key_path)
                    logger.bind(ctx=ctx, key_path=key_path, key_size=key_size).info(
                        "Key file found"
                    )
                    _validate_pem_file(key_path, "key")
                else:
                    # If no separate key, the cert file should contain both cert and key
                    _validate_pem_file(cert_path, "key")

                logger.bind(ctx=ctx, cert_path=cert_path, key_path=key_path).info(
                    "Creating SSL context for mTLS"
                )

                # Create SSL context with client certificate for mTLS
                ssl_context = httpx.create_ssl_context()
                ssl_context.load_cert_chain(
                    certfile=cert_path, keyfile=key_path if key_path else None
                )

                async with httpx.AsyncClient(timeout=300.0, verify=ssl_context) as client:
                    response = await client.post(
                        url=ADP.TOKEN_URL,
                        data=payload,
                        headers=headers,
                    )

                    logger.bind(
                        ctx=ctx,
                        status=response.status_code,
                        response_text=response.text[:500] if response.text else "",
                    ).info("ADP token response")

                    if response.status_code == 200:
                        response_data = response.json()
                        logger.bind(ctx=ctx).info("Successfully obtained ADP access token")

                        return ADPAccessToken(
                            access_token=response_data["access_token"],
                            token_type=response_data.get("token_type", "Bearer"),
                            expires_in=response_data.get("expires_in", 3600),
                            scope=response_data.get("scope"),
                        )
                    else:
                        logger.bind(ctx=ctx).error(
                            f"Failed to get ADP token. Status: {response.status_code}, Response: {response.text}"
                        )
                        return HTTPException(status_code=response.status_code, detail=response.text)

        except Exception as e:
            logger.bind(ctx=ctx).exception(f"ADP access token error: {str(e)}")
            return e

    @staticmethod
    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True
    )
    async def refresh_access_token(
        ctx: Context, rq: ADPAccessTokenRq
    ) -> ADPRefreshedAccessToken | Exception:
        """
        Refresh access token by getting a new one.
        ADP client credentials flow doesn't use refresh tokens.
        """
        try:
            payload = {
                "grant_type": "client_credentials",
                "client_id": rq.client_id,
                "client_secret": rq.client_secret,
            }

            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
            }

            logger.bind(ctx=ctx).info(f"Refreshing ADP access token from {ADP.TOKEN_URL}")

            async with _get_cert_paths(ctx, rq) as (cert_path, key_path):
                # Create SSL context with client certificate for mTLS
                ssl_context = httpx.create_ssl_context()
                ssl_context.load_cert_chain(
                    certfile=cert_path, keyfile=key_path if key_path else None
                )

                async with httpx.AsyncClient(timeout=300.0, verify=ssl_context) as client:
                    response = await client.post(
                        url=ADP.TOKEN_URL,
                        data=payload,
                        headers=headers,
                    )

                    logger.bind(ctx=ctx).info(
                        f"ADP token refresh response status: {response.status_code}"
                    )

                    if response.status_code == 200:
                        response_data = response.json()
                        logger.bind(ctx=ctx).info("Successfully refreshed ADP access token")

                        return ADPRefreshedAccessToken(
                            access_token=response_data["access_token"],
                            token_type=response_data.get("token_type", "Bearer"),
                            expires_in=response_data.get("expires_in", 3600),
                            scope=response_data.get("scope"),
                        )
                    else:
                        logger.bind(ctx=ctx).error(
                            f"Failed to refresh ADP token. Status: {response.status_code}, Response: {response.text}"
                        )
                        return HTTPException(status_code=response.status_code, detail=response.text)

        except Exception as e:
            logger.bind(ctx=ctx).exception(f"ADP token refresh error: {str(e)}")
            return e

    @staticmethod
    async def create_auth_header(token: str) -> dict[str, str]:
        """Create authorization header with Bearer token"""
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def is_token_expired(token: ADPAccessToken) -> bool:
        """Check if the token is expired (with 5 minute buffer)"""
        from datetime import timedelta

        expiry_time = token.created_at + timedelta(seconds=token.expires_in - 300)
        return Clock.datetime_now() > expiry_time
