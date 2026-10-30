import glob
import os
import tempfile

import pytest

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_type.action_text2text.action_text2text_pdf_parser import (
    ActionPDFParser,
    PDFParserReq,
)
from src.autobots.core.cancellation.run_cancellation import RunCancellation, RunCancelledError
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.llm.tools.action_csv_perquet import ActionCSVToParquet, CSVToParquetReq


def _cancelled_app_state() -> AppState:
    app_state = AppState()
    cancellation = RunCancellation(run_id="run-1")
    cancellation.cancel("stop")
    app_state.set_cancellation(cancellation)
    return app_state


def _live_app_state() -> tuple[AppState, RunCancellation]:
    app_state = AppState()
    cancellation = RunCancellation(run_id="run-1")
    app_state.set_cancellation(cancellation)
    return app_state, cancellation


def _temp_files(suffix: str) -> set[str]:
    return set(glob.glob(os.path.join(tempfile.gettempdir(), f"*{suffix}")))


@pytest.mark.asyncio
async def test_pdf_parser_removes_downloaded_file_when_cancelled(monkeypatch):
    pdf_path = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf").name

    async def fake_download(url, ctx):
        return pdf_path

    monkeypatch.setattr(ActionPDFParser, "download_pdf", staticmethod(fake_download))

    action = ActionPDFParser(action_config=PDFParserReq(pdf_url="http://x/a.pdf"), user=None, db=None)

    # RunCancelledError is a BaseException, so the action's `except Exception:
    # return e` convention cannot absorb it into a normal-looking result.
    with pytest.raises(RunCancelledError):
        await action.run_action(
            ctx=Context(),
            app_state=_cancelled_app_state(),
            action_input=DataBlock(urls=["http://x/a.pdf"]),
        )

    assert not os.path.exists(pdf_path)


@pytest.mark.asyncio
async def test_pdf_parser_removes_downloaded_file_on_success(monkeypatch):
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 100), "hello")
    pdf_path = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf").name
    doc.save(pdf_path)
    doc.close()

    async def fake_download(url, ctx):
        return pdf_path

    monkeypatch.setattr(ActionPDFParser, "download_pdf", staticmethod(fake_download))

    action = ActionPDFParser(action_config=PDFParserReq(pdf_url="http://x/a.pdf"), user=None, db=None)
    result = await action.run_action(
        ctx=Context(),
        app_state=_live_app_state()[0],
        action_input=DataBlock(urls=["http://x/a.pdf"]),
    )

    assert "hello" in result.blocks[0].text
    assert not os.path.exists(pdf_path)


@pytest.mark.asyncio
async def test_csv_to_parquet_removes_temp_file_when_cancelled_during_upload(monkeypatch):
    app_state, cancellation = _live_app_state()
    captured: list[str] = []

    async def fake_download(url, ctx):
        return b"x,y\n1,2\n"

    async def fake_upload(parquet_path, ctx, user):
        captured.append(parquet_path)
        cancellation.cancel("stop")
        raise RunCancelledError("stop")

    monkeypatch.setattr(ActionCSVToParquet, "download_csv", staticmethod(fake_download))
    monkeypatch.setattr(ActionCSVToParquet, "upload_to_s3", staticmethod(fake_upload))

    action = ActionCSVToParquet(
        action_config=CSVToParquetReq(csv_urls=["http://x/a.csv"]), user=None, db=None
    )

    with pytest.raises(RunCancelledError):
        await action.run_action(
            ctx=Context(),
            app_state=app_state,
            action_input=DataBlock(urls=["http://x/a.csv"]),
        )

    assert captured, "upload_to_s3 should have been reached"
    assert not os.path.exists(captured[0])


@pytest.mark.asyncio
async def test_csv_to_parquet_stops_batch_and_leaves_no_temp_files(monkeypatch):
    """A cancel part-way through a batch must not convert the remaining URLs."""
    app_state, cancellation = _live_app_state()
    before = _temp_files(".parquet")
    uploaded: list[str] = []

    async def fake_download(url, ctx):
        return b"x,y\n1,2\n"

    async def fake_upload(parquet_path, ctx, user):
        uploaded.append(parquet_path)
        cancellation.cancel("stop")

        class _Meta:
            url = "https://s3/out.parquet"

        return _Meta()

    monkeypatch.setattr(ActionCSVToParquet, "download_csv", staticmethod(fake_download))
    monkeypatch.setattr(ActionCSVToParquet, "upload_to_s3", staticmethod(fake_upload))

    action = ActionCSVToParquet(
        action_config=CSVToParquetReq(csv_urls=[]), user=None, db=None
    )

    with pytest.raises(RunCancelledError):
        await action.run_action(
            ctx=Context(),
            app_state=app_state,
            action_input=DataBlock(urls=["http://x/a.csv", "http://x/b.csv", "http://x/c.csv"]),
        )

    assert len(uploaded) == 1, "the batch must stop at the first URL, not run all three"
    assert _temp_files(".parquet") == before
