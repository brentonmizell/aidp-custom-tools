"""
Text Utils Toolkit
==================
  TemplateRenderTool - render a Jinja2 template against variables
  RegexTool          - extract / replace / split text with a regex
  JsonTransformTool  - pull/reshape fields out of JSON with JSONPath

v1.2.0: Every tool now accepts optional source_uri / dest_uri to read input
from (and write output to) an AIDP master volume or workspace path. The legacy
inline parameters (template/text/data) remain fully supported; source_uri,
when provided, overrides the inline value for that input. dest_uri, when
provided, writes the primary text output to the named URI in addition to
returning it in the envelope.
"""

import json
import re

from aidputils.agents.tools.custom_tools.base import CustomToolBase
from .utils.config_utils import get_cfg

# --------------------------------------------------------------------------- #
# Shared file-IO helper (import-guarded so the module still loads if aidp_io
# is missing in the runtime; URI features simply become unavailable in that
# case and the legacy inline parameters keep working).
# --------------------------------------------------------------------------- #
try:
    from aidp_io import (  # type: ignore
        parse_uri,
        read_file,
        write_file,
        read_text,
        write_text,
    )
    _AIDP_IO_AVAILABLE = True
    _AIDP_IO_IMPORT_ERROR = None
except Exception as _e:  # pragma: no cover - runtime fallback
    _AIDP_IO_AVAILABLE = False
    _AIDP_IO_IMPORT_ERROR = str(_e)

    def parse_uri(*_a, **_kw):  # type: ignore
        raise RuntimeError(
            "aidp_io is not available in this runtime: "
            + (_AIDP_IO_IMPORT_ERROR or "unknown import error")
        )

    def read_file(*_a, **_kw):  # type: ignore
        raise RuntimeError(
            "aidp_io is not available in this runtime: "
            + (_AIDP_IO_IMPORT_ERROR or "unknown import error")
        )

    def write_file(*_a, **_kw):  # type: ignore
        raise RuntimeError(
            "aidp_io is not available in this runtime: "
            + (_AIDP_IO_IMPORT_ERROR or "unknown import error")
        )

    def read_text(*_a, **_kw):  # type: ignore
        raise RuntimeError(
            "aidp_io is not available in this runtime: "
            + (_AIDP_IO_IMPORT_ERROR or "unknown import error")
        )

    def write_text(*_a, **_kw):  # type: ignore
        raise RuntimeError(
            "aidp_io is not available in this runtime: "
            + (_AIDP_IO_IMPORT_ERROR or "unknown import error")
        )


# --------------------------------------------------------------------------- #
# Debug Channel (with no-op fallback if the runtime doesn't inject aidp_debug)
# --------------------------------------------------------------------------- #
try:
    from aidp_debug import debug, debug_warn, debug_error, DebugLog  # type: ignore
except ImportError:  # pragma: no cover - runtime fallback
    def debug(*args, **kwargs): pass
    def debug_warn(*args, **kwargs): pass
    def debug_error(*args, **kwargs): pass
    class DebugLog:  # type: ignore
        @staticmethod
        def embed(result):
            return result


# --------------------------------------------------------------------------- #
# Envelope helpers
# --------------------------------------------------------------------------- #
def _ok(data, **legacy):
    """Success envelope. Legacy top-level keys are preserved alongside `data`
    so existing callers continue to work."""
    out = {"ok": True, "data": data}
    for k, v in legacy.items():
        if k not in out:
            out[k] = v
    return out


def _err(msg, error_type="ToolError", **legacy):
    """Error envelope. Keeps legacy `error` key at top level for back-compat."""
    out = {"ok": False, "error": str(msg), "error_type": error_type}
    for k, v in legacy.items():
        if k not in out:
            out[k] = v
    # legacy key always present
    out.setdefault("error", str(msg))
    return out


# --------------------------------------------------------------------------- #
# URI helpers (shared across the three tools)
# --------------------------------------------------------------------------- #
def _norm_uri(value):
    """Treat empty strings as 'not provided'. Trim whitespace."""
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    v = value.strip()
    return v or None


def _read_text_from_uri(uri, conf, context_vars):
    """Read text content from a master:/workspace: URI. Returns (text, err)."""
    if not _AIDP_IO_AVAILABLE:
        return None, (
            "aidp_io is not available in this runtime; cannot read source_uri. "
            f"(import error: {_AIDP_IO_IMPORT_ERROR})"
        )
    try:
        text = read_text(uri, conf, context_vars)
        return text, None
    except Exception as e:
        return None, f"failed to read source_uri {uri!r}: {e}"


def _write_text_to_uri(uri, text, conf, context_vars):
    """Write text content to a master:/workspace: URI. Returns (meta, err)."""
    if not _AIDP_IO_AVAILABLE:
        return None, (
            "aidp_io is not available in this runtime; cannot write dest_uri. "
            f"(import error: {_AIDP_IO_IMPORT_ERROR})"
        )
    try:
        meta = write_text(uri, text, conf, context_vars)
        return meta, None
    except Exception as e:
        return None, f"failed to write dest_uri {uri!r}: {e}"


# --------------------------------------------------------------------------- #
# Template renderer
# --------------------------------------------------------------------------- #
@CustomToolBase.register
class TemplateRenderTool(CustomToolBase):
    """Render a Jinja2 template with a dict of variables. Use to build prompts,
    payloads, emails, or any text from flow variables. Autoescaping is
    configurable via conf.autoescape (off by default; turn it on for HTML).

    URI inputs (optional):
      source_uri - read the template body from a master:/workspace: URI
                   (overrides the inline `template` param when provided)
      dest_uri   - write the rendered text to a master:/workspace: URI
                   (in addition to returning it in the response envelope)
    """

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        debug("TemplateRenderTool: start")

        source_uri = _norm_uri(runtime_params.get("source_uri"))
        dest_uri = _norm_uri(runtime_params.get("dest_uri"))

        # ----- template: source_uri overrides inline `template` -----
        if source_uri:
            debug(f"TemplateRenderTool: reading template from {source_uri}")
            template, err = _read_text_from_uri(source_uri, conf, context_vars)
            if err:
                debug_error(f"TemplateRenderTool: source_uri read failed: {err}")
                return DebugLog.embed(_err(
                    err, error_type="SourceReadError", source_uri=source_uri,
                ))
        else:
            template = runtime_params.get("template", "")

        variables = runtime_params.get("variables", {})
        if isinstance(variables, str):
            try:
                variables = json.loads(variables) if variables.strip() else {}
            except Exception as e:
                debug_error(f"TemplateRenderTool: invalid variables JSON: {e}")
                return DebugLog.embed(_err(
                    "variables must be an object or valid JSON object",
                    error_type="InvalidInput",
                ))
        if not isinstance(variables, dict):
            debug_error("TemplateRenderTool: variables not a dict")
            return DebugLog.embed(_err(
                "variables must be an object", error_type="InvalidInput"
            ))

        autoescape = get_cfg(conf, "autoescape", False)
        strict = get_cfg(conf, "strict", False)
        debug(
            f"TemplateRenderTool: autoescape={autoescape} strict={strict} "
            f"vars={list(variables.keys())} source_uri={bool(source_uri)} "
            f"dest_uri={bool(dest_uri)}"
        )

        try:
            from jinja2 import Environment, StrictUndefined, Undefined, BaseLoader
            env = Environment(
                loader=BaseLoader(),
                autoescape=autoescape,
                undefined=StrictUndefined if strict else Undefined,
                trim_blocks=True,
                lstrip_blocks=True,
            )
            rendered = env.from_string(template).render(**variables)
        except Exception as e:
            debug_error(f"TemplateRenderTool: render failed: {e}")
            return DebugLog.embed(_err(str(e), error_type=type(e).__name__))

        data = {"rendered": rendered, "length": len(rendered)}
        legacy = {"rendered": rendered, "length": len(rendered)}

        # ----- dest_uri: write the rendered text -----
        if dest_uri:
            debug(f"TemplateRenderTool: writing rendered text to {dest_uri}")
            meta, werr = _write_text_to_uri(dest_uri, rendered, conf, context_vars)
            if werr:
                debug_error(f"TemplateRenderTool: dest_uri write failed: {werr}")
                return DebugLog.embed(_err(
                    werr, error_type="DestWriteError",
                    dest_uri=dest_uri,
                    rendered=rendered,
                    length=len(rendered),
                ))
            data["dest_uri"] = dest_uri
            data["written"] = meta
            legacy["dest_uri"] = dest_uri
            legacy["written"] = meta

        if source_uri:
            data["source_uri"] = source_uri
            legacy["source_uri"] = source_uri

        return DebugLog.embed(_ok(data, **legacy))


# --------------------------------------------------------------------------- #
# Regex
# --------------------------------------------------------------------------- #
@CustomToolBase.register
class RegexTool(CustomToolBase):
    """Run a regex against text. Supported modes:
      match   - re.match (anchored at start), returns first match or null
      search  - re.search, returns first match anywhere or null
      findall - all matches with groups (default)
      sub     - replace all matches with `replacement`
      split   - split text on the pattern
    Legacy aliases: extract->findall, replace->sub.

    URI inputs (optional):
      source_uri - read the haystack text from a master:/workspace: URI
                   (overrides the inline `text` param when provided)
      dest_uri   - write the match results as JSON to a master:/workspace: URI
                   (in addition to returning them in the response envelope)
    """

    _MODE_ALIASES = {"extract": "findall", "replace": "sub"}
    _VALID_MODES = {"match", "search", "findall", "sub", "split"}

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        debug("RegexTool: start")

        source_uri = _norm_uri(runtime_params.get("source_uri"))
        dest_uri = _norm_uri(runtime_params.get("dest_uri"))

        # ----- text: source_uri overrides inline `text` -----
        if source_uri:
            debug(f"RegexTool: reading haystack from {source_uri}")
            text, err = _read_text_from_uri(source_uri, conf, context_vars)
            if err:
                debug_error(f"RegexTool: source_uri read failed: {err}")
                return DebugLog.embed(_err(
                    err, error_type="SourceReadError", source_uri=source_uri,
                ))
        else:
            text = runtime_params.get("text", "")

        pattern = runtime_params.get("pattern", "")
        raw_mode = (runtime_params.get("mode") or "findall").strip().lower()
        mode = cls._MODE_ALIASES.get(raw_mode, raw_mode)
        replacement = runtime_params.get("replacement", "")

        if not pattern:
            debug_error("RegexTool: pattern missing")
            return DebugLog.embed(_err("pattern is required", error_type="InvalidInput"))
        if mode not in cls._VALID_MODES:
            debug_error(f"RegexTool: invalid mode {raw_mode!r}")
            return DebugLog.embed(_err(
                f"invalid mode {raw_mode!r}; must be one of "
                + ", ".join(sorted(cls._VALID_MODES)),
                error_type="InvalidInput",
            ))

        flags = 0
        if get_cfg(conf, "ignore_case", False):
            flags |= re.IGNORECASE
        if get_cfg(conf, "multiline", False):
            flags |= re.MULTILINE
        max_matches = get_cfg(conf, "max_matches", 500)
        debug(
            f"RegexTool: mode={mode} flags={flags} max_matches={max_matches} "
            f"source_uri={bool(source_uri)} dest_uri={bool(dest_uri)}"
        )

        try:
            rx = re.compile(pattern, flags)
        except re.error as e:
            debug_error(f"RegexTool: invalid regex: {e}")
            return DebugLog.embed(_err(f"invalid regex: {e}", error_type="RegexError"))

        try:
            if mode == "sub":
                result_text = rx.sub(replacement, text)
                data = {"result": result_text}
                legacy = {"result": result_text}
            elif mode == "split":
                parts = rx.split(text, maxsplit=max(0, max_matches))
                truncated = False
                data = {"parts": parts, "count": len(parts), "truncated": truncated}
                legacy = {"parts": parts, "count": len(parts)}
            elif mode in ("match", "search"):
                m = rx.match(text) if mode == "match" else rx.search(text)
                if m is None:
                    data = {"matched": False, "match": None}
                    legacy = {"matched": False, "match": None}
                else:
                    match_obj = {
                        "match": m.group(0),
                        "groups": list(m.groups()),
                        "named": m.groupdict(),
                        "span": list(m.span()),
                    }
                    data = {"matched": True, "match": match_obj}
                    legacy = {"matched": True, "match": match_obj}
            else:
                # findall (default)
                matches = []
                truncated = False
                for m in rx.finditer(text):
                    if len(matches) >= max_matches:
                        truncated = True
                        break
                    matches.append({
                        "match": m.group(0),
                        "groups": list(m.groups()),
                        "named": m.groupdict(),
                        "span": list(m.span()),
                    })
                data = {
                    "count": len(matches),
                    "matches": matches,
                    "truncated": truncated,
                }
                legacy = {"count": len(matches), "matches": matches}
        except Exception as e:
            debug_error(f"RegexTool: exception during {mode}: {e}")
            return DebugLog.embed(_err(str(e), error_type=type(e).__name__))

        # ----- dest_uri: write match results as JSON -----
        if dest_uri:
            debug(f"RegexTool: writing match results to {dest_uri}")
            try:
                payload_text = json.dumps(data, ensure_ascii=False, indent=2)
            except Exception as e:
                debug_error(f"RegexTool: failed to serialize results to JSON: {e}")
                return DebugLog.embed(_err(
                    f"failed to serialize match results to JSON: {e}",
                    error_type="SerializationError",
                    dest_uri=dest_uri,
                    **legacy,
                ))
            meta, werr = _write_text_to_uri(dest_uri, payload_text, conf, context_vars)
            if werr:
                debug_error(f"RegexTool: dest_uri write failed: {werr}")
                return DebugLog.embed(_err(
                    werr, error_type="DestWriteError",
                    dest_uri=dest_uri,
                    **legacy,
                ))
            data["dest_uri"] = dest_uri
            data["written"] = meta
            legacy["dest_uri"] = dest_uri
            legacy["written"] = meta

        if source_uri:
            data["source_uri"] = source_uri
            legacy["source_uri"] = source_uri

        return DebugLog.embed(_ok(data, **legacy))


# --------------------------------------------------------------------------- #
# JSON transform (JSONPath)
# --------------------------------------------------------------------------- #
@CustomToolBase.register
class JsonTransformTool(CustomToolBase):
    """Extract or reshape JSON using JSONPath expressions. Pass a single 'path'
    to pull one value/list, or a 'mapping' object to build a new shape
    ({'out_field': '$.json.path'}). Use to pluck fields out of an API response
    before handing them to the next step.

    URI inputs (optional):
      source_uri - read the input JSON from a master:/workspace: URI
                   (overrides the inline `data` param when provided)
      dest_uri   - write the transformed JSON output to a master:/workspace: URI
                   (in addition to returning it in the response envelope)
    """

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        debug("JsonTransformTool: start")

        source_uri = _norm_uri(runtime_params.get("source_uri"))
        dest_uri = _norm_uri(runtime_params.get("dest_uri"))

        max_input_bytes = get_cfg(conf, "max_input_bytes", 1048576)
        max_results = get_cfg(conf, "max_results", 1000)

        # ----- data: source_uri overrides inline `data` -----
        if source_uri:
            debug(f"JsonTransformTool: reading input JSON from {source_uri}")
            data_text, err = _read_text_from_uri(source_uri, conf, context_vars)
            if err:
                debug_error(f"JsonTransformTool: source_uri read failed: {err}")
                return DebugLog.embed(_err(
                    err, error_type="SourceReadError", source_uri=source_uri,
                ))
            data = data_text
        else:
            data = runtime_params.get("data")

        # Cap raw string input before parsing
        if isinstance(data, str):
            if len(data.encode("utf-8", errors="ignore")) > max_input_bytes:
                debug_warn(
                    f"JsonTransformTool: input exceeds max_input_bytes={max_input_bytes}"
                )
                return DebugLog.embed(_err(
                    f"data exceeds max_input_bytes={max_input_bytes}",
                    error_type="InputTooLarge",
                ))
            try:
                data = json.loads(data) if data.strip() else None
            except Exception as e:
                debug_error(f"JsonTransformTool: invalid JSON: {e}")
                return DebugLog.embed(_err(
                    "data must be an object/array or valid JSON",
                    error_type="InvalidInput",
                ))

        path = runtime_params.get("path", "")
        mapping = runtime_params.get("mapping", {})
        if isinstance(mapping, str) and mapping.strip():
            try:
                mapping = json.loads(mapping)
            except Exception as e:
                debug_error(f"JsonTransformTool: invalid mapping JSON: {e}")
                return DebugLog.embed(_err(
                    "mapping must be a JSON object", error_type="InvalidInput",
                ))

        try:
            from jsonpath_ng.ext import parse as jp_parse
        except Exception as e:
            debug_error(f"JsonTransformTool: jsonpath-ng missing: {e}")
            return DebugLog.embed(_err(
                f"jsonpath-ng not available: {e}", error_type="DependencyMissing",
            ))

        debug(
            f"JsonTransformTool: path={bool(path)} "
            f"mapping_keys={list(mapping) if isinstance(mapping, dict) else None} "
            f"source_uri={bool(source_uri)} dest_uri={bool(dest_uri)}"
        )

        # Local helper to attach dest_uri write + source_uri echo to a success
        # payload before returning. `primary_value` is what gets serialized to
        # the dest_uri (the meaningful tool output).
        def _finish(data_dict, primary_value, **legacy):
            if dest_uri:
                debug(f"JsonTransformTool: writing transformed JSON to {dest_uri}")
                try:
                    payload_text = json.dumps(primary_value, ensure_ascii=False, indent=2)
                except Exception as e:
                    debug_error(
                        f"JsonTransformTool: failed to serialize output JSON: {e}"
                    )
                    return DebugLog.embed(_err(
                        f"failed to serialize output JSON: {e}",
                        error_type="SerializationError",
                        dest_uri=dest_uri,
                        **legacy,
                    ))
                meta, werr = _write_text_to_uri(dest_uri, payload_text, conf, context_vars)
                if werr:
                    debug_error(f"JsonTransformTool: dest_uri write failed: {werr}")
                    return DebugLog.embed(_err(
                        werr, error_type="DestWriteError",
                        dest_uri=dest_uri,
                        **legacy,
                    ))
                data_dict["dest_uri"] = dest_uri
                data_dict["written"] = meta
                legacy["dest_uri"] = dest_uri
                legacy["written"] = meta
            if source_uri:
                data_dict["source_uri"] = source_uri
                legacy["source_uri"] = source_uri
            return DebugLog.embed(_ok(data_dict, **legacy))

        if path:
            try:
                expr = jp_parse(path)
            except Exception as e:
                debug_error(f"JsonTransformTool: invalid path expression: {e}")
                return DebugLog.embed(_err(
                    f"invalid jsonpath: {e}", error_type="JsonPathError",
                ))
            try:
                found = [m.value for m in expr.find(data)]
            except Exception as e:
                debug_error(f"JsonTransformTool: path find failed: {e}")
                return DebugLog.embed(_err(
                    f"jsonpath evaluation failed: {e}",
                    error_type="JsonPathError",
                ))
            truncated = False
            if len(found) > max_results:
                truncated = True
                found = found[:max_results]
            value = found[0] if len(found) == 1 else found
            payload = {
                "value": value,
                "count": len(found),
                "truncated": truncated,
            }
            return _finish(payload, value, value=value, count=len(found))

        if mapping:
            if not isinstance(mapping, dict):
                return DebugLog.embed(_err(
                    "mapping must be a JSON object", error_type="InvalidInput",
                ))
            out = {}
            errors = {}
            truncated = False
            for out_key, expr_str in mapping.items():
                try:
                    expr = jp_parse(expr_str)
                    found = [m.value for m in expr.find(data)]
                except Exception as e:
                    debug_error(
                        f"JsonTransformTool: mapping[{out_key!r}] failed: {e}"
                    )
                    errors[out_key] = str(e)
                    out[out_key] = None
                    continue
                if len(found) > max_results:
                    truncated = True
                    found = found[:max_results]
                out[out_key] = found[0] if len(found) == 1 else found
            if errors:
                return DebugLog.embed(_err(
                    "one or more mapping expressions failed",
                    error_type="JsonPathError",
                    result=out,
                    field_errors=errors,
                    truncated=truncated,
                ))
            payload = {"result": out, "truncated": truncated}
            return _finish(payload, out, result=out)

        debug_error("JsonTransformTool: neither path nor mapping provided")
        return DebugLog.embed(_err(
            "provide either 'path' or 'mapping'", error_type="InvalidInput",
        ))
