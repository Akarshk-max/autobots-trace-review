from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple
from urllib.parse import quote, unquote


def encode_sheet_name(sheet_name: str) -> str:
    # Encode to keep locator parse robust even if sheet names contain ';' etc.
    return quote(sheet_name or "", safe="")


def decode_sheet_name(sheet_name_enc: str) -> str:
    try:
        return unquote(sheet_name_enc or "")
    except Exception:
        return sheet_name_enc or ""


@dataclass(frozen=True)
class LocatorInfo:
    sheet: str
    sheet_enc: str
    kind: Optional[str] = None
    table: int = 1
    rows: Optional[Tuple[int, int]] = None  # (start,end), inclusive
    cols: Optional[str] = None              # "A-F"
    header_rows: Optional[Tuple[int, int]] = None  # (start,end), inclusive


def build_source_locator(
    *,
    sheet_name: str,
    kind: str,
    table: Optional[int] = None,
    rows: Optional[Tuple[int, int]] = None,
    cols: Optional[str] = None,
    header_rows: Optional[Tuple[int, int]] = None,
) -> str:
    sheet_enc = encode_sheet_name(sheet_name)
    parts = [f"Sheet={sheet_enc}", f"kind={kind}"]
    if table is not None:
        parts.append(f"table={int(table)}")
    if rows is not None:
        parts.append(f"rows={int(rows[0])}-{int(rows[1])}")
    if cols is not None:
        parts.append(f"cols={cols}")
    if header_rows is not None:
        parts.append(f"header_rows={int(header_rows[0])}-{int(header_rows[1])}")
    return "; ".join(parts)


def parse_source_locator(locator: str) -> LocatorInfo:
    """
    Parse 'Sheet=<enc>; kind=rows; table=1; rows=10-20; cols=A-F; header_rows=3-4'
    Backward compatible with older locators that may omit kind/table/cols.
    """
    loc = locator or ""
    parts = [p.strip() for p in loc.split(";") if p.strip()]
    kv: Dict[str, str] = {}
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            kv[k.strip()] = v.strip()

    sheet_enc = kv.get("Sheet", "") or ""
    sheet = decode_sheet_name(sheet_enc) if sheet_enc else "__UNKNOWN__"

    kind = kv.get("kind")
    table_raw = kv.get("table")
    try:
        table = int(table_raw) if table_raw is not None else 1
    except Exception:
        table = 1

    def _parse_range(s: Optional[str]) -> Optional[Tuple[int, int]]:
        if not s:
            return None
        if "-" not in s:
            return None
        a, b = s.split("-", 1)
        try:
            return int(a), int(b)
        except Exception:
            return None

    rows = _parse_range(kv.get("rows"))
    header_rows = _parse_range(kv.get("header_rows"))
    cols = kv.get("cols")

    return LocatorInfo(
        sheet=sheet,
        sheet_enc=sheet_enc or encode_sheet_name(sheet),
        kind=kind,
        table=table,
        rows=rows,
        cols=cols,
        header_rows=header_rows,
    )
