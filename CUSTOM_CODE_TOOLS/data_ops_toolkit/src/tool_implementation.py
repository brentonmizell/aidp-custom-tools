"""
Data Ops Toolkit
================
Three tools that operate on tabular data (a list of record objects):

  FilterTool          - keep records matching a condition
  CompareTool         - diff two record sets, report added/removed/changed
  DataManipulationTool- group-by / aggregate / sort / dedupe / select via pandas

All three accept data as a list of objects or a JSON string encoding one.

Return envelope (v1.1.0+):
    success: {"ok": true,  "data": {...}, <legacy top-level keys preserved>}
    error:   {"ok": false, "error": "...", "error_type": "..."}
"""

import json
import math
import re

from aidputils.agents.tools.custom_tools.base import CustomToolBase
from .utils.config_utils import get_cfg, as_rows


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
        debug(f"FilterTool start op={op} field={field!r}")

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
        # Preserve legacy top-level shape.
        return _ok(
            data,
            matched=data["matched"],
            total=data["total"],
            truncated=data["truncated"],
            results=data["results"],
        )


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

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        key = runtime_params.get("key", "")
        debug(f"CompareTool start key={key!r}")

        left, err = as_rows(runtime_params.get("left"))
        if err:
            msg = f"left: {err}"
            return _err(msg, "InputError", error=msg)
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
            return _ok(
                data,
                summary=summary,
                added=added,
                removed=removed,
                changed=changed,
            )
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
        debug(f"DataManipulationTool start op={op}")

        rows, err = as_rows(runtime_params.get("data"))
        if err:
            return _err(err, "InputError", error=err)
        if not rows:
            data = {"rows": [], "count": 0, "operation": op, "truncated": False}
            return _ok(data, rows=[], count=0)

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
            return _ok(
                data,
                operation=op,
                count=int(len(df)),
                truncated=truncated,
                rows=out,
            )
        except Exception as e:
            return _err(str(e), type(e).__name__, error=str(e))


def _num(x):
    """Best-effort numeric coercion for ordering comparisons."""
    if isinstance(x, bool):
        return int(x)
    if isinstance(x, (int, float)):
        return x
    return float(x)
