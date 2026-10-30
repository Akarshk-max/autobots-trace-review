from google.genai import types

from src.autobots.conn.gemini.message_normalizer_gemini import GeminiMessageNormalizer


def test_normalize_parts_drops_role_only_part():
    parts = [
        {"text": "hello"},
        {"role": "model"},
    ]

    assert GeminiMessageNormalizer.normalize_parts(parts) == [{"text": "hello"}]


def test_normalize_parts_strips_role_from_mixed_part():
    parts = [
        {"role": "model", "text": "hello"},
    ]

    assert GeminiMessageNormalizer.normalize_parts(parts) == [{"text": "hello"}]


def test_normalize_messages_produces_generate_content_compatible_contents():
    # If invalid parts leak into history, google-genai will raise a pydantic ValidationError.
    messages = [
        {
            "role": "user",
            "parts": [
                {"text": "hello"},
                {"role": "model"},
                {"role": "model", "text": "still ok"},
            ],
        }
    ]

    normalized = GeminiMessageNormalizer.normalize_messages(messages)

    # Should validate against the same schema the SDK uses for requests.
    types._GenerateContentParameters(model="gemini-2.0-flash", contents=normalized)
