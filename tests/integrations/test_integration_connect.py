"""One call connects an app, whichever kind it is.

A credential app turns its creds into auth here and comes back connected. An OAuth app
has nothing to turn — the user has to visit the provider — so it comes back with a consent
URL instead. These are the two outcomes `connect` promises, and the boundary between them
is a property of the app, not of the caller.
"""

import urllib.parse
from types import SimpleNamespace

import pytest

from src.autobots.api.v1.api_auth.oauth_state import OAuthState
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import (
    IntegrationConnectResolved,
    IntegrationConnectResult,
    IntegrationCreate,
    IntegrationDoc,
)
from src.autobots.integrations.integration_type.custom_auth import CustomAuth, CustomAuthModel
from src.autobots.integrations.integration_type.github_auth import GitHubAuth
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC
from src.autobots.user.user_orm_model import UserORM

CALLBACK = "https://api.example.com/v1/auth/github/oauth-callback"
LANDING = "https://app.example.com/connections"
USER_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def signing_key():
    """`new_oauth_state` signs with JWT_SECRET_KEY; pin it so the state is decodable."""
    from src.autobots import SettingsProvider

    settings = SettingsProvider.sget()
    previous = settings.JWT_SECRET_KEY
    settings.JWT_SECRET_KEY = "test-signing-key"
    yield
    settings.JWT_SECRET_KEY = previous


def _doc(name: str = "my-api", app_name: AppTypes = AppTypes.custom) -> IntegrationDoc:
    return IntegrationDoc(
        id="000000000000000000000000",
        user_id=USER_ID,
        app_name=app_name,
        name=name,
        auth={"api_key": "sk-1"},
    )


def _req(**over) -> IntegrationConnectResolved:
    fields = {"app_name": AppTypes.github, "name": "my-github"}
    fields.update(over)
    return IntegrationConnectResolved(**fields)


# ------------------------------------------------------------------- OAuth apps


@pytest.mark.asyncio
async def test_an_oauth_app_answers_with_a_url_and_stores_nothing(signing_key):
    ctx = Context(user_id=USER_ID)

    result = await GitHubAuth.connect(
        ctx=ctx,
        db=None,  # untouched: nothing is persisted on this path
        user=UserORM(id=USER_ID),
        req=_req(callback_url=CALLBACK, post_auth_uri=LANDING),
    )

    assert result.integration is None
    assert result.redirect_url.startswith("https://github.com/login/oauth/authorize?")


@pytest.mark.asyncio
async def test_the_url_sends_github_our_callback_and_hides_the_destination(signing_key):
    ctx = Context(user_id=USER_ID)

    url = GitHubAuth.build_authorize_url(
        ctx=ctx, name="my-github", callback_url=CALLBACK, post_auth_uri=LANDING
    )

    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    # What GitHub matches against its registration is ours, not the app's landing page.
    assert query["redirect_uri"] == [CALLBACK]

    state = OAuthState.from_base64(query["state"][0])
    assert state.user_id == USER_ID
    assert state.redirect_uri == LANDING
    assert state.trace_id == ctx.trace_id
    # The landing page reaches the provider only inside signed state, never as a parameter.
    assert LANDING not in url.replace(query["state"][0], "")


def test_the_route_and_connect_build_the_same_url(signing_key):
    """Two callers, one builder — the reason build_authorize_url exists."""
    ctx = Context(user_id=USER_ID)

    first = GitHubAuth.build_authorize_url(
        ctx=ctx, name="my-github", callback_url=CALLBACK, post_auth_uri=LANDING
    )
    second = GitHubAuth.build_authorize_url(
        ctx=ctx, name="my-github", callback_url=CALLBACK, post_auth_uri=LANDING
    )

    strip_state = lambda u: urllib.parse.urlparse(u)._replace(query="").geturl()  # noqa: E731
    assert strip_state(first) == strip_state(second)
    # The state differs per call (its own uuid), which is why the comparison drops it.
    assert first != second


def test_an_oauth_app_declares_where_its_callback_lives():
    assert GitHubAuth.oauth_callback_path() == "/v1/auth/github/oauth-callback"


def test_a_credential_app_has_no_callback_to_declare():
    """Blank is the answer that lets a caller ask every app the same question."""
    assert CustomAuth.oauth_callback_path() == ""


@pytest.mark.parametrize(
    "cls_path, client_id_key, slug",
    [
        ("google_auth.GoogleAuth", "GOOGLE_CLIENT_ID", "google"),
        ("google_read_only_auth.GoogleReadOnlyAuth", "GOOGLE_READ_ONLY_CLIENT_ID", "google-read-only"),
        ("google_drive_auth.GoogleDriveAuth", "GOOGLE_DRIVE_CLIENT_ID", "google-drive"),
        ("google_gmail_auth.GoogleGmailAuth", "GOOGLE_GMAIL_CLIENT_ID", "google-gmail"),
        ("google_analytics_auth.GoogleAnalyticsAuth", "GOOGLE_ANALYTICS_CLIENT_ID", "google-analytics"),
        ("google_cloud_auth.GoogleCloudAuth", "GOOGLE_CLOUD_CLIENT_ID", "google-cloud"),
        ("youtube_auth.YouTubeAuth", "GOOGLE_YOUTUBE_CLIENT_ID", "youtube"),
    ],
)
def test_each_google_variant_uses_its_own_credentials(signing_key, cls_path, client_id_key, slug):
    """They share one URL builder by inheritance, which is only safe if each still
    answers with its own client, scopes and callback."""
    import importlib

    from src.autobots import SettingsProvider

    module, cls_name = cls_path.split(".")
    cls = getattr(
        importlib.import_module(f"src.autobots.integrations.integration_type.{module}"),
        cls_name,
    )
    settings = SettingsProvider.sget()
    previous = getattr(settings, client_id_key)
    setattr(settings, client_id_key, f"client-for-{slug}")
    try:
        url = cls.build_authorize_url(
            ctx=Context(user_id=USER_ID), name="n", callback_url=CALLBACK, post_auth_uri=LANDING
        )
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert query["client_id"] == [f"client-for-{slug}"]
        assert cls.oauth_callback_path() == f"/v1/auth/{slug}/oauth-callback"
    finally:
        setattr(settings, client_id_key, previous)


# -------------------------------------------------------------- credential apps


@pytest.mark.asyncio
async def test_creds_and_auth_are_written_as_one_record(monkeypatch):
    """The credential half: derive auth from the creds, then store both together.

    The creds never land anywhere on their own — that is the whole point of folding
    them onto the auth record, so the two halves cannot disagree.
    """
    calls = {}
    stored = _doc()

    async def fake_get_by_name(ctx, db, user, name, app_name, decrypt=False):
        return None  # not connected yet

    async def fake_create_auth(ctx, app_creds):
        calls["derived_from"] = app_creds
        return CustomAuthModel(api_key="sk-1")

    async def fake_create(ctx, db, user, integration_create, decrypt=False):
        calls["written"] = integration_create
        return stored

    from src.autobots.integrations import user_integration as user_app_auth

    monkeypatch.setattr(user_app_auth.UserIntegration, "get_by_name", fake_get_by_name)
    monkeypatch.setattr(user_app_auth.UserIntegration, "create", fake_create)
    monkeypatch.setattr(CustomAuth, "create_auth", fake_create_auth)

    result = await IntegrationTypeABC.connect(
        ctx=Context(user_id=USER_ID),
        db=None,
        user=UserORM(id=USER_ID),
        req=_req(app_name=AppTypes.custom, name="my-api", creds={"api_key": "sk-1"}),
    )

    written = calls["written"]
    assert written.creds == {"api_key": "sk-1"}
    assert written.auth  # derived, not echoed
    assert calls["derived_from"].api_key == "sk-1"
    assert result.integration.id == stored.id


@pytest.mark.asyncio
async def test_connecting_again_with_no_creds_refreshes_rather_than_duplicates(monkeypatch):
    """No creds sent means "use what this connection already holds" — not "wipe it"."""
    calls = {"wrote": False}
    stored = _doc()

    async def fake_create(*a, **k):
        calls["wrote"] = True

    async def fake_update(*a, **k):
        calls["wrote"] = True

    async def fake_sync(ctx, db, user, name, app_name, decrypt=False):
        return stored

    from src.autobots.integrations import user_integration as user_app_auth
    from src.autobots.integrations.app_creds import user_app_creds

    monkeypatch.setattr(user_app_creds.UserAppCreds, "create_app_creds", fake_create)
    monkeypatch.setattr(user_app_creds.UserAppCreds, "update_app_creds", fake_update)
    monkeypatch.setattr(user_app_auth.UserIntegration, "sync_with_creds", fake_sync)

    result = await IntegrationTypeABC.connect(
        ctx=Context(user_id=USER_ID),
        db=None,
        user=UserORM(id=USER_ID),
        req=_req(app_name=AppTypes.custom, name="my-api"),
    )

    assert calls["wrote"] is False
    assert result.integration.id == stored.id


@pytest.mark.asyncio
async def test_a_connection_that_derives_nothing_is_reported_not_raised(monkeypatch):
    """The endpoint turns an empty result into a 400; the ABC does not throw at it."""

    async def fake_sync(ctx, db, user, name, app_name, decrypt=False):
        return None

    from src.autobots.integrations import user_integration as user_app_auth

    monkeypatch.setattr(user_app_auth.UserIntegration, "sync_with_creds", fake_sync)

    result = await IntegrationTypeABC.connect(
        ctx=Context(user_id=USER_ID),
        db=None,
        user=UserORM(id=USER_ID),
        req=_req(app_name=AppTypes.custom, name="my-api"),
    )

    assert result == IntegrationConnectResult()


# ------------------------------------------------- creds folded onto the record


def test_a_connection_carries_its_creds_on_the_same_record():
    """One document holds both halves, so they cannot drift apart."""
    doc = IntegrationDoc(
        id="000000000000000000000000",
        user_id=USER_ID,
        app_name=AppTypes.custom,
        name="my-api",
        auth={"api_key": "sk-1"},
        creds={"api_key": "sk-1"},
    )

    assert doc.creds == {"api_key": "sk-1"}
    # An OAuth connection has no creds of its own, and must still be constructible.
    assert IntegrationDoc(
        id="000000000000000000000001",
        user_id=USER_ID,
        app_name=AppTypes.github,
        name="my-github",
        auth={"access_token": "t"},
    ).creds == {}


@pytest.mark.asyncio
async def test_reauthenticating_does_not_drop_the_creds_it_was_not_given(monkeypatch):
    """UserIntegration.create deletes before it inserts.

    An OAuth callback re-authenticating a connection sends only `auth`. Without carrying
    the stored creds across, that write would disconnect the credentials the connection
    still needs — Workday and iCIMS cannot refresh without them.
    """
    kept = {"creds": {"tenant_url": "https://acme.example"}}
    seen = {}

    class _Crud:
        def __init__(self, db):
            pass

        async def delete_many(self, doc_find):
            return SimpleNamespace(deleted_count=1)

        async def insert_one(self, create_doc):
            seen["creds"] = create_doc.creds
            return None

    async def fake_get_by_name(ctx, db, user, name, app_name, decrypt=False):
        return IntegrationDoc(
            id="000000000000000000000000",
            user_id=USER_ID,
            app_name=AppTypes.custom,
            name="my-api",
            auth={},
            creds=kept["creds"],
        )

    from src.autobots.integrations import user_integration as user_app_auth

    monkeypatch.setattr(user_app_auth, "IntegrationCRUD", _Crud)
    monkeypatch.setattr(user_app_auth.UserIntegration, "get_by_name", fake_get_by_name)
    monkeypatch.setattr(user_app_auth, "extract_lookup", lambda app_name, auth: {})
    monkeypatch.setattr(user_app_auth, "encrypt_if_needed", lambda d: d)
    monkeypatch.setattr(
        user_app_auth,
        "AppTypeToIntegrationClass",
        SimpleNamespace(
            get=lambda app_name: SimpleNamespace(
                get_auth_model_type=lambda: SimpleNamespace(model_validate=lambda a: a)
            )
        ),
    )

    await user_app_auth.UserIntegration.create(
        ctx=Context(user_id=USER_ID),
        db=None,
        user=UserORM(id=USER_ID),
        integration_create=IntegrationCreate(
            app_name=AppTypes.custom, name="my-api", auth={"access_token": "new"}
        ),
    )

    assert seen["creds"] == kept["creds"]


@pytest.mark.asyncio
async def test_the_credentials_do_not_come_back_in_the_answer(monkeypatch):
    """Connecting stores the secrets. It must not also recite them.

    The record is written with `creds` and `auth`; the result carries neither. Without
    this the API answers a connect call with the very API key it was handed, and that
    answer goes wherever response bodies go.
    """
    stored = _doc()
    stored.creds = {"api_key": "sk-secret"}
    stored.auth = {"api_key": "sk-secret"}

    async def fake_get_by_name(ctx, db, user, name, app_name, decrypt=False):
        return None

    async def fake_create_auth(ctx, app_creds):
        return CustomAuthModel(api_key="sk-secret")

    async def fake_create(ctx, db, user, integration_create, decrypt=False):
        return stored

    from src.autobots.integrations import user_integration as user_app_auth

    monkeypatch.setattr(user_app_auth.UserIntegration, "get_by_name", fake_get_by_name)
    monkeypatch.setattr(user_app_auth.UserIntegration, "create", fake_create)
    monkeypatch.setattr(CustomAuth, "create_auth", fake_create_auth)

    result = await IntegrationTypeABC.connect(
        ctx=Context(user_id=USER_ID),
        db=None,
        user=UserORM(id=USER_ID),
        req=_req(app_name=AppTypes.custom, name="my-api", creds={"api_key": "sk-secret"}),
    )

    assert result.integration.creds == {}
    assert result.integration.auth == {}
    assert "sk-secret" not in result.model_dump_json()
    # the record itself kept them
    assert stored.creds == {"api_key": "sk-secret"}
