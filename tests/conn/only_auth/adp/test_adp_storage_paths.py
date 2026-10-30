import os
import tempfile
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import pytest

from src.autobots.integrations.integration_type import adp_auth
from src.autobots.conn.only_auth.adp import adp_client
from src.autobots.conn.only_auth.adp.adp_client import ADPAccessTokenRq
from src.autobots.data_model.context import Context

pytestmark = pytest.mark.asyncio


async def test_get_cert_paths_preserves_local_paths(tmp_path):
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("certificate")
    key.write_text("key")

    request = ADPAccessTokenRq(
        client_id="client",
        client_secret="secret",
        ssl_cert_path=str(cert),
        ssl_key_path=str(key),
    )

    async with adp_client._get_cert_paths(
        Context(),
        request,
    ) as (cert_path, key_path):
        assert cert_path == str(cert)
        assert key_path == str(key)

    assert cert.exists()
    assert key.exists()


async def test_get_cert_paths_uses_shared_resolver_for_both_files(
    monkeypatch,
):
    calls = []

    @asynccontextmanager
    async def fake_resolver(*, ctx, path_or_url, suffix, max_bytes=None):
        del ctx, max_bytes
        fd, path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        calls.append((path_or_url, suffix, path))
        try:
            yield path
        finally:
            os.unlink(path)

    monkeypatch.setattr(
        adp_client,
        "resolve_local_or_storage_file",
        fake_resolver,
    )

    request = ADPAccessTokenRq(
        client_id="client",
        client_secret="secret",
        ssl_cert_path=("https://account.blob.core.windows.net/private-container/adp/cert.pem"),
        ssl_key_path=("https://account.blob.core.windows.net/private-container/adp/key.pem"),
    )

    resolved_paths = []
    async with adp_client._get_cert_paths(
        Context(),
        request,
    ) as (cert_path, key_path):
        resolved_paths = [cert_path, key_path]
        assert all(os.path.exists(path) for path in resolved_paths)

    assert calls[0][1] == ".pem"
    assert calls[1][1] == ".key"
    assert all(not os.path.exists(path) for path in resolved_paths)


async def test_adp_ssl_context_cleans_resolved_files(monkeypatch):
    captured = {}

    @asynccontextmanager
    async def fake_get_cert_paths(ctx, request):
        del ctx, request
        cert_fd, cert_path = tempfile.mkstemp(suffix=".pem")
        key_fd, key_path = tempfile.mkstemp(suffix=".key")
        os.close(cert_fd)
        os.close(key_fd)
        captured["cert_path"] = cert_path
        captured["key_path"] = key_path
        try:
            yield cert_path, key_path
        finally:
            os.unlink(cert_path)
            os.unlink(key_path)

    ssl_context = MagicMock()

    def load_cert_chain(*, certfile, keyfile):
        assert os.path.exists(certfile)
        assert os.path.exists(keyfile)

    ssl_context.load_cert_chain.side_effect = load_cert_chain

    monkeypatch.setattr(
        adp_auth,
        "_get_cert_paths",
        fake_get_cert_paths,
    )
    monkeypatch.setattr(
        adp_auth.httpx,
        "create_ssl_context",
        lambda: ssl_context,
    )

    creds = adp_auth.ADPCreds(
        client_id="client",
        client_secret="secret",
        ssl_cert_path="remote-cert",
        ssl_key_path="remote-key",
    )

    result = await adp_auth.ADPAuth.get_ssl_context(
        ctx=Context(),
        app_creds=creds,
    )

    assert result is ssl_context
    ssl_context.load_cert_chain.assert_called_once()
    assert not os.path.exists(captured["cert_path"])
    assert not os.path.exists(captured["key_path"])
