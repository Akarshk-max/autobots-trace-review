# Runbook: Adding a New App Auth Integration

This guide covers everything an agent (or engineer) needs to add a new third-party app integration to the auth system. Follow the section that matches the auth type of the service you're adding.

---

## Step 0: Determine the Auth Type

Before writing any code, check the service's developer documentation:

| Auth Type | Description | Examples |
|-----------|-------------|---------|
| **Simple API Key** | User provides a static key; no OAuth flow | Bluedot, Firecrawl, Stripe, AssemblyAI |
| **Basic Auth** | Credentials encoded as base64 `username:password` | Avalara, Jira |
| **OAuth 2.0** | 3-legged auth code flow; requires redirect + token exchange | Smartsheet, Fireflies, HubSpot, Google |

> Many services support both OAuth and API key. Prefer OAuth when available — it provides per-user tokens that can be refreshed without user re-entry.

---

## Files to Touch (by auth type)

### Simple API Key
| # | Repo | File | Action |
|---|------|------|--------|
| 1 | `autobots` | `src/autobots/integrations/app_types.py` | Add enum value |
| 2 | `autobots` | `src/autobots/integrations/integration_type/{service}_auth.py` | Create new file |
| 3 | `autobots` | `src/autobots/integrations/app_type_to_integration_class.py` | Add import + mapping |
| 4 | `autobots-web` | `public/logos/{service}_logo.{png\|svg\|webp}` | Add logo file |
| 5 | `autobots-web` | `src/app/dashboard/connections/_components/apps-revamp.tsx` | Add to `imgObject` (+ `modifiedNames` / `appNameList` if needed) |

### Basic Auth
Same as API Key (all 5 rows above), but the auth class encodes credentials into a `basic_token`.

### OAuth 2.0
| # | Repo | File | Action |
|---|------|------|--------|
| 1 | `autobots` | `src/autobots/integrations/app_types.py` | Add enum value |
| 2 | `autobots` | `src/autobots/integrations/integration_type/{service}_auth.py` | Create new file |
| 3 | `autobots` | `src/autobots/conn/only_auth/{service}/{service}_client.py` | Create new file |
| 4 | `autobots` | `src/autobots/api/v1/api_auth/api_{service}_auth.py` | Create new file |
| 5 | `autobots` | `src/autobots/integrations/app_type_to_integration_class.py` | Add import + mapping |
| 6 | `autobots` | `src/autobots/api/v1/v1.py` | Add import + `include_router()` |
| 7 | `autobots` | `src/autobots/core/settings.py` | Add env vars |
| 8 | `autobots-web` | `public/logos/{service}_logo.{png\|svg\|webp}` | Add logo file |
| 9 | `autobots-web` | `src/app/dashboard/connections/_components/apps-revamp.tsx` | Add to `imgObject` (+ `modifiedNames` / `appNameList` if needed) |
| 10 | `autobots-web` | `src/app/api/{service}-callback-app-link/route.ts` | Create frontend OAuth callback route |
| 11 | `autobots-web` | `src/api/app-auth/index.ts` | Add `getMyServiceAuthURL()` using generated client + `window.location.origin` |
| 12 | `autobots-infra` | AWS Secrets Manager + ECS service config | Store OAuth credentials; inject as env vars |

---

## Step 1: Add the Enum Value

**File:** `src/autobots/integrations/app_types.py`

Add at the bottom of the `AppTypes` class (before the closing docstring):

```python
myservice = "myservice"
```

---

## Step 2A: Create Auth Class — Simple API Key

**File:** `src/autobots/integrations/integration_type/myservice_auth.py`

Reference implementation: `firecrawl_auth.py`

```python
from typing import Dict, Optional, Type
from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.integrations.integration_model import ApiKeyAuthModel
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC
from src.autobots.integrations.app_creds.app_creds_model import ApiKeyCredsModel
from src.autobots.integrations.app_types import AppTypes
from src.autobots.data_model.context import Context


class MyServiceAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.myservice

    @staticmethod
    async def get_api_domain(app_creds: ApiKeyCredsModel | None) -> str:
        return "https://api.myservice.com"

    @staticmethod
    def get_creds_model_type() -> Type[ApiKeyCredsModel]:
        return ApiKeyCredsModel

    @staticmethod
    def get_auth_model_type() -> Type[ApiKeyAuthModel]:
        return ApiKeyAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: ApiKeyCredsModel) -> ApiKeyAuthModel | Exception:
        return ApiKeyAuthModel(api_key=app_creds.api_key)

    @staticmethod
    async def refresh_auth(ctx: Context, app_creds: ApiKeyCredsModel, app_auth: ApiKeyAuthModel) -> ApiKeyAuthModel | Exception:
        return app_auth if app_auth else ApiKeyAuthModel(api_key=app_creds.api_key)

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: ApiKeyAuthModel) -> bool:
        return False

    @staticmethod
    async def get_auth_header(ctx: Context, app_auth: ApiKeyAuthModel, config: Optional[APIRequest] = None) -> Dict[str, str]:
        return {"Authorization": f"Bearer {app_auth.api_key}"}
        # For custom header (e.g. Bluedot): return {"x-myservice-api-key": app_auth.api_key}
```

**Note:** Use `ApiKeyCredsModel` and `ApiKeyAuthModel` from the shared models unless the service needs a custom header name or extra credential fields.

---

## Step 2B: Create Auth Class — Basic Auth

**File:** `src/autobots/integrations/integration_type/myservice_auth.py`

Reference implementation: `jira_auth.py`, `avalara_auth.py`

```python
import base64
from typing import Dict, Optional, Type
from pydantic import BaseModel
from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC
from src.autobots.integrations.app_types import AppTypes
from src.autobots.data_model.context import Context


class MyServiceCredsModel(BaseModel):
    username: str   # or account_id, client_id, etc.
    password: str   # or license_key, secret, etc.


class MyServiceAuthModel(BaseModel):
    basic_token: str


class MyServiceAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.myservice

    @staticmethod
    async def get_api_domain(app_creds: MyServiceCredsModel | None) -> str:
        return "https://api.myservice.com"

    @staticmethod
    def get_creds_model_type() -> Type[MyServiceCredsModel]:
        return MyServiceCredsModel

    @staticmethod
    def get_auth_model_type() -> Type[MyServiceAuthModel]:
        return MyServiceAuthModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: MyServiceCredsModel) -> MyServiceAuthModel | Exception:
        raw = f"{app_creds.username}:{app_creds.password}".encode("utf-8")
        basic_token = base64.b64encode(raw).decode("utf-8")
        return MyServiceAuthModel(basic_token=basic_token)

    @staticmethod
    async def refresh_auth(ctx: Context, app_creds: MyServiceCredsModel, app_auth: MyServiceAuthModel) -> MyServiceAuthModel | Exception:
        return app_auth if app_auth else await MyServiceAuth.create_auth(ctx=ctx, app_creds=app_creds)

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: MyServiceAuthModel) -> bool:
        return False

    @staticmethod
    async def get_auth_header(ctx: Context, app_auth: MyServiceAuthModel, config: Optional[APIRequest] = None) -> Dict[str, str]:
        return {"Authorization": f"Basic {app_auth.basic_token}"}
```

---

## Step 2C: Create Auth Class — OAuth 2.0

**File:** `src/autobots/integrations/integration_type/myservice_auth.py`

Reference implementation: `hubspot_auth.py`, `smartsheet_auth.py`, `fireflies_auth.py`

```python
import urllib.parse
from typing import Dict, Optional, Type
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel
from src.autobots import SettingsProvider
from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.user.user_orm_model import UserORM
from src.autobots.integrations.integration_model import (
    IntegrationConnectResolved, IntegrationConnectResult, OAuthModel,
)
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC
from src.autobots.api.v1.api_auth.oauth_state import new_oauth_state
from src.autobots.integrations.app_types import AppTypes
from src.autobots.conn.only_auth.myservice.myservice_client import (
    MyService, MyServiceAccessTokenRq, MyServiceRefreshTokenRq,
)
from src.autobots.core.clock.clock import Clock
from src.autobots.data_model.context import Context


class MyServiceCreds(BaseModel):
    pass  # Empty unless the service requires per-user config (e.g. instance_url)


class MyServiceAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.myservice

    @staticmethod
    async def get_api_domain(app_creds: MyServiceCreds | None) -> str:
        return "https://api.myservice.com"

    @staticmethod
    def get_creds_model_type() -> Type[MyServiceCreds]:
        return MyServiceCreds

    @staticmethod
    def get_auth_model_type() -> Type[OAuthModel]:
        return OAuthModel

    # --- the three that make POST /v1/integrations/connect work for this app ---
    # `connect` and `oauth_callback_path` are on the ABC and inherited if you omit
    # them, which is how a credential app works. `build_authorize_url` is not: it is
    # a convention every OAuth class follows, and nothing but `connect` calls it, so
    # a typo in its name fails as "this app returned no consent URL", not as an error.

    @staticmethod
    def oauth_callback_path() -> str:
        """Where the provider sends the browser back. Register exactly this with them."""
        return f"{SettingsProvider.sget().API_v1}{SettingsProvider.sget().API_AUTH}/myservice/oauth-callback"

    @staticmethod
    def build_authorize_url(ctx: Context, name: str, callback_url: str, post_auth_uri: str) -> str:
        """The consent URL, built here so the route and connect() cannot drift apart.

        `callback_url` is ours and is what the provider matches against its registration.
        `post_auth_uri` is where the user lands afterwards; it never reaches the provider,
        it rides inside the signed state. Both are resolved by the caller, which keeps
        this method free of FastAPI — these classes run in workers too.
        """
        state_b64 = new_oauth_state(ctx=ctx, name=name, redirect_uri=post_auth_uri).to_base64()
        return (
            "https://myservice.com/oauth/authorize?"
            f"client_id={SettingsProvider.sget().MYSERVICE_CLIENT_ID}&"
            f"redirect_uri={urllib.parse.quote_plus(callback_url)}&"
            f"scope={SettingsProvider.sget().MYSERVICE_SCOPES}&"
            f"state={urllib.parse.quote_plus(state_b64)}&"
            "response_type=code"
        )

    @staticmethod
    async def connect(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        req: IntegrationConnectResolved,
        decrypt: bool = False,
    ) -> IntegrationConnectResult:
        """No creds to turn into auth — the user has to visit the provider."""
        return IntegrationConnectResult(
            redirect_url=MyServiceAuth.build_authorize_url(
                ctx=ctx,
                name=req.name,
                callback_url=req.callback_url or "",
                post_auth_uri=req.post_auth_uri or "",
            )
        )

    @staticmethod
    async def create_auth(ctx: Context, app_creds: MyServiceAccessTokenRq) -> OAuthModel | Exception:
        try:
            token = await MyService.access_token(ctx=ctx, rq=app_creds)
            if isinstance(token, Exception):
                return token
            return OAuthModel(
                access_token=token.access_token,
                refresh_token=token.refresh_token,
                expires_in=token.expires_in,
                token_type=token.token_type,
            )
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error creating MyService auth: {e}")
            return e

    @staticmethod
    async def refresh_auth(ctx: Context, app_creds: MyServiceCreds, app_auth: OAuthModel) -> OAuthModel | Exception:
        try:
            rq = MyServiceRefreshTokenRq(refresh_token=app_auth.refresh_token)
            token = await MyService.refresh_access_token(ctx=ctx, rq=rq)
            if isinstance(token, Exception):
                return token
            return OAuthModel(
                access_token=token.access_token,
                refresh_token=token.refresh_token,
                expires_in=token.expires_in,
                token_type=token.token_type,
            )
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error refreshing MyService auth: {e}")
            return e

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: OAuthModel) -> bool:
        duration_sec = (Clock.datetime_now() - app_auth.created_at).total_seconds()
        return duration_sec > app_auth.expires_in - 300  # 5-min buffer

    @staticmethod
    async def get_auth_header(ctx: Context, app_auth: OAuthModel, config: Optional[APIRequest] = None) -> Dict[str, str]:
        return {"Authorization": f"Bearer {app_auth.access_token}"}
```

---

## Step 3: Create the OAuth Client (OAuth only)

**File:** `src/autobots/conn/only_auth/myservice/myservice_client.py`

Reference implementation: `hubspot_client.py`, `fireflies_client.py`

```python
from datetime import datetime
from typing import Optional
import httpx
from pydantic import BaseModel, Field
from src.autobots import SettingsProvider
from src.autobots.data_model.context import Context


class MyServiceAccessTokenRq(BaseModel):
    code: str
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    redirect_uri: Optional[str] = None

    def __init__(self, **data):
        super().__init__(**data)
        self.client_id = self.client_id or SettingsProvider.sget().MYSERVICE_CLIENT_ID
        self.client_secret = self.client_secret or SettingsProvider.sget().MYSERVICE_CLIENT_SECRET
        self.redirect_uri = self.redirect_uri or SettingsProvider.sget().MYSERVICE_REDIRECT_URL


class MyServiceAccessToken(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int = 3600
    token_type: str = "Bearer"
    created_at: datetime = Field(default_factory=datetime.now)


class MyServiceRefreshTokenRq(BaseModel):
    refresh_token: str
    client_id: Optional[str] = None
    client_secret: Optional[str] = None

    def __init__(self, **data):
        super().__init__(**data)
        self.client_id = self.client_id or SettingsProvider.sget().MYSERVICE_CLIENT_ID
        self.client_secret = self.client_secret or SettingsProvider.sget().MYSERVICE_CLIENT_SECRET


class MyServiceRefreshedAccessToken(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str = "Bearer"


class MyService:
    TOKEN_URL = "https://api.myservice.com/oauth/token"

    @staticmethod
    async def access_token(ctx: Context, rq: MyServiceAccessTokenRq) -> MyServiceAccessToken | Exception:
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                resp = await client.post(
                    url=MyService.TOKEN_URL,
                    data={
                        "grant_type": "authorization_code",
                        "client_id": rq.client_id,
                        "client_secret": rq.client_secret,
                        "code": rq.code,
                        "redirect_uri": rq.redirect_uri,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                if resp.status_code != 200:
                    return Exception(f"MyService access_token error: {resp.status_code}, {resp.text}")
                return MyServiceAccessToken(**resp.json())
        except Exception as e:
            return e

    @staticmethod
    async def refresh_access_token(ctx: Context, rq: MyServiceRefreshTokenRq) -> MyServiceRefreshedAccessToken | Exception:
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                resp = await client.post(
                    url=MyService.TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "client_id": rq.client_id,
                        "client_secret": rq.client_secret,
                        "refresh_token": rq.refresh_token,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                if resp.status_code != 200:
                    return Exception(f"MyService refresh_token error: {resp.status_code}, {resp.text}")
                return MyServiceRefreshedAccessToken(**resp.json())
        except Exception as e:
            return e
```

**Non-standard token exchanges:**
- **Smartsheet** requires a plain SHA-256 hash: `hashlib.sha256(f"{client_secret}|{code}".encode()).hexdigest()` sent as a `hash` param. See `smartsheet_client.py`.
- **Some services** use HTTP Basic auth on the token endpoint instead of body params. Pass `auth=(client_id, client_secret)` to httpx and omit those from the body.

---

## Step 4: Create OAuth API Routes (OAuth only)

**File:** `src/autobots/api/v1/api_auth/api_myservice_auth.py`

Reference implementation: `api_hubspot_auth.py`, `api_smartsheet_auth.py`

**One endpoint is required:**

`GET /myservice/oauth-callback` — receives the `code` from the provider, exchanges it for
tokens, and stores them via `UserIntegration.create()`. This is the URL you register in
the provider's console, and `oauth_callback_path()` on the auth class must return the
same path.

**`GET /myservice/oauth-url` is optional and deprecated.** `POST /v1/integrations/connect`
supersedes it: it looks the class up by `app_name` and calls the `connect()` you wrote in
Step 2C, which returns the consent URL in the response body. Add the `/oauth-url` route
only if something still calls the per-provider path; new integrations do not need it.
If you do add it, build its URL by calling `build_authorize_url()` rather than assembling
a second copy — the two drifting apart is what that method exists to prevent.

```python
import urllib.parse
import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, HttpUrl
from starlette.requests import Request
from starlette.responses import RedirectResponse

from src.autobots import SettingsProvider
from src.autobots.api.v1.api_auth.oauth_state import OAuthState
from src.autobots.integrations.integration_model import IntegrationCreate, OAuthModel
from src.autobots.integrations.integration_type.myservice_auth import MyServiceAuth
from src.autobots.integrations.user_integration import UserIntegration
from src.autobots.integrations.app_types import AppTypes
from src.autobots.auth.security import get_user_from_access_token, raise_if_redirect_uri_not_allowed
from src.autobots.conn.only_auth.myservice.myservice_client import MyServiceAccessTokenRq
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(prefix=SettingsProvider.sget().API_AUTH, tags=[SettingsProvider.sget().API_AUTH])


class UrlResponse(BaseModel):
    url: HttpUrl


@router.get("/myservice/oauth-url")
async def myservice_oauth_url(
    request: Request,
    redirect_uri: str = SettingsProvider.sget().MYSERVICE_REDIRECT_URL,
    user_res: supabase_auth.UserResponse = Depends(get_user_from_access_token),
) -> UrlResponse:
    _ = await raise_if_redirect_uri_not_allowed(redirect_uri)
    ctx = request.state.context
    logger.bind(ctx=ctx).info(f"MyService OAuth URL requested for user: {user_res.user.id}")
    client_id = SettingsProvider.sget().MYSERVICE_CLIENT_ID
    scopes = SettingsProvider.sget().MYSERVICE_SCOPES  # omit if service has no scope param
    state_b64 = OAuthState(redirect_uri=redirect_uri).to_base64()

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scopes,
        "state": state_b64,
    }
    oauth_url = "https://myservice.com/oauth/authorize?" + urllib.parse.urlencode(params)
    logger.bind(ctx=ctx).info("MyService OAuth URL generated")
    return UrlResponse(url=HttpUrl(oauth_url))


@router.get("/myservice/oauth-callback")
async def myservice_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    user_res: supabase_auth.UserResponse = Depends(get_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    ctx = request.state.context
    logger.bind(ctx=ctx).info(
        f"MyService OAuth callback received: has_code={bool(code)}, has_state={bool(state)}, error={error}"
    )
    if error:
        raise HTTPException(status_code=400, detail=error)
    if not code:
        raise HTTPException(status_code=400, detail="Missing 'code'")

    user = UserORM(**user_res.user.model_dump())

    try:
        redirect_uri = SettingsProvider.sget().MYSERVICE_REDIRECT_URL
        if state:
            try:
                redirect_uri = OAuthState.from_base64(state).redirect_uri
                _ = await raise_if_redirect_uri_not_allowed(redirect_uri)  # re-validate after decoding
            except HTTPException:
                raise
            except Exception as e:
                logger.bind(ctx=ctx).warning(f"Failed to parse state: {e}")

        token_rq = MyServiceAccessTokenRq(
            client_id=SettingsProvider.sget().MYSERVICE_CLIENT_ID,
            client_secret=SettingsProvider.sget().MYSERVICE_CLIENT_SECRET,
            redirect_uri=redirect_uri,
            code=code,
        )
        oauth_rs = await MyServiceAuth.create_auth(ctx=ctx, app_creds=token_rq)
        if isinstance(oauth_rs, Exception):
            raise HTTPException(status_code=400, detail=str(oauth_rs))

        assert isinstance(oauth_rs, OAuthModel)
        integration_create = IntegrationCreate(
            app_name=AppTypes.myservice, auth=oauth_rs.model_dump(exclude_none=True)
        )
        await UserIntegration.create(ctx=ctx, db=db, user=user, integration_create=integration_create)

    except HTTPException:
        raise
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"MyService OAuth callback failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    return RedirectResponse("/", status_code=303)
```

---

## Step 5: Register the Router in v1.py (OAuth only)

**File:** `src/autobots/api/v1/v1.py`

1. Add to the `api_auth` import block:
```python
from src.autobots.api.v1.api_auth import (
    ...
    api_myservice_auth,
    ...
)
```

2. Add `include_router` near the bottom (before the docs router):
```python
router.include_router(api_myservice_auth.router)
```

---

## Step 6: Add Settings (OAuth only)

**File:** `src/autobots/core/settings.py`

Add a section inside the `Settings` class:

```python
# OAuth Configuration - MyService
MYSERVICE_CLIENT_ID: str = ""
MYSERVICE_CLIENT_SECRET: str = ""
MYSERVICE_REDIRECT_URL: str = ""
MYSERVICE_SCOPES: str = ""   # omit if service has no scopes
```

> Simple API key and Basic Auth integrations do NOT need settings entries — credentials are stored per-user in the DB.

---

## Step 7: Register in AppTypeToIntegrationClass

**File:** `src/autobots/integrations/app_type_to_integration_class.py`

Add import:
```python
from src.autobots.integrations.integration_type.myservice_auth import MyServiceAuth
```

Add to the `AppTypeToIntegrationClass` dict:
```python
AppTypes.myservice: MyServiceAuth,
```

---

## Step 8: Frontend — autobots-web (all auth types)

### 8a. Add the logo file

Drop the logo in `autobots-web/public/logos/`. Prefer `.svg` for vector logos; `.png` or `.webp` are fine otherwise.

```
autobots-web/public/logos/myservice_logo.png
```

### 8b. Register in `apps-revamp.tsx`

**File:** `autobots-web/src/app/dashboard/connections/_components/apps-revamp.tsx`

1. **`imgObject`** — map the enum key to the logo path (required for all integrations):
```typescript
myservice: "/logos/myservice_logo.png",
```

2. **`modifiedNames`** (optional) — only needed if the display name requires custom formatting (e.g. contains underscores or needs capitalisation):
```typescript
myservice: "My Service",
```

3. **`appNameList`** — add the enum key here if the integration should appear as a card in the Connections UI:
```typescript
const appNameList = [..., "myservice"];
```

### 8c. Add the frontend callback route (OAuth only)

**File:** `autobots-web/src/app/api/{service}-callback-app-link/route.ts` *(create new file)*

Every OAuth service needs a Next.js API route that receives the provider's redirect, calls the backend to exchange the code, and then redirects the user to `/dashboard/connections`. This is the same pattern used by every existing integration (Asana, HubSpot, Slack, GitHub, etc.).

```typescript
import { BASE_URL } from "@/api/api-constants";
import { ABClient } from "@/generated/client";
import { createClient } from "@/lib/supabase-server";
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const requestUrl = new URL(request.url);
  const code = requestUrl.searchParams.get("code");
  const state = requestUrl.searchParams.get("state");

  const supabaseServer = createClient();

  if (code) {
    try {
      const {
        data: { session },
      } = await supabaseServer.auth.getSession();
      const client = new ABClient({
        BASE: BASE_URL,
        TOKEN: session?.access_token,
      });

      await client.auth.myserviceCallbackV1AuthMyserviceOauthCallbackGet(code, state);
    } catch (error) {
      console.error(error);
    }
  }

  return NextResponse.redirect(`${requestUrl.origin}/dashboard/connections`);
}
```

> **Important:** Always pass both `code` and `state` to the backend callback. The `state` contains the encoded `redirect_uri` which the backend needs to verify and use during token exchange.

### 8d. Add the OAuth URL helper (OAuth only)

**File:** `autobots-web/src/api/app-auth/index.ts`

Use the generated client and pass `window.location.origin` as the `redirect_uri`. This automatically resolves to the correct frontend callback URL in each environment (dev, prod) without hardcoding:

```typescript
export const getMyServiceAuthURL = async () => {
  const { data: { session } } = await supabase.auth.getSession();
  const client = new ABClient({ BASE: BASE_URL, TOKEN: session?.access_token });
  const redirectUri = `${window.location.origin}/api/myservice-callback-app-link`;

  return client.auth.myserviceOauthUrlV1AuthMyserviceOauthUrlGet(redirectUri);
};
```

> **Never hardcode the redirect URI** (e.g. `"https://dev.runkai.ai/..."`) — it will break in the other environment. Always use `window.location.origin`.

Then wire `getMyServiceAuthURL` into `formPopup.tsx` as described in step 8e below.

### 8e. Wire the OAuth redirect in `formPopup.tsx` (OAuth only)

**File:** `autobots-web/src/app/dashboard/connections/_components/formPopup.tsx`

OAuth services must be handled in two places in this file:

**1. Import the URL helper at the top:**
```typescript
import {
  // ...existing imports...
  getMyServiceAuthURL,
} from "@/api/app-auth";
```

**2. Add an `else if` branch inside the `onConnect` function** (before the final `else` fallback that calls `syncApps`):
```typescript
} else if (appName === "myservice") {
  const res = await getMyServiceAuthURL();
  window.location.href = res.url;
}
```

> **Why this matters:** Services in `appNameList` skip the credentials form and go straight to `onConnect`. Without an explicit branch here, the service falls through to the `else` clause which calls `syncApps` — a no-op for OAuth integrations that will silently fail instead of redirecting the user to the provider's consent screen.

---

## Step 9: Infrastructure — autobots-infra (OAuth only)

> **Simple API Key / Basic Auth**: credentials are stored per-user in MongoDB — no infra changes needed unless the integration also requires a global API key in `settings.py`.

### 9a. Store credentials in AWS Secrets Manager

Create or update the secret used by the autobots ECS service. Add the new integration's keys to the JSON payload:

```json
{
  "MYSERVICE_CLIENT_ID": "your-client-id",
  "MYSERVICE_CLIENT_SECRET": "your-client-secret",
  "MYSERVICE_REDIRECT_URL": "https://app.yourdomain.com/api/myservice-callback-app-link",
  "MYSERVICE_SCOPES": "scope1 scope2"
}
```

> **`MYSERVICE_REDIRECT_URL` points to the frontend callback route**, not the backend. The frontend callback (`/api/myservice-callback-app-link`) receives the OAuth redirect from the provider, then calls the backend to exchange the code.
>
> Set this per environment:
> - **dev:** `https://dev.yourdomain.com/api/myservice-callback-app-link`
> - **prod:** `https://app.yourdomain.com/api/myservice-callback-app-link`
>
> This value is also registered as the redirect URI in the OAuth app's developer console. Both must match exactly.

Use the AWS console or CLI:
```bash
aws secretsmanager put-secret-value \
  --secret-id <autobots-secret-arn> \
  --secret-string file://updated-secret.json
```

### 9b. Expose secrets as ECS environment variables

**File:** `autobots-infra/aws-ecs/variables.tf` (or the relevant `.tfvars` / workspace file)

Add the new keys to the `secret_keys` list for the autobots service so Terraform injects them as individual environment variables into the container:

```hcl
services = {
  autobots = {
    # ... existing config ...
    secret_keys = [
      # ... existing keys ...
      "MYSERVICE_CLIENT_ID",
      "MYSERVICE_CLIENT_SECRET",
      "MYSERVICE_REDIRECT_URL",
      "MYSERVICE_SCOPES",
    ]
  }
}
```

After updating, run `terraform plan` then `terraform apply` to redeploy the task definition with the new variables.

---

## Quick Checklist

### Simple API Key / Basic Auth
**autobots**
- [ ] `app_types.py` — new enum value
- [ ] `{service}_auth.py` — new auth type class
- [ ] `app_type_to_integration_class.py` — import + mapping
- [ ] `api_domain_to_app.py` — add `"{api_host}": AppTypes.myservice.value`

**autobots-web**
- [ ] `public/logos/{service}_logo.{ext}` — logo file (download from official site)
- [ ] `apps-revamp.tsx` — add to `imgObject` (+ `modifiedNames` / `appNameList` as needed)

### OAuth 2.0 (all of the above, plus)
**autobots**
- [ ] `conn/only_auth/{service}/__init__.py` — empty file (required by repo convention)
- [ ] `conn/only_auth/{service}/{service}_client.py` — token exchange client
- [ ] `{service}_auth.py` — `oauth_callback_path()`, `build_authorize_url()`, `connect()`
- [ ] `api/v1/api_auth/api_{service}_auth.py` — oauth-callback route (oauth-url optional, deprecated)
- [ ] `v1.py` — import + `include_router()`
- [ ] `settings.py` — `CLIENT_ID`, `CLIENT_SECRET`, `REDIRECT_URL`, (optionally `SCOPES`)

**autobots-web**
- [ ] `public/docs/autobots-api.json` — regenerate; the connect call is `POST /v1/integrations/connect`
- [ ] Run `npx openapi-typescript-codegen ...` to regenerate `src/generated/client`
- [ ] Run `./node_modules/.bin/tsc --noEmit` and fix any **new** errors (see Step 10)
- [ ] `src/app/api/{service}-callback-app-link/route.ts` — frontend callback route (passes `code` + `state` to backend callback)
- [ ] connect via `IntegrationsService.connect({app_name, name, redirect_uri})` and navigate to the
      `redirect_url` it returns — no per-provider URL helper is needed
- [ ] `formPopup.tsx` — import `getMyServiceAuthURL` and add `else if (appName === "myservice")` branch in `onConnect`

**autobots-infra**
- [ ] AWS Secrets Manager — add `CLIENT_ID`, `CLIENT_SECRET`, `REDIRECT_URL`, `SCOPES` to the secret
- [ ] `variables.tf` / workspace — add keys to `secret_keys`; run `terraform apply`

---

## Common Gotchas

- **Smartsheet** — uses a plain **SHA-256** hash of `"{client_secret}|{code}"` as a `hash` body param (not HMAC, not standard OAuth). See `smartsheet_client.py`.
- **Services with `instance_url`** (e.g. Salesforce, Zendesk) — store the domain in `AppCredsModel` and override `get_updated_url()` in the auth class to swap the host per-user. See `salesforce_auth.py`.
- **Services without refresh tokens** — set `refresh_token=None` in `OAuthModel` and make `is_refresh_required()` always return `False`, or re-prompt the user.
- **`OAuthModel.created_at`** is set at token creation time. The `is_refresh_required()` check uses `Clock.datetime_now() - created_at` to determine age. Always subtract a 300s (5 min) buffer from `expires_in`.
- **No `scope` param** — some services (Fireflies) don't require a `scope` in the auth URL. Just omit it.
- **`formPopup.tsx` fallback** — OAuth services in `appNameList` skip the credentials form and jump straight to `onConnect`. If you forget to add the `else if` branch there, the service silently calls `syncApps` instead of redirecting to the OAuth consent screen.
- **HubSpot-style** — some services don't require `response_type=code` in the auth URL. Check the docs.
- **Token endpoint auth** — some services require HTTP Basic auth on the token endpoint (`Authorization: Basic base64(client_id:client_secret)`) instead of body params. Use httpx `auth=` kwarg in that case.
- **Always use `urllib.parse.urlencode()` for OAuth URLs** — never build query strings with f-strings. Scopes commonly contain spaces or special characters that break `HttpUrl` validation and browser redirects if not encoded. Build with `urlencode(params)` and wrap in `HttpUrl()` as normal (repo convention keeps `HttpUrl` in the response model).
- **Never log OAuth codes or full OAuth URLs** — the `code` param is a credential. Log `has_code={bool(code)}` and `has_state={bool(state)}` instead. Don't log the full generated URL as it contains `state` and other sensitive params.
- **Refresh token preservation** — some providers don't return a new refresh token on token refresh (they expect you to keep the original). Always use `refreshed.refresh_token or app_auth.refresh_token` when constructing the new `OAuthModel`. Make the `refresh_token` field on your `RefreshedAccessToken` model `Optional[str] = None`.
- **Missing `__init__.py`** — every new subdirectory under `conn/only_auth/` must have an `__init__.py`. The repo convention uses normal packages, not namespace packages.
- **`api_domain_to_app.py`** — add a `"{api_host}": AppTypes.myservice.value` entry for each new service. This file maps API hostnames to app types for automatic auth resolution in API call actions.
- **OAuth redirect URI points to the frontend, not the backend** — the pattern is: provider → frontend `/api/{service}-callback-app-link` → backend `/v1/auth/{service}/oauth-callback`. `MYSERVICE_REDIRECT_URL` in AWS Secrets Manager must be set to the frontend callback URL (e.g. `https://app.yourdomain.com/api/myservice-callback-app-link`), and must be registered as an allowed redirect URI in the OAuth app's developer console.
- **Never hardcode redirect URIs** — use `window.location.origin` in `getMyServiceAuthURL()` so the correct frontend URL is used automatically per environment. Hardcoding dev or prod URLs will break the other environment.
