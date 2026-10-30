from src.autobots.conn.gemini.streaming_adapter_gemini import GeminiStreamAccumulator


def test_stream_accumulator_preserves_function_call_when_later_chunk_is_empty():
    accumulator = GeminiStreamAccumulator()

    accumulator.ingest(
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "function_call": {
                                    "id": "call-1",
                                    "name": "api_call",
                                    "args": {"url": "https://example.com"},
                                },
                                "thought_signature": "sig-1",
                            }
                        ],
                    }
                }
            ]
        }
    )
    accumulator.ingest(
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [],
                    },
                    "finish_reason": "STOP",
                }
            ]
        }
    )

    snapshot = accumulator.snapshot()

    assert snapshot.function_calls == [
        {
            "id": "call-1",
            "name": "api_call",
            "args": {"url": "https://example.com"},
        }
    ]
    assert snapshot.assistant_message is not None
    assert snapshot.assistant_message["parts"] == [
        {
            "function_call": {
                "id": "call-1",
                "name": "api_call",
                "args": {"url": "https://example.com"},
            },
            "thought_signature": "sig-1",
        }
    ]
    assert snapshot.finish_reason == "STOP"


def test_stream_accumulator_preserves_streamed_text_when_final_chunk_is_empty():
    class FakeChunk:
        def __init__(self, text):
            self.text = text

    accumulator = GeminiStreamAccumulator()
    accumulator.ingest(FakeChunk("Hello "))
    accumulator.ingest(FakeChunk("world"))
    accumulator.ingest(
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [],
                    },
                    "finish_reason": "STOP",
                }
            ]
        }
    )

    snapshot = accumulator.snapshot()

    assert snapshot.text == "Hello world"
    assert snapshot.assistant_message == {
        "role": "model",
        "parts": [{"text": "Hello world"}],
    }
    assert snapshot.finish_reason == "STOP"
