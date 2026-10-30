from src.autobots.conn.gemini.chat_model_gemini import GeminiChatConfig
from src.autobots.conn.gemini.gemini_chat import GeminiChatRuntime


def test_rebuild_working_messages_from_history_strips_old_tool_plumbing():
    config = GeminiChatConfig(
        pinned_messages=[
            {
                "role": "user",
                "parts": [{"text": "Pinned instruction"}],
            }
        ],
        turn_history=[
            {
                "turn": 1,
                "messages": [
                    {
                        "role": "user",
                        "parts": [{"text": "Find my last 5 emails"}],
                    },
                    {
                        "role": "model",
                        "parts": [
                            {
                                "function_call": {
                                    "id": "call-1",
                                    "name": "api_call",
                                    "args": {"url": "https://gmail.googleapis.com/..."},
                                }
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "parts": [
                            {
                                "function_response": {
                                    "id": "call-1",
                                    "name": "api_call",
                                    "response": {"ok": True, "text": "{... giant gmail payload ...}"},
                                }
                            }
                        ],
                    },
                    {
                        "role": "model",
                        "parts": [{"text": "Here is the overview of your emails."}],
                    },
                ],
            }
        ],
    )

    GeminiChatRuntime.rebuild_working_messages_from_history(config)

    assert config.messages == [
        {"role": "user", "parts": [{"text": "Pinned instruction"}]},
        {"role": "user", "parts": [{"text": "Find my last 5 emails"}]},
        {"role": "model", "parts": [{"text": "Here is the overview of your emails."}]},
    ]


def test_rebuild_working_messages_from_history_keeps_non_tool_conversational_parts():
    file_uri = "https://generativelanguage.googleapis.com/v1beta/files/report"
    config = GeminiChatConfig(
        turn_history=[
            {
                "turn": 1,
                "messages": [
                    {
                        "role": "user",
                        "parts": [
                            {"file_data": {"file_uri": file_uri, "mime_type": "application/pdf"}},
                            {"text": "Please summarize this PDF."},
                        ],
                    },
                    {
                        "role": "model",
                        "parts": [
                            {"text": "This PDF is about quarterly performance.", "thought_signature": "abc"},
                            {"function_call": {"id": "call-2", "name": "api_call", "args": {}}},
                        ],
                    },
                ],
            }
        ]
    )

    GeminiChatRuntime.rebuild_working_messages_from_history(config)

    assert len(config.messages) == 2
    assert config.messages[0]["parts"][0]["file_data"]["file_uri"] == file_uri
    assert config.messages[0]["parts"][1]["text"] == "Please summarize this PDF."
    assert config.messages[1]["parts"] == [
        {"text": "This PDF is about quarterly performance.", "thought_signature": "abc"}
    ]
