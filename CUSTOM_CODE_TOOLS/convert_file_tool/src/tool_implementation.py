"""
Convert File Tool
=================
Convert tabular data between CSV, JSON, JSONL, Parquet, Excel, TSV, and TXT.

Two modes:
  - Inline:  pass 'content' (text for csv/json/jsonl/tsv/txt) and get converted text back.
  - File:    pass 'input_path' / 'output_path' to read and write workspace files.

Parquet needs pyarrow and Excel needs openpyxl. Those imports are deferred to
the actual conversion paths so that this module imports cleanly even if those
optional deps are missing. CSV<->JSON works on the base runtime alone.
"""

import io
import json
import os

from aidputils.agents.tools.custom_tools.base import CustomToolBase
from .utils.config_utils import get_cfg

# Debug channel — fall back to no-op stubs if the runtime doesn't inject them.
try:
    from aidp_debug import debug, debug_warn, debug_error, DebugLog
except ImportError:
    def debug(*a, **k): pass
    def debug_warn(*a, **k): pass
    def debug_error(*a, **k): pass
    class DebugLog:
        @staticmethod
        def embed(result):
            return result

_FORMATS = {"csv", "json", "jsonl", "parquet", "excel", "xlsx", "tsv", "txt"}
_TEXT_FORMATS = {"csv", "json", "jsonl", "tsv", "txt"}
_BINARY_FORMATS = {"parquet", "excel", "xlsx"}


def _ok(data):
    """Success envelope. Mirrors top-level legacy fields for back-compat."""
    out = {"ok": True, "data": data}
    if isinstance(data, dict):
        for k, v in data.items():
            if k not in out:
                out[k] = v
    return out


def _err(msg, error_type="ToolError"):
    return {"ok": False, "error": str(msg), "error_type": error_type, "isError": True}


@CustomToolBase.register
class ConvertFileTool(CustomToolBase):
    """Convert tabular data between csv, json, jsonl, parquet, excel, tsv, txt.
    Provide either inline 'content' (for text formats) or 'input_path'/'output_path'
    for workspace files. Use to reshape data files between flow steps or to prep
    an export."""

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        debug("ConvertFileTool._execute_tool: start", params_keys=list(runtime_params.keys()))

        from_fmt = _norm(runtime_params.get("from_format", ""))
        to_fmt = _norm(runtime_params.get("to_format", ""))
        if from_fmt not in _FORMATS or to_fmt not in _FORMATS:
            debug_error("invalid format", from_fmt=from_fmt, to_fmt=to_fmt)
            return DebugLog.embed(_err(
                f"from_format and to_format must each be one of: {sorted(_FORMATS)}",
                "ValidationError",
            ))

        content = runtime_params.get("content")
        input_path = runtime_params.get("input_path")
        output_path = runtime_params.get("output_path")
        base_dir = get_cfg(conf, "base_dir", "/workspace")
        max_rows = get_cfg(conf, "max_rows", 100000)
        max_bytes = get_cfg(conf, "max_bytes", 100 * 1024 * 1024)  # 100 MiB

        debug("config resolved", base_dir=base_dir, max_rows=max_rows, max_bytes=max_bytes,
              from_fmt=from_fmt, to_fmt=to_fmt)

        try:
            import pandas as pd
        except Exception as e:
            debug_error("pandas import failed", err=str(e))
            return DebugLog.embed(_err(f"pandas not available: {e}", "DependencyError"))

        truncated = False

        try:
            # ---- read with byte/row bounds ----
            if input_path:
                src = _safe_join(base_dir, input_path)
                if src is None:
                    debug_error("invalid input_path", input_path=input_path)
                    return DebugLog.embed(_err("invalid input_path", "ValidationError"))
                # Pre-check file size for binary formats and as a guard for text ones.
                try:
                    fsize = os.path.getsize(src)
                    if fsize > max_bytes:
                        debug_error("input file too large", size=fsize, max_bytes=max_bytes)
                        return DebugLog.embed(_err(
                            f"input file is {fsize} bytes, exceeds max_bytes={max_bytes}",
                            "SizeLimitError",
                        ))
                except OSError as e:
                    debug_warn("getsize failed", err=str(e))
                df, read_truncated = _read(pd, from_fmt, path=src, max_rows=max_rows, max_bytes=max_bytes)
            elif content is not None:
                if isinstance(content, str) and len(content.encode("utf-8", errors="ignore")) > max_bytes:
                    debug_error("inline content too large", max_bytes=max_bytes)
                    return DebugLog.embed(_err(
                        f"inline content exceeds max_bytes={max_bytes}",
                        "SizeLimitError",
                    ))
                df, read_truncated = _read(pd, from_fmt, text=content, max_rows=max_rows, max_bytes=max_bytes)
            else:
                debug_error("no source provided")
                return DebugLog.embed(_err("provide either 'content' or 'input_path'", "ValidationError"))

            truncated = truncated or read_truncated

            # Final row-cap guard (handles formats without native chunking).
            if len(df) > max_rows:
                debug_warn("row cap hit post-read", rows=len(df), max_rows=max_rows)
                df = df.head(max_rows)
                truncated = True

            debug("read complete", rows=len(df), cols=len(df.columns), truncated=truncated)

            # ---- write ----
            if output_path:
                dst = _safe_join(base_dir, output_path)
                if dst is None:
                    debug_error("invalid output_path", output_path=output_path)
                    return DebugLog.embed(_err("invalid output_path", "ValidationError"))
                parent = os.path.dirname(dst)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                _write(df, to_fmt, path=dst)
                data = {
                    "rows": len(df),
                    "columns": list(df.columns),
                    "output_path": output_path,
                    "from_format": from_fmt,
                    "to_format": to_fmt,
                    "truncated": truncated,
                }
                debug("write complete (file)", **{k: data[k] for k in ("rows", "output_path", "to_format", "truncated")})
                return DebugLog.embed(_ok(data))
            else:
                if to_fmt in _BINARY_FORMATS:
                    debug_error("binary output without output_path", to_fmt=to_fmt)
                    return DebugLog.embed(_err(
                        f"{to_fmt} output requires an output_path (binary format)",
                        "ValidationError",
                    ))
                out_text = _write(df, to_fmt, text=True)
                data = {
                    "rows": len(df),
                    "columns": list(df.columns),
                    "content": out_text,
                    "from_format": from_fmt,
                    "to_format": to_fmt,
                    "truncated": truncated,
                }
                debug("write complete (inline)", rows=len(df), to_fmt=to_fmt, truncated=truncated)
                return DebugLog.embed(_ok(data))
        except Exception as e:
            debug_error("conversion failed", err=str(e), err_type=type(e).__name__)
            return DebugLog.embed(_err(str(e), type(e).__name__))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _norm(fmt):
    fmt = (fmt or "").strip().lower()
    if fmt == "xls":
        return "excel"
    return fmt


def _read(pd, fmt, path=None, text=None, max_rows=100000, max_bytes=100 * 1024 * 1024):
    """Returns (df, truncated). Uses chunked reads where possible to bound memory."""
    truncated = False

    if fmt == "csv" or fmt == "tsv" or fmt == "txt":
        sep = "," if fmt == "csv" else "\t"
        src = path if path else io.StringIO(text)
        # Chunked read so a giant CSV doesn't blow up memory before the row cap.
        try:
            chunks = []
            collected = 0
            reader = pd.read_csv(src, sep=sep, chunksize=max(1000, min(max_rows, 50000)))
            for chunk in reader:
                remaining = max_rows - collected
                if remaining <= 0:
                    truncated = True
                    break
                if len(chunk) > remaining:
                    chunks.append(chunk.iloc[:remaining])
                    collected += remaining
                    truncated = True
                    break
                chunks.append(chunk)
                collected += len(chunk)
            if not chunks:
                return pd.DataFrame(), truncated
            return pd.concat(chunks, ignore_index=True), truncated
        except TypeError:
            # Older pandas that doesn't accept chunksize on StringIO — fall back.
            src2 = path if path else io.StringIO(text)
            df = pd.read_csv(src2, sep=sep, nrows=max_rows + 1)
            if len(df) > max_rows:
                df = df.head(max_rows)
                truncated = True
            return df, truncated

    if fmt == "json":
        if path:
            with open(path) as f:
                data = json.load(f)
        else:
            data = json.loads(text)
        if isinstance(data, dict):
            for k in ("rows", "data", "items", "records"):
                if isinstance(data.get(k), list):
                    data = data[k]
                    break
        rows = data if isinstance(data, list) else [data]
        if len(rows) > max_rows:
            rows = rows[:max_rows]
            truncated = True
        return pd.DataFrame(rows), truncated

    if fmt == "jsonl":
        if path:
            with open(path, "r") as f:
                lines = []
                for i, line in enumerate(f):
                    if i >= max_rows:
                        truncated = True
                        break
                    line = line.strip()
                    if line:
                        lines.append(json.loads(line))
        else:
            lines = []
            for i, line in enumerate((text or "").splitlines()):
                if i >= max_rows:
                    truncated = True
                    break
                line = line.strip()
                if line:
                    lines.append(json.loads(line))
        return pd.DataFrame(lines), truncated

    if fmt == "parquet":
        # Lazy import — module loads even if pyarrow isn't installed.
        try:
            import pyarrow  # noqa: F401
        except ImportError as e:
            raise RuntimeError(f"parquet requires pyarrow: {e}")
        df = pd.read_parquet(path)
        if len(df) > max_rows:
            df = df.head(max_rows)
            truncated = True
        return df, truncated

    if fmt in ("excel", "xlsx"):
        # Lazy import — module loads even if openpyxl isn't installed.
        try:
            import openpyxl  # noqa: F401
        except ImportError as e:
            raise RuntimeError(f"excel requires openpyxl: {e}")
        df = pd.read_excel(path, nrows=max_rows + 1)
        if len(df) > max_rows:
            df = df.head(max_rows)
            truncated = True
        return df, truncated

    raise ValueError(f"cannot read format {fmt}")


def _write(df, fmt, path=None, text=False):
    if fmt == "csv" or fmt == "tsv" or fmt == "txt":
        sep = "," if fmt == "csv" else "\t"
        if text:
            return df.to_csv(index=False, sep=sep)
        df.to_csv(path, index=False, sep=sep)
        return None
    if fmt == "json":
        records = df.to_dict(orient="records")
        if text:
            return json.dumps(records, default=str, indent=2)
        with open(path, "w") as f:
            json.dump(records, f, default=str, indent=2)
        return None
    if fmt == "jsonl":
        records = df.to_dict(orient="records")
        if text:
            return "\n".join(json.dumps(r, default=str) for r in records)
        with open(path, "w") as f:
            for r in records:
                f.write(json.dumps(r, default=str))
                f.write("\n")
        return None
    if fmt == "parquet":
        try:
            import pyarrow  # noqa: F401
        except ImportError as e:
            raise RuntimeError(f"parquet requires pyarrow: {e}")
        df.to_parquet(path, index=False)
        return None
    if fmt in ("excel", "xlsx"):
        try:
            import openpyxl  # noqa: F401
        except ImportError as e:
            raise RuntimeError(f"excel requires openpyxl: {e}")
        df.to_excel(path, index=False)
        return None
    raise ValueError(f"cannot write format {fmt}")


def _safe_join(base_dir, rel):
    full = os.path.normpath(os.path.join(base_dir, rel))
    if not full.startswith(os.path.normpath(base_dir)):
        return None
    return full
