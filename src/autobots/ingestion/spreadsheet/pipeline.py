from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Callable, Dict, Iterator, List, Literal, Optional, Tuple

from src.autobots.ingestion.spreadsheet.layout import (
    detect_header_region,
    flatten_header_rows,
    infer_table_span_from_header_rows,
    is_blank_row,
    row_matches_header,
)
from src.autobots.ingestion.spreadsheet.locator import build_source_locator, encode_sheet_name
from src.autobots.ingestion.spreadsheet.merges import apply_merge_fill_to_row
from src.autobots.ingestion.spreadsheet.render import emit_notes_chunks, emit_row_chunks_no_loss
from src.autobots.ingestion.spreadsheet.utils import (
    excel_col_name,
    is_empty_cell,
    rightmost_nonempty_index_1based,
)

RenderMode = Literal["kv", "compact_kv"]


@dataclass(frozen=True)
class SheetIngestConfig:
    header_scan_rows: int
    blank_row_break: int
    blank_row_empty_ratio: float
    max_rows_per_sheet: int
    max_rows_per_chunk: int
    max_cols_included: int
    max_cols_hard: int
    max_chars_per_chunk: int
    max_tokens_per_chunk: Optional[int] = None
    render_mode: RenderMode = "compact_kv"
    forward_fill_cols: int = 3  # best-effort for vertically-merged labels
    enable_side_notes: bool = True
    side_note_max_cols: int = 5
    side_note_min_chars: int = 30


def ingest_sheet_from_rows_iter(
    *,
    rows_iter: Iterator[Tuple[int, List[object]]],
    workbook_name: str,
    sheet_name: str,
    add_doc_cb: Callable[..., None],
    global_order_ref: List[int],
    cfg: SheetIngestConfig,
    warnings: List[str],
    merge_fill_map: Optional[Dict[Tuple[int, int], object]] = None,
    merge_fill_max_row_1based: int = 0,
) -> None:
    """
    Stream a sheet iterator and emit:
      - spreadsheet_sheet_summary (once)
      - spreadsheet_rows chunks for each table block
      - spreadsheet_notes for text blocks above/between/after tables
      - optional side notes outside the main table span
    """
    sheet_enc = encode_sheet_name(sheet_name)

    pending = deque()  # type: ignore[var-annotated]
    table_id = 1
    emitted_sheet_summary = False
    rows_seen_total = 0
    found_any_table = False
    eof_tail_after_break = False
    eof_tail_prev_table_id: Optional[int] = None

    def _take_rows(n: int) -> List[Tuple[int, List[object]]]:
        out: List[Tuple[int, List[object]]] = []
        nonlocal pending, rows_seen_total
        while pending and len(out) < n:
            out.append(pending.popleft())
        while len(out) < n:
            try:
                rn, rr = next(rows_iter)
                rows_seen_total += 1
                if rows_seen_total > cfg.max_rows_per_sheet:
                    raise ValueError(
                        f"Sheet '{sheet_name}' exceeds max rows ({cfg.max_rows_per_sheet}). Please split/export."
                    )
                out.append((rn, rr))
            except StopIteration:
                break
        return out

    def _emit_sheet_summary(headers: List[str], header_rows_1based: Tuple[int, int]) -> None:
        nonlocal emitted_sheet_summary
        if emitted_sheet_summary:
            return
        emitted_sheet_summary = True
        locator = build_source_locator(
            sheet_name=sheet_name,
            kind="sheet_summary",
            table=0,
            rows=None,
            cols=None,
            header_rows=header_rows_1based,
        )
        text = (
            f"Workbook: {workbook_name}\n"
            f"Sheet: {sheet_name}\n"
            f"Detected columns ({len(headers)}): " + " | ".join(headers[: cfg.max_cols_included])
        )
        add_doc_cb(
            text=text,
            doc_type="spreadsheet_sheet_summary",
            source_locator=locator,
            order_val=global_order_ref[0],
            seed=f"sheet_summary:{sheet_enc}",
        )
        global_order_ref[0] += 1

    def _apply_merge_fill(rn: int, rr: List[object]) -> List[object]:
        if merge_fill_map and rn <= merge_fill_max_row_1based:
            return apply_merge_fill_to_row(
                rn,
                rr,
                merge_fill_map,
                max_col_1based=cfg.max_cols_hard,
            )
        return rr

    def _rows_have_any_content(rows: List[Tuple[int, List[object]]]) -> bool:
        for _, rr in rows:
            for v in rr:
                if not is_empty_cell(v):
                    return True
        return False

    # Outer: detect tables until exhaustion
    while True:
        preview = _take_rows(cfg.header_scan_rows)
        if not preview:
            break

        # Drop leading fully blank rows from preview (they are not useful notes)
        while preview and not _rows_have_any_content([preview[0]]):
            preview.pop(0)

        if not preview:
            continue

        preview_nums = [rn for rn, _ in preview]
        preview_rows_raw = [rr for _, rr in preview]

        # Use a merge-filled copy ONLY for header detection (keeps notes rendering faithful).
        preview_rows_for_detect: List[List[object]] = []
        for rn, rr in preview:
            rr2 = list(rr)
            rr2 = _apply_merge_fill(rn, rr2)
            preview_rows_for_detect.append(rr2)

        header = detect_header_region(
            preview_rows=preview_rows_for_detect,
            preview_row_nums_1based=preview_nums,
            max_cols_hard=cfg.max_cols_hard,
            max_header_rows=3,
        )

        # If no header found: treat as notes and either continue (try further down) or stop.
        if header is None:
            # Notes-only (or no-table-detected so far). Emit a sheet summary once for observability.
            if not emitted_sheet_summary:
                max_cols = max((len(rr) for _, rr in preview), default=0)
                max_cols = min(max_cols, cfg.max_cols_hard)
                synth_headers = [excel_col_name(i) for i in range(max_cols)]
                _emit_sheet_summary(synth_headers, (preview_nums[0], preview_nums[0]))

            # If this preview is the EOF tail after a blank-run break, then it's bottom notes
            # for the PREVIOUS table (not "between tables").
            if eof_tail_after_break:
                kind = "notes_bottom"
                notes_table_id = eof_tail_prev_table_id or max(1, table_id - 1)
            else:
                kind = "notes_top" if not found_any_table else "notes_between"
                notes_table_id = table_id
            emit_notes_chunks(
                note_rows=preview,
                workbook_name=workbook_name,
                sheet_name=sheet_name,
                table_id=notes_table_id,
                kind=kind,
                add_doc_cb=add_doc_cb,
                global_order_ref=global_order_ref,
                max_rows_per_chunk=cfg.max_rows_per_chunk,
                max_cols_included=cfg.max_cols_included,
                max_cols_hard=cfg.max_cols_hard,
                max_chars_per_chunk=cfg.max_chars_per_chunk,
                max_tokens_per_chunk=cfg.max_tokens_per_chunk,
                render_mode=cfg.render_mode,
            )
            if eof_tail_after_break:
                # We reached EOF and the tail wasn't a table; stop scanning.
                break
            continue

        found_any_table = True
        eof_tail_after_break = False
        eof_tail_prev_table_id = None

        # Notes before header region
        pre_notes = preview[: header.header_start_idx]
        if pre_notes and _rows_have_any_content(pre_notes):
            emit_notes_chunks(
                note_rows=pre_notes,
                workbook_name=workbook_name,
                sheet_name=sheet_name,
                table_id=table_id,
                kind=("notes_top" if table_id == 1 else "notes_between"),
                add_doc_cb=add_doc_cb,
                global_order_ref=global_order_ref,
                max_rows_per_chunk=cfg.max_rows_per_chunk,
                max_cols_included=cfg.max_cols_included,
                max_cols_hard=cfg.max_cols_hard,
                max_chars_per_chunk=cfg.max_chars_per_chunk,
                max_tokens_per_chunk=cfg.max_tokens_per_chunk,
                render_mode=cfg.render_mode,
            )

        # Rebuild header with merge-fill ONLY on chosen header rows (avoid title-row mis-detection).
        hdr_start = header.header_start_idx
        hdr_count = header.header_row_count
        raw_header_rows = [preview_rows_raw[i] for i in range(hdr_start, hdr_start + hdr_count)]
        filled_header_rows: List[List[object]] = []
        for k in range(hdr_count):
            rn = preview_nums[hdr_start + k]
            rr = list(raw_header_rows[k])
            rr = _apply_merge_fill(rn, rr)
            filled_header_rows.append(rr)

        max_w = min(cfg.max_cols_hard, max((len(r) for r in filled_header_rows), default=0))
        headers_flat = flatten_header_rows(filled_header_rows, max_cols=max_w)
        col_start_0, col_end_excl_0 = infer_table_span_from_header_rows(filled_header_rows, max_cols=max_w)
        header = type(header)(
            header_start_idx=header.header_start_idx,
            header_row_count=header.header_row_count,
            header_rows_1based=header.header_rows_1based,
            headers_flat=headers_flat,
            col_start_0=col_start_0,
            col_end_excl_0=col_end_excl_0,
        )

        # Emit sheet summary when first table detected
        _emit_sheet_summary(header.headers_flat, header.header_rows_1based)

        # Prepare first batch of data rows (from preview tail)
        header_end_idx = header.header_start_idx + header.header_row_count
        data_seed = preview[header_end_idx:] if header_end_idx < len(preview) else []

        # Expand table span using the first N data rows (prevents misclassifying real columns as side notes).
        SPAN_SCAN = 50
        scan_rows = [rr for _, rr in data_seed[:SPAN_SCAN]]
        if scan_rows:
            beyond_count = 0
            max_right = header.col_end_excl_0
            for rr in scan_rows:
                rm = rightmost_nonempty_index_1based(rr, cfg.max_cols_hard)
                if rm <= header.col_end_excl_0:
                    continue

                # Count rows that have any non-empty value beyond the current header span
                any_beyond = False
                for j in range(header.col_end_excl_0, min(rm, cfg.max_cols_hard)):
                    v = rr[j] if j < len(rr) else None
                    if not is_empty_cell(v):
                        any_beyond = True
                        break
                if any_beyond:
                    beyond_count += 1
                    max_right = max(max_right, rm)

            # Expand only if beyond-span content appears consistently.
            if beyond_count >= max(3, int(0.30 * max(1, len(scan_rows)))):
                header = type(header)(
                    header_start_idx=header.header_start_idx,
                    header_row_count=header.header_row_count,
                    header_rows_1based=header.header_rows_1based,
                    headers_flat=header.headers_flat,
                    col_start_0=header.col_start_0,
                    col_end_excl_0=min(cfg.max_cols_hard, max_right),
                )

        # We'll push any unused rows into pending when switching to next table
        # Data stream = data_seed then rows_iter
        main_buf: List[Tuple[int, List[object]]] = []
        side_left_buf: List[Tuple[int, List[object]]] = []
        side_right_buf: List[Tuple[int, List[object]]] = []

        blank_run = 0
        in_break = False
        next_preview_seed: List[Tuple[int, List[object]]] = []

        # Forward fill state for first N columns in the table span
        ff_n = max(0, cfg.forward_fill_cols)
        ff_last: List[object] = [None] * ff_n

        def _maybe_forward_fill(rr: List[object]) -> List[object]:
            if ff_n <= 0:
                return rr
            for k in range(ff_n):
                j = header.col_start_0 + k
                if j >= header.col_end_excl_0:
                    break
                if j >= len(rr):
                    break
                if is_empty_cell(rr[j]) and not is_empty_cell(ff_last[k]):
                    rr[j] = ff_last[k]
                elif not is_empty_cell(rr[j]):
                    ff_last[k] = rr[j]
            return rr

        def _row_has_nonempty_outside_span(rr: List[object]) -> Tuple[bool, bool]:
            # (left_has, right_has)
            left_has = False
            right_has = False
            for j, v in enumerate(rr):
                if is_empty_cell(v):
                    continue
                if j < header.col_start_0:
                    left_has = True
                elif j >= header.col_end_excl_0:
                    right_has = True
                if left_has and right_has:
                    return True, True
            return left_has, right_has

        def _outside_span_text_len(rr: List[object]) -> int:
            total = 0
            for j, v in enumerate(rr):
                if is_empty_cell(v):
                    continue
                if j < header.col_start_0 or j >= header.col_end_excl_0:
                    total += len(str(v))
            return total

        def _flush_all_buffers() -> None:
            if main_buf:
                emit_row_chunks_no_loss(
                    headers=header.headers_flat,
                    buffer=main_buf,
                    workbook_name=workbook_name,
                    sheet_name=sheet_name,
                    table_id=table_id,
                    header_rows_1based=header.header_rows_1based,
                    kind="rows",
                    doc_type="spreadsheet_rows",
                    add_doc_cb=add_doc_cb,
                    global_order_ref=global_order_ref,
                    max_rows_per_chunk=cfg.max_rows_per_chunk,
                    max_cols_included=cfg.max_cols_included,
                    max_cols_hard=cfg.max_cols_hard,
                    max_chars_per_chunk=cfg.max_chars_per_chunk,
                    max_tokens_per_chunk=cfg.max_tokens_per_chunk,
                    render_mode=cfg.render_mode,
                    col_span=(header.col_start_0, header.col_end_excl_0),
                )
            if cfg.enable_side_notes and side_left_buf:
                left_end = header.col_start_0
                left_start = max(0, left_end - cfg.side_note_max_cols)
                emit_row_chunks_no_loss(
                    headers=header.headers_flat,
                    buffer=side_left_buf,
                    workbook_name=workbook_name,
                    sheet_name=sheet_name,
                    table_id=table_id,
                    header_rows_1based=header.header_rows_1based,
                    kind="notes_side_left",
                    doc_type="spreadsheet_notes",
                    add_doc_cb=add_doc_cb,
                    global_order_ref=global_order_ref,
                    max_rows_per_chunk=cfg.max_rows_per_chunk,
                    max_cols_included=cfg.max_cols_included,
                    max_cols_hard=cfg.max_cols_hard,
                    max_chars_per_chunk=cfg.max_chars_per_chunk,
                    max_tokens_per_chunk=cfg.max_tokens_per_chunk,
                    render_mode=cfg.render_mode,
                    col_span=(left_start, left_end),
                )
            if cfg.enable_side_notes and side_right_buf:
                # Determine max cols seen for right span from buffered rows
                max_cols_seen = 0
                for _, rr in side_right_buf:
                    if len(rr) > max_cols_seen:
                        max_cols_seen = len(rr)
                right_end = min(max_cols_seen, cfg.max_cols_hard)
                right_start = max(header.col_end_excl_0, right_end - cfg.side_note_max_cols)
                emit_row_chunks_no_loss(
                    headers=header.headers_flat + [excel_col_name(i) for i in range(len(header.headers_flat), max_cols_seen)],
                    buffer=side_right_buf,
                    workbook_name=workbook_name,
                    sheet_name=sheet_name,
                    table_id=table_id,
                    header_rows_1based=header.header_rows_1based,
                    kind="notes_side_right",
                    doc_type="spreadsheet_notes",
                    add_doc_cb=add_doc_cb,
                    global_order_ref=global_order_ref,
                    max_rows_per_chunk=cfg.max_rows_per_chunk,
                    max_cols_included=cfg.max_cols_included,
                    max_cols_hard=cfg.max_cols_hard,
                    max_chars_per_chunk=cfg.max_chars_per_chunk,
                    max_tokens_per_chunk=cfg.max_tokens_per_chunk,
                    render_mode=cfg.render_mode,
                    col_span=(right_start, right_end),
                )

        # Iterate data rows (including preview tail) while preserving ability to detect EOF for bottom notes.
        data_q = deque(data_seed)
        eof = False
        while True:
            if data_q:
                rn, rr0 = data_q.popleft()
            else:
                try:
                    rn, rr0 = next(rows_iter)
                    rows_seen_total += 1
                    if rows_seen_total > cfg.max_rows_per_sheet:
                        raise ValueError(
                            f"Sheet '{sheet_name}' exceeds max rows ({cfg.max_rows_per_sheet}). Please split/export."
                        )
                except StopIteration:
                    eof = True
                    break

            rr = _apply_merge_fill(rn, rr0)

            # repeated header rows inside data => skip
            if row_matches_header(
                rr,
                header.headers_flat,
                col_start_0=header.col_start_0,
                col_end_excl_0=header.col_end_excl_0,
            ):
                continue

            # detect blank row in table span
            if is_blank_row(
                rr,
                col_start_0=header.col_start_0,
                col_end_excl_0=header.col_end_excl_0,
                empty_ratio_threshold=cfg.blank_row_empty_ratio,
                key_cols=2,
            ):
                blank_run += 1
                if blank_run >= cfg.blank_row_break:
                    in_break = True
                continue
            else:
                blank_run = 0

            if in_break:
                # First non-blank after a blank-run break => seed next table detection
                next_preview_seed.append((rn, rr))
                if len(next_preview_seed) >= cfg.header_scan_rows:
                    break
                continue

            rr = _maybe_forward_fill(rr)

            # Side notes outside the table span
            if cfg.enable_side_notes:
                left_has, right_has = _row_has_nonempty_outside_span(rr)
                if left_has and _outside_span_text_len(rr) >= cfg.side_note_min_chars:
                    side_left_buf.append((rn, rr))
                if right_has and _outside_span_text_len(rr) >= cfg.side_note_min_chars:
                    side_right_buf.append((rn, rr))

            # Main table row
            main_buf.append((rn, rr))

            if len(main_buf) >= cfg.max_rows_per_chunk:
                emit_row_chunks_no_loss(
                    headers=header.headers_flat,
                    buffer=main_buf,
                    workbook_name=workbook_name,
                    sheet_name=sheet_name,
                    table_id=table_id,
                    header_rows_1based=header.header_rows_1based,
                    kind="rows",
                    doc_type="spreadsheet_rows",
                    add_doc_cb=add_doc_cb,
                    global_order_ref=global_order_ref,
                    max_rows_per_chunk=cfg.max_rows_per_chunk,
                    max_cols_included=cfg.max_cols_included,
                    max_cols_hard=cfg.max_cols_hard,
                    max_chars_per_chunk=cfg.max_chars_per_chunk,
                    max_tokens_per_chunk=cfg.max_tokens_per_chunk,
                    render_mode=cfg.render_mode,
                    col_span=(header.col_start_0, header.col_end_excl_0),
                )

            # keep side buffers bounded too
            if cfg.enable_side_notes and len(side_left_buf) >= cfg.max_rows_per_chunk:
                left_end = header.col_start_0
                left_start = max(0, left_end - cfg.side_note_max_cols)
                emit_row_chunks_no_loss(
                    headers=header.headers_flat,
                    buffer=side_left_buf,
                    workbook_name=workbook_name,
                    sheet_name=sheet_name,
                    table_id=table_id,
                    header_rows_1based=header.header_rows_1based,
                    kind="notes_side_left",
                    doc_type="spreadsheet_notes",
                    add_doc_cb=add_doc_cb,
                    global_order_ref=global_order_ref,
                    max_rows_per_chunk=cfg.max_rows_per_chunk,
                    max_cols_included=cfg.max_cols_included,
                    max_cols_hard=cfg.max_cols_hard,
                    max_chars_per_chunk=cfg.max_chars_per_chunk,
                    max_tokens_per_chunk=cfg.max_tokens_per_chunk,
                    render_mode=cfg.render_mode,
                    col_span=(left_start, left_end),
                )
            if cfg.enable_side_notes and len(side_right_buf) >= cfg.max_rows_per_chunk:
                max_cols_seen = 0
                for _, rrr in side_right_buf:
                    if len(rrr) > max_cols_seen:
                        max_cols_seen = len(rrr)
                right_end = min(max_cols_seen, cfg.max_cols_hard)
                right_start = max(header.col_end_excl_0, right_end - cfg.side_note_max_cols)
                emit_row_chunks_no_loss(
                    headers=header.headers_flat + [excel_col_name(i) for i in range(len(header.headers_flat), max_cols_seen)],
                    buffer=side_right_buf,
                    workbook_name=workbook_name,
                    sheet_name=sheet_name,
                    table_id=table_id,
                    header_rows_1based=header.header_rows_1based,
                    kind="notes_side_right",
                    doc_type="spreadsheet_notes",
                    add_doc_cb=add_doc_cb,
                    global_order_ref=global_order_ref,
                    max_rows_per_chunk=cfg.max_rows_per_chunk,
                    max_cols_included=cfg.max_cols_included,
                    max_cols_hard=cfg.max_cols_hard,
                    max_chars_per_chunk=cfg.max_chars_per_chunk,
                    max_tokens_per_chunk=cfg.max_tokens_per_chunk,
                    render_mode=cfg.render_mode,
                    col_span=(right_start, right_end),
                )

        # Flush whatever remains for this table
        _flush_all_buffers()

        # EOF reached after a break:
        # Do NOT automatically classify as bottom notes; it may be a final table.
        # Feed it back into the outer loop. If no header is found, we emit notes_bottom there.
        if eof and next_preview_seed:
            eof_tail_after_break = True
            eof_tail_prev_table_id = table_id
            pending = deque(next_preview_seed)
            table_id += 1
            in_break = False
            continue

        if next_preview_seed:
            # Continue with next table
            pending = deque(next_preview_seed)
            table_id += 1
            in_break = False
            continue

        # No next table seed => we're done. Any remaining pending rows are bottom notes (if any).
        # (We don't have remaining rows here because the iterator is consumed; this is a v1.)
        break
