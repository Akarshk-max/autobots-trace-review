from loguru import logger

from src.autobots.core.logging.app_code import AppCode
from src.autobots.exception.app_exception import AppException


class GraphCycleError(AppException):
    """
    Raised when the data-edge graph handed to the layering is not acyclic.

    Cycles belong in `redirect_edges`, never in `graph`. Kahn's algorithm below cannot reach a
    node inside a cycle, so without this check those nodes are silently dropped from the layering
    and never execute - and the run still reports success.
    """

    def __init__(self, unreachable_nodes: set):
        self.unreachable_nodes = unreachable_nodes
        super().__init__(
            detail=(
                f"Action graph contains a cycle in its data edges; "
                f"nodes never reachable by topological order: {sorted(unreachable_nodes)}"
            ),
            http_status=400,
        )


def coffman_graham(graph, width, *, raise_on_cycle: bool = True):
    """Layer a DAG by topological depth.

    `raise_on_cycle=False` keeps the historical behaviour - nodes inside a cycle are dropped from
    the layering and simply never execute. It exists so the engine can stay warn-only in step with
    `ACTION_GRAPH_VALIDATION_ENFORCED`: raising here is the only change that turns a currently
    (wrongly) succeeding run into a failure, so it flips when validation flips, not before.
    Validation itself always passes True - it wants the condition detected regardless.
    """

    in_degree = {node: 0 for node in graph}
    for node in graph:
        for neighbor in graph[node]:
            in_degree[neighbor] += 1

    queue = [node for node in in_degree if in_degree[node] == 0]
    ordered_nodes = []

    while queue:
        current = queue.pop(0)
        ordered_nodes.append(current)
        for neighbor in graph[current]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if len(ordered_nodes) != len(graph):
        unreachable = set(graph) - set(ordered_nodes)
        if raise_on_cycle:
            raise GraphCycleError(unreachable_nodes=unreachable)
        logger.bind(code=AppCode.ACTION_GRAPH_RUN, unreachable_nodes=sorted(unreachable)).error(
            "Action graph has a cycle in its data edges; those nodes will not execute and the run "
            "will still report success. Enable ACTION_GRAPH_VALIDATION_ENFORCED to fail instead."
        )

    node_labels = {}
    for node in reversed(ordered_nodes):
        dependencies_labels = [node_labels[dep] for dep in graph[node] if dep in node_labels]
        if dependencies_labels:
            node_labels[node] = max(dependencies_labels) + 1
        else:
            node_labels[node] = 1

    layers = [[] for _ in range(len(graph))]

    for node in ordered_nodes:
        layer_index = node_labels[node] - 1

        while len(layers[layer_index]) >= width:
            layer_index += 1
        layers[layer_index].append(node)

    return layers


def invert_graph(graph):
    inverted = {node: [] for node in graph}

    for node in graph:
        for neighbor in graph[node]:
            if neighbor not in inverted:
                inverted[neighbor] = []
            inverted[neighbor].append(node)

    return inverted


if __name__ == "__main__":
    graph = {
        "A": ["B", "C", "X", "Y"],
        "B": ["D", "E"],
        "C": ["F"],
        # "D": [],
        # "E": [],
        # "F": [],
        # "X": [],
        # "Y": []
    }
    layers = coffman_graham(invert_graph(graph), 2)
    print(f"coffman_graham layers:\n{layers}")
