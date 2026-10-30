import json
from copy import deepcopy
from typing import Any, Callable, Dict, List, Mapping, Tuple

import pytest
from pydantic import BaseModel, ConfigDict

from src.autobots.conn.openai.openai_responses.response_model import OpenaiResponsesReq
from src.autobots.core.settings import SettingsProvider

settings = SettingsProvider.sget()
pytestmark = pytest.mark.skipif(
    settings.ENV != "local",
    reason=(
        "Local-only strategy matrix test (diagnostic harness); "
        "skipped in non-local environments/CI."
    ),
)


# -----------------------------------------------------------------------------
# Stop expensive autouse fixtures if your repo defines them (safe no-ops)
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


# -----------------------------------------------------------------------------
# Builders (raw dicts; keep minimal OpenaiResponsesReq fields)
# -----------------------------------------------------------------------------
def typed_user_message(text: str) -> dict:
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def base_payload() -> Dict[str, Any]:
    return {
        "model": "gpt-4.1-mini",
        "store": True,
        "tools": ["code_interpreter"],
        "input": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Give me a file-wise breakdown."},
        ],
    }


def assistant_output_message(*, content_parts: List[dict]) -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": content_parts,
    }


def output_text_part(text: str, annotations: List[Any]) -> dict:
    # IMPORTANT: In OpenAI SDK types, output_text parts require "annotations" (can be empty list).
    return {"type": "output_text", "text": text, "annotations": annotations}


def dummy_typed_messages(n: int) -> List[dict]:
    return [typed_user_message(f"dummy-{i}") for i in range(n)]


# -----------------------------------------------------------------------------
# Scenarios
# -----------------------------------------------------------------------------
class Scenario(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        arbitrary_types_allowed=True,  # allows Callable fields
    )
    name: str
    build: Callable[[], Dict[str, Any]]


def scenario_container_file_citation_missing_index() -> Dict[str, Any]:
    p = base_payload()
    anns = [
        {"type": "file_citation", "file_id": "file_1", "index": 2},
        {
            "type": "container_file_citation",
            "container_id": "cntr_123",
            "file_id": "cfile_123",
            "filename": "out.csv",
            "start_index": 5,
            "end_index": 7,
            # missing index
        },
    ]
    p["input"].append(assistant_output_message(content_parts=[output_text_part("Here is out.csv", anns)]))
    p["input"].append(typed_user_message("follow up"))
    return p


def scenario_url_citation_missing_index() -> Dict[str, Any]:
    p = base_payload()
    anns = [
        {"type": "file_citation", "file_id": "file_1", "index": 0},
        {
            "type": "url_citation",
            "url": "https://example.com",
            "title": "Example",
            "start_index": 4,
            "end_index": 10,
            # missing index
        },
    ]
    p["input"].append(assistant_output_message(content_parts=[output_text_part("See https://example.com", anns)]))
    p["input"].append(typed_user_message("continue"))
    return p


def scenario_file_citation_missing_index_no_start_index() -> Dict[str, Any]:
    p = base_payload()
    anns = [{"type": "file_citation", "file_id": "file_1", "filename": "a.txt"}]  # missing index, no start_index
    p["input"].append(assistant_output_message(content_parts=[output_text_part("Citing a file", anns)]))
    p["input"].append(typed_user_message("continue"))
    return p


def scenario_missing_index_in_second_content_part_only() -> Dict[str, Any]:
    p = base_payload()

    anns_ok = [{"type": "file_citation", "file_id": "file_ok", "index": 0}]
    anns_bad = [{"type": "url_citation", "url": "x", "start_index": 1, "end_index": 2}]  # missing index

    msg = assistant_output_message(
        content_parts=[
            output_text_part("part0", anns_ok),
            output_text_part("part1", anns_bad),
        ]
    )
    p["input"].append(msg)
    p["input"].append(typed_user_message("continue"))
    return p


def scenario_deep_input_index_369_pointer() -> Dict[str, Any]:
    """
    Mirrors pointer depth. Ensure indices are in-bounds to avoid accidental strict failures.
    """
    p = base_payload()
    p["input"].extend(dummy_typed_messages(369))

    text = "file link here"  # len=14
    anns = [
        {"type": "file_citation", "file_id": "file_1", "index": 2},  # in-bounds
        {
            "type": "container_file_citation",
            "container_id": "cntr",
            "file_id": "cfile",
            "filename": "out.csv",
            "start_index": 5,
            "end_index": 9,
            # missing index
        },
    ]
    p["input"].append(assistant_output_message(content_parts=[output_text_part(text, anns)]))
    p["input"].append(typed_user_message("follow up"))
    return p


def scenario_container_start_index_out_of_bounds() -> Dict[str, Any]:
    """
    If you backfill index=start_index, strict sanity should fail.
    """
    p = base_payload()
    text = "short"  # len=5
    anns = [
        {
            "type": "container_file_citation",
            "container_id": "cntr",
            "file_id": "cfile",
            "filename": "out.csv",
            "start_index": 999,  # out of bounds
            "end_index": 1000,
            # missing index
        }
    ]
    p["input"].append(assistant_output_message(content_parts=[output_text_part(text, anns)]))
    p["input"].append(typed_user_message("continue"))
    return p


def scenario_index_present_but_wrong_type() -> Dict[str, Any]:
    """
    Min server rule passes (index present), strict sanity should fail unless annotations removed.
    """
    p = base_payload()
    anns = [{"type": "file_citation", "file_id": "file_1", "index": "12"}]  # wrong type
    p["input"].append(assistant_output_message(content_parts=[output_text_part("hello world", anns)]))
    p["input"].append(typed_user_message("continue"))
    return p


def scenario_text_mutation_makes_index_oob() -> Dict[str, Any]:
    """
    Models the production hazard: text changed after indices were created.
    Here we simulate the post-mutation form directly: index is now out-of-bounds.
    """
    p = base_payload()
    # index points past the end of the shortened text
    anns = [{"type": "file_citation", "file_id": "file_1", "index": 20}]
    p["input"].append(assistant_output_message(content_parts=[output_text_part("tiny", anns)]))  # len=4
    p["input"].append(typed_user_message("continue"))
    return p


SCENARIOS: List[Scenario] = [
    Scenario(name="container_file_citation_missing_index", build=scenario_container_file_citation_missing_index),
    Scenario(name="url_citation_missing_index", build=scenario_url_citation_missing_index),
    Scenario(name="file_citation_missing_index_no_start_index", build=scenario_file_citation_missing_index_no_start_index),
    Scenario(name="missing_index_in_second_content_part_only", build=scenario_missing_index_in_second_content_part_only),
    Scenario(name="deep_input_index_369_pointer", build=scenario_deep_input_index_369_pointer),
    Scenario(name="container_start_index_out_of_bounds", build=scenario_container_start_index_out_of_bounds),
    Scenario(name="index_present_but_wrong_type", build=scenario_index_present_but_wrong_type),
    Scenario(name="text_mutation_makes_index_oob", build=scenario_text_mutation_makes_index_oob),
]


# -----------------------------------------------------------------------------
# Server-like validators
# -----------------------------------------------------------------------------
class ServerLike400(ValueError):
    def __init__(self, path: str):
        super().__init__(f"Missing required parameter: '{path}'.")
        self.path = path


def iter_annotation_locations(payload: Mapping[str, Any]):
    inp = payload.get("input")
    if not isinstance(inp, list):
        return
    for i, item in enumerate(inp):
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for j, part in enumerate(content):
            if not isinstance(part, dict):
                continue
            anns = part.get("annotations")
            if not isinstance(anns, list):
                continue
            for k, ann in enumerate(anns):
                yield i, j, k, part, ann


def validate_server_min_index_required(payload: Dict[str, Any]) -> None:
    # Match the actual OpenAI 400 you saw: missing annotation.index
    for i, j, k, _part, ann in iter_annotation_locations(payload):
        if isinstance(ann, dict) and "index" not in ann:
            raise ServerLike400(f"input[{i}].content[{j}].annotations[{k}].index")


def validate_server_strict_sanity(payload: Dict[str, Any]) -> None:
    """
    Optional sanity checks. Useful for catching your own text mutation problems.
    Runs only after min index rule passes.
    """
    validate_server_min_index_required(payload)

    for i, j, k, part, ann in iter_annotation_locations(payload):
        if not isinstance(ann, dict):
            continue

        idx = ann.get("index")
        if not isinstance(idx, int):
            raise ValueError(
                f"annotation index must be int at input[{i}].content[{j}].annotations[{k}].index"
            )

        txt = part.get("text")
        if isinstance(txt, str):
            if idx < 0 or idx > len(txt):
                raise ValueError(
                    f"annotation index out of bounds at input[{i}].content[{j}].annotations[{k}].index "
                    f"(idx={idx}, len(text)={len(txt)})"
                )


# -----------------------------------------------------------------------------
# Patch strategies
# -----------------------------------------------------------------------------
def dc(p: Dict[str, Any]) -> Dict[str, Any]:
    return deepcopy(p)


def patch_A_backfill_index_from_start_index(payload: Dict[str, Any]) -> Dict[str, Any]:
    p = dc(payload)
    for _i, _j, _k, _part, ann in iter_annotation_locations(p):
        if isinstance(ann, dict) and "index" not in ann and isinstance(ann.get("start_index"), int):
            ann["index"] = ann["start_index"]
    return p


def patch_A2_backfill_then_drop_ranges(payload: Dict[str, Any]) -> Dict[str, Any]:
    p = patch_A_backfill_index_from_start_index(payload)
    for _i, _j, _k, _part, ann in iter_annotation_locations(p):
        if isinstance(ann, dict):
            ann.pop("start_index", None)
            ann.pop("end_index", None)
    return p


def patch_B_drop_container_only(payload: Dict[str, Any]) -> Dict[str, Any]:
    p = dc(payload)
    inp = p.get("input")
    if not isinstance(inp, list):
        return p
    for item in inp:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            anns = part.get("annotations")
            if not isinstance(anns, list):
                continue
            part["annotations"] = [
                a for a in anns
                if not (isinstance(a, dict) and a.get("type") == "container_file_citation")
            ]
    return p


def patch_B2_clear_annotations_list(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    IMPORTANT: for output_text parts, 'annotations' is required by OpenAI SDK param models.
    So we keep the key but clear its contents.
    """
    p = dc(payload)
    inp = p.get("input")
    if not isinstance(inp, list):
        return p
    for item in inp:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("annotations"), list):
                part["annotations"] = []
    return p


def patch_B3_flatten_output_to_system_input_text(payload: Dict[str, Any]) -> Dict[str, Any]:
    p = dc(payload)
    inp = p.get("input")
    if not isinstance(inp, list):
        return p

    for idx, item in enumerate(inp):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message" and item.get("role") == "assistant":
            content = item.get("content")
            if not isinstance(content, list):
                continue
            texts: List[str] = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    texts.append(part["text"])
            if texts:
                inp[idx] = {
                    "type": "message",
                    "role": "system",
                    "content": [{"type": "input_text", "text": "\n".join(texts)}],
                }

    p["input"] = inp
    return p


def patch_C_previous_response_id_only(payload: Dict[str, Any]) -> Dict[str, Any]:
    p = dc(payload)
    p["previous_response_id"] = "resp_prev_test_123"
    inp = p.get("input")
    if isinstance(inp, list) and inp:
        last = inp[-1]
        p["input"] = [last] if isinstance(last, dict) else []
    else:
        p["input"] = []
    return p


PATCHES: List[Tuple[str, Callable[[Dict[str, Any]], Dict[str, Any]]]] = [
    ("A_backfill_index", patch_A_backfill_index_from_start_index),
    ("A2_backfill_then_drop_ranges", patch_A2_backfill_then_drop_ranges),
    ("B_drop_container_only", patch_B_drop_container_only),
    ("B2_clear_annotations_list", patch_B2_clear_annotations_list),
    ("B3_flatten_output_to_system_input_text", patch_B3_flatten_output_to_system_input_text),
    ("C_previous_response_id_only", patch_C_previous_response_id_only),
]


# -----------------------------------------------------------------------------
# Expected outcomes
# -----------------------------------------------------------------------------
EXPECTED_MIN_PASS: Dict[str, Dict[str, bool]] = {
    "container_file_citation_missing_index": {
        "A_backfill_index": True,
        "A2_backfill_then_drop_ranges": True,
        "B_drop_container_only": True,
        "B2_clear_annotations_list": True,
        "B3_flatten_output_to_system_input_text": True,
        "C_previous_response_id_only": True,
    },
    "url_citation_missing_index": {
        "A_backfill_index": True,
        "A2_backfill_then_drop_ranges": True,
        "B_drop_container_only": False,  # doesn't remove url_citation
        "B2_clear_annotations_list": True,
        "B3_flatten_output_to_system_input_text": True,
        "C_previous_response_id_only": True,
    },
    "file_citation_missing_index_no_start_index": {
        "A_backfill_index": False,  # cannot backfill
        "A2_backfill_then_drop_ranges": False,
        "B_drop_container_only": False,
        "B2_clear_annotations_list": True,
        "B3_flatten_output_to_system_input_text": True,
        "C_previous_response_id_only": True,
    },
    "missing_index_in_second_content_part_only": {
        "A_backfill_index": True,
        "A2_backfill_then_drop_ranges": True,
        "B_drop_container_only": False,  # url_citation remains
        "B2_clear_annotations_list": True,
        "B3_flatten_output_to_system_input_text": True,
        "C_previous_response_id_only": True,
    },
    "deep_input_index_369_pointer": {
        "A_backfill_index": True,
        "A2_backfill_then_drop_ranges": True,
        "B_drop_container_only": True,
        "B2_clear_annotations_list": True,
        "B3_flatten_output_to_system_input_text": True,
        "C_previous_response_id_only": True,
    },
    "container_start_index_out_of_bounds": {
        "A_backfill_index": True,  # min passes; strict should fail
        "A2_backfill_then_drop_ranges": True,
        "B_drop_container_only": True,
        "B2_clear_annotations_list": True,
        "B3_flatten_output_to_system_input_text": True,
        "C_previous_response_id_only": True,
    },
    "index_present_but_wrong_type": {
        # min passes for everyone because index exists already
        "A_backfill_index": True,
        "A2_backfill_then_drop_ranges": True,
        "B_drop_container_only": True,
        "B2_clear_annotations_list": True,
        "B3_flatten_output_to_system_input_text": True,
        "C_previous_response_id_only": True,
    },
    "text_mutation_makes_index_oob": {
        "A_backfill_index": True,
        "A2_backfill_then_drop_ranges": True,
        "B_drop_container_only": True,
        "B2_clear_annotations_list": True,
        "B3_flatten_output_to_system_input_text": True,
        "C_previous_response_id_only": True,
    },
}

EXPECTED_STRICT_PASS: Dict[str, Dict[str, bool]] = {
    # normal scenarios: strict should pass
    "container_file_citation_missing_index": {k: True for k, _ in PATCHES},
    "url_citation_missing_index": {k: True for k, _ in PATCHES},
    "file_citation_missing_index_no_start_index": {k: True for k, _ in PATCHES},
    "missing_index_in_second_content_part_only": {k: True for k, _ in PATCHES},
    "deep_input_index_369_pointer": {k: True for k, _ in PATCHES},

    # strict should fail for A/A2 when start_index is huge
    "container_start_index_out_of_bounds": {
        "A_backfill_index": False,
        "A2_backfill_then_drop_ranges": False,
        "B_drop_container_only": True,
        "B2_clear_annotations_list": True,
        "B3_flatten_output_to_system_input_text": True,
        "C_previous_response_id_only": True,
    },

    # strict should fail for patches that keep wrong-type index
    "index_present_but_wrong_type": {
        "A_backfill_index": False,
        "A2_backfill_then_drop_ranges": False,
        "B_drop_container_only": False,
        "B2_clear_annotations_list": True,
        "B3_flatten_output_to_system_input_text": True,
        "C_previous_response_id_only": True,
    },

    # strict should fail for patches that keep an OOB index after text “mutation”
    "text_mutation_makes_index_oob": {
        "A_backfill_index": False,
        "A2_backfill_then_drop_ranges": False,
        "B_drop_container_only": False,
        "B2_clear_annotations_list": True,
        "B3_flatten_output_to_system_input_text": True,
        "C_previous_response_id_only": True,
    },
}


# -----------------------------------------------------------------------------
# Invariants
# -----------------------------------------------------------------------------
def assert_not_mutated(before: Dict[str, Any], after: Dict[str, Any]) -> None:
    assert json.dumps(before, sort_keys=True) == json.dumps(after, sort_keys=True), "Patch mutated input in place"


def assert_idempotent(patch_fn: Callable[[Dict[str, Any]], Dict[str, Any]], payload: Dict[str, Any]) -> None:
    once = patch_fn(payload)
    twice = patch_fn(once)
    assert json.dumps(once, sort_keys=True) == json.dumps(twice, sort_keys=True), "Patch is not idempotent"


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_local_validation_gate_matches_production(scenario: Scenario):
    """
    Mirrors production gate:
        OpenaiResponsesReq(**payload) should succeed for our constructed scenarios.
    """
    payload = scenario.build()
    _ = OpenaiResponsesReq(**payload)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
@pytest.mark.parametrize("patch_name,patch_fn", PATCHES, ids=[p[0] for p in PATCHES])
def test_patch_matrix_min_server_rule_and_local_validation(scenario: Scenario, patch_name: str, patch_fn):
    payload = scenario.build()
    before = deepcopy(payload)

    fixed = patch_fn(payload)

    assert_not_mutated(before, payload)
    assert_idempotent(patch_fn, payload)

    json.dumps(fixed, sort_keys=True)  # wire safety
    _ = OpenaiResponsesReq(**fixed)    # local gate safety

    expected = EXPECTED_MIN_PASS[scenario.name][patch_name]
    if expected:
        validate_server_min_index_required(fixed)
    else:
        with pytest.raises(ServerLike400):
            validate_server_min_index_required(fixed)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
@pytest.mark.parametrize("patch_name,patch_fn", PATCHES, ids=[p[0] for p in PATCHES])
def test_patch_matrix_strict_sanity_optional(scenario: Scenario, patch_name: str, patch_fn):
    """
    Optional sanity: models future strictness + your own text-mutation hazards.
    Only evaluated when min is expected to pass.
    """
    payload = scenario.build()
    fixed = patch_fn(payload)

    if not EXPECTED_MIN_PASS[scenario.name][patch_name]:
        pytest.skip("min rule not expected to pass; strict sanity irrelevant")

    validate_server_min_index_required(fixed)

    expected_strict = EXPECTED_STRICT_PASS[scenario.name][patch_name]
    if expected_strict:
        validate_server_strict_sanity(fixed)
    else:
        with pytest.raises(ValueError):
            validate_server_strict_sanity(fixed)
