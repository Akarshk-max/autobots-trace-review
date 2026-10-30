from types import SimpleNamespace

from src.autobots.action_graph.action_graph.node_units_recorder import NodeUnitsRecorder
from src.autobots.run_metrics.run_metrics_doc_model import NodeUnits, UnitKind


def _no_loop_graph():
    return SimpleNamespace(node_units=None, time_saved_per_file=0.5)


def test_a_graph_with_no_loop_node_counts_one_succeeded_unit_when_it_succeeds():
    assert NodeUnitsRecorder.totals(_no_loop_graph(), True) == (1, 1, 0.5)


def test_a_failed_graph_with_no_loop_node_counts_no_succeeded_unit():
    units_total, units_succeeded, hours = NodeUnitsRecorder.totals(_no_loop_graph(), False)
    assert (units_total, units_succeeded) == (1, 0)
    assert hours is None


def test_a_failed_loop_graph_still_reports_the_units_it_did_process():
    """The correction must not swing into undercounting the work that really happened."""
    graph = SimpleNamespace(
        node_units={
            "n1": NodeUnits(
                node_id="n1", kind=UnitKind.file, total=10, succeeded=8,
                rate_per_unit=0.1, hours_saved=0.8,
            )
        }
    )
    assert NodeUnitsRecorder.totals(graph, False) == (10, 8, 0.8)
