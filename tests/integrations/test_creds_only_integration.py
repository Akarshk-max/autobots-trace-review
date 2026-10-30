"""A connection can hold credentials and no auth yet, and must still work.

Folding gives every connection one document, so a credentials-only connection — one whose
auth has never been minted — is now a document with `auth: {}` rather than no document at
all. Every app's auth model rejects `{}`, so anything that validates it without checking
first turns a working connection into an empty header.
"""

from types import SimpleNamespace

import pytest

from src.autobots.data_model.context import Context
from src.autobots.integrations import user_integration as user_integration_module
from src.autobots.integrations.app_creds import user_app_creds as user_app_creds_module
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import IntegrationDoc
from src.autobots.integrations.integration_type.custom_auth import CustomAuth, CustomAuthModel
from src.autobots.user.user_orm_model import UserORM

USER_ID = "11111111-2222-3333-4444-555555555555"
CREDS = {"api_key": "sk-1", "api_domain": "https://example.test"}


def _creds_only_doc():
    return IntegrationDoc.model_validate(
        dict(
            id="0" * 24,
            user_id=USER_ID,
            app_name=AppTypes.custom,
            name="apitest",
            auth={},          # never minted
            creds=CREDS,
        )
    )


@pytest.mark.asyncio
async def test_a_connection_with_creds_and_no_auth_still_produces_a_header(monkeypatch):
    written = {}

    async def fake_get_creds(ctx, db, user, name, app_name=None, decrypt=False):
        return SimpleNamespace(app_name=AppTypes.custom, creds=CREDS, name="apitest")

    async def fake_get_by_name(ctx, db, user, name, app_name=None, decrypt=False):
        return _creds_only_doc()

    async def fake_create(ctx, db, user, integration_create, decrypt=False):
        written.update(integration_create.model_dump())
        return IntegrationDoc.model_validate(
            dict(id="0" * 24, user_id=USER_ID, app_name=AppTypes.custom, name="apitest",
                 auth=integration_create.auth, creds=CREDS)
        )

    async def fake_create_auth(ctx, app_creds):
        return CustomAuthModel(api_key="sk-1")

    monkeypatch.setattr(
        user_app_creds_module.UserAppCreds, "get_app_creds_by_name", fake_get_creds
    )
    monkeypatch.setattr(user_integration_module.UserIntegration, "get_by_name", fake_get_by_name)
    monkeypatch.setattr(user_integration_module.UserIntegration, "create", fake_create)
    monkeypatch.setattr(CustomAuth, "create_auth", fake_create_auth)

    header = await user_integration_module.UserIntegration.auth_header(
        ctx=Context(user_id=USER_ID),
        db=None,
        user=UserORM(id=USER_ID),
        name="apitest",
        app_name=AppTypes.custom,
        decrypt=True,
    )

    assert header, "a connection holding credentials must still yield an auth header"
    # and it does not stay half-built: the auth it just minted is written back, so the
    # record is a whole connection from here on
    assert written["auth"], "the minted auth must be stored, not recomputed on every call"
    assert written["auth"]["api_key"] == "sk-1"


class _Result:
    def __init__(self, matched):
        self.matched_count = matched


class _Collection:
    """Records what the mirror does instead of doing it."""

    name = "AppAuth"

    def __init__(self, matched):
        self.matched = matched
        self.updates = []
        self.inserts = []

    async def update_one(self, identity, update):
        self.updates.append((identity, update))
        return _Result(self.matched)

    async def insert_one(self, doc):
        self.inserts.append(doc)
        return None


class _Db:
    def __init__(self, collection):
        self.collection = collection

    def __getitem__(self, name):
        return self.collection


@pytest.mark.asyncio
async def test_saving_creds_writes_them_onto_the_connection_too():
    """Reads take the folded copy first, so the old collection alone is not enough."""
    from src.autobots.integrations.app_creds.user_app_creds import UserAppCreds

    collection = _Collection(matched=1)
    await UserAppCreds._mirror_onto_integration(
        ctx=Context(user_id=USER_ID), db=_Db(collection), user_id=USER_ID, space_id=None,
        app_name=AppTypes.custom, name="apitest", creds={"_enc": True, "ct": "…"},
    )

    assert collection.inserts == [], "a connection that exists must be updated, not duplicated"
    identity, update = collection.updates[0]
    assert identity == {"user_id": USER_ID, "space_id": None, "app_name": "custom", "name": "apitest"}
    assert update["$set"]["creds"] == {"_enc": True, "ct": "…"}


@pytest.mark.asyncio
async def test_creds_for_a_connection_that_has_no_record_yet_create_one():
    """Either half present means the connection exists — with the absent half as {}."""
    from src.autobots.integrations.app_creds.user_app_creds import UserAppCreds

    collection = _Collection(matched=0)
    await UserAppCreds._mirror_onto_integration(
        ctx=Context(user_id=USER_ID), db=_Db(collection), user_id=USER_ID, space_id=None,
        app_name=AppTypes.custom, name="apitest", creds={"_enc": True, "ct": "…"},
    )

    assert len(collection.inserts) == 1
    written = collection.inserts[0]
    assert written["creds"] == {"_enc": True, "ct": "…"}
    assert written["auth"] == {}, "absent auth is an empty dict, not a missing field"
    assert written["name"] == "apitest" and written["app_name"] == "custom"


@pytest.mark.asyncio
async def test_a_broken_mirror_does_not_fail_the_save():
    """The credentials are stored either way; the folded copy is a cache until the old
    collection goes. A user told their credentials failed to save would retype them."""
    from src.autobots.integrations.app_creds.user_app_creds import UserAppCreds

    class _Exploding:
        name = "AppAuth"

        async def update_one(self, *a, **k):
            raise RuntimeError("mongo is having a day")

    await UserAppCreds._mirror_onto_integration(
        ctx=Context(user_id=USER_ID), db=_Db(_Exploding()), user_id=USER_ID, space_id=None,
        app_name=AppTypes.custom, name="apitest", creds={"_enc": True},
    )  # must not raise


class _MutableCollection:
    """A record the clear path can find, change and remove."""

    name = "AppAuth"

    def __init__(self, doc):
        self.doc = doc
        self.updates = []
        self.deleted = []

    async def find_one(self, query):
        return self.doc

    async def update_one(self, query, update):
        self.updates.append(update)
        return _Result(1)

    async def delete_one(self, query):
        self.deleted.append(query)
        return None


@pytest.mark.asyncio
async def test_deleting_creds_takes_them_off_the_connection_too():
    """Otherwise the engine keeps authenticating with credentials the user removed."""
    from src.autobots.integrations.app_creds.user_app_creds import UserAppCreds

    collection = _MutableCollection({"_id": "x", "auth": {"api_key": "sk-1"}, "creds": {"api_key": "sk-1"}})
    await UserAppCreds._clear_mirrored_creds(
        ctx=Context(user_id=USER_ID), db=_Db(collection),
        identities=[{"user_id": USER_ID, "space_id": None, "app_name": "custom", "name": "apitest"}],
    )

    assert collection.updates[0]["$set"]["creds"] == {}
    assert collection.deleted == [], "a connection that still authenticates must survive"


@pytest.mark.asyncio
async def test_a_record_left_with_neither_half_is_removed():
    """Credentials gone and auth never minted leaves nothing that is a connection."""
    from src.autobots.integrations.app_creds.user_app_creds import UserAppCreds

    collection = _MutableCollection({"_id": "x", "auth": {}, "creds": {"api_key": "sk-1"}})
    await UserAppCreds._clear_mirrored_creds(
        ctx=Context(user_id=USER_ID), db=_Db(collection),
        identities=[{"user_id": USER_ID, "space_id": None, "app_name": "custom", "name": "apitest"}],
    )

    assert collection.deleted, "an empty record is not a connection"
    assert collection.updates == []
