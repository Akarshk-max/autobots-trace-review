"""Counting doubles for the run-persistence path, shared by the write-amplification tests.

The point is to measure the *real* `UserActionGraphResult.update_action_graph_result` - the S3 GET,
the S3 PUT, the Mongo write and the Redis cache bump - rather than a stub of it, because those four
are exactly what Part 2 changes. So the service object is real; only its three collaborators are
replaced with recorders.

`UserActionGraphResult.__init__` builds an S3 client and reads settings, neither of which a unit
test should need, so the instance is assembled field by field instead.
"""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from typing import Any, Dict, List

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.action_graph.action_graph.action_graph_doc_model import (
    ActionGraphDoc,
    Node,
    NodeData,
)
from src.autobots.action_graph.action_graph_result.action_graph_result_model_doc import (
    ActionGraphResultDoc,
)
from src.autobots.action_graph.action_graph_result.user_action_graph_result import (
    UserActionGraphResult,
)
from src.autobots.event_result.event_result_model import (
    EventResultDoc,
    EventResultStatus,
    EventType,
)


class CountingS3:
    """Stands in for `UserFiles`. Keeps the bytes so reads see what writes produced."""

    def __init__(self) -> None:
        self.gets = 0
        self.puts = 0
        self.bytes_read = 0
        self.bytes_written = 0
        self.store: Dict[str, bytes] = {}

    async def get_bytes(self, ctx: Any, filename: str) -> bytes | None:
        self.gets += 1
        payload = self.store.get(filename)
        self.bytes_read += len(payload or b"")
        return payload

    async def upload_binary(self, ctx: Any, file_obj: BytesIO, filename: str) -> List[Any]:
        payload = file_obj.read()
        self.puts += 1
        self.bytes_written += len(payload)
        self.store[filename] = payload
        return [SimpleNamespace(url=f"s3://runs/{filename}")]


class CountingMongo:
    """Stands in for `EventResultCRUD`. Only `update_one` is on the hot path."""

    def __init__(self, seed: EventResultDoc) -> None:
        self.writes = 0
        self.reads = 0
        self.bytes_written = 0
        self.doc = seed

    async def update_one(self, update: Any) -> EventResultDoc:
        self.writes += 1
        patch = update.model_dump(exclude_none=True, exclude={"id", "user_id"})
        self.bytes_written += len(str(patch))
        self.doc = self.doc.model_copy(update=patch)
        return self.doc

    async def find_one(self, find: Any) -> EventResultDoc:
        self.reads += 1
        return self.doc


class Probe:
    """Everything one benchmarked run recorded."""

    def __init__(self, s3: CountingS3, mongo: CountingMongo) -> None:
        self.s3 = s3
        self.mongo = mongo
        self.cache_bumps = 0

    def snapshot(self) -> Dict[str, int]:
        return {
            "s3_gets": self.s3.gets,
            "s3_puts": self.s3.puts,
            "s3_bytes_written": self.s3.bytes_written,
            "mongo_writes": self.mongo.writes,
            "cache_bumps": self.cache_bumps,
        }

    def stored_document(self, run_id: str) -> str:
        return self.s3.store[f"{run_id}.json"].decode("utf-8")


def a_graph_doc(
    graph: Dict[str, List[str]],
    *,
    graph_id: str = "graph-1",
    loop_nodes: tuple[str, ...] = (),
) -> ActionGraphDoc:
    nodes = {n for n in graph} | {d for ds in graph.values() for d in ds}
    return ActionGraphDoc(
        _id=graph_id,
        user_id="user-1",
        name="benchmark graph",
        nodes={n: f"action-{n}" for n in nodes},
        node_details={
            n: Node(
                id=n,
                data=NodeData(
                    label=n,
                    actionId="" if n == "s" else f"action-{n}",
                    is_loop=n in loop_nodes,
                ),
            )
            for n in nodes
        },
        graph=graph,
        output={},
    )


def a_run_doc(graph_doc: ActionGraphDoc, *, run_id: str = "run-1") -> ActionGraphResultDoc:
    return ActionGraphResultDoc(
        _id=run_id,
        user_id="user-1",
        type=EventType.action_graph_run,
        status=EventResultStatus.processing,
        result=graph_doc,
    )


def a_probed_service(run_doc: ActionGraphResultDoc) -> tuple[UserActionGraphResult, Probe]:
    """A real `UserActionGraphResult` wired to recorders instead of S3, Mongo and Redis."""
    seed = EventResultDoc(
        _id=run_doc.id,
        user_id=run_doc.user_id,
        type=EventType.action_graph_run,
        status=EventResultStatus.processing,
        result=run_doc.result.model_dump(),
    )
    s3, mongo = CountingS3(), CountingMongo(seed)
    probe = Probe(s3, mongo)

    service = object.__new__(UserActionGraphResult)
    service.user = SimpleNamespace(id="user-1", space_member=None)
    service.user_id = "user-1"
    service.space_member = None
    service.fs_crud = s3
    service.event_result_crud = mongo

    async def counting_bust(_user_id: str) -> None:
        probe.cache_bumps += 1

    # Shadows the classmethod on this instance only; the real one talks to Redis.
    service._bust_search_cache_for_user = counting_bust
    return service, probe


def an_action_result(node: str, *, payload_blocks: int = 1) -> ActionDoc:
    """One node's output. `payload_blocks` is the knob that makes the document grow."""
    return ActionDoc(
        _id=f"action-{node}",
        user_id="user-1",
        name=f"action {node}",
        type=ActionType.mock_action,
        input={"text": f"in {node}"},
        output={"blocks": [{"text": f"out {node} {i}"} for i in range(payload_blocks)]},
        results=[],
    )


def a_cyclic_run_doc(
    graph: Dict[str, List[str]],
    *,
    slots: int = 3,
    turns: int = 2,
    payload_blocks: int = 1,
) -> ActionGraphResultDoc:
    """A run document in the shape a cyclic graph produces: K slots per node, several turns each.

    Part 3 builds the scheduler that produces these for real. The persistence path does not care
    how the vector got there - only how large the document is - so the shape can be constructed
    directly, which is what lets Part 2's exit criterion be met before Part 3 exists.

    `slots` is K. Slot 0 is the baseline; the rest are indexed by redirecting node.
    """
    from src.autobots.action_graph.action_graph.context_vector import BASELINE_SLOT
    from src.autobots.action_graph.action_graph.node_result_writer import (
        NodeRunContext,
        record_node_result,
    )

    graph_doc = a_graph_doc(graph)
    run_doc = a_run_doc(graph_doc)
    start = "s"
    target_slots = tuple([BASELINE_SLOT] + [str(i) for i in range(1, slots)])

    for node in sorted(graph_doc.node_details):
        if node == start:
            continue
        for turn_no in range(turns):
            record_node_result(
                run_doc,
                an_action_result(f"action-{node}", payload_blocks=payload_blocks),
                NodeRunContext(
                    node=node,
                    start_node_id=start,
                    # The start node on the forward pass, a redirecting node on a re-entry.
                    tag=start if turn_no == 0 else node,
                    turn_no=turn_no,
                    target_slots=target_slots,
                ),
            )
    return run_doc
