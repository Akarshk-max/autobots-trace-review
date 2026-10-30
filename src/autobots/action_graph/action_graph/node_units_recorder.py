from src.autobots.action_graph.action_graph.action_graph_doc_model import ActionGraphDoc, NodeData
from src.autobots.run_metrics.run_metrics_doc_model import NodeUnits, UnitKind


class NodeUnitsRecorder:
    """What one loop node processed, recorded onto the run itself.

    Keyed by node, so a graph with several loops keeps each one separate and a single-node
    re-run supersedes its own entry rather than adding to it. Hours are summed per node
    against that node's own rate, so counts of different kinds are never added together.
    """

    @staticmethod
    def kind_of(node_data: NodeData) -> UnitKind:
        if node_data.excel_loop:
            return UnitKind.row
        if node_data.file_split:
            return UnitKind.file
        if node_data.api_loop:
            return UnitKind.api_batch
        if node_data.is_loop or node_data.is_split:
            return UnitKind.text
        return UnitKind.single

    @classmethod
    def record(
        cls,
        run_result: object,
        node_id: str,
        node_data: NodeData,
        total: int,
        succeeded: int,
    ) -> None:
        if not isinstance(run_result, ActionGraphDoc):
            return
        rate = node_data.time_saved_per_unit
        if rate is None:
            rate = run_result.time_saved_per_file
        if run_result.node_units is None:
            run_result.node_units = {}
        run_result.node_units[node_id] = NodeUnits(
            node_id=node_id,
            kind=cls.kind_of(node_data),
            total=total,
            succeeded=succeeded,
            rate_per_unit=rate,
            hours_saved=rate * succeeded if rate is not None else None,
        )

    @staticmethod
    def totals(run_result: object, run_succeeded: bool) -> tuple[int, int, float | None]:
        node_units = getattr(run_result, "node_units", None)
        if not node_units:
            rate = getattr(run_result, "time_saved_per_file", None)
            if not run_succeeded:
                return 1, 0, None
            return 1, 1, rate
        total = sum(units.total for units in node_units.values())
        succeeded = sum(units.succeeded for units in node_units.values())
        hours = [units.hours_saved for units in node_units.values() if units.hours_saved is not None]
        return total, succeeded, sum(hours) if hours else None
