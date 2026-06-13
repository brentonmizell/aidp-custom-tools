"""
Text Utils Toolkit
==================
  TemplateRenderTool - render a Jinja2 template against variables
  RegexTool          - extract / replace / split text with a regex
  JsonTransformTool  - pull/reshape fields out of JSON with JSONPath
"""

import json
import re

from aidputils.agents.tools.custom_tools.base import CustomToolBase
from .utils.config_utils import get_cfg

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
# Template renderer
# --------------------------------------------------------------------------- #
@CustomToolBase.register
class TemplateRenderTool(CustomToolBase):
    """Render a Jinja2 template with a dict of variables. Use to build prompts,
    payloads, emails, or any text from flow variables. Autoescaping is
    configurable via conf.autoescape (off by default; turn it on for HTML)."""

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        debug("TemplateRenderTool: start")
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
        debug(f"TemplateRenderTool: autoescape={autoescape} strict={strict} vars={list(variables.keys())}")

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
            data = {"rendered": rendered, "length": len(rendered)}
            return DebugLog.embed(_ok(data, rendered=rendered, length=len(rendered)))
        except Exception as e:
            debug_error(f"TemplateRenderTool: render failed: {e}")
            return DebugLog.embed(_err(str(e), error_type=type(e).__name__))


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
    Legacy aliases: extract->findall, replace->sub."""

    _MODE_ALIASES = {"extract": "findall", "replace": "sub"}
    _VALID_MODES = {"match", "search", "findall", "sub", "split"}

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        debug("RegexTool: start")
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
        debug(f"RegexTool: mode={mode} flags={flags} max_matches={max_matches}")

        try:
            rx = re.compile(pattern, flags)
        except re.error as e:
            debug_error(f"RegexTool: invalid regex: {e}")
            return DebugLog.embed(_err(f"invalid regex: {e}", error_type="RegexError"))

        try:
            if mode == "sub":
                result_text = rx.sub(replacement, text)
                return DebugLog.embed(_ok(
                    {"result": result_text},
                    result=result_text,
                ))

            if mode == "split":
                parts = rx.split(text, maxsplit=max(0, max_matches))
                truncated = False
                # re.split with maxsplit produces at most maxsplit+1 parts
                # we expose a cap for parity with finditer-based limits
                return DebugLog.embed(_ok(
                    {"parts": parts, "count": len(parts), "truncated": truncated},
                    parts=parts,
                    count=len(parts),
                ))

            if mode in ("match", "search"):
                m = rx.match(text) if mode == "match" else rx.search(text)
                if m is None:
                    return DebugLog.embed(_ok(
                        {"matched": False, "match": None},
                        matched=False,
                        match=None,
                    ))
                match_obj = {
                    "match": m.group(0),
                    "groups": list(m.groups()),
                    "named": m.groupdict(),
                    "span": list(m.span()),
                }
                return DebugLog.embed(_ok(
                    {"matched": True, "match": match_obj},
                    matched=True,
                    match=match_obj,
                ))

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
            return DebugLog.embed(_ok(
                data,
                count=len(matches),
                matches=matches,
            ))
        except Exception as e:
            debug_error(f"RegexTool: exception during {mode}: {e}")
            return DebugLog.embed(_err(str(e), error_type=type(e).__name__))


# --------------------------------------------------------------------------- #
# JSON transform (JSONPath)
# --------------------------------------------------------------------------- #
@CustomToolBase.register
class JsonTransformTool(CustomToolBase):
    """Extract or reshape JSON using JSONPath expressions. Pass a single 'path'
    to pull one value/list, or a 'mapping' object to build a new shape
    ({'out_field': '$.json.path'}). Use to pluck fields out of an API response
    before handing them to the next step."""

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        debug("JsonTransformTool: start")
        data = runtime_params.get("data")
        max_input_bytes = get_cfg(conf, "max_input_bytes", 1048576)
        max_results = get_cfg(conf, "max_results", 1000)

        # Cap raw string input before parsing
        truncated_input = False
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

        debug(f"JsonTransformTool: path={bool(path)} mapping_keys={list(mapping) if isinstance(mapping, dict) else None}")

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
            return DebugLog.embed(_ok(payload, value=value, count=len(found)))

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
            return DebugLog.embed(_ok(payload, result=out))

        debug_error("JsonTransformTool: neither path nor mapping provided")
        return DebugLog.embed(_err(
            "provide either 'path' or 'mapping'", error_type="InvalidInput",
        ))
