from src.autobots.llm.tools.action_code_execution_gemini import ActionCodeExecutionGemini


def test_strip_inline_artifact_data_removes_raw_base64_and_keeps_metadata():
    payload = {
        "artifact_parts": [
            {
                "inline_data": {
                    "data": "AAAABBBBCCCC",
                    "mime_type": "application/json",
                    "display_name": "random_data.json",
                }
            }
        ],
        "raw_response": {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "inlineData": {
                                    "data": "ZZZZYYYYXXXX",
                                    "mimeType": "text/html",
                                    "displayName": "report.html",
                                }
                            }
                        ]
                    }
                }
            ]
        },
        "generated_files_count": 1,
    }

    cleaned = ActionCodeExecutionGemini._strip_inline_artifact_data(payload)

    inline_part = cleaned["artifact_parts"][0]["inline_data"]
    assert "data" not in inline_part
    assert inline_part["mime_type"] == "application/json"
    assert inline_part["display_name"] == "random_data.json"

    nested_inline = cleaned["raw_response"]["candidates"][0]["content"]["parts"][0]["inlineData"]
    assert "data" not in nested_inline
    assert nested_inline["mimeType"] == "text/html"
    assert nested_inline["displayName"] == "report.html"

    assert cleaned["generated_files_count"] == 1
