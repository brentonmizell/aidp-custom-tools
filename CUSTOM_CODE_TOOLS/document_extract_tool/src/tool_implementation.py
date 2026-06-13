"""
Document Extract Tool
=====================
Extract clean text (and simple table text) from a document in a specific format:
PDF, DOCX, CSV, or plain text.

Input is the file's bytes as base64 (so it works with whatever fetched the file:
the Catalog/Workspace file tools return content you can pass here, or pass raw
base64 directly). Keeping extraction separate from fetching means one extractor
works for files from volumes, the workspace, object storage, or an HTTP tool.

Format is taken from the `format` parameter, or guessed from a `filename`.
"""

import base64
import io

from aidputils.agents.tools.custom_tools.base import CustomToolBase
from .utils.config_utils import get_cfg

# Debug Channel — optional in runtime; provide no-op fallback so the tool still
# works if the runtime hasn't injected aidp_debug.
try:
    from aidp_debug import debug, debug_warn, debug_error, DebugLog  # type: ignore
except ImportError:  # pragma: no cover
    def debug(*args, **kwargs):
        pass

    def debug_warn(*args, **kwargs):
        pass

    def debug_error(*args, **kwargs):
        pass

    class DebugLog:  # type: ignore
        @staticmethod
        def embed(result):
            return result


def _ok(data, **extra):
    """Build a success envelope while preserving legacy top-level keys."""
    payload = {"ok": True, "data": data}
    # Preserve legacy top-level keys for callers that read them directly.
    if isinstance(data, dict):
        for k, v in data.items():
            if k not in payload:
                payload[k] = v
    payload.update(extra)
    return payload


def _err(msg, error_type="ToolError", **extra):
    payload = {"ok": False, "error": msg, "error_type": error_type}
    payload.update(extra)
    return payload


@CustomToolBase.register
class DocumentExtractTool(CustomToolBase):
    """Extract text from a document (PDF, DOCX, CSV, or text). Pass the file as
    base64 in `content_base64` (or plain text in `content`) plus the `format`
    (or a `filename` to infer it). Use to turn a document into text an agent can
    read or feed into RAG."""

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        fmt = (runtime_params.get("format", "") or "").lower().lstrip(".")
        filename = runtime_params.get("filename", "")
        if not fmt and filename and "." in filename:
            fmt = filename.rsplit(".", 1)[-1].lower()

        text_content = runtime_params.get("content", "")
        b64 = runtime_params.get("content_base64", "")

        max_chars = get_cfg(conf, "max_chars", 100000)
        max_csv_rows = get_cfg(conf, "max_csv_rows", 1000)
        max_input_bytes = get_cfg(conf, "max_input_bytes", 26214400)  # 25 MiB

        page_start = runtime_params.get("page_start")
        page_end = runtime_params.get("page_end")
        try:
            page_start = int(page_start) if page_start is not None else None
        except (TypeError, ValueError):
            page_start = None
        try:
            page_end = int(page_end) if page_end is not None else None
        except (TypeError, ValueError):
            page_end = None

        debug(
            "DocumentExtractTool start",
            format=fmt,
            filename=filename,
            has_b64=bool(b64),
            has_text=bool(text_content),
            page_start=page_start,
            page_end=page_end,
        )

        # Resolve to bytes (or text for the text/csv paths).
        raw = None
        if b64:
            try:
                raw = base64.b64decode(b64)
            except Exception as e:
                debug_error("base64 decode failed", error=str(e))
                result = _err("content_base64 is not valid base64", "InvalidInput")
                return DebugLog.embed(result)
        elif text_content:
            raw = text_content.encode("utf-8")
        else:
            debug_warn("no content supplied")
            result = _err("provide content_base64 (binary) or content (text)", "InvalidInput")
            return DebugLog.embed(result)

        # Byte cap on input.
        input_truncated = False
        if raw is not None and len(raw) > max_input_bytes:
            debug_warn(
                "input bytes exceeded cap; truncating",
                size=len(raw),
                cap=max_input_bytes,
            )
            raw = raw[:max_input_bytes]
            input_truncated = True

        if not fmt:
            debug_warn("format missing")
            result = _err(
                "format is required (pdf, docx, csv, or txt), or pass a filename to infer it",
                "InvalidInput",
            )
            return DebugLog.embed(result)

        try:
            if fmt == "pdf":
                text, pages, page_count, pages_truncated = cls._extract_pdf(
                    raw, page_start, page_end
                )
                data = {
                    "format": "pdf",
                    "pages": pages,
                    "page_count": page_count,
                    "chars": len(text),
                    "text": text[:max_chars],
                    "truncated": len(text) > max_chars or pages_truncated or input_truncated,
                }
                debug("pdf extracted", pages=pages, page_count=page_count, chars=len(text))
                return DebugLog.embed(_ok(data))

            if fmt in ("docx", "doc"):
                text, tables = cls._extract_docx(raw)
                data = {
                    "format": "docx",
                    "tables": len(tables),
                    "chars": len(text),
                    "text": text[:max_chars],
                    "table_text": tables[:50],
                    "truncated": len(text) > max_chars or len(tables) > 50 or input_truncated,
                }
                debug("docx extracted", chars=len(text), tables=len(tables))
                return DebugLog.embed(_ok(data))

            if fmt == "csv":
                import csv

                rows = list(csv.reader(io.StringIO(raw.decode("utf-8", "replace"))))
                header = rows[0] if rows else []
                records = [dict(zip(header, r)) for r in rows[1:]] if header else []
                total_records = len(records)
                capped = records[:max_csv_rows]
                truncated = total_records > max_csv_rows or input_truncated
                data = {
                    "format": "csv",
                    "columns": header,
                    "row_count": total_records,
                    "rows": capped,
                    "returned_rows": len(capped),
                    "truncated": truncated,
                }
                debug(
                    "csv extracted",
                    columns=len(header),
                    row_count=total_records,
                    returned=len(capped),
                    truncated=truncated,
                )
                return DebugLog.embed(_ok(data))

            if fmt in ("txt", "text", "md", "json", "log"):
                text = raw.decode("utf-8", "replace")
                data = {
                    "format": fmt,
                    "chars": len(text),
                    "text": text[:max_chars],
                    "truncated": len(text) > max_chars or input_truncated,
                }
                debug("text extracted", format=fmt, chars=len(text))
                return DebugLog.embed(_ok(data))

            debug_warn("unsupported format", format=fmt)
            result = _err(
                f"unsupported format '{fmt}'. Supported: pdf, docx, csv, txt/md/json.",
                "InvalidInput",
            )
            return DebugLog.embed(result)
        except Exception as e:
            debug_error("extraction failed", format=fmt, error=str(e))
            result = _err(str(e), type(e).__name__)
            return DebugLog.embed(result)

    # ------------------------------------------------------------------ #
    @classmethod
    def _extract_pdf(cls, raw, page_start=None, page_end=None):
        """Extract text from a PDF, optionally restricted to a 1-based page range.

        Returns (text, pages_extracted, total_pages, range_truncated).
        Imports are lazy so callers using non-PDF formats don't pay the cost.
        """
        # Prefer pdfplumber (better layout + tables); fall back to pypdf.
        try:
            import pdfplumber  # lazy
            with pdfplumber.open(io.BytesIO(raw)) as pdf:
                total = len(pdf.pages)
                start_idx, end_idx, truncated = cls._resolve_page_range(
                    total, page_start, page_end
                )
                parts = []
                for i in range(start_idx, end_idx):
                    parts.append(pdf.pages[i].extract_text() or "")
            return "\n\n".join(parts).strip(), (end_idx - start_idx), total, truncated
        except Exception as e:
            debug_warn("pdfplumber failed; falling back to pypdf", error=str(e))

        from pypdf import PdfReader  # lazy
        reader = PdfReader(io.BytesIO(raw))
        total = len(reader.pages)
        start_idx, end_idx, truncated = cls._resolve_page_range(
            total, page_start, page_end
        )
        parts = [(reader.pages[i].extract_text() or "") for i in range(start_idx, end_idx)]
        return "\n\n".join(parts).strip(), (end_idx - start_idx), total, truncated

    @staticmethod
    def _resolve_page_range(total, page_start, page_end):
        """Resolve 1-based inclusive page_start/page_end into 0-based [start, end).

        Returns (start_idx, end_idx, truncated_flag). truncated is True when the
        caller supplied a range that was clamped to fit the document.
        """
        if total <= 0:
            return 0, 0, False
        truncated = False
        start_idx = 0 if page_start is None else max(0, page_start - 1)
        end_idx = total if page_end is None else min(total, page_end)
        if page_start is not None and page_start - 1 > total - 1:
            truncated = True
            start_idx = total
        if page_end is not None and page_end > total:
            truncated = True
        if end_idx < start_idx:
            end_idx = start_idx
        return start_idx, end_idx, truncated

    @classmethod
    def _extract_docx(cls, raw):
        from docx import Document  # lazy
        doc = Document(io.BytesIO(raw))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        tables = []
        for t in doc.tables:
            for row in t.rows:
                cells = [c.text.strip() for c in row.cells]
                if any(cells):
                    tables.append(" | ".join(cells))
        return "\n".join(paragraphs).strip(), tables
