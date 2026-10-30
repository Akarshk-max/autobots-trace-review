import asyncio
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from src.autobots.browser_task.exception_handler.base_exception import ErrorCode

STEALTH_UA_PROFILES = [
    {
        "name": "linux_chrome_121",
        "ua": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0.6167.85 Safari/537.36"
        ),
        "platform": "Linux x86_64",
        "sec_ch_platform": "Linux",
        "sec_ch_ua": '"Chromium";v="121", "Not:A-Brand";v="99"',
    },
    {
        "name": "linux_chrome_120",
        "ua": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.6099.216 Safari/537.36"
        ),
        "platform": "Linux x86_64",
        "sec_ch_platform": "Linux",
        "sec_ch_ua": '"Chromium";v="120", "Not:A-Brand";v="99"',
    },
]

_DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.6167.85 Safari/537.36"
)

_bg_url_re = re.compile(r'url\(\s*([\'"]?)(.*?)\1\s*\)', re.IGNORECASE)

def _pick_best_from_srcset(srcset: str) -> str:
    if not srcset:
        return ""
    parts = [p.strip() for p in srcset.split(",") if p.strip()]
    best_url = ""
    best_w = -1
    for p in parts:
        toks = p.split()
        if not toks:
            continue
        u = toks[0].strip()
        w = -1
        if len(toks) >= 2 and toks[1].endswith("w"):
            try:
                w = int(toks[1][:-1])
            except Exception:
                w = -1
        if w > best_w:
            best_w = w
            best_url = u
        if not best_url:
            best_url = u
    return best_url


def extract_images_http(
    html: str,
    *,
    base_url: str,
    limit: int = 200,
    include_background_images: bool = False,
) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html or "", "html.parser")
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def _push(u: str, *, alt: str = "", source: str = "img", meta: Optional[dict] = None):
        if not u:
            return
        abs_u = urljoin(base_url, u.strip())
        if not abs_u:
            return
        key = abs_u
        if key in seen:
            return
        seen.add(key)
        out.append({"url": abs_u, "alt": alt or "", "source": source, "meta": meta or {}})

    for img in soup.find_all("img"):
        if len(out) >= limit:
            break
        alt = (img.get("alt") or "").strip()

        src = (img.get("src") or "").strip()
        data_src = (img.get("data-src") or img.get("data-original") or img.get("data-lazy-src") or "").strip()
        srcset = (img.get("srcset") or img.get("data-srcset") or "").strip()

        chosen = ""
        if srcset:
            chosen = _pick_best_from_srcset(srcset)
        elif data_src:
            chosen = data_src
        else:
            chosen = src

        _push(chosen, alt=alt, source="img", meta={"srcset": srcset} if srcset else {})

    for pic in soup.find_all("picture"):
        if len(out) >= limit:
            break
        sources = pic.find_all("source")
        for s in sources:
            if len(out) >= limit:
                break
            ss = (s.get("srcset") or "").strip()
            if not ss:
                continue
            chosen = _pick_best_from_srcset(ss)
            _push(chosen, alt="", source="picture_source", meta={"srcset": ss})

    if include_background_images:
        for el in soup.find_all(style=True):
            if len(out) >= limit:
                break
            st = (el.get("style") or "")
            if "background" not in st.lower():
                continue
            for m in _bg_url_re.finditer(st):
                u = (m.group(2) or "").strip()
                if u:
                    _push(u, alt="", source="background_style")
    return out

def _default_http_headers(*, accept_language: Optional[str] = None) -> Dict[str, str]:
    h = {
        "User-Agent": _DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": accept_language or "en-IN,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "DNT": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }
    return h

_http_client_lock = asyncio.Lock()
_http_client: httpx.AsyncClient | None = None

async def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    async with _http_client_lock:
        if _http_client is not None:
            return _http_client

        limits = httpx.Limits(max_connections=100, max_keepalive_connections=30, keepalive_expiry=30)
        timeout = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
        transport = httpx.AsyncHTTPTransport(retries=0)

        _http_client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            limits=limits,
            transport=transport,
            headers=_default_http_headers(),
            http2=True,
        )
        return _http_client

def _is_probably_html(content_type: str) -> bool:
    ct = (content_type or "").lower()
    return ("text/html" in ct) or ("application/xhtml" in ct) or (ct.startswith("text/"))

def _truncate_text(s: str, max_chars: int) -> str:
    if not s:
        return ""
    return s[:max_chars]


def _clean_text(s: str, *, collapse_newlines: bool = True, trim_whitespace: bool = True) -> str:
    out = s or ""
    if collapse_newlines:
        out = re.sub(r"[ \t]+\n", "\n", out)
        out = re.sub(r"\n{3,}", "\n\n", out)
    if trim_whitespace:
        out = out.strip()
    return out


def normalize_html(
    html: str,
    *,
    strip_scripts_styles: bool = True,
) -> str:
    """
    Returns possibly reduced html (scripts/styles removed) to avoid noisy text extraction.
    """
    if not html:
        return ""
    if not strip_scripts_styles:
        return html
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        try:
            tag.decompose()
        except Exception:
            pass
    return str(soup)


def normalize_text(
    s: str,
    *,
    trim_whitespace: bool = True,
    collapse_newlines: bool = True,
    unicode_normalize: bool = False,
) -> str:
    out = s or ""
    if unicode_normalize:
        out = unicodedata.normalize("NFKC", out)
    out = _clean_text(out, collapse_newlines=collapse_newlines, trim_whitespace=trim_whitespace)
    return out


def extract_metadata(html: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html or "", "html.parser")
    title = (soup.title.text.strip() if soup.title and soup.title.text else "")

    desc = ""
    m = soup.find("meta", attrs={"name": "description"})
    if m and m.get("content"):
        desc = m["content"].strip()

    canon = ""
    c = soup.find("link", attrs={"rel": "canonical"})
    if c and c.get("href"):
        canon = c["href"].strip()

    return {"title": title, "description": desc, "canonical": canon}


def _strip_query(url: str) -> str:
    try:
        p = urlparse(url)
        return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))
    except Exception:
        return url


async def fetch_html(
    url: str,
    *,
    timeout_s: float,
    max_redirects: int,
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[int, str, str]:
    """
    Robust HTTP fetch:
    - shared keepalive client
    - browser-like defaults
    - retry for transient failures
    - returns (status_code, final_url, html_text)
    """
    client = await _get_http_client()

    merged = dict(_default_http_headers())
    if headers:
        for k, v in headers.items():
            if v is not None:
                merged[str(k)] = str(v)

    timeout = httpx.Timeout(timeout_s, connect=min(5.0, timeout_s), read=timeout_s, write=timeout_s, pool=5.0)

    retry_delays = [0.15, 0.35, 0.75]
    last_exc: Exception | None = None

    for attempt in range(len(retry_delays) + 1):
        try:
            r = await client.get(url, headers=merged, timeout=timeout, follow_redirects=True)
            status = r.status_code
            final_url = str(r.url)

            ctype = r.headers.get("content-type", "")
            if not _is_probably_html(ctype):
                return status, final_url, ""

            # Avoid pathological huge HTML bodies
            text = r.text or ""
            text = _truncate_text(text, 3_000_000)  # 3MB cap of text
            return status, final_url, text

        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_exc = e
            if attempt >= len(retry_delays):
                raise
            await asyncio.sleep(retry_delays[attempt])
        except Exception as e:
            last_exc = e
            raise

    raise last_exc or RuntimeError("http_fetch_failed")

def detect_soft_block_html(html: str) -> Optional[str]:
    h = (html or "").lower()
    if not h:
        return None

    patterns = [
        "checking your browser",
        "cloudflare",
        "attention required",
        "enable javascript",
        "captcha",
        "verify you are human",
        "access denied",
        "request blocked",
        "unusual traffic",
        "bot detection",
        "automated requests",
        "akamai",
        "datadome",
        "perimeterx",
        "incapsula",
        "sucuri",
    ]
    for p in patterns:
        if p in h:
            return p
    return None

def extract_links(
    html: str,
    *,
    base_url: str,
    scope_selectors: List[str],
    limit: int,
    allow_external_links: bool,
    dedupe: bool = True,
    keep_query_params: bool = False,
) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html or "", "html.parser")
    scope = None
    for sel in scope_selectors or []:
        scope = soup.select_one(sel)
        if scope:
            break
    root = scope or soup

    base_host = (urlparse(base_url).hostname or "").lower()

    links: List[Dict[str, str]] = []
    seen = set()

    for a in root.find_all("a", href=True):
        href_raw = (a.get("href") or "").strip()
        text = a.get_text(" ", strip=True)

        if not href_raw:
            continue

        href_abs = urljoin(base_url, href_raw)
        if not keep_query_params:
            href_abs = _strip_query(href_abs)

        host = (urlparse(href_abs).hostname or "").lower()
        if not allow_external_links and host and base_host and host != base_host:
            continue

        key = (href_abs, text) if dedupe else None
        if dedupe and key in seen:
            continue
        if dedupe:
            seen.add(key)

        links.append({"href": href_abs, "text": text})
        if len(links) >= limit:
            break

    return links


def extract_main_content(
    html: str,
    *,
    selectors: List[str],
    exclude_selectors: List[str],
    normalize_cfg: Dict[str, Any],
) -> str:
    html2 = normalize_html(html or "", strip_scripts_styles=bool(normalize_cfg.get("strip_scripts_styles", True)))
    soup = BeautifulSoup(html2, "html.parser")

    for ex in exclude_selectors or []:
        for node in soup.select(ex):
            try:
                node.decompose()
            except Exception:
                pass

    node = None
    for sel in selectors or []:
        node = soup.select_one(sel)
        if node:
            break

    if not node:
        node = soup.select_one("article") or soup.select_one("main") or soup.body

    txt = node.get_text("\n", strip=True) if node else ""
    return normalize_text(
        txt,
        trim_whitespace=bool(normalize_cfg.get("trim_whitespace", True)),
        collapse_newlines=bool(normalize_cfg.get("collapse_newlines", True)),
        unicode_normalize=bool(normalize_cfg.get("unicode_normalize", False)),
    )


def _remove_scripts_styles_inplace(soup: BeautifulSoup) -> None:
    for tag in soup(["script", "style", "noscript"]):
        try:
            tag.decompose()
        except Exception:
            pass


def _extract_links_from_root(
    root,
    *,
    base_url: str,
    limit: int,
    allow_external_links: bool,
    dedupe: bool = True,
    keep_query_params: bool = False,
) -> List[Dict[str, str]]:
    base_host = (urlparse(base_url).hostname or "").lower()

    links: List[Dict[str, str]] = []
    seen = set()

    for a in root.find_all("a", href=True):
        href_raw = (a.get("href") or "").strip()
        text = a.get_text(" ", strip=True)

        if not href_raw:
            continue

        href_abs = urljoin(base_url, href_raw)
        if not keep_query_params:
            href_abs = _strip_query(href_abs)

        host = (urlparse(href_abs).hostname or "").lower()
        if not allow_external_links and host and base_host and host != base_host:
            continue

        key = (href_abs, text) if dedupe else None
        if dedupe and key in seen:
            continue
        if dedupe:
            seen.add(key)

        links.append({"href": href_abs, "text": text})
        if len(links) >= limit:
            break

    return links


def extract_all(
    html: str,
    *,
    base_url: str,
    tasks: List[str],
    # links
    scope_selectors: List[str],
    links_limit: int,
    allow_external_links: bool,
    dedupe_links: bool,
    keep_query_params: bool,
    # main content
    content_selectors: List[str],
    exclude_selectors: List[str],
    normalize_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Single-pass extractor:
    - parses BeautifulSoup once
    - optionally strips scripts/styles once
    - extracts metadata/links/main_content based on tasks
    """
    if not html:
        return {}

    wanted = set(tasks or [])
    if not wanted:
        return {}

    soup = BeautifulSoup(html, "html.parser")

    # Strip scripts/styles once (matches previous main_content behavior)
    if bool(normalize_cfg.get("strip_scripts_styles", True)):
        _remove_scripts_styles_inplace(soup)

    out: Dict[str, Any] = {}

    if "metadata" in wanted:
        title = (soup.title.text.strip() if soup.title and soup.title.text else "")

        desc = ""
        m = soup.find("meta", attrs={"name": "description"})
        if m and m.get("content"):
            desc = m["content"].strip()

        canon = ""
        c = soup.find("link", attrs={"rel": "canonical"})
        if c and c.get("href"):
            canon = c["href"].strip()

        out["metadata"] = {"title": title, "description": desc, "canonical": canon}

    if "links" in wanted:
        scope = None
        for sel in scope_selectors or []:
            try:
                scope = soup.select_one(sel)
            except Exception:
                scope = None
            if scope:
                break
        root = scope or soup
        out["links"] = _extract_links_from_root(
            root,
            base_url=base_url,
            limit=int(links_limit),
            allow_external_links=bool(allow_external_links),
            dedupe=bool(dedupe_links),
            keep_query_params=bool(keep_query_params),
        )

    if "main_content" in wanted:
        # Apply excludes only for main_content
        for ex in exclude_selectors or []:
            try:
                nodes = soup.select(ex)
            except Exception:
                nodes = []
            for node in nodes:
                try:
                    node.decompose()
                except Exception:
                    pass

        node = None
        for sel in content_selectors or []:
            try:
                node = soup.select_one(sel)
            except Exception:
                node = None
            if node:
                break

        if not node:
            node = soup.select_one("article") or soup.select_one("main") or soup.body

        txt = node.get_text("\n", strip=True) if node else ""
        out["main_content"] = normalize_text(
            txt,
            trim_whitespace=bool(normalize_cfg.get("trim_whitespace", True)),
            collapse_newlines=bool(normalize_cfg.get("collapse_newlines", True)),
            unicode_normalize=bool(normalize_cfg.get("unicode_normalize", False)),
        )

    if "images" in wanted:
        limit = int(200)
        out["images"] = extract_images_http(
            str(soup),
            base_url=base_url,
            limit=limit,
        )
    return out


FailureType = Literal[
    "timeout",
    "budget_requests",
    "budget_total",
    "http_404",
    "http_403",
    "http_429",
    "http_5xx",
    "blocked_signal",
    "network_error",
    "expectation_failed",
    "unknown",
]


@dataclass
class ClassifiedFailure:
    failure_type: FailureType
    retryable: bool
    block_reason: Optional[str] = None



def classify_exception(msg: str) -> tuple[ErrorCode, bool]:
    m = (msg or "").lower()

    if "budget_exceeded_max_requests" in m:
        return ErrorCode.budget_requests, False
    if "budget_exceeded_total_ms" in m:
        return ErrorCode.budget_total, True

    if "http_429" in m:
        return ErrorCode.http_429, True
    if "http_403" in m or "http_401" in m:
        return ErrorCode.http_403, False
    if "http_404" in m:
        return ErrorCode.http_404, False
    if "http_5" in m:
        return ErrorCode.http_5xx, True

    if "expectation_failed" in m:
        return ErrorCode.expectation_failed, False

    if "timeout" in m:
        return ErrorCode.timeout, True
    if "net::" in m or "dns" in m or "connection" in m:
        return ErrorCode.network_error, True

    if "storage_state" in m:
        return ErrorCode.storage_state_error, False

    return ErrorCode.unknown, True

class BudgetTracker:
    def __init__(self, *, max_total_ms: int, max_requests: int, max_chars: int):
        self.max_total_ms = max_total_ms
        self.max_requests = max_requests
        self.max_chars = max_chars
        self._t0 = time.time()
        self.requests = 0

    def elapsed_ms(self) -> int:
        return int((time.time() - self._t0) * 1000)

    def check_time(self) -> None:
        if self.elapsed_ms() > self.max_total_ms:
            raise TimeoutError("budget_exceeded_total_ms")

    def inc_request(self) -> None:
        self.requests += 1
        if self.requests > self.max_requests:
            raise RuntimeError("budget_exceeded_max_requests")

    def clamp_text(self, s: str) -> str:
        if len(s) <= self.max_chars:
            return s
        return s[: self.max_chars]


def get_rss_bytes() -> int:
    """
    - Linux containers: /proc/self/status (VmRSS)
    - Fallback: resource.ru_maxrss
    """
    # Linux: /proc/self/status
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    kb = int(parts[1])
                    return kb * 1024
    except Exception:
        pass

    # Fallback
    try:
        import resource

        r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if os.name == "posix":
            return int(r) * 1024
        return int(r)
    except Exception:
        return 0