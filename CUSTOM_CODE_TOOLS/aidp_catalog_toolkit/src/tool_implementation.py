"""
AIDP Catalog Toolkit
====================
Tools that work against the AIDP Data Lake REST API (resource-principal signed)
to operate on Standard Catalogs: read/list/write volume files, browse catalog
metadata, and trigger KB ingestion.

  CatalogFileTool     - read or list files in a Standard Catalog volume
  VolumeWriteTool     - write a file into a Standard Catalog volume
  CatalogBrowserTool  - list catalogs/schemas/tables/volumes/KBs, describe a table
  KBIngestTool        - list KB ingestion jobs, trigger a run
  WorkspaceFileTool   - read/list workspace files (Jupyter Contents API)

Base URL: https://aidp.<region>.oci.oraclecloud.com/<apiVersion>/aiDataPlatforms/<dataLakeOcid>
          (apiVersion default 20260430; resource segment 'aiDataPlatforms')
Auth:     configurable via auth_mode - resource_principal (default), user_principal
          (creds from config / Credential Store), or instance_principal. No keys
          are hard-coded in the tool source.

Endpoint conventions (matching the AIDP Flow Designer client):
  - List endpoints: GET with query params, response under res["items"].
  - File "meta" actions (upload/download): POST where the file `path` (and
    `type`) are sent as HTTP HEADERS, not in the JSON body. The action returns a
    pre-authenticated URL (parUrl); the bytes are then PUT/GET on that URL.

Returns a structured envelope: {"ok": true, "data": {...}, ...legacy fields}
or {"ok": false, "error": "...", "error_type": "..."}. Legacy top-level fields
(volume_key, path, content, etc.) are preserved alongside the envelope so
callers reading them directly keep working.

File IO delegation
------------------
All file IO (read/write/list against master: volumes or workspace: paths) and,
where available, catalog browsing + KB job control delegate to the shared
``aidp_io`` module so every custom toolkit speaks the same URI contract.
Legacy code paths remain in this file as graceful fallbacks for the catalog
browsing / KB helpers that aidp_io does not yet export, and for the
name-resolution semantics (allow_auto_pick) that are specific to this toolkit.
"""

import json
import os

from aidputils.agents.tools.custom_tools.base import CustomToolBase
from .utils.config_utils import get_cfg, ok, fail

# Debug Channel — graceful no-op fallback if the runtime doesn't inject it.
try:
    from aidp_debug import debug, debug_warn, debug_error, DebugLog
except ImportError:
    def debug(*args, **kwargs): pass
    def debug_warn(*args, **kwargs): pass
    def debug_error(*args, **kwargs): pass
    class DebugLog:
        @staticmethod
        def embed(result):
            return result


# --------------------------------------------------------------------------- #
# Shared aidp_io import (import-guarded so partial rollouts still work)
# --------------------------------------------------------------------------- #
# Newer aidp_io revisions are expected to add catalog browsing + KB helpers.
# We try to import every symbol we might use; missing ones get set to None and
# the relevant code paths fall back to direct REST calls below.
try:
    from .utils.aidp_io import parse_uri as _io_parse_uri
except Exception:  # pragma: no cover - shared module missing entirely
    _io_parse_uri = None
try:
    from .utils.aidp_io import read_file as _io_read_file
except Exception:
    _io_read_file = None
try:
    from .utils.aidp_io import write_file as _io_write_file
except Exception:
    _io_write_file = None
try:
    from .utils.aidp_io import read_text as _io_read_text
except Exception:
    _io_read_text = None
try:
    from .utils.aidp_io import write_text as _io_write_text
except Exception:
    _io_write_text = None
try:
    from .utils.aidp_io import list_files as _io_list_files
except Exception:
    _io_list_files = None
try:
    from .utils.aidp_io import list_catalogs as _io_list_catalogs
except Exception:
    _io_list_catalogs = None
try:
    from .utils.aidp_io import list_schemas as _io_list_schemas
except Exception:
    _io_list_schemas = None
try:
    from .utils.aidp_io import list_tables as _io_list_tables
except Exception:
    _io_list_tables = None
try:
    from .utils.aidp_io import list_volumes as _io_list_volumes
except Exception:
    _io_list_volumes = None
try:
    from .utils.aidp_io import list_knowledge_bases as _io_list_kbs
except Exception:
    _io_list_kbs = None
try:
    from .utils.aidp_io import describe_table as _io_describe_table
except Exception:
    _io_describe_table = None
try:
    from .utils.aidp_io import list_kb_jobs as _io_list_kb_jobs
except Exception:
    _io_list_kb_jobs = None
try:
    from .utils.aidp_io import trigger_kb_job_run as _io_trigger_kb_job_run
except Exception:
    _io_trigger_kb_job_run = None
try:
    from .utils.aidp_io import resolve_volume_key as _io_resolve_volume_key_public
except Exception:
    _io_resolve_volume_key_public = None

# Internal helpers (best effort — if the underlying module exposes them, we
# delegate; otherwise we use our local copies that mirror them verbatim).
try:
    from .utils.aidp_io import _client as _io_client
except Exception:
    _io_client = None
try:
    from .utils.aidp_io import _build_signer as _io_build_signer
except Exception:
    _io_build_signer = None
try:
    from .utils.aidp_io import _resolve_volume_key as _io_resolve_volume_key_internal
except Exception:
    _io_resolve_volume_key_internal = None
try:
    from .utils.aidp_io import _get as _io_http_get
except Exception:
    _io_http_get = None
try:
    from .utils.aidp_io import _post as _io_http_post
except Exception:
    _io_http_post = None


# --------------------------------------------------------------------------- #
# Shared client helpers — thin wrappers that prefer aidp_io's implementation.
# --------------------------------------------------------------------------- #
def _client(conf, context_vars):
    """Return (base_url, signer, requests, timeout).

    Delegates to aidp_io._client when available so config + signer behavior is
    centralized. Falls back to a local mirror that is byte-for-byte equivalent
    to the version that lives in aidp_io.py today.
    """
    if _io_client is not None:
        return _io_client(conf, context_vars)

    # Local fallback (mirrors aidp_io._client).
    region = get_cfg(conf, "region", "") or os.environ.get("OCI_REGION", "")
    data_lake = (get_cfg(conf, "data_lake_ocid", "")
                 or os.environ.get("DATALAKE_ID", "")
                 or (context_vars or {}).get("datalake_id", ""))
    api_version = get_cfg(conf, "api_version", "20260430") or "20260430"
    service_path = get_cfg(conf, "service_path", "") or "aiDataPlatforms"
    timeout = get_cfg(conf, "timeout", 30)
    if not region or not data_lake:
        raise ValueError("region and data_lake_ocid are required (config or OCI_REGION/DATALAKE_ID env)")
    import requests
    signer = _build_signer(conf)
    base = f"https://aidp.{region}.oci.oraclecloud.com/{api_version}/{service_path}/{data_lake}"
    return base, signer, requests, int(timeout)


def _build_signer(conf):
    """Build the OCI request signer.

    Resolution order:
      1. conf.credential_name  → resolve via aidputils.secrets.get(name) and
         build an oci.signer.Signer(private_key_content=...). This is the
         supported production path for public AIDP data-plane calls per the
         Jun-17 JR/Sambit thread (resource principal 401s those endpoints).
      2. aidp_io._build_signer  (legacy fallback if the shared module exists)
      3. auth_mode-based legacy paths (user_principal / instance_principal /
         resource_principal) — kept for backwards compat.
    """
    # 1. Credential Store (preferred).
    try:
        from .utils.credential_resolver import resolve_oci_signer
        cred_name = get_cfg(conf, "credential_name", "")
        signer, _meta, err = resolve_oci_signer(cred_name)
        if err:
            raise ValueError(f"credential_name='{cred_name}' failed: {err}")
        if signer is not None:
            return signer
    except ImportError:
        pass  # helper not bundled in this build — fall through

    # 2. Legacy aidp_io path.
    if _io_build_signer is not None:
        return _io_build_signer(conf)

    import oci
    mode = (get_cfg(conf, "auth_mode", "resource_principal") or "resource_principal").lower()
    if mode in ("user", "user_principal"):
        tenancy = get_cfg(conf, "tenancy_ocid", "")
        user = get_cfg(conf, "user_ocid", "")
        fingerprint = get_cfg(conf, "fingerprint", "")
        key = get_cfg(conf, "private_key_content", "")
        passphrase = get_cfg(conf, "pass_phrase", "") or None
        missing = [k for k, v in (("tenancy_ocid", tenancy), ("user_ocid", user),
                                  ("fingerprint", fingerprint), ("private_key_content", key)) if not v]
        if missing:
            raise ValueError(f"user_principal auth needs {missing} in config "
                             f"(supply via the Credential Store; never hard-code a private key)")
        return oci.signer.Signer(tenancy=tenancy, user=user, fingerprint=fingerprint,
                                 private_key_file_location=None,
                                 private_key_content=key, pass_phrase=passphrase)
    if mode in ("instance", "instance_principal"):
        return oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    return oci.auth.signers.get_resource_principals_signer()


def _get(requests, signer, url, timeout):
    """GET helper. Delegates to aidp_io._get when available."""
    if _io_http_get is not None:
        return _io_http_get(requests, signer, url, timeout)
    r = requests.get(url, auth=signer, timeout=timeout, headers={"Accept": "application/json"})
    r.raise_for_status()
    return r.json() if r.content else {}


def _post(requests, signer, url, timeout, body=None, headers=None):
    """POST helper. Delegates to aidp_io._post when available."""
    if _io_http_post is not None:
        return _io_http_post(requests, signer, url, timeout, body=body, headers=headers)
    h = {"Accept": "application/json", "Content-Type": "application/json"}
    if headers:
        h.update(headers)
    r = requests.post(url, auth=signer, timeout=timeout,
                      json=(body if body is not None else {}), headers=h)
    r.raise_for_status()
    return r.json() if r.content else {}


def _err(e, **extra):
    """Build a structured error envelope from an exception, surfacing the
    OPC request id and up to 1024 chars of the server response body."""
    detail = str(e)
    resp = getattr(e, "response", None)
    opc_request_id = None
    body_preview = None
    status = None
    if resp is not None:
        try:
            status = getattr(resp, "status_code", None)
        except Exception:
            pass
        try:
            opc_request_id = (resp.headers or {}).get("opc-request-id")
        except Exception:
            pass
        try:
            body_preview = (resp.text or "")[:1024]
            if body_preview:
                detail += f" | {body_preview}"
        except Exception:
            pass
    debug_error("catalog_toolkit error", error=str(e), status=status, opc_request_id=opc_request_id)
    payload = fail(detail, error_type=type(e).__name__, **extra)
    if opc_request_id:
        payload["opc_request_id"] = opc_request_id
    if status is not None:
        payload["status"] = status
    if body_preview:
        payload["response_body"] = body_preview
    return DebugLog.embed(payload)


def _resolve_volume_key(requests, signer, base, timeout, catalog, schema, volume, *,
                        allow_auto_pick=True):
    """Walk catalogs -> schemas -> volumes matching display names or keys.

    When allow_auto_pick is False (destructive ops), refuse to auto-pick a
    single-item catalog/schema — the caller must name them explicitly. The
    volume itself ALWAYS requires an explicit name (no auto-pick), regardless.

    Note: when allow_auto_pick=True and a public resolver is exposed by
    aidp_io, we delegate to it. The allow_auto_pick=False path is
    toolkit-specific (used by VolumeWriteTool) and stays local.
    """
    from urllib.parse import quote
    if not volume:
        return None, "provide volume_key, or catalog + schema + volume names"

    if allow_auto_pick and _io_resolve_volume_key_public is not None:
        try:
            key = _io_resolve_volume_key_public(
                requests, signer, base, timeout, catalog, schema, volume,
            )
            return key, None
        except Exception as e:
            return None, str(e)

    def _match(items, name):
        for it in items:
            if name in (it.get("displayName"), it.get("key")):
                return it
        return None

    catalogs = _get(requests, signer, f"{base}/catalogs", timeout).get("items", [])
    if catalog:
        cat = _match(catalogs, catalog)
    elif allow_auto_pick and len(catalogs) == 1:
        cat = catalogs[0]
    else:
        cat = None
    if not cat:
        if not catalog and not allow_auto_pick:
            return None, "catalog is required for destructive ops (no auto-pick)"
        return None, f"catalog '{catalog}' not found among {[c.get('displayName') for c in catalogs]}"

    schemas = _get(requests, signer, f"{base}/schemas?catalogKey={quote(cat['key'], safe='')}", timeout).get("items", [])
    if schema:
        sch = _match(schemas, schema)
    elif allow_auto_pick and len(schemas) == 1:
        sch = schemas[0]
    else:
        sch = None
    if not sch:
        if not schema and not allow_auto_pick:
            return None, "schema is required for destructive ops (no auto-pick)"
        return None, f"schema '{schema}' not found among {[s.get('displayName') for s in schemas]}"

    volumes = _get(requests, signer,
                   f"{base}/volumes?catalogKey={quote(cat['key'], safe='')}&schemaKey={quote(sch['key'], safe='')}",
                   timeout).get("items", [])
    vol = _match(volumes, volume)
    if not vol:
        return None, f"volume '{volume}' not found among {[v.get('displayName') for v in volumes]}"
    return vol["key"], None


def _resolve_target(rp, conf, requests, signer, base, timeout, *, allow_auto_pick=True):
    """Decide which volume to act on, with explicit, predictable precedence.
    For destructive ops, pass allow_auto_pick=False."""
    rk = (rp.get("volume_key") or "").strip()
    if rk:
        return rk, {"source": "param:volume_key", "volume_key": rk}, None
    rcat, rsch, rvol = rp.get("catalog", ""), rp.get("schema", ""), rp.get("volume", "")
    if rvol:
        vk, err = _resolve_volume_key(requests, signer, base, timeout, rcat, rsch, rvol,
                                      allow_auto_pick=allow_auto_pick)
        return vk, {"source": "param:names", "catalog": rcat, "schema": rsch, "volume": rvol, "volume_key": vk}, err
    ck = (get_cfg(conf, "volume_key", "") or "").strip()
    if ck:
        return ck, {"source": "config:volume_key", "volume_key": ck}, None
    ccat, csch, cvol = get_cfg(conf, "catalog", ""), get_cfg(conf, "schema", ""), get_cfg(conf, "volume", "")
    if cvol:
        vk, err = _resolve_volume_key(requests, signer, base, timeout, ccat, csch, cvol,
                                      allow_auto_pick=allow_auto_pick)
        return vk, {"source": "config:names", "catalog": ccat, "schema": csch, "volume": cvol, "volume_key": vk}, err
    return None, {}, ("provide volume_key, or catalog + schema + volume — in the Test tab "
                      "parameters (they take precedence) or in config")


def _is_truncation_error(e):
    """True iff an aidp_io exception represents a streaming truncation."""
    return bool(getattr(e, "truncated", False))


def _partial_bytes(e):
    """Bytes streamed before truncation, when aidp_io attaches them."""
    return getattr(e, "partial_bytes", b"") or b""


# --------------------------------------------------------------------------- #
# Read / list volume files
# --------------------------------------------------------------------------- #
@CustomToolBase.register
class CatalogFileTool(CustomToolBase):
    """Pull a specific file from a Standard Catalog volume, or list a volume's files.

    Two routing modes:
      * URI mode (new in 1.2.0): set ``source_uri`` to a master: or workspace:
        URI. The tool delegates straight to aidp_io.
      * Legacy mode: provide volume_key + path, or catalog/schema/volume names
        + path. The tool still uses the path-based code below.
    """

    @classmethod
    def _validate_config(cls, conf, runtime_params=None, **context_vars):
        if not (get_cfg(conf, "region", "") or os.environ.get("OCI_REGION", "")):
            raise ValueError("region is required (config or OCI_REGION env)")
        if not (get_cfg(conf, "data_lake_ocid", "") or os.environ.get("DATALAKE_ID", "")):
            raise ValueError("data_lake_ocid is required (config or DATALAKE_ID env)")

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        from urllib.parse import quote
        op = (runtime_params.get("operation", "get") or "get").lower()
        max_bytes = get_cfg(conf, "max_bytes", 5_000_000)
        source_uri = (runtime_params.get("source_uri") or "").strip()
        debug("CatalogFileTool._execute_tool", operation=op, max_bytes=max_bytes,
              source_uri=source_uri)

        # ----- URI mode: delegate everything to aidp_io ----- #
        if source_uri:
            if _io_parse_uri is None or _io_read_file is None or _io_list_files is None:
                return _err(ValueError(
                    "source_uri provided but aidp_io is not available; "
                    "omit source_uri or update the shared aidp_io module"))
            try:
                parsed = _io_parse_uri(source_uri)
            except Exception as e:
                return _err(e)
            resolved = {"source": "param:source_uri", "uri": source_uri, **parsed}

            try:
                if op == "list":
                    listing = _io_list_files(source_uri, conf, context_vars)
                    volume_key = parsed.get("volume_key", "")
                    data = {"volume_key": volume_key,
                            "path": parsed.get("path", "/"),
                            "count": len(listing),
                            "files": listing,
                            "resolved": resolved}
                    return DebugLog.embed(ok(data, **data))

                if op == "get":
                    try:
                        raw = _io_read_file(source_uri, conf, context_vars)
                        truncated = False
                    except ValueError as e:
                        if _is_truncation_error(e):
                            raw = _partial_bytes(e)
                            truncated = True
                        else:
                            raise
                    total = len(raw)
                    debug("CatalogFileTool.get (uri)", uri=source_uri, bytes=total,
                          truncated=truncated)
                    volume_key = parsed.get("volume_key", "")
                    path = parsed.get("path", "")
                    try:
                        data = {"volume_key": volume_key, "path": path,
                                "content": raw.decode("utf-8"),
                                "bytes": total, "truncated": truncated,
                                "resolved": resolved}
                    except UnicodeDecodeError:
                        import base64
                        data = {"volume_key": volume_key, "path": path,
                                "content_base64": base64.b64encode(raw).decode(),
                                "bytes": total, "binary": True,
                                "truncated": truncated, "resolved": resolved}
                    return DebugLog.embed(ok(data, **data))

                return _err(ValueError("operation must be 'get' or 'list'"))
            except Exception as e:
                return _err(e, resolved=resolved)

        # ----- Legacy path-based mode (unchanged behavior) ----- #
        try:
            base, signer, requests, timeout = _client(conf, context_vars)
        except Exception as e:
            return _err(e)

        try:
            volume_key, resolved, err = _resolve_target(runtime_params, conf, requests, signer, base, timeout)
            if err:
                return _err(ValueError(err), resolved=resolved)

            if op == "list":
                path = runtime_params.get("path", "/") or "/"
                # Prefer aidp_io.list_files via a synthesized master: URI when
                # we have a dotted volume_key; falling back to the direct REST
                # call otherwise.
                listing = None
                if _io_list_files is not None and isinstance(volume_key, str) and volume_key.count(".") == 2:
                    try:
                        listing = _io_list_files(f"master:{volume_key}:{path}", conf, context_vars)
                    except Exception:
                        listing = None
                if listing is None:
                    items = _get(requests, signer,
                                 f"{base}/volumes/{quote(volume_key, safe='')}/files?path={quote(path)}", timeout)
                    files = items.get("items", []) if isinstance(items, dict) else []
                    listing = [{"name": f.get("displayName"), "path": f.get("path"), "type": f.get("type")} for f in files]
                data = {"volume_key": volume_key, "path": path, "count": len(listing),
                        "files": listing, "resolved": resolved}
                return DebugLog.embed(ok(data, **data))

            if op == "get":
                path = runtime_params.get("path", "")
                if not path:
                    return _err(ValueError("path is required for get (e.g. /safety_manual.md)"))
                meta = _post(requests, signer,
                             f"{base}/volumes/{quote(volume_key, safe='')}/actions/downloadFileMeta",
                             timeout, body={}, headers={"path": path, "type": "FILE"})
                par = meta.get("parUrl")
                if not par:
                    return _err(ValueError(
                        f"no parUrl for {path}. On a MANAGED volume the file may not be "
                        f"indexed (written outside the AIDP upload workflow)."), resolved=resolved)
                truncated = False
                with requests.get(par, timeout=timeout, stream=True) as blob:
                    blob.raise_for_status()
                    chunks = []
                    total = 0
                    for chunk in blob.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        if total + len(chunk) > max_bytes:
                            remaining = max_bytes - total
                            if remaining > 0:
                                chunks.append(chunk[:remaining])
                                total += remaining
                            truncated = True
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                raw = b"".join(chunks)
                debug("CatalogFileTool.get", path=path, bytes=total, truncated=truncated)
                try:
                    data = {"volume_key": volume_key, "path": path,
                            "content": raw.decode("utf-8"),
                            "bytes": total, "truncated": truncated, "resolved": resolved}
                except UnicodeDecodeError:
                    import base64
                    data = {"volume_key": volume_key, "path": path,
                            "content_base64": base64.b64encode(raw).decode(),
                            "bytes": total, "binary": True, "truncated": truncated,
                            "resolved": resolved}
                return DebugLog.embed(ok(data, **data))

            return _err(ValueError("operation must be 'get' or 'list'"))
        except Exception as e:
            return _err(e)


# --------------------------------------------------------------------------- #
# Write a volume file
# --------------------------------------------------------------------------- #
@CustomToolBase.register
class VolumeWriteTool(CustomToolBase):
    """Write a file into a Standard Catalog volume.

    Two routing modes:
      * URI mode (new in 1.2.0): set ``dest_uri`` to a master: URI. The tool
        delegates straight to aidp_io.write_text / write_file. Workspace writes
        go through WorkspaceFileTool, not here.
      * Legacy mode: provide volume_key + path + content, or names + path +
        content.
    """

    @classmethod
    def _validate_config(cls, conf, runtime_params=None, **context_vars):
        CatalogFileTool._validate_config(conf, runtime_params, **context_vars)

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        from urllib.parse import quote
        dest_uri = (runtime_params.get("dest_uri") or "").strip()
        content = runtime_params.get("content", "")
        debug("VolumeWriteTool._execute_tool", dest_uri=dest_uri,
              content_len=(len(content) if content else 0))

        # ----- URI mode: delegate to aidp_io ----- #
        if dest_uri:
            if _io_parse_uri is None or _io_write_file is None:
                return _err(ValueError(
                    "dest_uri provided but aidp_io is not available; "
                    "omit dest_uri or update the shared aidp_io module"))
            try:
                parsed = _io_parse_uri(dest_uri)
            except Exception as e:
                return _err(e)
            if parsed.get("kind") != "master":
                return _err(ValueError(
                    "dest_uri must be a master: URI for VolumeWriteTool; "
                    "use WorkspaceFileTool for workspace: writes"))
            try:
                payload_bytes = content.encode("utf-8") if isinstance(content, str) else bytes(content)
                result = _io_write_file(dest_uri, payload_bytes, conf, context_vars)
                resolved = {"source": "param:dest_uri", "uri": dest_uri, **parsed}
                payload = {"volume_key": parsed.get("volume_key", ""),
                           "path": result.get("path", parsed.get("path", "")),
                           "written_bytes": result.get("bytes", len(payload_bytes)),
                           "version_id": result.get("version_id", ""),
                           "resolved": resolved}
                return DebugLog.embed(ok(payload, **payload))
            except Exception as e:
                return _err(e)

        # ----- Legacy mode (unchanged behavior) ----- #
        path = runtime_params.get("path", "")
        if not path:
            return _err(ValueError("path is required (the destination file path in the volume)"))
        try:
            base, signer, requests, timeout = _client(conf, context_vars)
        except Exception as e:
            return _err(e)

        try:
            volume_key, resolved, err = _resolve_target(
                runtime_params, conf, requests, signer, base, timeout,
                allow_auto_pick=False)
            if err:
                return _err(ValueError(err), resolved=resolved)

            # Prefer aidp_io.write_file when we have a dotted volume_key.
            if (_io_write_file is not None and isinstance(volume_key, str)
                    and volume_key.count(".") == 2):
                try:
                    payload_bytes = content.encode("utf-8") if isinstance(content, str) else bytes(content)
                    result = _io_write_file(f"master:{volume_key}:{path}",
                                            payload_bytes, conf, context_vars)
                    payload = {"volume_key": volume_key, "path": path,
                               "written_bytes": result.get("bytes", len(payload_bytes)),
                               "version_id": result.get("version_id", ""),
                               "resolved": resolved}
                    return DebugLog.embed(ok(payload, **payload))
                except Exception:
                    # Fall through to the direct REST path on any failure so
                    # callers still get the original error envelope shape.
                    pass

            meta = _post(requests, signer,
                         f"{base}/volumes/{quote(volume_key, safe='')}/actions/uploadFileMeta?isOverwrite=true",
                         timeout, body={"action": "CREATE"}, headers={"path": path})
            par = meta.get("parUrl")
            if not par:
                return _err(ValueError("no parUrl returned from uploadFileMeta"), resolved=resolved)
            data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
            put = requests.put(par, data=data, headers={"Content-Type": "application/octet-stream"}, timeout=timeout)
            put.raise_for_status()
            payload = {"volume_key": volume_key, "path": path, "written_bytes": len(data),
                       "version_id": put.headers.get("version-id", ""), "resolved": resolved}
            return DebugLog.embed(ok(payload, **payload))
        except Exception as e:
            return _err(e)


# --------------------------------------------------------------------------- #
# Catalog metadata browser
# --------------------------------------------------------------------------- #
@CustomToolBase.register
class CatalogBrowserTool(CustomToolBase):
    """Browse catalog metadata so an agent can discover what data exists.

    Every list operation prefers the aidp_io.list_* helper when present and
    falls back to a direct REST call otherwise so we don't break against the
    current aidp_io shipping in master.
    """

    @classmethod
    def _validate_config(cls, conf, runtime_params=None, **context_vars):
        CatalogFileTool._validate_config(conf, runtime_params, **context_vars)

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        from urllib.parse import quote
        what = (runtime_params.get("list", "catalogs") or "catalogs").lower()
        catalog_key = runtime_params.get("catalog_key", "")
        schema_key = runtime_params.get("schema_key", "")
        table_key = runtime_params.get("table_key", "")
        debug("CatalogBrowserTool._execute_tool", list=what,
              catalog_key=catalog_key, schema_key=schema_key, table_key=table_key)
        try:
            base, signer, requests, timeout = _client(conf, context_vars)
        except Exception as e:
            return _err(e)

        max_rows = get_cfg(conf, "max_rows", 1000)

        def _normalize(rows, *fields):
            """Apply the max_rows truncation + project (key, name, +fields)."""
            truncated = False
            if len(rows) > max_rows:
                rows = rows[:max_rows]
                truncated = True
            out = []
            for it in rows:
                row = {"key": it.get("key"), "name": it.get("displayName") or it.get("name")}
                for f in fields:
                    if f in it:
                        row[f] = it[f]
                out.append(row)
            return out, truncated

        try:
            if what == "catalogs":
                if _io_list_catalogs is not None:
                    items = _io_list_catalogs(conf, context_vars)
                else:
                    items = _get(requests, signer, f"{base}/catalogs", timeout).get("items", [])
                rows, truncated = _normalize(items, "catalogType")
                payload = {"count": len(rows), "catalogs": rows, "truncated": truncated}
                return DebugLog.embed(ok(payload, **payload))

            if what == "schemas":
                if not catalog_key:
                    return _err(ValueError("catalog_key is required to list schemas"))
                if _io_list_schemas is not None:
                    items = _io_list_schemas(catalog_key, conf, context_vars)
                else:
                    items = _get(requests, signer,
                                 f"{base}/schemas?catalogKey={quote(catalog_key, safe='')}",
                                 timeout).get("items", [])
                rows, truncated = _normalize(items)
                payload = {"count": len(rows), "schemas": rows, "truncated": truncated}
                return DebugLog.embed(ok(payload, **payload))

            if what == "tables":
                if not (catalog_key and schema_key):
                    return _err(ValueError("catalog_key and schema_key are required to list tables"))
                if _io_list_tables is not None:
                    items = _io_list_tables(catalog_key, schema_key, conf, context_vars)
                else:
                    items = _get(requests, signer,
                                 f"{base}/tables?catalogKey={quote(catalog_key, safe='')}"
                                 f"&schemaKey={quote(schema_key, safe='')}",
                                 timeout).get("items", [])
                rows, truncated = _normalize(items)
                payload = {"count": len(rows), "tables": rows, "truncated": truncated}
                return DebugLog.embed(ok(payload, **payload))

            if what == "volumes":
                if not (catalog_key and schema_key):
                    return _err(ValueError("catalog_key and schema_key are required to list volumes"))
                if _io_list_volumes is not None:
                    items = _io_list_volumes(catalog_key, schema_key, conf, context_vars)
                else:
                    items = _get(requests, signer,
                                 f"{base}/volumes?catalogKey={quote(catalog_key, safe='')}"
                                 f"&schemaKey={quote(schema_key, safe='')}",
                                 timeout).get("items", [])
                rows, truncated = _normalize(items, "volumeType")
                payload = {"count": len(rows), "volumes": rows, "truncated": truncated}
                return DebugLog.embed(ok(payload, **payload))

            if what in ("knowledgebases", "kbs"):
                if not (catalog_key and schema_key):
                    return _err(ValueError("catalog_key and schema_key are required to list knowledge bases"))
                if _io_list_kbs is not None:
                    items = _io_list_kbs(catalog_key, schema_key, conf, context_vars)
                else:
                    items = _get(requests, signer,
                                 f"{base}/knowledgeBases?catalogKey={quote(catalog_key, safe='')}"
                                 f"&schemaKey={quote(schema_key, safe='')}&limit=1000",
                                 timeout).get("items", [])
                rows, truncated = _normalize(items, "lifecycleState")
                payload = {"count": len(rows), "knowledge_bases": rows, "truncated": truncated}
                return DebugLog.embed(ok(payload, **payload))

            if what == "table":
                if not table_key:
                    return _err(ValueError("table_key is required to describe a table"))
                if _io_describe_table is not None:
                    detail = _io_describe_table(table_key, conf, context_vars)
                else:
                    detail = _get(requests, signer,
                                  f"{base}/tables/{quote(table_key, safe='')}", timeout)
                cols = detail.get("columns", detail.get("schema", []))
                payload = {"table_key": table_key, "name": detail.get("displayName") or detail.get("name"),
                           "columns": cols if cols else detail}
                return DebugLog.embed(ok(payload, **payload))

            return _err(ValueError("list must be one of: catalogs, schemas, tables, volumes, knowledgeBases, table"))
        except Exception as e:
            return _err(e)


# --------------------------------------------------------------------------- #
# KB ingestion
# --------------------------------------------------------------------------- #
@CustomToolBase.register
class KBIngestTool(CustomToolBase):
    """List a knowledge base's ingestion jobs, or trigger an ingestion run.

    Prefers aidp_io.list_kb_jobs / aidp_io.trigger_kb_job_run when available;
    falls back to a direct REST call otherwise.
    """

    @classmethod
    def _validate_config(cls, conf, runtime_params=None, **context_vars):
        CatalogFileTool._validate_config(conf, runtime_params, **context_vars)

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        from urllib.parse import quote
        op = (runtime_params.get("operation", "list_jobs") or "list_jobs").lower()
        kb_key = runtime_params.get("kb_key", "") or get_cfg(conf, "kb_key", "")
        job_key = runtime_params.get("job_key", "")
        run_config = runtime_params.get("run_config")
        debug("KBIngestTool._execute_tool", operation=op, kb_key=kb_key, job_key=job_key,
              has_run_config=bool(run_config))
        if not kb_key:
            return _err(ValueError("kb_key is required (the knowledge base key, e.g. catalog.schema.kbName)"))
        try:
            base, signer, requests, timeout = _client(conf, context_vars)
        except Exception as e:
            return _err(e)

        max_rows = get_cfg(conf, "max_rows", 1000)

        try:
            if op == "list_jobs":
                if _io_list_kb_jobs is not None:
                    items = _io_list_kb_jobs(kb_key, conf, context_vars)
                else:
                    items = _get(requests, signer,
                                 f"{base}/knowledgeBases/{quote(kb_key, safe='')}/jobs", timeout).get("items", [])
                truncated = False
                if len(items) > max_rows:
                    items = items[:max_rows]
                    truncated = True
                jobs = [{"key": j.get("key"), "name": j.get("displayName") or j.get("name"),
                         "state": j.get("lifecycleState") or j.get("state")} for j in items]
                payload = {"kb_key": kb_key, "count": len(jobs), "jobs": jobs, "truncated": truncated}
                return DebugLog.embed(ok(payload, **payload))

            if op == "trigger":
                if not job_key:
                    return _err(ValueError("job_key is required to trigger a run (use operation=list_jobs to find it)"))
                body = {}
                if run_config is not None:
                    if isinstance(run_config, str):
                        try:
                            body = json.loads(run_config) if run_config.strip() else {}
                        except Exception:
                            return _err(ValueError("run_config must be a JSON object (or omitted)"))
                    elif isinstance(run_config, dict):
                        body = run_config
                    else:
                        return _err(ValueError("run_config must be a JSON object (or omitted)"))
                if _io_trigger_kb_job_run is not None:
                    run = _io_trigger_kb_job_run(kb_key, job_key, body, conf, context_vars)
                else:
                    run = _post(requests, signer,
                                f"{base}/knowledgeBases/{quote(kb_key, safe='')}/jobs/{quote(job_key, safe='')}/runs",
                                timeout, body=body)
                run_key = ""
                if isinstance(run, dict):
                    run_key = (run.get("key") or run.get("runKey") or run.get("id") or "")
                payload = {"kb_key": kb_key, "job_key": job_key, "triggered": True,
                           "run_key": run_key, "run": run}
                return DebugLog.embed(ok(payload, **payload))
            return _err(ValueError("operation must be 'list_jobs' or 'trigger'"))
        except Exception as e:
            return _err(e)


# --------------------------------------------------------------------------- #
# Workspace files (Jupyter Contents API)
# --------------------------------------------------------------------------- #
@CustomToolBase.register
class WorkspaceFileTool(CustomToolBase):
    """Read or list files in the workspace (not a catalog volume).

    Two routing modes:
      * URI mode (new in 1.2.0): set ``source_uri`` / ``dest_uri`` to a
        workspace: URI. The tool delegates straight to aidp_io.
      * Legacy mode: provide ``path`` and rely on the configured workspace_id.
    """

    @classmethod
    def _validate_config(cls, conf, runtime_params=None, **context_vars):
        CatalogFileTool._validate_config(conf, runtime_params, **context_vars)
        if not (get_cfg(conf, "workspace_id", "") or os.environ.get("WORKSPACE_ID", "")):
            raise ValueError("workspace_id is required in tool config")

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        from urllib.parse import quote
        op = (runtime_params.get("operation", "get") or "get").lower()
        ws_id = get_cfg(conf, "workspace_id", "") or os.environ.get("WORKSPACE_ID", "")
        max_bytes = get_cfg(conf, "max_bytes", 5_000_000)
        source_uri = (runtime_params.get("source_uri") or "").strip()
        dest_uri = (runtime_params.get("dest_uri") or "").strip()
        debug("WorkspaceFileTool._execute_tool", operation=op, workspace_id=bool(ws_id),
              max_bytes=max_bytes, source_uri=source_uri, dest_uri=dest_uri)
        if not ws_id:
            return _err(ValueError("workspace_id is required in config"))

        # ----- URI mode: delegate to aidp_io (workspace: only) ----- #
        chosen_uri = source_uri or dest_uri
        if chosen_uri:
            if _io_parse_uri is None:
                return _err(ValueError(
                    "source_uri/dest_uri provided but aidp_io is not available; "
                    "omit URI fields or update the shared aidp_io module"))
            try:
                parsed = _io_parse_uri(chosen_uri)
            except Exception as e:
                return _err(e)
            if parsed.get("kind") != "workspace":
                return _err(ValueError(
                    "WorkspaceFileTool only accepts workspace: URIs; "
                    "use CatalogFileTool / VolumeWriteTool for master: URIs"))
            try:
                if op == "list":
                    if _io_list_files is None:
                        return _err(ValueError("aidp_io.list_files is unavailable"))
                    listing = _io_list_files(chosen_uri, conf, context_vars)
                    payload = {"path": parsed.get("path", "/"), "count": len(listing),
                               "files": listing, "truncated": False}
                    return DebugLog.embed(ok(payload, **payload))

                if op == "get":
                    if _io_read_file is None:
                        return _err(ValueError("aidp_io.read_file is unavailable"))
                    try:
                        raw = _io_read_file(chosen_uri, conf, context_vars)
                        truncated = False
                    except ValueError as e:
                        if _is_truncation_error(e):
                            raw = _partial_bytes(e)
                            truncated = True
                        else:
                            raise
                    try:
                        content = raw.decode("utf-8")
                        binary = False
                    except UnicodeDecodeError:
                        import base64
                        content = base64.b64encode(raw).decode()
                        binary = True
                    payload = {"path": parsed.get("path", ""),
                               "name": (parsed.get("path", "").rsplit("/", 1) or [""])[-1],
                               "format": "base64" if binary else "text",
                               "content": content,
                               "bytes": len(raw),
                               "truncated": truncated}
                    return DebugLog.embed(ok(payload, **payload))

                return _err(ValueError("operation must be 'get' or 'list'"))
            except Exception as e:
                return _err(e)

        # ----- Legacy path-based mode (unchanged behavior) ----- #
        try:
            base, signer, requests, timeout = _client(conf, context_vars)
        except Exception as e:
            return _err(e)
        ws_base = f"{base}/workspaces/{quote(ws_id, safe='')}"

        try:
            if op == "list":
                path = runtime_params.get("path", "/") or "/"
                if not path.startswith("/"):
                    path = "/" + path
                items = _get(requests, signer, f"{ws_base}/objects?path={quote(path)}", timeout).get("items", [])
                truncated = False
                max_rows = get_cfg(conf, "max_rows", 1000)
                if len(items) > max_rows:
                    items = items[:max_rows]
                    truncated = True
                listing = [{"name": it.get("displayName"), "path": it.get("path"), "type": it.get("type")} for it in items]
                payload = {"path": path, "count": len(listing), "files": listing, "truncated": truncated}
                return DebugLog.embed(ok(payload, **payload))

            if op == "get":
                path = runtime_params.get("path", "")
                if not path:
                    return _err(ValueError("path is required for get, e.g. Notebooks/foo.ipynb"))
                rel = path.lstrip("/")
                enc = quote(f"Workspace/{rel}", safe="")
                url = f"{ws_base}/notebook/api/contents/{enc}?type=file&content=1&format=text"
                data = _get(requests, signer, url, timeout)
                content = data.get("content", "")
                truncated = False
                if isinstance(content, str) and len(content.encode("utf-8", errors="ignore")) > max_bytes:
                    content = content[:max_bytes]
                    truncated = True
                bytes_len = len(content.encode("utf-8", errors="ignore")) if isinstance(content, str) else 0
                payload = {"path": path, "name": data.get("name"), "format": data.get("format"),
                           "content": content, "bytes": bytes_len, "truncated": truncated}
                return DebugLog.embed(ok(payload, **payload))

            return _err(ValueError("operation must be 'get' or 'list'"))
        except Exception as e:
            return _err(e)
