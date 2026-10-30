import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

TRACE_LOG_DIR = os.getenv("TRACE_LOG_DIR", os.path.join(Path.home(), ".kiwi", "log", "autobots"))
TRACE_LOG_FILE = os.path.join(TRACE_LOG_DIR, "tracing.log")
TRACE_LOG_MAX_BYTES = int(os.getenv("TRACE_LOG_MAX_BYTES", 10 * 1024 * 1024))  # 10 MB
TRACE_LOG_BACKUP_COUNT = int(os.getenv("TRACE_LOG_BACKUP_COUNT", 5))


def _setup_file_logging() -> None:
    """Add a rotating file handler so trace-related logs persist on disk
    when Datadog is unavailable."""

    logger = logging.getLogger(__name__)
    Path(TRACE_LOG_DIR).mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        TRACE_LOG_FILE,
        maxBytes=TRACE_LOG_MAX_BYTES,
        backupCount=TRACE_LOG_BACKUP_COUNT,
    )
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s - %(message)s"))
    handler.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.info("Datadog tracing unavailable; logging to %s", TRACE_LOG_FILE)


def enable_ddtrace_web_tracing() -> None:
    """Best-effort ddtrace instrumentation for FastAPI/Starlette apps.

    Nuitka standalone builds can miss some optional ddtrace integration modules.
    This function avoids hard startup failures and applies the best available
    web framework instrumentation.

    If ddtrace cannot be loaded or patched, a rotating file logger is set up
    at ``TRACE_LOG_DIR/tracing.log`` so that trace-level diagnostics are not
    lost.
    """

    logger = logging.getLogger(__name__)

    try:
        from ddtrace import patch
    except Exception as exc:
        logger.warning("ddtrace import failed; tracing disabled: %s", exc)
        _setup_file_logging()
        return

    try:
        patch(fastapi=True)
        return
    except Exception as fastapi_exc:
        logger.warning("ddtrace fastapi patch failed; trying starlette: %s", fastapi_exc)

    try:
        patch(starlette=True)
    except Exception as starlette_exc:
        logger.warning("ddtrace starlette patch failed; tracing disabled: %s", starlette_exc)
        _setup_file_logging()
