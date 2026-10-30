from types import SimpleNamespace

import pytest
from openai.types.responses import ResponseOutputMessage
from openai.types.responses.response_output_text import (
    AnnotationURLCitation,
    ResponseOutputText,
)

from src.autobots.conn.openai.openai_responses.openai_responses import OpenaiResponses


@pytest.mark.asyncio
async def test_update_openai_urls_skips_url_citation_without_filename() -> None:
    """Regression test.

    OpenAI Responses API "url_citation" annotations do not have a `filename` field.
    Our sanitizer/rewriter must skip them without raising AttributeError.
    """

    annotation = AnnotationURLCitation(
        type="url_citation",
        url="https://example.com",
        title="Example",
        start_index=0,
        end_index=10,
    )

    output_text = ResponseOutputText(
        type="output_text",
        text="hello",
        annotations=[annotation],
        logprobs=[],
    )

    output_item = ResponseOutputMessage(
        id="msg_1",
        role="assistant",
        status="completed",
        type="message",
        content=[output_text],
    )

    # Minimal request object required by update_openai_urls_with_public_s3_urls
    responses_req = SimpleNamespace(
        input=[],
        sandbox_url_to_s3_url={},
        file_url_to_file_object_map={},
    )

    has_sandbox_path = await OpenaiResponses.update_openai_urls_with_public_s3_urls(
        ctx=None,
        responses_req=responses_req,
        output_item=output_item,
        client=object(),
        user_files=object(),
    )

    assert has_sandbox_path is False
