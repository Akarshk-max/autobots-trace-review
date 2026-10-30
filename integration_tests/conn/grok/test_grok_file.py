import asyncio
import os
import tempfile

import pytest

from src.autobots.conn.grok.grok import get_grok_conn
from src.autobots.conn.grok.grok_files.file_models import (
    XAIDeleteError,
    XAIDownloadError,
    XAIGetError,
)
from src.autobots.conn.grok.grok_files.grok_file import XAIFileManager
from src.autobots.core.settings import Settings, SettingsProvider
from src.autobots.user.user_orm_model import UserORM

_SAMPLE_URL = os.getenv(
    "GROK_SAMPLE_PDF_URL",
    "https://ontheline.trincoll.edu/images/bookdown/sample-local-pdf.pdf",
)

_ARTIFACTS: list[dict] = []
_UPLOADED_FILE_IDS: set[str] = set()


def init_loader():
    settings: Settings = SettingsProvider.sget()
    return settings.GROK_API_KEY, settings.GROK_MANAGEMENT_API_KEY


def _track(**row):
    _ARTIFACTS.append(row)


def _remember(file_id: str, **meta):
    if file_id:
        _UPLOADED_FILE_IDS.add(file_id)
    _track(action="remember_file", file_id=file_id, ok=True, **meta)


@pytest.mark.asyncio
async def test_grok_file_upload_url(ctx, db, set_test_settings):
    api_key, _ = init_loader()
    if not api_key:
        pytest.skip("GROK_API_KEY missing")
    user = UserORM(id="8c45fca2-9d1a-4657-b782-674195cc8a99")
    client = get_grok_conn(ctx=ctx, user=user, db=db, api_key=api_key).client

    mgr = XAIFileManager()

    out = await mgr.upload_urls(
        ctx=ctx, user=user, db=db, client=client, urls=[_SAMPLE_URL], batch_size=1
    )
    assert _SAMPLE_URL in out
    meta = out[_SAMPLE_URL]
    assert meta.id
    assert meta.filename
    assert meta.size > 0

    _remember(meta.id, filename=meta.filename, size=meta.size)
    _track(action="upload_urls", file_id=meta.id, filename=meta.filename, size=meta.size, ok=True)


@pytest.mark.asyncio
async def test_grok_file_upload_download_roundtrip(ctx, db, set_test_settings):
    api_key, _ = init_loader()
    if not api_key:
        pytest.skip("GROK_API_KEY missing")
    user = UserORM(id="8c45fca2-9d1a-4657-b782-674195cc8a99")
    client = get_grok_conn(ctx=ctx, user=user, db=db, api_key=api_key).client
    mgr = XAIFileManager()

    payload = b"hello from grok-file tests\n" * 50
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
    src_path = tmp.name
    tmp.write(payload)
    tmp.close()

    try:
        meta = await mgr.upload(
            ctx=ctx, user=user, db=db, client=client, source=src_path, filename="roundtrip.txt"
        )
        assert meta.id
        assert meta.size > 0
        _remember(meta.id, filename=meta.filename, size=meta.size)
        _track(action="upload", file_id=meta.id, filename=meta.filename, size=meta.size, ok=True)

        got = await mgr.download_content(ctx=ctx, user=user, db=db, client=client, file_id=meta.id)
        assert got == payload
        _track(action="download_content", file_id=meta.id, ok=True)

        md = await mgr.get_metadata(ctx=ctx, user=user, db=db, client=client, file_id=meta.id)
        assert md.id == meta.id
        assert md.filename
        _track(action="get_metadata", file_id=meta.id, ok=True)
    finally:
        try:
            os.unlink(src_path)
        except OSError:
            pass


@pytest.mark.asyncio
async def test_grok_file_list_files_basic(ctx, db, set_test_settings):
    api_key, _ = init_loader()
    if not api_key:
        pytest.skip("GROK_API_KEY missing")
    user = UserORM(id="8c45fca2-9d1a-4657-b782-674195cc8a99")
    client = get_grok_conn(ctx=ctx, user=user, db=db, api_key=api_key).client
    mgr = XAIFileManager()

    res = await mgr.list_files(
        ctx=ctx, user=user, db=db, client=client, limit=10, order="desc", sort_by="created_at"
    )
    assert res.files is not None
    assert isinstance(res.files, list)
    _track(action="list_files", count=len(res.files), ok=True)


@pytest.mark.asyncio
async def test_grok_file_list_files_contains_recent_upload(ctx, db, set_test_settings):
    api_key, _ = init_loader()
    if not api_key:
        pytest.skip("GROK_API_KEY missing")
    user = UserORM(id="8c45fca2-9d1a-4657-b782-674195cc8a99")
    client = get_grok_conn(ctx=ctx, user=user, db=db, api_key=api_key).client
    mgr = XAIFileManager()

    payload = b"list visibility\n"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
    src_path = tmp.name
    tmp.write(payload)
    tmp.close()

    try:
        meta = await mgr.upload(
            ctx=ctx, user=user, db=db, client=client, source=src_path, filename="list_check.txt"
        )
        _remember(meta.id, filename=meta.filename, size=meta.size)
        _track(action="upload", file_id=meta.id, filename=meta.filename, size=meta.size, ok=True)

        found = False
        for _ in range(6):
            res = await mgr.list_files(
                ctx=ctx,
                user=user,
                db=db,
                client=client,
                limit=50,
                order="desc",
                sort_by="created_at",
            )
            if any(f.id == meta.id for f in res.files):
                found = True
                break
            await asyncio.sleep(1)

        assert found is True
        _track(action="list_contains", file_id=meta.id, ok=True)
    finally:
        try:
            os.unlink(src_path)
        except OSError:
            pass


@pytest.mark.asyncio
async def test_grok_file_download_to_path(ctx, db, set_test_settings):
    api_key, _ = init_loader()
    if not api_key:
        pytest.skip("GROK_API_KEY missing")
    user = UserORM(id="8c45fca2-9d1a-4657-b782-674195cc8a99")
    client = get_grok_conn(ctx=ctx, user=user, db=db, api_key=api_key).client
    mgr = XAIFileManager()

    payload = os.urandom(1024 * 32)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
    src_path = tmp.name
    tmp.write(payload)
    tmp.close()

    out_path = tempfile.NamedTemporaryFile(delete=False, suffix=".bin").name

    try:
        meta = await mgr.upload(
            ctx=ctx, user=user, db=db, client=client, source=src_path, filename="dl_to_path.bin"
        )
        _remember(meta.id, filename=meta.filename, size=meta.size)
        _track(action="upload", file_id=meta.id, filename=meta.filename, size=meta.size, ok=True)

        await mgr.download_to_path(
            ctx=ctx, user=user, db=db, client=client, file_id=meta.id, path=out_path
        )
        assert open(out_path, "rb").read() == payload
        _track(action="download_to_path", file_id=meta.id, ok=True)
    finally:
        for p in (src_path, out_path):
            try:
                os.unlink(p)
            except OSError:
                pass


@pytest.mark.asyncio
async def test_grok_file_upload_urls_blocks_http_and_reports_callback(ctx, db, set_test_settings):
    api_key, _ = init_loader()
    if not api_key:
        pytest.skip("GROK_API_KEY missing")
    user = UserORM(id="8c45fca2-9d1a-4657-b782-674195cc8a99")
    client = get_grok_conn(ctx=ctx, user=user, db=db, api_key=api_key).client
    mgr = XAIFileManager()

    seen: list[tuple[int, str, object]] = []

    def on_file_complete(i: int, u: str, r: object):
        seen.append((i, u, r))

    out = await mgr.upload_urls(
        ctx=ctx,
        user=user,
        db=db,
        client=client,
        urls=["http://example.com/file.pdf"],
        batch_size=1,
        on_file_complete=on_file_complete,
    )

    assert out == {}
    assert len(seen) == 1
    assert isinstance(seen[0][2], Exception)
    assert isinstance(seen[0][2], XAIDownloadError)
    _track(action="upload_urls_http_blocked", ok=True)


@pytest.mark.asyncio
async def test_grok_file_upload_urls_blocks_unsupported_scheme(ctx, db, set_test_settings):
    api_key, _ = init_loader()
    if not api_key:
        pytest.skip("GROK_API_KEY missing")
    user = UserORM(id="8c45fca2-9d1a-4657-b782-674195cc8a99")
    client = get_grok_conn(ctx=ctx, user=user, db=db, api_key=api_key).client
    mgr = XAIFileManager()

    seen: list[object] = []

    def on_file_complete(i: int, u: str, r: object):
        seen.append(r)

    out = await mgr.upload_urls(
        ctx=ctx,
        user=user,
        db=db,
        client=client,
        urls=["ftp://example.com/a.pdf"],
        batch_size=1,
        on_file_complete=on_file_complete,
    )

    assert out == {}
    assert len(seen) == 1
    assert isinstance(seen[0], XAIDownloadError)
    _track(action="upload_urls_bad_scheme", ok=True)


@pytest.mark.asyncio
async def test_grok_file_get_metadata_invalid_file_id_raises(ctx, db, set_test_settings):
    api_key, _ = init_loader()
    if not api_key:
        pytest.skip("GROK_API_KEY missing")
    user = UserORM(id="8c45fca2-9d1a-4657-b782-674195cc8a99")
    client = get_grok_conn(ctx=ctx, user=user, db=db, api_key=api_key).client
    mgr = XAIFileManager()

    with pytest.raises(XAIGetError):
        await mgr.get_metadata(
            ctx=ctx, user=user, db=db, client=client, file_id="file_DOES_NOT_EXIST_0000000000"
        )
    _track(action="get_metadata_invalid", ok=True)


@pytest.mark.asyncio
async def test_grok_file_download_content_invalid_file_id_raises(ctx, db, set_test_settings):
    api_key, _ = init_loader()
    if not api_key:
        pytest.skip("GROK_API_KEY missing")
    user = UserORM(id="8c45fca2-9d1a-4657-b782-674195cc8a99")
    client = get_grok_conn(ctx=ctx, user=user, db=db, api_key=api_key).client
    mgr = XAIFileManager()

    with pytest.raises(XAIDownloadError):
        await mgr.download_content(
            ctx=ctx, user=user, db=db, client=client, file_id="file_DOES_NOT_EXIST_0000000000"
        )
    _track(action="download_invalid", ok=True)


@pytest.mark.asyncio
async def test_grok_file_delete_invalid_file_id_raises(ctx, db, set_test_settings):
    api_key, _ = init_loader()
    if not api_key:
        pytest.skip("GROK_API_KEY missing")
    user = UserORM(id="8c45fca2-9d1a-4657-b782-674195cc8a99")
    client = get_grok_conn(ctx=ctx, user=user, db=db, api_key=api_key).client
    mgr = XAIFileManager()

    with pytest.raises(XAIDeleteError):
        await mgr.delete(
            ctx=ctx,
            user=user,
            db=db,
            client=client,
            file_id="file_487905c1-e313-4b22-9354-249496c81cab",
        )
        await mgr.delete(
            ctx=ctx,
            user=user,
            db=db,
            client=client,
            file_id="file_7abc45c6-c595-41be-9c36-0302cd2e7168",
        )
        await mgr.delete(
            ctx=ctx,
            user=user,
            db=db,
            client=client,
            file_id="file_7d7f29aa-666f-4b43-a8ff-f9879b6225c0",
        )
    _track(action="delete_invalid", ok=True)


@pytest.mark.asyncio
async def test_delete_all_uploaded_files(ctx, db, set_test_settings):
    api_key, _ = init_loader()
    if not api_key:
        pytest.skip("GROK_API_KEY missing")
    user = UserORM(id="8c45fca2-9d1a-4657-b782-674195cc8a99")
    client = get_grok_conn(ctx=ctx, user=user, db=db, api_key=api_key).client
    mgr = XAIFileManager()

    if not _UPLOADED_FILE_IDS:
        pytest.skip("No uploaded file ids collected")

    failed: list[str] = []
    deleted: list[str] = []

    for fid in sorted(_UPLOADED_FILE_IDS):
        try:
            res = await mgr.delete(ctx=ctx, user=user, db=db, client=client, file_id=fid)
            if bool(res.deleted):
                deleted.append(fid)
                _track(action="delete_all", file_id=fid, ok=True)
            else:
                failed.append(fid)
                _track(action="delete_all", file_id=fid, ok=False, err="not_deleted")
        except Exception as exc:
            failed.append(fid)
            _track(action="delete_all", file_id=fid, ok=False, err=type(exc).__name__)

    for fid in deleted:
        _UPLOADED_FILE_IDS.discard(fid)

    assert not failed, f"Failed to delete: {failed}"
    _track(action="delete_all_done", count=len(deleted), ok=True)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    key = "test_grok_file"
    passed = sum(1 for r in terminalreporter.stats.get("passed", []) if key in r.nodeid)
    failed = sum(1 for r in terminalreporter.stats.get("failed", []) if key in r.nodeid) + sum(
        1 for r in terminalreporter.stats.get("error", []) if key in r.nodeid
    )
    skipped = sum(1 for r in terminalreporter.stats.get("skipped", []) if key in r.nodeid)
    total = passed + failed + skipped
    if total == 0:
        return

    terminalreporter.section("XAIFileManager summary", sep="=")
    terminalreporter.write_line(
        f"Total:  {total} | Passed: {passed} | Failed: {failed} | Skipped: {skipped}"
    )

    if not _ARTIFACTS:
        return

    terminalreporter.write_line("")
    terminalreporter.write_line("Artifacts:")
    headers = ["action", "file_id", "filename", "size", "count", "ok", "err"]
    rows = [[str(a.get(h, "")) for h in headers] for a in _ARTIFACTS[-40:]]

    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(row):
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))

    terminalreporter.write_line(fmt(headers))
    terminalreporter.write_line(fmt(["-" * w for w in widths]))
    for row in rows:
        terminalreporter.write_line(fmt(row))
