import pytest
from pydantic import BaseModel

from src.autobots.conn.openrouter.utils import (
    dedupe_urls,
    is_tool_present,
    strip_reasoning_fields,
    to_plain,
)


class _Nested(BaseModel):
    a: int = 1
    b: str | None = None


class _Holder(BaseModel):
    nested: _Nested
    name: str = "x"


# to_plain
def test_to_plain_excludes_none_from_models():
    out = to_plain(_Nested())
    assert out == {"a": 1}
    assert "b" not in out


def test_to_plain_recursively_converts():
    obj = {
        "none": None,
        "model": _Holder(nested=_Nested(a=2)),
        "list": [_Nested(a=3), {"k": _Nested()}],
        "plain": 42,
        "s": "str",
    }
    out = to_plain(obj)
    assert out["none"] is None
    assert out["model"] == {"nested": {"a": 2}, "name": "x"}
    assert out["list"] == [{"a": 3}, {"k": {"a": 1}}]
    assert out["plain"] == 42


# dedupe_urls
def test_dedupe_urls_preserves_order_and_strips():
    assert dedupe_urls(["b", " a ", "b", "", None]) == ["b", "a"]


def test_dedupe_urls_handles_none():
    assert dedupe_urls(None) == []


# strip_reasoning_fields
def test_strip_reasoning_fields_removes_reasoning():
    msg = {"role": "assistant", "content": "hi", "reasoning": "secret", "reasoning_details": "x"}
    out = strip_reasoning_fields(msg)
    assert "reasoning" not in out
    assert "reasoning_details" not in out
    assert out["content"] == "hi"
    assert out is msg


def test_strip_reasoning_fields_noop_on_non_dict():
    assert strip_reasoning_fields("string") == "string"
    assert strip_reasoning_fields(None) is None


# is_tool_present
@pytest.mark.parametrize(
    "tool_defs",
    [
        ["daytona_sandbox"],
        [{"name": "daytona_sandbox"}],
        [{"type": "function", "function": {"name": "daytona_sandbox"}}],
        [{"type": "function", "function": {"name": "other"}}, "daytona_sandbox"],
    ],
)
def test_is_tool_present_true_across_all_shapes(tool_defs):
    assert is_tool_present(tool_defs, "daytona_sandbox") is True


@pytest.mark.parametrize(
    "tool_defs",
    [
        [],
        None,
        ["openrouter_inspect"],
        [{"name": "other"}],
        [{"type": "function", "function": {"name": "other"}}],
    ],
)
def test_is_tool_present_false_when_absent(tool_defs):
    assert is_tool_present(tool_defs, "daytona_sandbox") is False
