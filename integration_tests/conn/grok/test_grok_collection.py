import asyncio
import os
import tempfile

import pytest

from src.autobots.conn.grok.grok import get_grok_conn
from src.autobots.conn.grok.grok_collection.grok_collection import XAICollectionsManager
from src.autobots.core.settings import Settings, SettingsProvider
from src.autobots.user.user_orm_model import UserORM

_COLLECTION_IDS = set()
_UPLOADED_DOCS = dict()
_ARTIFACTS = []


def init_loader():
    settings: Settings = SettingsProvider.sget()
    return settings.GROK_API_KEY, settings.GROK_MANAGEMENT_API_KEY


def record(**row):
    _ARTIFACTS.append(row)


def remember_collection(cid):
    if cid:
        _COLLECTION_IDS.add(cid)


def remember_doc(cid, fid):
    if cid not in _UPLOADED_DOCS:
        _UPLOADED_DOCS[cid] = []
    _UPLOADED_DOCS[cid].append(fid)


@pytest.mark.asyncio
async def test_collection_create(ctx, db, set_test_settings):
    api_key, mapi_key = init_loader()
    if not api_key:
        pytest.skip("missing GROK_API_KEY")
    user = UserORM(id="000-test-user")
    client = get_grok_conn(
        ctx=ctx, user=user, db=db, api_key=api_key, management_key=mapi_key
    ).client
    mgr = XAICollectionsManager()

    meta = await mgr.create(ctx=ctx, user=user, db=db, client=client, name="pytest_collection")
    assert meta.collection_id
    assert meta.collection_name == "pytest_collection"
    remember_collection(meta.collection_id)
    record(action="create", collection_id=meta.collection_id, ok=True)


@pytest.mark.asyncio
async def test_collection_list_and_visible(ctx, db, set_test_settings):
    api_key, mapi_key = init_loader()
    if not api_key:
        pytest.skip("missing GROK_API_KEY")
    user = UserORM(id="000-test-user")
    client = get_grok_conn(
        ctx=ctx, user=user, db=db, api_key=api_key, management_key=mapi_key
    ).client
    mgr = XAICollectionsManager()

    res = await mgr.list_collections(ctx=ctx, user=user, db=db, client=client, limit=20)
    assert isinstance(res.collections, list)
    found = any(c.collection_id in _COLLECTION_IDS for c in res.collections)
    assert found
    record(action="list", count=len(res.collections), ok=True)


@pytest.mark.asyncio
async def test_collection_get(ctx, db, set_test_settings):
    api_key, mapi_key = init_loader()
    if not api_key or not _COLLECTION_IDS:
        pytest.skip("setup incomplete")
    user = UserORM(id="000-test-user")
    client = get_grok_conn(
        ctx=ctx, user=user, db=db, api_key=api_key, management_key=mapi_key
    ).client
    mgr = XAICollectionsManager()

    cid = next(iter(_COLLECTION_IDS))
    meta = await mgr.get_collection(ctx=ctx, user=user, db=db, client=client, collection_id=cid)
    assert meta.collection_id == cid
    record(action="get", collection_id=cid, ok=True)


@pytest.mark.asyncio
async def test_collection_update(ctx, db, set_test_settings):
    api_key, mapi_key = init_loader()
    if not api_key or not _COLLECTION_IDS:
        pytest.skip("setup incomplete")
    user = UserORM(id="000-test-user")
    client = get_grok_conn(
        ctx=ctx, user=user, db=db, api_key=api_key, management_key=mapi_key
    ).client
    mgr = XAICollectionsManager()

    cid = next(iter(_COLLECTION_IDS))
    new_name = "pytest_collection_renamed"
    meta = await mgr.update_collection(
        ctx=ctx, user=user, db=db, client=client, collection_id=cid, name=new_name
    )
    assert meta.collection_name == new_name
    record(action="update", collection_id=cid, ok=True)


@pytest.mark.asyncio
async def test_document_upload(ctx, db, set_test_settings):
    api_key, mapi_key = init_loader()
    if not api_key or not _COLLECTION_IDS:
        pytest.skip("setup incomplete")
    user = UserORM(id="000-test-user")
    client = get_grok_conn(
        ctx=ctx, user=user, db=db, api_key=api_key, management_key=mapi_key
    ).client
    mgr = XAICollectionsManager()

    cid = next(iter(_COLLECTION_IDS))

    payload = b"pytest grok collection document\n" * 20
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
    tmp.write(payload)
    tmp.close()

    try:
        doc = await mgr.upload_document(
            ctx=ctx,
            user=user,
            db=db,
            client=client,
            collection_id=cid,
            name="test_doc.txt",
            data=payload,
        )
        assert doc.file_metadata.file_id
        remember_doc(cid, doc.file_metadata.file_id)
        record(action="upload_doc", collection_id=cid, file_id=doc.file_metadata.file_id, ok=True)
    finally:
        os.unlink(tmp.name)


@pytest.mark.asyncio
async def test_document_list_contains_upload(ctx, db, set_test_settings):
    api_key, mapi_key = init_loader()
    if not api_key or not _COLLECTION_IDS or not _UPLOADED_DOCS:
        pytest.skip("setup incomplete")
    user = UserORM(id="000-test-user")
    client = get_grok_conn(
        ctx=ctx, user=user, db=db, api_key=api_key, management_key=mapi_key
    ).client
    mgr = XAICollectionsManager()

    cid = next(iter(_COLLECTION_IDS))
    fid = _UPLOADED_DOCS[cid][0]

    for _ in range(5):
        res = await mgr.list_documents(
            ctx=ctx, user=user, db=db, client=client, collection_id=cid, limit=50
        )
        if any(d.file_metadata.file_id == fid for d in res.documents):
            record(action="list_docs", collection_id=cid, ok=True)
            return
        await asyncio.sleep(1)

    raise AssertionError("uploaded document not visible")


@pytest.mark.asyncio
async def test_document_get_and_batch(ctx, db, set_test_settings):
    api_key, mapi_key = init_loader()
    if not api_key or not _COLLECTION_IDS or not _UPLOADED_DOCS:
        pytest.skip("setup incomplete")
    user = UserORM(id="000-test-user")
    client = get_grok_conn(
        ctx=ctx, user=user, db=db, api_key=api_key, management_key=mapi_key
    ).client
    mgr = XAICollectionsManager()

    cid = next(iter(_COLLECTION_IDS))
    fid = _UPLOADED_DOCS[cid][0]

    meta = await mgr.get_document(
        ctx=ctx, user=user, db=db, client=client, collection_id=cid, file_id=fid
    )
    assert meta.file_metadata.file_id == fid

    batch = await mgr.batch_get_documents(
        ctx=ctx, user=user, db=db, client=client, collection_id=cid, file_ids=[fid]
    )
    assert len(batch.documents) == 1
    record(action="get_and_batch", collection_id=cid, file_id=fid, ok=True)


@pytest.mark.asyncio
async def test_document_update_and_reindex(ctx, db, set_test_settings):
    api_key, mapi_key = init_loader()
    if not api_key or not _COLLECTION_IDS or not _UPLOADED_DOCS:
        pytest.skip("setup incomplete")
    user = UserORM(id="000-test-user")
    client = get_grok_conn(
        ctx=ctx, user=user, db=db, api_key=api_key, management_key=mapi_key
    ).client
    mgr = XAICollectionsManager()

    cid = next(iter(_COLLECTION_IDS))
    fid = _UPLOADED_DOCS[cid][0]

    updated = await mgr.update_document(
        ctx=ctx,
        user=user,
        db=db,
        client=client,
        collection_id=cid,
        file_id=fid,
        name="renamed_doc.txt",
    )
    assert "renamed" in updated.file_metadata.name
    await mgr.reindex_document(
        ctx=ctx, user=user, db=db, client=client, collection_id=cid, file_id=fid
    )
    record(action="update_reindex", collection_id=cid, file_id=fid, ok=True)


@pytest.mark.asyncio
async def test_search(ctx, db, set_test_settings):
    api_key, mapi_key = init_loader()
    if not api_key or not _COLLECTION_IDS or not _UPLOADED_DOCS:
        pytest.skip("setup incomplete")
    user = UserORM(id="000-test-user")
    client = get_grok_conn(
        ctx=ctx, user=user, db=db, api_key=api_key, management_key=mapi_key
    ).client
    mgr = XAICollectionsManager()

    cid = next(iter(_COLLECTION_IDS))
    res = await mgr.search(
        ctx=ctx, user=user, db=db, client=client, query="pytest", collection_ids=[cid], limit=5
    )
    assert isinstance(res.matches, list)
    record(action="search", collection_id=cid, ok=True)


@pytest.mark.asyncio
async def test_document_remove(ctx, db, set_test_settings):
    api_key, mapi_key = init_loader()
    if not api_key or not _COLLECTION_IDS or not _UPLOADED_DOCS:
        pytest.skip("setup incomplete")
    user = UserORM(id="000-test-user")
    client = get_grok_conn(
        ctx=ctx, user=user, db=db, api_key=api_key, management_key=mapi_key
    ).client
    mgr = XAICollectionsManager()

    cid = next(iter(_COLLECTION_IDS))
    fid = _UPLOADED_DOCS[cid][0]

    await mgr.remove_document(
        ctx=ctx, user=user, db=db, client=client, collection_id=cid, file_id=fid
    )
    record(action="remove_doc", collection_id=cid, file_id=fid, ok=True)


@pytest.mark.asyncio
async def test_collection_delete_all(ctx, db, set_test_settings):
    api_key, mapi_key = init_loader()
    if not api_key or not _COLLECTION_IDS:
        pytest.skip("nothing to clean")

    user = UserORM(id="000-test-user")
    client = get_grok_conn(
        ctx=ctx, user=user, db=db, api_key=api_key, management_key=mapi_key
    ).client
    mgr = XAICollectionsManager()

    failed = []
    for cid in list(_COLLECTION_IDS):
        try:
            await mgr.delete_collection(ctx=ctx, user=user, db=db, client=client, collection_id=cid)
            record(action="delete_collection", collection_id=cid, ok=True)
            _COLLECTION_IDS.remove(cid)
        except Exception:
            failed.append(cid)

    assert not failed


async def _list_all_documents(
    mgr, ctx, user, db, client, collection_id: str, *, page_size: int = 100
):
    docs = []
    token = None
    while True:
        res = await mgr.list_documents(
            ctx=ctx,
            user=user,
            db=db,
            client=client,
            collection_id=collection_id,
            limit=page_size,
            order="desc",
            sort_by="age",
            pagination_token=token,
        )
        docs.extend(res.documents or [])
        token = res.pagination_token
        if not token:
            break
    return docs


async def _count_docs(mgr, ctx, user, db, client, collection_id: str) -> int:
    docs = await _list_all_documents(mgr, ctx, user, db, client, collection_id)
    return len(
        {d.file_metadata.file_id for d in docs if d and d.file_metadata and d.file_metadata.file_id}
    )


async def _wait_for_doc_count(
    mgr,
    ctx,
    user,
    db,
    client,
    collection_id: str,
    expected: int,
    *,
    tries: int = 8,
    delay: float = 1.0,
):
    for _ in range(tries):
        c = await _count_docs(mgr, ctx, user, db, client, collection_id)
        if c == expected:
            return
        await asyncio.sleep(delay)
    # final assert with actual
    actual = await _count_docs(mgr, ctx, user, db, client, collection_id)
    assert actual == expected, f"doc count mismatch: expected={expected} actual={actual}"


@pytest.mark.asyncio
async def test_url_upload_no_duplicate(ctx, db, set_test_settings):
    api_key, mapi_key = init_loader()
    if not api_key:
        pytest.skip("missing GROK_API_KEY")

    collection_id = "dummy-collection-id"
    url = "https://storage.dtelab.com.ar/uploads/2023/02/short-stories-for-children-ingles-primaria-continuemos-estudiando.pdf"
    if not collection_id or not url:
        pytest.skip("set GROK_TEST_COLLECTION_ID and GROK_TEST_FILE_URL to run this test")

    user = UserORM(id="000-test-user")
    client = get_grok_conn(
        ctx=ctx, user=user, db=db, api_key=api_key, management_key=mapi_key
    ).client
    mgr = XAICollectionsManager()

    before = await _count_docs(mgr, ctx, user, db, client, collection_id)

    r1 = await mgr.upload_urls_to_collection(
        ctx=ctx,
        user=user,
        db=db,
        client=client,
        collection_id=collection_id,
        urls=[url],
        batch_size=1,
        timeout_sec=120.0,
        max_bytes=10 * 1024 * 1024,
    )

    after_first = await _count_docs(mgr, ctx, user, db, client, collection_id)
    if after_first < before:
        after_first = before
    assert after_first in (before, before + 1)

    r2 = await mgr.upload_urls_to_collection(
        ctx=ctx,
        user=user,
        db=db,
        client=client,
        collection_id=collection_id,
        urls=[url],
        batch_size=1,
        timeout_sec=120.0,
        max_bytes=10 * 1024 * 1024,
    )

    for _ in range(8):
        after_second = await _count_docs(mgr, ctx, user, db, client, collection_id)
        if after_second == after_first:
            break
        await asyncio.sleep(1.0)
    after_second = await _count_docs(mgr, ctx, user, db, client, collection_id)

    assert after_second == after_first, (
        f"duplicate uploaded: before={before}, after_first={after_first}, after_second={after_second}"
    )

    doc1 = r1.get(url)
    doc2 = r2.get(url)
    if doc1 and doc2:
        assert doc1.file_metadata.file_id == doc2.file_metadata.file_id

    record(
        action="url_upload_no_duplicate",
        collection_id=collection_id,
        file_id=(doc1.file_metadata.file_id if doc1 else ""),
        ok=True,
    )


@pytest.mark.asyncio
async def test_remove_all_documents_from_collection(ctx, db, set_test_settings):
    api_key, mapi_key = init_loader()
    if not api_key:
        pytest.skip("missing GROK_API_KEY")

    collection_id = "dummy-collection-id"
    if not collection_id:
        pytest.skip("set GROK_PURGE_COLLECTION_ID to run this test (THIS DELETES ALL DOCS)")

    user = UserORM(id="000-test-user")
    client = get_grok_conn(
        ctx=ctx, user=user, db=db, api_key=api_key, management_key=mapi_key
    ).client
    mgr = XAICollectionsManager()

    docs = await _list_all_documents(mgr, ctx, user, db, client, collection_id)
    file_ids = [
        d.file_metadata.file_id for d in docs if d and d.file_metadata and d.file_metadata.file_id
    ]

    # remove sequentially, you can make it concurrent if needed
    for fid in file_ids:
        await mgr.remove_document(
            ctx=ctx, user=user, db=db, client=client, collection_id=collection_id, file_id=fid
        )

    # verify empty
    for _ in range(10):
        remaining = await _count_docs(mgr, ctx, user, db, client, collection_id)
        if remaining == 0:
            break
        await asyncio.sleep(1.0)

    remaining = await _count_docs(mgr, ctx, user, db, client, collection_id)
    assert remaining == 0, f"failed to purge all docs, remaining={remaining}"

    record(action="purge_all_docs", collection_id=collection_id, file_id="", ok=True)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    key = "test_grok_collection"
    passed = sum(1 for r in terminalreporter.stats.get("passed", []) if key in r.nodeid)
    failed = sum(1 for r in terminalreporter.stats.get("failed", []) if key in r.nodeid) + sum(
        1 for r in terminalreporter.stats.get("error", []) if key in r.nodeid
    )
    skipped = sum(1 for r in terminalreporter.stats.get("skipped", []) if key in r.nodeid)
    total = passed + failed + skipped
    if total == 0:
        return

    terminalreporter.section("XAICollectionsManager summary", sep="=")
    terminalreporter.write_line(
        f"Total {total} | Passed {passed} | Failed {failed} | Skipped {skipped}"
    )

    if _ARTIFACTS:
        headers = ["action", "collection_id", "file_id", "ok"]
        rows = [[str(a.get(h, "")) for h in headers] for a in _ARTIFACTS[-40:]]
        widths = [max(len(row[i]) for row in [headers] + rows) for i in range(len(headers))]

        def fmt(row):
            return "  ".join(row[i].ljust(widths[i]) for i in range(len(headers)))

        terminalreporter.write_line("")
        terminalreporter.write_line(fmt(headers))
        terminalreporter.write_line(fmt(["-" * w for w in widths]))
        for row in rows:
            terminalreporter.write_line(fmt(row))
