import copy

import pytest
from pydantic import BaseModel

from src.autobots.conn.openai.openai_responses.replay_sanitizer import (
    SanitizerPolicy,
    sanitize_outgoing_payload,
)


# -----------------------------------------------------------------------------
# Stop expensive autouse fixtures (DB/tooling) for this pure unit-test module.
# If your repo defines these in tests/conftest.py, overriding here keeps this fast
# and prevents test failures unrelated to sanitizer behavior.
# -----------------------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def register_tools():
    yield

@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    yield

@pytest.fixture(scope="session", autouse=True)
def start():
    yield


REPAIR_POLICY = SanitizerPolicy(clear_output_text_annotations=False)


def _mk_payload(*, text: str = "hi", annotations=None, model: str = "gpt-test"):
    if annotations is None:
        annotations = []
    return {
        "model": model,
        "input": [
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                        "annotations": annotations,
                    }
                ],
            }
        ],
    }


def _sanitize(payload, *, policy=REPAIR_POLICY):
    payload = copy.deepcopy(payload)
    return sanitize_outgoing_payload(payload, policy=policy)


def _anns(out):
    return out["input"][0]["content"][0]["annotations"]


def _label_idx(text: str, filename: str) -> int:
    needle = f"[{filename}]("
    pos = text.find(needle)
    assert pos != -1, f"Expected {needle!r} to exist in text={text!r}"
    return pos + 1


def test_outage_case_repairs_missing_index_from_markdown_label_filename():
    fn = "stunnin_file_breakdown.csv"
    text = f"Here is your file: [{fn}](https://example.com/{fn})\nDone."
    ann = {
        "type": "container_file_citation",
        "filename": fn,
        "start_index": 344,  # stale/OOB after mutation
        "end_index": 388,
    }
    out, rep = _sanitize(_mk_payload(text=text, annotations=[ann]))

    assert len(_anns(out)) == 1
    assert _anns(out)[0]["index"] == _label_idx(text, fn)
    assert rep.repaired == 1
    assert rep.dropped == 0
    assert rep.cleared_lists == 0


def test_prefers_markdown_label_over_url_match_when_filename_repeats():
    fn = "x.csv"
    text = f"See [{fn}](https://host/download?f={fn}&also={fn}) end"
    ann = {"type": "container_file_citation", "filename": fn}
    out, rep = _sanitize(_mk_payload(text=text, annotations=[ann]))

    assert _anns(out)[0]["index"] == _label_idx(text, fn)
    assert rep.repaired == 1


@pytest.mark.parametrize(
    "ann,text,expected_index",
    [
        ({"type": "url_citation", "url": "https://h/u"}, "link: https://h/u end", lambda t: t.index("https://h/u")),
        ({"type": "url_citation", "text": "needle"}, "hay needle stack", lambda t: t.index("needle")),
    ],
)
def test_repairs_missing_index_from_url_or_snippet_fallback(ann, text, expected_index):
    out, rep = _sanitize(_mk_payload(text=text, annotations=[ann]))

    assert len(_anns(out)) == 1
    assert _anns(out)[0]["index"] == expected_index(text)
    assert rep.repaired == 1
    assert rep.dropped == 0


def test_repairs_missing_index_from_sandbox_path_occurrence():
    # Derivation supports finding `sandbox:/mnt/data/<filename>` and returns index pointing at filename
    fn = "a.csv"
    text = f"sandbox:/mnt/data/{fn} is here"
    ann = {"type": "file_citation", "filename": fn}  # missing index
    out, rep = _sanitize(_mk_payload(text=text, annotations=[ann]))

    assert len(_anns(out)) == 1
    assert _anns(out)[0]["index"] == text.index(fn)
    assert rep.repaired == 1


def test_repairs_missing_index_using_start_index_when_in_bounds():
    text = "hello world"
    ann = {"type": "file_citation", "start_index": 6}  # points at 'w'
    out, rep = _sanitize(_mk_payload(text=text, annotations=[ann]))

    assert _anns(out)[0]["index"] == 6
    assert rep.repaired == 1
    assert rep.cleared_lists == 0


def test_drops_unrepairable_annotation():
    text = "No matching filename, url, or snippet here."
    ann = {"type": "container_file_citation", "filename": "missing.csv"}  # not in text
    out, rep = _sanitize(_mk_payload(text=text, annotations=[ann]))

    assert _anns(out) == []
    assert rep.dropped == 1
    assert rep.cleared_lists == 0


def test_drops_only_bad_annotations_but_keeps_good_ones():
    fn = "a.csv"
    text = f"[{fn}](https://example.com/{fn})"
    anns_in = [
        {"type": "container_file_citation", "filename": fn},  # repairable (derive)
        {"type": "file_citation"},  # unrepairable
    ]

    out, rep = _sanitize(_mk_payload(text=text, annotations=anns_in))
    anns_out = _anns(out)

    assert len(anns_out) == 1
    assert anns_out[0]["filename"] == fn
    assert isinstance(anns_out[0]["index"], int)
    assert rep.repaired == 1
    assert rep.dropped == 1
    assert rep.cleared_lists == 0


def test_normalizes_annotations_wrong_type_to_empty_list():
    # annotations must be list/tuple; dict should normalize to []
    payload = _mk_payload(text="hello", annotations={"not": "a list"})
    out, rep = _sanitize(payload)

    assert _anns(out) == []
    assert rep.dropped == 0
    assert rep.cleared_lists == 0


def test_normalizes_annotations_none_to_empty_list():
    payload = _mk_payload(text="hello", annotations=None)
    out, rep = _sanitize(payload)

    assert _anns(out) == []
    assert rep.dropped == 0
    assert rep.cleared_lists == 0


def test_clear_annotations_when_text_missing_and_annotations_exist():
    payload = _mk_payload(text="", annotations=[{"type": "file_citation", "index": 0}])
    out, rep = _sanitize(payload)

    assert _anns(out) == []
    assert rep.cleared_lists == 1


def test_policy_keeps_annotations_when_text_missing_if_clear_disabled_and_index_present():
    policy = SanitizerPolicy(
        clear_output_text_annotations=False,
        clear_annotations_if_text_missing=False,
        enforce_bounds=True,
    )
    payload = _mk_payload(text="", annotations=[{"type": "file_citation", "index": "3"}])

    out, rep = _sanitize(payload, policy=policy)

    assert len(_anns(out)) == 1
    assert _anns(out)[0]["index"] == 3
    assert rep.cleared_lists == 0
    assert rep.dropped == 0


def test_enforce_bounds_true_drops_oob_index_when_unrepairable():
    text = "abc"
    payload = _mk_payload(text=text, annotations=[{"type": "file_citation", "index": 999}])
    out, rep = _sanitize(payload)

    assert _anns(out) == []
    assert rep.dropped == 1


def test_enforce_bounds_false_keeps_oob_index():
    policy = SanitizerPolicy(
        clear_output_text_annotations=False,
        clear_annotations_if_text_missing=False,
        enforce_bounds=False,
    )
    text = "abc"
    payload = _mk_payload(text=text, annotations=[{"type": "file_citation", "index": 999}])

    out, rep = _sanitize(payload, policy=policy)

    assert len(_anns(out)) == 1
    assert _anns(out)[0]["index"] == 999
    assert rep.dropped == 0
    assert rep.cleared_lists == 0


def test_tuple_normalization_for_input_content_and_annotations():
    fn = "t.csv"
    text = f"File: [{fn}](https://example.com/{fn})"
    payload = {
        "model": "gpt-test",
        "input": (
            {
                "type": "message",
                "role": "assistant",
                "content": (
                    {
                        "type": "output_text",
                        "text": text,
                        "annotations": (
                            {"type": "container_file_citation", "filename": fn},
                        ),
                    },
                ),
            },
        ),
    }

    out, rep = _sanitize(payload)

    assert isinstance(out["input"], list)
    assert isinstance(out["input"][0]["content"], list)
    assert isinstance(out["input"][0]["content"][0]["annotations"], list)
    assert out["input"][0]["content"][0]["annotations"][0]["index"] == _label_idx(text, fn)
    assert rep.repaired == 1


def test_base_model_leaks_are_handled_for_message_part_and_annotation():
    fn = "leak.csv"
    text = f"Here: [{fn}](https://example.com/{fn})"

    class AnnBM(BaseModel):
        type: str = "container_file_citation"
        filename: str

    class PartBM(BaseModel):
        type: str = "output_text"
        text: str
        annotations: list[object]

    class MsgBM(BaseModel):
        type: str = "message"
        role: str = "assistant"
        content: list[object]

    msg = MsgBM(content=[PartBM(text=text, annotations=[AnnBM(filename=fn)])])
    payload = {"model": "gpt-test", "input": [msg]}

    out, rep = _sanitize(payload)

    assert isinstance(out["input"][0], dict)
    assert isinstance(out["input"][0]["content"][0], dict)
    assert isinstance(out["input"][0]["content"][0]["annotations"][0], dict)
    assert out["input"][0]["content"][0]["annotations"][0]["index"] == _label_idx(text, fn)
    assert rep.repaired == 1


def test_ignores_non_message_items_and_non_output_text_parts():
    # Ensure sanitizer doesn't crash and doesn't touch unrelated items
    payload = {
        "model": "gpt-test",
        "input": [
            {"type": "function_call", "name": "tool", "arguments": "{}"},
            {"type": "message", "role": "assistant", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok", "annotations": []}]},
        ],
    }

    out, rep = _sanitize(payload)

    assert out["input"][2]["content"][0]["annotations"] == []
    assert rep.repaired == 0
    assert rep.dropped == 0
    assert rep.cleared_lists == 0


def test_idempotent_second_run_makes_no_further_changes():
    fn = "idempotent.csv"
    text = f"See [{fn}](https://example.com/{fn})"
    payload = _mk_payload(text=text, annotations=[{"type": "container_file_citation", "filename": fn}])

    out1, rep1 = _sanitize(payload)
    out2, rep2 = sanitize_outgoing_payload(copy.deepcopy(out1), policy=REPAIR_POLICY)

    assert out2 == out1
    assert rep1.repaired == 1
    assert rep2.repaired == 0
    assert rep2.dropped == 0
    assert rep2.cleared_lists == 0
