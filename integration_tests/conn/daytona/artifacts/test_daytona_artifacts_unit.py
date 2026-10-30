import pytest

from src.autobots.conn.daytona.artifacts import daytona_artifacts as da

pytestmark = pytest.mark.asyncio


class _FakeStorage:
    def __init__(self, exists_keys: set[str]):
        self.exists_keys = exists_keys
        self.exists_calls: list[str] = []

    async def exists(self, key: str) -> bool:
        self.exists_calls.append(key)
        return key in self.exists_keys

    async def get_url(self, key: str) -> str:
        return f"https://example.test/{key}"


async def test_join_s3_key_normalizes_slashes():
    assert da.join_s3_key("a/b///", "///c//d.txt") == "a/b/c/d.txt"
    assert da.join_s3_key("a/b/", "c.txt") == "a/b/c.txt"
    assert da.join_s3_key("a/b", "") == "a/b"
    assert da.join_s3_key("", "x/y.txt") == "x/y.txt"


async def test_reconcile_artifacts_from_s3_adds_only_existing(monkeypatch):
    exists = {"prefix/out.txt", "prefix/sub/a.txt"}
    storage = _FakeStorage(exists_keys=exists)

    monkeypatch.setattr(
        da,
        "get_public_storage",
        lambda object_prefix="": storage,
    )

    merged = await da.reconcile_artifacts_from_s3(
        ctx=None,
        s3_prefix="prefix/",
        output_files=["out.txt", "missing.txt", "sub/a.txt"],
        expected_artifacts={"out.txt": "https://expected/out.txt"},
        artifacts={"already.txt": "https://already"},
        concurrency=4,
    )

    assert merged["already.txt"] == "https://already"
    assert merged["out.txt"] == "https://expected/out.txt"
    assert merged["sub/a.txt"] == "https://example.test/prefix/sub/a.txt"
    assert "missing.txt" not in merged
    assert set(storage.exists_calls) == {
        "prefix/out.txt",
        "prefix/missing.txt",
        "prefix/sub/a.txt",
    }


async def test_build_expected_artifacts(monkeypatch):
    storage = _FakeStorage(exists_keys=set())

    monkeypatch.setattr(
        da,
        "get_public_storage",
        lambda object_prefix="": storage,
    )

    expected = await da.build_expected_artifacts(
        ctx=None,
        s3_prefix="pfx/",
        output_files=["a.txt", "sub/b.txt"],
    )

    assert expected["a.txt"] == "https://example.test/pfx/a.txt"
    assert expected["sub/b.txt"] == "https://example.test/pfx/sub/b.txt"
