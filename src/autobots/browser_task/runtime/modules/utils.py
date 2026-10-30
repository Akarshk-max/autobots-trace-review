import hashlib
import json
import os
import tempfile
from io import BytesIO
from typing import Any, Optional, Tuple

from src.autobots.browser_task.inmem.state import JobEnvelope
from src.autobots.browser_task.runtime.artifacts import Artifact, ArtifactKind
from src.autobots.conn.aws.aws_s3 import get_public_s3


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def bounded_bytes(data: bytes, max_bytes: int) -> Tuple[bytes, bool]:
    if max_bytes <= 0:
        return data, False
    if len(data) <= max_bytes:
        return data, False
    return data[:max_bytes], True


def safe_json_loads(b: bytes) -> Any:
    try:
        return json.loads(b.decode("utf-8", errors="strict"))
    except Exception:
        try:
            return json.loads(b.decode("utf-8", errors="replace"))
        except Exception:
            return None
        
def log_ctx(job) -> dict:
    ctx = getattr(job, "ctx", None)
    return {
        "job_id": getattr(job, "job_id", None),
        "request_id": getattr(job, "request_id", None),
        "attempt": getattr(job, "attempt_no", None),
        "trace_id": getattr(ctx, "trace_id", None),
    }

async def upload_bytes_public_s3(
    *,
    job: JobEnvelope,
    data: bytes,
    filename: str,
    mime: str,
    kind: ArtifactKind,
    label: Optional[str] = None,
    selector: Optional[str] = None,
    meta: Optional[dict] = None,
) -> Artifact:
    """
    Upload bytes to public S3, grouped by request/job.
    Result is an Artifact with s3_url.
    """
    s3 = get_public_s3(object_prefix="playwright-artifacts")
    rel_name = f"{job.request_id}/{job.job_id}/{filename}"
    file_obj = BytesIO(data)

    url = await s3.upload_fileobj(ctx=job.ctx, file_obj=file_obj, filename=rel_name)

    return Artifact(
        kind=kind,
        label=label,
        s3_url=str(url) if url else None,
        mime=mime,
        byte_length=len(data),
        sha256=sha256_bytes(data),
        source_url=str(job.plan.url),
        selector=selector,
        meta={**(meta or {}), "path": rel_name},
    )


def mk_temp_path(prefix: str, suffix: str) -> str:
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    os.close(fd)
    return path


def try_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except Exception:
        pass


def guess_mime_from_filename(name: str) -> str:
    import mimetypes

    t, _ = mimetypes.guess_type(name)
    return t or "application/octet-stream"