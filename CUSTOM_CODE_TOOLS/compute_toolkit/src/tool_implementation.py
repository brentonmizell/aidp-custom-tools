"""
Compute Toolkit
===============
  MathTool            - evaluate arithmetic safely (no eval) + basic stats
  SchemaValidatorTool - validate data against a JSON Schema

MathTool exists because LLMs are unreliable at arithmetic. It parses the
expression into an AST and only allows a safe set of nodes/operators, so there
is no code-execution risk.

Returns a standardized envelope:
  Success: {"ok": true, "data": {...}, ...legacy keys for back-compat...}
  Error:   {"ok": false, "error": "...", "error_type": "...", ...legacy keys...}
"""

import ast
import json
import math
import operator

from aidputils.agents.tools.custom_tools.base import CustomToolBase
from .utils.config_utils import get_cfg

# Debug channel with no-op fallback so this module imports even when the AIDP
# runtime hasn't injected the helper module.
try:
    from aidp_debug import debug, debug_warn, debug_error, DebugLog  # type: ignore
except ImportError:  # pragma: no cover
    def debug(*args, **kwargs): pass
    def debug_warn(*args, **kwargs): pass
    def debug_error(*args, **kwargs): pass
    class DebugLog:  # type: ignore
        @staticmethod
        def embed(result):
            return result


# --------------------------------------------------------------------------- #
# Safe expression evaluator
# --------------------------------------------------------------------------- #
_BIN_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {
    "abs": abs, "round": round, "min": min, "max": max, "sum": sum,
    "sqrt": math.sqrt, "log": math.log, "log10": math.log10, "exp": math.exp,
    "floor": math.floor, "ceil": math.ceil, "pow": math.pow,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
}
_NAMES = {"pi": math.pi, "e": math.e}

# Explicit denylist: AST node types that could escape the sandbox even if a bug
# accidentally accepted them. We refuse Attribute access (blocks getattr-chain
# tricks like (1).__class__.__base__.__subclasses__()), subscripts, comprehensions,
# lambdas, and any name lookup outside the _NAMES allowlist.
_DENIED_NODES = (
    ast.Attribute,
    ast.Subscript,
    ast.Lambda,
    ast.GeneratorExp,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.IfExp,
    ast.Await,
    ast.Yield,
    ast.YieldFrom,
    ast.Starred,
    ast.NamedExpr,
)

# Identifiers banned even if they showed up as a Name — defensive belt-and-braces.
_DENIED_NAMES = {
    "__builtins__", "__import__", "__class__", "__base__", "__bases__",
    "__subclasses__", "__globals__", "__getattribute__", "__dict__",
    "__mro__", "__code__", "__closure__", "getattr", "setattr", "delattr",
    "eval", "exec", "compile", "open", "globals", "locals", "vars",
    "__loader__", "__spec__",
}


def _safe_eval(node, depth=0):
    if depth > 50:
        raise ValueError("expression nested too deeply")
    if isinstance(node, _DENIED_NODES):
        raise ValueError(f"disallowed expression element: {type(node).__name__}")
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body, depth + 1)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("only numeric constants allowed")
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](
            _safe_eval(node.left, depth + 1),
            _safe_eval(node.right, depth + 1),
        )
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_safe_eval(node.operand, depth + 1))
    if isinstance(node, ast.Name):
        if node.id in _DENIED_NAMES:
            raise ValueError(f"disallowed name: {node.id}")
        if node.id in _NAMES:
            return _NAMES[node.id]
        raise ValueError(f"unknown name: {node.id}")
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in _DENIED_NAMES:
            raise ValueError(f"disallowed call: {node.func.id}")
        if node.func.id in _FUNCS:
            args = [_safe_eval(a, depth + 1) for a in node.args]
            if node.keywords:
                raise ValueError("keyword arguments not allowed")
            return _FUNCS[node.func.id](*args)
        raise ValueError(f"unknown function: {node.func.id}")
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_safe_eval(e, depth + 1) for e in node.elts]
    raise ValueError(f"disallowed expression element: {type(node).__name__}")


def _ok(data, **extra):
    out = {"ok": True, "data": data}
    out.update(extra)
    return out


def _err(error, error_type="ToolError", **extra):
    out = {"ok": False, "error": str(error), "error_type": error_type}
    out.update(extra)
    return out


@CustomToolBase.register
class MathTool(CustomToolBase):
    """Evaluate an arithmetic expression safely, or compute summary statistics
    over a list of numbers. Use this instead of letting the model do arithmetic
    in its head. Supports + - * / // % **, parentheses, and functions like
    sqrt, log, sin, abs, round, min, max, sum, plus constants pi and e."""

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        max_expr_len = get_cfg(conf, "max_expression_length", 4000)
        max_stats_values = get_cfg(conf, "max_stats_values", 100000)
        mode = (runtime_params.get("mode", "eval") or "eval").lower()
        debug(f"MathTool: mode={mode}")

        if mode not in ("eval", "stats"):
            debug_error(f"MathTool: invalid mode {mode!r}")
            result = _err("mode must be 'eval' or 'stats'", "InvalidMode")
            return DebugLog.embed(result)

        if mode == "eval":
            expr = runtime_params.get("expression", "") or ""
            if not expr.strip():
                debug_error("MathTool: empty expression")
                result = _err("expression is required", "InvalidInput")
                return DebugLog.embed(result)
            if len(expr) > max_expr_len:
                debug_error(f"MathTool: expression too long ({len(expr)} > {max_expr_len})")
                result = _err(
                    f"expression too long ({len(expr)} chars; max {max_expr_len})",
                    "InputTooLarge",
                    truncated=True,
                )
                return DebugLog.embed(result)
            try:
                tree = ast.parse(expr, mode="eval")
                value = _safe_eval(tree)
                debug(f"MathTool: eval ok -> {value}")
                result = _ok(
                    {"expression": expr, "result": value},
                    expression=expr,
                    result=value,
                )
                return DebugLog.embed(result)
            except ZeroDivisionError:
                debug_error("MathTool: division by zero")
                result = _err("division by zero", "ZeroDivisionError")
                return DebugLog.embed(result)
            except Exception as e:
                debug_error(f"MathTool: eval failed: {e}")
                result = _err(f"could not evaluate: {e}", "EvalError")
                return DebugLog.embed(result)

        # mode == "stats"
        values = runtime_params.get("values", [])
        if isinstance(values, str):
            try:
                values = json.loads(values)
            except Exception:
                debug_error("MathTool: values not valid JSON")
                result = _err(
                    "values must be a list of numbers or JSON array",
                    "InvalidInput",
                )
                return DebugLog.embed(result)
        if not isinstance(values, (list, tuple)):
            debug_error("MathTool: values not a list")
            result = _err("values must be a list of numbers", "InvalidInput")
            return DebugLog.embed(result)

        truncated = False
        if len(values) > max_stats_values:
            debug_warn(
                f"MathTool: stats truncated {len(values)} -> {max_stats_values}"
            )
            values = list(values)[:max_stats_values]
            truncated = True

        try:
            import numpy as np
            arr = np.array([float(v) for v in values], dtype=float)
            if arr.size == 0:
                debug_error("MathTool: values is empty")
                result = _err("values is empty", "InvalidInput")
                return DebugLog.embed(result)
            stats = {
                "count": int(arr.size),
                "sum": float(arr.sum()),
                "mean": float(arr.mean()),
                "median": float(np.median(arr)),
                "min": float(arr.min()),
                "max": float(arr.max()),
                "std": float(arr.std()),
                "p25": float(np.percentile(arr, 25)),
                "p75": float(np.percentile(arr, 75)),
            }
            debug(f"MathTool: stats ok (n={stats['count']})")
            # Preserve legacy top-level keys for back-compat.
            result = _ok(stats, truncated=truncated, **stats)
            return DebugLog.embed(result)
        except Exception as e:
            debug_error(f"MathTool: stats failed: {e}")
            result = _err(str(e), type(e).__name__)
            return DebugLog.embed(result)


# --------------------------------------------------------------------------- #
# Schema validator
# --------------------------------------------------------------------------- #
@CustomToolBase.register
class SchemaValidatorTool(CustomToolBase):
    """Validate a data object against a JSON Schema and report whether it passes,
    with a list of every violation. Use as a quality gate before passing data
    downstream or to confirm an API response has the expected shape."""

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        max_violations = get_cfg(conf, "max_violations", 500)
        mode = (runtime_params.get("mode", "best_effort") or "best_effort").lower()
        debug(f"SchemaValidatorTool: mode={mode}")

        if mode not in ("best_effort", "fail_fast"):
            debug_error(f"SchemaValidatorTool: invalid mode {mode!r}")
            result = _err(
                "mode must be 'best_effort' or 'fail_fast'",
                "InvalidMode",
            )
            return DebugLog.embed(result)

        data = runtime_params.get("data")
        schema = runtime_params.get("schema")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                debug_error("SchemaValidatorTool: data not valid JSON")
                result = _err(
                    "data must be an object/array or valid JSON",
                    "InvalidInput",
                )
                return DebugLog.embed(result)
        if isinstance(schema, str):
            try:
                schema = json.loads(schema)
            except Exception:
                debug_error("SchemaValidatorTool: schema not valid JSON")
                result = _err(
                    "schema must be a JSON Schema object or valid JSON",
                    "InvalidInput",
                )
                return DebugLog.embed(result)
        if not isinstance(schema, dict):
            debug_error("SchemaValidatorTool: schema not a dict")
            result = _err(
                "schema is required and must be a JSON Schema object",
                "InvalidInput",
            )
            return DebugLog.embed(result)

        try:
            from jsonschema import Draft202012Validator
            validator = Draft202012Validator(schema)

            if mode == "fail_fast":
                # validate() raises on first violation
                try:
                    validator.validate(data)
                    payload = {
                        "valid": True,
                        "violation_count": 0,
                        "violations": [],
                        "mode": "fail_fast",
                    }
                    debug("SchemaValidatorTool: fail_fast ok")
                    result = _ok(payload, **payload)
                    return DebugLog.embed(result)
                except Exception as ve:
                    violation = {
                        "path": "/".join(str(p) for p in getattr(ve, "path", [])) or "(root)",
                        "message": getattr(ve, "message", str(ve)),
                        "validator": getattr(ve, "validator", None),
                    }
                    payload = {
                        "valid": False,
                        "violation_count": 1,
                        "violations": [violation],
                        "mode": "fail_fast",
                        "truncated": False,
                    }
                    debug(f"SchemaValidatorTool: fail_fast violation at {violation['path']}")
                    result = _ok(payload, **payload)
                    return DebugLog.embed(result)

            # best_effort: gather all violations, bounded by max_violations
            truncated = False
            errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
            total = len(errors)
            if total > max_violations:
                errors = errors[:max_violations]
                truncated = True
                debug_warn(
                    f"SchemaValidatorTool: violations truncated {total} -> {max_violations}"
                )
            violations = [
                {
                    "path": "/".join(str(p) for p in e.path) or "(root)",
                    "message": e.message,
                    "validator": e.validator,
                }
                for e in errors
            ]
            payload = {
                "valid": total == 0,
                "violation_count": total,
                "violations": violations,
                "mode": "best_effort",
                "truncated": truncated,
            }
            debug(
                f"SchemaValidatorTool: best_effort total={total} returned={len(violations)} truncated={truncated}"
            )
            result = _ok(payload, **payload)
            return DebugLog.embed(result)
        except Exception as e:
            debug_error(f"SchemaValidatorTool: failed: {e}")
            result = _err(str(e), type(e).__name__)
            return DebugLog.embed(result)
