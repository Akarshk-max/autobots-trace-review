from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from typing import Dict, Optional, Type
from urllib.parse import parse_qsl, quote, urlparse

from pydantic import BaseModel

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC

AWS_DEFAULT_REGION = "us-east-1"


class AwsModel(BaseModel):
    aws_access_key_id: str
    aws_secret_access_key: str
    bucket_name: str = ""
    region_name: str = ""
    object_prefix: str = ""


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _sigv4_signing_key(secret_access_key: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = _hmac_sha256(("AWS4" + secret_access_key).encode("utf-8"), date_stamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    k_signing = _hmac_sha256(k_service, "aws4_request")
    return k_signing


def _normalize_header_value(value: str) -> str:
    # SigV4 requires trimming and collapsing sequential whitespace to a single space.
    return " ".join(value.strip().split())


def _find_existing_header_key(headers: Dict[str, str], header_name_lower: str) -> Optional[str]:
    for k in headers.keys():
        if isinstance(k, str) and k.lower() == header_name_lower:
            return k
    return None


def _aws_quote(s: str, safe: str = "-_.~") -> str:
    return quote(s, safe=safe)


def _canonical_uri(path: str) -> str:
    # Encode path per SigV4, keeping '/' separators.
    if not path:
        return "/"
    return quote(path, safe="/-_.~")


def _canonical_query_string(url_query: str, extra_params: Optional[Dict[str, object]] = None) -> str:
    # Merge querystring from URL with config.params (if any), then sort by (key, value).
    pairs = list(parse_qsl(url_query or "", keep_blank_values=True))
    if extra_params:
        for k, v in extra_params.items():
            if v is None:
                # httpx will drop None params; skip.
                continue
            pairs.append((str(k), str(v)))

    # Sort by key, then by value (both URL-encoded)
    encoded_pairs = [(_aws_quote(k), _aws_quote(v)) for k, v in pairs]
    encoded_pairs.sort(key=lambda kv: (kv[0], kv[1]))
    return "&".join([f"{k}={v}" for k, v in encoded_pairs])


def _infer_service_and_region(hostname: str, fallback_region: str) -> tuple[str, str]:
    if not hostname:
        return "s3", fallback_region

    hn = hostname.lower()

    # Common S3 forms:
    # - s3.<region>.amazonaws.com
    # - <bucket>.s3.<region>.amazonaws.com
    # - s3.amazonaws.com (global)
    # - <bucket>.s3.amazonaws.com
    # - s3-<region>.amazonaws.com (legacy)
    if "amazonaws.com" in hn:
        # legacy s3-us-west-2.amazonaws.com
        m = re.match(r"^s3[-.]([a-z0-9-]+)\.amazonaws\.com$", hn)
        if m:
            return "s3", m.group(1)

        parts = hn.split(".")
        if parts[0] == "s3":
            # s3.<region>.amazonaws.com or s3.amazonaws.com
            if len(parts) >= 4 and parts[1] != "amazonaws":
                return "s3", parts[1]
            return "s3", AWS_DEFAULT_REGION
        if len(parts) >= 2 and parts[1] == "s3":
            # <bucket>.s3.<region>.amazonaws.com or <bucket>.s3.amazonaws.com
            if len(parts) >= 5 and parts[2] != "amazonaws":
                return "s3", parts[2]
            return "s3", AWS_DEFAULT_REGION

        # Generic pattern: <service>.<region>.amazonaws.com
        if len(parts) >= 3 and parts[1] != "amazonaws":
            return parts[0], parts[1]

        # Global pattern: <service>.amazonaws.com
        if len(parts) >= 2 and parts[1] == "amazonaws":
            # Most global endpoints accept signing with us-east-1
            return parts[0], AWS_DEFAULT_REGION

    return "s3", fallback_region


class AwsAuth(IntegrationTypeABC):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.aws

    @staticmethod
    async def get_api_domain(app_creds: AwsModel | None) -> str:
        # Used primarily by frontend / connection UX; keep it broad.
        return "amazonaws.com"

    @staticmethod
    def get_creds_model_type() -> Type[AwsModel]:
        return AwsModel

    @staticmethod
    def get_auth_model_type() -> Type[AwsModel]:
        return AwsModel

    @staticmethod
    async def create_auth(ctx: Context, app_creds: AwsModel) -> AwsModel | Exception:
        return app_creds

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: AwsModel, app_auth: AwsModel
    ) -> AwsModel | Exception:
        return app_auth if app_auth else app_creds

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: AwsModel) -> bool:
        return False

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: AwsModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        """
        AWS SigV4 signing for arbitrary HTTPS calls.

        Notes / constraints:
        - ActionAPI currently signs before raw_body_url bytes are downloaded. For S3 we support
          'UNSIGNED-PAYLOAD' for raw_body_url/files to avoid double-download.
        - For JSON dict bodies, we sign the exact bytes that ActionAPI will send (json.dumps(...)).
        """
        if config is None:
            return {}

        method = (config.method or "GET").upper()
        url = config.url
        if not url:
            return {}

        parsed = urlparse(url)
        host = parsed.netloc
        hostname = parsed.hostname or ""
        service, region = _infer_service_and_region(hostname, app_auth.region_name or AWS_DEFAULT_REGION)

        # Start from provided headers, but avoid duplicates with different casing.
        existing_headers = dict(config.headers or {})

        # Use caller-provided X-Amz-Date if present; otherwise generate.
        amz_date_key = _find_existing_header_key(existing_headers, "x-amz-date") or "X-Amz-Date"
        amz_date_val = existing_headers.get(amz_date_key)
        if not amz_date_val:
            amz_date_val = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        date_stamp = amz_date_val[0:8]

        # Determine payload hash. For S3 over HTTPS we can use UNSIGNED-PAYLOAD when body bytes
        # are not available at signing time (raw_body_url/files).
        payload_hash_key = (
            _find_existing_header_key(existing_headers, "x-amz-content-sha256")
            or "X-Amz-Content-SHA256"
        )
        payload_hash_val = existing_headers.get(payload_hash_key)

        if not payload_hash_val:
            if service == "s3" and (config.raw_body_url or config.files):
                payload_hash_val = "UNSIGNED-PAYLOAD"
            elif config.body is not None:
                payload_hash_val = _sha256_hex(json.dumps(config.body).encode("utf-8"))
            else:
                payload_hash_val = _sha256_hex(b"")

        canonical_request = _build_canonical_request(
            method=method,
            parsed=parsed,
            params=dict(config.params or {}),
            headers=existing_headers,
            host=host,
            amz_date=amz_date_val,
            payload_hash=payload_hash_val,
        )

        canonical_request_hash = _sha256_hex(canonical_request.encode("utf-8"))
        credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
        string_to_sign = "\n".join(
            ["AWS4-HMAC-SHA256", amz_date_val, credential_scope, canonical_request_hash]
        )

        signing_key = _sigv4_signing_key(
            app_auth.aws_secret_access_key, date_stamp, region, service
        )
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        signed_headers = _signed_headers_list(
            headers=existing_headers,
            host=host,
            amz_date=amz_date_val,
            payload_hash=payload_hash_val,
        )
        authorization_value = (
            "AWS4-HMAC-SHA256 "
            f"Credential={app_auth.aws_access_key_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        )

        # Return only headers we must inject; keep existing casing when possible.
        auth_key = _find_existing_header_key(existing_headers, "authorization") or "Authorization"

        out: Dict[str, str] = {
            auth_key: authorization_value,
            amz_date_key: amz_date_val,
        }

        # For S3, x-amz-content-sha256 must be included (and is part of signature).
        # For other services it's optional, but harmless; we include it to make signature verifiable.
        out[payload_hash_key] = payload_hash_val
        return out


def _headers_to_sign(
    headers: Dict[str, str],
    host: str,
    amz_date: str,
    payload_hash: str,
) -> Dict[str, str]:
    """
    Return a lowercase-keyed dict of headers to include in the canonical request.
    We keep this minimal & stable:
    - host (always)
    - x-amz-date (always)
    - x-amz-content-sha256 (always; required for S3, ok for others)
    - any x-amz-* headers provided by caller (e.g. x-amz-acl, x-amz-security-token)
    - content-type / content-md5 if caller provided them
    """
    # Lowercase view of user headers
    lower_in: Dict[str, str] = {}
    for k, v in (headers or {}).items():
        if not isinstance(k, str):
            continue
        if v is None:
            continue
        lower_in[k.lower()] = str(v)

    to_sign: Dict[str, str] = {
        "host": host,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
    }

    for k, v in lower_in.items():
        if k in ("authorization", "user-agent", "connection"):
            continue
        if k.startswith("x-amz-"):
            to_sign[k] = v
        if k in ("content-type", "content-md5"):
            to_sign[k] = v

    return to_sign


def _signed_headers_list(
    headers: Dict[str, str],
    host: str,
    amz_date: str,
    payload_hash: str,
) -> str:
    to_sign = _headers_to_sign(headers, host, amz_date, payload_hash)
    return ";".join(sorted(to_sign.keys()))


def _build_canonical_request(
    method: str,
    parsed,
    params: Dict[str, object],
    headers: Dict[str, str],
    host: str,
    amz_date: str,
    payload_hash: str,
) -> str:
    canonical_uri = _canonical_uri(parsed.path)

    canonical_query = _canonical_query_string(parsed.query, extra_params=params)

    to_sign = _headers_to_sign(headers, host, amz_date, payload_hash)
    # Sorted by lowercase header name
    sorted_header_names = sorted(to_sign.keys())
    canonical_headers = "".join(
        f"{k}:{_normalize_header_value(str(to_sign[k]))}\n" for k in sorted_header_names
    )

    signed_headers = ";".join(sorted_header_names)
    return "\n".join(
        [
            method,
            canonical_uri,
            canonical_query,
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )
