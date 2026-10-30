import re
from typing import Any, Iterable, List

MAX_TARGETS = 30
MAX_TASKS = 10

MAX_SELECTORS = 25
MAX_SELECTOR_LEN = 160

MAX_TAGS = 20
MAX_TAG_LEN = 48

MAX_LABEL_LEN = 64
MAX_TRACE_ID_LEN = 128
MAX_NOTES_LEN = 500

# "contains_any_text_hint" etc.
MAX_HINT_STRINGS = 25
MAX_HINT_STRING_LEN = 80

# optional max_wait_hint_ms clamp (hint only)
MAX_WAIT_HINT_MS = 10_000
MIN_WAIT_HINT_MS = 0

# reasonable defaults
DEFAULT_INTENT_VERSION = "1"
DEFAULT_POLICY_ID = "default"

_ws_re = re.compile(r"\s+")
_bad_selector_tokens = [
    "javascript:",
    "vbscript:",
    "data:",
    "expression(",
    "<script",
    "</script",
]


def _normalize_whitespace(s: str) -> str:
    return _ws_re.sub(" ", s.strip())

def clip_int(
     v: Any,
     *,
     min_v: int,
     max_v: int,
     default: int,
 ) -> int:
     try:
         v = int(v)
     except Exception:
         return default
     return max(min_v, min(v, max_v))


def sanitize_selector_list(selectors: Iterable[str] | None) -> List[str]:
    """
    Sanitizes CSS selector hints:
    - trims
    - clamps length/count
    - filters obviously dangerous/weird tokens
    - dedupes preserving order
    """
    if not selectors:
        return []

    out: List[str] = []
    seen: set[str] = set()

    for raw in selectors:
        if not isinstance(raw, str):
            continue
        s = _normalize_whitespace(raw)
        if not s:
            continue
        if len(s) > MAX_SELECTOR_LEN:
            s = s[:MAX_SELECTOR_LEN]
        lowered = s.lower()
        if any(tok in lowered for tok in _bad_selector_tokens):
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= MAX_SELECTORS:
            break

    return out


def sanitize_hint_strings(items: Iterable[str] | None) -> List[str]:
    """
    For contains_any_text_hint, block_signals_hint, etc.
    """
    if not items:
        return []

    out: List[str] = []
    seen: set[str] = set()

    for raw in items:
        if not isinstance(raw, str):
            continue
        s = _normalize_whitespace(raw)
        if not s:
            continue
        if len(s) > MAX_HINT_STRING_LEN:
            s = s[:MAX_HINT_STRING_LEN]
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= MAX_HINT_STRINGS:
            break

    return out