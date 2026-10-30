from __future__ import annotations

from typing import Callable, List, Literal, Optional, Sequence, Tuple

import tiktoken

from src.autobots.ingestion.spreadsheet.locator import build_source_locator, encode_sheet_name
from src.autobots.ingestion.spreadsheet.utils import (
    col_range_str,
    excel_col_name,
    is_empty_cell,
    norm_text,
    sanitize_text,
    window_headers_with_letters,
)

RenderMode = Literal["kv", "compact_kv"]

_ENC = None

def _count_tokens(text: str) -> int:
    """
    Token counting for OpenAI-style models. We use cl100k_base as a practical default.
    """
    global _ENC
    if _ENC is None:
        _ENC = tiktoken.get_encoding("cl100k_base")
    return len(_ENC.encode(text))


def _render_kv_lines_with_rownums(
    headers: Sequence[str],
    items: List[Tuple[int, List[object]]],
    col_start_0: int,
    col_end_excl_0: int,
) -> List[Tuple[int, str]]:
    out: List[Tuple[int, str]] = []
    for row_num, row in items:
        parts: List[str] = []
        for j in range(col_start_0, col_end_excl_0):
            h = headers[j] if j < len(headers) and headers[j] else excel_col_name(j)
            val = row[j] if j < len(row) else None
            if is_empty_cell(val):
                continue
            s = sanitize_text(norm_text(val))
            if not s:
                continue
            parts.append(f"{h}={s}")
        if parts:
            out.append((row_num, f"- Row {row_num}: " + "; ".join(parts)))
    return out


def _render_compact_kv_lines_with_rownums(
    items: List[Tuple[int, List[object]]],
    col_start_0: int,
    col_end_excl_0: int,
) -> List[Tuple[int, str]]:
    """
    Compact, full-fidelity rendering:
      - row entries use column letters (A=..., B=...) to avoid repeating long headers
      - the chunk header still includes the mapping (A=Header | B=Header | ...)
    """
    out: List[Tuple[int, str]] = []
    for row_num, row in items:
        parts: List[str] = []
        for j in range(col_start_0, col_end_excl_0):
            val = row[j] if j < len(row) else None
            if is_empty_cell(val):
                continue
            s = sanitize_text(norm_text(val))
            if not s:
                continue
            parts.append(f"{excel_col_name(j)}={s}")
        if parts:
            out.append((row_num, f"- Row {row_num}: " + "; ".join(parts)))
    return out


def emit_row_chunks_no_loss(
    *,
    headers: List[str],
    buffer: List[Tuple[int, List[object]]],
    workbook_name: str,
    sheet_name: str,
    table_id: int,
    header_rows_1based: Optional[Tuple[int, int]],
    kind: str,
    doc_type: str,
    add_doc_cb: Callable[..., None],
    global_order_ref: List[int],
    max_rows_per_chunk: int,
    max_cols_included: int,
    max_cols_hard: int,
    max_chars_per_chunk: int,
    max_tokens_per_chunk: Optional[int] = None,
    render_mode: RenderMode = "compact_kv",
    col_span: Optional[Tuple[int, int]] = None,  # restrict to [start,end)
) -> None:
    """
    Emit row chunks with:
      - row splitting + vertical column windowing
      - strict no truncation: if 1-row / 1-col chunk cannot fit, raise
    Consumes buffer in-place.
    """
    sheet_enc = encode_sheet_name(sheet_name)
    while buffer:
        take_n = min(max_rows_per_chunk, len(buffer))
        window_span = max_cols_included

        while True:
            items = buffer[:take_n]

            # Determine max cols seen (bounded by hard cap) for this slice
            max_cols_seen = 0
            for _, row in items:
                if len(row) > max_cols_seen:
                    max_cols_seen = len(row)
            if max_cols_seen == 0:
                del buffer[:take_n]
                break

            if max_cols_seen > max_cols_hard:
                raise ValueError(
                    f"Sheet '{sheet_name}' has too many columns ({max_cols_seen} > {max_cols_hard}). "
                    "Please split/export the workbook."
                )

            # Restrict to requested span if provided
            span_start = 0
            span_end = max_cols_seen
            if col_span is not None:
                span_start = max(0, min(col_span[0], max_cols_seen))
                # If span_start lands at the end, clamp it back to the last valid column index.
                if span_start >= max_cols_seen:
                    span_start = max(0, max_cols_seen - 1)
                span_end = min(
                    max_cols_seen,
                    max(span_start + 1, min(col_span[1], max_cols_seen)),
                )

            span_width = max(1, span_end - span_start)
            window_span = min(window_span, span_width)
            if window_span < 1:
                window_span = 1

            planned: List[Tuple[str, str, str]] = []  # (chunk_text, locator, seed)
            any_nonempty = False
            oversized = False

            for col0 in range(span_start, span_end, window_span):
                col1 = min(span_end, col0 + window_span)
                if render_mode == "kv":
                    lines = _render_kv_lines_with_rownums(headers, items, col0, col1)
                else:
                    lines = _render_compact_kv_lines_with_rownums(items, col0, col1)
                if not lines:
                    continue
                any_nonempty = True

                start = lines[0][0]
                end = lines[-1][0]
                cols_range = col_range_str(col0, col1)
                locator = build_source_locator(
                    sheet_name=sheet_name,
                    kind=kind,
                    table=table_id,
                    rows=(start, end),
                    cols=cols_range,
                    header_rows=header_rows_1based,
                )
                header_window = window_headers_with_letters(headers, col0, col1)

                chunk_text = (
                    f"Workbook: {workbook_name}\n"
                    f"Sheet: {sheet_name}\n"
                    f"Table: {table_id}\n"
                    f"Kind: {kind}\n"
                    f"Rows: {start}-{end}\n"
                    f"Columns: {header_window}\n"
                    f"{locator}\n\n"
                    + "\n".join([ln for _, ln in lines])
                )

                too_big = len(chunk_text) > max_chars_per_chunk
                if (not too_big) and max_tokens_per_chunk:
                    if _count_tokens(chunk_text) > int(max_tokens_per_chunk):
                        too_big = True

                if too_big:
                    oversized = True
                    break

                seed_parts: List[str] = [
                    str(doc_type),
                    str(kind),
                    str(sheet_enc),
                    f"table{table_id}",
                ]
                if header_rows_1based is not None:
                    seed_parts.append(f"hdr{header_rows_1based[0]}-{header_rows_1based[1]}")
                seed_parts.append(f"rows{start}-{end}")
                seed_parts.append(f"cols{cols_range}")
                seed = ":".join(seed_parts)
                planned.append((chunk_text, locator, seed))

            if not any_nonempty:
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
                    f"(sheet='{sheet_name}', row={items[0][0]}). "
                    "Please split/export the workbook or reduce very large cell contents."
                )

            # Emit all planned
            for chunk_text, locator, seed in planned:
                add_doc_cb(
                    text=chunk_text,
                    doc_type=doc_type,
                    source_locator=locator,
                    order_val=global_order_ref[0],
                    seed=seed,
                )
                global_order_ref[0] += 1

            del buffer[:take_n]
            break


def emit_notes_chunks(
    *,
    note_rows: List[Tuple[int, List[object]]],
    workbook_name: str,
    sheet_name: str,
    table_id: int,
    kind: str,
    add_doc_cb: Callable[..., None],
    global_order_ref: List[int],
    max_rows_per_chunk: int,
    max_cols_included: int,
    max_cols_hard: int,
    max_chars_per_chunk: int,
    max_tokens_per_chunk: Optional[int] = None,
    render_mode: RenderMode = "compact_kv",
) -> None:
    """
    Emit notes as kv-ish chunks (no truncation) using column windowing too.
    Uses synthetic headers A.. for labeling.
    """
    if not note_rows:
        return
    max_cols = 0
    for _, r in note_rows:
        if len(r) > max_cols:
            max_cols = len(r)
    max_cols = min(max_cols, max_cols_hard)
    headers = [excel_col_name(i) for i in range(max_cols)]

    buf = list(note_rows)
    emit_row_chunks_no_loss(
        headers=headers,
        buffer=buf,
        workbook_name=workbook_name,
        sheet_name=sheet_name,
        table_id=table_id,
        header_rows_1based=None,
        kind=kind,
        doc_type="spreadsheet_notes",
        add_doc_cb=add_doc_cb,
        global_order_ref=global_order_ref,
        max_rows_per_chunk=max_rows_per_chunk,
        max_cols_included=max_cols_included,
        max_cols_hard=max_cols_hard,
        max_chars_per_chunk=max_chars_per_chunk,
        max_tokens_per_chunk=max_tokens_per_chunk,
        render_mode=render_mode,
    )
