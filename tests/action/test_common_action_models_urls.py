from __future__ import annotations

from src.autobots.action.action.common_action_models import DataBlock, Metadata
from src.autobots.data_model.context import Context


def test_get_llm_prompt_end_to_end_extracts_urls_and_appends_to_prompt():
    """
    In-process end-to-end test of the real production call chain:
      DataBlock.get_llm_prompt -> process_url_in_text (guarded by metadata flag)
      -> urls populated -> prompt includes the URLs list.

    This locks in the refactor to url_utils while avoiding any external dependencies.
    """
    ctx = Context(user_id="test-user")
    b = DataBlock(
        text=(
            "Intro: [md](https://md.com/a) and <https://auto.com/p>.\n"
            "CJK: 【https://cjk.com/路径】后续\n"
            "Wrappers: (https://ex.com)#hashtag (https://ex2.com)/more\n"
            "Query-cont: https://qc.com/?q=)&x=1\n"
            "Noise: ftp://ftp.example.com/file.txt //proto.relative.com www.fuzzy.com\n"
            "IPv6: http://[::1]/x\n"
            "Escaped (should NOT decode here): https:\\/\\/escaped.com\\/a?x=1\n"
        ),
        metadata=Metadata(process_url_in_text=True),
    )

    prompt = b.get_llm_prompt(ctx)

    # Callsite policy: http/https only, no escaped decoding, no IPv6
    assert b.urls == [
        "https://md.com/a",
        "https://auto.com/p",
        "https://cjk.com/路径",
        "https://ex.com",
        "https://ex2.com",
        "https://qc.com/?q=)&x=1",
    ]

    # Prompt should contain original text and appended URLs
    assert "Intro: [md](https://md.com/a)" in prompt
    assert "User-uploaded file URLs:" in prompt
    for u in b.urls:
        assert u in prompt


def test_get_llm_prompt_does_not_extract_when_flag_false():
    ctx = Context(user_id="test-user")
    b = DataBlock(
        text="See https://example.com and [x](https://a.com).",
        metadata=Metadata(process_url_in_text=False),
    )

    prompt = b.get_llm_prompt(ctx)

    # Since flag is false, we should not mutate b.urls and no URL list is appended
    assert b.urls == []
    assert "User-uploaded file URLs:" not in prompt


def test_process_url_in_text_http_https_only_policy_lock():
    """
    Regression guard: DataBlock.process_url_in_text must keep the old scope
    (http/https only) even though url_utils supports more schemes.
    """
    ctx = Context(user_id="test-user")
    b = DataBlock(
        text="ftp://ftp.example.com/x s3://bucket/key https://ok.com/x",
        metadata=Metadata(process_url_in_text=True),
    )
    b.process_url_in_text(ctx)
    assert b.urls == ["https://ok.com/x"]


def test_process_url_in_text_dedup_and_stable_order_across_calls():
    ctx = Context(user_id="test-user")
    b = DataBlock(
        text="https://a.com https://b.com https://a.com",
        metadata=Metadata(process_url_in_text=True),
    )

    b.process_url_in_text(ctx)
    assert b.urls == ["https://a.com", "https://b.com"]

    # Call again should not append duplicates
    b.process_url_in_text(ctx)
    assert b.urls == ["https://a.com", "https://b.com"]


def test_process_url_in_text_does_not_decode_json_escapes_policy_lock():
    """
    Callsite policy: decode_json_escapes=False.
    So JSON-escaped forms should not be extracted from text here.
    """
    ctx = Context(user_id="test-user")
    b = DataBlock(
        text=r'{"u":"https:\/\/escaped.com\/a?x=1"} and https://plain.com/x',
        metadata=Metadata(process_url_in_text=True),
    )
    b.process_url_in_text(ctx)
    assert b.urls == ["https://plain.com/x"]


def test_process_url_in_text_wrapper_common_usage_policy_lock():
    """
    Locks your common-usage wrapper expectations at the callsite level:
      (url)#hashtag -> url
      (url)/more -> url
    """
    ctx = Context(user_id="test-user")
    b = DataBlock(
        text="(https://example.com)#hashtag (https://example.com)/more",
        metadata=Metadata(process_url_in_text=True),
    )
    b.process_url_in_text(ctx)
    assert b.urls == ["https://example.com"]
