import base64
import hashlib
import hmac
import secrets
import time
import urllib.parse
from typing import Any, Dict, Mapping, Optional, Tuple

from loguru import logger
from pydantic import BaseModel


def _pct_encode(s: Any) -> str:
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    return urllib.parse.quote(s, safe="~")


def _normalize_base_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"

    port = parsed.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    else:
        netloc = host

    return f"{scheme}://{netloc}{path}"


def _flatten_params(params: Mapping[str, Any] | None) -> list[Tuple[str, str]]:
    if not params:
        return []
    out: list[Tuple[str, str]] = []
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            for item in v:
                if item is None:
                    continue
                out.append((str(k), str(item)))
        else:
            out.append((str(k), str(v)))
    return out


def _collect_query_params(url: str) -> list[Tuple[str, str]]:
    parsed = urllib.parse.urlsplit(url)
    return [(k, v) for (k, v) in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)]


def _normalize_params(all_params: list[Tuple[str, str]]) -> str:
    """
    Sort by (encoded_key, encoded_val), then join as k=v&k=v with percent encoding.
    """
    enc = [(_pct_encode(k), _pct_encode(v)) for (k, v) in all_params]
    enc.sort(key=lambda kv: (kv[0], kv[1]))
    return "&".join([f"{k}={v}" for (k, v) in enc])


def _signature_base_string(method: str, url: str, normalized_param_str: str) -> str:
    m = (method or "GET").upper()
    base_url = _normalize_base_url(url)
    return "&".join([_pct_encode(m), _pct_encode(base_url), _pct_encode(normalized_param_str)])


def _hmac_sha256_signature(signing_key: str, base_string: str) -> str:
    digest = hmac.new(
        signing_key.encode("utf-8"),
        base_string.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


class NetSuiteOAuth1(BaseModel):
    account_id: str  # realm
    consumer_key: str
    consumer_secret: str
    access_token: str
    token_secret: str
    signature_method: str = "HMAC-SHA256"
    version: str = "1.0"


class NetSuite:
    @staticmethod
    def build_oauth1_header(
        *,
        ctx,
        auth: NetSuiteOAuth1,
        method: str,
        url: str,
        params: Optional[Mapping[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        include_body_params: bool = False,
    ) -> Dict[str, str]:
        oauth_timestamp = str(int(time.time()))
        oauth_nonce = secrets.token_hex(16)

        oauth_params: list[Tuple[str, str]] = [
            ("oauth_consumer_key", auth.consumer_key),
            ("oauth_token", auth.access_token),
            ("oauth_signature_method", auth.signature_method),
            ("oauth_timestamp", oauth_timestamp),
            ("oauth_nonce", oauth_nonce),
            ("oauth_version", auth.version),
        ]

        all_params: list[Tuple[str, str]] = []
        all_params.extend(_collect_query_params(url))
        all_params.extend(_flatten_params(params))
        all_params.extend(oauth_params)

        if include_body_params and body:
            all_params.extend(_flatten_params(body))

        normalized = _normalize_params(all_params)
        base_string = _signature_base_string(method, url, normalized)

        signing_key = f"{_pct_encode(auth.consumer_secret)}&{_pct_encode(auth.token_secret)}"
        signature = _hmac_sha256_signature(signing_key, base_string)

        logger.bind(
            ctx=ctx,
            method=method,
            url=url,
            normalized_params=normalized,
            base_string=base_string,
        ).debug("NetSuite OAuth1 signature inputs")

        hdr_params = {
            "realm": auth.account_id,
            "oauth_consumer_key": auth.consumer_key,
            "oauth_token": auth.access_token,
            "oauth_signature_method": auth.signature_method,
            "oauth_timestamp": oauth_timestamp,
            "oauth_nonce": oauth_nonce,
            "oauth_version": auth.version,
            "oauth_signature": signature,
        }

        parts = []
        parts.append(f'realm="{_pct_encode(hdr_params.pop("realm"))}"')
        for k in [
            "oauth_consumer_key",
            "oauth_token",
            "oauth_signature_method",
            "oauth_timestamp",
            "oauth_nonce",
            "oauth_version",
            "oauth_signature",
        ]:
            parts.append(f'{k}="{_pct_encode(hdr_params[k])}"')

        return {"Authorization": "OAuth " + ", ".join(parts)}

    @staticmethod
    def derive_base_url(account_id: str) -> str:
        return f"https://{account_id}.suitetalk.api.netsuite.com"
