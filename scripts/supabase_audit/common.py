from __future__ import annotations

import os
from pathlib import Path


def load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv  # type: ignore
    except Exception:
        return
    here = Path(__file__).resolve().parent
    candidates = [
        here / ".env.supabase_audit",
        here / ".env",
        Path.cwd() / ".env.supabase_audit",
        Path.cwd() / ".env",
    ]
    for p in candidates:
        if p.exists():
            load_dotenv(dotenv_path=str(p))
            return
    load_dotenv()


def env_required(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


def env_optional(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v
