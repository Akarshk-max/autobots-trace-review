def coffman_graham(graph, width):
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