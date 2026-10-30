import itertools

import pytest

from src.autobots.core.url_utils import (
    UrlMatch,
    contains_url,
    extract_urls,
    iter_url_matches,
    mask_urls,
    unmask_urls,
)

# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _matches(text: str, **kwargs) -> list[UrlMatch]:
    return list(iter_url_matches(text, **kwargs))


def _urls(text: str, **kwargs) -> list[str]:
    return extract_urls(text, **kwargs)


def _assert_span_integrity(text: str, m: UrlMatch) -> None:
    assert 0 <= m.start < m.end <= len(text)
    assert text[m.start : m.end] == m.raw
    assert m.scheme == m.scheme.lower()
    assert m.url


# ---------------------------------------------------------------------
# Basic extraction / scheme allowlist / non-goals
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("visit https://example.com", ["https://example.com"]),
        ("visit http://example.com/", ["http://example.com/"]),
        ("ftp ftp://example.com/file.txt", ["ftp://example.com/file.txt"]),
        ("ftps FTPS://example.com/file.txt", ["FTPS://example.com/file.txt"]),
        ("s3 s3://bucket/key", ["s3://bucket/key"]),
        ("no fuzzy example.com/path", []),
        ("no protocol-relative //example.com/a", []),
        ("mailto:someone@example.com", []),
        ("tel:+123", []),
        ("data:text/plain,hi", []),
        ("javascript:alert(1)", []),
    ],
)
def test_basic_scheme_allowlist_and_non_goals(text, expected):
    assert _urls(text, unique=False) == expected


def test_allowed_schemes_override():
    text = "custom://example.com/x https://example.com"
    assert _urls(text) == ["https://example.com"]
    assert _urls(text, allowed_schemes={"https", "custom"}, unique=False) == [
        "custom://example.com/x",
        "https://example.com",
    ]


def test_case_insensitive_scheme_and_scheme_field_lower():
    text = "Go HTTPS://EXAMPLE.COM/x"
    ms = _matches(text)
    assert len(ms) == 1
    assert ms[0].raw == "HTTPS://EXAMPLE.COM/x"
    assert ms[0].url == "HTTPS://EXAMPLE.COM/x"
    assert ms[0].scheme == "https"
    _assert_span_integrity(text, ms[0])


# ---------------------------------------------------------------------
# Boundary behavior (ASCII-only blocker => CJK-friendly)
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "abchttps://example.com",
        "_https://example.com",
        "A0_https://example.com",
        "9https://example.com",
    ],
)
def test_boundary_blocks_ascii_identifier_prefix(text):
    assert _urls(text) == []


@pytest.mark.parametrize(
    "text, expected",
    [
        ("请看https://example.com/现在", ["https://example.com/现在"]),
        ("-https://example.com", ["https://example.com"]),
        (".https://example.com", ["https://example.com"]),
        ("+https://example.com", ["https://example.com"]),
        ("(https://example.com)", ["https://example.com"]),
        # CJK wrappers around URL should not leak into extracted URL
        ("【https://example.com】", ["https://example.com"]),
    ],
)
def test_boundary_allows_cjk_and_punctuation_prefix(text, expected):
    assert _urls(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("【https://example.com】", ["https://example.com"]),
        ("（https://example.com）", ["https://example.com"]),  # fullwidth parentheses
        ("「https://example.com」", ["https://example.com"]),
        ("『https://example.com』", ["https://example.com"]),
        ("《https://example.com》", ["https://example.com"]),
        ("【https://example.com/a?x=1#y】", ["https://example.com/a?x=1#y"]),
    ],
)
def test_cjk_wrappers_trimmed(text, expected):
    assert _urls(text) == expected


def test_cjk_wrapper_followed_by_text_no_space_documents_current_behavior():
    # Common-usage behavior: CJK wrapper closers are hard terminators.
    text = "【https://example.com】后续"
    assert _urls(text) == ["https://example.com"]


def test_url_in_parens_followed_by_hashtag_extracts_just_url():
    text = "(https://example.com)#hashtag"
    assert _urls(text) == ["https://example.com"]


def test_url_in_parens_followed_by_slash_text_extracts_just_url():
    text = "(https://example.com)/more"
    assert _urls(text) == ["https://example.com"]


# ---------------------------------------------------------------------
# Whitespace termination (including unicode whitespace)
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("x https://example.com y", ["https://example.com"]),
        ("x https://example.com\ny", ["https://example.com"]),
        ("x https://example.com\ty", ["https://example.com"]),
        ("x https://example.com\r\ny", ["https://example.com"]),
        ("x https://example.com\u00a0y", ["https://example.com"]),  # NBSP
        ("x https://example.com\u3000y", ["https://example.com"]),  # ideographic space
    ],
)
def test_whitespace_terminates_urls(text, expected):
    assert _urls(text) == expected


@pytest.mark.parametrize("ws", ["\v", "\f"])
def test_more_whitespace_terminators(ws):
    assert _urls(f"x https://example.com{ws}y") == ["https://example.com"]


# ---------------------------------------------------------------------
# Markdown patterns (more realistic)
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("[x](https://example.com/a)", ["https://example.com/a"]),
        ("![img](https://example.com/i.png)", ["https://example.com/i.png"]),
        ("<https://example.com/a>", ["https://example.com/a"]),
        ('[x](https://example.com/a "title")', ["https://example.com/a"]),
        # reference-style link definitions: [id]: URL
        ("[id]: https://example.com/ref\nUse [id].", ["https://example.com/ref"]),
        # blockquote
        ("> https://example.com/quoted", ["https://example.com/quoted"]),
        # list bullet without whitespace after '-'
        ("-https://example.com/bullet", ["https://example.com/bullet"]),
        # fenced code blocks (policy: extract even in code)
        ("```sh\ncurl https://example.com/x\n```", ["https://example.com/x"]),
        # inline code (policy: extract)
        ("`see https://example.com/inline`", ["https://example.com/inline"]),
        # markdown table separators (pipe is stop char)
        ("| col | https://example.com/a |", ["https://example.com/a"]),
    ],
)
def test_markdown_various_patterns(text, expected):
    assert _urls(text, unique=False) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        # Nested wrappers around markdown link
        ("See ([x](https://example.com/a)).", ["https://example.com/a"]),
        ("[x](https://example.com/a)), next", ["https://example.com/a"]),
        # Reference definitions bulk
        (
            "\n".join(
                [
                    "[a]: https://example.com/x",
                    "[b]: https://example.com/y",
                    "use [a] and [b]",
                    "[a2]: https://example.com/x",  # duplicate
                ]
            ),
            ["https://example.com/x", "https://example.com/y"],
        ),
        # Autolink inside parentheses
        ("(<https://example.com/a>)", ["https://example.com/a"]),
    ],
)
def test_more_markdown_patterns(text, expected):
    assert _urls(text, unique=True) == expected


# ---------------------------------------------------------------------
# JSON-ish / log-ish patterns
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ('{"url":"https://example.com/a"}', ["https://example.com/a"]),
        ('{"url": "https://example.com/a?b=c#d"}', ["https://example.com/a?b=c#d"]),
        (
            '{"urls":["https://example.com/a","https://example.com/b"]}',
            ["https://example.com/a", "https://example.com/b"],
        ),
        # trailing quote + comma should terminate URL cleanly
        ('{"url":"https://example.com/a"}, "x": 1', ["https://example.com/a"]),
        # typical log prefix
        ("INFO url=https://example.com/a status=200", ["https://example.com/a"]),
        # parentheses wrapper in logs
        ("(url=https://example.com/a)", ["https://example.com/a"]),
    ],
)
def test_json_and_logs(text, expected):
    assert _urls(text, unique=False) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ('<a href="https://example.com/a?x=1">link</a>', ["https://example.com/a?x=1"]),
        ("href='https://example.com/a'", ["https://example.com/a"]),
        ("src=https://example.com/a>", ["https://example.com/a"]),
        ("(https://example.com/a)</div>", ["https://example.com/a"]),
        ("https://example.com/a> trailing", ["https://example.com/a"]),
        ("https://example.com/a< trailing", ["https://example.com/a"]),
    ],
)
def test_htmlish_contexts(text, expected):
    assert _urls(text, unique=False) == expected


# ---------------------------------------------------------------------
# Stop characters: quotes, pipes, fullwidth pipes, unicode wrappers, backslash
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ('"https://example.com/a"', ["https://example.com/a"]),
        ("'https://example.com/a'", ["https://example.com/a"]),
        ("“https://example.com/a”", ["https://example.com/a"]),
        ("‘https://example.com/a’", ["https://example.com/a"]),
        ("«https://example.com/a»", ["https://example.com/a"]),
        ("https://a.com|https://b.com", ["https://a.com", "https://b.com"]),
        ("https://a.com｜https://b.com", ["https://a.com", "https://b.com"]),  # fullwidth
        (r"https://example.com/path\not-a-url", ["https://example.com/path"]),
    ],
)
def test_stop_chars(text, expected):
    assert _urls(text, unique=False) == expected


def test_ascii_apostrophe_allowed_in_path_real_world_s3_key():
    url = (
        "https://app-myautobots-public-dev.s3.amazonaws.com/files/"
        "4158d46b-ef1b-4a95-91bd-4e18bea4e562/"
        "Bebi,_Oliver_River_-_feels_like_i'll_be_ok_[11_26_25]_(1).pdf"
    )
    assert _urls(f"repeat the verbatim {url}") == [url]


def test_ascii_apostrophe_allowed_in_query():
    url = "https://example.com/search?q=rock'n'roll&x=1"
    assert _urls(f"see {url} next") == [url]


def test_apostrophe_in_netloc_is_treated_as_terminator_common_usage_possessive():
    assert _urls("See https://example.com's docs") == ["https://example.com"]


def test_single_quoted_url_followed_by_text_without_whitespace_does_not_swallow():
    # Regression guard: after allowing apostrophes inside URLs, we must not swallow
    # concatenated text in wrapper patterns like: 'url'foo
    assert _urls("See 'https://example.com/a'foo") == ["https://example.com/a"]


def test_single_quoted_url_with_internal_apostrophe_preserves_internal_apostrophe():
    # Ensure wrapper-opener handling does not terminate at internal apostrophes.
    assert _urls("See 'https://example.com/i'll_be_ok.pdf' next") == [
        "https://example.com/i'll_be_ok.pdf"
    ]


# ---------------------------------------------------------------------
# Trailing punctuation trimming (ASCII + CJK)
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("see https://example.com/a.", ["https://example.com/a"]),
        ("see https://example.com/a, next", ["https://example.com/a"]),
        ("see https://example.com/a; next", ["https://example.com/a"]),
        ("see https://example.com/a: next", ["https://example.com/a"]),
        ("see https://example.com/a!? next", ["https://example.com/a"]),
        ("see https://example.com/a… next", ["https://example.com/a"]),
        ("see https://example.com/a。 next", ["https://example.com/a"]),
        ("see https://example.com/a， next", ["https://example.com/a"]),
        ("see https://example.com/a； next", ["https://example.com/a"]),
        ("see https://example.com/a： next", ["https://example.com/a"]),
        ("see https://example.com/a！ next", ["https://example.com/a"]),
        ("see https://example.com/a？ next", ["https://example.com/a"]),
    ],
)
def test_trailing_punctuation_trim(text, expected):
    assert _urls(text) == expected


def test_query_fragment_preserved_when_not_trailing():
    assert _urls("https://example.com/a?x=1&y=2#frag next") == [
        "https://example.com/a?x=1&y=2#frag"
    ]


# ---------------------------------------------------------------------
# URL shapes: userinfo, percent-encoding, unicode domain/path
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("https://user:pass@example.com/a", ["https://user:pass@example.com/a"]),
        ("https://example.com/a%20b", ["https://example.com/a%20b"]),
        ("https://example.com/%E2%9C%93", ["https://example.com/%E2%9C%93"]),
        ("https://example.com/路径/東京", ["https://example.com/路径/東京"]),
        # unicode domain (no IDNA normalization here; urlsplit accepts it)
        ("https://bücher.example/abc", ["https://bücher.example/abc"]),
    ],
)
def test_url_shapes_unicode_and_percent(text, expected):
    assert _urls(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("https://example.com?x=1", ["https://example.com?x=1"]),
        ("https://example.com#frag", ["https://example.com#frag"]),
        ("https://example.com/?x=1#y", ["https://example.com/?x=1#y"]),
        ("https://example.com/a//b", ["https://example.com/a//b"]),
        ("https://example.com/a;b;c", ["https://example.com/a;b;c"]),
    ],
)
def test_more_url_shapes_query_fragment_and_semicolons(text, expected):
    assert _urls(text) == expected


def test_signed_url_like_query_preserved():
    url = (
        "https://example.com/file?"
        "X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=abc%2F20250101%2Fus-east-1%2Fs3%2Faws4_request"
        "&X-Amz-Date=20250101T000000Z&X-Amz-Expires=3600&X-Amz-Signature=deadbeef"
    )
    text = f"download {url} next"
    assert _urls(text) == [url]


def test_semicolon_in_query_not_split_if_next_is_not_url_start():
    text = "https://example.com/?a=1;b=2 next"
    assert _urls(text) == ["https://example.com/?a=1;b=2"]


# Document current behavior: port ranges are NOT validated by url_utils (urlsplit parses it).
@pytest.mark.parametrize(
    "text, expected",
    [
        ("https://example.com:0/a", ["https://example.com:0/a"]),
        ("https://example.com:99999/a", ["https://example.com:99999/a"]),
    ],
)
def test_ports_out_of_range_document_current_behavior(text, expected):
    assert _urls(text) == expected


# ---------------------------------------------------------------------
# Delimiters + wrapper-aware closers
# ---------------------------------------------------------------------


def test_balanced_parentheses_preserved_wikipedia_style():
    assert _urls("wiki https://en.wikipedia.org/wiki/Function_(mathematics)") == [
        "https://en.wikipedia.org/wiki/Function_(mathematics)"
    ]


def test_balanced_mixed_delimiters_preserved():
    assert _urls("u https://example.com/pdf_{hey(123[456])}.pdf end") == [
        "https://example.com/pdf_{hey(123[456])}.pdf"
    ]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("(https://example.com/a)", ["https://example.com/a"]),
        ("https://example.com/a)", ["https://example.com/a"]),
        ("https://example.com/a]", ["https://example.com/a"]),
        ("https://example.com/a}", ["https://example.com/a"]),
    ],
)
def test_unpaired_closers_end_url(text, expected):
    assert _urls(text) == expected


def test_unclosed_opener_is_kept_by_policy():
    # opener without closer is allowed (URL continues)
    assert _urls("x https://example.com/a( y") == ["https://example.com/a("]


def test_mismatched_closer_ends_url():
    # Mismatch is treated as wrapper unless next is an allowlisted continuation.
    # Next char here is 'b' => trim before ']'.
    text = "https://example.com/a(]b"
    assert _urls(text) == ["https://example.com/a("]


def test_unpaired_bracket_in_query_key_preserved_when_followed_by_equals():
    # ']' is unpaired but next char '=' is allowlisted continuation => keep it.
    text = "https://example.com/?a]=1&b=2"
    assert _urls(text) == ["https://example.com/?a]=1&b=2"]


def test_mismatched_closer_outside_query_trims_common_usage():
    # Under the refined policy, '&'/'=' continuation is only honored after '?' is seen.
    # With no '?' in this URL token, the mismatched closer is treated as wrapper-like and trims.
    text = "https://example.com/a(]&x=1"
    assert _urls(text) == ["https://example.com/a("]


def test_url_in_parens_followed_by_ampersand_text_extracts_just_url():
    # Common usage: "(url)&foo" means URL then prose ("and foo"), not URL continuation.
    text = "(https://example.com)&foo"
    assert _urls(text) == ["https://example.com"]


def test_escaped_unicode_continuation_after_closer_is_kept():
    # Here '&' is encoded as a literal backslash-u sequence, so the next char after ')'
    # is '\' in the source text. Option A should peek and treat it as continuation.
    #
    # NOTE: must use "\\u0026" (not "\u0026") in Python source to keep it literal.
    text = "x https:\\/\\/example.com\\/?q=)\\u0026x=1 y"
    assert _urls(text, decode_json_escapes=True) == ["https://example.com/?q=)&x=1"]


def test_escaped_unicode_after_closer_not_allowlisted_still_trims():
    # \u0061 -> 'a' (not allowlisted), so ')' should be treated as wrapper boundary.
    text = "x https:\\/\\/example.com\\/?q=)\\u0061x=1 y"
    assert _urls(text, decode_json_escapes=True) == ["https://example.com/?q="]


def test_escaped_continuation_with_multiple_backslashes():
    # Common in double-escaped logs: '\\u0026' appears with multiple backslashes.
    text = "x https:\\/\\/example.com\\/?q=)" + "\\\\u0026x=1 y"
    assert _urls(text, decode_json_escapes=True) == ["https://example.com/?q=)&x=1"]


def test_escaped_slash_after_closer_is_trimmed_common_usage():
    # '/' is encoded as '\/' (backslash + slash) in escaped mode.
    text = "x https:\\/\\/example.com\\/a)\\/more y"
    assert _urls(text, decode_json_escapes=True) == ["https://example.com/a"]


@pytest.mark.parametrize(
    "suffix, expected",
    [
        ("&x=1", "https://example.com/?q=)&x=1"),
        ("=x", "https://example.com/?q=)=x"),
    ],
)
def test_closer_kept_when_followed_by_allowlisted_continuation_chars(suffix, expected):
    # Continuation is only honored after '?' has occurred earlier in the token.
    text = f"x https://example.com/?q=){suffix} y"
    assert _urls(text) == [expected]


def test_unpaired_closer_followed_by_equals_outside_query_trims_common_usage():
    text = "x https://example.com/a)=x y"
    assert _urls(text) == ["https://example.com/a"]


def test_unpaired_closer_followed_by_ampersand_outside_query_trims_common_usage():
    text = "x https://example.com/a)&x=1 y"
    assert _urls(text) == ["https://example.com/a"]


def test_escaped_ampersand_outside_query_does_not_keep_closer():
    # \u0026 -> '&' but query has not started (no literal '?'), so do not keep ')'.
    text = "x https:\\/\\/example.com\\/a)\\u0026x=1 y"
    assert _urls(text, decode_json_escapes=True) == ["https://example.com/a"]


def test_escaped_equals_outside_query_does_not_keep_closer():
    # \u003d -> '=' but query has not started (no literal '?'), so do not keep ')'.
    text = "x https:\\/\\/example.com\\/a)\\u003dx=1 y"
    assert _urls(text, decode_json_escapes=True) == ["https://example.com/a"]


def test_mismatched_closer_kept_inside_query_when_followed_by_ampersand():
    # Query has started (seen '?'), and next char after mismatched ']' is '&' => keep.
    text = "https://example.com/?q=(]&x=1"
    assert _urls(text) == ["https://example.com/?q=(]&x=1"]


def test_escaped_question_mark_does_not_enable_query_continuation_documented():
    # Document current behavior: seen_qmark only triggers on literal '?' in the source text.
    # Here '?' is encoded as \u003f, so the later \u0026 does NOT keep the ')'.
    text = "x https:\\/\\/example.com\\/\\u003fq=)\\u0026x=1 y"
    assert _urls(text, decode_json_escapes=True) == ["https://example.com/?q="]


def test_balanced_paren_then_slash_is_preserved():
    assert _urls("x https://example.com/a(b)/c y") == ["https://example.com/a(b)/c"]


def test_balanced_paren_then_fragment_is_preserved():
    assert _urls("x https://example.com/a(b)#frag y") == ["https://example.com/a(b)#frag"]


def test_escaped_unicode_equals_continuation_after_closer_is_kept():
    # '=' encoded as \u003d, continuation should be honored because '?' already occurred.
    text = "x https:\\/\\/example.com\\/?q=)\\u003dx=1 y"
    assert _urls(text, decode_json_escapes=True) == ["https://example.com/?q=)=x=1"]


def test_unpaired_closer_followed_by_question_mark_is_treated_as_punctuation_common_usage():
    # Common text pattern: URL wrapped in parentheses followed by a '?' punctuation.
    # We should NOT swallow '?x=1' as if it were a query continuing the URL.
    text = "x https://example.com/a)?x=1 y"
    assert _urls(text) == ["https://example.com/a"]


def test_balanced_parentheses_then_query_is_preserved():
    # Legit URL case: ')' belongs to the URL because it matches an earlier '('.
    text = "x https://example.com/a(b)?x=1 y"
    assert _urls(text) == ["https://example.com/a(b)?x=1"]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("https://example.com/a)next", ["https://example.com/a"]),
        ("https://example.com/?q=)foo", ["https://example.com/?q="]),
        ("https://example.com/a]Z", ["https://example.com/a"]),
    ],
)
def test_option_a_non_allowlisted_following_char_trims(text, expected):
    assert _urls(text) == expected


def test_unpaired_closer_inside_query_truncates_by_policy():
    assert _urls("x https://example.com/?q=) y") == ["https://example.com/?q="]


def test_unpaired_closer_inside_query_not_truncated_when_followed_by_query_continuation():
    # Option A: ')' followed by '&' is treated as part of URL (common query continuation).
    text = "x https://example.com/?q=)&x=1 y"
    assert _urls(text) == ["https://example.com/?q=)&x=1"]


def test_wrapper_closer_followed_by_word_is_trimmed_not_swallowed():
    # Prevent regression: should NOT become "https://example.com/a)foo"
    text = "(https://example.com/a)foo"
    assert _urls(text) == ["https://example.com/a"]


def test_balanced_parentheses_in_query_preserved():
    assert _urls("x https://example.com/?q=(a,b)&x=1 y") == ["https://example.com/?q=(a,b)&x=1"]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("https://example.com/(a))", ["https://example.com/(a)"]),
        ("https://example.com/[a]]", ["https://example.com/[a]"]),
        ("https://example.com/{a}}", ["https://example.com/{a}"]),
        ("(https://example.com/(a[b]{c}))", ["https://example.com/(a[b]{c})"]),
        ("https://example.com/a(b[c{d}e]f)g", ["https://example.com/a(b[c{d}e]f)g"]),
    ],
)
def test_delimiter_stack_more_cases(text, expected):
    assert _urls(text) == expected


# ---------------------------------------------------------------------
# Adjacent URL splitting: , ;  (normal and escaped)
# ---------------------------------------------------------------------


def test_adjacent_urls_split_on_comma_and_semicolon():
    text = "https://a.com,https://b.com;https://c.com"
    assert _urls(text, unique=False) == ["https://a.com", "https://b.com", "https://c.com"]


def test_adjacent_urls_split_when_wrapped():
    text = "(https://a.com),(https://b.com)"
    assert _urls(text, unique=False) == ["https://a.com", "https://b.com"]


def test_adjacent_urls_split_on_wrapper_closer_then_next_scheme():
    text = "https://a.com)https://b.com"
    assert _urls(text, unique=False) == ["https://a.com", "https://b.com"]


def test_comma_in_query_can_split_if_next_is_url_start():
    # Documents current behavior
    text = "https://example.com/?q=1,https://evil.com/x"
    assert _urls(text, unique=False) == ["https://example.com/?q=1", "https://evil.com/x"]


# ---------------------------------------------------------------------
# JSON-escaped URLs (flagged), including literal backslash-u sequences
# ---------------------------------------------------------------------


def test_escaped_urls_not_extracted_by_default():
    text = r'{"u":"https:\/\/example.com\/a?x=1"}'
    assert _urls(text, decode_json_escapes=False) == []


def test_escaped_slash_url_extracted_when_flag_enabled():
    text = r'{"u":"https:\/\/example.com\/a?x=1#y"}'
    assert _urls(text, decode_json_escapes=True) == ["https://example.com/a?x=1#y"]


def test_double_escaped_slash_url_extracted_when_flag_enabled():
    text = r'{"u":"https:\\/\\/example.com\\/a"}'
    assert _urls(text, decode_json_escapes=True) == ["https://example.com/a"]


def test_unicode_escaped_slash_url_extracted_when_flag_enabled():
    # IMPORTANT: Python interprets \uXXXX even in raw strings. Use \\u in the literal.
    text = '{"u":"https:\\\\u002F\\\\u002Fexample.com\\\\u002Fa\\\\u0026b=c"}'
    assert _urls(text, decode_json_escapes=True) == ["https://example.com/a&b=c"]


def test_unicode_escape_to_space_is_rejected_by_validation():
    # After decoding: "https://example.com/a b" -> contains whitespace -> must be rejected
    text = "https:\\u002F\\u002Fexample.com\\u002Fa\\u0020b"
    assert _urls(text, decode_json_escapes=True) == []


@pytest.mark.parametrize("validate", [False, True])
@pytest.mark.parametrize("decode_json_escapes", [False, True])
def test_escaped_flag_matrix(validate, decode_json_escapes):
    text = r"https:\/\/example.com\/a"
    got = _urls(text, validate=validate, decode_json_escapes=decode_json_escapes, unique=False)
    assert got == (["https://example.com/a"] if decode_json_escapes else [])


def test_escaped_adjacency_split():
    text = r"https:\/\/a.com\/x,https:\/\/b.com\/y"
    assert _urls(text, decode_json_escapes=True, unique=False) == [
        "https://a.com/x",
        "https://b.com/y",
    ]


def test_iter_url_matches_span_raw_vs_decoded_url():
    text = r"xx https:\/\/example.com\/a yy"
    ms = _matches(text, decode_json_escapes=True)
    assert len(ms) == 1
    m = ms[0]
    _assert_span_integrity(text, m)
    assert m.raw == r"https:\/\/example.com\/a"
    assert m.url == "https://example.com/a"
    assert m.scheme == "https"


# ---------------------------------------------------------------------
# IPv6 bracketed literal (reject by default; allow with flag)
# ---------------------------------------------------------------------


def test_ipv6_rejected_by_default():
    assert _urls("http://[::1]/x") == []


def test_ipv6_allowed_with_flag():
    assert _urls("http://[::1]/x", allow_ipv6=True) == ["http://[::1]/x"]


def test_ipv6_userinfo_and_port():
    text = "http://user@[::1]:8080/x"
    assert _urls(text) == []
    assert _urls(text, allow_ipv6=True) == ["http://user@[::1]:8080/x"]


def test_malformed_ipv6_bracket_not_treated_as_ipv6_literal():
    # netloc starts with '[' but has no ']' => our bracketed check requires ']' so it's not "IPv6 literal"
    # still likely invalid by urlsplit/hostname rules => should be rejected by validation
    assert _urls("http://[notclosed/x") == []


# ---------------------------------------------------------------------
# s3 “forms” with colons should not be misdetected as IPv6
# ---------------------------------------------------------------------


def test_s3_colon_heavy_netloc_is_not_rejected_as_ipv6():
    text = "s3://arn:aws:s3:::mybucket/path/to/key"
    assert _urls(text) == ["s3://arn:aws:s3:::mybucket/path/to/key"]


# ---------------------------------------------------------------------
# Validation toggles
# ---------------------------------------------------------------------


def test_validation_drops_missing_netloc():
    text = "broken https:///path ok https://example.com/a"
    assert _urls(text, validate=True) == ["https://example.com/a"]


def test_validate_false_returns_candidates_anyway():
    text = "broken https:///path ok https://example.com/a"
    urls = _urls(text, validate=False, unique=False)
    assert "https:///path" in urls
    assert "https://example.com/a" in urls


@pytest.mark.parametrize(
    "text",
    [
        "https:///path",
        "ftp:///file",
        "s3:///bucket/key",
    ],
)
def test_validate_false_keeps_missing_netloc_candidates(text):
    assert _urls(text, validate=False, unique=False) == [text]
    assert _urls(text, validate=True, unique=False) == []


# ---------------------------------------------------------------------
# Guardrails: max_urls, max_url_len, slack, cap-hit discard
# ---------------------------------------------------------------------


def test_max_urls_caps_results():
    text = " ".join([f"https://example.com/{i}" for i in range(10)])
    assert _urls(text, max_urls=3, unique=False) == [
        "https://example.com/0",
        "https://example.com/1",
        "https://example.com/2",
    ]


def test_max_url_len_allows_trailing_punctuation_within_slack():
    base = "https://example.com/"
    max_len = 40
    tail_len = max_len - len(base)
    assert tail_len > 0
    url = base + ("a" * tail_len)
    text = url + ". next"
    assert _urls(text, max_url_len=max_len) == [url]


def test_scan_cap_discards_truncated_prefix_when_token_continues():
    max_len = 50
    prefix = "https://example.com/"
    long_tail = "a" * 2000
    text = prefix + long_tail + " END"  # terminator exists but far beyond cap
    assert _urls(text, max_url_len=max_len) == []


def test_scan_cap_with_closer_near_cap_still_discards_truncated_prefix():
    # Regression guard for Option A + scan cap:
    # Ensure we don't accept a truncated prefix when a closer appears near the scan cap
    # and token continues beyond it.
    max_len = 60
    base = "https://example.com/?q="
    text = base + ("a" * 2000) + ")&x=1 END"
    assert _urls(text, max_url_len=max_len) == []


def test_overlong_url_dropped_but_following_url_kept():
    long_path = "a" * 500
    text = f"https://example.com/{long_path} https://ok.com/x"
    assert _urls(text, max_url_len=64, unique=False) == ["https://ok.com/x"]


def test_max_url_len_exact_boundary_passes():
    url = "https://example.com/" + ("a" * 10)
    assert _urls(url, max_url_len=len(url)) == [url]


def test_max_url_len_one_over_drops():
    url = "https://example.com/" + ("a" * 11)
    assert _urls(url, max_url_len=len(url) - 1) == []


def test_max_urls_not_exhausted_by_nested_urls_in_query():
    text = "https://example.com/redirect?next=https://evil.com/x https://ok.com"
    assert _urls(text, unique=False) == [
        "https://example.com/redirect?next=https://evil.com/x",
        "https://ok.com",
    ]


# ---------------------------------------------------------------------
# Spans + mask/unmask correctness
# ---------------------------------------------------------------------


def test_span_integrity_normal():
    text = 'A ("https://example.com/a?x=1"), B <https://b.com>'
    ms = _matches(text)
    assert [m.url for m in ms] == ["https://example.com/a?x=1", "https://b.com"]
    for m in ms:
        _assert_span_integrity(text, m)


def test_mask_unmask_roundtrip():
    text = "A https://example.com/a and https://example.com/b."
    masked, mapping = mask_urls(text)
    assert masked != text
    assert len(mapping) == 2
    assert unmask_urls(masked, mapping) == text


def test_mask_handles_duplicate_urls_separately():
    text = "x https://example.com y https://example.com z"
    masked, mapping = mask_urls(text)
    assert len(mapping) == 2
    assert unmask_urls(masked, mapping) == text


def test_mask_preserves_surrounding_punctuation():
    text = "(https://example.com/a)."
    masked, mapping = mask_urls(text)
    assert masked.startswith("(")
    assert masked.endswith(").")
    assert unmask_urls(masked, mapping) == text


def test_mask_unmask_roundtrip_preserves_raw_escaped_url():
    text = r"x https:\/\/example.com\/a y"
    masked, mapping = mask_urls(text, decode_json_escapes=True)
    assert unmask_urls(masked, mapping) == text
    # mapping values should store raw (escaped) substring, not decoded.
    assert list(mapping.values())[0] == r"https:\/\/example.com\/a"


def test_matches_are_sorted_and_non_overlapping():
    text = "x https://a.com y https://b.com z https://c.com"
    ms = _matches(text)
    assert ms == sorted(ms, key=lambda m: (m.start, m.end))
    for a, b in zip(ms, ms[1:]):
        assert a.end <= b.start
        _assert_span_integrity(text, a)


# ---------------------------------------------------------------------
# Wrapper combinator “fuzz” (deterministic): ensure common wrappers don’t leak into URL
# ---------------------------------------------------------------------


@pytest.mark.parametrize("url", ["https://example.com/a", "https://example.com/wiki/Foo_(bar)"])
def test_common_wrapper_combinations(url):
    prefixes = ["", "(", "[", "{", '"', "'", "“", "«", "<", "> "]
    suffixes = ["", ")", "]", "}", '"', "'", "”", "»", ">", ".", ",", ").", "],", "}."]

    # We don't want to over-promise exact behavior for every wrapper combo,
    # but we at least want: extracted URL starts with the URL and validates.
    for pre, suf in itertools.product(prefixes, suffixes):
        text = f"{pre}{url}{suf}"
        got = _urls(text)
        # Some combos are invalid by construction (e.g., prefix with '>' then no boundary),
        # so only assert when we actually match something.
        if got:
            assert got[0].startswith("https://example.com")


# ---------------------------------------------------------------------
# contains_url pretest
# ---------------------------------------------------------------------


def test_contains_url_basic():
    assert contains_url("x https://a.com y") is True
    assert contains_url("no urls here") is False


def test_contains_url_escaped_pretest():
    text = r"https:\/\/example.com\/x"
    assert contains_url(text, decode_json_escapes=False) is False
    assert contains_url(text, decode_json_escapes=True) is True


def test_contains_url_is_heuristic_not_authoritative():
    # contains_url only checks for "://" (and optionally ':\' in escaped mode)
    assert contains_url("x foo://bar y") is True
    # but extraction should still respect allowed_schemes
    assert _urls("x foo://bar y") == []


def test_kitchen_sink_brutal_real_world_blob_common_usage_policy():
    text = "\n".join(
        [
            "Intro: (https://example.com)#hashtag and (https://example.com)/more.",
            "CJK: 【https://cjk.com/路径】后续 and 「https://cjk2.com/a」后续.",
            "Markdown: [md](https://md.com/a) and ![i](https://img.com/i.png) and <https://auto.com/path>.",
            "Quote: “https://quotes.com/q?x=1”.",
            'HTML-ish: <a href="https://html.com/a?x=1&y=2">x</a>',
            "Log: INFO url=https://log.com/a)next",
            "Balanced: https://en.wikipedia.org/wiki/Foo_(bar)#Baz and https://example.com/a(b)/c",
            "Redirect: https://redir.com/redirect?next=https://evil.com/x then https://after.com",
            "Query-cont: https://qc.com/?q=)&x=1;",
            # Escaped inside query: keep ) because '?' occurred and next decodes to '&'
            "EscapedQuery: https:\\/\\/esc.com\\/?q=)\\u0026x=1",
            # Outside query: do NOT keep ')' even if next decodes to '&'
            "OutsideCont: (https://noq.com)&foo; EscOutside: https:\\/\\/noq2.com\\/a)\\u0026x=1",
            "Adj: https://a.com,https://b.com;https://c.com and https://d.com)https://e.com",
            # JSON with duplicates + array
            'JSON: {"u":"https:\\/\\/json.com\\/p?q=1\\u0026x=2","arr":["https:\\/\\/json.com\\/p?q=1\\u0026x=2","https:\\/\\/json2.com\\/x"]}',
            # Unicode-escaped start outside JSON and inside JSON (double-backslash form)
            "UNI: https:\\u002F\\u002Funi.com\\u002Fa\\u0026b=c and "
            + '{"u":"https:\\\\u002F\\\\u002Funi2.com\\\\u002Fa"}',
            # Real-world S3 key containing ASCII apostrophe in the path (regression case)
            "S3KeyApos: "
            "https://app-myautobots-public-dev.s3.amazonaws.com/files/"
            "4158d46b-ef1b-4a95-91bd-4e18bea4e562/"
            "Bebi,_Oliver_River_-_feels_like_i'll_be_ok_[11_26_25]_(1).pdf",
            "More: ftp://ftp.example.com/file.txt FTPS://Case.COM/Path s3://arn:aws:s3:::mybucket/path/to/key",
            "Local: http://localhost:8000/x http://127.0.0.1:8000/x https://example.com:99999/a",
            "Noise: //proto.relative.com https:// example.com file:///etc/passwd mailto:a@b.com http://[::1]/x www.fuzzy.com",
            r"Backslash terminate: https://back.com/a\not-a-url",
            "Table: | https://pipe.com/x | https://pipe.com/y |",
        ]
    )

    expected = [
        "https://example.com",
        "https://example.com",
        "https://cjk.com/路径",
        "https://cjk2.com/a",
        "https://md.com/a",
        "https://img.com/i.png",
        "https://auto.com/path",
        "https://quotes.com/q?x=1",
        "https://html.com/a?x=1&y=2",
        "https://log.com/a",
        "https://en.wikipedia.org/wiki/Foo_(bar)#Baz",
        "https://example.com/a(b)/c",
        "https://redir.com/redirect?next=https://evil.com/x",
        "https://after.com",
        "https://qc.com/?q=)&x=1",
        "https://esc.com/?q=)&x=1",
        "https://noq.com",
        "https://noq2.com/a",
        "https://a.com",
        "https://b.com",
        "https://c.com",
        "https://d.com",
        "https://e.com",
        "https://json.com/p?q=1&x=2",
        "https://json.com/p?q=1&x=2",
        "https://json2.com/x",
        "https://uni.com/a&b=c",
        "https://uni2.com/a",
        "https://app-myautobots-public-dev.s3.amazonaws.com/files/"
        "4158d46b-ef1b-4a95-91bd-4e18bea4e562/"
        "Bebi,_Oliver_River_-_feels_like_i'll_be_ok_[11_26_25]_(1).pdf",
        "ftp://ftp.example.com/file.txt",
        "FTPS://Case.COM/Path",
        "s3://arn:aws:s3:::mybucket/path/to/key",
        "http://localhost:8000/x",
        "http://127.0.0.1:8000/x",
        "https://example.com:99999/a",
        "https://back.com/a",
        "https://pipe.com/x",
        "https://pipe.com/y",
    ]

    got = _urls(text, unique=False, decode_json_escapes=True)
    assert got == expected

    ms = _matches(text, decode_json_escapes=True)
    assert [m.url for m in ms] == expected
    for m in ms:
        _assert_span_integrity(text, m)

    masked, mapping = mask_urls(text, decode_json_escapes=True)
    assert unmask_urls(masked, mapping) == text
