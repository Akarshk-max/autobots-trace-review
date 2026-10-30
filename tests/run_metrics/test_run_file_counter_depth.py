from types import SimpleNamespace

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.run_metrics.run_file_counter import RunFileCounter


def _graph_result(nodes: int = 3, results_per_node: int = 4):
    output = {}
    for node in range(nodes):
        results = [
            {
                "input": {"text": "in"},
                "output": {"blocks": [{"text": "out", "urls": [f"deep_{node}_{i}.pdf"]}]},
            }
            for i in range(results_per_node)
        ]
        output[f"node-{node}"] = ActionDoc(
            _id=f"a{node}", id=f"a{node}", user_id="u", name=f"n{node}",
            type="text2text_llm_chat_openai", version=1.0, config={},
            input={"text": "in"},
            output={"results": results, "urls": [f"shallow_{node}.pdf"]},
        )
    return SimpleNamespace(input={"text": "start"}, output=output)


def test_files_nested_inside_a_nodes_results_are_counted():
    _, files_out = RunFileCounter.count(_graph_result())
    assert files_out == 15, "3 node-level urls plus 12 nested in their results"


def test_input_files_are_not_double_counted_as_output():
    result = _graph_result(nodes=1, results_per_node=1)
    result.input = {"urls": ["shallow_0.pdf"]}
    files_in, files_out = RunFileCounter.count(result)
    assert files_in == 1
    assert files_out == 1, "the pass-through file belongs to input, the nested one to output"
