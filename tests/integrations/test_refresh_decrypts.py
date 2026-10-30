"""Refreshing has to decrypt what it reads, whatever the caller asked to be handed back.

`decrypt=False` used to mean both "give me the stored blobs" and "read the stored blobs
as-is" — so refresh validated an encryption envelope against the app's creds model and
answered 500: `1 validation error for GoogleCreds, email: Field required, input_value=
{'_enc': True, 'v': 1, ...}`. These tests store ciphertext and read it back the way the
database does, so the envelope reaches model_validate if the decryption is ever dropped.
"""

from types import SimpleNamespace

import pytest

from src.autobots.data_model.context import Context
from src.autobots.integrations import user_integration as user_integration_module
from src.autobots.integrations.app_creds import user_app_creds as user_app_creds_module
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import IntegrationDoc
from src.autobots.integrations.integration_type.custom_auth import CustomAuthModel
from src.autobots.user.user_orm_model import UserORM

USER_ID = "11111111-2222-3333-4444-555555555555"
PLAIN_CREDS = {"api_key": "sk-1", "api_domain": "https://example.test"}
PLAIN_AUTH = {"api_key": "sk-1"}
# what the field actually looks like at rest
SEALED = {"_enc": True, "v": 1, "alg": "AES-GCM", "iv": "XncBS/bk9Lq6m0m/", "ct": "…"}


def _doc(auth):
    return IntegrationDoc.model_validate(
        dict(
            id="000000000000000000000000",
            user_id=USER_ID,
            app_name=AppTypes.custom,
            name="apitest",
            auth=auth,
            creds={},
        )
    )



class _Collection:
    """Named, because the CRUD builds its index names from the collection name."""

    name = "AppAuth"


class _Db:
    """Enough of a database for IntegrationCRUD's constructor, which indexes into it."""

    def __getitem__(self, name):
        return _Collection()


@pytest.fixture
def stored(monkeypatch):
    """Creds and auth that are only readable when the caller asks for decryption."""
    seen = {}

    async def fake_get_app_creds_by_name(ctx, db, user, name, app_name=None, decrypt=False):
        seen["creds_decrypt"] = decrypt
        return SimpleNamespace(app_name=AppTypes.custom, creds=PLAIN_CREDS if decrypt else SEALED)

    async def fake_get_by_name(ctx, db, user, name, app_name=None, decrypt=False):
        seen["auth_decrypt"] = decrypt
        return _doc(PLAIN_AUTH if decrypt else SEALED)

    async def fake_update(ctx, db, user, id, integration_update, decrypt=False):
        seen["returned_decrypt"] = decrypt
        return _doc(integration_update.auth)

    async def fake_refresh_auth(ctx, app_creds, app_auth):
        return CustomAuthModel(api_key="sk-2")

    monkeypatch.setattr(
        user_app_creds_module.UserAppCreds, "get_app_creds_by_name", fake_get_app_creds_by_name
    )
    monkeypatch.setattr(user_integration_module.UserIntegration, "get_by_name", fake_get_by_name)
    monkeypatch.setattr(user_integration_module.UserIntegration, "update", fake_update)

    from src.autobots.integrations.integration_type.custom_auth import CustomAuth

    monkeypatch.setattr(CustomAuth, "refresh_auth", fake_refresh_auth)
    return seen


@pytest.mark.asyncio
async def test_refresh_reads_through_the_encryption_even_when_asked_for_sealed_output(stored):
    """The dev 500, reproduced: the endpoint asks for nothing decrypted, refresh still works."""
    doc = await user_integration_module.UserIntegration.refresh(
        ctx=Context(user_id=USER_ID),
        db=None,
        user=UserORM(id=USER_ID),
        name="apitest",
        app_name=AppTypes.custom,
    )

    assert doc.auth["api_key"] == "sk-2"
    assert stored["creds_decrypt"] is True
    assert stored["auth_decrypt"] is True
    # and what it hands back is still the caller's choice
    assert stored["returned_decrypt"] is False


@pytest.mark.asyncio
async def test_sync_reads_through_the_encryption_too(stored):
    """`connect` with no creds lands here, so it broke the same way."""
    doc = await user_integration_module.UserIntegration.sync_with_creds(
        ctx=Context(user_id=USER_ID),
        db=None,
        user=UserORM(id=USER_ID),
        name="apitest",
        app_name=AppTypes.custom,
    )

    assert doc is not None, "sync swallowed the failure and returned None"
    assert doc.auth["api_key"] == "sk-2"
    assert stored["creds_decrypt"] is True
    assert stored["auth_decrypt"] is True


@pytest.mark.asyncio
async def test_an_update_that_says_nothing_about_creds_leaves_them_alone(monkeypatch):
    """A refresh carries new auth and no creds. It must not blank the creds it kept.

    `update` builds its $set from every non-None field, so a `creds` that defaults to an
    empty dict is indistinguishable from a caller asking for the credentials to be
    emptied — and the connection then has nothing left to refresh itself with next time.
    Invisible while the AppCreds collection is still there to fall back on; permanent the
    day the backfill runs.
    """
    from src.autobots.integrations import integration_crud
    from src.autobots.integrations.integration_model import IntegrationUpdate

    captured = {}

    async def fake_update_one(self, doc_update):
        captured["set"] = {k: v for k, v in doc_update.model_dump().items() if v is not None}
        return _doc(PLAIN_AUTH)

    monkeypatch.setattr(integration_crud.IntegrationCRUD, "update_one", fake_update_one)

    await user_integration_module.UserIntegration.update(
        ctx=Context(user_id=USER_ID),
        db=_Db(),
        user=UserORM(id=USER_ID),
        id="000000000000000000000000",
        integration_update=IntegrationUpdate(
            name="apitest", app_name=AppTypes.custom, auth={"api_key": "sk-2"}
        ),
    )

    assert "creds" not in captured["set"], "refresh would have blanked the stored credentials"


@pytest.mark.asyncio
async def test_an_update_that_does_carry_creds_still_writes_them(monkeypatch):
    """The other direction: reconnecting with new credentials must replace the old ones."""
    from src.autobots.integrations import integration_crud
    from src.autobots.integrations.integration_model import IntegrationUpdate

    captured = {}

    async def fake_update_one(self, doc_update):
        captured["set"] = {k: v for k, v in doc_update.model_dump().items() if v is not None}
        return _doc(PLAIN_AUTH)

    monkeypatch.setattr(integration_crud.IntegrationCRUD, "update_one", fake_update_one)

    await user_integration_module.UserIntegration.update(
        ctx=Context(user_id=USER_ID),
        db=_Db(),
        user=UserORM(id=USER_ID),
        id="000000000000000000000000",
        integration_update=IntegrationUpdate(
            name="apitest",
            app_name=AppTypes.custom,
            auth={"api_key": "sk-2"},
            creds={"api_key": "sk-2"},
        ),
    )

    assert captured["set"]["creds"], "a reconnect with new credentials must store them"
