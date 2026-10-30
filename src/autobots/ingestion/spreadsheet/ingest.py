from __future__ import annotations

import asyncio
import csv
import gc
import hashlib
import io
import math
import os
import re
import tempfile
import time
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Literal, Optional, Tuple, Union

import httpx
from loguru import logger

from src.autobots.conn.chroma.chroma import Document
from src.autobots.conn.neo4j.ontology import ChunkNode, EntityNode, ExtractedGraph, Relationship
from src.autobots.data_model.context import Context
from src.autobots.ingestion.exceptions import RetryableIngestionError
from src.autobots.ingestion.spreadsheet.locator import build_source_locator, parse_source_locator
from src.autobots.ingestion.spreadsheet.merges import apply_merge_fill_to_row, build_merge_fill_map
from src.autobots.ingestion.spreadsheet.pipeline import (
    SheetIngestConfig,
    ingest_sheet_from_rows_iter,
)
from src.autobots.ingestion.spreadsheet.utils import rightmost_nonempty_index_1based

# Optional deps (soft)
try:  # .xlsx/.xlsm
    import openpyxl
except Exception:  # pragma: no cover
    openpyxl = None

try:  # .xlsb
    from pyxlsb import open_workbook as open_xlsb_workbook
except Exception:  # pragma: no cover
    open_xlsb_workbook = None

try:  # .xls (legacy)
    import xlrd  # type: ignore
except Exception:  # pragma: no cover
    xlrd = None

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None

try:  # pragma: no cover (POSIX only)
    import fcntl  # type: ignore
except Exception:  # pragma: no cover
    fcntl = None


# ---------------------------------------------------------------------
# Limits / caps (kept in this file per requirement)
# ---------------------------------------------------------------------

# Reject if compressed file (the uploaded object) exceeds this size.
# This prevents 1GB uploads from being processed at all.
MAX_SPREADSHEET_BYTES_HARD = 250 * 1024 * 1024  # 250MB

# XLSX/XLSM zip-bomb protection. This cap applies to sum(uncompressed member sizes).
# If cgroup memory limit is detectable, we use a fraction of it.
DEFAULT_UNCOMPRESSED_CAP_BYTES = 2 * 1024 * 1024 * 1024  # 2GiB fallback
UNCOMPRESSED_CAP_FRACTION_OF_CGROUP = 0.65
UNCOMPRESSED_CAP_MIN_BYTES = 512 * 1024 * 1024  # never less than 512MiB
ZIP_MAX_MEMBERS = 50_000
ZIP_BOMB_RATIO_LIMIT = 80.0  # reject if uncompressed_total / compressed_bytes is extreme

# Targeted XLSX guard: sharedStrings.xml is a frequent openpyxl memory killer.
SHARED_STRINGS_MEMBER_NAME = "xl/sharedStrings.xml"
SHARED_STRINGS_MAX_ABS_BYTES = 350 * 1024 * 1024  # 350MB hard guard
SHARED_STRINGS_MAX_FRACTION_OF_CAP = 0.20         # also cap as fraction of uncompressed cap
SHARED_STRINGS_MIN_BYTES = 64 * 1024 * 1024       # never allow huge sharedStrings on tiny machines

# Per-sheet row cap. Reject rather than sample.
MAX_ROWS_PER_SHEET = 200_000

# Chunking caps
MAX_SHEETS = 50
HEADER_SCAN_ROWS = 60
MAX_COLS_INCLUDED = 60         # columns per chunk window (vertical chunking uses multiple windows)
MAX_COLS_HARD = 300            # hard reject if sheet/row exceeds this many columns (avoid runaway width)
MAX_ROWS_PER_CHUNK = 160       # let char/token caps be the main limiter; conservative bump from 80
MAX_CHARS_PER_CHUNK = 45_000
# Reduce risk of exceeding embedding model per-input token limits (e.g. 8192 tokens).
SPREADSHEET_MAX_CHARS_PER_CHUNK_FOR_VECTORIZATION = 12_000
# Token-aware cap (tiktoken is in pyproject). Keep headroom below 8192.
SPREADSHEET_MAX_TOKENS_PER_CHUNK_FOR_VECTORIZATION = 7000

# Cross-process lock: serialize spreadsheet parsing/chunking per container (across uvicorn workers).
SPREADSHEET_GLOBAL_LOCK_PATH = "/tmp/autobots_spreadsheet_ingest.lock"

# Telemetry tuning:
# - We update estimates frequently but rate-limit logs by time.
# - Designed so you can throw large files and observe RSS + estimates.
TELEMETRY_LOG_INTERVAL_SECONDS = 0.5
TELEMETRY_RSS_SAMPLE_INTERVAL_SECONDS = 0.25
CSV_TELEMETRY_UPDATE_EVERY_N_ROWS = 50
# XLSX/XLSM/XLS progress sampling cadence (best-effort)
XLSX_TELEMETRY_UPDATE_EVERY_N_ROWS = 200

# Total extracted text cap (early reject to avoid RAM blowups while accumulating docs).
# This is enforced inside _add_doc() before appending.
MAX_TOTAL_TEXT_CHARS = 25_000_000

# Global chunk count cap per workbook (reject if exceeded)
MAX_TOTAL_CHUNKS = 6000
# Workbook summary doc is inserted at the end; reserve one slot.
MAX_TOTAL_CHUNKS_BEFORE_WORKBOOK_SUMMARY = MAX_TOTAL_CHUNKS - 1

# --- Dynamic scaling (RAM-aware) ---
# These do NOT change your policy defaults; they compute effective limits per job based on RAM.
# Absolute hard ceilings to prevent pathological runs even on huge machines.
MAX_TOTAL_TEXT_CHARS_ABS_HARD = 500_000_000   # 500M chars
MAX_TOTAL_CHUNKS_ABS_HARD = 60_000            # avoid unbounded Weaviate/Neo4j load

# Fractions/tunables
TEXT_BUDGET_FRACTION_OF_MEM = 0.35            # reserve ~35% RAM for accumulated chunk text
BYTES_PER_CHAR_BUDGET = 4.0                   # conservative bytes/char for Python unicode + overhead
TARGET_AVG_CHARS_PER_CHUNK_FOR_CHUNK_CAP = 10_000  # used to derive a chunk cap from char cap

FALLBACK_MEM_LIMIT_BYTES = 4 * 1024 * 1024 * 1024  # if we can't detect, assume 4GB

def compute_effective_spreadsheet_caps(mem_limit_bytes: Optional[int]) -> Tuple[int, int]:
    """
    Returns (effective_max_total_text_chars, effective_max_total_chunks).
    Keeps current constants as minimums but scales up with memory.
    """
    mem = int(mem_limit_bytes or FALLBACK_MEM_LIMIT_BYTES)
    text_budget_bytes = int(mem * TEXT_BUDGET_FRACTION_OF_MEM)
    dyn_chars = int(text_budget_bytes / BYTES_PER_CHAR_BUDGET)
    effective_chars = max(MAX_TOTAL_TEXT_CHARS, min(MAX_TOTAL_TEXT_CHARS_ABS_HARD, dyn_chars))
    dyn_chunks = int(math.ceil(effective_chars / float(TARGET_AVG_CHARS_PER_CHUNK_FOR_CHUNK_CAP)))
    effective_chunks = max(MAX_TOTAL_CHUNKS, min(MAX_TOTAL_CHUNKS_ABS_HARD, dyn_chunks))
    return effective_chars, effective_chunks

# Neo4j ingestion strategy for spreadsheets.
# - "llm": always run GraphRAGProcessor (LLM extraction) on all chunks (only allowed for small files)
# - "deterministic": never run LLM; build deterministic graph (chunk adjacency + sheet nodes)
# - "hybrid": LLM for small spreadsheets; deterministic for larger ones
SPREADSHEET_NEO4J_MODE: Literal["llm", "deterministic", "hybrid"] = "deterministic"

# Thresholds for allowing LLM extraction in spreadsheet mode (also used in hybrid):
SPREADSHEET_LLM_MAX_BYTES = 25 * 1024 * 1024  # 25MB
SPREADSHEET_LLM_MAX_CHUNKS = 300
SPREADSHEET_LLM_MAX_TOTAL_CHARS = 2_500_000  # 2.5M chars total (guard for "small bytes, huge text")

# Bulkhead: limit concurrent spreadsheet parsing per worker process
SPREADSHEET_PARSE_SEMAPHORE = asyncio.Semaphore(1)


# ---------------------------------------------------------------------
# Utilities: cgroup memory limit, RSS logging (best-effort only)
# ---------------------------------------------------------------------

@contextmanager
def _spreadsheet_global_lock(*, ctx: Context, suffix: str, filename: str) -> Iterable[None]:
    """
    Per-container (cross-process) lock to prevent multiple heavy spreadsheet parses
    from running concurrently within the same container/VM when uvicorn has multiple workers.
    """
    if fcntl is None:
        # Non-POSIX env: best-effort no-op.
        yield
        return

    fd = None
    t0 = time.monotonic()
    try:
        fd = open(SPREADSHEET_GLOBAL_LOCK_PATH, "a+", encoding="utf-8")
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        waited = time.monotonic() - t0
        if waited >= 0.25:
            logger.bind(ctx=ctx, suffix=suffix, filename=filename, waited_s=round(waited, 3)).info(
                "Acquired spreadsheet global lock after waiting"
            )
        yield
    finally:
        try:
            if fd is not None:
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
                fd.close()
        except Exception:
            pass

def _read_int_file(path: str) -> Optional[int]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw or raw == "max":
            return None
        return int(raw)
    except Exception:
        return None


def _read_text_file(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        return raw or None
    except Exception:
        return None


def get_cgroup_memory_limit_bytes() -> Optional[int]:
    """
    Best-effort:
      - cgroup v2: /sys/fs/cgroup/memory.max
      - cgroup v1: /sys/fs/cgroup/memory/memory.limit_in_bytes
    Returns None if unknown/unlimited.
    """
    # v2
    v2 = _read_int_file("/sys/fs/cgroup/memory.max")
    if v2 and v2 > 0:
        return v2

    # v1
    v1 = _read_int_file("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    if v1 and v1 > 0:
        # Heuristic: some platforms report a huge number when "unlimited"
        if v1 >= (1 << 60):
            return None
        return v1
    return None


def get_cgroup_cpu_limit_cores() -> Optional[float]:
    """
    Best-effort CPU limit in cores.
    Works in ECS Fargate and other container runtimes using cgroups.

    cgroup v2: /sys/fs/cgroup/cpu.max -> "<quota> <period>" or "max <period>"
    cgroup v1: /sys/fs/cgroup/cpu/cpu.cfs_quota_us and cpu.cfs_period_us
    """
    raw = _read_text_file("/sys/fs/cgroup/cpu.max")
    if raw:
        parts = raw.split()
        if len(parts) >= 2:
            quota_s, period_s = parts[0], parts[1]
            if quota_s != "max":
                try:
                    quota = float(quota_s)
                    period = float(period_s)
                    if quota > 0 and period > 0:
                        return quota / period
                except Exception:
                    pass

    quota = _read_int_file("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    period = _read_int_file("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if quota and period and quota > 0 and period > 0:
        try:
            return float(quota) / float(period)
        except Exception:
            return None
    return None


def get_effective_memory_limit_bytes() -> Optional[int]:
    """
    Prefer cgroup limit when present (ECS Fargate).
    Fall back to host memory (Lightsail dev).
    """
    lim = get_cgroup_memory_limit_bytes()
    if lim:
        return lim
    if psutil:
        try:
            return int(psutil.virtual_memory().total)
        except Exception:
            return None
    return None


def get_effective_cpu_cores() -> Optional[float]:
    """
    Prefer cgroup CPU quota when present (ECS Fargate).
    Fall back to host cpu_count (Lightsail dev).
    """
    lim = get_cgroup_cpu_limit_cores()
    if lim and lim > 0:
        return lim
    try:
        c = os.cpu_count()
        return float(c) if c else None
    except Exception:
        return None


def _rss_bytes_best_effort() -> Optional[int]:
    if not psutil:
        return None
    try:
        return psutil.Process().memory_info().rss
    except Exception:
        return None


def _format_bytes(n: Optional[int]) -> str:
    if n is None:
        return "unknown"
    try:
        n = int(n)
    except Exception:
        return str(n)
    units = ["B", "KB", "MB", "GB", "TB"]
    x = float(n)
    u = 0
    while x >= 1024.0 and u < len(units) - 1:
        x /= 1024.0
        u += 1
    return f"{x:.2f}{units[u]}"


@dataclass
class SpreadsheetTelemetry:
    """
    High-frequency best-effort telemetry for estimating:
      - CSV density: chars/byte, estimated total chars at EOF
      - Memory: RSS slope per extracted char, estimated final RSS
    Works in Lightsail and ECS (via effective mem/cpu detection).
    """
    filename: str
    suffix: str
    file_size_bytes: int
    mem_limit_bytes: Optional[int]
    cpu_cores: Optional[float]
    rss_start: Optional[int]

    # progress
    extracted_chars_so_far: int = 0
    docs_so_far: int = 0
    bytes_read_so_far: int = 0  # CSV only

    # sampled memory
    rss_now: Optional[int] = None
    rss_peak: Optional[int] = None

    # estimates
    chars_per_byte: Optional[float] = None
    est_total_chars: Optional[int] = None
    rss_per_char: Optional[float] = None
    est_final_rss: Optional[int] = None

    # best-effort progress (csv_bytes or xlsx_rows)
    progress_kind: Optional[str] = None  # "csv_bytes" | "xlsx_rows"
    progress_ratio: Optional[float] = None  # 0..1 overall workbook progress
    progress_confidence: str = "none"  # "high" | "low" | "none"

    # xlsx/xlsm sheet context (best-effort)
    sheet_name: Optional[str] = None
    sheet_index_0: Optional[int] = None
    sheets_total: Optional[int] = None
    declared_max_row: Optional[int] = None
    current_row_1based: Optional[int] = None

    # rate limiting
    _last_log_ts: float = 0.0
    _last_rss_ts: float = 0.0

    def update_bytes_read(self, b: int) -> None:
        if b and b > self.bytes_read_so_far:
            self.bytes_read_so_far = int(b)
            if self.bytes_read_so_far > 0 and self.extracted_chars_so_far > 0:
                self.chars_per_byte = float(self.extracted_chars_so_far) / float(self.bytes_read_so_far)
                self.est_total_chars = int(self.chars_per_byte * float(self.file_size_bytes))
                self.progress_kind = "csv_bytes"
                # percent by bytes (works well for CSV)
                self.progress_ratio = min(1.0, float(self.bytes_read_so_far) / max(1.0, float(self.file_size_bytes)))
                self.progress_confidence = "high"

    def update_xlsx_progress(
        self,
        *,
        sheet_name: str,
        sheet_index_0: int,
        sheets_total: int,
        current_row_1based: int,
        declared_max_row: int,
        max_rows_cap: int,
    ) -> None:
        """
        Best-effort XLSX/XLSM progress model.
        Uses ws.max_row as denominator (often imperfect due to formatting).

        - If declared_max_row looks sane (<= max_rows_cap), confidence is higher.
        - If declared_max_row is huge, confidence is low, but we still emit a ratio.
        """
        self.progress_kind = "xlsx_rows"
        self.sheet_name = sheet_name
        self.sheet_index_0 = int(sheet_index_0)
        self.sheets_total = int(max(1, sheets_total))
        self.current_row_1based = int(current_row_1based)
        self.declared_max_row = int(declared_max_row) if declared_max_row is not None else None

        denom = float(declared_max_row or 0)
        if denom <= 0:
            self.progress_ratio = None
            self.progress_confidence = "none"
            return

        # Per-sheet ratio
        sheet_ratio = min(1.0, float(self.current_row_1based) / denom)

        # Overall workbook ratio: equal-weight sheets assumption
        self.progress_ratio = min(
            1.0,
            (float(self.sheet_index_0) + sheet_ratio) / float(self.sheets_total),
        )

        # Confidence heuristic:
        # - high only when declared_max_row is within our ingest row cap AND workbook is 1 sheet
        # - otherwise low (formatting inflation or multi-sheet variability)
        if (declared_max_row > 0) and (declared_max_row <= max_rows_cap) and (self.sheets_total == 1):
            self.progress_confidence = "high"
        else:
            self.progress_confidence = "low"

        # If we have some progress ratio, estimate final chars from current extraction.
        # Guard against tiny ratios to avoid absurd estimates early.
        if self.extracted_chars_so_far > 0 and self.progress_ratio and self.progress_ratio >= 0.02:
            try:
                self.est_total_chars = int(float(self.extracted_chars_so_far) / float(self.progress_ratio))
            except Exception:
                pass

    def maybe_sample_rss(self) -> None:
        now = time.monotonic()
        if (now - self._last_rss_ts) < TELEMETRY_RSS_SAMPLE_INTERVAL_SECONDS:
            return
        self._last_rss_ts = now
        self.rss_now = _rss_bytes_best_effort()
        if self.rss_now is not None:
            if self.rss_peak is None or self.rss_now > self.rss_peak:
                self.rss_peak = self.rss_now

        # update rss_per_char and est_final_rss if possible
        if (
            self.rss_start is not None
            and self.rss_now is not None
            and self.extracted_chars_so_far > 0
            and self.rss_now >= self.rss_start
        ):
            try:
                self.rss_per_char = float(self.rss_now - self.rss_start) / float(self.extracted_chars_so_far)
            except Exception:
                self.rss_per_char = None

        # Forecast final RSS if we have an estimated total chars (CSV or XLSX best-effort).
        if self.rss_now is not None and self.rss_per_char is not None and self.est_total_chars is not None:
            try:
                remaining = float(max(0, self.est_total_chars - self.extracted_chars_so_far))
                self.est_final_rss = int(float(self.rss_now) + self.rss_per_char * remaining)
            except Exception:
                self.est_final_rss = None

    def maybe_log_progress(self, ctx: Context) -> None:
        now = time.monotonic()
        if (now - self._last_log_ts) < TELEMETRY_LOG_INTERVAL_SECONDS:
            return
        self._last_log_ts = now
        headroom = None
        if self.mem_limit_bytes and self.rss_now:
            headroom = max(0, int(self.mem_limit_bytes - self.rss_now))
        logger.bind(
            ctx=ctx,
            filename=self.filename,
            rss_now_bytes=self.rss_now,
            rss_peak_bytes=self.rss_peak,
            mem_limit_bytes=self.mem_limit_bytes,
            suffix=self.suffix,
            file_size_bytes=self.file_size_bytes,
            bytes_read_so_far=self.bytes_read_so_far or None,
            extracted_chars_so_far=self.extracted_chars_so_far,
            docs_so_far=self.docs_so_far,
            chars_per_byte=None if self.chars_per_byte is None else round(self.chars_per_byte, 4),
            est_total_chars=self.est_total_chars,
            progress_kind=self.progress_kind,
            progress_ratio=None if self.progress_ratio is None else round(self.progress_ratio, 4),
            progress_confidence=self.progress_confidence,
            sheet_name=self.sheet_name,
            sheet_index_0=self.sheet_index_0,
            sheets_total=self.sheets_total,
            declared_max_row=self.declared_max_row,
            current_row_1based=self.current_row_1based,
            rss_now=_format_bytes(self.rss_now),
            rss_peak=_format_bytes(self.rss_peak),
            rss_start=_format_bytes(self.rss_start),
            rss_per_char=None if self.rss_per_char is None else round(self.rss_per_char, 2),
            est_final_rss=_format_bytes(self.est_final_rss),
            mem_limit=_format_bytes(self.mem_limit_bytes),
            mem_headroom_now=_format_bytes(headroom),
            cpu_cores=None if self.cpu_cores is None else round(self.cpu_cores, 2),
        ).debug("Spreadsheet ingest telemetry")


# ---------------------------------------------------------------------
# Download to tempfile (streaming)
# ---------------------------------------------------------------------

async def download_to_tempfile(
    ctx: Context,
    url: str,
    suffix: str,
    max_bytes: int,
    timeout_seconds: float = 240.0,
) -> Tuple[Path, int]:
    """
    Stream download URL to a temp file with a hard byte limit.
    Returns (path, bytes_written).
    """
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp_path = Path(tmp.name)
    tmp.close()

    timeout = httpx.Timeout(connect=10.0, read=timeout_seconds, write=30.0, pool=30.0)
    written = 0
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            # HEAD preflight (best effort)
            try:
                cl = None
                async with client.stream("HEAD", url, timeout=httpx.Timeout(10.0)) as head:
                    if head.status_code < 400:
                        cl = head.headers.get("content-length")

                if cl:
                    try:
                        cl_int = int(cl)
                    except Exception:
                        cl_int = None
                    if cl_int is not None and cl_int > max_bytes:
                        raise ValueError(
                            f"Spreadsheet exceeds size limit ({cl_int} bytes > {max_bytes} bytes)."
                        )
            except (httpx.RequestError, httpx.HTTPStatusError):
                # Network/HTTP failure on HEAD is non-fatal; we'll enforce during streaming GET.
                pass

            # GET with bounded retries for transient failures (S3 eventual consistency, 5xx, timeouts)
            # NOTE: 403 is usually ACL/permissions, not transient; do not retry by default.
            RETRYABLE_STATUSES = {404, 408, 429, 500, 502, 503, 504}
            MAX_ATTEMPTS = 3
            backoff = 1.0

            for attempt in range(1, MAX_ATTEMPTS + 1):
                written = 0
                try:
                    async with client.stream("GET", url) as resp:
                        # Treat auth/ACL failures as permanent (do not retry).
                        if resp.status_code in (401, 403):
                            raise ValueError(
                                f"Failed to download spreadsheet (HTTP {resp.status_code}). "
                                "The source URL is not accessible (authorization/permissions)."
                            )
                        if resp.status_code in RETRYABLE_STATUSES:
                            raise httpx.HTTPStatusError(
                                f"Retryable status {resp.status_code}",
                                request=resp.request,
                                response=resp,
                            )
                        resp.raise_for_status()
                        with open(tmp_path, "wb") as f:
                            async for chunk in resp.aiter_bytes():
                                if not chunk:
                                    continue
                                written += len(chunk)
                                if written > max_bytes:
                                    raise ValueError(
                                        f"Spreadsheet exceeds size limit during download ({written} bytes > {max_bytes} bytes)."
                                    )
                                f.write(chunk)
                    break
                except ValueError:
                    # permanent (size) failures: do not retry
                    raise
                except httpx.HTTPStatusError as e:
                    status = e.response.status_code if e.response is not None else None
                    # Permanent 4xx (except retryables) should not be retried by the worker.
                    if status is not None and (400 <= status < 500) and (status not in RETRYABLE_STATUSES):
                        raise ValueError(
                            f"Failed to download spreadsheet (HTTP {status}). The source URL is not accessible."
                        ) from e
                    if status in RETRYABLE_STATUSES and attempt < MAX_ATTEMPTS:
                        logger.bind(ctx=ctx).warning(
                            f"Retrying spreadsheet download after HTTP {status} (attempt {attempt}/{MAX_ATTEMPTS})"
                        )
                        await asyncio.sleep(backoff)
                        backoff *= 2.0
                        continue
                    raise RetryableIngestionError(f"spreadsheet_download_failed_http_{status}") from e
                except (httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError, httpx.RequestError) as e:
                    if attempt < MAX_ATTEMPTS:
                        logger.bind(ctx=ctx).warning(
                            f"Retrying spreadsheet download after transport error (attempt {attempt}/{MAX_ATTEMPTS}): {e}"
                        )
                        await asyncio.sleep(backoff)
                        backoff *= 2.0
                        continue
                    raise RetryableIngestionError("spreadsheet_download_failed_transport") from e

        return tmp_path, written
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------
# XLSX/XLSM ZIP preflight
# ---------------------------------------------------------------------

def _compute_uncompressed_cap() -> int:
    lim = get_cgroup_memory_limit_bytes()
    if lim:
        # conservative cap to leave room for parsing overhead
        return max(UNCOMPRESSED_CAP_MIN_BYTES, int(lim * UNCOMPRESSED_CAP_FRACTION_OF_CGROUP))
    return DEFAULT_UNCOMPRESSED_CAP_BYTES


def xlsx_zip_preflight_or_reject(path: Path, compressed_bytes: int) -> Dict[str, Union[int, float]]:
    """
    Opens the xlsx/xlsm ZIP container and rejects if:
      - too many members
      - uncompressed total exceeds cap
      - uncompressed/compressed ratio is extreme (zip bomb heuristic)

    This does NOT decompress file contents.

    Returns best-effort stats for observability:
      {
        "zip_members": ...,
        "zip_uncompressed_total": ...,
        "zip_max_member": ...,
        "zip_shared_strings_bytes": ...,
        "zip_uncompressed_cap": ...,
        "zip_shared_strings_cap": ...,
        "zip_compressed_bytes": ...,
        "zip_expansion_ratio": ...,
      }
    """
    cap = _compute_uncompressed_cap()
    shared_cap = min(
        SHARED_STRINGS_MAX_ABS_BYTES,
        max(SHARED_STRINGS_MIN_BYTES, int(cap * SHARED_STRINGS_MAX_FRACTION_OF_CAP)),
    )
    shared_strings_bytes = 0
    try:
        with zipfile.ZipFile(path, "r") as zf:
            infos = zf.infolist()
            n = len(infos)
            if n > ZIP_MAX_MEMBERS:
                raise ValueError(
                    f"Spreadsheet ZIP has too many entries ({n} > {ZIP_MAX_MEMBERS}). Rejecting as unsafe."
                )
            uncompressed_total = 0
            max_member = 0
            for info in infos:
                fs = int(getattr(info, "file_size", 0) or 0)
                # Targeted guard: sharedStrings.xml
                try:
                    name = str(getattr(info, "filename", "") or "")
                except Exception:
                    name = ""
                if name == SHARED_STRINGS_MEMBER_NAME:
                    shared_strings_bytes = fs
                if name == SHARED_STRINGS_MEMBER_NAME and fs > shared_cap:
                    raise ValueError(
                        f"Spreadsheet sharedStrings.xml is too large ({fs} bytes > {shared_cap} bytes). "
                        "This workbook is likely too memory-intensive to ingest safely. "
                        "Please split/export to CSV or reduce unique strings."
                    )
                uncompressed_total += fs
                if fs > max_member:
                    max_member = fs
                if uncompressed_total > cap:
                    raise ValueError(
                        f"Spreadsheet expands too large when unzipped (uncompressed_total={uncompressed_total} > cap={cap}). "
                        "Please split the workbook or export to CSV."
                    )
            if max_member > cap:
                raise ValueError(
                    "Spreadsheet contains a single very large member file after decompression. "
                    f"max_member={max_member} exceeds cap={cap}. Please split/export."
                )

        ratio = 0.0
        if compressed_bytes > 0:
            ratio = float(uncompressed_total) / float(compressed_bytes)
            if ratio > ZIP_BOMB_RATIO_LIMIT:
                raise ValueError(
                    f"Spreadsheet ZIP expansion ratio too high ({ratio:.1f}x > {ZIP_BOMB_RATIO_LIMIT}x). Rejecting as unsafe."
                )
        return {
            "zip_members": int(n),
            "zip_uncompressed_total": int(uncompressed_total),
            "zip_max_member": int(max_member),
            "zip_shared_strings_bytes": int(shared_strings_bytes),
            "zip_uncompressed_cap": int(cap),
            "zip_shared_strings_cap": int(shared_cap),
            "zip_compressed_bytes": int(compressed_bytes),
            "zip_expansion_ratio": float(ratio),
        }
    except zipfile.BadZipFile as e:
        raise ValueError("Invalid XLSX/XLSM file: not a valid ZIP container.") from e


# ---------------------------------------------------------------------
# DEAD CODE BLOCK (legacy helpers)
# These helpers are superseded by:
#   - src.autobots.ingestion.spreadsheet.layout
#   - src.autobots.ingestion.spreadsheet.pipeline
#   - src.autobots.ingestion.spreadsheet.render
# They are kept temporarily for reference and will be deleted after stabilization.
# START DEAD CODE BLOCK
# ---------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")


def _excel_col_name(idx0: int) -> str:
    """
    0-based index to Excel column letters: A, B, ..., Z, AA, AB...
    """
    n = idx0 + 1
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _norm(s: str) -> str:
    s = (s or "").strip()
    s = _WS_RE.sub(" ", s)
    return s


def _is_empty_cell(v: object) -> bool:
    if v is None:
        return True
    if isinstance(v, str) and not v.strip():
        return True
    return False


def detect_header_row(preview_rows: List[List[object]]) -> Tuple[int, List[str]]:
    """
    Heuristic header detection:
      - high non-empty count
      - high string ratio
      - low numeric ratio
    Returns (header_row_idx_in_preview, header_strings)
    If no good candidate, synthesizes headers col_1..col_n based on max width.

    TODO (future): table-region detection; repeated headers; multi-row header flattening.
    TODO (future): header-only merge handling using openpyxl merged ranges.
    """
    best_score = -1.0
    best_idx = -1
    best_headers: List[str] = []
    max_w = max((len(r) for r in preview_rows), default=0)

    for i, row in enumerate(preview_rows):
        if not row:
            continue
        non_empty = [c for c in row if not _is_empty_cell(c)]
        if not non_empty:
            continue
        str_count = 0
        num_count = 0
        for c in non_empty:
            if isinstance(c, (int, float)):
                num_count += 1
                continue
            if isinstance(c, str):
                s = c.strip()
                if not s:
                    continue
                # Treat numeric-looking strings as numeric-ish
                try:
                    float(s.replace(",", ""))
                    num_count += 1
                except Exception:
                    str_count += 1
                continue
            # Fallback: treat as string-ish
            str_count += 1
        ne = len(non_empty)

        str_ratio = str_count / max(1, ne)
        num_ratio = num_count / max(1, ne)
        density = ne / max(1, max_w)

        score = (2.0 * density) + (2.0 * str_ratio) - (1.0 * num_ratio)
        if score > best_score:
            best_score = score
            best_idx = i
            headers = []
            # Keep header names up to MAX_COLS_HARD so vertical windowing can label wide sheets.
            max_header_cols = min(MAX_COLS_HARD, max_w if max_w else len(row))
            for j in range(max_header_cols):
                c = row[j] if j < len(row) else None
                h = _norm(str(c)) if not _is_empty_cell(c) else _excel_col_name(j)
                headers.append(h)
            best_headers = headers

    if best_idx < 0 or not best_headers:
        # synthesize
        synth = [_excel_col_name(i) for i in range(min(MAX_COLS_HARD, max_w))]
        return 0, synth
    return best_idx, best_headers


def render_rows_as_kv(
    headers: List[str],
    rows: List[List[object]],
    row_start_1based: int,
) -> str:
    """
    Renders rows as key=value pairs for retrieval quality.
    Caps included columns to MAX_COLS_INCLUDED.
    """
    out_lines: List[str] = []
    cols = headers[:MAX_COLS_INCLUDED]
    for idx, row in enumerate(rows):
        ridx = row_start_1based + idx
        parts = []
        for cidx, h in enumerate(cols):
            val = row[cidx] if cidx < len(row) else None
            if _is_empty_cell(val):
                continue
            parts.append(f"{h}={_norm(str(val))}")
        if not parts:
            continue
        out_lines.append(f"- Row {ridx}: " + "; ".join(parts))
    return "\n".join(out_lines)

def _col_range_str(col_start_0: int, col_end_excl_0: int) -> str:
    """
    Returns an Excel-like col range string, e.g. 3..6 => D-F
    """
    if col_end_excl_0 <= col_start_0:
        return _excel_col_name(col_start_0)
    a = _excel_col_name(col_start_0)
    b = _excel_col_name(col_end_excl_0 - 1)
    return f"{a}-{b}" if a != b else a

def _window_headers_with_letters(headers: List[str], col_start_0: int, col_end_excl_0: int) -> str:
    """
    Format: D=Revenue | E=Q1 | F=Q2 ...
    """
    parts: List[str] = []
    for j in range(col_start_0, col_end_excl_0):
        col_letter = _excel_col_name(j)
        name = headers[j] if j < len(headers) else col_letter
        parts.append(f"{col_letter}={name}")
    return " | ".join(parts)

def _render_kv_lines_with_rownums(
    headers: List[str],
    items: List[Tuple[int, List[object]]],
    col_start_0: int,
    col_end_excl_0: int,
) -> List[Tuple[int, str]]:
    """
    Returns a list of (row_num, rendered_line).
    Skips rows that have no non-empty key=value parts.
    """
    out: List[Tuple[int, str]] = []
    cols = headers
    for row_num, row in items:
        parts: List[str] = []
        for j in range(col_start_0, col_end_excl_0):
            h = cols[j] if j < len(cols) else _excel_col_name(j)
            val = row[j] if j < len(row) else None
            if _is_empty_cell(val):
                continue
            s = _norm(str(val))
            if not s:
                continue
            # light sanitization
            s = s.replace("<|", "").replace("###", "")
            parts.append(f"{h}={s}")
        if parts:
            out.append((row_num, f"- Row {row_num}: " + "; ".join(parts)))
    return out

def _flush_buffer_no_loss(
    *,
    headers: List[str],
    buffer: List[Tuple[int, List[object]]],
    original_filename: str,
    sheet_name: str,
    header_row_1based: int,
    add_doc_cb,
    global_order_ref: List[int],
) -> None:
    """
    Emits 1+ chunks from buffer without dropping rows:
      - splits into subchunks
      - enforces MAX_CHARS_PER_CHUNK by reducing rows and/or column-window width (no truncation)
    Consumes the buffer in-place (clears it).
    """
    while buffer:
        # Two-phase: PLAN all windows; only then EMIT and consume.
        take_n = min(MAX_ROWS_PER_CHUNK, len(buffer))
        window_span = MAX_COLS_INCLUDED

        while True:
            items = buffer[:take_n]

            max_cols_seen = 0
            for _, row in items:
                if len(row) > max_cols_seen:
                    max_cols_seen = len(row)

            if max_cols_seen == 0:
                # empty rows => drop
                del buffer[:take_n]
                break

            if max_cols_seen > MAX_COLS_HARD:
                raise ValueError(
                    f"Sheet '{sheet_name}' has too many columns ({max_cols_seen} > {MAX_COLS_HARD}). "
                    "Please split/export the workbook."
                )

            window_span = min(window_span, max_cols_seen)
            if window_span < 1:
                window_span = 1

            planned: List[Tuple[str, str, str]] = []  # (chunk_text, locator, seed)
            any_nonempty = False
            oversized = False

            for col_start in range(0, max_cols_seen, window_span):
                col_end = min(max_cols_seen, col_start + window_span)
                lines = _render_kv_lines_with_rownums(headers, items, col_start, col_end)
                if not lines:
                    continue
                any_nonempty = True

                start = lines[0][0]
                end = lines[-1][0]
                cols_range = _col_range_str(col_start, col_end)
                locator = f"Sheet={sheet_name}; rows={start}-{end}; cols={cols_range}; header_row={header_row_1based}"
                header_window = _window_headers_with_letters(headers, col_start, col_end)
                chunk_text = (
                    f"Workbook: {original_filename}\n"
                    f"Sheet: {sheet_name}\n"
                    f"Rows: {start}-{end}\n"
                    f"Columns: {header_window}\n"
                    f"{locator}\n\n"
                    + "\n".join([ln for _, ln in lines])
                )

                if len(chunk_text) > MAX_CHARS_PER_CHUNK:
                    oversized = True
                    break

                # Stable seed: do NOT include global_order
                seed = f"rows:{sheet_name}:header{header_row_1based}:rows{start}-{end}:cols{cols_range}"
                planned.append((chunk_text, locator, seed))

            if not any_nonempty:
                # All rows empty => drop them
                del buffer[:take_n]
                break

            if oversized:
                # Retry without emitting anything.
                if take_n > 1:
                    take_n = max(1, take_n // 2)
                    continue
                if window_span > 1:
                    window_span = max(1, window_span // 2)
                    continue
                raise ValueError(
                    f"Single-row chunk is too large to ingest without truncation "
                    f"(sheet='{sheet_name}', row={items[0][0]}, cols=single). "
                    "Please split/export the workbook or reduce very large cell contents."
                )

            # EMIT phase (safe): all planned chunks fit
            for chunk_text, locator, seed in planned:
                add_doc_cb(
                    text=chunk_text,
                    doc_type="spreadsheet_rows",
                    source_locator=locator,
                    order_val=global_order_ref[0],
                    seed=seed,
                )
                global_order_ref[0] += 1

            del buffer[:take_n]
            break

# ---------------------------------------------------------------------
# END DEAD CODE BLOCK (legacy helpers)
# ---------------------------------------------------------------------

# ---------------------------------------------------------------------
# Spreadsheet ingestion (main entrypoint)
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class SpreadsheetIngestResult:
    processed_data: Dict[str, object]
    file_size_bytes: int


async def ingest_spreadsheet_from_url(
    ctx: Context,
    file_url: str,
    original_filename: str,
    url_for_metadata: str,
    tenant_id: str,
) -> SpreadsheetIngestResult:
    """
    Returns processed_data compatible with WeaviateLoader:
      {"documents": List[Document], "chunk_uuids": List[str], "images_for_vectorization": []}

    Rejects on size/cap violations (no sampling).
    """
    suffix = Path(original_filename).suffix.lower()
    if suffix not in {".csv", ".tsv", ".xlsx", ".xlsm", ".xlsb", ".xls"}:
        raise ValueError(f"Not a supported spreadsheet suffix: {suffix}")

    async with SPREADSHEET_PARSE_SEMAPHORE:
        rss0 = _rss_bytes_best_effort()
        mem_lim = get_effective_memory_limit_bytes()
        cpu_cores = get_effective_cpu_cores()

        effective_max_total_text_chars, effective_max_total_chunks = compute_effective_spreadsheet_caps(mem_lim)
        effective_max_total_chunks_before_summary = max(1, effective_max_total_chunks - 1)

        logger.bind(
            ctx=ctx,
            rss_bytes=rss0,
            rss=_format_bytes(rss0),
            filename=original_filename,
            mem_limit_bytes=mem_lim,
            mem_limit=_format_bytes(mem_lim),
            cpu_cores=None if cpu_cores is None else round(cpu_cores, 2),
            tenant_id=tenant_id,
            max_total_text_chars=effective_max_total_text_chars,
            max_total_chunks=effective_max_total_chunks,
            max_rows_per_sheet=MAX_ROWS_PER_SHEET,
            max_cols_hard=MAX_COLS_HARD,
            # enforced in cfg below (render.py uses cfg.max_chars_per_chunk)
            max_chars_per_chunk_enforced=min(MAX_CHARS_PER_CHUNK, SPREADSHEET_MAX_CHARS_PER_CHUNK_FOR_VECTORIZATION),
            max_chars_per_chunk_raw=MAX_CHARS_PER_CHUNK,
            max_tokens_per_chunk_enforced=SPREADSHEET_MAX_TOKENS_PER_CHUNK_FOR_VECTORIZATION,
        ).info("Spreadsheet ingest starting (bulkheaded).")

        # 1) download to tempfile (hard size gate)
        tmp_path, written = await download_to_tempfile(
            ctx=ctx,
            url=file_url,
            suffix=suffix,
            max_bytes=MAX_SPREADSHEET_BYTES_HARD,
        )

        try:
            # 2) zip preflight for xlsx/xlsm
            if suffix in (".xlsx", ".xlsm"):
                zip_stats = xlsx_zip_preflight_or_reject(tmp_path, compressed_bytes=written)
                # Log stats to correlate memory spikes with ZIP structure and sharedStrings size.
                logger.bind(
                    ctx=ctx,
                    filename=original_filename,
                    tenant_id=tenant_id,
                    **zip_stats,
                ).info(
                    "XLSX/XLSM ZIP preflight stats"
                )
            else:
                zip_stats = None

            # 3) build workbook-level document_id (stable)
            document_id = hashlib.sha256(str(url_for_metadata).encode("utf-8")).hexdigest()
            document_namespace = uuid.uuid5(uuid.NAMESPACE_DNS, document_id)

            # 4) parse + chunk
            docs: List[Document] = []
            chunk_ids: List[str] = []

            # workbook summary placeholder (filled as we discover sheets)
            sheet_names: List[str] = []
            warnings: List[str] = []

            # order is global across workbook so that `order` sorting works even without schema expansion
            global_order = [0]  # mutable ref for flush helper
            total_chars = 0
            # captured from download_to_tempfile()
            file_size_bytes = written
            filename_for_msg = original_filename
            telem = SpreadsheetTelemetry(
                filename=original_filename,
                suffix=suffix,
                file_size_bytes=int(file_size_bytes),
                mem_limit_bytes=mem_lim,
                cpu_cores=cpu_cores,
                rss_start=rss0,
            )

            def _new_chunk_uuid(seed: str) -> str:
                # Proper deterministic UUIDs
                return str(uuid.uuid5(document_namespace, seed))

            def _add_doc(
                text: str,
                *,
                doc_type: str,
                source_locator: str,
                order_val: int,
                seed: str,
            ):
                nonlocal docs, chunk_ids, total_chars

                # Reserve one slot for the workbook summary that is inserted at the end.
                if len(docs) >= effective_max_total_chunks_before_summary:
                    # Make sure telemetry fields used in the error are fresh (rss_now/est_final_rss).
                    telem.extracted_chars_so_far = total_chars
                    telem.docs_so_far = len(docs)
                    telem.maybe_sample_rss()
                    telem.maybe_log_progress(ctx)
                    raise ValueError(
                        "Spreadsheet produced too many chunks to ingest safely. "
                        f"(chunks_so_far={len(docs)}, max_chunks={effective_max_total_chunks}, "
                        f"file_size_bytes={file_size_bytes}, filename='{filename_for_msg}', "
                        f"rss_now={_format_bytes(telem.rss_now)}, mem_limit={_format_bytes(telem.mem_limit_bytes)}, "
                        f"est_final_rss={_format_bytes(telem.est_final_rss)}). "
                        "Please split the workbook (by sheet/table) or export to smaller CSVs."
                    )
                # Early reject on total extracted text to avoid RAM blowups.
                if total_chars + len(text) > effective_max_total_text_chars:
                    telem.extracted_chars_so_far = total_chars
                    telem.docs_so_far = len(docs)
                    telem.maybe_sample_rss()
                    headroom = None
                    if telem.mem_limit_bytes and telem.rss_now:
                        headroom = max(0, int(telem.mem_limit_bytes - telem.rss_now))
                    raise ValueError(
                        "Spreadsheet produced too much extracted text to ingest safely. "
                        f"(extracted_chars_so_far={total_chars}, attempted_add_chars={len(text)}, "
                        f"max_extracted_chars={effective_max_total_text_chars}, "
                        f"chunks_so_far={len(docs)}, max_chunks={effective_max_total_chunks}, "
                        f"file_size_bytes={file_size_bytes}, filename='{filename_for_msg}', "
                        f"bytes_read_so_far={telem.bytes_read_so_far or 'n/a'}, "
                        f"chars_per_byte={None if telem.chars_per_byte is None else round(telem.chars_per_byte, 4)}, "
                        f"est_total_chars={telem.est_total_chars or 'n/a'}, "
                        f"rss_start={_format_bytes(telem.rss_start)}, rss_now={_format_bytes(telem.rss_now)}, rss_peak={_format_bytes(telem.rss_peak)}, "
                        f"rss_per_char={None if telem.rss_per_char is None else round(telem.rss_per_char, 2)}, "
                        f"est_final_rss={_format_bytes(telem.est_final_rss)}, "
                        f"mem_limit={_format_bytes(telem.mem_limit_bytes)}, mem_headroom_now={_format_bytes(headroom)}, "
                        f"cpu_cores={None if telem.cpu_cores is None else round(telem.cpu_cores, 2)}). "
                        "Please split the workbook (by sheet/table), reduce row/column count, "
                        "or export smaller CSVs."
                    )
                # metadata keys must exist in current Weaviate schema (do NOT add new keys)
                meta = {
                    "source": original_filename,
                    "document_id": document_id,
                    "s3_url": str(url_for_metadata),
                    "order": order_val,
                    "doc_type": doc_type,
                    "source_locator": source_locator,
                    "page_number": None,
                }
                docs.append(Document(page_content=text, metadata=meta))
                chunk_ids.append(_new_chunk_uuid(seed))
                total_chars += len(text)
                telem.extracted_chars_so_far = total_chars
                telem.docs_so_far = len(docs)
                # High-frequency telemetry updates (rate-limited logging) AFTER append
                # so logs reflect the current state (not off-by-one).
                telem.maybe_sample_rss()
                telem.maybe_log_progress(ctx)

            cfg = SheetIngestConfig(
                header_scan_rows=HEADER_SCAN_ROWS,
                blank_row_break=3,
                blank_row_empty_ratio=0.95,
                max_rows_per_sheet=MAX_ROWS_PER_SHEET,
                max_rows_per_chunk=MAX_ROWS_PER_CHUNK,
                max_cols_included=MAX_COLS_INCLUDED,
                max_cols_hard=MAX_COLS_HARD,
                max_chars_per_chunk=min(MAX_CHARS_PER_CHUNK, SPREADSHEET_MAX_CHARS_PER_CHUNK_FOR_VECTORIZATION),
                max_tokens_per_chunk=SPREADSHEET_MAX_TOKENS_PER_CHUNK_FOR_VECTORIZATION,
                render_mode="compact_kv",
                forward_fill_cols=3,
                enable_side_notes=True,
            )

            def _parse_sync() -> None:
                """
                All parsing/chunking is CPU-heavy (openpyxl/xlrd parsing + chunk rendering).
                Run it in a thread to avoid blocking the event loop.
                """
                # Cross-process lock: only one spreadsheet parse/chunk job at a time per container.
                with _spreadsheet_global_lock(ctx=ctx, suffix=suffix, filename=original_filename):

                    # --- CSV/TSV path ---
                    if suffix in (".csv", ".tsv"):
                        delimiter = "," if suffix == ".csv" else "\t"
                        sheet_name = "CSV"
                        sheet_names.append(sheet_name)

                        # Open as binary + TextIOWrapper so we can reliably track bytes_read via fb.tell().
                        with open(tmp_path, "rb") as fb:
                            with io.TextIOWrapper(fb, encoding="utf-8-sig", errors="replace", newline="") as f:
                                try:
                                    csv.field_size_limit(50 * 1024 * 1024)
                                except Exception:
                                    pass
                                reader = csv.reader(f, delimiter=delimiter)
                                row_idx = 0

                                def _iter_csv_rows():
                                    nonlocal row_idx
                                    for r in reader:
                                        row_idx += 1
                                        if len(r) > MAX_COLS_HARD:
                                            raise ValueError(
                                                f"CSV/TSV has too many columns ({len(r)} > {MAX_COLS_HARD}). Please split/export."
                                            )
                                        if (row_idx % max(1, CSV_TELEMETRY_UPDATE_EVERY_N_ROWS)) == 0:
                                            try:
                                                telem.update_bytes_read(int(fb.tell()))
                                                telem.extracted_chars_so_far = total_chars
                                                telem.docs_so_far = len(docs)
                                                telem.maybe_sample_rss()
                                                telem.maybe_log_progress(ctx)
                                            except Exception:
                                                pass
                                        yield row_idx, [c for c in r]

                                ingest_sheet_from_rows_iter(
                                    rows_iter=_iter_csv_rows(),
                                    workbook_name=original_filename,
                                    sheet_name=sheet_name,
                                    add_doc_cb=_add_doc,
                                    global_order_ref=global_order,
                                    cfg=cfg,
                                    warnings=warnings,
                                )
                        return

                    # --- XLSX/XLSM path ---
                    if suffix in (".xlsx", ".xlsm"):
                        if openpyxl is None:
                            raise ValueError("openpyxl is required for .xlsx/.xlsm ingestion but is not installed.")

                        wb = None
                        try:
                            wb = openpyxl.load_workbook(
                                filename=str(tmp_path),
                                read_only=True,
                                data_only=True,
                                keep_links=False,
                            )
                            if len(wb.worksheets) > MAX_SHEETS:
                                raise ValueError(
                                    f"Workbook has too many sheets ({len(wb.worksheets)}). Max supported is {MAX_SHEETS}. "
                                    "Please split the workbook."
                                )
                            sheet_list = wb.worksheets[:MAX_SHEETS]
                            sheets_total = len(sheet_list)
                            for sidx, ws in enumerate(sheet_list):
                                sheet_name = ws.title or f"Sheet{sidx+1}"
                                sheet_names.append(sheet_name)

                                rss_sheet = _rss_bytes_best_effort()
                                logger.bind(ctx=ctx, sheet=sheet_name, rss=rss_sheet).debug("Spreadsheet sheet start.")

                                declared_max_col = int(getattr(ws, "max_column", 0) or 0)
                                effective_max_col = max(1, min(declared_max_col or MAX_COLS_HARD, MAX_COLS_HARD))
                                declared_max_row = int(getattr(ws, "max_row", 0) or 0)
                                # declared_max_row is best-effort only; can be inflated by formatting

                                merge_map = build_merge_fill_map(
                                    ws,
                                    max_row_1based=HEADER_SCAN_ROWS + 5,
                                    max_col_1based=min(effective_max_col, MAX_COLS_HARD),
                                    warnings=warnings,
                                    sheet_name=sheet_name,
                                )

                                if declared_max_col > MAX_COLS_HARD:
                                    warnings.append(
                                        f"Sheet '{sheet_name}' declares max_column={declared_max_col} (> {MAX_COLS_HARD}). "
                                        f"Only the first {MAX_COLS_HARD} columns are read; results may be partial."
                                    )
                                    try:
                                        obs_rightmost = 0
                                        preview_iter = ws.iter_rows(values_only=True, max_col=MAX_COLS_HARD)
                                        for rn in range(1, HEADER_SCAN_ROWS + 1):
                                            r = next(preview_iter, None)
                                            if r is None:
                                                break
                                            rr = list(r) if r else []
                                            rr = apply_merge_fill_to_row(rn, rr, merge_map, max_col_1based=MAX_COLS_HARD)
                                            rm = rightmost_nonempty_index_1based(rr, MAX_COLS_HARD)
                                            if rm > obs_rightmost:
                                                obs_rightmost = rm
                                        if obs_rightmost >= MAX_COLS_HARD:
                                            raise ValueError(
                                                f"Sheet '{sheet_name}' appears to have more than {MAX_COLS_HARD} used columns. "
                                                "Please split/export to a narrower sheet."
                                            )
                                    except ValueError:
                                        raise
                                    except Exception:
                                        warnings.append(
                                            f"Sheet '{sheet_name}' declares max_column={declared_max_col} (> {MAX_COLS_HARD}); "
                                            "could not fully validate width. Continuing best-effort."
                                        )

                                row_iter = ws.iter_rows(values_only=True, max_col=effective_max_col)

                                def _iter_xlsx_rows():
                                    for rn, r in enumerate(row_iter, start=1):
                                        # Best-effort XLSX progress updates:
                                        # - uses ws.max_row (may be inflated by formatting)
                                        # - still useful for "how far are we" and est_total_chars/est_final_rss
                                        if (rn % max(1, XLSX_TELEMETRY_UPDATE_EVERY_N_ROWS)) == 0:
                                            try:
                                                telem.extracted_chars_so_far = total_chars
                                                telem.docs_so_far = len(docs)
                                                telem.update_xlsx_progress(
                                                    sheet_name=sheet_name,
                                                    sheet_index_0=sidx,
                                                    sheets_total=sheets_total,
                                                    current_row_1based=rn,
                                                    declared_max_row=declared_max_row,
                                                    max_rows_cap=MAX_ROWS_PER_SHEET,
                                                )
                                                telem.maybe_sample_rss()
                                                telem.maybe_log_progress(ctx)
                                            except Exception:
                                                pass
                                        yield rn, (list(r) if r else [])

                                ingest_sheet_from_rows_iter(
                                    rows_iter=_iter_xlsx_rows(),
                                    workbook_name=original_filename,
                                    sheet_name=sheet_name,
                                    add_doc_cb=_add_doc,
                                    global_order_ref=global_order,
                                    cfg=cfg,
                                    warnings=warnings,
                                    merge_fill_map=merge_map,
                                    merge_fill_max_row_1based=HEADER_SCAN_ROWS + 5,
                                )

                                try:
                                    ws = None  # type: ignore
                                    gc.collect()
                                except Exception:
                                    pass
                        finally:
                            try:
                                if wb is not None:
                                    wb.close()
                            except Exception:
                                pass
                        return

                    # --- XLSB path ---
                    if suffix == ".xlsb":
                        if open_xlsb_workbook is None:
                            raise ValueError(
                                "pyxlsb is required for .xlsb ingestion but is not installed. "
                                "Please export to .xlsx or .csv."
                            )
                        with open_xlsb_workbook(str(tmp_path)) as wb:
                            sheets = list(wb.sheets)
                            if len(sheets) > MAX_SHEETS:
                                raise ValueError(
                                    f"Workbook has too many sheets ({len(sheets)}). Max supported is {MAX_SHEETS}. "
                                    "Please split the workbook."
                                )
                            for sname in sheets[:MAX_SHEETS]:
                                sheet_names.append(sname)
                                with wb.get_sheet(sname) as sh:
                                    rows = sh.rows()

                                    def _iter_xlsb_rows():
                                        rn = 0
                                        for r in rows:
                                            rn += 1
                                            # Best-effort telemetry sampling for XLSB too.
                                            # We don't have a reliable declared_max_row, so we don't call update_xlsx_progress,
                                            # but RSS sampling still helps explain spikes.
                                            if (rn % max(1, XLSX_TELEMETRY_UPDATE_EVERY_N_ROWS)) == 0:
                                                try:
                                                    telem.extracted_chars_so_far = total_chars
                                                    telem.docs_so_far = len(docs)
                                                    telem.sheet_name = sname
                                                    telem.progress_kind = "xlsx_rows"  # closest bucket for spreadsheets
                                                    telem.progress_confidence = "none"
                                                    telem.current_row_1based = rn
                                                    telem.declared_max_row = None
                                                    telem.maybe_sample_rss()
                                                    telem.maybe_log_progress(ctx)
                                                except Exception:
                                                    pass
                                            row_list = [c.v for c in r]
                                            if len(row_list) > MAX_COLS_HARD:
                                                raise ValueError(
                                                    f"Sheet '{sname}' has too many columns ({len(row_list)} > {MAX_COLS_HARD}). "
                                                    "Please split/export."
                                                )
                                            yield rn, row_list

                                    ingest_sheet_from_rows_iter(
                                        rows_iter=_iter_xlsb_rows(),
                                        workbook_name=original_filename,
                                        sheet_name=sname,
                                        add_doc_cb=_add_doc,
                                        global_order_ref=global_order,
                                        cfg=cfg,
                                        warnings=warnings,
                                    )
                        return

                    # --- XLS (legacy) path ---
                    if suffix == ".xls":
                        if xlrd is None:
                            raise ValueError("XLS ingestion requires xlrd (legacy). Please export to .xlsx or .csv.")
                        if written > 20 * 1024 * 1024:
                            raise ValueError("XLS file is too large to ingest safely. Please export to .xlsx or .csv.")
                        book = xlrd.open_workbook(str(tmp_path), on_demand=True)
                        try:
                            sheets_total = min(book.nsheets, MAX_SHEETS)
                            for sidx in range(min(book.nsheets, MAX_SHEETS)):
                                sh = book.sheet_by_index(sidx)
                                sname = sh.name or f"Sheet{sidx+1}"
                                sheet_names.append(sname)
                                declared_max_row = int(getattr(sh, "nrows", 0) or 0)

                                def _iter_xls_rows():
                                    for ridx in range(sh.nrows):
                                        rn = ridx + 1
                                        if (rn % max(1, XLSX_TELEMETRY_UPDATE_EVERY_N_ROWS)) == 0:
                                            try:
                                                telem.extracted_chars_so_far = total_chars
                                                telem.docs_so_far = len(docs)
                                                telem.update_xlsx_progress(
                                                    sheet_name=sname,
                                                    sheet_index_0=sidx,
                                                    sheets_total=sheets_total,
                                                    current_row_1based=rn,
                                                    declared_max_row=declared_max_row,
                                                    max_rows_cap=MAX_ROWS_PER_SHEET,
                                                )
                                                telem.maybe_sample_rss()
                                                telem.maybe_log_progress(ctx)
                                            except Exception:
                                                pass
                                        row_vals = sh.row_values(ridx)
                                        if len(row_vals) > MAX_COLS_HARD:
                                            raise ValueError(
                                                f"Sheet '{sname}' has too many columns ({len(row_vals)} > {MAX_COLS_HARD}). "
                                                "Please split/export."
                                            )
                                        yield ridx + 1, row_vals

                                ingest_sheet_from_rows_iter(
                                    rows_iter=_iter_xls_rows(),
                                    workbook_name=original_filename,
                                    sheet_name=sname,
                                    add_doc_cb=_add_doc,
                                    global_order_ref=global_order,
                                    cfg=cfg,
                                    warnings=warnings,
                                )
                        finally:
                            try:
                                book.release_resources()
                            except Exception:
                                pass
                        return

                    raise ValueError(f"Unsupported spreadsheet suffix: {suffix}")

            await asyncio.to_thread(_parse_sync)

            # Finally: workbook summary doc (at very top of ordering for discoverability)
            # Insert workbook summary as order=0 for discoverability.
            # Shift all existing docs by +1 to preserve ordering and keep `order` monotonic.
            for d in docs:
                if d.metadata and "order" in d.metadata:
                    d.metadata["order"] = int(d.metadata["order"]) + 1

            workbook_summary_text = (
                f"Workbook: {original_filename}\n"
                f"Source: {url_for_metadata}\n"
                f"Sheets ({len(sheet_names)}): " + ", ".join(sheet_names[:200])
                + ("\n\nWarnings:\n- " + "\n- ".join(warnings) if warnings else "")
            )

            if total_chars + len(workbook_summary_text) > effective_max_total_text_chars:
                raise ValueError(
                    "Spreadsheet produced too much extracted text to ingest safely (including summary). "
                    "Please split the workbook or export smaller files."
                )

            summary_doc = Document(
                page_content=workbook_summary_text,
                metadata={
                    "source": original_filename,
                    "document_id": document_id,
                    "s3_url": str(url_for_metadata),
                    "order": 0,
                    "doc_type": "spreadsheet_workbook_summary",
                    # Provide Sheet=... so deterministic Neo4j graph doesn't create "unknown"
                    "source_locator": build_source_locator(sheet_name="__WORKBOOK__", kind="workbook_summary", table=0),
                    "page_number": None,
                },
            )
            docs.insert(0, summary_doc)
            chunk_ids.insert(0, _new_chunk_uuid("workbook_summary"))
            total_chars += len(workbook_summary_text)

            if len(docs) > effective_max_total_chunks:
                raise ValueError(
                    f"Spreadsheet produced too many chunks (>{effective_max_total_chunks}). "
                    "Please split the workbook or export to smaller files."
                )

            if len(docs) != len(chunk_ids):
                raise RuntimeError("Internal spreadsheet ingestion error: docs and chunk_ids mismatch.")

            processed = {
                "documents": docs,
                "chunk_uuids": chunk_ids,
                "images_for_vectorization": [],
                # extra fields are fine; loaders ignore them
                "spreadsheet_file_size_bytes": written,
                "spreadsheet_total_chars": total_chars,
            }

            rss1 = _rss_bytes_best_effort()
            logger.bind(
                ctx=ctx,
                rss_bytes=rss1,
                rss=_format_bytes(rss1),
                rss_peak_bytes=telem.rss_peak,
                rss_peak=_format_bytes(telem.rss_peak),
                chunks=len(docs),
                total_chars=total_chars,
                file_size_bytes=file_size_bytes,
                est_total_chars=telem.est_total_chars,
                est_final_rss=_format_bytes(telem.est_final_rss),
                # Include zip stats if present
                zip_members=None if zip_stats is None else zip_stats.get("zip_members"),
                zip_uncompressed_total=None if zip_stats is None else zip_stats.get("zip_uncompressed_total"),
                zip_shared_strings_bytes=None if zip_stats is None else zip_stats.get("zip_shared_strings_bytes"),
                zip_expansion_ratio=None if zip_stats is None else zip_stats.get("zip_expansion_ratio"),
                zip_uncompressed_cap=None if zip_stats is None else zip_stats.get("zip_uncompressed_cap"),
                zip_shared_strings_cap=None if zip_stats is None else zip_stats.get("zip_shared_strings_cap"),
                zip_max_member=None if zip_stats is None else zip_stats.get("zip_max_member"),
                zip_compressed_bytes=None if zip_stats is None else zip_stats.get("zip_compressed_bytes"),
                filename=original_filename,
                tenant_id=tenant_id,
            ).info("Spreadsheet ingest completed.")
            return SpreadsheetIngestResult(processed_data=processed, file_size_bytes=written)
        finally:
            tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------
# Neo4j strategy helpers for spreadsheets
# ---------------------------------------------------------------------

def choose_spreadsheet_neo4j_strategy(
    file_size_bytes: int, chunk_count: int, total_chars: Optional[int] = None
) -> Literal["llm", "deterministic"]:
    """
    For SPREADSHEET_NEO4J_MODE:
      - llm: always LLM (but caller should enforce hard eligibility checks)
      - deterministic: always deterministic
      - hybrid: LLM for small, deterministic for large
    """
    if SPREADSHEET_NEO4J_MODE == "deterministic":
        return "deterministic"
    if SPREADSHEET_NEO4J_MODE == "llm":
        return "llm"
    # hybrid
    if (
        file_size_bytes <= SPREADSHEET_LLM_MAX_BYTES
        and chunk_count <= SPREADSHEET_LLM_MAX_CHUNKS
        and (total_chars is None or total_chars <= SPREADSHEET_LLM_MAX_TOTAL_CHARS)
    ):
        return "llm"
    return "deterministic"


# Keep Neo4j payload/storage small for deterministic spreadsheet graphs.
SPREADSHEET_NEO4J_TEXT_PREVIEW_CHARS = 200

def build_deterministic_spreadsheet_graph(
    *,
    processed_data: Dict[str, object],
    document_id: str,
    sheet_namespace_id: Optional[str] = None,
) -> ExtractedGraph:
    """
    Builds a deterministic graph with:
      - Chunk nodes for each chunk (local ids chunk_{order})
      - Sheet concept nodes (id = sheet:<name>)
      - NEXT relationships within same sheet (based on source_locator parsing)
      - IN_SHEET relationships from chunk -> sheet

    This avoids LLM extraction while still enabling graph traversal for adjacency/context.
    """
    docs: List[Document] = processed_data.get("documents")  # type: ignore
    if not docs:
        return ExtractedGraph()

    # NOTE FOR MAINTAINERS (IMPORTANT CONTRACT):
    # Neo4jLoader.load() maps ChunkNodes back to Weaviate chunk UUIDs (and chunk s3_urls) by using:
    #   uuid_map[node.id] = chunk_uuids[node.order]
    # and also enriches:
    #   node.properties["s3_url"] = chunk_s3_urls[node.order]
    #
    # Therefore, for any *subgraph* load (e.g., spreadsheet loaded per (sheet, table)), every ChunkNode.order
    # MUST be a local contiguous index in 0..N-1 where N == len(chunk_uuids) for that load call.
    # Do NOT use workbook-global metadata["order"] for ChunkNode.order here, or Neo4jLoader will skip mappings.
    maybe_uuids = processed_data.get("chunk_uuids")  # type: ignore
    if maybe_uuids is not None and isinstance(maybe_uuids, list) and len(maybe_uuids) != len(docs):
        raise ValueError("Deterministic spreadsheet graph: documents and chunk_uuids length mismatch.")

    def sheet_table_from_locator(loc: str) -> Tuple[str, int]:
        info = parse_source_locator(loc or "")
        return (info.sheet or "unknown")[:200], int(info.table) if info.table is not None else 1

    nodes: List[object] = []
    rels: List[Relationship] = []

    # Create sheet nodes on demand
    sheet_node_id_by_name: Dict[str, str] = {}

    chunk_local_ids_by_group: Dict[Tuple[str, int], List[str]] = {}

    for i, d in enumerate(docs):
        md = d.metadata or {}
        loc = str(md.get("source_locator") or "")
        sheet, table = sheet_table_from_locator(loc)
        sheet_id = sheet_node_id_by_name.get(sheet)
        if not sheet_id:
            ns = sheet_namespace_id or document_id
            sheet_id = f"sheet:{ns}:{sheet}"
            sheet_node_id_by_name[sheet] = sheet_id
            nodes.append(EntityNode(id=sheet_id, label="Concept", name=f"Sheet: {sheet}"))
            # Link Document -> Sheet (Document node is created by Neo4jClient.add_graph)
            rels.append(Relationship(source_id=document_id, target_id=sheet_id, type="HAS_SHEET"))

        # IMPORTANT:
        # Use local contiguous ordering (i) for this graph. See Neo4jLoader contract note above.
        order_val = i
        local_id = f"chunk_{i}"
        # Only chain NEXT among row chunks (summaries should not be in the adjacency chain).
        if str(md.get("doc_type") or "") == "spreadsheet_rows":
            chunk_local_ids_by_group.setdefault((sheet, table), []).append(local_id)

        # Chunk node local id must be stable and ordered
        nodes.append(
            ChunkNode(
                id=local_id,
                # Neo4j stores only a small preview; Weaviate remains the source of truth
                # for full chunk text.
                text=(d.page_content[:SPREADSHEET_NEO4J_TEXT_PREVIEW_CHARS] if d.page_content else ""),
                chunk_type="SpreadsheetChunk",
                speaker=None,
                order=order_val,
                properties={
                    "source_locator": loc,
                    "sheet": sheet,
                    "table": str(table),
                    "doc_type": str(md.get("doc_type") or ""),
                    "original_order": str((md or {}).get("order", "")),
                },
            )
        )
        rels.append(Relationship(source_id=local_id, target_id=sheet_id, type="IN_SHEET"))

    # NEXT edges within each (sheet, table) group
    for (_sheet, _table), local_ids in chunk_local_ids_by_group.items():
        for a, b in zip(local_ids, local_ids[1:]):
            rels.append(Relationship(source_id=a, target_id=b, type="NEXT"))

    return ExtractedGraph(nodes=nodes, relationships=rels)
