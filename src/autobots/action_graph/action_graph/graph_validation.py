"""Running graph validation on the write path.

Warn-only to begin with: some graphs already in the collection predate any validation at all, so
rejecting on write would break editing them. This logs one structured line per failing graph so the
real failure rate can be measured, then `ACTION_GRAPH_VALIDATION_ENFORCED` flips it to a 400.

Note that a run carries a frozen copy of its graph, so tightening this never retroactively touches
work already in flight.
"""

from typing import List, Optional

from fastapi import HTTPException
from loguru import logger
from pydantic import BaseModel

from src.autobots import SettingsProvider
from src.autobots.action_graph.action_graph.action_graph_doc_model import (
    ActionGraphCreate,
    ActionGraphDoc,
    ActionGraphUpdate,
)
from src.autobots.action_graph.action_graph.graph_topology import (
    GraphTopology,
    Severity,
    ValidationIssue,
)
from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.context import Context

# The only fields that can change a graph's shape. An update touching none of them cannot make a
# valid graph invalid, so it skips the merge-and-validate entirely.
TOPOLOGY_FIELDS = (
    "graph",
    "nodes",
    "node_details",
    "redirect_edges",
    "conditional_edges",
    "start_node_id",
)

# Not topology, but carried through the merge anyway: `name` is the label the warning is filed
# under, and a rename saved alongside a restructure would otherwise be reported against the old
# one. Kept out of TOPOLOGY_FIELDS so a plain rename still skips validation entirely.
DIAGNOSTIC_FIELDS = ("name",)


class ActionGraphValidation(BaseModel):
    action_graph_id: str
    is_valid: bool
    is_cyclic: bool
    issues: List[ValidationIssue] = []


def touches_topology(action_graph_update: ActionGraphUpdate) -> bool:
    return any(getattr(action_graph_update, f, None) is not None for f in TOPOLOGY_FIELDS)


def merge_topology(
    current: ActionGraphDoc, action_graph_update: ActionGraphUpdate
) -> ActionGraphDoc:
    """Apply an update's topology fields to the stored graph, to validate the *result* of the write.

    Only the fields validation actually reads are merged; everything else on the patch keeps the
    stored value, which is harmless because the merged doc is a throwaway and is never persisted.

    Uses the field values rather than `model_dump()`, so nested models stay as models - dumping
    would turn `redirect_edges` into a list of dicts and `model_copy` does not re-validate.
    """
    patch = {
        field: getattr(action_graph_update, field)
        for field in TOPOLOGY_FIELDS + DIAGNOSTIC_FIELDS
        if getattr(action_graph_update, field, None) is not None
    }
    return current.model_copy(update=patch)


def validate_action_graph(
    ctx: Context,
    doc: ActionGraphCreate,
    *,
    action_graph_id: Optional[str] = None,
) -> List[ValidationIssue]:
    """Validate a graph about to be written. Raises 400 only once enforcement is switched on."""
    issues = GraphTopology.build(doc).validate()
    if not issues:
        return []

    errors = [i for i in issues if i.severity is Severity.error]
    warnings = [i for i in issues if i.severity is Severity.warning]

    # Logged separately, and only when there is something to enforce on. `create` fires on every
    # new workflow and `update` on every autosave, so a half-built graph reporting `no_start_node`
    # is the common case - folding it in with real malformations would bury the failure-rate
    # signal this warn-only phase exists to collect.
    if errors:
        enforced = SettingsProvider.sget().ACTION_GRAPH_VALIDATION_ENFORCED
        logger.bind(
            ctx=ctx,
            code=AppCode.ACTION_GRAPH,
            action_graph_id=action_graph_id or getattr(doc, "id", None),
            action_graph_name=doc.name,
            validation_codes=sorted({i.code.value for i in errors}),
            validation_error_count=len(errors),
            validation_warning_count=len(warnings),
            validation_enforced=enforced,
        ).warning(
            "Action graph failed validation: " + "; ".join(i.message for i in errors)
        )
        if enforced:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "Action graph is not valid",
                    "issues": [i.model_dump(mode="json") for i in errors],
                },
            )
    else:
        logger.bind(
            ctx=ctx,
            code=AppCode.ACTION_GRAPH,
            action_graph_id=action_graph_id or getattr(doc, "id", None),
            validation_codes=sorted({i.code.value for i in warnings}),
            validation_warning_count=len(warnings),
        ).debug("Action graph is incomplete but not malformed")

    return issues


def describe_validation(doc: ActionGraphDoc) -> ActionGraphValidation:
    """Read-only report, for surfacing warnings in the editor without attempting a write."""
    topology = GraphTopology.build(doc)
    issues = topology.validate()
    return ActionGraphValidation(
        action_graph_id=doc.id,
        # Warnings are ordinary mid-authoring states, so they do not make a graph invalid.
        is_valid=not any(i.severity is Severity.error for i in issues),
        is_cyclic=topology.is_cyclic,
        issues=issues,
    )
