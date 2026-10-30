def topological_sort(graph):
    in_degree = {node: 0 for node in graph}

    # Calculate in-degrees
    for node in graph:
        for neighbor in graph[node]:
            in_degree[neighbor] += 1

    # Initialize the queue with nodes having in-degree 0
    queue = [node for node in in_degree if in_degree[node] == 0]
    topo_order = []

    while queue:
        current = queue.pop(0)
        topo_order.append(current)
        for neighbor in graph[current]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    # If the length of topo_order is not equal to the number of nodes, there's a cycle
    if len(topo_order) != len(graph):
        raise ValueError("Graph has at least one cycle, topological sort not possible.")

    return topo_order


if __name__ == "__main__":
    graph = {
        "A": ["B", "C"],
        "B": ["D", "E"],
        "C": ["F"],
        "D": ["G"],
        "E": [],
        "F": ["H"],
        "G": [],
        "H": [],
    }
    print(f"topological_sort: {topological_sort(graph)}")
