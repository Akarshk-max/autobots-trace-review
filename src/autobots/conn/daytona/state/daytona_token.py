# src/autobots/daytona/daytona_token.py
import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict

from fastapi import HTTPException

from src.autobots.core.settings import SettingsProvider

# Audience claim for connections-bridge tokens. Artifact/job tokens carry no
# audience, so the two token families can never be used interchangeably.
CONNECTIONS_TOKEN_AUD = "daytona:connections"


def create_job_token(*, user_id: str, job_id: str, s3_prefix: str, ttl_seconds: int = 7 * 24 * 3600) -> str:
    settings = SettingsProvider.sget()
    payload = {
        "user_id": user_id,
        "job_id": job_id,
        "s3_prefix": s3_prefix,
        "exp": int(time.time()) + int(ttl_seconds),
    }
    payload_b = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_b).decode("utf-8").rstrip("=")
    sig = hmac.new(settings.JWT_SECRET_KEY.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def verify_job_token(token: str) -> Dict[str, Any]:
    settings = SettingsProvider.sget()
    try:
        payload_b64, sig = token.rsplit(".", 1)
    except ValueError:
        raise HTTPException(status_code=401, detail="Malformed job token")

    expected = hmac.new(settings.JWT_SECRET_KEY.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(status_code=401, detail="Invalid job token signature")

    try:
        pad = "=" * ((4 - len(payload_b64) % 4) % 4)
        raw = base64.urlsafe_b64decode(payload_b64 + pad)
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=401, detail="Malformed job token payload")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=401, detail="Malformed job token payload")
    if int(payload.get("exp", 0)) < int(time.time()):
        raise HTTPException(status_code=401, detail="Job token expired")
    if payload.get("aud") == CONNECTIONS_TOKEN_AUD:
        # A connections-bridge token must never be accepted as an artifact/job token.
        raise HTTPException(status_code=401, detail="Invalid job token audience")

    return payload


def create_connections_token(*, user_id: str, job_id: str, ttl_seconds: int) -> str:
    """
    Signed, short lived token handed to a Daytona sandbox process so it can reach the
    connections bridge (/v1/daytona/connections/...) on behalf of one job only.
    """
    settings = SettingsProvider.sget()
    payload = {
        "aud": CONNECTIONS_TOKEN_AUD,
        "user_id": user_id,
        "job_id": job_id,
        "exp": int(time.time()) + int(ttl_seconds),
    }
    payload_b = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_b).decode("utf-8").rstrip("=")
    sig = hmac.new(settings.JWT_SECRET_KEY.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def verify_connections_token(token: str) -> Dict[str, Any]:
    settings = SettingsProvider.sget()
    try:
        payload_b64, sig = token.rsplit(".", 1)
    except ValueError:
        raise HTTPException(status_code=401, detail="Malformed connections token")

    expected = hmac.new(settings.JWT_SECRET_KEY.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(status_code=401, detail="Invalid connections token signature")

    try:
        pad = "=" * ((4 - len(payload_b64) % 4) % 4)
        raw = base64.urlsafe_b64decode(payload_b64 + pad)
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=401, detail="Malformed connections token payload")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=401, detail="Malformed connections token payload")
    if payload.get("aud") != CONNECTIONS_TOKEN_AUD:
        raise HTTPException(status_code=401, detail="Invalid connections token audience")
    if not payload.get("user_id") or not payload.get("job_id"):
        raise HTTPException(status_code=401, detail="Invalid connections token payload")
    if int(payload.get("exp", 0)) < int(time.time()):
        raise HTTPException(status_code=401, detail="Connections token expired")

    return payload
