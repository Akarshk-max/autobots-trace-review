# src/autobots/core/url_utils.py
r"""
Centralized URL extraction utilities (regex-only, span-first).

Why this module exists
----------------------
The codebase previously had multiple ad-hoc URL regexes with slightly different
behaviors (and different bugs). This module is intended to be the single source
of truth for:

- extracting URLs from free-form user text (including Markdown/JSON-like text)
- applying the project-specific delimiter rule:
    - balanced (), [], {} inside URLs are allowed
    - delimiter handling is wrapper-aware:
        - balanced (), [], {} inside URLs are allowed and preserved
        - an *unpaired* or *mismatched* closing delimiter (')', ']', '}') is treated as an
          end-of-URL **only when it looks like wrapper punctuation** (e.g., followed by
          whitespace/end-of-text, a quote, a stop character like '<', '>', '`', '|', or
          trailing punctuation)
        - if the closing delimiter is followed by a strong URL-continuation signal
          (typically '&' or '=' inside an already-started query string; or an escaped form
          that decodes to '&'/'=' when decode_json_escapes=True), it is kept as part of the URL token
- validating candidates using urllib.parse.urlsplit()
- returning spans (start/end) so callers can mask/restore URLs safely

Policy summary
--------------
- Explicit scheme only: scheme://... (no fuzzy links, no protocol-relative //...).
- Allowed schemes are configurable; defaults are "online" schemes + s3.
- Optional JSON-escaped support behind a flag (default False):
    - https:\/\/example.com\/a
    - https:\\/\\/example.com\\/a
    - https:\u002f\u002fexample.com\u002fa
  NOTE: When enabled, we do NOT decode the whole text (to preserve spans).
        We match escaped substrings and decode only within those matches.
- Optional IPv6 acceptance behind a flag (default False).
- Guardrails: max URL length and max number of URLs per input.

Design notes (performance & correctness)
----------------------------------------
This module deliberately uses a "start-marker regex + forward scan" approach:

1) A cached regex finds ONLY URL start markers like: <scheme>://
2) Python code scans forward linearly to determine the end:
   - stops on whitespace and common wrapper separators
   - applies delimiter stack logic for (), [], {} and wrapper-aware closer handling:
       - balanced delimiters are preserved
       - unpaired/mismatched closers are treated as wrappers only when boundary-like
   - prevents one URL from swallowing the next when two URLs are adjacent like:
       https://a.com,https://b.com

This avoids heavy "RFC-perfect" regexes (hard to maintain and can be slow).

Hardening improvements
----------------------
- Scan window is capped by max_url_len (+ slack) to bound worst-case work even if no terminator appears.
- Search position advances to the end of the accepted URL to avoid re-scanning inside the same URL
  (important for redirect-style URLs that contain nested 'http://' in query params).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Collection, Dict, Iterator, List, Tuple
from urllib.parse import urlsplit

# ---------------------------------------------------------------------------
# Defaults / guardrails
# ---------------------------------------------------------------------------

DEFAULT_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https", "ftp", "ftps", "s3"})

# Hardening to prevent pathological inputs from hurting performance/memory.
DEFAULT_MAX_URL_LEN = 16_384
DEFAULT_MAX_URLS = 500

# When max_url_len is enabled, allow a small slack window during scanning so we can:
# - scan and then trim trailing punctuation (e.g., "https://x.com/a).")
# - keep worst-case scan time bounded
_SCAN_SLACK = 32

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UrlMatch:
    r"""
    A span-aware URL match extracted from text.

    Attributes:
        start: Start index (inclusive) in the original text.
        end: End index (exclusive) in the original text.
        raw: Exact substring from the original text after trimming.
             - In JSON-escaped mode, this remains the escaped substring.
        url: Normalized URL value to store/use downstream.
             - In normal mode, this is typically equal to raw.
             - In JSON-escaped mode, this is decoded (\/ -> /, \uXXXX -> unicode char).
        scheme: Lowercased scheme (e.g., "https", "s3").
    """

    start: int
    end: int
    raw: str
    url: str
    scheme: str


# ---------------------------------------------------------------------------
# Terminators / wrappers
# ---------------------------------------------------------------------------
#
# We treat these as terminators while scanning:
# - whitespace (any unicode whitespace)
# - quotes/backticks (JSON/prose/code)
# - angle brackets (Markdown/HTML autolinks)
# - pipes (Markdown tables, logs)
# - common unicode quotes/guillmets found in user text
#
# We intentionally do NOT treat ')', ']', '}' as terminators because we want to allow
# balanced delimiters inside URLs (Wikipedia-style). Unpaired/mismatched closers are
# handled by the delimiter stack rule.
#

# Stop chars for normal URLs (backslash ends the URL).
# NOTE: We intentionally do NOT include ASCII apostrophe (') here.
# In practice it commonly appears in URL paths/queries (e.g. S3 object keys and
# user-uploaded filenames). Treating it as a universal stop char truncates
# legitimate URLs, so we handle it explicitly in the scanner with heuristics.
_STOP_CHARS_NORMAL = set("<>\"`|") | {"\uff5c"}  # include FULLWIDTH VERTICAL LINE
# Stop chars for escaped URLs (backslash allowed so we can capture '\/' and '\u002F').
_STOP_CHARS_ESCAPED = set("<>\"`|") | {"\uff5c"}

# Unicode wrappers/quotes commonly used in rich text (smart quotes, guillemets, etc.).
_UNICODE_WRAPPERS = set("“”‘’«»‹›")
_STOP_CHARS_NORMAL |= _UNICODE_WRAPPERS
_STOP_CHARS_ESCAPED |= _UNICODE_WRAPPERS

# Trailing punctuation that often follows URLs in prose/JSON.
# (We trim it after scanning. We do not include closers here; closers are handled via the
# delimiter stack rule.)
_TRAILING_PUNCTUATION = set(
    [
        ".",
        ",",
        ";",
        ":",
        "!",
        "?",
        "…",  # ellipsis
        "。",  # CJK full stop
        "，",  # CJK comma
        "；",
        "：",
        "！",
        "？",
    ]
)

# Common CJK / fullwidth “wrapper” characters that often enclose URLs in user text.
# We treat these like quotes/angle brackets in the sense that they should not be
# considered part of the URL when they appear at the end of the token.
#
# Examples:
#   【https://example.com】
#   （https://example.com）
#   「https://example.com」
#   『https://example.com』
#   《https://example.com》
#
# Common-usage policy:
# - We trim these if they appear at the end of a token.
# - We ALSO treat them as hard terminators during scanning to prevent swallowing
#   no-space trailing text in CJK writing, e.g. "【https://example.com】后续".
_TRAILING_CJK_WRAPPERS = set("】）》］｝〉》」』」』”’」』】）］｝")

# Common-usage behavior: treat CJK wrapper closers as hard terminators too, so we don't
# swallow no-space trailing text into URLs, e.g. "【https://example.com】后续".
_STOP_CHARS_NORMAL |= _TRAILING_CJK_WRAPPERS
_STOP_CHARS_ESCAPED |= _TRAILING_CJK_WRAPPERS

# Delimiter pairs we treat as "balance-aware" in URLs.
_OPENERS = "([{"
_CLOSERS = ")]}"
_MATCHING_CLOSER_FOR_OPENER = {"(": ")", "[": "]", "{": "}"}

# Option A continuation allowlist:
# If an unpaired/mismatched closer ')', ']', '}' is immediately followed by one of these
# characters, treat it as part of the URL token rather than wrapper punctuation.
#
# This preserves common query continuations like:
#   https://example.com/?q=)&x=1
#
# We keep this list intentionally small to avoid swallowing wrapper patterns like:
#   (https://example.com/a)foo   -> should not become https://example.com/a)foo
_URL_CONTINUATION_AFTER_CLOSER = frozenset("&=")


# ---------------------------------------------------------------------------
# Allowed scheme normalization + cached regex compilation
# ---------------------------------------------------------------------------


def _sanitize_allowed_schemes(allowed_schemes: Collection[str]) -> Tuple[str, ...]:
    """
    Normalize schemes to lowercase, remove empties, and return a stable tuple for caching.

    We sort by length descending so that alternations prefer longer schemes first
    (e.g., https before http) when compiled into regex.
    """
    schemes: List[str] = []
    for s in allowed_schemes or ():
        s2 = (s or "").strip().lower()
        if s2:
            schemes.append(s2)
    # stable + hashable + prefer longer first
    return tuple(sorted(set(schemes), key=len, reverse=True))


@lru_cache(maxsize=64)
def _compile_scheme_start_re(schemes_key: Tuple[str, ...]) -> re.Pattern[str]:
    """
    Compile a regex that finds URL start markers for allowed hierarchical schemes:

        <boundary><scheme>://

    Boundary rule:
    - CJK-friendly: we block only ASCII word-ish characters (A-Za-z0-9_) immediately before
      the scheme. This prevents matching inside typical identifiers like:
        abchttps://example.com
        _https://example.com
      while allowing URLs immediately after CJK characters (common in no-space text), e.g.:
        请看https://example.com
      and still allowing punctuation like:
        -https://example.com
        .https://example.com
        +https://example.com
    """
    if not schemes_key:
        return re.compile(r"(?!x)x")

    schemes_alt = "|".join(re.escape(s) for s in schemes_key)
    pattern = rf"(?<![A-Za-z0-9_])(?P<scheme>(?:{schemes_alt}))://"
    return re.compile(pattern, re.IGNORECASE)


@lru_cache(maxsize=64)
def _compile_json_escaped_start_re(schemes_key: Tuple[str, ...]) -> re.Pattern[str]:
    r"""
    Compile a regex that finds JSON-escaped URL start markers like:

        https:\/\/example.com\/a
        https:\\/\\/example.com\\/a

    We detect:

        <scheme>:(\\+/)(\\+/)

    meaning: scheme + ":" + two occurrences of "one-or-more backslashes then /".
    """
    if not schemes_key:
        return re.compile(r"(?!x)x")

    schemes_alt = "|".join(re.escape(s) for s in schemes_key)
    pattern = rf"(?<![A-Za-z0-9_])(?P<scheme>(?:{schemes_alt})):(?:\\+/)(?:\\+/)"
    return re.compile(pattern, re.IGNORECASE)


@lru_cache(maxsize=64)
def _compile_json_unicode_escaped_start_re(schemes_key: Tuple[str, ...]) -> re.Pattern[str]:
    r"""
    Compile a regex that finds unicode-escaped URL start markers like:

        https:\u002f\u002fexample.com\u002fa
        https:\\u002F\\u002Fexample.com\\u002Fa

    We detect:

        <scheme>:(\\+u002f)(\\+u002f)

    Kept behind decode_json_escapes=True (default False).
    """
    if not schemes_key:
        return re.compile(r"(?!x)x")

    schemes_alt = "|".join(re.escape(s) for s in schemes_key)
    pattern = rf"(?<![A-Za-z0-9_])(?P<scheme>(?:{schemes_alt})):(?:\\+u002f)(?:\\+u002f)"
    return re.compile(pattern, re.IGNORECASE)


# ---------------------------------------------------------------------------
# Scanning helpers
# ---------------------------------------------------------------------------


def contains_url(text: str, *, decode_json_escapes: bool = False) -> bool:
    """
    Cheap pretest to avoid regex work.

    - Normal URLs contain '://'
    - Escaped URLs commonly contain ':\\' in the source text

    This is heuristic: it may return True even if no valid URL exists, but it's fast.
    """
    if not text:
        return False
    if "://" in text:
        return True
    if decode_json_escapes and ":\\" in text:
        return True
    return False


def _is_stop_char(ch: str, *, escaped: bool) -> bool:
    r"""
    Terminator predicate used by the forward scanner.

    - Always stops on whitespace.
    - Stops on wrapper/separator characters (quotes, <>, pipes, etc.).
    - In normal mode, also stops on backslash.
    - In escaped mode, allows backslash so we can capture '\/' and '\u002f'.

    Note: Escaped mode is used only when decode_json_escapes=True.
    """
    if ch.isspace():
        return True

    if escaped:
        return ch in _STOP_CHARS_ESCAPED
    else:
        if ch == "\\":
            return True
        return ch in _STOP_CHARS_NORMAL


def _escaped_continuation_after_closer(text: str, idx: int) -> bool:
    """
    In escaped mode (decode_json_escapes=True), the next "character" after a closer might be
    an escape sequence that decodes into a continuation char, e.g.:

      )\\u0026x=1   -> )&x=1
      )\\u003dx=1   -> )=x=1

    This function checks whether text[idx:] begins with an escape sequence that decodes to
    a character in _URL_CONTINUATION_AFTER_CLOSER (currently only '&' and '=').
    """
    n = len(text)
    if idx >= n or text[idx] != "\\":
        return False

    j = idx
    while j < n and text[j] == "\\":
        j += 1
    if j >= n:
        return False

    # One-or-more backslashes then '/' -> decodes to '/'
    if text[j] == "/":
        # Note: with the current common-usage policy, '/' is not a continuation-after-closer
        # character, so this will return False. Kept for future-proofing.
        return "/" in _URL_CONTINUATION_AFTER_CLOSER

    # One-or-more backslashes then 'u' + 4 hex digits -> decodes to that codepoint
    if text[j].lower() == "u" and j + 4 < n:
        hexcode = text[j + 1 : j + 5]
        if len(hexcode) == 4 and all(c in "0123456789abcdefABCDEF" for c in hexcode):
            decoded = chr(int(hexcode, 16))
            return decoded in _URL_CONTINUATION_AFTER_CLOSER

    return False


def _looks_like_wrapper_boundary(
    text: str,
    idx: int,
    *,
    escaped: bool,
    max_end: int,
    seen_qmark: bool,
) -> bool:
    """
    Heuristic: decide whether an unpaired/mismatched closer ')', ']', '}' is likely
    wrapper punctuation (so URL should end BEFORE it) or likely part of the URL token.

    - End of string => boundary
    - Scan cap (idx >= max_end but idx < len(text)) => unknown; DO NOT treat as boundary
      (preserves cap-hit discard protections)
    - Whitespace / stop char / trailing punctuation / trailing CJK wrapper / another closer => boundary
    - Continuation allowlist char (currently '&' or '=') => NOT boundary, but only after a query string
      has started (seen '?' earlier in this token). This avoids swallowing patterns like "(url)&foo".
    - Escaped mode: if next chars are an escape that decodes to allowlist ('\\u0026' -> '&', '\\u003d' -> '=') => NOT boundary
    - Otherwise => boundary (prevents swallowing ')foo')
    """
    if idx >= len(text):
        return True
    if idx >= max_end:
        return False

    ch = text[idx]
    if ch.isspace():
        return True
    if _is_stop_char(ch, escaped=escaped):
        return True
    if ch in _TRAILING_PUNCTUATION:
        return True
    if ch in _TRAILING_CJK_WRAPPERS:
        return True
    if ch in _CLOSERS:
        return True

    # Common-usage refinement:
    # Treat '&'/'=' as continuation only once a query string has started (we've seen '?' earlier
    # in the same URL token). This avoids swallowing patterns like "(https://x.com)&foo".
    if seen_qmark and ch in _URL_CONTINUATION_AFTER_CLOSER:
        return False

    # Escaped-mode peek: continuation might be encoded as '\\uXXXX' or '\\/'.
    if escaped and seen_qmark and ch == "\\" and _escaped_continuation_after_closer(text, idx):
        return False

    return True


def _looks_like_quote_boundary(
    text: str,
    idx: int,
    *,
    escaped: bool,
    max_end: int,
) -> bool:
    """
    Heuristic: decide whether a quote-like character should terminate the URL token.

    Used for ASCII apostrophe (') which we allow inside URLs, but which also commonly
    encloses URLs in prose/JSON/HTML attributes.
    """
    if idx >= len(text):
        return True
    if idx >= max_end:
        return False

    ch = text[idx]
    if ch.isspace():
        return True
    if _is_stop_char(ch, escaped=escaped):
        return True
    if ch in _TRAILING_PUNCTUATION:
        return True
    if ch in _TRAILING_CJK_WRAPPERS:
        return True
    if ch in _CLOSERS:
        return True
    return False


def _has_later_apostrophe_before_token_boundary(
    text: str,
    start_idx: int,
    *,
    escaped: bool,
    max_end: int,
) -> bool:
    """
    Look ahead (within the current non-whitespace token) to see if there's another
    ASCII apostrophe before a token boundary.

    Token boundary here means:
      - whitespace
      - a hard stop char (quotes, <>, backticks, pipes, etc.)
      - trailing CJK wrapper closer (policy: these are hard terminators)

    Purpose:
    - If a URL is opened with an apostrophe wrapper (e.g. 'https://...'),
      treat the last apostrophe in that token as the closing wrapper even if it's
      immediately followed by letters (e.g. 'https://x.com/a'foo).
    - But do NOT treat apostrophes inside the URL path/query as closers if a later
      closing apostrophe exists in the same token.
    """
    i = start_idx
    n = len(text)
    while i < max_end and i < n:
        ch = text[i]
        if ch.isspace():
            return False
        if ch in _TRAILING_CJK_WRAPPERS:
            return False
        if _is_stop_char(ch, escaped=escaped):
            return False
        if ch == "'":
            return True
        i += 1
    return False


def _scan_url_end(
    text: str,
    *,
    url_start: int,
    scan_start: int,
    escaped: bool,
    schemes_key: Tuple[str, ...],
    max_end: int,
) -> int:
    r"""
    Scan forward from scan_start and return the end index (exclusive) of the URL substring.

    Rules applied during scanning:
    1) Stop on terminators (whitespace, quotes, <>, pipes, etc.).
    2) Apply delimiter-balance rule for (), [], {}:
       - Balanced delimiters are allowed and preserved.
       - Wrapper-aware closer handling:
           - an unpaired/mismatched closer ends the URL BEFORE it only when it looks like
             wrapper punctuation (boundary after it, e.g. whitespace/end/quotes/stop chars/punct)
           - if followed by '&' or '=' after a query string has started (seen '?' earlier in the token),
             keep it as part of the URL token (supports patterns like '?q=)&x=1')
    3) Adjacent URL salvage:
       If we see ',' or ';' and the next character begins another URL start marker,
       we end the current URL BEFORE the separator. This handles:
         'https://a.com,https://b.com'
       and (when escaped=True):
         'https:\/\/a.com\/x,https:\/\/b.com\/y'
       without requiring whitespace.

    Complexity: O(length_of_url_candidate), linear scan.
    """
    stack: List[str] = []
    seen_qmark = False
    in_netloc = True  # until we see first '/', '?', or '#'
    # Wrapper opener awareness: if URL was immediately preceded by a single-quote,
    # treat the last apostrophe within the current token as a closing wrapper even
    # if followed by letters (e.g. 'https://x.com/a'foo).
    opened_by_apostrophe = url_start > 0 and text[url_start - 1] == "'"

    # Precompiled for adjacent URL salvage
    scheme_start_re = _compile_scheme_start_re(schemes_key)
    esc_start_re = _compile_json_escaped_start_re(schemes_key) if escaped else None
    uni_start_re = _compile_json_unicode_escaped_start_re(schemes_key) if escaped else None

    i = scan_start
    while i < max_end:
        ch = text[i]

        # netloc ends at the first '/', '?', or '#'
        if in_netloc and ch in "/?#":
            in_netloc = False

        # ASCII apostrophe is commonly part of real-world URL paths/queries (e.g. S3 keys like "i'll"),
        # but it's also commonly used to *enclose* URLs:
        #   'https://example.com/a'
        #
        # Policy:
        # - In netloc/authority, treat "'" as a terminator (prevents "https://example.com's").
        # - In path/query/fragment, keep it unless it looks like a wrapper boundary.
        # - If the URL was opened by a wrapper apostrophe (character immediately before the scheme),
        #   treat the last apostrophe in the current token as the closing wrapper, even if followed
        #   by word characters (handles "'https://x.com/a'foo").
        if ch == "'":
            if in_netloc:
                break
            if _looks_like_quote_boundary(text, i + 1, escaped=escaped, max_end=max_end):
                break
            if opened_by_apostrophe and not _has_later_apostrophe_before_token_boundary(
                text,
                i + 1,
                escaped=escaped,
                max_end=max_end,
            ):
                break
            i += 1
            continue

        if _is_stop_char(ch, escaped=escaped):
            break

        if ch in ",;":
            nxt = i + 1
            if nxt < max_end:
                if scheme_start_re.match(text, nxt):
                    break
                if escaped and esc_start_re and esc_start_re.match(text, nxt):
                    break
                if escaped and uni_start_re and uni_start_re.match(text, nxt):
                    break

        if ch in _OPENERS:
            stack.append(ch)
            i += 1
            continue

        # Track whether we've entered a query string for the current token.
        if ch == "?":
            seen_qmark = True

        if ch in _CLOSERS:
            # Unpaired/mismatched closers only end the URL when they look like
            # wrapper punctuation (boundary after the closer). Otherwise keep them.
            boundary = _looks_like_wrapper_boundary(
                text,
                i + 1,
                escaped=escaped,
                max_end=max_end,
                seen_qmark=seen_qmark,
            )

            if not stack:
                if boundary:
                    break
                i += 1
                continue

            opener = stack[-1]
            expected = _MATCHING_CLOSER_FOR_OPENER[opener]
            if ch == expected:
                stack.pop()
                i += 1
                continue

            if boundary:
                break
            i += 1
            continue

        i += 1

    return i


def _trim_trailing_punctuation(s: str) -> str:
    """
    Trim trailing punctuation commonly attached to URLs in prose/JSON.

    Delimiters like ')', ']' are NOT trimmed here; those are handled via scan-time
    delimiter balancing.
    """
    while s and (s[-1] in _TRAILING_PUNCTUATION or s[-1] in _TRAILING_CJK_WRAPPERS):
        s = s[:-1]
    return s


# ---------------------------------------------------------------------------
# JSON escape decoding (applied only to matched substrings, never whole text)
# ---------------------------------------------------------------------------

_JSON_ESCAPED_SLASH_RE = re.compile(r"\\+/")
_JSON_UNICODE_ESCAPE_RE = re.compile(r"\\+u([0-9a-fA-F]{4})")


def _decode_json_escapes_in_url(raw: str) -> str:
    """
    Decode common JSON escape sequences within a matched URL substring.

    Supported:
      - '\\/' and '\\\\/' variants -> '/'
        (implemented as: one-or-more backslashes + '/' -> '/')
      - '\\uXXXX' and '\\\\uXXXX' variants -> unicode char

    This is intentionally limited: we do not perform full JSON string decoding.
    """
    s = _JSON_ESCAPED_SLASH_RE.sub("/", raw)

    def repl(m: re.Match[str]) -> str:
        return chr(int(m.group(1), 16))

    s = _JSON_UNICODE_ESCAPE_RE.sub(repl, s)
    return s


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _netloc_has_bracketed_ipv6(netloc: str) -> bool:
    """
    Detect bracketed IPv6 literals in a URL netloc.

    Valid IPv6 URL syntax is bracketed, e.g.:
        http://[::1]/x
        http://user@[::1]:8080/x

    We do NOT treat arbitrary ':' in netloc/hostname as IPv6, because schemes like
    s3://... can reasonably contain ':' in the "authority" portion (ARN-like forms),
    and urlsplit() can produce host strings containing ':' even when it's not IPv6.
    """
    if not netloc:
        return False
    hostport = netloc.rsplit("@", 1)[-1]  # strip optional userinfo
    return hostport.startswith("[") and "]" in hostport


def _validate_url(
    url: str,
    *,
    allowed_schemes: Collection[str],
    allow_ipv6: bool,
) -> Tuple[bool, str]:
    """
    Validate extracted URL candidates using urllib.parse.urlsplit.

    Policy:
    - scheme must be in allowed_schemes
    - for our supported schemes, netloc must be present
      (s3://bucket/key -> netloc is 'bucket')
    - IPv6 hostnames are rejected unless allow_ipv6=True
      (detection is strict: bracketed IPv6 literals only)
    """
    if not url:
        return False, ""

    # Defense in depth; regex/scan should avoid whitespace already.
    if any(c.isspace() for c in url):
        return False, ""

    # Backslashes should not appear in a normal URL string.
    if "\\" in url:
        return False, ""

    try:
        sp = urlsplit(url)
    except Exception:
        return False, ""

    scheme = (sp.scheme or "").lower()
    if scheme not in allowed_schemes:
        return False, ""

    if not sp.netloc:
        return False, scheme

    # For our "online" schemes, hostname is expected.
    # For s3, allow broader "authority-like" forms as long as netloc exists.
    if scheme != "s3" and not sp.hostname:
        return False, scheme

    if not allow_ipv6 and _netloc_has_bracketed_ipv6(sp.netloc):
        return False, scheme

    return True, scheme


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def iter_url_matches(
    text: str,
    *,
    allowed_schemes: Collection[str] = DEFAULT_ALLOWED_SCHEMES,
    validate: bool = True,
    allow_ipv6: bool = False,
    decode_json_escapes: bool = False,
    max_url_len: int = DEFAULT_MAX_URL_LEN,
    max_urls: int = DEFAULT_MAX_URLS,
) -> Iterator[UrlMatch]:
    r"""
    Iterate URL matches in `text`, returning span-aware UrlMatch objects.

    Supported contexts:
    - Markdown inline links: [x](https://example.com/a)
    - Markdown autolinks: <https://example.com/a>
    - JSON strings: {"url":"https://example.com/a"}
    - Prose punctuation: (https://example.com/a).

    Delimiter rule:
    - Balanced (), [], {} inside the URL are allowed and preserved.
    - Wrapper-aware closer handling:
        - unpaired/mismatched ')', ']', '}' are treated as end-of-URL only when they appear
          to be wrapper punctuation (e.g., followed by whitespace/end-of-text/quotes/stop chars)
        - if followed by '&' or '=' after a query string has started (seen '?' earlier in the token),
          keep them as part of the URL token (supports patterns like '?q=)&x=1')
        - in escaped mode (decode_json_escapes=True), URL escapes are decoded within the matched
          substring; after-closer continuation is only honored for escapes that decode to '&'/'='
          (e.g. '\u0026', '\u003d') once '?' has been seen

    JSON-escaped mode (optional, default False):
    - Also extracts:
        https:\/\/example.com\/a?x=1
        https:\\/\\/example.com\\/a
        https:\u002f\u002fexample.com\u002fa
      returning UrlMatch.raw as the escaped substring and UrlMatch.url as decoded.

    Guardrails:
    - max_url_len: drop candidates longer than this
    - max_urls: stop after this many matches

    Performance note:
    Scanning is capped to at most (max_url_len + _SCAN_SLACK) characters from each match start,
    and max length is enforced AFTER trimming punctuation.
    """
    if not text or max_urls <= 0:
        return

    schemes_key = _sanitize_allowed_schemes(allowed_schemes)
    if not schemes_key:
        return

    if not contains_url(text, decode_json_escapes=decode_json_escapes):
        return

    allowed_set = set(schemes_key)
    n = len(text)

    # Collect candidates across modes to deconflict overlaps and preserve order.
    # (Bounded by max_urls, so this remains small.)
    candidates: List[Tuple[int, int, str, bool, str]] = []
    # tuple: (start, end, raw_trimmed, is_escaped, scheme_hint)

    # Pass 1: normal scheme://
    start_re = _compile_scheme_start_re(schemes_key)
    pos = 0
    while len(candidates) < max_urls:
        m = start_re.search(text, pos)
        if not m:
            break

        start = m.start(0)
        scheme_hint = (m.group("scheme") or "").lower()
        scan_start = m.end(0)

        scan_limit = n if not max_url_len else min(n, start + max_url_len + _SCAN_SLACK)
        end = _scan_url_end(
            text,
            url_start=start,
            scan_start=scan_start,
            escaped=False,
            schemes_key=schemes_key,
            max_end=scan_limit,
        )
        # If we hit the scan cap and the text continues, treat as overflow to avoid
        # returning a truncated prefix of an overly-long URL token.
        if max_url_len and scan_limit < n and end == scan_limit:
            pos = max(end, start + 1)
            continue

        raw_full = text[start:end]
        raw_trimmed = _trim_trailing_punctuation(raw_full)
        if not raw_trimmed:
            pos = max(end, start + 1)
            continue

        if max_url_len and len(raw_trimmed) > max_url_len:
            pos = max(end, start + 1)
            continue

        end2 = start + len(raw_trimmed)
        candidates.append((start, end2, raw_trimmed, False, scheme_hint))

        # Progress: move to end of this URL (not into its body) to avoid inner rescans.
        pos = max(end2, start + 1)

    # Pass 2/3: escaped forms (optional)
    if decode_json_escapes and len(candidates) < max_urls:
        esc_re = _compile_json_escaped_start_re(schemes_key)
        pos = 0
        while len(candidates) < max_urls:
            m = esc_re.search(text, pos)
            if not m:
                break

            start = m.start(0)
            scheme_hint = (m.group("scheme") or "").lower()
            scan_start = m.end(0)

            scan_limit = n if not max_url_len else min(n, start + max_url_len + _SCAN_SLACK)
            end = _scan_url_end(
                text,
                url_start=start,
                scan_start=scan_start,
                escaped=True,
                schemes_key=schemes_key,
                max_end=scan_limit,
            )
            if max_url_len and scan_limit < n and end == scan_limit:
                pos = max(end, start + 1)
                continue

            raw_full = text[start:end]
            raw_trimmed = _trim_trailing_punctuation(raw_full)
            if not raw_trimmed:
                pos = max(end, start + 1)
                continue

            if max_url_len and len(raw_trimmed) > max_url_len:
                pos = max(end, start + 1)
                continue

            end2 = start + len(raw_trimmed)
            candidates.append((start, end2, raw_trimmed, True, scheme_hint))

            pos = max(end2, start + 1)

    if decode_json_escapes and len(candidates) < max_urls:
        uni_re = _compile_json_unicode_escaped_start_re(schemes_key)
        pos = 0
        while len(candidates) < max_urls:
            m = uni_re.search(text, pos)
            if not m:
                break

            start = m.start(0)
            scheme_hint = (m.group("scheme") or "").lower()
            scan_start = m.end(0)

            scan_limit = n if not max_url_len else min(n, start + max_url_len + _SCAN_SLACK)
            end = _scan_url_end(
                text,
                url_start=start,
                scan_start=scan_start,
                escaped=True,
                schemes_key=schemes_key,
                max_end=scan_limit,
            )
            if max_url_len and scan_limit < n and end == scan_limit:
                pos = max(end, start + 1)
                continue

            raw_full = text[start:end]
            raw_trimmed = _trim_trailing_punctuation(raw_full)
            if not raw_trimmed:
                pos = max(end, start + 1)
                continue

            if max_url_len and len(raw_trimmed) > max_url_len:
                pos = max(end, start + 1)
                continue

            end2 = start + len(raw_trimmed)
            candidates.append((start, end2, raw_trimmed, True, scheme_hint))

            pos = max(end2, start + 1)

    if not candidates:
        return

    # Deconflict overlaps:
    # - sort by start asc, then length desc; skip overlaps (keep earliest/longest).
    candidates.sort(key=lambda t: (t[0], -(t[1] - t[0])))

    last_end = -1
    for start, end, raw_trimmed, is_escaped, scheme_hint in candidates:
        if start < last_end:
            continue

        url_value = _decode_json_escapes_in_url(raw_trimmed) if is_escaped else raw_trimmed

        if validate:
            ok, scheme = _validate_url(
                url_value, allowed_schemes=allowed_set, allow_ipv6=allow_ipv6
            )
            if not ok:
                continue
        else:
            scheme = scheme_hint

        yield UrlMatch(start=start, end=end, raw=raw_trimmed, url=url_value, scheme=scheme)
        last_end = end


def extract_urls(
    text: str,
    *,
    allowed_schemes: Collection[str] = DEFAULT_ALLOWED_SCHEMES,
    unique: bool = True,
    validate: bool = True,
    allow_ipv6: bool = False,
    decode_json_escapes: bool = False,
    max_url_len: int = DEFAULT_MAX_URL_LEN,
    max_urls: int = DEFAULT_MAX_URLS,
) -> List[str]:
    """
    Convenience wrapper returning URL strings (UrlMatch.url).

    - If unique=True, dedupes while preserving first-seen order.
    """
    if not text:
        return []

    urls: List[str] = []
    if unique:
        seen: set[str] = set()
        for um in iter_url_matches(
            text,
            allowed_schemes=allowed_schemes,
            validate=validate,
            allow_ipv6=allow_ipv6,
            decode_json_escapes=decode_json_escapes,
            max_url_len=max_url_len,
            max_urls=max_urls,
        ):
            if um.url not in seen:
                urls.append(um.url)
                seen.add(um.url)
        return urls

    return [
        um.url
        for um in iter_url_matches(
            text,
            allowed_schemes=allowed_schemes,
            validate=validate,
            allow_ipv6=allow_ipv6,
            decode_json_escapes=decode_json_escapes,
            max_url_len=max_url_len,
            max_urls=max_urls,
        )
    ]


def mask_urls(
    text: str,
    *,
    allowed_schemes: Collection[str] = DEFAULT_ALLOWED_SCHEMES,
    validate: bool = True,
    allow_ipv6: bool = False,
    decode_json_escapes: bool = False,
    max_url_len: int = DEFAULT_MAX_URL_LEN,
    max_urls: int = DEFAULT_MAX_URLS,
    placeholder_prefix: str = "__URL_PLACEHOLDER_",
    placeholder_suffix: str = "__",
) -> Tuple[str, Dict[str, str]]:
    """
    Replace URLs in `text` with placeholders and return (masked_text, placeholder_to_raw_url).

    Why span-based masking:
    - avoids O(n^2) repeated .replace() loops
    - correct when the same URL occurs multiple times
    - does not rely on reconstructing Markdown substrings

    Mapping values store m.raw (the exact substring that was replaced), so restoration
    puts back exactly what was masked.
    """
    if not text:
        return text, {}

    matches = list(
        iter_url_matches(
            text,
            allowed_schemes=allowed_schemes,
            validate=validate,
            allow_ipv6=allow_ipv6,
            decode_json_escapes=decode_json_escapes,
            max_url_len=max_url_len,
            max_urls=max_urls,
        )
    )
    if not matches:
        return text, {}

    parts: List[str] = []
    mapping: Dict[str, str] = {}

    last = 0
    for i, m in enumerate(matches):
        if m.start < last:
            # defensive overlap guard (shouldn't happen due to deconflict logic)
            continue

        placeholder = f"{placeholder_prefix}{i}{placeholder_suffix}"
        parts.append(text[last : m.start])
        parts.append(placeholder)
        mapping[placeholder] = m.raw
        last = m.end

    parts.append(text[last:])
    return "".join(parts), mapping


def unmask_urls(masked_text: str, mapping: Dict[str, str]) -> str:
    """
    Restore placeholders produced by mask_urls().

    This is typically used after transforming masked_text (e.g., NSFW word replacement)
    to reinsert the original URL substrings.
    """
    if not masked_text or not mapping:
        return masked_text

    out = masked_text
    for placeholder, raw in mapping.items():
        out = out.replace(placeholder, raw)
    return out


__all__ = [
    "UrlMatch",
    "DEFAULT_ALLOWED_SCHEMES",
    "DEFAULT_MAX_URL_LEN",
    "DEFAULT_MAX_URLS",
    "contains_url",
    "iter_url_matches",
    "extract_urls",
    "mask_urls",
    "unmask_urls",
]
