"""
Data Ops Toolkit
================
Three tools that operate on tabular data (a list of record objects):

  FilterTool          - keep records matching a condition
  CompareTool         - diff two record sets, report added/removed/changed
  DataManipulationTool- group-by / aggregate / sort / dedupe / select via pandas

All three accept data as a list of objects or a JSON string encoding one.

v1.2.0 adds optional AIDP I/O via source_uri / dest_uri:

  source_uri  - read input from an AIDP target instead of inline `data`/`left`/`right`
  dest_uri    - write the structured output back to an AIDP target

URI format (uniform across all tools):

  master:<catalogName>.<schemaName>.<volumeName>:/<path>
  workspace:/<path>
  <catalogName>.<schemaName>.<volumeName>:/<path>   (alias for master:)

When source_uri is set, the file is loaded via aidp_io.read_text() and parsed
based on its extension (.json/.jsonl/.csv). When source_uri is empty, the
existing inline runtime params are used.

When dest_uri is set, the tool serializes its result envelope to JSON (or the
rows to CSV/JSONL based on extension) and writes via aidp_io.write_text. The
structured envelope is still returned to the caller so they see what happened.

Return envelope (v1.1.0+):
    success: {"ok": true,  "data": {...}, <legacy top-level keys preserved>}
    error:   {"ok": false, "error": "...", "error_type": "..."}
"""

import csv
import io
import json
import math
import os
import re

from aidputils.agents.tools.custom_tools.base import CustomToolBase
from .utils.config_utils import get_cfg, as_rows

# --------------------------------------------------------------------------- #
# AIDP I/O - optional. Tool keeps working without source_uri/dest_uri if the
# shared aidp_io module is not present at runtime.
# --------------------------------------------------------------------------- #
try:
    from .utils.aidp_io import read_text, read_file, write_text, write_file, parse_uri
except ImportError:  # pragma: no cover - graceful degradation
    read_text = read_file = write_text = write_file = parse_uri = None  # type: ignore


# --------------------------------------------------------------------------- #
# Debug Channel - safe shim if runtime didn't inject aidp_debug
# --------------------------------------------------------------------------- #
try:  # pragma: no cover - runtime injected
    from aidp_debug import debug, debug_warn, debug_error, DebugLog
except ImportError:  # pragma: no cover - local/no-op fallback
    def debug(*_a, **_kw): pass
    def debug_warn(*_a, **_kw): pass
    def debug_error(*_a, **_kw): pass

    class DebugLog:  # type: ignore
        @staticmethod
        def embed(result):
            return result


# --------------------------------------------------------------------------- #
# Envelope helpers
# --------------------------------------------------------------------------- #
def _ok(data, **legacy):
    """Success envelope. Embeds debug log and preserves legacy top-level keys."""
    out = {"ok": True, "data": data}
    # preserve legacy top-level keys callers may depend on
    for k, v in legacy.items():
        if k not in out:
            out[k] = v
    return DebugLog.embed(out)


def _err(message, error_type="ToolError", **legacy):
    """Error envelope. Embeds debug log and preserves legacy 'error' key."""
    debug_error(f"{error_type}: {message}")
    out = {"ok": False, "error": str(message), "error_type": error_type}
    for k, v in legacy.items():
        if k not in out:
            out[k] = v
    return DebugLog.embed(out)


# --------------------------------------------------------------------------- #
# AIDP source/dest helpers
# --------------------------------------------------------------------------- #
def _uri_ext(uri):
    """Return the lowercased extension of the path portion of a URI, e.g. 'json'."""
    if not uri:
        return ""
    # Strip any scheme/volume prefix, keep only the path after the last ':'
    tail = uri.rsplit(":", 1)[-1]
    _, ext = os.path.splitext(tail)
    return ext.lower().lstrip(".")


def _parse_payload_by_ext(text, ext):
    """Parse a text payload to a Python object based on file extension.

    Supported: json, jsonl/ndjson, csv. Anything else falls back to JSON parse,
    and if that fails the raw text is returned.
    """
    ext = (ext or "").lower()
    if ext == "jsonl" or ext == "ndjson":
        rows = []
        for i, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception as e:
                raise ValueError(f"invalid JSONL on line {i}: {e}")
        return rows
    if ext == "csv":
        reader = csv.DictReader(io.StringIO(text))
        return [dict(r) for r in reader]
    if ext == "json":
        return json.loads(text)
    # unknown extension: best effort JSON, else raw text
    try:
        return json.loads(text)
    except Exception:
        return text


def _load_from_source_uri(source_uri, conf, context_vars):
    """Read source_uri via aidp_io.read_text and parse based on extension.

    Returns (payload, error_message). payload is whatever _parse_payload_by_ext
    returned (usually a list of dicts, but may be a dict or string).
    """
    if read_text is None:
        return None, (
            "source_uri provided but aidp_io is not available in this runtime; "
            "deploy utils/aidp_io.py alongside the toolkit"
        )
    try:
        text = read_text(source_uri, conf, context_vars)
    except Exception as e:
        return None, f"failed to read source_uri {source_uri!r}: {e}"
    ext = _uri_ext(source_uri)
    try:
        payload = _parse_payload_by_ext(text, ext)
    except Exception as e:
        return None, f"failed to parse {source_uri!r} as {ext or 'json'}: {e}"
    return payload, None


def _serialize_for_dest(envelope, dest_uri, rows_key="results"):
    """Pick a serialization for the dest URI based on extension.

    For csv/jsonl we try to serialize the row list under envelope['data'][rows_key]
    (or top-level legacy 'rows'/'results'). For everything else we serialize the
    full envelope as JSON. Returns (text, error_message).
    """
    ext = _uri_ext(dest_uri)

    def _extract_rows():
        data = envelope.get("data") or {}
        for k in (rows_key, "rows", "results"):
            v = data.get(k) if isinstance(data, dict) else None
            if isinstance(v, list):
                return v
            v = envelope.get(k)
            if isinstance(v, list):
                return v
        return None

    if ext == "jsonl" or ext == "ndjson":
        rows = _extract_rows()
        if rows is None:
            return None, f"cannot serialize envelope as {ext}: no row list found"
        return ("\n".join(json.dumps(r, default=str) for r in rows) + "\n"), None
    if ext == "csv":
        rows = _extract_rows()
        if rows is None:
            return None, "cannot serialize envelope as csv: no row list found"
        if not rows:
            return "", None
        # Union of keys, stable order based on first row then any extras.
        seen = []
        for r in rows:
            if not isinstance(r, dict):
                return None, "cannot serialize as csv: rows must be objects"
            for k in r.keys():
                if k not in seen:
                    seen.append(k)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=seen, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in seen})
        return buf.getvalue(), None
    # default: JSON of the full envelope
    try:
        return json.dumps(envelope, indent=2, default=str), None
    except Exception as e:
        return None, f"failed to serialize envelope as json: {e}"


def _maybe_write_dest(envelope, dest_uri, conf, context_vars, rows_key="results"):
    """If dest_uri is non-empty, serialize the envelope and write it via aidp_io.

    Mutates `envelope` (under data.dest) to record the write outcome and returns
    the (possibly mutated) envelope. On error the envelope's data.dest carries
    an 'error' field but the envelope's top-level ok flag is left unchanged so
    the caller still gets to see the computed result.
    """
    if not dest_uri:
        return envelope
    if write_text is None:
        if isinstance(envelope.get("data"), dict):
            envelope["data"]["dest"] = {
                "uri": dest_uri,
                "written": False,
                "error": (
                    "dest_uri provided but aidp_io is not available in this runtime; "
                    "deploy utils/aidp_io.py alongside the toolkit"
                ),
            }
        debug_warn("dest_uri set but aidp_io unavailable; skipping write")
        return envelope
    text, err = _serialize_for_dest(envelope, dest_uri, rows_key=rows_key)
    if err:
        if isinstance(envelope.get("data"), dict):
            envelope["data"]["dest"] = {"uri": dest_uri, "written": False, "error": err}
        debug_error(f"dest serialize failed: {err}")
        return envelope
    try:
        info = write_text(dest_uri, text, conf, context_vars)
        if isinstance(envelope.get("data"), dict):
            envelope["data"]["dest"] = {
                "uri": dest_uri,
                "written": True,
                "info": info if isinstance(info, dict) else {"info": info},
            }
        debug(f"dest write ok: {dest_uri}")
    except Exception as e:
        if isinstance(envelope.get("data"), dict):
            envelope["data"]["dest"] = {
                "uri": dest_uri,
                "written": False,
                "error": f"failed to write dest_uri {dest_uri!r}: {e}",
            }
        debug_error(f"dest write failed: {e}")
    return envelope


# --------------------------------------------------------------------------- #
# Filter
# --------------------------------------------------------------------------- #
@CustomToolBase.register
class FilterTool(CustomToolBase):
    """Keep only the records where a field satisfies a condition."""

    _OPS = {
        "eq": lambda a, b: a == b,
        "ne": lambda a, b: a != b,
        "gt": lambda a, b: _num(a) > _num(b),
        "gte": lambda a, b: _num(a) >= _num(b),
        "lt": lambda a, b: _num(a) < _num(b),
        "lte": lambda a, b: _num(a) <= _num(b),
        "contains": lambda a, b: str(b).lower() in str(a).lower(),
        "startswith": lambda a, b: str(a).lower().startswith(str(b).lower()),
        "regex": lambda a, b: re.search(str(b), str(a)) is not None,
        "in": lambda a, b: a in (b if isinstance(b, (list, tuple)) else [b]),
        "is_null": lambda a, b: a is None,
        "not_null": lambda a, b: a is not None,
    }

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        op = (runtime_params.get("operator", "eq") or "eq").lower()
        field = runtime_params.get("field", "")
        source_uri = (runtime_params.get("source_uri") or "").strip()
        dest_uri = (runtime_params.get("dest_uri") or "").strip()
        debug(f"FilterTool start op={op} field={field!r} source_uri={source_uri!r} dest_uri={dest_uri!r}")

        # Resolve input: source_uri wins over inline data.
        if source_uri:
            payload, perr = _load_from_source_uri(source_uri, conf, context_vars)
            if perr:
                return _err(perr, "InputError", error=perr)
            rows, err = as_rows(payload)
        else:
            rows, err = as_rows(runtime_params.get("data"))
        if err:
            return _err(err, "InputError", error=err)

        value = runtime_params.get("value")
        max_results = get_cfg(conf, "max_results", 1000)

        if op not in cls._OPS:
            msg = f"Unknown operator '{op}'. Valid: {', '.join(cls._OPS)}"
            return _err(msg, "InvalidOperator", error=msg)
        if op not in ("is_null", "not_null") and not field:
            msg = "field is required for this operator"
            return _err(msg, "MissingField", error=msg)

        # Validate regex up-front so a bad pattern is a real error, not a
        # silent zero-match.
        if op == "regex":
            try:
                re.compile(str(value))
            except re.error as rex:
                msg = f"invalid regex pattern: {rex}"
                return _err(msg, "InvalidRegex", error=msg)

        # Validate numeric value up-front for ordering operators so a non-
        # numeric value surfaces as a real error rather than zero matches.
        if op in ("gt", "gte", "lt", "lte"):
            try:
                _num(value)
            except Exception:
                msg = f"operator '{op}' requires a numeric value (got {value!r})"
                return _err(msg, "InvalidValue", error=msg)

        fn = cls._OPS[op]
        kept = []
        skipped = 0
        try:
            for r in rows:
                cell = r.get(field) if field else None
                try:
                    if fn(cell, value):
                        kept.append(r)
                except Exception:
                    # A row whose value can't be compared simply doesn't match.
                    skipped += 1
                    continue
        except Exception as e:
            return _err(str(e), type(e).__name__, error=str(e))

        truncated = len(kept) > max_results
        if truncated:
            debug_warn(f"FilterTool truncated kept={len(kept)} -> {max_results}")
        if skipped:
            debug(f"FilterTool skipped {skipped} uncomparable rows")

        data = {
            "matched": len(kept),
            "total": len(rows),
            "truncated": truncated,
            "results": kept[:max_results],
        }
        if source_uri:
            data["source"] = {"uri": source_uri}
        envelope = _ok(
            data,
            matched=data["matched"],
            total=data["total"],
            truncated=data["truncated"],
            results=data["results"],
        )
        # Optional write-back to AIDP.
        envelope = _maybe_write_dest(envelope, dest_uri, conf, context_vars,
                                     rows_key="results")
        return envelope


# --------------------------------------------------------------------------- #
# Compare
# --------------------------------------------------------------------------- #
@CustomToolBase.register
class CompareTool(CustomToolBase):
    """Diff two record sets keyed on an id field. Reports added, removed, and
    changed records (with per-field old/new values).

    Numeric fields are compared with an absolute tolerance. The default is
    1e-9 (effectively float-equality but resilient to representation noise);
    a caller can set conf.tolerance=0 to force strict equality.
    """

    @staticmethod
    def _split_paired_payload(payload):
        """Try to interpret a single source_uri payload as {left:..., right:...}.

        Returns (left, right) or (None, None) if the payload is not a dict
        with both keys.
        """
        if isinstance(payload, dict) and "left" in payload and "right" in payload:
            return payload.get("left"), payload.get("right")
        return None, None

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        key = runtime_params.get("key", "")
        source_uri = (runtime_params.get("source_uri") or "").strip()
        left_uri = (runtime_params.get("left_uri") or "").strip()
        right_uri = (runtime_params.get("right_uri") or "").strip()
        dest_uri = (runtime_params.get("dest_uri") or "").strip()
        debug(
            f"CompareTool start key={key!r} source_uri={source_uri!r} "
            f"left_uri={left_uri!r} right_uri={right_uri!r} dest_uri={dest_uri!r}"
        )

        # Resolve left/right. Precedence:
        #   1) left_uri / right_uri (per-side AIDP URIs)
        #   2) source_uri pointing to a paired {left, right} JSON document
        #   3) inline runtime_params['left'] / ['right']
        left_payload = None
        right_payload = None
        if left_uri:
            payload, perr = _load_from_source_uri(left_uri, conf, context_vars)
            if perr:
                return _err(f"left_uri: {perr}", "InputError", error=perr)
            left_payload = payload
        if right_uri:
            payload, perr = _load_from_source_uri(right_uri, conf, context_vars)
            if perr:
                return _err(f"right_uri: {perr}", "InputError", error=perr)
            right_payload = payload

        if (left_payload is None or right_payload is None) and source_uri:
            payload, perr = _load_from_source_uri(source_uri, conf, context_vars)
            if perr:
                return _err(perr, "InputError", error=perr)
            lp, rp = cls._split_paired_payload(payload)
            if lp is None and rp is None:
                msg = (
                    "source_uri payload must be an object with 'left' and 'right' "
                    "row lists, or use left_uri/right_uri for split inputs"
                )
                return _err(msg, "InputError", error=msg)
            if left_payload is None:
                left_payload = lp
            if right_payload is None:
                right_payload = rp

        if left_payload is not None:
            left, err = as_rows(left_payload)
        else:
            left, err = as_rows(runtime_params.get("left"))
        if err:
            msg = f"left: {err}"
            return _err(msg, "InputError", error=msg)

        if right_payload is not None:
            right, err = as_rows(right_payload)
        else:
            right, err = as_rows(runtime_params.get("right"))
        if err:
            msg = f"right: {err}"
            return _err(msg, "InputError", error=msg)

        if not key:
            msg = "key (the id field to match records on) is required"
            return _err(msg, "MissingKey", error=msg)

        # Tolerance: explicit 0 is respected (strict equality). Default 1e-9
        # guards against float-representation noise without hiding real
        # changes.
        tolerance = get_cfg(conf, "tolerance", 1e-9)
        try:
            tolerance = float(tolerance)
        except (TypeError, ValueError):
            tolerance = 1e-9

        def _equal(a, b):
            if a == b:
                return True
            if isinstance(a, bool) or isinstance(b, bool):
                return False
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                try:
                    if math.isnan(a) and math.isnan(b):
                        return True
                except TypeError:
                    pass
                return abs(a - b) <= tolerance
            return False

        try:
            lmap = {r.get(key): r for r in left}
            rmap = {r.get(key): r for r in right}
            lkeys, rkeys = set(lmap), set(rmap)

            added = [rmap[k] for k in (rkeys - lkeys)]
            removed = [lmap[k] for k in (lkeys - rkeys)]
            changed = []
            for k in (lkeys & rkeys):
                a, b = lmap[k], rmap[k]
                diffs = {}
                for f in set(a) | set(b):
                    if not _equal(a.get(f), b.get(f)):
                        diffs[f] = {"old": a.get(f), "new": b.get(f)}
                if diffs:
                    changed.append({"key": k, "changes": diffs})

            summary = {
                "added": len(added),
                "removed": len(removed),
                "changed": len(changed),
                "unchanged": len(lkeys & rkeys) - len(changed),
            }
            debug(f"CompareTool {summary} tolerance={tolerance}")
            data = {
                "summary": summary,
                "added": added,
                "removed": removed,
                "changed": changed,
                "tolerance": tolerance,
            }
            if source_uri or left_uri or right_uri:
                data["source"] = {
                    "uri": source_uri or None,
                    "left_uri": left_uri or None,
                    "right_uri": right_uri or None,
                }
            envelope = _ok(
                data,
                summary=summary,
                added=added,
                removed=removed,
                changed=changed,
            )
            # For CompareTool, csv/jsonl dest gets the 'changed' list as rows
            # (most useful single-table view). Json dest gets the full envelope.
            envelope = _maybe_write_dest(envelope, dest_uri, conf, context_vars,
                                         rows_key="changed")
            return envelope
        except Exception as e:
            return _err(str(e), type(e).__name__, error=str(e))


# --------------------------------------------------------------------------- #
# Data manipulation (pandas)
# --------------------------------------------------------------------------- #
@CustomToolBase.register
class DataManipulationTool(CustomToolBase):
    """Reshape tabular data with pandas: select/rename columns, sort, dedupe,
    and group-by aggregate. Driven by an 'operation' parameter."""

    _VALID_OPS = ("select", "rename", "sort", "dedupe", "groupby")

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        op = (runtime_params.get("operation", "") or "").lower()
        source_uri = (runtime_params.get("source_uri") or "").strip()
        dest_uri = (runtime_params.get("dest_uri") or "").strip()
        debug(
            f"DataManipulationTool start op={op} "
            f"source_uri={source_uri!r} dest_uri={dest_uri!r}"
        )

        # Resolve input: source_uri wins over inline data.
        if source_uri:
            payload, perr = _load_from_source_uri(source_uri, conf, context_vars)
            if perr:
                return _err(perr, "InputError", error=perr)
            rows, err = as_rows(payload)
        else:
            rows, err = as_rows(runtime_params.get("data"))
        if err:
            return _err(err, "InputError", error=err)
        if not rows:
            data = {"rows": [], "count": 0, "operation": op, "truncated": False}
            if source_uri:
                data["source"] = {"uri": source_uri}
            envelope = _ok(data, rows=[], count=0)
            envelope = _maybe_write_dest(envelope, dest_uri, conf, context_vars,
                                         rows_key="rows")
            return envelope

        spec = runtime_params.get("spec", {})
        if isinstance(spec, str):
            try:
                spec = json.loads(spec)
            except Exception:
                msg = "spec must be an object or valid JSON object"
                return _err(msg, "InvalidSpec", error=msg)
        if not isinstance(spec, dict):
            msg = "spec must be a JSON object"
            return _err(msg, "InvalidSpec", error=msg)

        max_rows = get_cfg(conf, "max_rows", 5000)

        # Structured error if pandas isn't installed (instead of RuntimeError).
        try:
            import pandas as pd
        except ImportError as ie:
            msg = (
                "pandas is not available in this runtime; install pandas>=2.0 "
                f"(import error: {ie})"
            )
            return _err(msg, "DependencyMissing", error=msg)

        if op not in cls._VALID_OPS:
            msg = "operation must be one of: " + ", ".join(cls._VALID_OPS)
            return _err(msg, "InvalidOperation", error=msg)

        try:
            df = pd.DataFrame(rows)

            if op == "select":
                cols = spec.get("columns", [])
                df = df[[c for c in cols if c in df.columns]]
            elif op == "rename":
                df = df.rename(columns=spec.get("map", {}))
            elif op == "sort":
                by = spec.get("by", [])
                if not by:
                    msg = "sort requires spec.by"
                    return _err(msg, "InvalidSpec", error=msg)
                asc = spec.get("ascending", True)
                df = df.sort_values(by=by, ascending=asc)
            elif op == "dedupe":
                subset = spec.get("subset") or None
                df = df.drop_duplicates(subset=subset)
            elif op == "groupby":
                by = spec.get("by", [])
                aggs = spec.get("agg", {})
                if not by or not aggs:
                    msg = "groupby requires spec.by and spec.agg"
                    return _err(msg, "InvalidSpec", error=msg)
                df = df.groupby(by, dropna=False).agg(aggs).reset_index()

            truncated = len(df) > max_rows
            if truncated:
                debug_warn(f"DataManipulationTool truncated {len(df)} -> {max_rows}")
            out = df.head(max_rows).to_dict(orient="records")
            data = {
                "operation": op,
                "count": int(len(df)),
                "truncated": truncated,
                "rows": out,
            }
            if source_uri:
                data["source"] = {"uri": source_uri}
            envelope = _ok(
                data,
                operation=op,
                count=int(len(df)),
                truncated=truncated,
                rows=out,
            )
            envelope = _maybe_write_dest(envelope, dest_uri, conf, context_vars,
                                         rows_key="rows")
            return envelope
        except Exception as e:
            return _err(str(e), type(e).__name__, error=str(e))


def _num(x):
    """Best-effort numeric coercion for ordering comparisons."""
    if isinstance(x, bool):
        return int(x)
    if isinstance(x, (int, float)):
        return x
    return float(x)
