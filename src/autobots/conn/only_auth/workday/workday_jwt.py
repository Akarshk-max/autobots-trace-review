import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from jose import jwt
from pydantic import BaseModel, Field

from src.autobots.conn.object_storage.storage_file_resolver import (
    download_storage_file_to_temp,
    resolve_local_or_storage_file,
)
from src.autobots.data_model.context import Context

CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
JWT_BEARER_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"


class WorkdayDebugAttempt(BaseModel):
    name: str
    endpoint: str
    status_code: int | None = None
    success: bool = False
    method: str | None = None
    subject: str | None = None
    provider_body: str | None = None
    response_json: dict[str, Any] | None = None
    jwt_claims: dict[str, Any] | None = None
    jwt_headers: dict[str, Any] | None = None
    suggestions: list[str] = Field(default_factory=list)


class WorkdayDebugResult(BaseModel):
    resolved_token_url: str | None = None
    inferred_tenant: str | None = None
    warnings: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    attempts: list[WorkdayDebugAttempt] = Field(default_factory=list)


class WorkdayConnectResult(BaseModel):
    success: bool = False
    method: str | None = None
    subject: str | None = None
    token_response: dict[str, Any] | None = None
    debug: WorkdayDebugResult


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value.rstrip('/') if value else None


def _normalize_scope(scope: str | None) -> str | None:
    if not scope:
        return None
    value = scope
    for sep in [',', ';'] + [chr(10), chr(9)]:
        value = value.replace(sep, ' ')
    return ' '.join(value.split()) or None


def _suggestions_for_response(status_code: int | None, provider_body: str | None) -> list[str]:
    body = (provider_body or '').lower()
    suggestions: list[str] = []
    if status_code == 404 or 'not available' in body or 'resource not found' in body:
        suggestions.extend([
            'The Workday token endpoint is probably wrong.',
            'Ask the admin for the exact token endpoint from View API Client for Integrations.',
            'Do not use a login or identity URL as the token endpoint.',
        ])
    if status_code in (401, 403) or 'unauthor' in body or 'invalid_client' in body:
        suggestions.extend([
            'Verify the certificate is attached to the same Workday API client as the client_id.',
            'Verify the JWT audience exactly matches the token URL.',
            'Try both subject variants: client_id and integration_user.',
            'Try both JWT methods: client_assertion and jwt_bearer.',
        ])
    if 'invalid_scope' in body or 'scope' in body:
        suggestions.append('Verify the exact Workday Scope (Functional Areas) configured on the API client.')
    if status_code == 400 and 'invalid request' in body:
        suggestions.append('The endpoint exists, but Workday rejected the request payload; inspect JWT method, claims, scope, and cert binding.')
    return list(dict.fromkeys(suggestions))


def _is_s3_url(path: str) -> bool:
    """Legacy compatibility helper retained for downstream imports."""
    return path.startswith('s3://') or '.s3.' in path


async def _download_s3_to_temp(
    ctx: Context,
    s3_url: str,
    suffix: str = '.pem',
) -> str:
    """Legacy wrapper around provider-neutral object-storage resolution."""
    return await download_storage_file_to_temp(
        ctx=ctx,
        path_or_url=s3_url,
        suffix=suffix,
    )


@asynccontextmanager
async def _resolved_private_key_path(
    ctx: Context,
    private_key_path: str,
):
    """Resolve a Workday signing key for the context duration."""
    async with resolve_local_or_storage_file(
        ctx=ctx,
        path_or_url=private_key_path,
        suffix='.pem',
    ) as resolved_path:
        yield resolved_path


def resolve_token_url(tenant_url: str | None, token_url: str | None) -> tuple[str | None, str | None, list[str]]:
    warnings: list[str] = []
    token_url = _clean(token_url)
    tenant_url = _clean(tenant_url)
    tenant_name = None

    if token_url:
        parsed = urlparse(token_url)
        parts = [p for p in parsed.path.split('/') if p]
        if len(parts) >= 4 and parts[0] == 'ccx' and parts[1] == 'oauth2' and parts[-1] == 'token':
            tenant_name = parts[2]
        return token_url, tenant_name, warnings

    if not tenant_url:
        return None, None, ['Missing tenant_url/token_url']

    parsed = urlparse(tenant_url)
    parts = [p for p in parsed.path.split('/') if p]
    host_root = f"{parsed.scheme}://{parsed.netloc}"

    if '/wday/authgwy/' in parsed.path:
        warnings.append('Provided tenant_url looks like a login URL, not an API base URL.')
        try:
            idx = parts.index('authgwy')
            tenant_name = parts[idx + 1]
            host_root = host_root.replace('-identity.', '-services1.')
            return f"{host_root}/ccx/oauth2/{tenant_name}/token", tenant_name, warnings
        except Exception:
            return None, None, warnings

    if len(parts) >= 3 and parts[0] == 'ccx' and parts[1] == 'api':
        tenant_name = parts[2]
        return f"{host_root}/ccx/oauth2/{tenant_name}/token", tenant_name, warnings

    warnings.append('Could not infer token URL with confidence from tenant_url.')
    return None, None, warnings


def build_signed_jwt(client_id: str, token_url: str, private_key_pem: str, subject: str, key_id: str | None):
    now = datetime.now(timezone.utc)
    claims = {
        'iss': client_id,
        'sub': subject,
        'aud': token_url,
        'iat': int(now.timestamp()),
        'exp': int((now + timedelta(minutes=5)).timestamp()),
        'jti': str(uuid.uuid4()),
    }
    headers = {'kid': key_id} if key_id else {}
    token = jwt.encode(claims, private_key_pem, algorithm='RS256', headers=headers or None)
    return token, claims, headers


async def preflight(url: str) -> WorkdayDebugAttempt:
    try:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            res = await client.get(url)
        body = (res.text or '')[:1000]
        return WorkdayDebugAttempt(
            name='preflight',
            endpoint=url,
            status_code=res.status_code,
            success=res.status_code < 400,
            provider_body=body,
            suggestions=_suggestions_for_response(res.status_code, body),
        )
    except Exception as exc:
        return WorkdayDebugAttempt(
            name='preflight',
            endpoint=url,
            provider_body=str(exc),
            success=False,
            suggestions=['Network or execution error during endpoint preflight.'],
        )


def _build_form(method: str, payload: dict[str, Any], signed_jwt: str, scope: str | None) -> dict[str, Any]:
    if method == 'client_assertion':
        form = {
            'grant_type': 'client_credentials',
            'client_id': payload['client_id'],
            'client_assertion_type': CLIENT_ASSERTION_TYPE,
            'client_assertion': signed_jwt,
        }
        if scope:
            form['scope'] = scope
        return form

    form = {
        'grant_type': JWT_BEARER_GRANT_TYPE,
        'assertion': signed_jwt,
    }
    if scope:
        form['scope'] = scope
    return form


async def debug_workday_connection(ctx: Context, payload: dict[str, Any]) -> WorkdayDebugResult:
    result = WorkdayDebugResult()
    token_url, tenant_name, warnings = resolve_token_url(payload.get('tenant_url'), payload.get('token_url'))
    result.resolved_token_url = token_url
    result.inferred_tenant = tenant_name
    result.warnings.extend(warnings)

    if token_url:
        pf = await preflight(token_url)
        result.attempts.append(pf)
        result.suggestions.extend(pf.suggestions)
    else:
        result.suggestions = list(dict.fromkeys(result.suggestions + result.warnings))
        return result

    if payload.get('auth_type') != 'jwt_certificate':
        result.warnings.append('This debug helper currently focuses on JWT/certificate Workday auth.')
        result.suggestions = list(dict.fromkeys(result.suggestions + result.warnings))
        return result

    private_key_path = payload.get('private_key_path')
    if not private_key_path:
        result.warnings.append('No private_key_path provided; cannot attempt JWT token exchange.')
        result.suggestions = list(dict.fromkeys(result.suggestions + result.warnings))
        return result

    scope = _normalize_scope(payload.get('scopes'))
    subjects = [payload.get('client_id')]
    if payload.get('integration_user') and payload.get('integration_user') not in subjects:
        subjects.append(payload.get('integration_user'))

    methods = ['client_assertion', 'jwt_bearer']
    configured = payload.get('jwt_method')
    if configured in methods:
        methods = [configured] + [m for m in methods if m != configured]

    try:
        async with _resolved_private_key_path(ctx, private_key_path) as key_path:
            private_key_pem = Path(key_path).read_text()
    except Exception as exc:
        result.attempts.append(
            WorkdayDebugAttempt(
                name='private_key_resolution',
                endpoint=token_url,
                success=False,
                provider_body=str(exc),
                suggestions=[
                    'Verify the private key path belongs to configured deployment object storage.'
                ],
            )
        )
        result.suggestions = list(
            dict.fromkeys(
                result.suggestions
                + result.warnings
                + ['Unable to resolve or read the Workday private key.']
            )
        )
        return result

    async with httpx.AsyncClient() as client:
        for method in methods:
            for subject in subjects:
                signed_jwt, claims, headers = build_signed_jwt(
                    payload['client_id'], token_url, private_key_pem, subject, payload.get('key_id')
                )
                form = _build_form(method, payload, signed_jwt, scope)
                try:
                    res = await client.post(
                        token_url,
                        data=form,
                        headers={'Content-Type': 'application/x-www-form-urlencoded'},
                    )
                    body = (res.text or '')[:2000]
                    response_json = None
                    try:
                        response_json = res.json()
                    except Exception:
                        response_json = None
                    attempt = WorkdayDebugAttempt(
                        name='token_exchange',
                        endpoint=token_url,
                        status_code=res.status_code,
                        success=res.status_code == 200,
                        method=method,
                        subject=subject,
                        provider_body=body,
                        response_json=response_json,
                        jwt_claims=claims,
                        jwt_headers=headers,
                        suggestions=_suggestions_for_response(res.status_code, body),
                    )
                    result.attempts.append(attempt)
                    result.suggestions.extend(attempt.suggestions)
                except Exception as exc:
                    attempt = WorkdayDebugAttempt(
                        name='token_exchange',
                        endpoint=token_url,
                        success=False,
                        method=method,
                        subject=subject,
                        provider_body=str(exc),
                        jwt_claims=claims,
                        jwt_headers=headers,
                        suggestions=['Network or execution error during token exchange.'],
                    )
                    result.attempts.append(attempt)
                    result.suggestions.extend(attempt.suggestions)

    result.suggestions = list(dict.fromkeys(result.suggestions + result.warnings))
    return result


async def connect_workday_jwt(ctx: Context, payload: dict[str, Any], try_fallbacks: bool = True) -> WorkdayConnectResult:
    debug = await debug_workday_connection(ctx, payload)
    for attempt in debug.attempts:
        if attempt.name == 'token_exchange' and attempt.success and attempt.response_json:
            return WorkdayConnectResult(
                success=True,
                method=attempt.method,
                subject=attempt.subject,
                token_response=attempt.response_json,
                debug=debug,
            )

    if not try_fallbacks:
        return WorkdayConnectResult(success=False, debug=debug)
    return WorkdayConnectResult(success=False, debug=debug)
