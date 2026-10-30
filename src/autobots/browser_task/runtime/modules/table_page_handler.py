import asyncio
from typing import Any, Dict, List, Optional

from loguru import logger
from playwright.async_api import Locator, Page

from src.autobots.browser_task.inmem.state import JobEnvelope
from src.autobots.browser_task.runtime.artifacts import Artifact, ArtifactKind
from src.autobots.browser_task.runtime.modules.auth_handler import dismiss_post_login_popups
from src.autobots.browser_task.runtime.modules.base import ModuleResult
from src.autobots.browser_task.runtime.modules.utils import (
    log_ctx,
)


def _infer(node: Any, prefix: str, out: Dict[str, str]):
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{prefix}.{k}" if prefix else k
            out[p] = type(v).__name__
            _infer(v, p, out)
    elif isinstance(node, list) and node:
        _infer(node[0], prefix + "[]", out)


def _is_noise(url: str, ctype: str) -> bool:
    if any(url.endswith(x) for x in (".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico")):
        return True
    if any(x in url for x in ("doubleclick", "googletagmanager", "google-analytics", "adsystem", "facebook", "pixel")):
        return True
    if ctype.startswith("image/") or ctype.startswith("text/css"):
        return True
    return False

def _locator_for(page: Page, sel: str) -> Locator:
    s = (sel or "").strip()
    if not s:
        return page.locator("css=__never__")
    if s.startswith("xpath="):
        return page.locator(s)
    if s.startswith("css="):
        return page.locator(s[len("css="):])
    if s.startswith("//") or s.startswith("(//"):
        return page.locator("xpath=" + s)
    return page.locator(s)

def _is_xpath(sel: str) -> bool:
    s = (sel or "").strip()
    return s.startswith("xpath=") or s.startswith("//") or s.startswith("(//")


_JS_EXTRACT_TABLE = r"""
(arg) => {
  const selector = arg.selector;
  const maxRows = Number(arg.maxRows || 200);

  const el = document.querySelector(selector);
  if (!el) return null;

  const table = el.tagName && el.tagName.toLowerCase() === "table" ? el : el.querySelector("table");
  if (!table) return null;

  // Extract headers with multiple strategies
  let headers = [];

  // Strategy 1: Standard thead > th
  const thead = table.querySelector("thead");
  if (thead) {
    const ths = Array.from(thead.querySelectorAll("th"));
    headers = ths.map(th => (th.innerText || th.textContent || "").trim());
  }

  // Strategy 2: First row with th elements
  if (headers.length === 0) {
    const firstRowTh = table.querySelector("tr th");
    if (firstRowTh) {
      const firstRow = firstRowTh.closest("tr");
      if (firstRow) {
        headers = Array.from(firstRow.querySelectorAll("th, td")).map(
          th => (th.innerText || th.textContent || "").trim()
        );
      }
    }
  }

  // Strategy 3: ARIA headers
  if (headers.length === 0) {
    const headerCells = table.querySelectorAll("[role='columnheader']");
    if (headerCells.length > 0) {
      headers = Array.from(headerCells).map(th => (th.innerText || th.textContent || "").trim());
    }
  }

  // Strategy 4: scope="col" attributes
  if (headers.length === 0) {
    const scopeHeaders = table.querySelectorAll("th[scope='col']");
    if (scopeHeaders.length > 0) {
      headers = Array.from(scopeHeaders).map(th => (th.innerText || th.textContent || "").trim());
    }
  }

  const rows = [];
  const tbody = table.querySelector("tbody") || table;
  const trs = Array.from(tbody.querySelectorAll("tr"));

  for (const tr of trs.slice(0, maxRows)) {
    // Skip header rows
    if (tr.querySelector("th[scope='col']") || tr.querySelector("[role='columnheader']")) {
      continue;
    }

    const tds = Array.from(tr.querySelectorAll("td, th"));
    const row = tds.map(td => {
      // Get text content, handling nested elements
      let text = (td.innerText || td.textContent || "").trim();
      // Handle common formatting
      text = text.replace(/\s+/g, " "); // Normalize whitespace
      return text;
    });

    // Only add non-empty rows
    if (row.some(x => x && x.length > 0)) {
      rows.push(row);
    }
  }

  return { kind: "table", headers, rows };
}
"""

_JS_EXTRACT_TABLE_EL = r"""
(arg) => {
  const el = arg.el;
  const maxRows = Number(arg.maxRows || 200);
  if (!el) return null;

  const table = (el.tagName && el.tagName.toLowerCase() === "table")
    ? el
    : (el.querySelector ? el.querySelector("table") : null);
  if (!table) return null;

  // Extract headers with multiple strategies
  let headers = [];

  // Strategy 1: Standard thead > th
  const thead = table.querySelector("thead");
  if (thead) {
    const ths = Array.from(thead.querySelectorAll("th"));
    headers = ths.map(th => (th.innerText || th.textContent || "").trim());
  }

  // Strategy 2: First row with th elements
  if (headers.length === 0) {
    const firstRowTh = table.querySelector("tr th");
    if (firstRowTh) {
      const firstRow = firstRowTh.closest("tr");
      if (firstRow) {
        headers = Array.from(firstRow.querySelectorAll("th, td")).map(
          th => (th.innerText || th.textContent || "").trim()
        );
      }
    }
  }

  // Strategy 3: ARIA headers
  if (headers.length === 0) {
    const headerCells = table.querySelectorAll("[role='columnheader']");
    if (headerCells.length > 0) {
      headers = Array.from(headerCells).map(th => (th.innerText || th.textContent || "").trim());
    }
  }

  // Strategy 4: scope="col" attributes
  if (headers.length === 0) {
    const scopeHeaders = table.querySelectorAll("th[scope='col']");
    if (scopeHeaders.length > 0) {
      headers = Array.from(scopeHeaders).map(th => (th.innerText || th.textContent || "").trim());
    }
  }

  const rows = [];
  const tbody = table.querySelector("tbody") || table;
  const trs = Array.from(tbody.querySelectorAll("tr"));

  for (const tr of trs.slice(0, maxRows)) {
    // Skip header rows
    if (tr.querySelector("th[scope='col']") || tr.querySelector("[role='columnheader']")) {
      continue;
    }

    const tds = Array.from(tr.querySelectorAll("td, th"));
    const row = tds.map(td => {
      // Get text content, handling nested elements
      let text = (td.innerText || td.textContent || "").trim();
      // Handle common formatting
      text = text.replace(/\s+/g, " "); // Normalize whitespace
      return text;
    });

    // Only add non-empty rows
    if (row.some(x => x && x.length > 0)) {
      rows.push(row);
    }
  }

  return { kind: "table", headers, rows };
}
"""

_JS_EXTRACT_VISIBLE_GRID = r"""
(arg) => {
  const selector = arg.selector;
  const maxRows = Number(arg.maxRows || 200);

  const root = document.querySelector(selector);
  if (!root) return null;

  // Try multiple row selectors for different grid implementations
  const rowSelectors = [
    "[role='row']",           // ARIA grids
    ".ag-row",                // AG-Grid
    ".ag-center-cols .ag-row", // AG-Grid center columns
    ".react-grid-Row",        // React Data Grid
    ".rdg-row",               // React Data Grid v7+
    ".MuiDataGrid-row",       // Material-UI DataGrid
    ".MuiTableRow-root",      // Material-UI Table
    ".ant-table-row",         // Ant Design
    ".bx--data-table tr",     // Carbon Design System
    ".v-data-table__tr",      // Vuetify
    ".slick-row",             // SlickGrid
    ".ui-widget-content",     // jQuery UI
    ".k-grid tr",             // Kendo UI
    ".dx-data-row",           // DevExpress
    ".wj-row",                // Wijmo
    ".smart-table-row",       // Smart HTML Elements
    "tr[data-row-id]",        // Generic data rows
    "div[aria-rowindex]",     // ARIA rowindex
    ".tr",                    // Generic row class
    ".row",                   // Generic row class
    "> div > div",            // Common flex/grid containers
  ];

  let candidates = [];
  for (const sel of rowSelectors) {
    try {
      const found = root.querySelectorAll(sel);
      if (found.length > 0) {
        candidates = Array.from(found);
        break;
      }
    } catch (e) {}
  }

  // Fallback: try to find row-like structures
  if (candidates.length === 0) {
    const allChildren = root.children;
    for (const child of allChildren) {
      // Check if child has cell-like children
      const hasCells = child.querySelectorAll("[role='cell'], [role='gridcell'], td, .cell, .col, [class*='cell'], [class*='col']").length > 0;
      if (hasCells) {
        candidates.push(child);
      }
    }
  }

  const rows = [];

  for (const el of candidates.slice(0, maxRows)) {
    // Skip header rows
    if (el.getAttribute("role") === "rowheader" ||
        el.querySelector("[role='columnheader']") ||
        el.classList.contains("header") ||
        el.classList.contains("ag-header-row")) {
      continue;
    }

    // Try multiple cell selectors
    const cellSelectors = [
      "[role='cell']",
      "[role='gridcell']",
      "td",
      "th",
      ".ag-cell",
      ".react-grid-Cell",
      ".rdg-cell",
      ".MuiDataGrid-cell",
      ".MuiTableCell-root",
      ".ant-table-cell",
      ".bx--table-header-label",
      ".v-data-table__td",
      ".slick-cell",
      ".ui-widget-content td",
      ".k-grid td",
      ".dx-cell",
      ".wj-cell",
      ".smart-table-cell",
      "[data-col-id]",
      "div[aria-colindex]",
      ".cell",
      ".col",
      "> div",  // Direct div children
    ];

    let cells = [];
    for (const sel of cellSelectors) {
      try {
        const found = el.querySelectorAll(sel);
        if (found.length > 0) {
          cells = Array.from(found);
          break;
        }
      } catch (e) {}
    }

    let row = [];
    if (cells.length > 0) {
      row = cells.map(c => {
        let text = (c.innerText || c.textContent || "").trim();
        text = text.replace(/\s+/g, " "); // Normalize whitespace
        return text;
      });
    } else {
      // Fallback: get all text from the row
      let text = (el.innerText || el.textContent || "").trim();
      text = text.replace(/\s+/g, " ");
      if (text) row = [text];
    }

    // Filter out empty rows and header-like rows
    const hasContent = row.some(x => x && x.length > 0);
    const isHeaderRow = row.every(cell => {
      // Check if cell looks like a header (all caps, short, etc.)
      return cell === cell.toUpperCase() && cell.length < 50;
    });

    if (hasContent && !isHeaderRow) {
      rows.push(row);
    }
  }

  return { kind: "grid", rows };
}
"""

_JS_EXTRACT_VISIBLE_GRID_EL = r"""
(arg) => {
  const el = arg.el;
  const maxRows = Number(arg.maxRows || 200);
  if (!el) return null;

  // Try multiple row selectors for different grid implementations
  const rowSelectors = [
    "[role='row']",
    ".ag-row",
    ".ag-center-cols .ag-row",
    ".react-grid-Row",
    ".rdg-row",
    ".MuiDataGrid-row",
    ".MuiTableRow-root",
    ".ant-table-row",
    ".bx--data-table tr",
    ".v-data-table__tr",
    ".slick-row",
    ".ui-widget-content",
    ".k-grid tr",
    ".dx-data-row",
    ".wj-row",
    ".smart-table-row",
    "tr[data-row-id]",
    "div[aria-rowindex]",
    ".tr",
    ".row",
    "> div > div",
  ];

  let candidates = [];
  for (const sel of rowSelectors) {
    try {
      const found = el.querySelectorAll(sel);
      if (found.length > 0) {
        candidates = Array.from(found);
        break;
      }
    } catch (e) {}
  }

  // Fallback: try to find row-like structures
  if (candidates.length === 0) {
    const allChildren = el.children;
    for (const child of allChildren) {
      const hasCells = child.querySelectorAll("[role='cell'], [role='gridcell'], td, .cell, .col, [class*='cell'], [class*='col']").length > 0;
      if (hasCells) {
        candidates.push(child);
      }
    }
  }

  const rows = [];

  for (const rowEl of candidates.slice(0, maxRows)) {
    // Skip header rows
    if (rowEl.getAttribute("role") === "rowheader" ||
        rowEl.querySelector("[role='columnheader']") ||
        rowEl.classList.contains("header") ||
        rowEl.classList.contains("ag-header-row")) {
      continue;
    }

    // Try multiple cell selectors
    const cellSelectors = [
      "[role='cell']",
      "[role='gridcell']",
      "td",
      "th",
      ".ag-cell",
      ".react-grid-Cell",
      ".rdg-cell",
      ".MuiDataGrid-cell",
      ".MuiTableCell-root",
      ".ant-table-cell",
      ".bx--table-header-label",
      ".v-data-table__td",
      ".slick-cell",
      ".ui-widget-content td",
      ".k-grid td",
      ".dx-cell",
      ".wj-cell",
      ".smart-table-cell",
      "[data-col-id]",
      "div[aria-colindex]",
      ".cell",
      ".col",
      "> div",
    ];

    let cells = [];
    for (const sel of cellSelectors) {
      try {
        const found = rowEl.querySelectorAll(sel);
        if (found.length > 0) {
          cells = Array.from(found);
          break;
        }
      } catch (e) {}
    }

    let row = [];
    if (cells.length > 0) {
      row = cells.map(c => {
        let text = (c.innerText || c.textContent || "").trim();
        text = text.replace(/\s+/g, " ");
        return text;
      });
    } else {
      let text = (rowEl.innerText || rowEl.textContent || "").trim();
      text = text.replace(/\s+/g, " ");
      if (text) row = [text];
    }

    // Filter out empty rows and header-like rows
    const hasContent = row.some(x => x && x.length > 0);
    const isHeaderRow = row.every(cell => {
      return cell === cell.toUpperCase() && cell.length < 50;
    });

    if (hasContent && !isHeaderRow) {
      rows.push(row);
    }
  }

  return { kind: "grid", rows };
}
"""

_JS_SCROLL_CONTAINER = r"""
(arg) => {
  const selector = arg.selector;
  const delta = Number(arg.delta || 800);

  const el = document.querySelector(selector);
  if (!el) return { ok: false, reason: "missing" };

  function isScrollable(n) {
    if (!n) return false;
    const s = getComputedStyle(n);
    return (s.overflowY === "auto" || s.overflowY === "scroll") && (n.scrollHeight > n.clientHeight + 2);
  }

  let target = el;
  while (target && target !== document.body && !isScrollable(target)) target = target.parentElement;
  if (!target) target = el;

  const before = target.scrollTop;
  target.scrollTop = before + delta;
  const after = target.scrollTop;

  return {
    ok: true,
    before,
    after,
    canScroll: (after !== before),
    scrollHeight: target.scrollHeight,
    clientHeight: target.clientHeight
  };
}
"""

_JS_SCROLL_CONTAINER_EL = r"""
(arg) => {
  const el = arg.el;
  const delta = Number(arg.delta || 800);

  if (!el) return { ok: false, reason: "missing" };

  function isScrollable(n) {
    if (!n) return false;
    const s = getComputedStyle(n);
    return (s.overflowY === "auto" || s.overflowY === "scroll") && (n.scrollHeight > n.clientHeight + 2);
  }

  let target = el;
  while (target && target !== document.body && !isScrollable(target)) target = target.parentElement;
  if (!target) target = el;

  const before = target.scrollTop;
  target.scrollTop = before + delta;
  const after = target.scrollTop;

  return {
    ok: true,
    before,
    after,
    canScroll: (after !== before),
    scrollHeight: target.scrollHeight,
    clientHeight: target.clientHeight
  };
}
"""

_JS_TABLE_FINGERPRINT = r"""
(arg) => {
  const sel = arg.selector || ".semi-table";
  const root = document.querySelector(sel);
  if (!root) return "";
  // grab a stable-ish slice of visible text
  const t = (root.innerText || root.textContent || "").trim().replace(/\s+/g, " ");
  return t.slice(0, 20000);
}
"""

class TableExtractionModule:
    name: str = "tables"

    DEFAULT_TABLE_SELECTORS = [
        "table",
        "table.data",
        "table.table",
        ".table",
        ".data-table",
        ".dataTable",
        '[role="table"]',
        '[role="grid"]',
        '[role="grid"] > div',
        ".ag-root-wrapper",
        ".ag-grid",
        ".ag-center-cols-viewport",
        ".react-grid-Container",
        ".rdg",
        ".react-data-grid",
        ".MuiTable-root",
        ".MuiDataGrid-root",
        ".MuiDataGrid-main",
        ".ant-table",
        ".ant-table-wrapper",
        ".ant-table-container",
        ".table-responsive",
        ".table > tbody",
        ".bx--data-table",
        ".bx--data-table-container",
        ".v-data-table",
        ".v-data-table__wrapper",
        ".v-table",
        ".k-grid",
        ".k-grid-content",
        ".dx-datagrid",
        ".dx-datagrid-rowsview",
        ".dataTables_wrapper",
        ".dataTable",
        ".slickgrid-container",
        ".slick-viewport",
        ".ht_master",
        ".handsontable",
        ".tabulator",
        ".tabulator-tableholder",
        ".fancy-grid",
        ".webix_datatable",
        ".dhx_grid",
        ".x-grid-view",
        '[class*="table"][class*="container"]',
        '[class*="data"][class*="grid"]',
        '[class*="grid"][class*="container"]',
        ".grid-container",
        ".table-container",
        ".list-container",
        ".div-table",
        ".divTable",
        ".flex-table",
    ]

    async def _get_current_page_num(self, page: Page) -> int:
        """
        Returns active page number, or 0 if unknown.
        """
        try:
            value = await page.evaluate(
                """
                () => {
                  const candidates = [
                    'li.semi-page-item.semi-page-item-active',
                    'li.semi-page-item[aria-current="page"]',
                    '.semi-page-item-active',
                    '[aria-current="page"]',
                    '.semi-pagination-item-active',
                    '.semi-page-item-active button',
                    '.semi-page-item-active span'
                  ];

                  for (const sel of candidates) {
                    const el = document.querySelector(sel);
                    if (!el) continue;
                    const txt = (el.innerText || el.textContent || "").trim();
                    const m = txt.match(/\\d+/);
                    if (m) return Number(m[0]);
                  }

                  const items = Array.from(document.querySelectorAll('[aria-label^="Page "]'));
                  for (const el of items) {
                    const cls = el.className || "";
                    const cur = el.getAttribute("aria-current");
                    const selected = el.getAttribute("aria-selected");
                    if (
                      cls.includes("active") ||
                      cur === "page" ||
                      selected === "true"
                    ) {
                      const label = el.getAttribute("aria-label") || "";
                      const m = label.match(/Page\\s+(\\d+)/i);
                      if (m) return Number(m[1]);
                    }
                  }

                  return null;
                }
                """
            )
            if isinstance(value, int) and value > 0:
                return value
            return 0
        except Exception:
            return 0

    async def _get_pagination_state(self, page: Page) -> dict:
        """
        Reads current pagination strip.
        """
        try:
            state = await page.evaluate(
                """
                () => {
                  const pager =
                    document.querySelector('.semi-table-pagination-outer ul.semi-page') ||
                    document.querySelector('ul.semi-page');

                  if (!pager) {
                    return {
                      current: 0,
                      visible_pages: [],
                      more_count: 0,
                      has_prev: false,
                      has_next: false
                    };
                  }

                  const pageEls = Array.from(
                    pager.querySelectorAll('li.semi-page-item[aria-label^="Page "]')
                  );

                  const visible_pages = [];
                  for (const el of pageEls) {
                    const label = el.getAttribute("aria-label") || "";
                    const m = label.match(/Page\\s+(\\d+)/i);
                    if (m) visible_pages.push(Number(m[1]));
                  }

                  let current = 0;
                  const currentEl =
                    pager.querySelector('li.semi-page-item.semi-page-item-active') ||
                    pager.querySelector('li.semi-page-item[aria-current="page"]') ||
                    pager.querySelector('.semi-page-item-active') ||
                    pager.querySelector('[aria-current="page"]');

                  if (currentEl) {
                    const txt = (currentEl.innerText || currentEl.textContent || "").trim();
                    const m = txt.match(/\\d+/);
                    if (m) current = Number(m[0]);

                    if (!current) {
                      const label = currentEl.getAttribute && currentEl.getAttribute("aria-label");
                      const mm = label && label.match(/Page\\s+(\\d+)/i);
                      if (mm) current = Number(mm[1]);
                    }
                  }

                  const moreEls = Array.from(
                    pager.querySelectorAll('li.semi-page-item[aria-label="More"]')
                  );

                  const prevEl =
                    pager.querySelector('li.semi-page-prev, li[aria-label="Previous"]');
                  const nextEl =
                    pager.querySelector('li.semi-page-next, li[aria-label="Next"]');

                  const prevDisabled = !!(
                    prevEl &&
                    (
                      (prevEl.getAttribute("aria-disabled") || "").toLowerCase() === "true" ||
                      prevEl.className.includes("disabled")
                    )
                  );

                  const nextDisabled = !!(
                    nextEl &&
                    (
                      (nextEl.getAttribute("aria-disabled") || "").toLowerCase() === "true" ||
                      nextEl.className.includes("disabled")
                    )
                  );

                  return {
                    current,
                    visible_pages,
                    more_count: moreEls.length,
                    has_prev: !!prevEl && !prevDisabled,
                    has_next: !!nextEl && !nextDisabled
                  };
                }
                """
            )
            if isinstance(state, dict):
                return state
        except Exception:
            pass

        return {
            "current": 0,
            "visible_pages": [],
            "more_count": 0,
            "has_prev": False,
            "has_next": False,
        }

    async def _table_fingerprint(self, page: Page, selector: str) -> str:
        sel = (selector or "").strip()
        if not sel:
            return ""

        try:
            if _is_xpath(sel):
                h = await _locator_for(page, sel).first.element_handle()
                if not h:
                    return ""
                try:
                    return await page.evaluate(
                        """
                        (el) => {
                          if (!el) return "";
                          const t = (el.innerText || el.textContent || "")
                            .trim()
                            .replace(/\\s+/g, " ");
                          return t.slice(0, 20000);
                        }
                        """,
                        h,
                    )
                finally:
                    try:
                        await h.dispose()
                    except Exception:
                        pass

            return await page.evaluate(
                """
                (arg) => {
                  const root = document.querySelector(arg.selector);
                  if (!root) return "";
                  const t = (root.innerText || root.textContent || "")
                    .trim()
                    .replace(/\\s+/g, " ");
                  return t.slice(0, 20000);
                }
                """,
                {"selector": sel},
            )
        except Exception:
            return ""

    async def _selector_has_rows(
        self,
        page: Page,
        selector: str,
        *,
        max_rows: int = 5,
    ) -> bool:
        sel = (selector or "").strip()
        if not sel:
            return False

        try:
            h = None
            if _is_xpath(sel):
                h = await _locator_for(page, sel).first.element_handle()

            try:
                if h:
                    extracted = await page.evaluate(
                        _JS_EXTRACT_TABLE_EL,
                        {"el": h, "maxRows": max_rows},
                    )
                else:
                    extracted = await page.evaluate(
                        _JS_EXTRACT_TABLE,
                        {"selector": sel, "maxRows": max_rows},
                    )
            finally:
                if h:
                    try:
                        await h.dispose()
                    except Exception:
                        pass

            rows = (extracted or {}).get("rows") or []
            if any(any((cell or "").strip() for cell in row) for row in rows):
                return True

            h_grid = None
            if _is_xpath(sel):
                h_grid = await _locator_for(page, sel).first.element_handle()

            try:
                if h_grid:
                    vis = await page.evaluate(
                        _JS_EXTRACT_VISIBLE_GRID_EL,
                        {"el": h_grid, "maxRows": max_rows},
                    )
                else:
                    vis = await page.evaluate(
                        _JS_EXTRACT_VISIBLE_GRID,
                        {"selector": sel, "maxRows": max_rows},
                    )
            finally:
                if h_grid:
                    try:
                        await h_grid.dispose()
                    except Exception:
                        pass

            rows = (vis or {}).get("rows") or []
            return any(any((cell or "").strip() for cell in row) for row in rows)

        except Exception:
            return False

    async def _safe_click(self, page: Page, loc: Locator) -> tuple[bool, str]:
        errors: list[str] = []

        try:
            await loc.scroll_into_view_if_needed(timeout=3000)
        except Exception as e:
            errors.append(f"scroll_into_view:{str(e)[:120]}")

        try:
            child = loc.locator("button, a, [role='button'], span").first
            if await child.count() > 0:
                try:
                    await child.click(timeout=6000)
                    return True, "child_click"
                except Exception as e:
                    errors.append(f"child_click:{str(e)[:120]}")
        except Exception as e:
            errors.append(f"child_lookup:{str(e)[:120]}")

        try:
            await loc.click(timeout=6000)
            return True, "locator_click"
        except Exception as e:
            errors.append(f"locator_click:{str(e)[:120]}")

        try:
            h = await loc.element_handle()
            if h:
                try:
                    await page.evaluate("(el) => el && el.click && el.click()", h)
                    return True, "js_click"
                finally:
                    try:
                        await h.dispose()
                    except Exception:
                        pass
        except Exception as e:
            errors.append(f"js_click:{str(e)[:120]}")

        try:
            h = await loc.element_handle()
            if h:
                try:
                    await page.evaluate(
                        """
                        (el) => {
                          if (!el) return false;
                          const opts = { bubbles: true, cancelable: true, view: window };
                          el.dispatchEvent(new MouseEvent("mousedown", opts));
                          el.dispatchEvent(new MouseEvent("mouseup", opts));
                          el.dispatchEvent(new MouseEvent("click", opts));
                          return true;
                        }
                        """,
                        h,
                    )
                    return True, "dispatch_click"
                finally:
                    try:
                        await h.dispose()
                    except Exception:
                        pass
        except Exception as e:
            errors.append(f"dispatch_click:{str(e)[:120]}")

        return False, "; ".join(errors[-6:]) or "unknown_click_failure"

    async def _wait_for_page_transition(
        self,
        page: Page,
        *,
        expected_page: int,
        before_fp: str,
        verify_selector: str,
        settle_ms: int,
    ) -> dict:
        wait_errors: list[str] = []
        active_changed = False
        fp_changed = False

        try:
            await page.wait_for_function(
                """
                (n) => {
                  const candidates = [
                    'li.semi-page-item.semi-page-item-active',
                    'li.semi-page-item[aria-current="page"]',
                    '.semi-page-item-active',
                    '[aria-current="page"]',
                    '.semi-pagination-item-active'
                  ];

                  for (const sel of candidates) {
                    const el = document.querySelector(sel);
                    if (!el) continue;

                    const txt = (el.innerText || el.textContent || "").trim();
                    const m = txt.match(/\\d+/);
                    if (m && Number(m[0]) === Number(n)) return true;

                    const label = el.getAttribute && el.getAttribute("aria-label");
                    if (label && label === `Page ${n}`) return true;
                  }

                  return false;
                }
                """,
                arg=expected_page,
                timeout=15000,
            )
            active_changed = True
        except Exception as e:
            wait_errors.append(f"active_wait:{str(e)[:160]}")

        after_active = await self._get_current_page_num(page)

        try:
            if verify_selector and not _is_xpath(verify_selector):
                await page.wait_for_function(
                    """
                    (arg) => {
                      const root = document.querySelector(arg.selector);
                      if (!root) return false;
                      const t = (root.innerText || root.textContent || "")
                        .trim()
                        .replace(/\\s+/g, " ");
                      return t.slice(0, 20000) !== arg.before;
                    }
                    """,
                    arg={"selector": verify_selector, "before": before_fp},
                    timeout=20000,
                )
        except Exception as e:
            wait_errors.append(f"fingerprint_wait:{str(e)[:160]}")

        after_fp = await self._table_fingerprint(page, verify_selector)
        fp_changed = bool(before_fp != after_fp)

        if settle_ms > 0:
            await asyncio.sleep(min(1.2, float(settle_ms) / 1000.0))

        return {
            "active_changed": active_changed or (after_active == expected_page),
            "fp_changed": fp_changed,
            "after_active": after_active,
            "before_fp_len": len(before_fp or ""),
            "after_fp_len": len(after_fp or ""),
            "wait_errors": wait_errors,
        }

    async def _click_visible_page(
        self,
        page: Page,
        target_page: int,
        *,
        verify_selector: str,
        settle_ms: int,
        job: JobEnvelope,
        pre_click_ms: int = 300,
        post_click_ms: int = 1200,
        ready_timeout_ms: int = 15000,
    ) -> tuple[bool, str]:
        before_fp = await self._table_fingerprint(page, verify_selector)

        candidates = [
            page.locator(f"li.semi-page-item[aria-label='Page {target_page}']").first,
            page.locator(f"[aria-label='Page {target_page}']").first,
        ]

        for idx, loc in enumerate(candidates, start=1):
            try:
                if await loc.count() == 0:
                    continue
            except Exception as e:
                return False, f"visible_page_count_failed[{idx}]:{str(e)[:120]}"

            if pre_click_ms > 0:
                await asyncio.sleep(pre_click_ms / 1000.0)

            clicked, detail = await self._safe_click(page, loc)
            if not clicked:
                continue

            status = await self._wait_for_page_transition(
                page,
                expected_page=target_page,
                before_fp=before_fp,
                verify_selector=verify_selector,
                settle_ms=settle_ms,
            )
            ready = await self._wait_for_table_ready(
                page,
                verify_selector=verify_selector,
                timeout_ms=ready_timeout_ms,
                extra_sleep_ms=post_click_ms,
            )
            has_rows_after = await self._selector_has_rows(page, verify_selector, max_rows=5)

            ok = False
            reason = None
            if status["after_active"] == target_page and (has_rows_after or ready["rows_seen"]):
                ok = True
                reason = "active_page_match_with_rows"
            elif status["after_active"] == 0 and status["fp_changed"] and (has_rows_after or ready["rows_seen"]):
                ok = True
                reason = "rows_present_after_transition"
            elif status["after_active"] == target_page and ready["container_seen"]:
                ok = True
                reason = "active_page_match_with_container_ready"

            if ok:
                logger.bind(ctx=log_ctx(job)).info(
                    "table_pagination_success page_num={} method=visible_click verify_selector={} "
                    "reason={} after_active={} fp_changed={} has_rows_after={} ready={} ready_rows={} "
                    "before_fp_len={} after_fp_len={}",
                    target_page,
                    verify_selector,
                    reason,
                    status["after_active"],
                    status["fp_changed"],
                    has_rows_after,
                    ready["ready"],
                    ready["rows_seen"],
                    status["before_fp_len"],
                    status["after_fp_len"],
                )
                return True, "visible_click_success"

            return (
                False,
                "visible_click_unconfirmed "
                f"after_active={status['after_active']} "
                f"fp_changed={status['fp_changed']} "
                f"has_rows_after={has_rows_after} "
                f"ready={ready['ready']} "
                f"ready_rows={ready['rows_seen']} "
                f"ready_errors={ready['errors']} "
                f"wait_errors={status['wait_errors']}",
            )

        return False, "visible_page_not_found"


    async def _open_more_popup(
        self,
        page: Page,
        *,
        more_index: int,
    ) -> tuple[bool, Optional[str], str]:
        """
        Open Semi UI 'More' popup robustly.

        Tries:
        - normal click
        - hover
        - focus
        - Enter
        - Space
        - JS click
        Then waits for popup root with id=data-popupid / aria-describedby
        OR a visible floating container that contains numeric page items.
        """
        more_loc = page.locator("li.semi-page-item[aria-label='More']").nth(more_index)

        try:
            if await more_loc.count() == 0:
                return False, None, f"more_button_missing[{more_index}]"
        except Exception as e:
            return False, None, f"more_button_count_failed[{more_index}]:{str(e)[:120]}"

        popup_id = None
        try:
            popup_id = await more_loc.get_attribute("data-popupid")
        except Exception:
            popup_id = None

        if not popup_id:
            try:
                popup_id = await more_loc.get_attribute("aria-describedby")
            except Exception:
                popup_id = None

        if not popup_id:
            return False, None, f"popup_id_missing[{more_index}]"

        # Try multiple open strategies
        open_attempt_errors = []

        try:
            await more_loc.scroll_into_view_if_needed(timeout=3000)
        except Exception as e:
            open_attempt_errors.append(f"scroll:{str(e)[:120]}")

        # 1) normal click
        clicked, detail = await self._safe_click(page, more_loc)
        if not clicked:
            open_attempt_errors.append(f"click:{detail}")

        # 2) hover
        try:
            await more_loc.hover(timeout=2000)
        except Exception as e:
            open_attempt_errors.append(f"hover:{str(e)[:120]}")

        # 3) focus
        try:
            await more_loc.focus()
        except Exception as e:
            open_attempt_errors.append(f"focus:{str(e)[:120]}")

        # 4) Enter
        try:
            await more_loc.press("Enter", timeout=2000)
        except Exception as e:
            open_attempt_errors.append(f"enter:{str(e)[:120]}")

        # 5) Space
        try:
            await more_loc.press(" ", timeout=2000)
        except Exception as e:
            open_attempt_errors.append(f"space:{str(e)[:120]}")

        # 6) JS click directly on the trigger
        try:
            h = await more_loc.element_handle()
            if h:
                try:
                    await page.evaluate(
                        """
                        (el) => {
                        if (!el) return false;
                        try { el.click(); } catch (_) {}
                        const opts = { bubbles: true, cancelable: true, view: window };
                        try { el.dispatchEvent(new MouseEvent("mousedown", opts)); } catch (_) {}
                        try { el.dispatchEvent(new MouseEvent("mouseup", opts)); } catch (_) {}
                        try { el.dispatchEvent(new MouseEvent("click", opts)); } catch (_) {}
                        return true;
                        }
                        """,
                        h,
                    )
                finally:
                    try:
                        await h.dispose()
                    except Exception:
                        pass
        except Exception as e:
            open_attempt_errors.append(f"js:{str(e)[:120]}")

        # Wait for popup by exact id OR visible floating popup containing numbers
        try:
            await page.wait_for_function(
                """
                (pid) => {
                function isVisible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                        s.display !== "none" &&
                        s.visibility !== "hidden" &&
                        s.opacity !== "0";
                }

                // 1) exact popup id
                const byId = document.getElementById(pid);
                if (isVisible(byId)) return true;

                // 2) common Semi portal/popover wrappers
                const portalCandidates = Array.from(document.querySelectorAll(
                    [
                    '[id="' + pid + '"]',
                    '.semi-portal',
                    '.semi-popover',
                    '.semi-dropdown',
                    '.semi-tooltip',
                    '.semi-popper',
                    '[role="tooltip"]'
                    ].join(',')
                ));

                for (const root of portalCandidates) {
                    if (!isVisible(root)) continue;
                    const txt = (root.innerText || root.textContent || "").trim();
                    // popup should contain several numeric page items
                    const nums = txt.match(/\\b\\d+\\b/g) || [];
                    if (nums.length >= 3) return true;
                }

                return false;
                }
                """,
                arg=popup_id,
                timeout=5000,
            )
            return True, popup_id, "popup_opened"
        except Exception as e:
            return False, popup_id, (
                f"popup_not_visible[{more_index}]:{str(e)[:120]} "
                f"open_attempts={open_attempt_errors}"
            )
        


    async def _get_popup_page_numbers(self, page: Page, popup_id: str) -> list[int]:
        try:
            nums = await page.evaluate(
                """
                (pid) => {
                const INTERACTIVE =
                    'li,button,a,[role="option"],[role="menuitem"],[role="button"],' +
                    '.semi-dropdown-item,.semi-select-option,.semi-page-item';

                function textOf(el) {
                    return (el?.innerText || el?.textContent || "")
                    .replace(/\\s+/g, " ")
                    .trim();
                }

                function isVisible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return (
                    r.width > 0 &&
                    r.height > 0 &&
                    s.display !== "none" &&
                    s.visibility !== "hidden" &&
                    s.opacity !== "0"
                    );
                }

                function getRoot(pid) {
                    let root = document.getElementById(pid);
                    if (root && isVisible(root)) return root;

                    const candidates = Array.from(
                    document.querySelectorAll(
                        '.semi-portal, .semi-popover, .semi-dropdown, .semi-tooltip, .semi-popper, [role="tooltip"]'
                    )
                    ).filter(isVisible);

                    let best = null;
                    let bestCount = -1;

                    for (const el of candidates) {
                    const items = Array.from(el.querySelectorAll(INTERACTIVE)).filter(isVisible);
                    let count = 0;
                    for (const item of items) {
                        const txt = textOf(item);
                        const aria = (item.getAttribute("aria-label") || "").trim();
                        if (/^\\d+$/.test(txt) || /^Page\\s+\\d+$/i.test(aria)) {
                        count += 1;
                        }
                    }

                    if (count > bestCount) {
                        best = el;
                        bestCount = count;
                    }
                    }

                    return best;
                }

                const root = getRoot(pid);
                if (!root) return [];

                const out = new Set();
                const items = Array.from(root.querySelectorAll(INTERACTIVE)).filter(isVisible);

                for (const el of items) {
                    const txt = textOf(el);
                    const aria = (el.getAttribute("aria-label") || "").trim();

                    if (/^\\d+$/.test(txt)) {
                    out.add(Number(txt));
                    continue;
                    }

                    const m = aria.match(/^Page\\s+(\\d+)$/i);
                    if (m) {
                    out.add(Number(m[1]));
                    }
                }

                return Array.from(out).sort((a, b) => a - b);
                }
                """,
                popup_id,
            )
            if isinstance(nums, list):
                return [
                    int(x)
                    for x in nums
                    if isinstance(x, int) or (isinstance(x, float) and x.is_integer())
                ]
        except Exception:
            pass
        return []


    async def _click_popup_number(
        self,
        page: Page,
        *,
        popup_id: str,
        page_num: int,
        verify_selector: str,
        settle_ms: int,
        job: JobEnvelope,
        pre_click_ms: int = 3000,
        post_click_ms: int = 3500,
        ready_timeout_ms: int = 15000,
        scroll_direction: str = "down",
    ) -> tuple[bool, str]:
        before_fp = await self._table_fingerprint(page, verify_selector)

        if pre_click_ms > 0:
            await asyncio.sleep(pre_click_ms / 1000.0)

        try:
            clicked = await page.evaluate(
                """
                async (arg) => {
                  const INTERACTIVE =
                    'li,button,a,[role="option"],[role="menuitem"],[role="button"],' +
                    '.semi-dropdown-item,.semi-select-option,.semi-page-item';

                  function textOf(el) {
                    return (el?.innerText || el?.textContent || "")
                      .replace(/\\s+/g, " ")
                      .trim();
                  }

                  function isVisible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return (
                      r.width > 0 &&
                      r.height > 0 &&
                      s.display !== "none" &&
                      s.visibility !== "hidden" &&
                      s.opacity !== "0"
                    );
                  }

                  function getRoot(pid) {
                    let root = document.getElementById(pid);
                    if (root && isVisible(root)) return root;

                    const candidates = Array.from(
                      document.querySelectorAll(
                        '.semi-portal, .semi-popover, .semi-dropdown, .semi-tooltip, .semi-popper, [role="tooltip"]'
                      )
                    ).filter(isVisible);

                    let best = null;
                    let bestCount = -1;
                    for (const el of candidates) {
                      const nums = (textOf(el).match(/\\b\\d+\\b/g) || []).length;
                      if (nums.length > bestCount) {
                        best = el;
                        bestCount = nums.length;
                      }
                    }
                    return best;
                  }

                  function getScroller(root) {
                    const nodes = [root, ...Array.from(root.querySelectorAll("*"))];
                    for (const n of nodes) {
                      const s = getComputedStyle(n);
                      if (
                        /(auto|scroll)/.test(s.overflowY || "") &&
                        n.scrollHeight > n.clientHeight + 4
                      ) {
                        return n;
                      }
                    }
                    return root;
                  }

                  function interactiveItems(root) {
                    return Array.from(root.querySelectorAll(INTERACTIVE)).filter(isVisible);
                  }

                  function findTarget(root, pageNum) {
                    const targetText = String(pageNum);
                    const items = interactiveItems(root);

                    // exact interactive match
                    let target = items.find(el => {
                      const txt = textOf(el);
                      const aria = (el.getAttribute("aria-label") || "").trim();
                      return txt === targetText || aria === `Page ${targetText}`;
                    });
                    if (target) return target;

                    // fallback: descendant exact text -> closest clickable ancestor
                    const descendants = Array.from(root.querySelectorAll("*")).filter(isVisible);
                    const raw = descendants.find(el => {
                      const txt = textOf(el);
                      const aria = (el.getAttribute && el.getAttribute("aria-label")) || "";
                      return txt === targetText || aria.trim() === `Page ${targetText}`;
                    });
                    if (!raw) return null;

                    return raw.closest(INTERACTIVE) || raw;
                  }

                  function clickElement(el) {
                    const clickable = el.closest(INTERACTIVE) || el;

                    try {
                      clickable.scrollIntoView({ block: "nearest", inline: "nearest" });
                    } catch (_) {}

                    try {
                      clickable.click();
                      return {
                        ok: true,
                        reason: "dom_click",
                        tag: clickable.tagName,
                        cls: clickable.className || ""
                      };
                    } catch (_) {}

                    try {
                      const opts = { bubbles: true, cancelable: true, view: window };
                      clickable.dispatchEvent(new MouseEvent("mousedown", opts));
                      clickable.dispatchEvent(new MouseEvent("mouseup", opts));
                      clickable.dispatchEvent(new MouseEvent("click", opts));
                      return {
                        ok: true,
                        reason: "dispatch_click",
                        tag: clickable.tagName,
                        cls: clickable.className || ""
                      };
                    } catch (_) {}

                    return { ok: false, reason: "click_failed" };
                  }

                  function visibleNums(root) {
                    const nums = [];
                    for (const el of interactiveItems(root)) {
                      const t = textOf(el);
                      if (/^\\d+$/.test(t)) nums.push(Number(t));
                    }
                    return Array.from(new Set(nums)).sort((a, b) => a - b);
                  }

                  async function wait(ms) {
                    await new Promise(r => setTimeout(r, ms));
                  }

                  async function smoothScrollBy(scroller, delta, durationMs = 180) {
                    const start = scroller.scrollTop;
                    const target = start + delta;

                    await new Promise(resolve => {
                      const t0 = performance.now();

                      function tick(now) {
                        const p = Math.min(1, (now - t0) / durationMs);
                        scroller.scrollTop = start + (target - start) * p;
                        if (p < 1) {
                          requestAnimationFrame(tick);
                        } else {
                          resolve();
                        }
                      }

                      requestAnimationFrame(tick);
                    });
                  }

                  const root = getRoot(arg.popupId);
                  if (!root) return { ok: false, reason: "popup_missing" };

                  const scroller = getScroller(root);
                  const direction = arg.scrollDirection === "up" ? -1 : 1;

                  // Much smaller step than before
                  const step = Math.max(
                    36,
                    Math.min(72, Math.floor((scroller.clientHeight || 240) * 0.25))
                  );

                  const maxRounds = 80;
                  let stagnantRounds = 0;
                  let lastSeen = [];
                  let previousTop = scroller.scrollTop;

                  // Try current viewport before any scrolling
                  let target = findTarget(root, arg.pageNum);
                  if (target) {
                    const res = clickElement(target);
                    return { ...res, seen: visibleNums(root), attempts: 0 };
                  }

                  for (let round = 1; round <= maxRounds; round++) {
                    lastSeen = visibleNums(root);

                    await smoothScrollBy(scroller, direction * step, 160);
                    await wait(120); // let virtualized popup mount new items

                    const nowTop = scroller.scrollTop;
                    if (Math.abs(nowTop - previousTop) < 1) {
                      stagnantRounds += 1;
                    } else {
                      stagnantRounds = 0;
                    }
                    previousTop = nowTop;

                    target = findTarget(root, arg.pageNum);
                    if (target) {
                      const res = clickElement(target);
                      return {
                        ...res,
                        seen: visibleNums(root),
                        attempts: round
                      };
                    }

                    if (stagnantRounds >= 3) {
                      return {
                        ok: false,
                        reason: "popup_scroll_stagnated",
                        seen: lastSeen,
                        attempts: round
                      };
                    }
                  }

                  return {
                    ok: false,
                    reason: "popup_page_not_found_after_smooth_scroll",
                    seen: lastSeen,
                    attempts: maxRounds
                  };
                }
                """,
                {
                    "popupId": popup_id,
                    "pageNum": page_num,
                    "scrollDirection": scroll_direction,
                },
            )
        except Exception as e:
            return False, f"popup_click_eval_failed:{str(e)[:120]}"

        if not clicked or not clicked.get("ok"):
            return False, (
                clicked.get("reason", "popup_click_failed") if isinstance(clicked, dict)
                else "popup_click_failed"
            )

        status = await self._wait_for_page_transition(
            page,
            expected_page=page_num,
            before_fp=before_fp,
            verify_selector=verify_selector,
            settle_ms=settle_ms,
        )
        ready = await self._wait_for_table_ready(
            page,
            verify_selector=verify_selector,
            timeout_ms=ready_timeout_ms,
            extra_sleep_ms=post_click_ms,
        )
        has_rows_after = await self._selector_has_rows(page, verify_selector, max_rows=5)

        ok = False
        reason = None
        if status["after_active"] == page_num and (has_rows_after or ready["rows_seen"]):
            ok = True
            reason = "active_page_match_with_rows"
        elif status["after_active"] == 0 and status["fp_changed"] and (has_rows_after or ready["rows_seen"]):
            ok = True
            reason = "rows_present_after_transition"
        elif status["after_active"] == page_num and ready["container_seen"]:
            ok = True
            reason = "active_page_match_with_container_ready"

        if ok:
            logger.bind(ctx=log_ctx(job)).info(
                "table_pagination_success page_num={} method=more_popup "
                "reason={} after_active={} fp_changed={} has_rows_after={} ready={} ready_rows={}",
                page_num,
                reason,
                status["after_active"],
                status["fp_changed"],
                has_rows_after,
                ready["ready"],
                ready["rows_seen"],
            )
            return True, "popup_click_success"

        return (
            False,
            "popup_click_unconfirmed "
            f"after_active={status['after_active']} "
            f"fp_changed={status['fp_changed']} "
            f"has_rows_after={has_rows_after} "
            f"ready={ready['ready']} "
            f"ready_rows={ready['rows_seen']} "
            f"ready_errors={ready['errors']} "
            f"wait_errors={status['wait_errors']}"
        )







    def _choose_more_index_for_target(self, state: dict, target_page: int) -> int:
        visible = state.get("visible_pages") or []
        more_count = int(state.get("more_count") or 0)
        current = int(state.get("current") or 0)

        if more_count <= 1:
            return 0

        if visible:
            min_v = min(visible)
            max_v = max(visible)

            if target_page < min_v:
                return 0
            if target_page > max_v:
                return more_count - 1

        if current and target_page < current:
            return 0
        return more_count - 1



    async def _jump_via_more_popup(
        self,
        page: Page,
        *,
        target_page: int,
        state: dict,
        verify_selector: str,
        settle_ms: int,
        job: JobEnvelope,
        pre_click_ms: int = 6000,
        post_click_ms: int = 6500,
        ready_timeout_ms: int = 15000,
    ) -> tuple[bool, Optional[int], str]:
        more_count = int(state.get("more_count") or 0)
        if more_count <= 0:
            return False, None, "no_more_button"

        more_index = self._choose_more_index_for_target(state, target_page)

        ok, popup_id, detail = await self._open_more_popup(page, more_index=more_index)
        if not ok or not popup_id:
            return False, None, detail

        current = int(state.get("current") or 0)
        scroll_direction = "up" if current and target_page < current else "down"

        # First: try exact target directly inside scrollable popup
        ok, click_detail = await self._click_popup_number(
            page,
            popup_id=popup_id,
            page_num=target_page,
            verify_selector=verify_selector,
            settle_ms=settle_ms,
            job=job,
            pre_click_ms=pre_click_ms,
            post_click_ms=post_click_ms,
            ready_timeout_ms=ready_timeout_ms,
            scroll_direction=scroll_direction,
        )
        if ok:
            return True, target_page, f"popup_selected={target_page}"

        # Fallback: choose visible extreme
        popup_nums = await self._get_popup_page_numbers(page, popup_id)
        if not popup_nums:
            return False, None, f"popup_has_no_numbers target_click_failed={click_detail}"

        chosen = min(popup_nums) if (current and target_page < current) else max(popup_nums)

        ok, click_detail = await self._click_popup_number(
            page,
            popup_id=popup_id,
            page_num=chosen,
            verify_selector=verify_selector,
            settle_ms=settle_ms,
            job=job,
            pre_click_ms=pre_click_ms,
            post_click_ms=post_click_ms,
            ready_timeout_ms=ready_timeout_ms,
            scroll_direction=scroll_direction,
        )
        if not ok:
            return False, None, click_detail

        return True, chosen, f"popup_selected={chosen}"
    



    async def _click_next_page(
        self,
        page: Page,
        *,
        expected_page: int,
        verify_selector: str,
        settle_ms: int,
        job: JobEnvelope,
        pre_click_ms: int = 2500,
        post_click_ms: int = 5500,
        ready_timeout_ms: int = 6500,
    ) -> tuple[bool, str]:
        next_btn_candidates = [
            page.locator("li.semi-page-item[aria-label='Next']").first,
            page.locator("li.semi-page-next").first,
            page.locator("[aria-label='Next']").first,
            page.locator("button[aria-label='Next']").first,
            page.locator("text='Next'").first,
            page.locator("text='›'").first,
            page.locator("text='>'").first,
        ]

        next_btn = None
        idx_used = None
        for idx, cand in enumerate(next_btn_candidates, start=1):
            try:
                if await cand.count() > 0:
                    next_btn = cand
                    idx_used = idx
                    break
            except Exception:
                continue

        if next_btn is None:
            return False, "next_button_missing"

        try:
            aria_disabled = (await next_btn.get_attribute("aria-disabled")) or "false"
            disabled_attr = await next_btn.get_attribute("disabled")
            if aria_disabled.lower() == "true" or disabled_attr is not None:
                return False, f"next_button_disabled[{idx_used}]"
        except Exception as e:
            return False, f"next_button_attr_failed[{idx_used}]:{str(e)[:120]}"

        if pre_click_ms > 0:
            await asyncio.sleep(pre_click_ms / 1000.0)

        before_fp = await self._table_fingerprint(page, verify_selector)
        clicked, click_detail = await self._safe_click(page, next_btn)
        if not clicked:
            return False, f"next_click_failed[{idx_used}]:{click_detail}"

        status = await self._wait_for_page_transition(
            page,
            expected_page=expected_page,
            before_fp=before_fp,
            verify_selector=verify_selector,
            settle_ms=settle_ms,
        )
        ready = await self._wait_for_table_ready(
            page,
            verify_selector=verify_selector,
            timeout_ms=ready_timeout_ms,
            extra_sleep_ms=post_click_ms,
        )
        has_rows_after = await self._selector_has_rows(page, verify_selector, max_rows=5)

        ok = False
        reason = None
        if status["after_active"] == expected_page and (has_rows_after or ready["rows_seen"]):
            ok = True
            reason = "active_page_match_with_rows"
        elif status["after_active"] == 0 and status["fp_changed"] and (has_rows_after or ready["rows_seen"]):
            ok = True
            reason = "rows_present_after_transition"
        elif status["after_active"] == expected_page and ready["container_seen"]:
            ok = True
            reason = "active_page_match_with_container_ready"

        if ok:
            logger.bind(ctx=log_ctx(job)).info(
                "table_pagination_success page_num={} method=next_click verify_selector={} "
                "reason={} after_active={} fp_changed={} has_rows_after={} ready={} ready_rows={} "
                "before_fp_len={} after_fp_len={}",
                expected_page,
                verify_selector,
                reason,
                status["after_active"],
                status["fp_changed"],
                has_rows_after,
                ready["ready"],
                ready["rows_seen"],
                status["before_fp_len"],
                status["after_fp_len"],
            )
            return True, "next_click_success"

        return (
            False,
            "next_click_unconfirmed "
            f"after_active={status['after_active']} "
            f"fp_changed={status['fp_changed']} "
            f"has_rows_after={has_rows_after} "
            f"ready={ready['ready']} "
            f"ready_rows={ready['rows_seen']} "
            f"ready_errors={ready['errors']} "
            f"wait_errors={status['wait_errors']}",
        )


    async def _click_prev_page(
        self,
        page: Page,
        *,
        expected_page: int,
        verify_selector: str,
        settle_ms: int,
        job: JobEnvelope,
        pre_click_ms: int = 6000,
        post_click_ms: int = 6500,
        ready_timeout_ms: int = 15000,
    ) -> tuple[bool, str]:
        prev_btn_candidates = [
            page.locator("li.semi-page-item[aria-label='Previous']").first,
            page.locator("li.semi-page-prev").first,
            page.locator("[aria-label='Previous']").first,
            page.locator("button[aria-label='Previous']").first,
            page.locator("text='Previous'").first,
            page.locator("text='‹'").first,
            page.locator("text='<'").first,
        ]

        prev_btn = None
        idx_used = None
        for idx, cand in enumerate(prev_btn_candidates, start=1):
            try:
                if await cand.count() > 0:
                    prev_btn = cand
                    idx_used = idx
                    break
            except Exception:
                continue

        if prev_btn is None:
            return False, "prev_button_missing"

        try:
            aria_disabled = (await prev_btn.get_attribute("aria-disabled")) or "false"
            disabled_attr = await prev_btn.get_attribute("disabled")
            if aria_disabled.lower() == "true" or disabled_attr is not None:
                return False, f"prev_button_disabled[{idx_used}]"
        except Exception as e:
            return False, f"prev_button_attr_failed[{idx_used}]:{str(e)[:120]}"

        if pre_click_ms > 0:
            await asyncio.sleep(pre_click_ms / 1000.0)

        before_fp = await self._table_fingerprint(page, verify_selector)
        clicked, click_detail = await self._safe_click(page, prev_btn)
        if not clicked:
            return False, f"prev_click_failed[{idx_used}]:{click_detail}"

        status = await self._wait_for_page_transition(
            page,
            expected_page=expected_page,
            before_fp=before_fp,
            verify_selector=verify_selector,
            settle_ms=settle_ms,
        )
        ready = await self._wait_for_table_ready(
            page,
            verify_selector=verify_selector,
            timeout_ms=ready_timeout_ms,
            extra_sleep_ms=post_click_ms,
        )
        has_rows_after = await self._selector_has_rows(page, verify_selector, max_rows=5)

        ok = False
        reason = None
        if status["after_active"] == expected_page and (has_rows_after or ready["rows_seen"]):
            ok = True
            reason = "active_page_match_with_rows"
        elif status["after_active"] == 0 and status["fp_changed"] and (has_rows_after or ready["rows_seen"]):
            ok = True
            reason = "rows_present_after_transition"
        elif status["after_active"] == expected_page and ready["container_seen"]:
            ok = True
            reason = "active_page_match_with_container_ready"

        if ok:
            logger.bind(ctx=log_ctx(job)).info(
                "table_pagination_success page_num={} method=prev_click verify_selector={} "
                "reason={} after_active={} fp_changed={} has_rows_after={} ready={} ready_rows={} "
                "before_fp_len={} after_fp_len={}",
                expected_page,
                verify_selector,
                reason,
                status["after_active"],
                status["fp_changed"],
                has_rows_after,
                ready["ready"],
                ready["rows_seen"],
                status["before_fp_len"],
                status["after_fp_len"],
            )
            return True, "prev_click_success"

        return (
            False,
            "prev_click_unconfirmed "
            f"after_active={status['after_active']} "
            f"fp_changed={status['fp_changed']} "
            f"has_rows_after={has_rows_after} "
            f"ready={ready['ready']} "
            f"ready_rows={ready['rows_seen']} "
            f"ready_errors={ready['errors']} "
            f"wait_errors={status['wait_errors']}",
        )




    async def _click_page_num(
        self,
        page: Page,
        page_num: int,
        *,
        settle_ms: int,
        job: JobEnvelope,
        verify_selector: Optional[str] = None,
        retries: int = 3,
        allow_step_fallback: bool = True,
        pre_click_ms: int = 6000,
        post_click_ms: int = 6500,
        ready_timeout_ms: int = 15000,
    ) -> bool:
        """
        Jump to arbitrary page:
        - near jumps: step-only (much more stable)
        - visible direct click if page is shown
        - otherwise More popup
        - optional step fallback
        """
        target_page = max(1, int(page_num or 1))
        verify_selector = (verify_selector or ".semi-table").strip()

        current = await self._get_current_page_num(page)
        if current == target_page:
            logger.bind(ctx=log_ctx(job)).info(
                "table_pagination_already_on_target page_num={} verify_selector={}",
                target_page,
                verify_selector,
            )
            return True

        if current > 0 and abs(target_page - current) <= 3:
            logger.bind(ctx=log_ctx(job)).info(
                "table_pagination_near_jump current={} target={} method=step_only verify_selector={}",
                current,
                target_page,
                verify_selector,
            )

            while current < target_page:
                ok, detail = await self._click_next_page(
                    page,
                    expected_page=current + 1,
                    verify_selector=verify_selector,
                    settle_ms=settle_ms,
                    job=job,
                    pre_click_ms=pre_click_ms,
                    post_click_ms=post_click_ms,
                    ready_timeout_ms=ready_timeout_ms,
                )
                if not ok:
                    logger.bind(ctx=log_ctx(job)).warning(
                        "table_pagination_near_jump_failed current={} target={} direction=next detail={}",
                        current,
                        target_page,
                        detail,
                    )
                    return False
                current += 1

            while current > target_page:
                ok, detail = await self._click_prev_page(
                    page,
                    expected_page=current - 1,
                    verify_selector=verify_selector,
                    settle_ms=settle_ms,
                    job=job,
                    pre_click_ms=pre_click_ms,
                    post_click_ms=post_click_ms,
                    ready_timeout_ms=ready_timeout_ms,
                )
                if not ok:
                    logger.bind(ctx=log_ctx(job)).warning(
                        "table_pagination_near_jump_failed current={} target={} direction=prev detail={}",
                        current,
                        target_page,
                        detail,
                    )
                    return False
                current -= 1

            return True

        failure_reasons: list[str] = []
        max_hops = max(8, retries * 4)

        for hop in range(1, max_hops + 1):
            state = await self._get_pagination_state(page)
            current = int(state.get("current") or 0)
            visible_pages = list(state.get("visible_pages") or [])

            if current == target_page:
                logger.bind(ctx=log_ctx(job)).info(
                    "table_pagination_success page_num={} hop={} method=already_active verify_selector={}",
                    target_page,
                    hop,
                    verify_selector,
                )
                return True

            hop_reasons: list[str] = []

            if target_page in visible_pages:
                ok, detail = await self._click_visible_page(
                    page,
                    target_page,
                    verify_selector=verify_selector,
                    settle_ms=settle_ms,
                    job=job,
                    pre_click_ms=pre_click_ms,
                    post_click_ms=post_click_ms,
                    ready_timeout_ms=ready_timeout_ms,
                )
                if ok:
                    return True
                hop_reasons.append(detail)
            else:
                ok, chosen_page, detail = await self._jump_via_more_popup(
                    page,
                    target_page=target_page,
                    state=state,
                    verify_selector=verify_selector,
                    settle_ms=settle_ms,
                    job=job,
                    pre_click_ms=pre_click_ms,
                    post_click_ms=post_click_ms,
                    ready_timeout_ms=ready_timeout_ms,
                )
                if ok:
                    if chosen_page == target_page:
                        return True

                    new_current = await self._get_current_page_num(page)
                    logger.bind(ctx=log_ctx(job)).info(
                        "table_pagination_progress target_page={} hop={} chosen_page={} current_after={} verify_selector={}",
                        target_page,
                        hop,
                        chosen_page,
                        new_current,
                        verify_selector,
                    )
                    continue
                hop_reasons.append(detail)

            if allow_step_fallback:
                if current and target_page > current and state.get("has_next"):
                    ok, detail = await self._click_next_page(
                        page,
                        expected_page=current + 1,
                        verify_selector=verify_selector,
                        settle_ms=settle_ms,
                        job=job,
                        pre_click_ms=pre_click_ms,
                        post_click_ms=post_click_ms,
                        ready_timeout_ms=ready_timeout_ms,
                    )
                    if ok:
                        continue
                    hop_reasons.append(detail)

                elif current and target_page < current and state.get("has_prev"):
                    ok, detail = await self._click_prev_page(
                        page,
                        expected_page=max(1, current - 1),
                        verify_selector=verify_selector,
                        settle_ms=settle_ms,
                        job=job,
                        pre_click_ms=pre_click_ms,
                        post_click_ms=post_click_ms,
                        ready_timeout_ms=ready_timeout_ms,
                    )
                    if ok:
                        continue
                    hop_reasons.append(detail)

                else:
                    hop_reasons.append(
                        f"no_route current={current} visible_pages={visible_pages} more_count={state.get('more_count')}"
                    )
            else:
                hop_reasons.append(
                    f"jump_only_mode_failed current={current} visible_pages={visible_pages} more_count={state.get('more_count')}"
                )

            failure_reasons.append(f"hop={hop} reasons={hop_reasons}")

            if hop < max_hops:
                await asyncio.sleep(0.2)

        logger.bind(ctx=log_ctx(job)).warning(
            "table_pagination_click_failed page_num={} verify_selector={} reasons={}",
            target_page,
            verify_selector,
            " | ".join(failure_reasons),
        )
        return False





    async def _ensure_page_range(
        self,
        page: Page,
        start_count: int,
        end_count: int,
        *,
        settle_ms: int,
        job: JobEnvelope,
        verify_selector: Optional[str] = None,
        pre_click_ms: int = 6000,
        post_click_ms: int = 6500,
        ready_timeout_ms: int = 15000,
    ) -> tuple[int, int]:
        start = max(1, int(start_count or 1))
        end = max(start, int(end_count or start))

        if start == 1:
            return start, end

        ok = await self._click_page_num(
            page,
            start,
            settle_ms=settle_ms,
            job=job,
            verify_selector=verify_selector,
            retries=3,
            allow_step_fallback=False,
            pre_click_ms=pre_click_ms,
            post_click_ms=post_click_ms,
            ready_timeout_ms=ready_timeout_ms,
        )

        if not ok:
            logger.bind(ctx=log_ctx(job)).warning(
                "table_start_page_jump_failed start_page={} end_page={} verify_selector={} action=abort_module",
                start,
                end,
                verify_selector or ".semi-table",
            )
            raise RuntimeError(f"failed_to_jump_to_start_page_{start}")

        return start, end



    async def _extract_selector_artifact(
        self,
        *,
        job: JobEnvelope,
        page: Page,
        sel: str,
        max_rows: int,
        max_scrolls: int,
        scroll_delta: int,
        settle_ms: int,
        selector_source: str,
        page_no: int,
    ) -> tuple[Optional[Artifact], dict]:
        info = {
            "mode": None,
            "rows": 0,
            "headers": 0,
            "scrolls": 0,
            "selector": sel,
            "source": selector_source,
            "page_no": page_no,
        }

        h = None
        try:
            if _is_xpath(sel):
                h = await _locator_for(page, sel).first.element_handle()

            if h:
                extracted = await page.evaluate(
                    _JS_EXTRACT_TABLE_EL,
                    {"el": h, "maxRows": max_rows},
                )
            else:
                extracted = await page.evaluate(
                    _JS_EXTRACT_TABLE,
                    {"selector": sel, "maxRows": max_rows},
                )
        finally:
            if h:
                try:
                    await h.dispose()
                except Exception:
                    pass

        if extracted:
            headers = extracted.get("headers") or []
            rows = extracted.get("rows") or []

            if rows:
                info.update(
                    {
                        "mode": "html",
                        "rows": len(rows),
                        "headers": len(headers),
                    }
                )
                return (
                    Artifact(
                        kind=ArtifactKind.table,
                        label="table",
                        source_url=str(job.plan.url),
                        selector=sel,
                        data={
                            "type": "table",
                            "headers": headers,
                            "rows": rows,
                        },
                        meta={
                            "virtualized": False,
                            "source": selector_source,
                            "page_no": page_no,
                        },
                    ),
                    info,
                )

            logger.bind(ctx=log_ctx(job)).info(
                "table_empty_html_skipped selector={} headers={} source={} page_no={}",
                sel,
                len(headers),
                selector_source,
                page_no,
            )

        merged: List[List[str]] = []
        seen_row_keys: set[str] = set()
        no_new_rounds = 0
        scroll_count = 0
        h_grid = None

        try:
            if _is_xpath(sel):
                h_grid = await _locator_for(page, sel).first.element_handle()

            for _ in range(max_scrolls):
                scroll_count += 1

                if h_grid:
                    vis = await page.evaluate(
                        _JS_EXTRACT_VISIBLE_GRID_EL,
                        {"el": h_grid, "maxRows": max_rows},
                    )
                else:
                    vis = await page.evaluate(
                        _JS_EXTRACT_VISIBLE_GRID,
                        {"selector": sel, "maxRows": max_rows},
                    )

                rows = (vis.get("rows") if isinstance(vis, dict) else None) or []

                new_rows = 0
                for r in rows:
                    key = "|".join((x or "") for x in r)[:500]
                    if not key or key in seen_row_keys:
                        continue
                    seen_row_keys.add(key)
                    merged.append(r)
                    new_rows += 1
                    if len(merged) >= max_rows:
                        break

                if new_rows == 0:
                    no_new_rounds += 1
                else:
                    no_new_rounds = 0

                if len(merged) >= max_rows or no_new_rounds >= 3:
                    break

                if h_grid:
                    await page.evaluate(
                        _JS_SCROLL_CONTAINER_EL,
                        {"el": h_grid, "delta": scroll_delta},
                    )
                else:
                    await page.evaluate(
                        _JS_SCROLL_CONTAINER,
                        {"selector": sel, "delta": scroll_delta},
                    )

                if settle_ms > 0:
                    await asyncio.sleep(min(0.8, float(settle_ms) / 1000.0))
        finally:
            if h_grid:
                try:
                    await h_grid.dispose()
                except Exception:
                    pass

        if merged:
            info.update(
                {
                    "mode": "grid",
                    "rows": len(merged[:max_rows]),
                    "headers": 0,
                    "scrolls": scroll_count,
                }
            )
            return (
                Artifact(
                    kind=ArtifactKind.table,
                    label="grid_table",
                    source_url=str(job.plan.url),
                    selector=sel,
                    data={
                        "type": "grid",
                        "headers": [],
                        "rows": merged[:max_rows],
                    },
                    meta={
                        "virtualized": True,
                        "max_scrolls": max_scrolls,
                        "actual_scrolls": scroll_count,
                        "source": selector_source,
                        "page_no": page_no,
                    },
                ),
                info,
            )

        logger.bind(ctx=log_ctx(job)).debug(
            "table_no_rows_for_selector selector={} source={} page_no={} scrolls={}",
            sel,
            selector_source,
            page_no,
            scroll_count,
        )
        return None, info

    async def _detect_table_selectors(self, page: Page, max_tables: int, job: JobEnvelope) -> list[str]:
        logger.bind(ctx=log_ctx(job)).info(
            "table_detection_starting max_tables={}",
            max_tables,
        )

        js = """
        (maxTables) => {
            const selectors = [];
            const seen = new Set();
            const stats = {
                html_tables_checked: 0, html_tables_found: 0,
                aria_tables_found: 0, ag_grids_found: 0,
                react_grids_found: 0, mui_tables_found: 0,
                bootstrap_tables_found: 0, antd_tables_found: 0,
                vuetify_tables_found: 0, kendo_grids_found: 0,
                devexpress_grids_found: 0, datatables_found: 0,
                virtualized_found: 0, div_tables_found: 0,
                flex_div_tables_found: 0
            };

            function addSelector(sel, reason) {
                if (!sel || seen.has(sel)) return;
                seen.add(sel);
                selectors.push({ selector: sel, reason: reason });
            }

            function getStableSelector(el) {
                if (!el) return null;

                if (el.id) return '#' + el.id;

                const testId = el.getAttribute('data-testid');
                if (testId) return `[data-testid="${testId}"]`;
                const dataCy = el.getAttribute('data-cy');
                if (dataCy) return `[data-cy="${dataCy}"]`;

                if (el.className && typeof el.className === 'string') {
                    const classes = el.className.split(' ').filter(c =>
                        c && !c.includes(':') && c.length > 2
                    );
                    if (classes.length > 0 && classes.length <= 3) {
                        const classSel = '.' + classes.join('.');
                        try {
                            const matches = document.querySelectorAll(classSel);
                            if (matches.length === 1 || matches[0] === el) {
                                return classSel;
                            }
                        } catch (e) {}
                    }
                }

                const path = [];
                let curr = el;
                let depth = 0;
                while (curr && curr !== document.body && depth < 5) {
                    let part = curr.tagName.toLowerCase();
                    if (curr.id) {
                        part = '#' + curr.id;
                        path.unshift(part);
                        break;
                    }
                    if (curr.className && typeof curr.className === 'string') {
                        const classes = curr.className.split(' ').filter(c =>
                            c && !c.includes(':') && c.length > 2
                        ).slice(0, 2);
                        if (classes.length > 0) {
                            part += '.' + classes.join('.');
                        }
                    }
                    const parent = curr.parentElement;
                    if (parent) {
                        const siblings = Array.from(parent.children).filter(
                            s => s.tagName === curr.tagName
                        );
                        if (siblings.length > 1) {
                            const index = siblings.indexOf(curr) + 1;
                            part += `:nth-of-type(${index})`;
                        }
                    }
                    path.unshift(part);
                    curr = parent;
                    depth++;
                }

                return path.length > 0 ? path.join(' > ') : null;
            }

            const tables = Array.from(document.querySelectorAll('table'));
            stats.html_tables_checked = tables.length;
            for (const table of tables) {
                const rows = table.querySelectorAll('tr');
                if (rows.length < 2) continue;
                const cells = table.querySelectorAll('td, th');
                if (cells.length < 4) continue;

                const sel = getStableSelector(table);
                if (sel) {
                    addSelector(sel, 'html_table');
                    stats.html_tables_found++;
                }
            }

            const ariaTables = Array.from(document.querySelectorAll('[role="table"], [role="grid"]'));
            for (const el of ariaTables) {
                const hasRows = el.querySelectorAll('[role="row"], [role="rowgroup"] > *').length > 0;
                if (!hasRows) continue;

                const sel = getStableSelector(el);
                if (sel) {
                    addSelector(sel, 'aria_table');
                    stats.aria_tables_found++;
                }
            }

            const agGrids = Array.from(document.querySelectorAll('.ag-root-wrapper, .ag-grid, .ag-center-cols-viewport, [class*="ag-grid"]'));
            for (const el of agGrids) {
                const target = el.querySelector('.ag-center-cols-viewport') || el.querySelector('.ag-root-wrapper') || el;
                const sel = getStableSelector(target);
                if (sel) {
                    addSelector(sel, 'ag_grid');
                    stats.ag_grids_found++;
                }
            }

            const reactGrids = Array.from(document.querySelectorAll(
                '.rdg, .react-data-grid, .react-grid-Container, [class*="data-grid"], [class*="DataGrid"]'
            ));
            for (const el of reactGrids) {
                const sel = getStableSelector(el);
                if (sel) {
                    addSelector(sel, 'react_grid');
                    stats.react_grids_found++;
                }
            }

            const muiTables = Array.from(document.querySelectorAll(
                '.MuiTable-root, .MuiDataGrid-root, .MuiDataGrid-main, [class*="MuiTable"], [class*="MuiDataGrid"]'
            ));
            for (const el of muiTables) {
                const target = el.querySelector('.MuiDataGrid-main') || el.querySelector('.MuiDataGrid-virtualScroller') || el;
                const sel = getStableSelector(target);
                if (sel) {
                    addSelector(sel, 'mui_table');
                    stats.mui_tables_found++;
                }
            }

            const bsTables = Array.from(document.querySelectorAll('.table, .table-responsive'));
            for (const el of bsTables) {
                const hasRows = el.querySelectorAll('tr, [role="row"]').length > 0;
                if (hasRows) {
                    const sel = getStableSelector(el);
                    if (sel) {
                        addSelector(sel, 'bootstrap_table');
                        stats.bootstrap_tables_found++;
                    }
                }
            }

            const antTables = Array.from(document.querySelectorAll(
                '.ant-table, .ant-table-wrapper, .ant-table-container, [class*="ant-table"]'
            ));
            for (const el of antTables) {
                const target = el.querySelector('.ant-table-container') || el.querySelector('.ant-table-body') || el;
                const sel = getStableSelector(target);
                if (sel) {
                    addSelector(sel, 'antd_table');
                    stats.antd_tables_found++;
                }
            }

            const vuetifyTables = Array.from(document.querySelectorAll(
                '.v-data-table, .v-data-table__wrapper, .v-table, [class*="v-data-table"]'
            ));
            for (const el of vuetifyTables) {
                const sel = getStableSelector(el);
                if (sel) {
                    addSelector(sel, 'vuetify_table');
                    stats.vuetify_tables_found++;
                }
            }

            const kendoGrids = Array.from(document.querySelectorAll(
                '.k-grid, .k-grid-content, [class*="k-grid"]'
            ));
            for (const el of kendoGrids) {
                const target = el.querySelector('.k-grid-content') || el;
                const sel = getStableSelector(target);
                if (sel) {
                    addSelector(sel, 'kendo_grid');
                    stats.kendo_grids_found++;
                }
            }

            const devExpressGrids = Array.from(document.querySelectorAll(
                '.dx-datagrid, .dx-datagrid-rowsview, [class*="dx-datagrid"]'
            ));
            for (const el of devExpressGrids) {
                const target = el.querySelector('.dx-datagrid-rowsview') || el;
                const sel = getStableSelector(target);
                if (sel) {
                    addSelector(sel, 'devexpress_grid');
                    stats.devexpress_grids_found++;
                }
            }

            const dataTables = Array.from(document.querySelectorAll(
                '.dataTables_wrapper, .dataTable, [class*="dataTable"]'
            ));
            for (const el of dataTables) {
                const target = el.querySelector('table') || el;
                const sel = getStableSelector(target);
                if (sel) {
                    addSelector(sel, 'datatables');
                    stats.datatables_found++;
                }
            }

            const virtualized = Array.from(document.querySelectorAll(
                '[class*="virtualized"], [class*="VirtualList"], [class*="react-window"], [class*="react-virtualized"], [class*="virtual-list"]'
            ));
            for (const el of virtualized) {
                const hasRowLike = el.querySelectorAll('[role="row"], .row, [class*="row"], > div > div').length > 0;
                if (hasRowLike) {
                    const sel = getStableSelector(el);
                    if (sel) {
                        addSelector(sel, 'virtualized_list');
                        stats.virtualized_found++;
                    }
                }
            }

            const candidates = Array.from(document.querySelectorAll(
                'div[class*="table"], div[class*="grid"], div[class*="list"], section[class*="table"], section[class*="grid"]'
            ));
            for (const el of candidates) {
                const children = el.children;
                if (children.length < 3) continue;

                const childTags = Array.from(children).map(c => c.tagName);
                const uniqueTags = new Set(childTags);
                const hasConsistentStructure = uniqueTags.size <= 2 && childTags.length >= 3;

                const hasCellLike = el.querySelectorAll(
                    '[class*="cell"], [class*="col"], [class*="column"], > div > div'
                ).length > 0;

                const rect = el.getBoundingClientRect();
                const hasReasonableSize = rect.width > 200 && rect.height > 100;

                if ((hasConsistentStructure || hasCellLike) && hasReasonableSize) {
                    const sel = getStableSelector(el);
                    if (sel) {
                        addSelector(sel, 'div_based_table');
                        stats.div_tables_found++;
                    }
                }
            }

            const flexGrids = Array.from(document.querySelectorAll(
                '.flex-table, .div-table, .divTable, [class*="flex-table"], [class*="div-table"]'
            ));
            for (const el of flexGrids) {
                const children = el.children;
                if (children.length >= 2) {
                    const sel = getStableSelector(el);
                    if (sel) {
                        addSelector(sel, 'flex_div_table');
                        stats.flex_div_tables_found++;
                    }
                }
            }

            return {
                selectors: selectors.slice(0, maxTables).map(s => s.selector),
                stats: stats
            };
        }
        """
        try:
            result = await page.evaluate(js, max_tables)
            selectors = result.get("selectors", []) if isinstance(result, dict) else []
            stats = result.get("stats", {}) if isinstance(result, dict) else {}

            logger.bind(ctx=log_ctx(job)).info(
                "table_detection_complete "
                "selectors_found={} html_checked={} html_found={} aria_found={} "
                "ag_grid={} react_grid={} mui={} bootstrap={} antd={} vuetify={} "
                "kendo={} devexpress={} datatables={} virtualized={} div_based={} flex_div={}",
                len(selectors),
                stats.get("html_tables_checked", 0),
                stats.get("html_tables_found", 0),
                stats.get("aria_tables_found", 0),
                stats.get("ag_grids_found", 0),
                stats.get("react_grids_found", 0),
                stats.get("mui_tables_found", 0),
                stats.get("bootstrap_tables_found", 0),
                stats.get("antd_tables_found", 0),
                stats.get("vuetify_tables_found", 0),
                stats.get("kendo_grids_found", 0),
                stats.get("devexpress_grids_found", 0),
                stats.get("datatables_found", 0),
                stats.get("virtualized_found", 0),
                stats.get("div_tables_found", 0),
                stats.get("flex_div_tables_found", 0),
            )
            return selectors
        except Exception as e:
            logger.bind(ctx=log_ctx(job)).warning("table_detection_failed err={}", str(e))
            return []
        

    async def _wait_for_table_ready(
        self,
        page: Page,
        *,
        verify_selector: str,
        timeout_ms: int = 15000,
        extra_sleep_ms: int = 3000,
    ) -> dict:
        """
        Conservative readiness wait after pagination.
        We wait for:
        - container to exist and be visible
        - rows to appear if possible
        - an extra sleep buffer for animated/headful UI
        """
        result = {
            "container_seen": False,
            "rows_seen": False,
            "ready": False,
            "errors": [],
        }

        sel = (verify_selector or "").strip()
        if not sel:
            if extra_sleep_ms > 0:
                await asyncio.sleep(extra_sleep_ms / 1000.0)
            result["ready"] = True
            return result

        try:
            if _is_xpath(sel):
                loc = _locator_for(page, sel).first
                await loc.wait_for(state="visible", timeout=timeout_ms)
                result["container_seen"] = True
            else:
                await page.wait_for_function(
                    """
                    (selector) => {
                    const el = document.querySelector(selector);
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 &&
                            r.height > 0 &&
                            s.display !== "none" &&
                            s.visibility !== "hidden";
                    }
                    """,
                    arg=sel,
                    timeout=timeout_ms,
                )
                result["container_seen"] = True
        except Exception as e:
            result["errors"].append(f"container_wait:{str(e)[:160]}")

        try:
            result["rows_seen"] = await self._selector_has_rows(page, sel, max_rows=5)
        except Exception as e:
            result["errors"].append(f"rows_check:{str(e)[:160]}")

        if extra_sleep_ms > 0:
            await asyncio.sleep(extra_sleep_ms / 1000.0)

        try:
            result["rows_seen"] = result["rows_seen"] or await self._selector_has_rows(
                page,
                sel,
                max_rows=5,
            )
        except Exception as e:
            result["errors"].append(f"rows_recheck:{str(e)[:160]}")

        result["ready"] = result["container_seen"] or result["rows_seen"]
        return result

    async def run(self, *, job: JobEnvelope, page: Page, plan: object, tracker) -> ModuleResult:
        browser = getattr(plan, "browser", None)
        if not browser:
            return ModuleResult(artifacts=[])

        tcfg = getattr(browser, "tables", None)
        if not tcfg or not getattr(tcfg, "enabled", False):
            return ModuleResult(artifacts=[])

        selectors = list(getattr(tcfg, "selectors", []) or [])
        max_tables = int(getattr(tcfg, "max_tables", 100) or 100)
        max_rows = int(getattr(tcfg, "max_rows", 200) or 200)
        max_scrolls = int(getattr(tcfg, "max_scrolls", 20) or 20)
        scroll_delta = int(getattr(tcfg, "scroll_delta", 800) or 800)
        settle_ms = int(getattr(tcfg, "settle_ms", 250) or 250)
        auto_detect = bool(getattr(tcfg, "auto_detect", True))
        use_default_fallbacks = bool(getattr(tcfg, "use_default_fallbacks", True))
        pre_detect_settle_ms = int(getattr(tcfg, "pre_detect_settle_ms", 500) or 500)
        retry_detect = bool(getattr(tcfg, "retry_detect", True))
        pagination_retries = int(getattr(tcfg, "pagination_retries", 3) or 3)


        pagination_pre_click_ms = 5000
        pagination_post_click_ms =  6500
        pagination_ready_timeout_ms = 15000

        logger.bind(ctx=log_ctx(job)).info(
            "table_extraction_starting max_tables={} user_selectors={} auto_detect={} fallback={} "
            "pre_detect_settle_ms={} pagination_retries={} pagination_pre_click_ms={} "
            "pagination_post_click_ms={} pagination_ready_timeout_ms={}",
            max_tables,
            len(selectors),
            auto_detect,
            use_default_fallbacks,
            pre_detect_settle_ms,
            pagination_retries,
            pagination_pre_click_ms,
            pagination_post_click_ms,
            pagination_ready_timeout_ms,
        )

        if max_tables <= 0:
            logger.bind(ctx=log_ctx(job)).warning("table_extraction_invalid_max_tables max_tables={}", max_tables)
            return ModuleResult(artifacts=[])

        final_selectors: list[str] = []
        selector_sources: dict[str, str] = {}

        if selectors:
            for sel in selectors:
                if sel not in selector_sources:
                    final_selectors.append(sel)
                    selector_sources[sel] = "user"
            logger.bind(ctx=log_ctx(job)).info(
                "table_user_selectors_added count={}",
                len(selectors),
            )

        if auto_detect and (not selectors or getattr(tcfg, "auto_detect_always", False)):
            if pre_detect_settle_ms > 0:
                logger.bind(ctx=log_ctx(job)).debug(
                    "table_pre_detect_settle waiting_ms={}",
                    pre_detect_settle_ms,
                )
                await asyncio.sleep(pre_detect_settle_ms / 1000.0)

            detected = await self._detect_table_selectors(page, max_tables, job)

            if not detected and retry_detect:
                retry_wait_ms = min(pre_detect_settle_ms * 2, 3000)
                logger.bind(ctx=log_ctx(job)).info(
                    "table_detect_retry waiting_ms={}",
                    retry_wait_ms,
                )
                await asyncio.sleep(retry_wait_ms / 1000.0)
                detected = await self._detect_table_selectors(page, max_tables, job)

            if detected:
                for sel in detected:
                    if sel not in selector_sources:
                        final_selectors.append(sel)
                        selector_sources[sel] = "auto_detected"
                logger.bind(ctx=log_ctx(job)).info(
                    "table_auto_detected_added count={}",
                    len(detected),
                )

        if use_default_fallbacks and len(final_selectors) < max_tables:
            defaults_added = 0
            for default_sel in self.DEFAULT_TABLE_SELECTORS:
                if default_sel not in selector_sources:
                    final_selectors.append(default_sel)
                    selector_sources[default_sel] = "default_fallback"
                    defaults_added += 1
                    if len(final_selectors) >= max_tables * 2:
                        break
            logger.bind(ctx=log_ctx(job)).info(
                "table_default_fallbacks_added count={}",
                defaults_added,
            )

        if not final_selectors:
            logger.bind(ctx=log_ctx(job)).warning("table_extraction_no_selectors")
            return ModuleResult(artifacts=[])

        logger.bind(ctx=log_ctx(job)).info(
            "table_extraction_final_selectors total={} unique={}",
            len(final_selectors),
            len(set(final_selectors)),
        )

        start_page = int(getattr(tcfg, "start_count", 1) or 1)
        end_page = int(getattr(tcfg, "end_count", start_page) or start_page)
        await dismiss_post_login_popups(page, job)

        start_page, end_page = await self._ensure_page_range(
            page,
            start_page,
            end_page,
            settle_ms=settle_ms,
            job=job,
            verify_selector=".semi-table",
            pre_click_ms=pagination_pre_click_ms,
            post_click_ms=pagination_post_click_ms,
            ready_timeout_ms=pagination_ready_timeout_ms,
        )

        logger.bind(ctx=log_ctx(job)).info(
            "table_pagination_range start_page={} end_page={}",
            start_page,
            end_page,
        )

        artifacts: List[Artifact] = []
        seen_hashes: set[str] = set()
        locked_selector: Optional[str] = None

        extraction_stats = {
            "attempted": 0,
            "html_extracted": 0,
            "grid_extracted": 0,
            "duplicates_skipped": 0,
            "failed": 0,
            "empty_skipped": 0,
            "selector_relocks": 0,
        }

        def _cheap_table_hash(rows: list[list[str]]) -> str:
            return str(rows[:20])[:50_000]

        current_page = start_page
        while current_page <= end_page:
            logger.bind(ctx=log_ctx(job)).info(
                "table_page_processing page_no={} locked_selector={}",
                current_page,
                locked_selector,
            )
            await dismiss_post_login_popups(page, job)
            if settle_ms > 0:
                await asyncio.sleep(min(1.0, float(settle_ms) / 1000.0))

            page_artifact_added = False

            selector_passes: list[list[str]] = []
            if locked_selector:
                selector_passes.append([locked_selector])
                selector_passes.append([s for s in final_selectors if s != locked_selector])
            else:
                selector_passes.append(final_selectors)

            for pass_idx, selector_batch in enumerate(selector_passes, start=1):
                if not selector_batch:
                    continue

                if pass_idx == 2 and locked_selector:
                    logger.bind(ctx=log_ctx(job)).warning(
                        "table_locked_selector_miss page_no={} locked_selector={} action=fallback_to_all_selectors",
                        current_page,
                        locked_selector,
                    )

                for sel in selector_batch:
                    if len(artifacts) >= max_tables:
                        logger.bind(ctx=log_ctx(job)).info(
                            "table_extraction_max_reached count={}",
                            len(artifacts),
                        )
                        break

                    extraction_stats["attempted"] += 1

                    try:
                        artifact, info = await self._extract_selector_artifact(
                            job=job,
                            page=page,
                            sel=sel,
                            max_rows=max_rows,
                            max_scrolls=max_scrolls,
                            scroll_delta=scroll_delta,
                            settle_ms=settle_ms,
                            selector_source=selector_sources.get(sel, "unknown"),
                            page_no=current_page,
                        )

                        if not artifact:
                            extraction_stats["empty_skipped"] += 1
                            continue

                        rows = artifact.data.get("rows") or []
                        if not rows:
                            extraction_stats["empty_skipped"] += 1
                            logger.bind(ctx=log_ctx(job)).info(
                                "table_empty_artifact_skipped selector={} source={} page_no={}",
                                sel,
                                selector_sources.get(sel, "unknown"),
                                current_page,
                            )
                            continue

                        hh = _cheap_table_hash(rows)
                        if hh in seen_hashes:
                            extraction_stats["duplicates_skipped"] += 1
                            logger.bind(ctx=log_ctx(job)).info(
                                "table_duplicate_skipped selector={} source={} page_no={} rows={} locked_selector={}",
                                sel,
                                selector_sources.get(sel, "unknown"),
                                current_page,
                                len(rows),
                                locked_selector,
                            )
                            continue

                        seen_hashes.add(hh)
                        artifacts.append(artifact)

                        if info["mode"] == "html":
                            extraction_stats["html_extracted"] += 1
                            logger.bind(ctx=log_ctx(job)).info(
                                "table_html_extracted selector={} rows={} headers={} source={} page_no={}",
                                sel,
                                info["rows"],
                                info["headers"],
                                selector_sources.get(sel, "unknown"),
                                current_page,
                            )
                        else:
                            extraction_stats["grid_extracted"] += 1
                            logger.bind(ctx=log_ctx(job)).info(
                                "table_grid_extracted selector={} rows={} scrolls={} source={} page_no={}",
                                sel,
                                info["rows"],
                                info["scrolls"],
                                selector_sources.get(sel, "unknown"),
                                current_page,
                            )

                        if locked_selector != sel:
                            prev = locked_selector
                            locked_selector = sel
                            extraction_stats["selector_relocks"] += 1
                            logger.bind(ctx=log_ctx(job)).info(
                                "table_selector_locked selector={} source={} page_no={} previous_selector={}",
                                sel,
                                selector_sources.get(sel, "unknown"),
                                current_page,
                                prev,
                            )

                        page_artifact_added = True
                        break

                    except Exception as e:
                        extraction_stats["failed"] += 1
                        logger.bind(ctx=log_ctx(job)).warning(
                            "table_extract_failed selector={} source={} page_no={} locked_selector={} err={}",
                            sel,
                            selector_sources.get(sel, "unknown"),
                            current_page,
                            locked_selector,
                            str(e)[:200],
                        )
                        continue

                if page_artifact_added or len(artifacts) >= max_tables:
                    break

            if not page_artifact_added:
                logger.bind(ctx=log_ctx(job)).warning(
                    "table_page_no_unique_artifact page_no={} locked_selector={}",
                    current_page,
                    locked_selector,
                )
                locked_selector = None

            if len(artifacts) >= max_tables:
                break

            if current_page >= end_page:
                break

            verify_selector = locked_selector or ".semi-table"

            ok, detail = await self._click_next_page(
                page,
                expected_page=current_page + 1,
                verify_selector=verify_selector,
                settle_ms=settle_ms,
                job=job,
                pre_click_ms=pagination_pre_click_ms,
                post_click_ms=pagination_post_click_ms,
                ready_timeout_ms=pagination_ready_timeout_ms,
            )
            if not ok:
                logger.bind(ctx=log_ctx(job)).warning(
                    "table_pagination_next_failed current_page_no={} target_page_no={} locked_selector={} verify_selector={} detail={}",
                    current_page,
                    current_page + 1,
                    locked_selector,
                    verify_selector,
                    detail,
                )
                break

            current_page += 1

        logger.bind(ctx=log_ctx(job)).info(
            "table_extraction_complete artifacts={} attempted={} html={} grid={} duplicates_skipped={} empty_skipped={} failed={} selector_relocks={} final_locked_selector={}",
            len(artifacts),
            extraction_stats["attempted"],
            extraction_stats["html_extracted"],
            extraction_stats["grid_extracted"],
            extraction_stats["duplicates_skipped"],
            extraction_stats["empty_skipped"],
            extraction_stats["failed"],
            extraction_stats["selector_relocks"],
            locked_selector,
        )

        return ModuleResult(artifacts=artifacts)