import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autobots.action.action_type.action_text2text.response_format_utils import (
    build_text_artifact_summary,
    infer_text_artifact_extension,
    is_ndjson_content_type,
    is_text_content_type,
)


def test_ndjson_content_types_are_detected_as_text():
    assert is_ndjson_content_type("application/x-ndjson") is True
    assert is_text_content_type("application/x-ndjson") is True
    assert is_text_content_type("application/problem+json") is True


def test_single_json_object_ndjson_prefers_json_extension():
    text = '{"yearBegin":"2023-07-01","yearEnd":"2024-06-30"}\n'
    assert infer_text_artifact_extension("application/x-ndjson", text) == ".json"


def test_scalar_ndjson_prefers_ndjson_extension():
    assert infer_text_artifact_extension("application/x-ndjson", "20230701\n") == ".ndjson"


def test_multi_record_ndjson_prefers_ndjson_extension():
    text = '{"accountNumber":6000000}\n{"accountNumber":6010000}\n'
    assert infer_text_artifact_extension("application/x-ndjson", text) == ".ndjson"


def test_ndjson_summary_reports_count_and_keys():
    text = '{"accountNumber":6000000,"caption":"A"}\n{"accountNumber":6010000,"caption":"B"}\n'
    summary = build_text_artifact_summary("application/x-ndjson", text)
    assert "2 records" in summary
    assert "accountNumber" in summary
    assert "caption" in summary


def test_single_object_summary_reports_keys():
    summary = build_text_artifact_summary("application/x-ndjson", '{"yearBegin":"2023-07-01","yearEnd":"2024-06-30"}\n')
    assert "1 JSON object" in summary
    assert "yearBegin" in summary
