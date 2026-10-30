from bson import ObjectId

from src.autobots.event_result.event_result_custom_search import (
    build_event_results_custom_search_filter,
)
from src.autobots.event_result.event_result_model import EventType


def test_build_custom_search_filter_scope_and_tokens() -> None:
    flt = build_event_results_custom_search_filter(
        user_id="u1", event_type=EventType.action_run, q="Error success"
    )

    assert flt["user_id"] == "u1"
    assert flt["type"] == EventType.action_run.value
    assert "$and" in flt
    assert len(flt["$and"]) == 2  # two tokens

    token_clause = flt["$and"][0]
    assert "$or" in token_clause
    # token clause should include some regexes for the searchable paths
    assert any("name" in c for c in token_clause["$or"])
    assert any("status" in c for c in token_clause["$or"])


def test_build_custom_search_filter_full_objectid_adds_exact_id_match() -> None:
    token = "69fa" + ("0" * 20)
    flt = build_event_results_custom_search_filter(
        user_id="u1", event_type=EventType.action_run, q=token
    )
    assert len(flt["$and"]) == 1
    token_clause = flt["$and"][0]
    or_clauses = token_clause["$or"]
    assert {"_id": ObjectId(token)} in or_clauses


def test_build_custom_search_filter_short_hex_does_not_trigger_id_match() -> None:
    flt = build_event_results_custom_search_filter(
        user_id="u1", event_type=EventType.action_run, q="69fa"
    )
    token_clause = flt["$and"][0]
    assert all("_id" not in c for c in token_clause["$or"])
