from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential

from src.autobots.core.clock.clock import Clock
from src.autobots.data_model.context import Context


class WorkdayAccessTokenRq(BaseModel):
    """
    Workday OAuth2 Access Token Request.
    
    Used to exchange authorization code for access/refresh tokens.
    """
    client_id: str
    client_secret: str
    code: str
    redirect_uri: str
    grant_type: str = "authorization_code"
    tenant_url: str  # e.g., https://wd2-impl-services1.workday.com or tenant REST base
    token_url: Optional[str] = None


class WorkdayRefreshTokenRq(BaseModel):
    """
    Workday OAuth2 Refresh Token Request.
    
    Used to refresh an expired access token.
    """
    client_id: str
    client_secret: str
    refresh_token: str
    grant_type: str = "refresh_token"
    tenant_url: str
    token_url: Optional[str] = None


class WorkdayAccessToken(BaseModel):
    """Token response from Workday OAuth"""
    access_token: str
    token_type: str = "Bearer"
    expires_in: int = 3600  # Typically 1 hour
    refresh_token: Optional[str] = None  # Non-expiring refresh tokens recommended
    scope: Optional[str] = None
    created_at: datetime = Field(default_factory=Clock.datetime_now)


class WorkdayRefreshedAccessToken(BaseModel):
    """Refreshed token response from Workday"""
    access_token: str
    token_type: str = "Bearer"
    expires_in: int = 3600
    refresh_token: Optional[str] = None  # May return new refresh token
    scope: Optional[str] = None




def _clean_url(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value.rstrip('/') if value else None


def _extract_host_root_and_tenant(*candidates: str | None, explicit_tenant_name: str | None = None) -> tuple[str | None, str | None]:
    tenant_name = explicit_tenant_name.strip() if explicit_tenant_name else None
    host_root = None
    for candidate in candidates:
        candidate = _clean_url(candidate)
        if not candidate:
            continue
        parsed = urlparse(candidate)
        host_root = f"{parsed.scheme}://{parsed.netloc}"
        parts = [p for p in parsed.path.split('/') if p]
        if not tenant_name and len(parts) >= 4 and parts[0] == 'ccx' and parts[1] == 'oauth2' and parts[-1] == 'token':
            tenant_name = parts[2]
        if not tenant_name and len(parts) >= 4 and parts[0] == 'ccx' and parts[1] == 'api' and parts[2] == 'v1':
            tenant_name = parts[3]
        if not tenant_name and len(parts) >= 3 and parts[0] == 'ccx' and parts[1] == 'api':
            tenant_name = parts[2]
    return host_root, tenant_name


def _replace_path_suffix(url: str, old_suffix: str, new_suffix: str) -> str:
    if url.endswith(old_suffix):
        return url[:-len(old_suffix)] + new_suffix
    return url


class Workday:
    """
    Workday OAuth 2.0 Client
    
    Workday uses OAuth 2.0 for:
    - REST APIs
    - Custom Objects
    - Workday Query Language (WQL)
    
    Scopes are called "Functional Areas" in Workday:
    - System
    - Integration  
    - Staffing
    - Personal Data
    - Contact Information
    - Organization & Roles
    - Workday Query Language
    - Custom Objects > System
    - Custom Objects > Integration
    """
    
    @staticmethod
    def _extract_host_root_and_tenant(*candidates: str | None, explicit_tenant_name: str | None = None) -> tuple[str | None, str | None]:
        return _extract_host_root_and_tenant(*candidates, explicit_tenant_name=explicit_tenant_name)

    @staticmethod
    def get_authorize_url(
        tenant_url: str,
        authorization_url: str | None = None,
        token_url: str | None = None,
        rest_base_url: str | None = None,
        tenant_name: str | None = None,
    ) -> str:
        """Get Workday authorization URL, preferring explicit values when provided."""
        if authorization_url:
            return _clean_url(authorization_url)
        if token_url:
            return _replace_path_suffix(_clean_url(token_url), '/token', '/authorize')
        host_root, inferred_tenant = _extract_host_root_and_tenant(rest_base_url, tenant_url, explicit_tenant_name=tenant_name)
        if host_root and inferred_tenant:
            return f"{host_root}/ccx/oauth2/{inferred_tenant}/authorize"
        tenant_url = _clean_url(tenant_url) or tenant_url
        return f"{tenant_url}/oauth2/authorize"
    
    @staticmethod
    def get_token_url(
        tenant_url: str,
        token_url: str | None = None,
        rest_base_url: str | None = None,
        tenant_name: str | None = None,
    ) -> str:
        """Get Workday token endpoint URL, preferring explicit values when provided."""
        if token_url:
            return _clean_url(token_url)
        host_root, inferred_tenant = _extract_host_root_and_tenant(rest_base_url, tenant_url, explicit_tenant_name=tenant_name)
        if host_root and inferred_tenant:
            return f"{host_root}/ccx/oauth2/{inferred_tenant}/token"
        tenant_url = _clean_url(tenant_url) or tenant_url
        return f"{tenant_url}/oauth2/token"
    
    @staticmethod
    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        reraise=True
    )
    async def access_token(
        ctx: Context, rq: WorkdayAccessTokenRq
    ) -> WorkdayAccessToken | Exception:
        """
        Exchange authorization code for access token.
        
        Workday returns:
        - access_token: Short-lived (1 hour)
        - refresh_token: Long-lived or non-expiring (recommended)
        """
        token_url = Workday.get_token_url(rq.tenant_url, token_url=rq.token_url)
        
        payload = {
            "grant_type": rq.grant_type,
            "client_id": rq.client_id,
            "client_secret": rq.client_secret,
            "code": rq.code,
            "redirect_uri": rq.redirect_uri,
        }
        
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
        }
        
        async with httpx.AsyncClient() as client:
            res = await client.post(token_url, data=payload, headers=headers)
            if res.status_code == 200:
                token_data = res.json()
                return WorkdayAccessToken(**token_data)
            else:
                return HTTPException(status_code=res.status_code, detail=res.text)
    
    @staticmethod
    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        reraise=True
    )
    async def refresh_access_token(
        ctx: Context, rq: WorkdayRefreshTokenRq
    ) -> WorkdayRefreshedAccessToken | Exception:
        """
        Refresh an expired access token using refresh token.
        
        Workday can be configured to return non-expiring refresh tokens
        for long-running integrations.
        """
        token_url = Workday.get_token_url(rq.tenant_url, token_url=rq.token_url)
        
        payload = {
            "grant_type": rq.grant_type,
            "client_id": rq.client_id,
            "client_secret": rq.client_secret,
            "refresh_token": rq.refresh_token,
        }
        
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
        }
        
        async with httpx.AsyncClient() as client:
            res = await client.post(token_url, data=payload, headers=headers)
            if res.status_code == 200:
                token_data = res.json()
                return WorkdayRefreshedAccessToken(**token_data)
            else:
                return HTTPException(status_code=res.status_code, detail=res.text)
