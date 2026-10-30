"""The Batch document and its derived progress."""

from __future__ import annotations

import pytest

from src.autobots.batch.batch_doc_model import (
    BatchDoc,
    BatchLiteDoc,
    BatchResponse,
    BatchStatus,
    BatchUpdate,
    roll_up_status,
)


def _lite(total: int = 20) -> BatchLiteDoc:
    return BatchLiteDoc(
        _id="b1",
        user_id="u1",
        runnable_type="action_run",
        runnable_id="a1",
        runnable_name="ai1",
        name="Summarise the filings",
        total=total,
        batch_size=1,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


def test_collection_name():
    assert BatchDoc.__collection__ == "Batches"


def test_document_records_only_what_was_launched():
    """No run ids, no counts - membership lives on the runs and counts are derived."""
    fields = set(BatchDoc.model_fields)
    assert "run_ids" not in fields
    for derived in ("success", "error", "processing", "done", "status", "progress"):
        assert derived not in fields, f"{derived} must not be persisted"


def test_only_name_is_updatable():
    """Everything else is a record of what was launched, so it cannot drift."""
    assert set(BatchUpdate.model_fields) == {"name"}


@pytest.mark.parametrize(
    "counts,expected",
    [
        ({}, BatchStatus.processing),  # written before its runs exist
        ({"processing": 3}, BatchStatus.processing),
        ({"success": 2, "processing": 1}, BatchStatus.processing),
        ({"success": 2, "waiting": 1}, BatchStatus.processing),
        ({"success": 5}, BatchStatus.success),
        ({"error": 5}, BatchStatus.error),
        ({"success": 3, "error": 2}, BatchStatus.partial),
    ],
)
def test_roll_up_status(counts, expected):
    assert roll_up_status(counts) == expected


def test_response_shape_is_flat():
    r = BatchResponse.from_doc(_lite(), {"success": 8, "error": 1, "processing": 11})
    assert r.batch_id == "b1"
    assert (r.success, r.error, r.processing, r.waiting) == (8, 1, 11, 0)
    assert r.done == 9
    assert r.total == 20
    assert r.status == BatchStatus.processing
    assert r.runnable_id == "a1"
    assert r.runnable_type.value == "action_run"


def test_response_with_no_runs_yet():
    """A batch is readable the instant it is written, before any run exists."""
    r = BatchResponse.from_doc(_lite(), {})
    assert (r.success, r.error, r.processing, r.done) == (0, 0, 0, 0)
    assert r.status == BatchStatus.processing
    assert r.total == 20


def test_done_counts_only_terminal_runs():
    r = BatchResponse.from_doc(_lite(), {"success": 4, "error": 2, "processing": 14})
    assert r.done == 6
