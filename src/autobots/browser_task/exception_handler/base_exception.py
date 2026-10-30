from enum import Enum
from typing import Any, Dict

from fastapi import HTTPException
from pydantic import BaseModel, Field

from src.autobots.browser_task.inmem.state import JobEnvelope


class ErrorCode(str, Enum):
    invalid_intent = "invalid_intent"
    service_unavailable = "service_unavailable"
    queue_full = "queue_full"
    timeout = "timeout"
    internal_error = "internal_error"

    http_403 = "http_403"
    http_404 = "http_404"
    http_429 = "http_429"
    http_5xx = "http_5xx"

    blocked_signal = "blocked_signal"
    expectation_failed = "expectation_failed"

    budget_total = "budget_total"
    budget_requests = "budget_requests"

    network_error = "network_error"
    storage_state_error = "storage_state_error"

    cancelled = "cancelled"
    unknown = "unknown"


class ErrorInfo(BaseModel):
    code: ErrorCode
    message: str
    retryable: bool = False
    details: Dict[str, Any] = Field(default_factory=dict)


class JobError(BaseModel):
    ok: bool = False
    status: str = "failed"

    job_id: str
    request_id: str
    url: str
    policy_id: str
    attempt: int

    error: ErrorInfo


class RequestError(BaseModel):
    ok: bool = False
    request_id: str
    error: ErrorInfo


class ScrapeException(Exception):
    code: ErrorCode
    retryable: bool = False
    http_status: int = 500

    def __init__(self, message: str, *, details: Dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


class InvalidIntent(ScrapeException):
    code = ErrorCode.invalid_intent
    http_status = 422


class QueueFull(ScrapeException):
    code = ErrorCode.queue_full
    retryable = True
    http_status = 503


class ServiceUnavailable(ScrapeException):
    code = ErrorCode.service_unavailable
    retryable = True
    http_status = 503


class RequestTimeout(ScrapeException):
    code = ErrorCode.timeout
    retryable = True
    http_status = 504



def normalize_job_failure(env: JobEnvelope, exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, ScrapeException):
        err = ErrorInfo(
            code=exc.code,
            message=str(exc),
            retryable=exc.retryable,
            details=exc.details,
        )
    else:
        err = ErrorInfo(
            code=ErrorCode.unknown,
            message=str(exc) or "unknown_error",
            retryable=False,
        )

    return JobError(
        job_id=env.job_id,
        request_id=env.request_id,
        url=str(env.plan.url),
        policy_id=env.plan.policy_id,
        attempt=env.attempt_no,
        error=err,
    ).model_dump(mode="json")


def raise_http_error(exc: ScrapeException, *, request_id: str) -> None:
    raise HTTPException(
        status_code=exc.http_status,
        detail=RequestError(
            request_id=request_id,
            error=ErrorInfo(
                code=exc.code,
                message=str(exc),
                retryable=exc.retryable,
                details=exc.details,
            ),
        ).model_dump(mode="json"),
    )
