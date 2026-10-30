from datetime import datetime
from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field

from src.autobots.core.clock.clock import Clock
from src.autobots.core.database.mongo_base_crud import DocFindPage
from src.autobots.data_model.created_at import CreatedAt
from src.autobots.data_model.id import ID
from src.autobots.data_model.MongoId import MongoID
from src.autobots.data_model.space_id import OptionalSpaceId
from src.autobots.data_model.updated_at import UpdatedAt
from src.autobots.data_model.user_id import UserId
from src.autobots.integrations.app_types import AppTypes


class OAuthModel(BaseModel):
    access_token: str
    refresh_token: str | None = None
    expires_in: int
    scope: str | None = None
    token_type: str | None = None
    created_at: datetime = Field(default_factory=lambda: Clock.datetime_now())
    code: str | None = None
    refresh_token_expires_in: int | None = None


class ApiKeyAuthModel(BaseModel):
    api_key: str


class GrokApiKeyAuthModel(BaseModel):
    api_key: str
    management_api_key: str = ""


class IntegrationCreate(BaseModel):
    app_name: str | AppTypes
    name: str
    auth: Dict[str, Any]
    default: bool = True
    # The credentials this connection was derived from, kept on the same record so the
    # two halves cannot disagree. Encrypted at rest, like `auth`. Empty for an OAuth
    # connection, which has no credentials of its own.
    creds: Dict[str, Any] = Field(default_factory=dict)


class IntegrationDocCreate(UserId, OptionalSpaceId, IntegrationCreate, CreatedAt, UpdatedAt):
    lookup: Dict[str, Any] = Field(default_factory=dict)


class IntegrationRead(ID):
    id: str | None = None
    app_name: AppTypes | None = None
    name: str | None = None
    default: bool | None = None


class IntegrationDocRead(UserId, OptionalSpaceId, IntegrationRead):
    pass


class IntegrationUpdate(IntegrationCreate):
    default: bool | None = None
    # None, not {}. An update is a partial: the fields it carries are $set and the rest
    # are left alone. Inheriting the empty-dict default made every update that did not
    # mention creds — a token refresh, a sync — $set creds to {} and blank the stored
    # credentials, which the connection needs to refresh itself next time.
    creds: Dict[str, Any] | None = None


class IntegrationDocUpdate(ID, UserId, IntegrationUpdate):
    lookup: Dict[str, Any] | None = None


class IntegrationDeleteDoc(IntegrationDocRead):
    pass


class IntegrationLiteDoc(MongoID, UserId, OptionalSpaceId, CreatedAt, UpdatedAt):
    app_name: AppTypes
    name: str | None = None
    default: bool | None = None
    model_config = ConfigDict(populate_by_name=True)


class IntegrationLiteDocPage(DocFindPage):
    docs: List[IntegrationLiteDoc]


class IntegrationDoc(MongoID, IntegrationDocCreate):
    __collection__ = "AppAuth"

    model_config = ConfigDict(populate_by_name=True)


class IntegrationConnectRequest(BaseModel):
    """What to connect, and what to connect it with.

    `creds` is optional on purpose: an OAuth app has none to give — the user has to visit
    the provider — and a credential app that is already connected can be re-derived from
    what it already holds.
    """

    app_name: str | AppTypes
    name: str
    creds: Dict[str, Any] | None = None
    default: bool = True
    # Where the user lands once the connection is stored. OAuth apps only, and never sent
    # to the provider: it travels inside signed state (see api/v1/api_auth/oauth_state.py).
    redirect_uri: str | None = None


class IntegrationConnectResolved(IntegrationConnectRequest):
    """The request plus the two URLs only the API layer can work out.

    Both are derived from the incoming Request — the backend's own callback, which is
    what the provider matches against its registration, and where to send the user once
    the connection is stored. A caller cannot supply either: the first is ours, and the
    second is signed into the state rather than trusted from a body.

    They live on their own type, not on the request, so they stay out of the published
    schema. A field a caller can fill in but that is overwritten before it is read is a
    field that documents a lie.
    """

    callback_url: str | None = None
    post_auth_uri: str | None = None


class IntegrationConnectResult(BaseModel):
    """The outcome of one connect call. Exactly one field is set.

    `integration` — the connection is live, nothing further to do. `redirect_url` — send
    the user to the provider; its callback writes the auth when they come back.

    The record that comes back has `auth` and `creds` emptied: the caller sent the
    credentials, so it has nothing to learn from being sent them again, and a response
    body is a poor place to keep a secret.
    """

    integration: IntegrationDoc | None = None
    redirect_url: str | None = None
