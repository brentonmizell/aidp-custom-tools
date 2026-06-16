"""
Python Runner Tool
==================
Run a block of Python code (dropped into a text box) in an isolated subprocess
and return stdout, stderr, return code, and an optional structured result.

This is the standalone form of the Custom Code capability. The code runs in a
separate python3 process (not the agent runtime), with a configurable timeout
and output cap.

Two conveniences over a bare exec:
  - Input injection: pass `data` (any JSON) and it is available in the script as
    a variable named `data`.
  - Result capture: if the script assigns a variable named `result`, the tool
    returns it as structured JSON in the `result` field (no need to print it).

Tracebacks keep the real line numbers of your code (it runs from its own file),
so errors point at the right line.
"""

import json
import os
import subprocess
import sys
import tempfile

from aidputils.agents.tools.custom_tools.base import CustomToolBase
from .utils.config_utils import get_cfg, ok, err

# Debug Channel — fall back to no-op shims if the runtime hasn't injected the
# helper module (e.g. local dev, unit tests).
try:
    from aidp_debug import debug, debug_warn, debug_error, DebugLog
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


# A small runner that execs the user's file with `data` predefined and captures
# a `result` variable if the user sets one. Keeping the user code in its own
# file (compiled with its real path) means tracebacks show correct line numbers.
#
# The runner also exposes AIDP file-IO helpers in the exec namespace:
#   aidp_read(uri)         -> bytes
#   aidp_read_text(uri)    -> str
#   aidp_write(uri, b)     -> dict
#   aidp_write_text(uri, s) -> dict
#   aidp_list(uri)         -> list[dict]
# Each is a closure over the tool's conf + context_vars (loaded from
# TOOL_CONF_PATH / TOOL_CTX_PATH). The aidp_io module + config_utils helper
# are staged in the runner's tmpdir so `import aidp_io` resolves without
# depending on the parent package layout.
#
# If `files` URIs were supplied, the runner pre-reads them into a dict named
# `files` keyed by the user's alias (or the URI's basename if no alias is
# provided) and exposes it in the namespace too.
_RUNNER = r'''
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_g = {"__name__": "__main__", "__file__": os.environ["USER_CODE"]}
try:
    _g["data"] = json.loads(os.environ.get("TOOL_INPUT", "null"))
except Exception:
    _g["data"] = None

# Load conf + context_vars passed in from the tool (JSON files written by the
# parent process). They drive the auth/region used by aidp_io.
def _load_json(env_key, default):
    p = os.environ.get(env_key, "")
    if not p:
        return default
    try:
        with open(p, "r") as f:
            return json.load(f)
    except Exception:
        return default

_conf = _load_json("TOOL_CONF_PATH", {})
_ctx = _load_json("TOOL_CTX_PATH", {})

# Bind AIDP IO helpers to the exec namespace as closures over (conf, ctx).
try:
    import aidp_io as _aidp_io

    def _bind_read(uri):
        return _aidp_io.read_file(uri, _conf, _ctx)
    def _bind_read_text(uri):
        return _aidp_io.read_text(uri, _conf, _ctx)
    def _bind_write(uri, content_bytes):
        return _aidp_io.write_file(uri, content_bytes, _conf, _ctx)
    def _bind_write_text(uri, content_str):
        return _aidp_io.write_text(uri, content_str, _conf, _ctx)
    def _bind_list(uri):
        return _aidp_io.list_files(uri, _conf, _ctx)

    _g["aidp_read"] = _bind_read
    _g["aidp_read_text"] = _bind_read_text
    _g["aidp_write"] = _bind_write
    _g["aidp_write_text"] = _bind_write_text
    _g["aidp_list"] = _bind_list
except Exception as _io_err:
    # Surface the import error lazily — user code that doesn't call these
    # helpers should still run. Calling any of them raises with the real cause.
    def _io_unavailable(*a, **k):
        raise RuntimeError(f"aidp_io is not available: {_io_err}")
    _g["aidp_read"] = _io_unavailable
    _g["aidp_read_text"] = _io_unavailable
    _g["aidp_write"] = _io_unavailable
    _g["aidp_write_text"] = _io_unavailable
    _g["aidp_list"] = _io_unavailable

# Pre-read any `files` URIs the caller requested into a {alias: bytes} dict.
_files = {}
_files_spec = _load_json("TOOL_FILES_PATH", [])
if isinstance(_files_spec, list):
    for _entry in _files_spec:
        if isinstance(_entry, str):
            _uri, _alias = _entry, None
        elif isinstance(_entry, dict):
            _uri = _entry.get("uri") or _entry.get("path") or ""
            _alias = _entry.get("alias") or _entry.get("name")
        else:
            continue
        if not _uri:
            continue
        if not _alias:
            # Derive an alias from the URI's basename (after the final '/').
            _tail = _uri.rsplit("/", 1)[-1]
            _alias = _tail or _uri
        try:
            _files[_alias] = _g["aidp_read"](_uri)
        except Exception as _e:
            # Record the read error so the user can detect it without aborting
            # the whole run — they may have provided multiple files.
            _files[_alias] = {"_error": str(_e), "_uri": _uri}
_g["files"] = _files

with open(os.environ["USER_CODE"], "r") as _f:
    _src = _f.read()
_code = compile(_src, os.environ["USER_CODE"], "exec")
exec(_code, _g)
_rp = os.environ.get("TOOL_RESULT_PATH")
if _rp and "result" in _g:
    try:
        with open(_rp, "w") as _o:
            json.dump(_g["result"], _o, default=str)
    except Exception as _e:
        with open(_rp, "w") as _o:
            json.dump({"_unserializable_result": str(_g["result"])[:2000]}, _o)
'''


@CustomToolBase.register
class RunPythonTool(CustomToolBase):
    """Run a block of Python code and return its output. The code runs in an
    isolated subprocess with a timeout. Optional input passed as `data` is
    available as a variable named `data`; if the code sets a variable named
    `result`, it is returned as structured JSON. Use for ad-hoc computation,
    data wrangling, or logic the other tools don't cover."""

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        debug("RunPythonTool._execute_tool start")
        code = runtime_params.get("code", "")
        if not code.strip():
            debug_error("RunPythonTool: missing code")
            return DebugLog.embed(err("code is required", "ValidationError", error="code is required"))

        stdin_data = runtime_params.get("stdin", "")
        input_data = runtime_params.get("data")
        files_spec = runtime_params.get("files")
        timeout = get_cfg(conf, "timeout", 60)
        max_lines = get_cfg(conf, "max_output_lines", 500)
        max_bytes = get_cfg(conf, "max_output_bytes", 1048576)
        python_bin = get_cfg(conf, "python_bin", sys.executable or "python3")
        debug(f"RunPythonTool: timeout={timeout}s max_lines={max_lines} max_bytes={max_bytes} python_bin={python_bin}")

        # Normalize input_data to a JSON string for the env var.
        if input_data is None:
            tool_input = "null"
        elif isinstance(input_data, str):
            # If it's already JSON, pass through; else wrap as a JSON string.
            try:
                json.loads(input_data)
                tool_input = input_data
            except Exception:
                tool_input = json.dumps(input_data)
        else:
            try:
                tool_input = json.dumps(input_data, default=str)
            except Exception:
                tool_input = "null"

        # Normalize files_spec to a list[dict|str] for the subprocess.
        files_list = _normalize_files_spec(files_spec)

        tmpdir = tempfile.mkdtemp(prefix="pyrun_")
        user_path = os.path.join(tmpdir, "user_code.py")
        runner_path = os.path.join(tmpdir, "_runner.py")
        result_path = os.path.join(tmpdir, "result.json")
        conf_path = os.path.join(tmpdir, "conf.json")
        ctx_path = os.path.join(tmpdir, "ctx.json")
        files_path = os.path.join(tmpdir, "files.json")
        # Stage aidp_io + config_utils next to the runner so `import aidp_io`
        # works in the subprocess without depending on the parent package.
        aidp_io_stage = os.path.join(tmpdir, "aidp_io.py")
        config_utils_stage = os.path.join(tmpdir, "config_utils.py")
        try:
            with open(user_path, "w") as f:
                f.write(code)
            with open(runner_path, "w") as f:
                f.write(_RUNNER)
            try:
                _stage_io_modules(aidp_io_stage, config_utils_stage)
            except Exception as e:
                debug_warn(f"RunPythonTool: aidp_io stage failed: {e}")
            # Conf + context vars: serialize what's JSON-safe; drop the rest.
            with open(conf_path, "w") as f:
                json.dump(_jsonable(conf), f, default=str)
            with open(ctx_path, "w") as f:
                json.dump(_jsonable(context_vars), f, default=str)
            with open(files_path, "w") as f:
                json.dump(files_list, f, default=str)

            env = dict(os.environ)
            env["USER_CODE"] = user_path
            env["TOOL_INPUT"] = tool_input
            env["TOOL_RESULT_PATH"] = result_path
            env["TOOL_CONF_PATH"] = conf_path
            env["TOOL_CTX_PATH"] = ctx_path
            env["TOOL_FILES_PATH"] = files_path

            # Clean stdout/stderr capture via subprocess pipes (no shared
            # namespace dict, no in-process exec). Tracebacks remain accurate
            # because we compile the user file with its real path above.
            proc = subprocess.run(
                [python_bin, runner_path],
                input=stdin_data if stdin_data else None,
                capture_output=True, text=True,
                timeout=int(timeout), env=env, cwd=tmpdir,
            )

            stdout_trunc, stdout_was_truncated = _truncate(proc.stdout, max_lines, max_bytes)
            stderr_trunc, stderr_was_truncated = _truncate(proc.stderr, max_lines, max_bytes)
            truncated = stdout_was_truncated or stderr_was_truncated

            data = {
                "returncode": proc.returncode,
                "stdout": stdout_trunc,
                "stderr": stderr_trunc,
                "truncated": truncated,
            }
            # Pull captured result if the script set one.
            if os.path.exists(result_path):
                try:
                    with open(result_path) as rf:
                        data["result"] = json.load(rf)
                except Exception as e:
                    debug_warn(f"RunPythonTool: could not parse captured result: {e}")

            # Non-zero exit means the user's code raised. Surface as an error so
            # the framework marks isError, but keep stdout/stderr for debugging.
            if proc.returncode != 0:
                err_msg = _last_traceback_line(proc.stderr) or f"exited with code {proc.returncode}"
                debug_error(f"RunPythonTool: user code failed: {err_msg}")
                # Preserve legacy top-level keys (returncode/stdout/stderr/result/error)
                # alongside the envelope so existing callers keep working.
                return DebugLog.embed(err(
                    err_msg, "UserCodeError",
                    returncode=data["returncode"],
                    stdout=data["stdout"],
                    stderr=data["stderr"],
                    truncated=truncated,
                    result=data.get("result"),
                ))

            debug("RunPythonTool: success")
            return DebugLog.embed(ok(
                data,
                returncode=data["returncode"],
                stdout=data["stdout"],
                stderr=data["stderr"],
                truncated=truncated,
                result=data.get("result"),
            ))

        except subprocess.TimeoutExpired:
            debug_error(f"RunPythonTool: timed out after {timeout}s")
            return DebugLog.embed(err(
                f"execution timed out after {timeout}s", "TimeoutError",
            ))
        except Exception as e:
            debug_error(f"RunPythonTool: unexpected error: {e}")
            return DebugLog.embed(err(str(e), type(e).__name__))
        finally:
            for p in (user_path, runner_path, result_path,
                      conf_path, ctx_path, files_path,
                      aidp_io_stage, config_utils_stage):
                try:
                    os.remove(p)
                except Exception:
                    pass
            try:
                os.rmdir(tmpdir)
            except Exception:
                pass


# --------------------------------------------------------------------------- #
def _normalize_files_spec(files):
    """Coerce the `files` runtime param into a list[dict|str] for the subprocess.

    Accepts:
      - list of URIs (str)
      - list of {"uri"/"path", optional "alias"/"name"} dicts
      - dict of {alias: uri}
      - a JSON-string encoding any of the above
    Returns [] for None / unrecognized input.
    """
    if files is None or files == "":
        return []
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except Exception:
            # Comma-separated fallback for plain string lists.
            return [u.strip() for u in files.split(",") if u.strip()]
    if isinstance(files, dict):
        out = []
        for alias, uri in files.items():
            if isinstance(uri, str) and uri:
                out.append({"alias": alias, "uri": uri})
        return out
    if isinstance(files, list):
        out = []
        for entry in files:
            if isinstance(entry, str):
                if entry.strip():
                    out.append(entry.strip())
            elif isinstance(entry, dict):
                uri = entry.get("uri") or entry.get("path")
                if uri:
                    item = {"uri": uri}
                    alias = entry.get("alias") or entry.get("name")
                    if alias:
                        item["alias"] = alias
                    out.append(item)
        return out
    return []


def _jsonable(value):
    """Drop non-JSON-serializable entries from a dict so json.dump won't fail.
    Preserves the keys auth/region/etc. that aidp_io reads."""
    if not isinstance(value, dict):
        try:
            json.dumps(value, default=str)
            return value
        except Exception:
            return {}
    out = {}
    for k, v in value.items():
        try:
            json.dumps(v, default=str)
            out[k] = v
        except Exception:
            # Skip handles, signers, file objects, etc.
            continue
    return out


def _stage_io_modules(aidp_io_stage, config_utils_stage):
    """Copy aidp_io.py + config_utils.py from this package's utils/ into the
    runner's tmpdir so `import aidp_io` resolves in the subprocess."""
    import shutil
    here = os.path.dirname(os.path.abspath(__file__))
    src_io = os.path.join(here, "utils", "aidp_io.py")
    src_cfg = os.path.join(here, "utils", "config_utils.py")
    shutil.copyfile(src_io, aidp_io_stage)
    shutil.copyfile(src_cfg, config_utils_stage)
    # The staged aidp_io uses 'from .config_utils import get_cfg' — rewrite to
    # a flat import since the staged copy isn't inside a package.
    with open(aidp_io_stage, "r") as f:
        src = f.read()
    src = src.replace("from .config_utils import get_cfg",
                      "from config_utils import get_cfg")
    with open(aidp_io_stage, "w") as f:
        f.write(src)


def _truncate(text, max_lines, max_bytes=None):
    """Truncate by both line count and total byte size. Returns
    (truncated_text, was_truncated)."""
    if not text:
        return "", False
    try:
        max_lines = int(max_lines)
    except (TypeError, ValueError):
        max_lines = 500
    try:
        max_bytes = int(max_bytes) if max_bytes is not None else None
    except (TypeError, ValueError):
        max_bytes = None
    was_truncated = False
    lines = text.rstrip("\n").split("\n")
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... ({len(lines) - max_lines} more lines truncated)"]
        was_truncated = True
    result = "\n".join(lines)
    if max_bytes is not None and len(result.encode("utf-8", errors="replace")) > max_bytes:
        # Slice on character boundary that fits within the byte cap.
        encoded = result.encode("utf-8", errors="replace")[:max_bytes]
        result = encoded.decode("utf-8", errors="replace") + f"\n... (output truncated to {max_bytes} bytes)"
        was_truncated = True
    return result, was_truncated


def _last_traceback_line(stderr):
    if not stderr:
        return ""
    lines = [ln for ln in stderr.strip().split("\n") if ln.strip()]
    return lines[-1] if lines else ""


# =============================================================================
#  RunNotebookTool — execute a .ipynb from the workspace on the AIDP kernel
# =============================================================================
#  Reuses the proven AIDP notebook protocol (utils/jupyter_protocol.py,
#  utils/oci_signer.py) from the Spark tool. Reads a workspace notebook, creates
#  a kernel session, runs each code cell in order over the signed WebSocket, and
#  returns per-cell output. Unlike RunPythonTool (which runs locally in a
#  subprocess), this runs against the real AIDP kernel/cluster, so the notebook
#  gets the live workspace context (Spark session, datalake, etc.).
# =============================================================================

import time as _time
import uuid as _uuid
from urllib.parse import quote as _quote

_API_VERSION = "20260430"   # Live AIDP REST surface — /aiDataPlatforms/{lake}/...
_WS_SUBPROTOCOL = "v1.kernel.websocket.jupyter.org"


@CustomToolBase.register
class RunNotebookTool(CustomToolBase):
    """Run a Jupyter notebook (.ipynb) from the workspace against the AIDP
    kernel and return each code cell's output. Provide notebook_path (a Workspace
    path) and the tool reads it, or pass notebook_json directly. Use to execute
    an existing workspace notebook as part of a flow."""

    @classmethod
    def _validate_config(cls, conf, runtime_params=None, **context_vars):
        for k in ("aidp_endpoint", "lake_ocid", "ws_host"):
            if not get_cfg(conf, k, ""):
                raise ValueError(f"{k} is required in tool config")

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        debug("RunNotebookTool._execute_tool start")
        from .utils.oci_signer import get_auth_provider, make_signed_request, sign_request
        from .utils.jupyter_protocol import (
            make_execute_request, make_kernel_info_request, decode_binary_message,
        )

        aidp_endpoint = get_cfg(conf, "aidp_endpoint", "").rstrip("/")
        lake_ocid = get_cfg(conf, "lake_ocid", "")
        ws_host = get_cfg(conf, "ws_host", "")
        workspace_key = get_cfg(conf, "workspace_key", "default")
        cluster_key = get_cfg(conf, "cluster_key", "default_cluster")
        oci_profile = get_cfg(conf, "oci_config_profile", "DEFAULT")
        api_version = get_cfg(conf, "api_version", _API_VERSION) or _API_VERSION
        # AIDP's live REST surface uses /aiDataPlatforms/. Override only if
        # AIDP exposes a non-standard path on a specific tenancy.
        service_path = get_cfg(conf, "service_path", "") or "aiDataPlatforms"
        timeout = get_cfg(conf, "execution_timeout", 120)
        connect_timeout = get_cfg(conf, "connect_timeout", 30)
        max_cells = get_cfg(conf, "max_cells", 200)
        max_output_chars = get_cfg(conf, "max_output_chars", 20000)
        max_notebook_bytes = get_cfg(conf, "max_notebook_bytes", 5 * 1024 * 1024)
        ws_retries = get_cfg(conf, "ws_retries", 3)
        debug(f"RunNotebookTool: workspace={workspace_key} cluster={cluster_key} timeout={timeout}s max_cells={max_cells}")

        if not (aidp_endpoint and lake_ocid and ws_host):
            debug_error("RunNotebookTool: missing required config")
            return DebugLog.embed(err(
                "aidp_endpoint, lake_ocid, and ws_host are required in config",
                "ValidationError",
            ))

        base_url = f"{aidp_endpoint}/{api_version}/{service_path}/{_quote(lake_ocid, safe='')}"

        try:
            signer = get_auth_provider(oci_profile)
        except Exception as e:
            debug_error(f"RunNotebookTool: signer init failed: {e}")
            return DebugLog.embed(err(f"could not initialize OCI signer: {e}", "AuthError"))

        # 1. Get the notebook JSON (from a workspace path, or passed directly).
        nb_truncated = False
        try:
            default_path = get_cfg(conf, "default_notebook_path", "")
            nb = cls._load_notebook(
                signer, base_url, runtime_params, timeout, make_signed_request,
                default_path, int(max_notebook_bytes),
            )
        except Exception as e:
            debug_error(f"RunNotebookTool: notebook load failed: {e}")
            return DebugLog.embed(_nb_err(e))
        if isinstance(nb, dict) and "error" in nb and "cells" not in nb:
            debug_error(f"RunNotebookTool: notebook load error: {nb.get('error')}")
            return DebugLog.embed(err(nb["error"], "NotebookLoadError"))
        if isinstance(nb, dict) and nb.get("_truncated"):
            nb_truncated = True

        # 2. Pull ordered code cells.
        code_cells = []
        for cell in nb.get("cells", []):
            if cell.get("cell_type") == "code":
                src = cell.get("source", "")
                if isinstance(src, list):
                    src = "".join(src)
                if src.strip():
                    code_cells.append(src)
        if not code_cells:
            debug_warn("RunNotebookTool: notebook has no executable code cells")
            return DebugLog.embed(err("notebook has no executable code cells", "EmptyNotebookError"))
        cells_truncated = len(code_cells) > int(max_cells)
        code_cells = code_cells[:int(max_cells)]

        # 3. Create a session/kernel, run each cell, collect output, clean up.
        session_id = None
        try:
            session_id, kernel_id = cls._create_session(
                signer, base_url, workspace_key, cluster_key, make_signed_request,
            )
            cell_results = cls._run_cells(
                signer, ws_host, lake_ocid, workspace_key, kernel_id, session_id,
                code_cells, int(timeout), int(connect_timeout), int(ws_retries),
                sign_request, make_execute_request, make_kernel_info_request,
                decode_binary_message, int(max_output_chars),
                api_version=api_version, service_path=service_path,
            )
            failed = next((c for c in cell_results if c.get("error")), None)
            truncated = nb_truncated or cells_truncated or any(c.get("truncated") for c in cell_results)
            data = {
                "cells_run": len(cell_results),
                "cells": cell_results,
                "truncated": truncated,
            }
            if failed:
                err_msg = f"cell {failed['cell']} failed: {failed['error'][:300]}"
                debug_error(f"RunNotebookTool: {err_msg}")
                # Preserve legacy keys: ok=False (envelope) with cells/cells_run at top level.
                return DebugLog.embed(err(
                    err_msg, "CellExecutionError",
                    cells_run=data["cells_run"],
                    cells=data["cells"],
                    truncated=truncated,
                ))
            debug(f"RunNotebookTool: success, {len(cell_results)} cell(s) ran")
            return DebugLog.embed(ok(
                data,
                cells_run=data["cells_run"],
                cells=data["cells"],
                truncated=truncated,
            ))
        except Exception as e:
            debug_error(f"RunNotebookTool: execution failed: {e}")
            return DebugLog.embed(_nb_err(e))
        finally:
            if session_id:
                try:
                    cls._delete_session(signer, base_url, workspace_key, session_id, make_signed_request)
                except Exception as e:
                    debug_warn(f"RunNotebookTool: cleanup failed: {e}")

    # ------------------------------------------------------------------ #
    @classmethod
    def _load_notebook(cls, signer, base_url, runtime_params, timeout,
                       make_signed_request, default_path="", max_bytes=5 * 1024 * 1024):
        import json as _json
        raw = runtime_params.get("notebook_json", "")
        if raw:
            if isinstance(raw, str):
                if len(raw.encode("utf-8", errors="replace")) > max_bytes:
                    return {"error": f"notebook_json exceeds max_notebook_bytes ({max_bytes})"}
                return _json.loads(raw)
            return raw

        # The notebook_path parameter overrides the configured default; if the
        # parameter is empty, fall back to default_notebook_path from config.
        path = runtime_params.get("notebook_path", "") or default_path
        if not path:
            return {"error": "provide notebook_path (a Workspace path), notebook_json, or set default_notebook_path in config"}

        # Workspace file read: downloadFileMeta with path/type as HEADERS -> parUrl -> GET
        import requests
        url = f"{base_url}/actions/downloadFileMeta"
        resp = make_signed_request(signer, "POST", url, body="",
                                   additional_headers={"path": path, "type": "FILE", "accept": "application/json"},
                                   timeout=int(timeout))
        meta = resp.json()
        par = meta.get("parUrl")
        if not par:
            return {"error": f"could not read notebook at '{path}' (no parUrl). Check the Workspace path."}
        # Stream with a hard byte cap so a giant notebook doesn't blow memory.
        blob = requests.get(par, timeout=int(timeout), stream=True)
        blob.raise_for_status()
        chunks = []
        total = 0
        truncated = False
        for chunk in blob.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                truncated = True
                break
            chunks.append(chunk)
        if truncated:
            return {"error": f"notebook at '{path}' exceeds max_notebook_bytes ({max_bytes})"}
        nb_obj = _json.loads(b"".join(chunks).decode("utf-8"))
        if isinstance(nb_obj, dict):
            nb_obj["_truncated"] = False
        return nb_obj

    @classmethod
    def _create_session(cls, signer, base_url, workspace_key, cluster_key, make_signed_request):
        import json as _json
        ts = int(_time.time())
        name = f"agent_runnotebook_{ts}.ipynb"
        url = f"{base_url}/workspaces/{_quote(workspace_key, safe='')}/notebook/api/sessions"
        body = _json.dumps({
            "type": "notebook", "name": name,
            "kernel": {"name": "notebook"},
            "path": f"Workspace/{name}", "cluster_id": cluster_key,
        })
        resp = make_signed_request(signer, "POST", url, body=body,
                                   additional_headers={"accept": "application/json"})
        data = resp.json()
        return data["id"], data["kernel"]["id"]

    @classmethod
    def _delete_session(cls, signer, base_url, workspace_key, session_id, make_signed_request):
        url = (f"{base_url}/workspaces/{_quote(workspace_key, safe='')}"
               f"/notebook/api/sessions/{_quote(session_id, safe='')}")
        make_signed_request(signer, "DELETE", url)

    @classmethod
    def _run_cells(cls, signer, ws_host, lake_ocid, workspace_key, kernel_id, session_id,
                   code_cells, timeout, connect_timeout, ws_retries,
                   sign_request, make_execute_request,
                   make_kernel_info_request, decode_binary_message, max_output_chars,
                   api_version=_API_VERSION, service_path="aiDataPlatforms"):
        import websocket

        req_id = str(_uuid.uuid4())
        ws_path = (
            f"/{api_version}/{service_path}/{_quote(lake_ocid, safe='')}"
            f"/notebook/workspaces/{_quote(workspace_key, safe='')}"
            f"/api/kernels/{_quote(kernel_id, safe='')}/channels"
            f"?session_id={req_id}---{session_id}"
        )
        ws_url = f"wss://{ws_host}{ws_path}"
        sign_url = f"https://{ws_host}{ws_path}"

        ws = cls._ws_connect_with_retry(
            websocket, signer, sign_request, ws_url, sign_url, ws_host,
            connect_timeout, ws_retries,
        )
        results = []
        try:
            # kernel ready
            _, info_msg = make_kernel_info_request(session_id)
            ws.send_binary(info_msg)
            cls._wait_ready(ws, decode_binary_message, timeout)

            for i, code in enumerate(code_cells, 1):
                msg_id, exec_msg = make_execute_request(session_id, code)
                ws.send_binary(exec_msg)
                output, error = cls._collect_cell(ws, msg_id, timeout, decode_binary_message)
                truncated = len(output) > max_output_chars
                results.append({
                    "cell": i,
                    "output": output[:max_output_chars],
                    "error": error,
                    "truncated": truncated,
                })
                if error:
                    break  # stop_on_error semantics
            return results
        finally:
            try:
                ws.close()
            except Exception:
                pass

    @classmethod
    def _ws_connect_with_retry(cls, websocket, signer, sign_request, ws_url, sign_url,
                                ws_host, connect_timeout, ws_retries):
        """Connect the websocket with bounded retry. Each attempt re-signs the
        request so a fresh timestamp/auth is used. Backs off with a short
        capped delay between attempts."""
        last_err = None
        attempts = max(1, int(ws_retries))
        for attempt in range(1, attempts + 1):
            try:
                ws_headers = sign_request(signer, "GET", sign_url, additional_headers={"host": ws_host})
                ws_headers.pop("host", None)
                ws_headers.pop("Host", None)
                header_list = [f"{k}: {v}" for k, v in ws_headers.items()]
                ws = websocket.WebSocket(sslopt={"cert_reqs": 0})
                ws.connect(
                    ws_url, header=header_list,
                    subprotocols=[_WS_SUBPROTOCOL], timeout=connect_timeout,
                )
                if attempt > 1:
                    debug(f"RunNotebookTool: WS connect succeeded on attempt {attempt}")
                return ws
            except Exception as e:
                last_err = e
                debug_warn(f"RunNotebookTool: WS connect attempt {attempt}/{attempts} failed: {e}")
                if attempt < attempts:
                    _time.sleep(min(2 ** (attempt - 1), 5))
        raise last_err if last_err else RuntimeError("websocket connection failed")

    @classmethod
    def _wait_ready(cls, ws, decode_binary_message, timeout):
        import websocket as ws_module
        deadline = _time.time() + min(60, timeout)
        while _time.time() < deadline:
            ws.settimeout(max(0.1, deadline - _time.time()))
            try:
                frame = ws.recv()
            except ws_module.WebSocketTimeoutException:
                return
            msg = decode_binary_message(frame) if frame else None
            if isinstance(msg, dict):
                mt = msg.get("header", {}).get("msg_type", "")
                if mt in ("kernel_info_reply", "status"):
                    return

    @classmethod
    def _collect_cell(cls, ws, msg_id, timeout, decode_binary_message):
        import re as _re
        import websocket as ws_module
        outs, errs = [], []
        ansi = _re.compile(r"\x1b\[[0-9;]*m")
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            ws.settimeout(max(0.1, deadline - _time.time()))
            try:
                frame = ws.recv()
            except ws_module.WebSocketTimeoutException:
                break
            if not frame:
                continue
            msg = decode_binary_message(frame)
            if not isinstance(msg, dict):
                continue
            header = msg.get("header", {}); parent = msg.get("parent_header", {}); content = msg.get("content", {})
            mt = header.get("msg_type", "")
            if parent.get("msg_id") != msg_id and mt != "status":
                continue
            if mt == "stream":
                text = content.get("text", "")
                if text.startswith("[{") and '"stages"' in text:
                    continue
                outs.append(text)
            elif mt in ("execute_result", "display_data"):
                outs.append(content.get("data", {}).get("text/plain", ""))
            elif mt == "error":
                tb = content.get("traceback", [])
                errs.append("\n".join(ansi.sub("", l) for l in tb) if tb
                            else f"{content.get('ename','Error')}: {content.get('evalue','')}")
            elif mt == "execute_reply":
                if content.get("status") == "error" and not errs:
                    errs.append(f"{content.get('ename','Error')}: {content.get('evalue','')}")
                break
        return "".join(o for o in outs if o), ("\n".join(errs) if errs else "")


def _nb_err(e):
    detail = str(e)
    resp = getattr(e, "response", None)
    if resp is not None:
        try:
            detail += f" | {resp.text[:300]}"
        except Exception:
            pass
    return err(detail, type(e).__name__ if not isinstance(e, str) else "ToolError")
