import os
import tempfile
from contextlib import asynccontextmanager

import pytest

from src.autobots.conn.only_auth.workday import workday_jwt
from src.autobots.data_model.context import Context

pytestmark = pytest.mark.asyncio


async def test_resolved_private_key_path_preserves_local_path(tmp_path):
    key = tmp_path / "key.pem"
    key.write_text("private-key")

    async with workday_jwt._resolved_private_key_path(
        Context(),
        str(key),
    ) as resolved:
        assert resolved == str(key)

    assert key.exists()
    assert key.read_text() == "private-key"


async def test_resolved_private_key_path_uses_shared_resolver(
    monkeypatch,
):
    captured = {}

    @asynccontextmanager
    async def fake_resolver(*, ctx, path_or_url, suffix, max_bytes=None):
        del ctx, max_bytes
        fd, path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        captured["source"] = path_or_url
        captured["path"] = path
        try:
            yield path
        finally:
            os.unlink(path)

    monkeypatch.setattr(
        workday_jwt,
        "resolve_local_or_storage_file",
        fake_resolver,
    )

    source = "https://account.blob.core.windows.net/private-container/workday/key.pem"

    async with workday_jwt._resolved_private_key_path(
        Context(),
        source,
    ) as resolved:
        assert os.path.exists(resolved)
        assert captured["source"] == source

    assert not os.path.exists(captured["path"])
