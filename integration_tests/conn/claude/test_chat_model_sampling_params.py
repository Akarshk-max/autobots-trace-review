import pytest

from src.autobots.conn.claude.chat_model import ChatReqClaude


def test_opus_48_drops_sampling_params():
    req = ChatReqClaude(
        messages=[{"role": "user", "content": "hello"}],
        model="claude-opus-4-8",
        temperature=0.3,
        top_p=0.9,
        top_k=50,
    )

    req.sanitize_for_model()

    assert req.temperature is None
    assert req.top_p is None
    assert req.top_k is None


def test_opus_46_keeps_temperature_when_no_thinking():
    req = ChatReqClaude(
        messages=[{"role": "user", "content": "hello"}],
        model="claude-opus-4-6",
        temperature=0.3,
    )

    req.sanitize_for_model()

    assert req.temperature == pytest.approx(0.3)
