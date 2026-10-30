import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from playwright.async_api import Download, Locator, Page, Response
from pydantic import BaseModel, ConfigDict, Field

from src.autobots.browser_task.inmem.state import JobEnvelope
from src.autobots.browser_task.runtime.artifacts import Artifact, ArtifactKind
from src.autobots.browser_task.runtime.modules.base import ModuleResult
from src.autobots.browser_task.runtime.modules.utils import (
    bounded_bytes,
    guess_mime_from_filename,
    log_ctx,
    mk_temp_path,
    safe_json_loads,
    try_unlink,
    upload_bytes_public_s3,
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


class NetworkCapture(BaseModel):
    attempt_no: int
    policy_id: str

    url: str
    method: str
    status: int
    content_type: str

    elapsed_ms: int
    response_size: Optional[int] = None

    request_headers: Dict[str, str] = Field(default_factory=dict)
    response_headers: Dict[str, str] = Field(default_factory=dict)

    body: Optional[bytes] = None
    parsed_json: Optional[Any] = None

    graphql_operation: Optional[str] = None
    graphql_variables: Optional[dict] = None
    graphql_query: Optional[str] = None

    captured_at: float = Field(default_factory=time.time)

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
    )

def network_capture_to_artifact(job: JobEnvelope, cap: NetworkCapture) -> Artifact:
    return Artifact(
        kind=ArtifactKind.network_json
        if "application/json" in cap.content_type
        else ArtifactKind.binary,
        label="network_capture",
        source_url=cap.url,
        data={
            "url": cap.url,
            "method": cap.method,
            "status": cap.status,
            "content_type": cap.content_type,
            "elapsed_ms": cap.elapsed_ms,
            "graphql_operation": cap.graphql_operation,
            "graphql_variables": cap.graphql_variables,
            "graphql_query": cap.graphql_query,
            "parsed_json": cap.parsed_json,
        },
        meta={
            "attempt": cap.attempt_no,
            "policy": cap.policy_id,
            "response_size": cap.response_size,
        },
        blob=cap.body,
    )


class NetworkRecorder:
    def __init__(
        self,
        *,
        job,
        attempt_no: int,
        policy_id: str,
        net_cfg,
    ):
        self.job = job
        self.attempt_no = attempt_no
        self.policy_id = policy_id
        self.cfg = net_cfg

        self._captures: list[NetworkCapture] = []
        self._lock = asyncio.Lock()
        self._handler = None

    def attach(self, page: Page):
        async def _on_response(resp: Response):
            await self._handle_response(resp)

        self._handler = _on_response
        page.on("response", _on_response)

        logger.bind(
            ctx=log_ctx(self.job),
            attempt=self.attempt_no,
            policy=self.policy_id,
        ).info("network_recorder_attached")

    def detach(self, page: Page):
        if self._handler:
            page.remove_listener("response", self._handler)
            self._handler = None

            logger.bind(
                ctx=log_ctx(self.job),
                attempt=self.attempt_no,
                policy=self.policy_id,
            ).info("network_recorder_detached")

    async def _handle_response(self, resp: Response):
        started = time.time()
        
        try:
            req = resp.request
            url = resp.url or ""
            method = req.method or "GET"

            try:
                headers = await resp.all_headers()
            except Exception:
                return
            ctype = (headers.get("content-type") or "").lower()

            if _is_noise(url, ctype):
                return

            if "application/json" not in ctype:
                logger.bind(ctx=log_ctx(self.job)).debug(
                    "network_skip_non_json url={} ctype={}",
                    url,
                    ctype,
                )
                return
            
            # if not (
            #     "application/json" in ctype
            #     or ctype.startswith("image/")
            # ):
            #     logger.bind(ctx=log_ctx(self.job)).debug(
            #         "network_skip_non_json url={} ctype={}",
            #         url,
            #         ctype,
            #     )
            #     return

            max_body = int(getattr(self.cfg, "max_body_bytes", 2_000_000))

            cl = headers.get("content-length")
            if cl:
                try:
                    if int(cl) > max_body:
                        logger.bind(ctx=log_ctx(self.job)).warning(
                            "network_body_too_large url={} size={}",
                            url,
                            cl,
                        )
                        return
                except ValueError:
                    pass

            body = None
            parsed = None

            if getattr(self.cfg, "capture_bodies", True):
                try:
                    body = await resp.body()
                    if body and len(body) > max_body:
                        body = body[:max_body]
                except Exception as e:
                    logger.bind(ctx=log_ctx(self.job)).debug(
                        "network_body_read_failed url={} err={}",
                        url,
                        str(e),
                    )

            if body:
                parsed = safe_json_loads(body)

            graphql_op = None
            graphql_vars = None
            graphql_query = None

            if method == "POST" and "graphql" in url:
                try:
                    post = await req.post_data_json()
                    if isinstance(post, dict):
                        graphql_op = post.get("operationName")
                        graphql_vars = post.get("variables")
                        graphql_query = post.get("query")
                except Exception:
                    pass

            elapsed_ms = int((time.time() - started) * 1000)

            cap = NetworkCapture(
                attempt_no=self.attempt_no,
                policy_id=self.policy_id,
                url=url,
                method=method,
                status=resp.status,
                content_type=ctype,
                elapsed_ms=elapsed_ms,
                response_size=len(body) if body else None,
                request_headers=dict(req.headers),
                response_headers=headers,
                body=body,
                parsed_json=parsed,
                graphql_operation=graphql_op,
                graphql_variables=graphql_vars,
                graphql_query=graphql_query,
            )

            async with self._lock:
                self._captures.append(cap)

        except Exception as e:
            logger.bind(ctx=log_ctx(self.job)).warning(
                "network_capture_failed err={}",
                str(e),
            )

    async def snapshot(self) -> list[NetworkCapture]:
        async with self._lock:
            return list(self._captures)


class NetworkUploadWorker:
    def __init__(self, *, job):
        self.job = job
        self.q: asyncio.Queue[NetworkCapture] = asyncio.Queue()
        self.task = asyncio.create_task(self._run())

    async def enqueue(self, cap: NetworkCapture):
        if cap.body:
            await self.q.put(cap)

    async def shutdown(self):
        # wait until all queued uploads are done
        await self.q.join()

        # stop the worker task
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass

        logger.bind(ctx=log_ctx(self.job)).debug("network_upload_worker_shutdown")

    async def _run(self):
        while True:
            cap = await self.q.get()
            try:
                await upload_bytes_public_s3(
                    job=self.job,
                    data=cap.body,
                    filename="network.json",
                    mime="application/json",
                    kind=ArtifactKind.network_json,
                    label="network_body",
                    meta={
                        "url": cap.url,
                        "method": cap.method,
                        "status": cap.status,
                        "attempt": cap.attempt_no,
                        "policy": cap.policy_id,
                        "graphql_operation": cap.graphql_operation,
                    },
                )

            except Exception as e:
                logger.bind(ctx=log_ctx(self.job)).error(
                    "network_upload_failed url={} err={}",
                    cap.url,
                    str(e),
                )
            finally:
                self.q.task_done()

class ScreenshotPdfModule(BaseModel):
    name: str = "screenshots_pdf"

    async def run(self, *, job: JobEnvelope, page: Page, plan: object, tracker) -> ModuleResult:
        artifacts: List[Artifact] = []
        browser = getattr(plan, "browser", None)
        if not browser:
            return ModuleResult(artifacts=[])

        # Screenshots
        shots = getattr(browser, "screenshots", None)
        if shots and getattr(shots, "enabled", False):
            max_images = int(getattr(shots, "max_images", 0) or 0)
            if max_images > 0:
                count = 0

                if bool(getattr(shots, "full_page", False)) and count < max_images:
                    try:
                        img = await page.screenshot(full_page=True, type="png")
                        artifacts.append(
                            await upload_bytes_public_s3(
                                job=job,
                                data=img,
                                filename=f"page_full_{count+1}.png",
                                mime="image/png",
                                kind=ArtifactKind.screenshot,
                                label="full_page",
                            )
                        )
                        count += 1
                    except Exception as e:
                        logger.bind(ctx=log_ctx(job)).warning("full_page_screenshot_failed err={}", str(e))

                selectors = list(getattr(shots, "selectors", []) or [])
                for sel in selectors:
                    if count >= max_images:
                        break
                    try:
                        loc = _locator_for(page, sel).first

                        if await loc.count() <= 0:
                            continue
                        img = await loc.screenshot(type="png")
                        artifacts.append(
                            await upload_bytes_public_s3(
                                job=job,
                                data=img,
                                filename=f"element_{count+1}.png",
                                mime="image/png",
                                kind=ArtifactKind.screenshot,
                                label="element",
                                selector=sel,
                            )
                        )
                        count += 1
                    except Exception as e:
                        logger.bind(ctx=log_ctx(job)).warning("element_screenshot_failed selector={} err={}", sel, str(e))

        pdfp = getattr(browser, "pdf", None)
        if pdfp and getattr(pdfp, "enabled", False) and bool(getattr(pdfp, "page", True)):
            try:
                pdf_bytes = await page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
                artifacts.append(
                    await upload_bytes_public_s3(
                        job=job,
                        data=pdf_bytes,
                        filename="page.pdf",
                        mime="application/pdf",
                        kind=ArtifactKind.pdf,
                        label="page_pdf",
                    )
                )
            except Exception as e:
                logger.bind(ctx=log_ctx(job)).warning("page_pdf_failed err={}", str(e))

        return ModuleResult(artifacts=artifacts)



class DownloadsModule(BaseModel):
    name: str = "downloads"

    async def _wait_for_download(self, page: Page, timeout_ms: int) -> Optional[Download]:
        try:
            d = await page.wait_for_event("download", timeout=timeout_ms)
            return d
        except Exception:
            return None
        

    async def download_to_artifact(
        self,
        *,
        job: JobEnvelope,
        d: Download,
        ordinal: int,
        max_bytes: int,
        label: str,
        selector: Optional[str] = None,
        meta: Optional[dict] = None,
    ) -> Artifact:
        raw, suggested, mime, truncated = await self._save_download_to_bytes(d, max_bytes=max_bytes)
        filename = f"download_{ordinal}_{suggested}"
        return await upload_bytes_public_s3(
            job=job,
            data=raw,
            filename=filename,
            mime=mime,
            kind=ArtifactKind.download,
            label=label,
            selector=selector,
            meta={
                **(meta or {}),
                "suggested_filename": suggested,
                "truncated": truncated,
                "download_url": getattr(d, "url", None),
            },
        )

    async def _save_download_to_bytes(self, d: Download, *, max_bytes: int) -> Tuple[bytes, str, str, bool]:
        """
        Returns (bytes, filename, mime, was_truncated)
        """
        suggested = d.suggested_filename or "download.bin"
        mime = guess_mime_from_filename(suggested)

        tmp = mk_temp_path(prefix="pw_dl_", suffix="_" + suggested.replace("/", "_"))
        try:
            await d.save_as(tmp)
            with open(tmp, "rb") as f:
                raw = f.read()
            raw2, truncated = bounded_bytes(raw, max_bytes=max_bytes)
            return raw2, suggested, mime, truncated
        finally:
            try_unlink(tmp)

    async def run(self, *, job: JobEnvelope, page: Page, plan: object, tracker) -> ModuleResult:
        artifacts: List[Artifact] = []
        browser = getattr(plan, "browser", None)
        if not browser:
            return ModuleResult(artifacts=[])

        dl = getattr(browser, "downloads", None)
        if not dl or not getattr(dl, "enabled", False):
            return ModuleResult(artifacts=[])

        click_selectors = list(getattr(dl, "click_selectors", []) or [])
        max_files = int(getattr(dl, "max_files", 0) or 0)
        timeout_ms = int(getattr(dl, "timeout_ms", 15_000) or 15_000)
        max_bytes = int(getattr(dl, "max_bytes", 20_000_000) or 20_000_000)  # 20MB default

        if max_files <= 0 or not click_selectors:
            return ModuleResult(artifacts=[])

        # allow downloads in chromium
        # (Playwright handles this automatically if download triggered)
        # this thing directly came from documentation no hindress
        # this could be a race condition below but yeah it's rare and it did not occur till now.

        count = 0
        for sel in click_selectors:
            if count >= max_files:
                break

            try:
                loc = _locator_for(page, sel).first
                if await loc.count() <= 0:
                    continue

                # Click and wait for download event
                # Using wait_for_event is reliable vs expect_download w/ loc.click in some cases.
                click_task = asyncio.create_task(loc.click())
                d = await self._wait_for_download(page, timeout_ms=timeout_ms)
                await click_task

                if not d:
                    continue

                raw, suggested, mime, truncated = await self._save_download_to_bytes(d, max_bytes=max_bytes)

                filename = suggested
                # group by ordinal to avoid duplicates
                s3_name = f"download_{count+1}_{filename}"
                art = await upload_bytes_public_s3(
                    job=job,
                    data=raw,
                    filename=s3_name,
                    mime=mime,
                    kind=ArtifactKind.download,
                    label="download",
                    selector=sel,
                    meta={
                        "suggested_filename": suggested,
                        "truncated": truncated,
                        "download_url": getattr(d, "url", None),
                    },
                )
                artifacts.append(art)
                count += 1

            except Exception as e:
                logger.bind(ctx=log_ctx(job)).warning("download_capture_failed selector={} err={}", sel, str(e))
                continue

        return ModuleResult(artifacts=artifacts)


class NetworkJsonModule(BaseModel):
    name: str = "network_json"
    attach_before_goto: bool = True

    async def run(self, *, job, page, plan, tracker):
        browser = getattr(plan, "browser", None)
        net = getattr(browser, "network", None) if browser else None
        if not net or not getattr(net, "enabled", False):
            return None

        # Per-attempt recorder
        recorder = NetworkRecorder(
            job=job,
            attempt_no=job.attempt_no,
            policy_id=plan.policy_id,
            net_cfg=net,
        )

        # Upload worker (one per attempt) - preventing race condition and also memory leaks all will be handled seamlessly.
        # uploader = NetworkUploadWorker(job=job)

        # Attach before navigation
        recorder.attach(page)

        tracker.check_time()

        # Executor will call run() again after_ready
        # if not hasattr(job, "_network_finalizers"):
        #     job._network_finalizers = []
        # job._network_finalizers.append((recorder, uploader))

        if not hasattr(job, "_network_recorders"):
            job._network_recorders = []
        job._network_recorders.append(recorder)

        return None


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


class DefaultTableExtractionModule:
    name: str = "tables"

    # Default selectors for common table patterns and frameworks
    DEFAULT_TABLE_SELECTORS = [
        # Classic HTML tables
        "table",
        "table.data",
        "table.table",
        ".table",
        ".data-table",
        ".dataTable",
        # ARIA tables/grids
        '[role="table"]',
        '[role="grid"]',
        '[role="grid"] > div',  # Common grid container pattern
        # AG-Grid
        ".ag-root-wrapper",
        ".ag-grid",
        ".ag-center-cols-viewport",
        # React Data Grid
        ".react-grid-Container",
        ".rdg",
        ".react-data-grid",
        # Material-UI
        ".MuiTable-root",
        ".MuiDataGrid-root",
        ".MuiDataGrid-main",
        # Ant Design
        ".ant-table",
        ".ant-table-wrapper",
        ".ant-table-container",
        # Bootstrap
        ".table-responsive",
        ".table > tbody",
        # Carbon Design System
        ".bx--data-table",
        ".bx--data-table-container",
        # Vuetify
        ".v-data-table",
        ".v-data-table__wrapper",
        ".v-table",
        # Kendo UI
        ".k-grid",
        ".k-grid-content",
        # DevExpress
        ".dx-datagrid",
        ".dx-datagrid-rowsview",
        # jQuery DataTables
        ".dataTables_wrapper",
        ".dataTable",
        # SlickGrid
        ".slickgrid-container",
        ".slick-viewport",
        # Handsontable
        ".ht_master",
        ".handsontable",
        # Tabulator
        ".tabulator",
        ".tabulator-tableholder",
        # FancyGrid
        ".fancy-grid",
        # Webix
        ".webix_datatable",
        # DHTMLX
        ".dhx_grid",
        # Sencha/ExtJS
        ".x-grid-view",
        # Generic patterns
        '[class*="table"][class*="container"]',
        '[class*="data"][class*="grid"]',
        '[class*="grid"][class*="container"]',
        ".grid-container",
        ".table-container",
        ".list-container",
        # Div-based tables
        ".div-table",
        ".divTable",
        ".flex-table",
    ]

    async def _detect_table_selectors(self, page: Page, max_tables: int, job: JobEnvelope) -> list[str]:
        """
        Auto-detect table selectors on the page.
        Covers:
        - Classic HTML <table> elements
        - Modern virtualized grids (AG-Grid, React Data Grid, etc.)
        - Data tables with role="table" or role="grid"
        - Common CSS framework tables (Bootstrap, Material, etc.)
        """
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

                // Prefer ID
                if (el.id) return '#' + el.id;

                // Prefer data-testid or data-cy
                const testId = el.getAttribute('data-testid');
                if (testId) return `[data-testid="${testId}"]`;
                const dataCy = el.getAttribute('data-cy');
                if (dataCy) return `[data-cy="${dataCy}"]`;

                // Prefer class if unique enough
                if (el.className && typeof el.className === 'string') {
                    const classes = el.className.split(' ').filter(c =>
                        c && !c.includes(':') && c.length > 2
                    );
                    if (classes.length > 0 && classes.length <= 3) {
                        const classSel = '.' + classes.join('.');
                        // Check if this selector is unique enough
                        try {
                            const matches = document.querySelectorAll(classSel);
                            if (matches.length === 1 || matches[0] === el) {
                                return classSel;
                            }
                        } catch (e) {}
                    }
                }

                // Build path-based selector as fallback
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
                    // Add nth-child for specificity
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

            // 1. Find classic HTML tables
            const tables = Array.from(document.querySelectorAll('table'));
            stats.html_tables_checked = tables.length;
            for (const table of tables) {
                // Skip tiny tables (likely layout tables)
                const rows = table.querySelectorAll('tr');
                if (rows.length < 2) continue;
                const cells = table.querySelectorAll('td, th');
                if (cells.length < 4) continue;

                // Skip if inside another table (nested tables)
                if (table.closest('table') !== table) continue;

                const sel = getStableSelector(table);
                if (sel) {
                    addSelector(sel, 'html_table');
                    stats.html_tables_found++;
                }
            }

            // 2. Find ARIA tables/grids
            const ariaTables = Array.from(document.querySelectorAll('[role="table"], [role="grid"]'));
            for (const el of ariaTables) {
                // Skip if already found as a child of another table
                if (el.closest('table') || el.closest('[role="table"]') || el.closest('[role="grid"]')) {
                    continue;
                }
                // Check it has row-like children
                const hasRows = el.querySelectorAll('[role="row"], [role="rowgroup"] > *').length > 0;
                if (!hasRows) continue;

                const sel = getStableSelector(el);
                if (sel) {
                    addSelector(sel, 'aria_table');
                    stats.aria_tables_found++;
                }
            }

            // 3. Find AG-Grid containers
            const agGrids = Array.from(document.querySelectorAll('.ag-root-wrapper, .ag-grid, .ag-center-cols-viewport, [class*="ag-grid"]'));
            for (const el of agGrids) {
                // Prefer the viewport or root wrapper for better extraction
                const target = el.querySelector('.ag-center-cols-viewport') || el.querySelector('.ag-root-wrapper') || el;
                const sel = getStableSelector(target);
                if (sel) {
                    addSelector(sel, 'ag_grid');
                    stats.ag_grids_found++;
                }
            }

            // 4. Find React Data Grid / common grid classes
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

            // 5. Find Material-UI / MUI tables
            const muiTables = Array.from(document.querySelectorAll(
                '.MuiTable-root, .MuiDataGrid-root, .MuiDataGrid-main, [class*="MuiTable"], [class*="MuiDataGrid"]'
            ));
            for (const el of muiTables) {
                // For DataGrid, prefer the main content area
                const target = el.querySelector('.MuiDataGrid-main') || el.querySelector('.MuiDataGrid-virtualScroller') || el;
                const sel = getStableSelector(target);
                if (sel) {
                    addSelector(sel, 'mui_table');
                    stats.mui_tables_found++;
                }
            }

            // 6. Find Bootstrap tables
            const bsTables = Array.from(document.querySelectorAll('.table, .table-responsive'));
            for (const el of bsTables) {
                // Check if it contains table-like structure
                const hasRows = el.querySelectorAll('tr, [role="row"]').length > 0;
                if (hasRows) {
                    const sel = getStableSelector(el);
                    if (sel) {
                        addSelector(sel, 'bootstrap_table');
                        stats.bootstrap_tables_found++;
                    }
                }
            }

            // 7. Find Ant Design tables
            const antTables = Array.from(document.querySelectorAll(
                '.ant-table, .ant-table-wrapper, .ant-table-container, [class*="ant-table"]'
            ));
            for (const el of antTables) {
                // Prefer the container for better extraction
                const target = el.querySelector('.ant-table-container') || el.querySelector('.ant-table-body') || el;
                const sel = getStableSelector(target);
                if (sel) {
                    addSelector(sel, 'antd_table');
                    stats.antd_tables_found++;
                }
            }

            // 8. Find Vuetify tables
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

            // 9. Find Kendo UI grids
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

            // 10. Find DevExpress grids
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

            // 11. Find jQuery DataTables
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

            // 12. Find generic virtualized lists that look like tables
            const virtualized = Array.from(document.querySelectorAll(
                '[class*="virtualized"], [class*="VirtualList"], [class*="react-window"], [class*="react-virtualized"], [class*="virtual-list"]'
            ));
            for (const el of virtualized) {
                // Check if it has row-like children
                const hasRowLike = el.querySelectorAll('[role="row"], .row, [class*="row"], > div > div').length > 0;
                if (hasRowLike) {
                    const sel = getStableSelector(el);
                    if (sel) {
                        addSelector(sel, 'virtualized_list');
                        stats.virtualized_found++;
                    }
                }
            }

            // 13. Find div-based tables (common in modern SPAs)
            // Look for containers with many row-like children
            const candidates = Array.from(document.querySelectorAll(
                'div[class*="table"], div[class*="grid"], div[class*="list"], section[class*="table"], section[class*="grid"]'
            ));
            for (const el of candidates) {
                // Skip if already captured
                if (el.closest('table') || el.closest('[role="table"]') || el.closest('[role="grid"]')) {
                    continue;
                }

                const children = el.children;
                if (children.length < 3) continue;

                // Check if children have similar structure (indicating rows)
                const childTags = Array.from(children).map(c => c.tagName);
                const uniqueTags = new Set(childTags);
                const hasConsistentStructure = uniqueTags.size <= 2 && childTags.length >= 3;

                // Check for cell-like structure
                const hasCellLike = el.querySelectorAll(
                    '[class*="cell"], [class*="col"], [class*="column"], > div > div'
                ).length > 0;

                // Check for reasonable size (not tiny)
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

            // 14. Find flex/grid layouts that look like tables
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

            // Return top selectors, prioritizing by specificity
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

    async def run(self, *, job: JobEnvelope, page: Page, plan: object, tracker) -> ModuleResult:
        browser = getattr(plan, "browser", None)
        if not browser:
            return ModuleResult(artifacts=[])

        tcfg = getattr(browser, "tables", None)
        if not tcfg or not getattr(tcfg, "enabled", False):
            return ModuleResult(artifacts=[])

        selectors = list(getattr(tcfg, "selectors", []) or [])
        max_tables = int(getattr(tcfg, "max_tables", 0) or 0)
        max_rows = int(getattr(tcfg, "max_rows", 200) or 200)
        max_scrolls = int(getattr(tcfg, "max_scrolls", 20) or 20)
        scroll_delta = int(getattr(tcfg, "scroll_delta", 800) or 800)
        settle_ms = int(getattr(tcfg, "settle_ms", 250) or 250)
        auto_detect = bool(getattr(tcfg, "auto_detect", True))
        use_default_fallbacks = bool(getattr(tcfg, "use_default_fallbacks", True))
        pre_detect_settle_ms = int(getattr(tcfg, "pre_detect_settle_ms", 500) or 500)
        retry_detect = bool(getattr(tcfg, "retry_detect", True))

        logger.bind(ctx=log_ctx(job)).info(
            "table_extraction_starting max_tables={} user_selectors={} auto_detect={} fallback={} pre_detect_settle_ms={}",
            max_tables,
            len(selectors),
            auto_detect,
            use_default_fallbacks,
            pre_detect_settle_ms,
        )

        if max_tables <= 0:
            logger.bind(ctx=log_ctx(job)).warning("table_extraction_invalid_max_tables max_tables={}", max_tables)
            return ModuleResult(artifacts=[])

        # Build selector list: user provided -> auto-detected -> default fallbacks
        final_selectors: list[str] = []
        selector_sources: dict[str, str] = {}  # Track where each selector came from

        # 1. Use user-provided selectors
        if selectors:
            for sel in selectors:
                if sel not in selector_sources:
                    final_selectors.append(sel)
                    selector_sources[sel] = "user"
            logger.bind(ctx=log_ctx(job)).info(
                "table_user_selectors_added count={}",
                len(selectors),
            )

        # 2. Auto-detect if enabled
        if auto_detect and (not selectors or getattr(tcfg, "auto_detect_always", False)):
            # Wait for JS-rendered tables to load before detection
            if pre_detect_settle_ms > 0:
                logger.bind(ctx=log_ctx(job)).debug(
                    "table_pre_detect_settle waiting_ms={}",
                    pre_detect_settle_ms,
                )
                await asyncio.sleep(pre_detect_settle_ms / 1000.0)

            detected = await self._detect_table_selectors(page, max_tables, job)

            # Retry once if no tables found and retry is enabled
            if not detected and retry_detect:
                retry_wait_ms = min(pre_detect_settle_ms * 2, 3000)  # Double the wait, max 3s
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

        # 3. Add default fallback selectors if enabled and still need more
        if use_default_fallbacks and len(final_selectors) < max_tables:
            defaults_added = 0
            for default_sel in self.DEFAULT_TABLE_SELECTORS:
                if default_sel not in selector_sources:
                    final_selectors.append(default_sel)
                    selector_sources[default_sel] = "default_fallback"
                    defaults_added += 1
                    if len(final_selectors) >= max_tables * 2:  # Limit fallbacks
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

        artifacts: List[Artifact] = []
        seen_hashes: set[str] = set()
        extraction_stats = {
            "attempted": 0,
            "html_extracted": 0,
            "grid_extracted": 0,
            "duplicates_skipped": 0,
            "failed": 0,
        }

        def _cheap_table_hash(rows: list[list[str]]) -> str:
            return str(rows[:20])[:50_000]

        for sel in final_selectors:
            if len(artifacts) >= max_tables:
                logger.bind(ctx=log_ctx(job)).info(
                    "table_extraction_max_reached count={}",
                    len(artifacts),
                )
                break

            extraction_stats["attempted"] += 1
            # logger.bind(ctx=log_ctx(job)).debug(
            #     "table_extracting selector={} source={}",
            #     sel,
            #     selector_sources.get(sel, "unknown"),
            # )

            try:
                h = None
                if _is_xpath(sel):
                    h = await _locator_for(page, sel).first.element_handle()

                if h:
                    try:
                        extracted = await page.evaluate(_JS_EXTRACT_TABLE_EL, {"el": h, "maxRows": max_rows})
                    finally:
                        try:
                            await h.dispose()
                        except Exception:
                            pass
                        h = None
                else:
                    extracted = await page.evaluate(_JS_EXTRACT_TABLE, {"selector": sel, "maxRows": max_rows})

                if extracted:
                    rows = extracted.get("rows") or []
                    hh = _cheap_table_hash(rows)

                    if hh in seen_hashes:
                        extraction_stats["duplicates_skipped"] += 1
                        # logger.bind(ctx=log_ctx(job)).debug(
                        #     "table_duplicate_skipped selector={}",
                        #     sel,
                        # )
                        continue
                    seen_hashes.add(hh)

                    artifacts.append(
                        Artifact(
                            kind=ArtifactKind.table,
                            label="table",
                            source_url=str(job.plan.url),
                            selector=sel,
                            data={
                                "type": "table",
                                "headers": extracted.get("headers") or [],
                                "rows": rows,
                            },
                            meta={
                                "virtualized": False,
                                "source": selector_sources.get(sel, "unknown"),
                            },
                        )
                    )
                    extraction_stats["html_extracted"] += 1
                    # logger.bind(ctx=log_ctx(job)).info(
                    #     "table_html_extracted selector={} rows={} headers={} source={}",
                    #     sel,
                    #     row_count,
                    #     len(extracted.get("headers") or []),
                    #     selector_sources.get(sel, "unknown"),
                    # )
                    continue

                # Try virtualized/grid extraction
                merged: List[List[str]] = []
                seen_row_keys: set[str] = set()
                no_new_rounds = 0
                scroll_count = 0

                h_grid = None
                if _is_xpath(sel):
                    h_grid = await _locator_for(page, sel).first.element_handle()

                # logger.bind(ctx=log_ctx(job)).debug(
                #     "table_grid_extraction_starting selector={} max_scrolls={} source={}",
                #     sel,
                #     max_scrolls,
                #     selector_sources.get(sel, "unknown"),
                # )

                for _ in range(max_scrolls):
                    scroll_count += 1
                    if h_grid:
                        vis = await page.evaluate(_JS_EXTRACT_VISIBLE_GRID_EL, {"el": h_grid, "maxRows": max_rows})
                    else:
                        vis = await page.evaluate(_JS_EXTRACT_VISIBLE_GRID, {"selector": sel, "maxRows": max_rows})

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
                        _ = await page.evaluate(_JS_SCROLL_CONTAINER_EL, {"el": h_grid, "delta": scroll_delta})
                    else:
                        _ = await page.evaluate(_JS_SCROLL_CONTAINER, {"selector": sel, "delta": scroll_delta})

                    if settle_ms > 0:
                        await asyncio.sleep(min(0.8, float(settle_ms) / 1000.0))

                if h_grid:
                    try:
                        await h_grid.dispose()
                    except Exception:
                        pass

                if merged:
                    hh = _cheap_table_hash(merged)
                    if hh in seen_hashes:
                        extraction_stats["duplicates_skipped"] += 1
                        # logger.bind(ctx=log_ctx(job)).debug(
                        #     "table_grid_duplicate_skipped selector={}",
                        #     sel,
                        # )
                        continue
                    seen_hashes.add(hh)

                    artifacts.append(
                        Artifact(
                            kind=ArtifactKind.table,
                            label="grid_table",
                            source_url=str(job.plan.url),
                            selector=sel,
                            data={"type": "grid", "headers": [], "rows": merged[:max_rows]},
                            meta={
                                "virtualized": True,
                                "max_scrolls": max_scrolls,
                                "actual_scrolls": scroll_count,
                                "source": selector_sources.get(sel, "unknown"),
                            },
                        )
                    )
                    extraction_stats["grid_extracted"] += 1
                    logger.bind(ctx=log_ctx(job)).info(
                        "table_grid_extracted selector={} rows={} scrolls={} source={}",
                        sel,
                        len(merged),
                        scroll_count,
                        selector_sources.get(sel, "unknown"),
                    )
                else:
                    # logger.bind(ctx=log_ctx(job)).debug(
                    #     "table_grid_no_data selector={} scrolls={}",
                    #     sel,
                    #     scroll_count,
                    # )
                    pass

            except Exception as e:
                extraction_stats["failed"] += 1
                logger.bind(ctx=log_ctx(job)).warning(
                    "table_extract_failed selector={} source={} err={}",
                    sel,
                    selector_sources.get(sel, "unknown"),
                    str(e)[:200],
                )
                continue

        # Log comprehensive summary
        logger.bind(ctx=log_ctx(job)).info(
            "table_extraction_complete "
            "artifacts={} attempted={} html={} grid={} duplicates_skipped={} failed={}",
            len(artifacts),
            extraction_stats["attempted"],
            extraction_stats["html_extracted"],
            extraction_stats["grid_extracted"],
            extraction_stats["duplicates_skipped"],
            extraction_stats["failed"],
        )

        return ModuleResult(artifacts=artifacts)

_JS_EXTRACT_SVG = r"""
(arg) => {
  const selector = arg.selector;
  const root = document.querySelector(selector);
  if (!root) return null;

  const svg = (root.tagName && root.tagName.toLowerCase() === "svg") ? root : root.querySelector("svg");
  if (!svg) return null;

  function attr(el, k) { return (el.getAttribute(k) || "").trim(); }

  const texts = Array.from(svg.querySelectorAll("text")).map(t => ({
    text: (t.textContent || "").trim(),
    x: attr(t, "x"),
    y: attr(t, "y"),
    transform: attr(t, "transform")
  })).filter(x => x.text);

  const paths = Array.from(svg.querySelectorAll("path")).map(p => ({
    d: attr(p, "d"),
    stroke: attr(p, "stroke"),
    fill: attr(p, "fill"),
    className: (p.getAttribute("class") || "").trim()
  })).filter(x => x.d);

  const polylines = Array.from(svg.querySelectorAll("polyline, polygon")).map(p => ({
    points: attr(p, "points"),
    stroke: attr(p, "stroke"),
    fill: attr(p, "fill"),
    className: (p.getAttribute("class") || "").trim()
  })).filter(x => x.points);

  const rects = Array.from(svg.querySelectorAll("rect")).map(r => ({
    x: attr(r,"x"), y: attr(r,"y"),
    width: attr(r,"width"), height: attr(r,"height"),
    fill: attr(r,"fill"), stroke: attr(r,"stroke"),
    className: (r.getAttribute("class") || "").trim()
  })).filter(x => x.width || x.height);

  const lines = Array.from(svg.querySelectorAll("line")).map(l => ({
    x1: attr(l,"x1"), y1: attr(l,"y1"), x2: attr(l,"x2"), y2: attr(l,"y2"),
    stroke: attr(l,"stroke"),
    className: (l.getAttribute("class") || "").trim()
  })).filter(x => x.x1 || x.x2);

  const circles = Array.from(svg.querySelectorAll("circle")).map(c => ({
    cx: attr(c,"cx"), cy: attr(c,"cy"), r: attr(c,"r"),
    fill: attr(c,"fill"), stroke: attr(c,"stroke"),
    className: (c.getAttribute("class") || "").trim()
  })).filter(x => x.r);

  return {
    kind: "svg",
    viewBox: attr(svg, "viewBox"),
    width: attr(svg, "width"),
    height: attr(svg, "height"),
    texts,
    paths,
    polylines,
    rects,
    lines,
    circles
  };
}
"""

_JS_EXTRACT_SVG_EL = r"""
(arg) => {
  const el = arg.el;
  if (!el) return null;

  const svg = (el.tagName && el.tagName.toLowerCase() === "svg")
    ? el
    : (el.querySelector ? el.querySelector("svg") : null);
  if (!svg) return null;

  function attr(el, k) { return (el.getAttribute(k) || "").trim(); }

  const texts = Array.from(svg.querySelectorAll("text")).map(t => ({
    text: (t.textContent || "").trim(),
    x: attr(t, "x"),
    y: attr(t, "y"),
    transform: attr(t, "transform")
  })).filter(x => x.text);

  const paths = Array.from(svg.querySelectorAll("path")).map(p => ({
    d: attr(p, "d"),
    stroke: attr(p, "stroke"),
    fill: attr(p, "fill"),
    className: (p.getAttribute("class") || "").trim()
  })).filter(x => x.d);

  const polylines = Array.from(svg.querySelectorAll("polyline, polygon")).map(p => ({
    points: attr(p, "points"),
    stroke: attr(p, "stroke"),
    fill: attr(p, "fill"),
    className: (p.getAttribute("class") || "").trim()
  })).filter(x => x.points);

  const rects = Array.from(svg.querySelectorAll("rect")).map(r => ({
    x: attr(r,"x"), y: attr(r,"y"),
    width: attr(r,"width"), height: attr(r,"height"),
    fill: attr(r,"fill"), stroke: attr(r,"stroke"),
    className: (r.getAttribute("class") || "").trim()
  })).filter(x => x.width || x.height);

  const lines = Array.from(svg.querySelectorAll("line")).map(l => ({
    x1: attr(l,"x1"), y1: attr(l,"y1"), x2: attr(l,"x2"), y2: attr(l,"y2"),
    stroke: attr(l,"stroke"),
    className: (l.getAttribute("class") || "").trim()
  })).filter(x => x.x1 || x.x2);

  const circles = Array.from(svg.querySelectorAll("circle")).map(c => ({
    cx: attr(c,"cx"), cy: attr(c,"cy"), r: attr(c,"r"),
    fill: attr(c,"fill"), stroke: attr(c,"stroke"),
    className: (c.getAttribute("class") || "").trim()
  })).filter(x => x.r);

  return {
    kind: "svg",
    viewBox: attr(svg, "viewBox"),
    width: attr(svg, "width"),
    height: attr(svg, "height"),
    texts,
    paths,
    polylines,
    rects,
    lines,
    circles
  };
}
"""


class ChartExtractionModule:
    name: str = "charts"

    # Default selectors for common chart libraries and patterns
    DEFAULT_CHART_SELECTORS = [
        # SVG-based charts
        'svg[class*="chart"]', 'svg[class*="Chart"]',
        '[class*="recharts"] svg', '[class*="Recharts"] svg',
        '.plotly svg', '[class*="plotly"] svg',
        '.d3-chart svg', '[class*="d3"] svg',
        '.highcharts-container svg', '[class*="highcharts"] svg',
        '.c3 svg', '[class*="c3"] svg',
        '.bb svg', '[class*="billboard"] svg',
        '.nvd3 svg', '[class*="nvd3"] svg',
        '.apexcharts-svg', '[class*="apexcharts"] svg',
        '.echarts-container svg', '[class*="echarts"] svg',
        '.chartjs-wrapper svg', '[class*="chartjs"] svg',
        # Generic SVG containers
        'svg[viewBox]', '[data-testid*="chart"] svg', '[data-testid*="Chart"] svg',
        '[data-cy*="chart"] svg', '[data-cy*="Chart"] svg',
        # Canvas-based charts
        'canvas[class*="chart"]', 'canvas[class*="Chart"]',
        '.chartjs canvas', '[class*="chartjs"] canvas',
        '.plotly canvas', '[class*="plotly"] canvas',
        '.echarts canvas', '[class*="echarts"] canvas',
        '.highcharts-container canvas', '[class*="highcharts"] canvas',
        '.recharts-wrapper canvas', '[class*="recharts"] canvas',
        '.d3-canvas', '[class*="d3"] canvas',
        'canvas[data-testid*="chart"]', 'canvas[data-testid*="Chart"]',
        # Chart container divs (for screenshots)
        '[class*="chart-container"]', '[class*="ChartContainer"]',
        '[class*="recharts-wrapper"]', '[class*="RechartsWrapper"]',
        '.plotly.js-plot', '[class*="plotly"]',
        '.highcharts-container', '[class*="highcharts"]',
        '.echarts-container', '[class*="echarts"]',
        '.chart-wrapper', '[class*="chart-wrapper"]',
        '[class*="visualization"]', '[class*="Visualization"]',
        '[class*="graph-container"]', '[class*="GraphContainer"]',
        # Specific library containers
        '.apexcharts-canvas', '[class*="apexcharts"]',
        '.c3-chart', '[class*="c3"]',
        '.nvd3-chart', '[class*="nvd3"]',
        '.bb-chart', '[class*="billboard"]',
        '.vis-graph', '[class*="vis"]',
        '.sigma-container', '[class*="sigma"]',
        '.g6-graph', '[class*="g6"]',
        # Data attributes
        '[data-chart-type]', '[data-chart]', '[data-viz]',
        '[role="img"][class*="chart"]', '[role="img"][class*="Chart"]',
    ]

    async def _detect_chart_selectors(self, page: Page, max_charts: int, job: JobEnvelope) -> list[str]:
        """
        Auto-detect chart selectors on the page.
        Covers SVG charts, Canvas charts, and chart containers.
        """
        logger.bind(ctx=log_ctx(job)).info(
            "chart_detection_starting max_charts={}",
            max_charts,
        )

        js = """
        (maxCharts) => {
            const selectors = [];
            const seen = new Set();
            const stats = { svgs_checked: 0, svgs_found: 0, canvases_checked: 0, canvases_found: 0, containers_found: 0 };

            function addSelector(sel, reason) {
                if (!sel || seen.has(sel)) return;
                seen.add(sel);
                selectors.push({ selector: sel, reason: reason });
            }

            function getStableSelector(el) {
                if (!el) return null;

                // Prefer ID (but only if it's a valid CSS identifier)
                // CSS IDs cannot start with a digit
                if (el.id && /^[a-zA-Z_][a-zA-Z0-9_-]*$/.test(el.id)) {
                    return '#' + el.id;
                }
                // If ID starts with digit, use attribute selector instead
                if (el.id) {
                    return `[id="${el.id}"]`;
                }

                // Prefer data-testid or data-cy
                const testId = el.getAttribute('data-testid');
                if (testId) return `[data-testid="${testId}"]`;
                const dataCy = el.getAttribute('data-cy');
                if (dataCy) return `[data-cy="${dataCy}"]`;

                // Prefer specific chart-related classes
                if (el.className && typeof el.className === 'string') {
                    const classes = el.className.split(' ').filter(c =>
                        c && !c.includes(':') && c.length > 2
                    );
                    // Prioritize classes with chart-related keywords
                    const chartClasses = classes.filter(c =>
                        /chart|graph|viz|plot|recharts|plotly|d3|highcharts|echarts|apex|canvas/i.test(c)
                    );
                    const otherClasses = classes.filter(c =>
                        !/chart|graph|viz|plot|recharts|plotly|d3|highcharts|echarts|apex|canvas/i.test(c)
                    );
                    const prioritized = [...chartClasses, ...otherClasses].slice(0, 3);

                    if (prioritized.length > 0) {
                        const classSel = '.' + prioritized.join('.');
                        try {
                            const matches = document.querySelectorAll(classSel);
                            if (matches.length === 1 || matches[0] === el) {
                                return classSel;
                            }
                        } catch (e) {}
                    }
                }

                // Build path-based selector
                const path = [];
                let curr = el;
                let depth = 0;
                while (curr && curr !== document.body && depth < 5) {
                    let part = curr.tagName.toLowerCase();
                    if (curr.id) {
                        // Use attribute selector for IDs starting with digit
                        if (/^[0-9]/.test(curr.id)) {
                            part = `[id="${curr.id}"]`;
                        } else {
                            part = '#' + curr.id;
                        }
                        path.unshift(part);
                        break;
                    }
                    if (curr.className && typeof el.className === 'string') {
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

            // 1. Find SVG charts
            const svgs = Array.from(document.querySelectorAll('svg'));
            stats.svgs_checked = svgs.length;
            for (const svg of svgs) {
                // Skip tiny SVGs (likely icons)
                const rect = svg.getBoundingClientRect();
                if (rect.width < 100 || rect.height < 100) continue;

                // Check if it looks like a chart
                const hasChartContent =
                    svg.querySelectorAll('path, rect, circle, line, polyline').length > 5 ||
                    /chart|graph|plot|viz/i.test(svg.className || '') ||
                    /chart|graph|plot|viz/i.test(svg.parentElement?.className || '');

                if (hasChartContent) {
                    const sel = getStableSelector(svg);
                    if (sel) {
                        addSelector(sel, 'svg_chart');
                        stats.svgs_found++;
                    }
                }
            }

            // 2. Find Canvas charts
            const canvases = Array.from(document.querySelectorAll('canvas'));
            stats.canvases_checked = canvases.length;
            for (const canvas of canvases) {
                // Skip tiny canvases
                const rect = canvas.getBoundingClientRect();
                if (rect.width < 100 || rect.height < 100) continue;

                // Check if it looks like a chart
                const hasChartClass = /chart|graph|plot|viz|recharts|plotly|d3|highcharts|echarts|apex|canvas/i.test(canvas.className || '');
                const parentHasChartClass = /chart|graph|plot|viz|recharts|plotly|d3|highcharts|echarts|apex|canvas/i.test(canvas.parentElement?.className || '');

                if (hasChartClass || parentHasChartClass) {
                    const sel = getStableSelector(canvas);
                    if (sel) {
                        addSelector(sel, 'canvas_chart');
                        stats.canvases_found++;
                    }
                }
            }

            // 3. Find chart containers (divs with chart-like content)
            const chartContainers = Array.from(document.querySelectorAll(
                '[class*="chart"], [class*="Chart"], [class*="graph"], [class*="Graph"], ' +
                '[class*="plot"], [class*="Plot"], [class*="viz"], [class*="Viz"], ' +
                '[class*="recharts"], [class*="plotly"], [class*="highcharts"], ' +
                '[class*="echarts"], [class*="apexcharts"], [class*="d3"], ' +
                '[class*="visualization"], [class*="Visualization"]'
            ));

            for (const container of chartContainers) {
                // Skip if already captured as SVG or canvas
                if (container.querySelector('svg') || container.querySelector('canvas')) {
                    continue;
                }

                const rect = container.getBoundingClientRect();
                if (rect.width < 100 || rect.height < 100) continue;

                const sel = getStableSelector(container);
                if (sel) {
                    addSelector(sel, 'chart_container');
                    stats.containers_found++;
                }
            }

            // 4. Find elements with chart-related data attributes
            const dataChartElements = Array.from(document.querySelectorAll(
                '[data-chart-type], [data-chart], [data-viz], [data-graph]'
            ));
            for (const el of dataChartElements) {
                const rect = el.getBoundingClientRect();
                if (rect.width < 100 || rect.height < 100) continue;

                const sel = getStableSelector(el);
                if (sel) addSelector(sel, 'data_attr_chart');
            }

            // Return top selectors
            return {
                selectors: selectors.slice(0, maxCharts).map(s => s.selector),
                stats: stats
            };
        }
        """
        try:
            result = await page.evaluate(js, max_charts)
            selectors = result.get("selectors", []) if isinstance(result, dict) else []
            stats = result.get("stats", {}) if isinstance(result, dict) else {}

            logger.bind(ctx=log_ctx(job)).info(
                "chart_detection_complete selectors_found={} svgs_checked={} svgs_found={} canvases_checked={} canvases_found={} containers_found={}",
                len(selectors),
                stats.get("svgs_checked", 0),
                stats.get("svgs_found", 0),
                stats.get("canvases_checked", 0),
                stats.get("canvases_found", 0),
                stats.get("containers_found", 0),
            )
            return selectors
        except Exception as e:
            logger.bind(ctx=log_ctx(job)).warning("chart_detection_failed err={}", str(e))
            return []

    async def _try_extract_with_fallbacks(
        self,
        page: Page,
        selector: str,
        job: JobEnvelope,
        screenshot_canvas: bool,
        hover_before_extract: bool = False,
        hover_selector: Optional[str] = None,
    ) -> Optional[Artifact]:
        """
        Try to extract chart from selector with fallback strategies:
        1. Optional: Hover over element to reveal data
        2. Try SVG extraction
        3. Try Canvas extraction (if screenshot_canvas enabled)
        4. Try screenshot of container
        """
        logger.bind(ctx=log_ctx(job)).debug(
            "chart_extraction_attempting selector={} hover={}",
            selector,
            hover_before_extract,
        )

        loc = _locator_for(page, selector).first

        # Check if element exists
        try:
            if await loc.count() == 0:
                logger.bind(ctx=log_ctx(job)).debug(
                    "chart_element_not_found selector={}",
                    selector,
                )
                return None
        except Exception as e:
            logger.bind(ctx=log_ctx(job)).debug(
                "chart_locator_failed selector={} err={}",
                selector,
                str(e),
            )
            return None

        # Strategy 0: Hover to reveal data (if enabled)
        if hover_before_extract:
            try:
                hover_target = _locator_for(page, hover_selector).first if hover_selector else loc
                logger.bind(ctx=log_ctx(job)).info(
                    "chart_hovering selector={} hover_selector={}",
                    selector,
                    hover_selector or "same",
                )
                await hover_target.hover()
                await asyncio.sleep(0.5)  # Wait for hover animation/data to appear
                logger.bind(ctx=log_ctx(job)).debug(
                    "chart_hover_complete selector={}",
                    selector,
                )
            except Exception as e:
                logger.bind(ctx=log_ctx(job)).debug(
                    "chart_hover_failed selector={} err={}",
                    selector,
                    str(e),
                )
                # Continue with extraction even if hover fails

        # Strategy 1: SVG extraction
        try:
            svg_data = None
            if _is_xpath(selector):
                h = await loc.element_handle()
                if h:
                    try:
                        svg_data = await page.evaluate(_JS_EXTRACT_SVG_EL, {"el": h})
                    finally:
                        try:
                            await h.dispose()
                        except Exception:
                            pass
            else:
                svg_data = await page.evaluate(_JS_EXTRACT_SVG, {"selector": selector})

            if svg_data and (svg_data.get("texts") or svg_data.get("paths")):
                text_count = len(svg_data.get("texts", []))
                path_count = len(svg_data.get("paths", []))
                logger.bind(ctx=log_ctx(job)).info(
                    "chart_svg_extracted selector={} texts={} paths={}",
                    selector,
                    text_count,
                    path_count,
                )
                return Artifact(
                    kind=ArtifactKind.chart,
                    label="svg_chart",
                    source_url=str(job.plan.url),
                    selector=selector,
                    data=svg_data,
                    meta={
                        "extraction_method": "svg_parse",
                        "svg_texts": text_count,
                        "svg_paths": path_count,
                        "hover_used": hover_before_extract,
                    },
                )
            else:
                logger.bind(ctx=log_ctx(job)).debug(
                    "chart_svg_no_content selector={}",
                    selector,
                )
        except Exception as e:
            logger.bind(ctx=log_ctx(job)).debug(
                "chart_svg_extract_failed selector={} err={}",
                selector,
                str(e),
            )

        # Strategy 2: Canvas extraction (screenshot)
        if screenshot_canvas:
            try:
                # Check if element is visible and large enough
                box = await loc.bounding_box()
                if box and box.get("width", 0) >= 50 and box.get("height", 0) >= 50:
                    width = int(box.get("width", 0))
                    height = int(box.get("height", 0))
                    logger.bind(ctx=log_ctx(job)).info(
                        "chart_screenshotting selector={} width={} height={}",
                        selector,
                        width,
                        height,
                    )
                    img = await loc.screenshot(type="png")
                    artifact = await upload_bytes_public_s3(
                        job=job,
                        data=img,
                        filename=f"chart_canvas_{hash(selector) % 10000}.png",
                        mime="image/png",
                        kind=ArtifactKind.chart,
                        label="canvas_chart_screenshot",
                        selector=selector,
                        meta={
                            "extraction_method": "canvas_screenshot",
                            "fallback": True,
                            "width": width,
                            "height": height,
                            "hover_used": hover_before_extract,
                        },
                    )
                    logger.bind(ctx=log_ctx(job)).info(
                        "chart_screenshot_complete selector={} size_bytes={}",
                        selector,
                        len(img),
                    )
                    return artifact
                else:
                    logger.bind(ctx=log_ctx(job)).debug(
                        "chart_too_small_for_screenshot selector={} width={} height={}",
                        selector,
                        box.get("width", 0) if box else 0,
                        box.get("height", 0) if box else 0,
                    )
            except Exception as e:
                logger.bind(ctx=log_ctx(job)).debug(
                    "chart_screenshot_failed selector={} err={}",
                    selector,
                    str(e),
                )

        logger.bind(ctx=log_ctx(job)).debug(
            "chart_all_extraction_strategies_failed selector={}",
            selector,
        )
        return None

    async def run(self, *, job: JobEnvelope, page: Page, plan: object, tracker) -> ModuleResult:
        browser = getattr(plan, "browser", None)
        if not browser:
            logger.bind(ctx=log_ctx(job)).debug("chart_extraction_skipped_no_browser")
            return ModuleResult(artifacts=[])

        ccfg = getattr(browser, "charts", None)
        if not ccfg or not getattr(ccfg, "enabled", False):
            logger.bind(ctx=log_ctx(job)).debug("chart_extraction_skipped_disabled")
            return ModuleResult(artifacts=[])

        selectors = list(getattr(ccfg, "selectors", []) or [])
        max_charts = int(getattr(ccfg, "max_charts", 0) or 0)
        screenshot_canvas = bool(getattr(ccfg, "screenshot_canvas", True))
        auto_detect = bool(getattr(ccfg, "auto_detect", True))
        use_default_fallbacks = bool(getattr(ccfg, "use_default_fallbacks", True))
        hover_before_extract = bool(getattr(ccfg, "hover_before_extract", False))
        hover_selector = getattr(ccfg, "hover_selector", None)

        logger.bind(ctx=log_ctx(job)).info(
            "chart_extraction_starting max_charts={} user_selectors={} auto_detect={} hover={}",
            max_charts,
            len(selectors),
            auto_detect,
            hover_before_extract,
        )

        if max_charts <= 0:
            logger.bind(ctx=log_ctx(job)).warning("chart_extraction_invalid_max_charts max_charts={}", max_charts)
            return ModuleResult(artifacts=[])

        # Build selector list: user provided -> auto-detected -> default fallbacks
        final_selectors: list[str] = []
        selector_sources: dict[str, str] = {}  # Track where each selector came from

        # 1. Use user-provided selectors
        if selectors:
            for sel in selectors:
                if sel not in selector_sources:
                    final_selectors.append(sel)
                    selector_sources[sel] = "user"
            logger.bind(ctx=log_ctx(job)).info(
                "chart_user_selectors_added count={}",
                len(selectors),
            )

        # 2. Auto-detect if enabled and no user selectors (or if explicitly requested)
        if auto_detect and (not selectors or getattr(ccfg, "auto_detect_always", False)):
            detected = await self._detect_chart_selectors(page, max_charts, job)
            if detected:
                for sel in detected:
                    if sel not in selector_sources:
                        final_selectors.append(sel)
                        selector_sources[sel] = "auto_detected"
                logger.bind(ctx=log_ctx(job)).info(
                    "chart_auto_detected_added count={}",
                    len(detected),
                )

        # 3. Add default fallback selectors if enabled and still need more
        if use_default_fallbacks and len(final_selectors) < max_charts:
            defaults_added = 0
            for default_sel in self.DEFAULT_CHART_SELECTORS:
                if default_sel not in selector_sources:
                    final_selectors.append(default_sel)
                    selector_sources[default_sel] = "default_fallback"
                    defaults_added += 1
                    if len(final_selectors) >= max_charts * 2:  # Limit fallbacks
                        break
            logger.bind(ctx=log_ctx(job)).info(
                "chart_default_fallbacks_added count={}",
                defaults_added,
            )

        if not final_selectors:
            logger.bind(ctx=log_ctx(job)).warning("chart_extraction_no_selectors")
            return ModuleResult(artifacts=[])

        logger.bind(ctx=log_ctx(job)).info(
            "chart_extraction_final_selectors total={} unique={}",
            len(final_selectors),
            len(set(final_selectors)),
        )

        artifacts: List[Artifact] = []
        seen_selectors: set[str] = set()
        extraction_stats = {
            "attempted": 0,
            "successful": 0,
            "svg_extracted": 0,
            "canvas_screenshot": 0,
            "failed": 0,
        }

        for sel in final_selectors:
            if len(artifacts) >= max_charts:
                logger.bind(ctx=log_ctx(job)).info(
                    "chart_extraction_max_reached count={}",
                    len(artifacts),
                )
                break

            if sel in seen_selectors:
                continue
            seen_selectors.add(sel)
            extraction_stats["attempted"] += 1

            try:
                artifact = await self._try_extract_with_fallbacks(
                    page=page,
                    selector=sel,
                    job=job,
                    screenshot_canvas=screenshot_canvas,
                    hover_before_extract=hover_before_extract,
                    hover_selector=hover_selector,
                )
                if artifact:
                    artifacts.append(artifact)
                    extraction_stats["successful"] += 1
                    if artifact.label == "svg_chart":
                        extraction_stats["svg_extracted"] += 1
                    elif artifact.label == "canvas_chart_screenshot":
                        extraction_stats["canvas_screenshot"] += 1

                    logger.bind(ctx=log_ctx(job)).info(
                        "chart_extracted selector={} label={} source={} method={}",
                        sel,
                        artifact.label,
                        selector_sources.get(sel, "unknown"),
                        artifact.meta.get("extraction_method", "unknown"),
                    )
                else:
                    extraction_stats["failed"] += 1
                    logger.bind(ctx=log_ctx(job)).debug(
                        "chart_extraction_no_result selector={} source={}",
                        sel,
                        selector_sources.get(sel, "unknown"),
                    )

            except Exception as e:
                extraction_stats["failed"] += 1
                logger.bind(ctx=log_ctx(job)).warning(
                    "chart_extract_exception selector={} source={} err={}",
                    sel,
                    selector_sources.get(sel, "unknown"),
                    str(e)[:200],
                )
                continue

        # Log comprehensive summary
        logger.bind(ctx=log_ctx(job)).info(
            "chart_extraction_complete "
            "artifacts={} attempted={} successful={} svg={} canvas={} failed={} "
            "methods={}",
            len(artifacts),
            extraction_stats["attempted"],
            extraction_stats["successful"],
            extraction_stats["svg_extracted"],
            extraction_stats["canvas_screenshot"],
            extraction_stats["failed"],
            [a.meta.get("extraction_method", "unknown") for a in artifacts],
        )

        return ModuleResult(artifacts=artifacts)
