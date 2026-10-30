"""Part 2's other half: fewer round trips must not mean different documents.

`save_run_doc` takes the caller's document as authoritative; `update_action_graph_result` fetches
the stored one and merges a patch onto it. Both end in `_write_run_doc`, so the bytes can only
diverge through what happens *before* that - which is what these tests pin.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest

from src.autobots.action_graph.action_graph.action_graph import ActionGraph
from src.autobots.action_graph.action_graph_result.action_graph_result_model_doc import (
    ActionGraphResultUpdate,
)
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_model import EventResultStatus
from tests.graphs.persistence_probe import (
    a_graph_doc,
    a_probed_service,
    a_run_doc,
    an_action_result,
)

CHAIN = {"s": ["a"], "a": ["b"], "b": ["c"]}

# `2026-08-27T13:25:36.702043` and `...702043Z` are the same instant written two ways.
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?")


def without_timestamps(document: str) -> Any:
    """The document with every timestamp blanked, so the comparison is about content."""

    def scrub(value: Any) -> Any:
        if isinstance(value, str):
            return "<ts>" if TIMESTAMP.fullmatch(value) else value
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return value

    return scrub(json.loads(document))


async def a_completed_run() -> tuple[Any, Any, Dict[str, Any]]:
    graph_doc = a_graph_doc(CHAIN)
    run_doc = a_run_doc(graph_doc)
    service, probe = a_probed_service(run_doc)

    async def fake_run_action(ctx, app_state, user_actions, action_id, input_dict, notify=None):
        return an_action_result(action_id)

    with patch.object(ActionGraph, "run_action", AsyncMock(side_effect=fake_run_action)):
        final = await ActionGraph.run_action_graph_v2(
            ctx=Context(),
            app_state=AppState(),
            user_actions=AsyncMock(),
            action_graph_input_dict={"text": "in"},
            action_graph_result_doc=run_doc,
            action_graph_node_id=None,
            is_continue_run_graph=False,
            user_action_graph_result=service,
            notify=None,
        )
    return service, probe, final


@pytest.mark.asyncio
async def test_the_two_write_paths_agree_on_content():
    """The fetching path and the authoritative path must produce the same document.

    `update_action_graph_result` is run *after* the engine finished, patching nothing that changes
    content. Whatever it writes is the document the old engine would have left behind, because the
    old engine reached S3 through exactly this method.
    """
    service, probe, final = await a_completed_run()
    written_directly = probe.stored_document("run-1")

    await service.update_action_graph_result(
        Context(), "run-1", ActionGraphResultUpdate(result=final.result)
    )
    written_via_fetch = probe.stored_document("run-1")

    assert without_timestamps(written_directly) == without_timestamps(written_via_fetch)


@pytest.mark.asyncio
async def test_the_run_document_holds_every_node_and_no_context_vector():
    """The content assertion behind the byte comparison, stated in terms a reader can check."""
    _, probe, final = await a_completed_run()
    stored = json.loads(probe.stored_document("run-1"))

    assert set(stored["result"]["output"]) == {"s", "a", "b", "c"}
    # A DAG now stores a context vector too - one slot per node. It used to be `None` here, and
    # the byte-identity property that pinned it was given up deliberately: the acyclic and cyclic
    # paths are one code path now. What still has to hold is that the vector covers exactly the
    # nodes `output` does, which `assert_vector_covers_output` guards at run time.
    assert set(stored["result"]["node_vectors"]) == set(stored["result"]["output"])
    for node, vector in stored["result"]["node_vectors"].items():
        assert list(vector["slots"]) == ["0"], f"{node} carries a dimension a DAG cannot have"
    assert stored["status"] == EventResultStatus.success.value
    assert final.result.output.keys() == stored["result"]["output"].keys()


@pytest.mark.asyncio
async def test_the_engine_never_rebinds_the_document_it_was_given():
    """§2.4's root cause: `action_response` aliased a doc the write path kept replacing.

    `save_run_doc` returns None, so there is nothing to rebind and the object the caller passed in
    is the object that gets written.
    """
    graph_doc = a_graph_doc(CHAIN)
    run_doc = a_run_doc(graph_doc)
    service, _ = a_probed_service(run_doc)

    async def fake_run_action(ctx, app_state, user_actions, action_id, input_dict, notify=None):
        return an_action_result(action_id)

    with patch.object(ActionGraph, "run_action", AsyncMock(side_effect=fake_run_action)):
        final = await ActionGraph.run_action_graph_v2(
            ctx=Context(),
            app_state=AppState(),
            user_actions=AsyncMock(),
            action_graph_input_dict={"text": "in"},
            action_graph_result_doc=run_doc,
            action_graph_node_id=None,
            is_continue_run_graph=False,
            user_action_graph_result=service,
            notify=None,
        )

    assert final is run_doc, "the run document was replaced part-way through the run"
    assert final.result is graph_doc


# --------------------------------------------------------------------------- the known difference


@pytest.mark.asyncio
async def test_timestamps_are_no_longer_relabelled_as_utc_by_the_round_trip():
    """The one respect in which the documents are *not* byte-identical, pinned deliberately.

    `Clock.datetime_now()` returns `datetime.now()` - naive local time - into fields annotated as
    tz-aware `DateTime`. Serialised fresh that is `...702043`; parsed back, pydantic reads the
    naive value as UTC and re-serialises it as `...702043Z` - the same wall-clock reading now
    labelled as UTC, and so wrong by the deployment's offset.

    An update carrying `result=` overwrites the fetched copy with the caller's, so those writes
    never relabelled anything. The run's **final** write did not carry one - it set `status` alone
    - so it persisted the copy it had just fetched and parsed, and that is the document the user
    ends up with. That is the whole of the difference Part 2 makes to stored bytes.

    This test does not assert which form is *right* - the naive value is what `Clock` actually
    produced - only that the difference is confined to timestamps and follows from the round trip
    rather than from any change to what the engine records.
    """
    service, probe, final = await a_completed_run()
    direct = json.loads(probe.stored_document("run-1"))

    # What the old engine's final write did: status only, no `result`.
    await service.update_action_graph_result(
        Context(), "run-1", ActionGraphResultUpdate(status=EventResultStatus.success)
    )
    round_tripped = json.loads(probe.stored_document("run-1"))

    direct_stamp = direct["result"]["output"]["a"]["created_at"]
    round_tripped_stamp = round_tripped["result"]["output"]["a"]["created_at"]

    assert not direct_stamp.endswith("Z"), "the authoritative path writes what Clock produced"
    assert round_tripped_stamp == f"{direct_stamp}Z", (
        "the fetching path reinterprets the same naive value as UTC"
    )
    assert without_timestamps(json.dumps(direct)) == without_timestamps(json.dumps(round_tripped)), (
        "nothing but the timestamps differs"
    )


@pytest.mark.asyncio
async def test_the_two_write_paths_agree_on_a_cyclic_document_too():
    """Exit criterion 2's other half: the K > 1 shape must survive both write paths intact.

    `node_vectors` is the field the Mongo projection deliberately drops, so this is where a
    "write the lite doc to S3 by mistake" bug would show up.
    """
    from src.autobots.action_graph.action_graph_result.user_action_graph_result import (
        UserActionGraphResult,
    )
    from tests.graphs.persistence_probe import a_cyclic_run_doc, a_probed_service

    run_doc = a_cyclic_run_doc(CHAIN, slots=3, turns=2)
    service, probe = a_probed_service(run_doc)
    assert isinstance(service, UserActionGraphResult)

    await service.save_run_doc(Context(), run_doc)
    written_directly = probe.stored_document("run-1")

    # The same status the document already carries, so the patch changes nothing and any
    # difference in the result is the write path's doing rather than the patch's.
    await service.update_action_graph_result(
        Context(), "run-1", ActionGraphResultUpdate(status=EventResultStatus.processing)
    )
    written_via_fetch = probe.stored_document("run-1")

    direct = json.loads(written_directly)
    assert direct["result"]["node_vectors"] is not None, "the fixture built no vector"
    assert set(direct["result"]["node_vectors"]) == set(direct["result"]["output"]), (
        "output and node_vectors must cover the same nodes"
    )
    assert set(direct["result"]["node_vectors"]["a"]["slots"]) == {"0", "1", "2"}

    assert without_timestamps(written_directly) == without_timestamps(written_via_fetch)
