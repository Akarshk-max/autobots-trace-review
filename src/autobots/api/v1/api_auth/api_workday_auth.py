import urllib.parse
from datetime import datetime, timezone

import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from jose import jwt as jose_jwt
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, HttpUrl
from starlette.requests import Request
from starlette.responses import RedirectResponse

from src.autobots import SettingsProvider
from src.autobots.api.v1.api_auth.oauth_state import (
    build_backend_callback_url,
    oauth_error_redirect,
    post_auth_redirect_uri,
    resolve_oauth_state,
)
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.conn.only_auth.workday.workday_client import Workday, WorkdayAccessTokenRq
from src.autobots.conn.only_auth.workday.workday_jwt import connect_workday_jwt
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_creds.user_app_creds import UserAppCreds
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import IntegrationCreate, OAuthModel
from src.autobots.integrations.integration_type.workday_auth import WorkdayAuth, WorkdayCreds
from src.autobots.integrations.user_integration import UserIntegration
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=f"{SettingsProvider.sget().API_AUTH}{SettingsProvider.sget().API_PATH_WORKDAY}",
    tags=[SettingsProvider.sget().API_TAG_EXTERNAL_APP_AUTH],
)

_CALLBACK_PATH = (
    f"{SettingsProvider.sget().API_v1}{SettingsProvider.sget().API_AUTH}"
    f"{SettingsProvider.sget().API_PATH_WORKDAY}/oauth-callback"
)


class UrlObj(BaseModel):
    url: HttpUrl


def _workday_callback_url(ctx: Context, request: Request) -> str:
    return build_backend_callback_url(ctx=ctx, request=request, callback_path=_CALLBACK_PATH)


def _post_connect_redirect_url(request) -> str:
    extra_query = {'app': 'workday', 'connected': '1'}
    for candidate in [request.headers.get('referer'), request.headers.get('origin')]:
        if not candidate:
            continue
        parsed = urllib.parse.urlparse(candidate)
        if parsed.scheme and parsed.netloc:
            merged_query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
            merged_query.update(extra_query)
            path = parsed.path or '/'
            return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, '', urllib.parse.urlencode(merged_query), ''))
    return '/?' + urllib.parse.urlencode(extra_query)


class WorkdayDebugRequest(WorkdayCreds):
    pass


def _is_jwt_auth(app_creds: WorkdayCreds) -> bool:
    return (app_creds.auth_type or '').strip() == 'jwt_certificate'


async def _get_saved_workday_creds(
    ctx: Context, db: AsyncIOMotorDatabase, user: UserORM, name: str
) -> WorkdayCreds:
    app_creds_doc = await UserAppCreds.get_app_creds_by_name(
        ctx=ctx, db=db, user=user, name=name, app_name=AppTypes.workday, decrypt=True
    )
    if not app_creds_doc or not app_creds_doc.creds:
        raise HTTPException(
            status_code=400,
            detail='Workday credentials not found. Please configure your Workday credentials in app_creds first.',
        )
    return WorkdayCreds.model_validate(app_creds_doc.creds)


def _workday_diag_fields(payload: dict) -> dict:
    client_id = payload.get('client_id')
    return {
        'workday_auth_type': payload.get('auth_type'),
        'workday_jwt_method': payload.get('jwt_method'),
        'workday_integration_user': payload.get('integration_user'),
        'workday_has_private_key_path': bool(payload.get('private_key_path')),
        'workday_has_public_certificate': bool(payload.get('public_certificate')),
        'workday_has_client_secret': bool(payload.get('client_secret')),
        'workday_client_id_suffix': client_id[-8:] if client_id else None,
        'workday_token_url': payload.get('token_url'),
        'workday_tenant_url': payload.get('tenant_url'),
    }


def _attempt_matrix(debug_result) -> list[dict]:
    return [
        {
            'name': attempt.name,
            'status_code': attempt.status_code,
            'success': attempt.success,
            'method': attempt.method,
            'subject': attempt.subject,
        }
        for attempt in debug_result.attempts
    ]


def _debug_log_fields(debug_result) -> dict:
    return {
        'resolved_token_url': debug_result.resolved_token_url,
        'inferred_tenant': debug_result.inferred_tenant,
        'warnings': debug_result.warnings,
        'suggestions': debug_result.suggestions,
        'attempt_count': len(debug_result.attempts),
        'attempt_matrix': _attempt_matrix(debug_result),
    }

def _base_workday_route_response(classification: str, body_snippet: str, *, app_creds: WorkdayCreds | None = None, app_auth: OAuthModel | None = None, api_domain: str | None = None, error_stage: str | None = None) -> dict:
    rest_base_candidates = WorkdayAuth._build_rest_base_candidates(app_creds) if app_creds else []
    rest_workers_probe_urls = WorkdayAuth._build_rest_workers_probe_urls(app_creds) if app_creds else []
    rest_workers_me_probe_urls = WorkdayAuth._build_rest_workers_me_probe_urls(app_creds) if app_creds else []
    wql_probe_urls = WorkdayAuth._build_wql_probe_urls(app_creds) if app_creds else []
    tenant_name = None
    if app_creds:
        _, tenant_name = Workday._extract_host_root_and_tenant(
            app_creds.rest_base_url,
            app_creds.tenant_url,
            app_creds.token_url,
            explicit_tenant_name=app_creds.tenant_name,
        )
    surfaces = {
        'rest_workers': {
            'surface': 'rest_workers',
            'requested_url': 'https://api.workday.com/ccx/api/v1/workers?limit=1',
            'effective_url': rest_workers_probe_urls[0] if rest_workers_probe_urls else None,
            'http_status': None,
            'classification': classification,
            'body_snippet': body_snippet,
            'attempts': [],
        },
        'rest_workers_me': {
            'surface': 'rest_workers_me',
            'requested_url': 'https://api.workday.com/ccx/api/v1/workers/me',
            'effective_url': rest_workers_me_probe_urls[0] if rest_workers_me_probe_urls else None,
            'http_status': None,
            'classification': classification,
            'body_snippet': body_snippet,
            'attempts': [],
        },
        'wql_data_sources': {
            'surface': 'wql_data_sources',
            'requested_url': None,
            'effective_url': wql_probe_urls[0] if wql_probe_urls else None,
            'http_status': None,
            'classification': classification,
            'body_snippet': body_snippet,
            'attempts': [],
        },
    }
    return {
        'connected': False,
        'has_app_auth': bool(app_auth),
        'business_ready': False,
        'exact_url_requested': surfaces['rest_workers']['effective_url'],
        'http_status': None,
        'body_snippet': body_snippet,
        'classification': classification,
        'error_stage': error_stage,
        'attempts': [],
        'rest_base_candidates': rest_base_candidates,
        'wql_candidate_urls': wql_probe_urls,
        'api_domain': api_domain,
        'tenant_name': tenant_name,
        'recommended_surface': 'rest_workers',
        'recommended_primary_probe': rest_workers_probe_urls[0] if rest_workers_probe_urls else None,
        'recommended_secondary_probe': rest_workers_me_probe_urls[0] if rest_workers_me_probe_urls else None,
        'surfaces': surfaces,
    }


def _connection_info_error_response(classification: str, body_snippet: str, *, app_creds: WorkdayCreds | None = None, app_auth: OAuthModel | None = None, api_domain: str | None = None, error_stage: str | None = None) -> dict:
    rest_base_candidates = WorkdayAuth._build_rest_base_candidates(app_creds) if app_creds else []
    wql_probe_urls = WorkdayAuth._build_wql_probe_urls(app_creds) if app_creds else []
    tenant_name = None
    if app_creds:
        _, tenant_name = Workday._extract_host_root_and_tenant(
            app_creds.rest_base_url,
            app_creds.tenant_url,
            app_creds.token_url,
            explicit_tenant_name=app_creds.tenant_name,
        )
    return {
        'api_domain': api_domain,
        'auth_type': getattr(app_creds, 'auth_type', None),
        'jwt_method': getattr(app_creds, 'jwt_method', None),
        'integration_user': getattr(app_creds, 'integration_user', None),
        'tenant_name': tenant_name,
        'resolved_token_url': getattr(app_creds, 'token_url', None),
        'rest_base_candidates': rest_base_candidates,
        'wql_candidate_urls': wql_probe_urls,
        'has_app_auth': bool(app_auth),
        'classification': classification,
        'supports_health_check': True,
        'recommended_surface': 'rest_workers',
        'recommended_primary_probe': (WorkdayAuth._build_rest_workers_probe_urls(app_creds)[0] if app_creds and WorkdayAuth._build_rest_workers_probe_urls(app_creds) else None),
        'recommended_secondary_probe': (WorkdayAuth._build_rest_workers_me_probe_urls(app_creds)[0] if app_creds and WorkdayAuth._build_rest_workers_me_probe_urls(app_creds) else None),
        'error_stage': error_stage,
        'body_snippet': body_snippet,
    }

def _workday_result_needs_auth_retry(result: dict | None) -> bool:
    if not result:
        return False
    if result.get('classification') == 'token_not_accepted':
        return True
    for surface in (result.get('surfaces') or {}).values():
        if surface.get('classification') == 'token_not_accepted':
            return True
    return False


async def _merge_with_saved_workday_payload(
    ctx: Context,
    db: AsyncIOMotorDatabase,
    user: UserORM,
    name: str,
    incoming_payload: dict,
) -> tuple[dict, WorkdayCreds | None]:
    try:
        saved_creds = await _get_saved_workday_creds(ctx, db, user, name)
    except HTTPException:
        saved_creds = None
    merged_payload = saved_creds.model_dump(exclude_none=True) if saved_creds else {}
    merged_payload.update({key: value for key, value in incoming_payload.items() if value not in (None, '')})
    return merged_payload, saved_creds




def _infer_expires_in_from_access_token(access_token: str | None) -> int | None:
    if not access_token or access_token.count('.') < 2:
        return None
    try:
        claims = jose_jwt.get_unverified_claims(access_token)
    except Exception:
        return None
    exp = claims.get('exp')
    if exp is None:
        return None
    try:
        remaining = int(exp) - int(datetime.now(timezone.utc).timestamp())
    except Exception:
        return None
    return max(remaining, 0)


def _normalize_workday_token_response(token_response: dict) -> tuple[dict, dict]:
    normalized = dict(token_response or {})
    inferred_expires_in = None
    original_has_expires_in = normalized.get('expires_in') not in (None, '')
    if not original_has_expires_in:
        inferred_expires_in = _infer_expires_in_from_access_token(normalized.get('access_token'))
        normalized['expires_in'] = inferred_expires_in if inferred_expires_in is not None else 3600
    meta = {
        'token_response_keys': sorted(normalized.keys()),
        'token_response_had_expires_in': original_has_expires_in,
        'token_response_inferred_expires_in': inferred_expires_in,
        'token_response_final_expires_in': normalized.get('expires_in'),
    }
    return normalized, meta


async def _run_workday_post_connect_probe(
    ctx: Context,
    payload: dict,
    oauth_model: OAuthModel,
) -> dict:
    app_creds = WorkdayCreds.model_validate(payload)
    api_domain = await WorkdayAuth.get_api_domain(app_creds)
    headers = await WorkdayAuth.get_auth_header(ctx=ctx, app_auth=oauth_model)
    result = await WorkdayAuth.health_check(
        ctx=ctx,
        app_creds=app_creds,
        app_auth=oauth_model,
        api_domain=api_domain,
        headers=headers,
    )
    result['surfaces'] = {
        key: value
        for key, value in (result.get('surfaces') or {}).items()
        if key in {'rest_workers', 'rest_workers_me'}
    }
    result['wql_candidate_urls'] = []
    return result


async def _persist_workday_jwt_connect(
    ctx: Context,
    db: AsyncIOMotorDatabase,
    user: UserORM,
    name: str,
    payload: dict,
    route_name: str,
):
    logger.bind(
        ctx=ctx,
        app_name=AppTypes.workday,
        route_name=route_name,
        **_workday_diag_fields(payload),
    ).info('Starting Workday JWT connect flow')

    connect_result = await connect_workday_jwt(ctx, payload)
    if not connect_result.success or not connect_result.token_response:
        logger.bind(
            ctx=ctx,
            app_name=AppTypes.workday,
            route_name=route_name,
            **_workday_diag_fields(payload),
            **_debug_log_fields(connect_result.debug),
        ).warning('Workday JWT connect failed')
        raise HTTPException(
            status_code=400,
            detail={
                'message': 'Workday JWT connection failed.',
                'route_name': route_name,
                'debug': connect_result.debug.model_dump(),
            },
        )

    normalized_token_response, token_meta = _normalize_workday_token_response(connect_result.token_response)
    logger.bind(
        ctx=ctx,
        app_name=AppTypes.workday,
        route_name=route_name,
        **token_meta,
    ).info('Normalized Workday JWT token response')

    oauth_model = OAuthModel.model_validate(normalized_token_response)
    integration_create = IntegrationCreate(
        app_name=AppTypes.workday,
        name=name,
        auth=oauth_model.model_dump(exclude_none=True),
    )
    created_auth = await UserIntegration.create(
        ctx=ctx, db=db, user=user, integration_create=integration_create, decrypt=True
    )
    logger.bind(
        ctx=ctx,
        app_name=AppTypes.workday,
        route_name=route_name,
        auth_id=created_auth.id,
        method=connect_result.method,
        subject=connect_result.subject,
        **_debug_log_fields(connect_result.debug),
    ).info('Workday JWT App Auth Created')

    try:
        post_connect_probe = await _run_workday_post_connect_probe(
            ctx=ctx,
            payload=payload,
            oauth_model=oauth_model,
        )
    except Exception as probe_exc:
        logger.bind(
            ctx=ctx,
            app_name=AppTypes.workday,
            route_name=route_name,
        ).exception('Workday post-connect probe failed')
        post_connect_probe = _base_workday_route_response(
            'backend_error',
            str(probe_exc),
            app_creds=WorkdayCreds.model_validate(payload),
            app_auth=oauth_model,
            api_domain=None,
            error_stage='post_connect_probe',
        )
    else:
        logger.bind(
            ctx=ctx,
            app_name=AppTypes.workday,
            route_name=route_name,
            probe_classification=post_connect_probe.get('classification'),
            probe_http_status=post_connect_probe.get('http_status'),
            probe_surface_classifications={
                k: v.get('classification')
                for k, v in (post_connect_probe.get('surfaces') or {}).items()
            },
        ).info('Completed Workday post-connect probe')

    return {
        'message': 'Workday JWT authentication successful',
        'auth_id': str(created_auth.id),
        'method': connect_result.method,
        'subject': connect_result.subject,
        'route_name': route_name,
        'debug': connect_result.debug.model_dump(),
        'post_connect_probe': post_connect_probe,
    }


@router.post('/jwt-auth')
async def workday_jwt_auth(
    request: Request,
    name: str,
    debug_request: WorkdayDebugRequest,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    """Attempt direct Workday JWT connection from request payload and persist app auth on success."""
    ctx = Context(user_id=str(user_res.user.id))
    user = UserORM(id=user_res.user.id)
    incoming_payload = debug_request.model_dump(exclude_none=True)
    merged_payload, saved_creds = await _merge_with_saved_workday_payload(ctx, db, user, name, incoming_payload)
    logger.bind(
        ctx=ctx,
        app_name=AppTypes.workday,
        route_name='jwt-auth',
        incoming_jwt_method=incoming_payload.get('jwt_method'),
        incoming_integration_user=incoming_payload.get('integration_user'),
        saved_jwt_method=getattr(saved_creds, 'jwt_method', None),
        saved_integration_user=getattr(saved_creds, 'integration_user', None),
        **_workday_diag_fields(merged_payload),
    ).info('Received Workday JWT auth request')

    app_creds = WorkdayCreds.model_validate(merged_payload)
    if not _is_jwt_auth(app_creds):
        raise HTTPException(
            status_code=400,
            detail={
                'message': 'Workday JWT auth endpoint requires auth_type=jwt_certificate.',
                'auth_type': app_creds.auth_type,
            },
        )

    return await _persist_workday_jwt_connect(
        ctx=ctx,
        db=db,
        user=user,
        name=name,
        payload=app_creds.model_dump(exclude_none=True),
        route_name='jwt-auth',
    )


# Engineering/debug-only route kept for future troubleshooting.
# Not exposed as a public API route by default.
# @router.post('/connect-saved')
async def workday_connect_saved(
    request: Request,
    name: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    """Attempt direct Workday JWT connection using saved credentials and persist app auth on success."""
    ctx = Context(user_id=str(user_res.user.id))
    user = UserORM(id=user_res.user.id)
    app_creds = await _get_saved_workday_creds(ctx, db, user, name)

    if not _is_jwt_auth(app_creds):
        raise HTTPException(
            status_code=400,
            detail={
                'message': 'Saved Workday credentials are not configured for JWT/certificate auth.',
                'auth_type': app_creds.auth_type,
                'next_step': 'Use /auth/workday/oauth-url for client_secret OAuth flows.',
            },
        )

    return await _persist_workday_jwt_connect(
        ctx=ctx,
        db=db,
        user=user,
        name=name,
        payload=app_creds.model_dump(exclude_none=True),
        route_name='connect-saved',
    )




@router.post('/connection-info')
async def workday_connection_info(
    request: Request,
    name: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    """Return safe saved Workday connection metadata for deterministic support/debugging."""
    ctx = Context(user_id=str(user_res.user.id))
    user = UserORM(id=user_res.user.id)
    app_creds = None
    app_auth = None
    api_domain = None
    try:
        app_creds = await _get_saved_workday_creds(ctx, db, user, name)
        integration_doc = await UserIntegration.get_by_name(
            ctx=ctx, db=db, user=user, name=name, app_name=AppTypes.workday, decrypt=True
        )
        app_auth = OAuthModel.model_validate(integration_doc.auth) if integration_doc else None
        api_domain = await WorkdayAuth.get_api_domain(app_creds)
        result = await WorkdayAuth.get_connection_info(ctx=ctx, app_creds=app_creds, app_auth=app_auth, api_domain=api_domain)
    except HTTPException as exc:
        logger.bind(ctx=ctx, app_name=AppTypes.workday, route_name='connection-info').warning('Workday connection info unavailable')
        result = _connection_info_error_response('missing_credentials', str(exc.detail), app_creds=app_creds, app_auth=app_auth, api_domain=api_domain, error_stage='load_saved_creds')
    except Exception as exc:
        logger.bind(ctx=ctx, app_name=AppTypes.workday, route_name='connection-info').exception('Workday connection info failed')
        result = _connection_info_error_response('backend_error', str(exc), app_creds=app_creds, app_auth=app_auth, api_domain=api_domain, error_stage='connection_info')
    logger.bind(
        ctx=ctx,
        app_name=AppTypes.workday,
        route_name='connection-info',
        tenant_name=result.get('tenant_name'),
        rest_base_candidates=result.get('rest_base_candidates'),
        recommended_surface=result.get('recommended_surface'),
        recommended_primary_probe=result.get('recommended_primary_probe'),
        has_app_auth=result.get('has_app_auth'),
        classification=result.get('classification'),
        error_stage=result.get('error_stage'),
    ).info('Returned Workday connection info')
    return result


@router.post('/health-saved')
async def workday_health_saved(
    request: Request,
    name: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    """Read-only Workday REST health check using saved creds and saved auth."""
    ctx = Context(user_id=str(user_res.user.id))
    user = UserORM(id=user_res.user.id)
    app_creds = None
    app_auth = None
    api_domain = None
    try:
        app_creds = await _get_saved_workday_creds(ctx, db, user, name)
        integration_doc = await UserIntegration.get_by_name(
            ctx=ctx, db=db, user=user, name=name, app_name=AppTypes.workday, decrypt=True
        )
        app_auth = OAuthModel.model_validate(integration_doc.auth) if integration_doc else None
        api_domain = await WorkdayAuth.get_api_domain(app_creds)
        headers = await UserIntegration.auth_header(
            ctx=ctx, db=db, user=user, name=name, app_name=AppTypes.workday, decrypt=True
        )
        result = await WorkdayAuth.health_check(
            ctx=ctx,
            app_creds=app_creds,
            app_auth=app_auth,
            api_domain=api_domain,
            headers=headers,
        )
        if _workday_result_needs_auth_retry(result):
            logger.bind(ctx=ctx, app_name=AppTypes.workday, route_name='health-saved').info('Retrying Workday health check with freshly synced auth')
            try:
                synced_auth_doc = await UserIntegration.sync_with_creds(
                    ctx=ctx, db=db, user=user, name=name, app_name=AppTypes.workday, decrypt=True
                )
                if synced_auth_doc:
                    app_auth = OAuthModel.model_validate(synced_auth_doc.auth)
                    headers = await WorkdayAuth.get_auth_header(ctx=ctx, app_auth=app_auth)
                    result = await WorkdayAuth.health_check(
                        ctx=ctx,
                        app_creds=app_creds,
                        app_auth=app_auth,
                        api_domain=api_domain,
                        headers=headers,
                    )
                    result['auth_retry_attempted'] = True
                    result['auth_retry_recovered'] = result.get('classification') != 'token_not_accepted'
                else:
                    result['auth_retry_attempted'] = True
                    result['auth_retry_recovered'] = False
                    result['auth_retry_error'] = 'sync_with_creds returned no app auth doc'
            except Exception as retry_exc:
                result['auth_retry_attempted'] = True
                result['auth_retry_recovered'] = False
                result['auth_retry_error'] = str(retry_exc)
    except HTTPException as exc:
        logger.bind(ctx=ctx, app_name=AppTypes.workday, route_name='health-saved').warning('Workday health unavailable')
        result = _base_workday_route_response('missing_credentials', str(exc.detail), app_creds=app_creds, app_auth=app_auth, api_domain=api_domain, error_stage='load_saved_creds')
    except Exception as exc:
        stage = 'health_saved'
        message = str(exc)
        if 'auth_header' in message.lower() or 'authorization' in message.lower():
            stage = 'auth_header'
            classification = 'auth_header_error'
        else:
            classification = 'backend_error'
        logger.bind(ctx=ctx, app_name=AppTypes.workday, route_name='health-saved', error_stage=stage).exception('Workday health check failed')
        result = _base_workday_route_response(classification, message, app_creds=app_creds, app_auth=app_auth, api_domain=api_domain, error_stage=stage)
    logger.bind(
        ctx=ctx,
        app_name=AppTypes.workday,
        route_name='health-saved',
        exact_url_requested=result.get('exact_url_requested'),
        http_status=result.get('http_status'),
        classification=result.get('classification'),
        recommended_surface=result.get('recommended_surface'),
        surface_classifications={k: v.get('classification') for k, v in (result.get('surfaces') or {}).items()},
        error_stage=result.get('error_stage'),
        auth_retry_attempted=result.get('auth_retry_attempted'),
        auth_retry_recovered=result.get('auth_retry_recovered'),
    ).info('Completed Workday health check')
    return result

@router.get('/oauth-url', deprecated=True)
async def workday_oauth_url(
    request: Request,
    name: str,
    # Where to land once the connection is stored; never sent to Workday.
    redirect_uri: str = "",
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> UrlObj:
    """Generate Workday OAuth authorization URL for client-secret/browser OAuth flows."""
    ctx: Context = request.state.context
    ctx.user_id = str(user_res.user.id)
    user = UserORM(id=user_res.user.id)
    try:
        maybe_workday_creds = await _get_saved_workday_creds(ctx, db, user, name)
    except HTTPException:
        maybe_workday_creds = None
    logger.bind(
        ctx=ctx,
        app_name=AppTypes.workday,
        route_name='oauth-url',
        has_saved_workday_creds=bool(maybe_workday_creds),
        saved_auth_type=getattr(maybe_workday_creds, 'auth_type', None),
        saved_jwt_method=getattr(maybe_workday_creds, 'jwt_method', None),
        saved_integration_user=getattr(maybe_workday_creds, 'integration_user', None),
    ).info('Entered Workday oauth-url route')
    if maybe_workday_creds and _is_jwt_auth(maybe_workday_creds):
        logger.bind(
            ctx=ctx,
            app_name=AppTypes.workday,
            route_name='oauth-url',
            saved_jwt_method=getattr(maybe_workday_creds, 'jwt_method', None),
            saved_integration_user=getattr(maybe_workday_creds, 'integration_user', None),
        ).info('Routing Workday oauth-url request through saved JWT compatibility path')
        await workday_connect_saved(request=request, name=name, user_res=user_res, db=db)
        return {'url': _post_connect_redirect_url(request)}
    post_auth_uri = await post_auth_redirect_uri(ctx=ctx, redirect_uri=redirect_uri)

    app_creds = await _get_saved_workday_creds(ctx, db, user, name)

    if _is_jwt_auth(app_creds):
        logger.bind(ctx=ctx, app_name='workday', auth_type=app_creds.auth_type).warning(
            'Workday oauth-url requested for JWT/certificate credentials; browser OAuth is not applicable.'
        )
        raise HTTPException(
            status_code=409,
            detail={
                'message': 'This Workday connection uses JWT/certificate auth and does not require a browser OAuth URL.',
                'auth_type': app_creds.auth_type,
                'next_steps': {
                    'jwt_auth_endpoint': f"{SettingsProvider.sget().API_AUTH}{SettingsProvider.sget().API_PATH_WORKDAY}/jwt-auth",
                    'connection_info_endpoint': f"{SettingsProvider.sget().API_AUTH}{SettingsProvider.sget().API_PATH_WORKDAY}/connection-info",
                    'health_saved_endpoint': f"{SettingsProvider.sget().API_AUTH}{SettingsProvider.sget().API_PATH_WORKDAY}/health-saved",
                },
            },
        )

    if not app_creds.client_id or not app_creds.client_secret or not app_creds.tenant_url:
        raise HTTPException(
            status_code=400,
            detail='Workday client_id, client_secret, and tenant_url are all required.',
        )

    logger.bind(
        ctx=ctx,
        app_name=AppTypes.workday,
        route_name='oauth-url',
        saved_auth_type=app_creds.auth_type,
    ).info('Generating Workday browser OAuth URL')

    # Built by the auth class, so this route and /integrations/connect agree.
    oauth_url = WorkdayAuth.build_authorize_url(
        ctx=ctx,
        name=name,
        callback_url=_workday_callback_url(ctx=ctx, request=request),
        post_auth_uri=post_auth_uri,
        app_creds=app_creds,
    )

    return UrlObj(url=HttpUrl(oauth_url))


@router.get('/oauth-callback')
async def workday_oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    # No bearer token: Workday redirects the browser here; identity comes from state.
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    """
    Handle Workday OAuth callback after user authorization.

    This endpoint receives the authorization code from Workday and exchanges it
    for an access token and refresh token (ideally non-expiring), then stores them.
    """
    ctx: Context = request.state.context
    landing_uri = ""
    try:
        oauth_state = resolve_oauth_state(ctx=ctx, state=state)
        landing_uri = await post_auth_redirect_uri(ctx=ctx, redirect_uri=oauth_state.redirect_uri)
        user = UserORM(id=oauth_state.user_id)
        name = oauth_state.name

        if error:
            raise HTTPException(status_code=400, detail=error)

        if not code:
            raise HTTPException(status_code=400, detail='Authorization code not provided')

        app_creds = await _get_saved_workday_creds(ctx, db, user, name)

        if not app_creds.tenant_url:
            raise HTTPException(
                status_code=400,
                detail='Workday tenant_url is required in credentials.',
            )

        if not app_creds.client_id or not app_creds.client_secret:
            raise HTTPException(
                status_code=400,
                detail='Workday client_id and client_secret are required in credentials.',
            )

        token_rq = WorkdayAccessTokenRq(
            client_id=app_creds.client_id,
            client_secret=app_creds.client_secret,
            code=code,
            redirect_uri=_workday_callback_url(ctx=ctx, request=request),
            tenant_url=app_creds.tenant_url,
            token_url=app_creds.token_url,
        )

        oauth_rs = await WorkdayAuth.create_auth(ctx=ctx, app_creds=token_rq)

        if isinstance(oauth_rs, Exception):
            raise HTTPException(status_code=400, detail=str(oauth_rs))

        assert isinstance(oauth_rs, OAuthModel)

        integration_create = IntegrationCreate(
            app_name=AppTypes.workday, name=name, auth=oauth_rs.model_dump(exclude_none=True)
        )

        created_auth = await UserIntegration.create(
            ctx=ctx, db=db, user=user, integration_create=integration_create, decrypt=True
        )

        logger.bind(ctx=ctx, app_name=AppTypes.workday, id=created_auth.id).info(
            'Workday App Auth Created'
        )
        return RedirectResponse(landing_uri, status_code=303)

    except Exception as e:
        logger.bind(ctx=ctx, app_name='workday').exception(f'Error in Workday oauth callback: {str(e)}')
        if not landing_uri:
            # Nowhere safe to send the browser — the state never yielded a destination.
            raise HTTPException(status_code=400, detail=str(e))
        return oauth_error_redirect(ctx=ctx, redirect_uri=landing_uri, error=e)
