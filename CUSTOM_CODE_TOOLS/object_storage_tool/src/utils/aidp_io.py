"""
aidp_io — Shared AIDP file-IO module for custom tools.

This module centralizes the file read/write/list logic that every custom tool
needs when working against AIDP resources (Volumes in master catalogs, and
files in the Workspace). It exposes a uniform URI contract so tool authors
never have to think about volume_key resolution, PAR URLs, or Jupyter
Contents API quirks.

URI FORMAT
----------
    master:<catalogName>.<schemaName>.<volumeName>:/<path>
        e.g. master:construction_catalog.construction_schema.construction_documents:/Plans.pdf

    workspace:/<path>
        e.g. workspace:/Notebooks/foo.ipynb

    Optional alias (sniffed by the parser when no scheme prefix is present):
        <catalogName>.<schemaName>.<volumeName>:/<path>   (treated as master:)

PUBLIC API
----------
    parse_uri(uri)                 -> dict
    read_file(uri, conf, ctx)      -> bytes
    write_file(uri, content, ...)  -> dict
    list_files(uri, conf, ctx)     -> list[dict]
    read_text(uri, ..., encoding)  -> str
    write_text(uri, ..., encoding) -> dict

    # Catalog-toolkit delegation helpers
    resolve_volume_key(conf, ctx, catalog, schema, volume, *,
                       allow_auto_pick=True) -> str | None
    list_catalogs(conf, ctx)                         -> list[dict]
    list_schemas(conf, ctx, catalog_key)             -> list[dict]
    list_tables(conf, ctx, catalog_key, schema_key)  -> list[dict]
    list_volumes(conf, ctx, catalog_key, schema_key) -> list[dict]
    list_knowledge_bases(conf, ctx, catalog_key, schema_key) -> list[dict]
    describe_table(conf, ctx, table_key)             -> dict
    list_kb_jobs(conf, ctx, kb_key)                  -> list[dict]
    trigger_kb_job_run(conf, ctx, kb_key, job_key, run_config=None) -> dict

CONFIG KEYS (conf dict, mirrors aidp_catalog_toolkit)
-----------------------------------------------------
    region              OCI region short code (also OCI_REGION env)
    data_lake_ocid      data lake OCID (also DATALAKE_ID env / ctx["datalake_id"])
    api_version         default "20260430"
    service_path        default "aiDataPlatforms"
    timeout             HTTP timeout seconds, default 30
    auth_mode           "resource_principal" (default) | "user_principal" | "instance_principal"
    tenancy_ocid        (user_principal only)
    user_ocid           (user_principal only)
    fingerprint         (user_principal only)
    private_key_content (user_principal only)
    pass_phrase         (user_principal only, optional)
    workspace_id        required for workspace: URIs
    max_bytes           read_file streaming guard, default 50 MiB

ERROR CONTRACT
--------------
All errors raise Python exceptions. Tool wrappers should catch and translate
to the standard {"ok": false, "error": ...} envelope. A streaming-truncation
error includes a ``truncated`` attribute set to True on the raised ValueError.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Config + signer helpers (mirrors aidp_catalog_toolkit verbatim)
# ---------------------------------------------------------------------------

DEFAULT_MAX_BYTES = 50 * 1024 * 1024  # 50 MiB


def _get_cfg(conf: Optional[Dict[str, Any]], key: str, default: Any = "") -> Any:
    """Read a config value tolerantly: conf may be None, missing, or have falsy values."""
    if not conf:
        return default
    val = conf.get(key, default)
    if val is None or val == "":
        return default
    return val


def _build_signer(conf: Dict[str, Any]):
    """Build the OCI request signer for the configured auth_mode.

    Mirrors aidp_catalog_toolkit._build_signer. Supported modes:
      - resource_principal (default)
      - user_principal
      - instance_principal
    """
    import oci  # lazy
    mode = (_get_cfg(conf, "auth_mode", "resource_principal") or "resource_principal").lower()
    if mode in ("user", "user_principal"):
        tenancy = _get_cfg(conf, "tenancy_ocid", "")
        user = _get_cfg(conf, "user_ocid", "")
        fingerprint = _get_cfg(conf, "fingerprint", "")
        key = _get_cfg(conf, "private_key_content", "")
        passphrase = _get_cfg(conf, "pass_phrase", "") or None
        missing = [
            k for k, v in (
                ("tenancy_ocid", tenancy),
                ("user_ocid", user),
                ("fingerprint", fingerprint),
                ("private_key_content", key),
            ) if not v
        ]
        if missing:
            raise ValueError(f"user_principal auth needs {missing} in config")
        return oci.signer.Signer(
            tenancy=tenancy, user=user, fingerprint=fingerprint,
            private_key_file_location=None,
            private_key_content=key, pass_phrase=passphrase,
        )
    if mode in ("instance", "instance_principal"):
        return oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    return oci.auth.signers.get_resource_principals_signer()


def _client(conf: Optional[Dict[str, Any]], context_vars: Optional[Dict[str, Any]]):
    """Return (base_url, signer, requests, timeout) for the AIDP REST API.

    Raises ValueError if region or data_lake_ocid are not resolvable.
    """
    conf = conf or {}
    context_vars = context_vars or {}
    region = _get_cfg(conf, "region", "") or os.environ.get("OCI_REGION", "")
    data_lake = (
        _get_cfg(conf, "data_lake_ocid", "")
        or os.environ.get("DATALAKE_ID", "")
        or context_vars.get("datalake_id", "")
    )
    api_version = _get_cfg(conf, "api_version", "20260430") or "20260430"
    service_path = _get_cfg(conf, "service_path", "aiDataPlatforms") or "aiDataPlatforms"
    timeout = _get_cfg(conf, "timeout", 30) or 30
    if not region or not data_lake:
        raise ValueError(
            "region and data_lake_ocid are required (config or OCI_REGION/DATALAKE_ID env)"
        )
    import requests  # lazy
    signer = _build_signer(conf)
    base = f"https://aidp.{region}.oci.oraclecloud.com/{api_version}/{service_path}/{data_lake}"
    return base, signer, requests, int(timeout)


def _workspace_base(conf: Dict[str, Any], context_vars: Dict[str, Any], base: str) -> str:
    """Resolve the workspace base URL for the Jupyter Contents API."""
    ws_id = (
        _get_cfg(conf, "workspace_id", "")
        or os.environ.get("WORKSPACE_ID", "")
        or (context_vars or {}).get("workspace_id", "")
    )
    if not ws_id:
        raise ValueError(
            "workspace_id is required for workspace: URIs "
            "(set conf['workspace_id'] or WORKSPACE_ID env or context_vars['workspace_id'])"
        )
    from urllib.parse import quote
    return f"{base}/workspaces/{quote(str(ws_id), safe='')}"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get(requests_mod, signer, url: str, timeout: int) -> Dict[str, Any]:
    r = requests_mod.get(url, auth=signer, timeout=timeout, headers={"Accept": "application/json"})
    r.raise_for_status()
    return r.json() if r.content else {}


def _post(requests_mod, signer, url: str, timeout: int,
          body: Optional[Dict[str, Any]] = None,
          headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    h = {"Accept": "application/json", "Content-Type": "application/json"}
    if headers:
        h.update(headers)
    r = requests_mod.post(
        url, auth=signer, timeout=timeout,
        json=(body if body is not None else {}), headers=h,
    )
    r.raise_for_status()
    return r.json() if r.content else {}


# ---------------------------------------------------------------------------
# URI parsing
# ---------------------------------------------------------------------------

def parse_uri(uri: str) -> Dict[str, Any]:
    """Parse an AIDP file URI into a normalized dict.

    Accepted forms:
        master:<cat>.<sch>.<vol>:/<path>
        workspace:/<path>
        <cat>.<sch>.<vol>:/<path>     (alias for master:)

    Returns:
        For master:  {"kind": "master", "volume_key": "<cat>.<sch>.<vol>",
                      "catalog": "<cat>", "schema": "<sch>", "volume": "<vol>",
                      "path": "/..."}
        For workspace: {"kind": "workspace", "path": "/..."}

    Raises:
        ValueError on a malformed URI.
    """
    if not isinstance(uri, str) or not uri.strip():
        raise ValueError("uri must be a non-empty string (expected master:... or workspace:...)")
    s = uri.strip()

    # workspace:
    if s.startswith("workspace:"):
        rest = s[len("workspace:"):]
        if not rest.startswith("/"):
            raise ValueError(
                f"malformed workspace URI {uri!r}: path must start with '/' "
                "(expected workspace:/<path>)"
            )
        return {"kind": "workspace", "path": rest}

    # master:
    if s.startswith("master:"):
        rest = s[len("master:"):]
    else:
        # alias: <cat>.<sch>.<vol>:/<path>  -- only when the first ':' is
        # followed by '/' AND the prefix before ':' parses as cat.sch.vol.
        first_colon = s.find(":")
        if first_colon <= 0 or first_colon + 1 >= len(s) or s[first_colon + 1] != "/":
            raise ValueError(
                f"malformed URI {uri!r}: expected master:<cat>.<sch>.<vol>:/<path> "
                "or workspace:/<path>"
            )
        rest = s

    # rest must look like "<cat>.<sch>.<vol>:/<path>"
    sep = rest.find(":/")
    if sep < 0:
        raise ValueError(
            f"malformed master URI {uri!r}: expected master:<cat>.<sch>.<vol>:/<path>"
        )
    triple = rest[:sep]
    path = rest[sep + 1:]  # keep the leading '/'

    parts = triple.split(".")
    if len(parts) != 3 or not all(p.strip() for p in parts):
        raise ValueError(
            f"malformed master URI {uri!r}: volume_key must be "
            "<catalogName>.<schemaName>.<volumeName>"
        )
    if not path.startswith("/"):
        raise ValueError(
            f"malformed master URI {uri!r}: path must start with '/'"
        )
    cat, sch, vol = (p.strip() for p in parts)
    return {
        "kind": "master",
        "volume_key": f"{cat}.{sch}.{vol}",
        "catalog": cat,
        "schema": sch,
        "volume": vol,
        "path": path,
    }


# ---------------------------------------------------------------------------
# Volume key resolution
# ---------------------------------------------------------------------------

def _resolve_volume_key(requests_mod, signer, base: str, timeout: int,
                        catalog: str, schema: str, volume: str) -> str:
    """Resolve a <cat>.<sch>.<vol> triple to the actual server-side volume key.

    Walks catalogs -> schemas -> volumes matching displayName or key field.
    Raises ValueError on a miss.
    """
    from urllib.parse import quote

    def _match(items, name):
        for it in items:
            if name in (it.get("displayName"), it.get("key")):
                return it
        return None

    catalogs = _get(requests_mod, signer, f"{base}/catalogs", timeout).get("items", [])
    cat = _match(catalogs, catalog)
    if not cat:
        raise ValueError(
            f"catalog {catalog!r} not found among "
            f"{[c.get('displayName') for c in catalogs]}"
        )
    schemas = _get(
        requests_mod, signer,
        f"{base}/schemas?catalogKey={quote(cat['key'], safe='')}", timeout,
    ).get("items", [])
    sch = _match(schemas, schema)
    if not sch:
        raise ValueError(
            f"schema {schema!r} not found among "
            f"{[s.get('displayName') for s in schemas]}"
        )
    volumes = _get(
        requests_mod, signer,
        f"{base}/volumes?catalogKey={quote(cat['key'], safe='')}"
        f"&schemaKey={quote(sch['key'], safe='')}", timeout,
    ).get("items", [])
    vol = _match(volumes, volume)
    if not vol:
        raise ValueError(
            f"volume {volume!r} not found among "
            f"{[v.get('displayName') for v in volumes]}"
        )
    return vol["key"]


# ---------------------------------------------------------------------------
# read_file / write_file / list_files
# ---------------------------------------------------------------------------

def read_file(uri: str, conf: Optional[Dict[str, Any]],
              context_vars: Optional[Dict[str, Any]]) -> bytes:
    """Read bytes from an AIDP target.

    URI dispatch:
        master:<cat>.<sch>.<vol>:/<path>  -> POST downloadFileMeta + GET parUrl (streamed)
        workspace:/<path>                  -> GET /notebook/api/contents/<enc>

    Raises:
        ValueError on a streaming-truncation event (with ``truncated=True`` attr),
        or any HTTP / OCI exception bubbled up from the underlying call.
    """
    from urllib.parse import quote

    parsed = parse_uri(uri)
    base, signer, requests_mod, timeout = _client(conf, context_vars)
    max_bytes = int(_get_cfg(conf, "max_bytes", DEFAULT_MAX_BYTES) or DEFAULT_MAX_BYTES)

    if parsed["kind"] == "workspace":
        ws_base = _workspace_base(conf or {}, context_vars or {}, base)
        rel = parsed["path"].lstrip("/")
        enc = quote(f"Workspace/{rel}", safe="")
        url = f"{ws_base}/notebook/api/contents/{enc}?type=file&content=1&format=base64"
        data = _get(requests_mod, signer, url, timeout)
        content = data.get("content", "") or ""
        fmt = (data.get("format") or "").lower()
        if fmt == "base64":
            import base64
            raw = base64.b64decode(content)
        else:
            raw = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        if len(raw) > max_bytes:
            err = ValueError(
                f"workspace file {parsed['path']!r} exceeds max_bytes={max_bytes}"
            )
            setattr(err, "truncated", True)
            raise err
        return raw

    # master:
    volume_key = parsed["volume_key"]
    if not volume_key:
        raise ValueError("master URI is missing a volume_key (<cat>.<sch>.<vol>)")

    # Try the literal triple as the server key first; fall back to resolve.
    try:
        meta = _post(
            requests_mod, signer,
            f"{base}/volumes/{quote(volume_key, safe='')}/actions/downloadFileMeta",
            timeout, body={}, headers={"path": parsed["path"], "type": "FILE"},
        )
    except Exception:
        resolved_key = _resolve_volume_key(
            requests_mod, signer, base, timeout,
            parsed["catalog"], parsed["schema"], parsed["volume"],
        )
        meta = _post(
            requests_mod, signer,
            f"{base}/volumes/{quote(resolved_key, safe='')}/actions/downloadFileMeta",
            timeout, body={}, headers={"path": parsed["path"], "type": "FILE"},
        )

    par = meta.get("parUrl")
    if not par:
        raise ValueError(f"no parUrl returned for {parsed['path']!r}")

    chunks: List[bytes] = []
    total = 0
    truncated = False
    with requests_mod.get(par, timeout=timeout, stream=True) as blob:
        blob.raise_for_status()
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

    if truncated:
        err = ValueError(
            f"file {parsed['path']!r} exceeds max_bytes={max_bytes} "
            f"(read {total} bytes before stopping)"
        )
        setattr(err, "truncated", True)
        setattr(err, "partial_bytes", b"".join(chunks))
        raise err

    return b"".join(chunks)


def write_file(uri: str, content: bytes,
               conf: Optional[Dict[str, Any]],
               context_vars: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Write bytes to an AIDP target.

    URI dispatch:
        master:<cat>.<sch>.<vol>:/<path>  -> POST uploadFileMeta + PUT parUrl
        workspace:/<path>                  -> PUT /notebook/api/contents/<enc>

    Returns:
        {"path": "<server path>", "bytes": <int>, "version_id": "<opt>"}

    Raises:
        ValueError if the master volume_key is empty,
        or any HTTP / OCI exception bubbled up from the underlying call.
    """
    from urllib.parse import quote

    parsed = parse_uri(uri)
    base, signer, requests_mod, timeout = _client(conf, context_vars)

    data = content if isinstance(content, (bytes, bytearray)) else bytes(content)
    data = bytes(data)

    if parsed["kind"] == "workspace":
        ws_base = _workspace_base(conf or {}, context_vars or {}, base)
        rel = parsed["path"].lstrip("/")
        enc = quote(f"Workspace/{rel}", safe="")
        url = f"{ws_base}/notebook/api/contents/{enc}"
        import base64 as _b64
        body = {
            "type": "file",
            "format": "base64",
            "content": _b64.b64encode(data).decode("ascii"),
        }
        r = requests_mod.put(
            url, auth=signer, timeout=timeout,
            json=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        r.raise_for_status()
        resp = r.json() if r.content else {}
        return {
            "path": parsed["path"],
            "bytes": len(data),
            "version_id": resp.get("last_modified") or resp.get("etag") or "",
        }

    # master:
    volume_key = parsed["volume_key"]
    if not volume_key:
        raise ValueError("master URI is missing a volume_key (<cat>.<sch>.<vol>)")

    def _upload_with(key: str) -> Dict[str, Any]:
        return _post(
            requests_mod, signer,
            f"{base}/volumes/{quote(key, safe='')}/actions/uploadFileMeta?isOverwrite=true",
            timeout, body={"action": "CREATE"}, headers={"path": parsed["path"]},
        )

    try:
        meta = _upload_with(volume_key)
    except Exception:
        resolved_key = _resolve_volume_key(
            requests_mod, signer, base, timeout,
            parsed["catalog"], parsed["schema"], parsed["volume"],
        )
        meta = _upload_with(resolved_key)

    par = meta.get("parUrl")
    if not par:
        raise ValueError(f"no parUrl returned from uploadFileMeta for {parsed['path']!r}")

    put = requests_mod.put(
        par, data=data,
        headers={"Content-Type": "application/octet-stream"},
        timeout=timeout,
    )
    put.raise_for_status()

    version_id = ""
    try:
        version_id = (put.headers or {}).get("x-content-sha256") or (put.headers or {}).get("etag") or ""
    except Exception:
        pass

    return {
        "path": parsed["path"],
        "bytes": len(data),
        "version_id": version_id,
    }


def list_files(uri: str, conf: Optional[Dict[str, Any]],
               context_vars: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """List files (and folders) at a folder URI.

    URI dispatch:
        master:<cat>.<sch>.<vol>:/<folder>  -> GET /volumes/<key>/files?path=...
        workspace:/<folder>                  -> GET /workspaces/<ws>/objects?path=...

    Returns:
        list of {"name": str, "path": str, "type": "file"|"directory"}.
    """
    from urllib.parse import quote

    parsed = parse_uri(uri)
    base, signer, requests_mod, timeout = _client(conf, context_vars)

    if parsed["kind"] == "workspace":
        ws_base = _workspace_base(conf or {}, context_vars or {}, base)
        url = f"{ws_base}/objects?path={quote(parsed['path'], safe='')}"
        items = _get(requests_mod, signer, url, timeout).get("items", [])
        out: List[Dict[str, Any]] = []
        for it in items:
            name = it.get("name") or it.get("displayName") or ""
            t = (it.get("type") or it.get("objectType") or "").lower()
            kind = "directory" if t in ("directory", "folder") else "file"
            ipath = it.get("path") or f"{parsed['path'].rstrip('/')}/{name}"
            out.append({"name": name, "path": ipath, "type": kind})
        return out

    # master:
    volume_key = parsed["volume_key"]
    if not volume_key:
        raise ValueError("master URI is missing a volume_key (<cat>.<sch>.<vol>)")

    def _list_with(key: str) -> List[Dict[str, Any]]:
        url = (
            f"{base}/volumes/{quote(key, safe='')}/files"
            f"?path={quote(parsed['path'], safe='')}"
        )
        return _get(requests_mod, signer, url, timeout).get("items", [])

    try:
        items = _list_with(volume_key)
    except Exception:
        resolved_key = _resolve_volume_key(
            requests_mod, signer, base, timeout,
            parsed["catalog"], parsed["schema"], parsed["volume"],
        )
        items = _list_with(resolved_key)

    out = []
    for it in items:
        name = it.get("name") or it.get("displayName") or ""
        t = (it.get("type") or it.get("fileType") or "FILE").upper()
        kind = "directory" if t in ("DIRECTORY", "FOLDER", "DIR") else "file"
        ipath = it.get("path") or f"{parsed['path'].rstrip('/')}/{name}"
        out.append({"name": name, "path": ipath, "type": kind})
    return out


# ---------------------------------------------------------------------------
# Text convenience wrappers
# ---------------------------------------------------------------------------

def read_text(uri: str, conf: Optional[Dict[str, Any]],
              context_vars: Optional[Dict[str, Any]],
              encoding: str = "utf-8") -> str:
    """Read a file and decode as text. See read_file for URI / error semantics."""
    return read_file(uri, conf, context_vars).decode(encoding)


def write_text(uri: str, content_str: str,
               conf: Optional[Dict[str, Any]],
               context_vars: Optional[Dict[str, Any]],
               encoding: str = "utf-8") -> Dict[str, Any]:
    """Encode text and write it. See write_file for URI / return semantics."""
    if not isinstance(content_str, str):
        raise ValueError("content_str must be a string for write_text")
    return write_file(uri, content_str.encode(encoding), conf, context_vars)


# ---------------------------------------------------------------------------
# Catalog-toolkit delegation helpers
# ---------------------------------------------------------------------------
#
# These public helpers let the aidp_catalog_toolkit (and any other consumer)
# fully delegate AIDP catalog browsing, volume resolution, and knowledge-base
# job triggering to this shared module. They all use the same internal
# _client / _build_signer / _get / _post helpers, so auth_mode dispatch and
# URL conventions (/{api_version}/{service_path}/{lake}/...) are consistent.
#
# Error contract: same as the rest of aidp_io — raise Python exceptions. Tool
# wrappers should catch and translate to the standard {"ok": false, "error":
# ...} envelope.


def resolve_volume_key(conf: Optional[Dict[str, Any]],
                       ctx: Optional[Dict[str, Any]],
                       catalog: Optional[str] = None,
                       schema: Optional[str] = None,
                       volume: Optional[str] = None,
                       *,
                       allow_auto_pick: bool = True) -> Optional[str]:
    """Resolve catalog/schema/volume names (or keys) to a dot-delimited key.

    Walks /catalogs -> /schemas -> /volumes matching ``displayName`` or
    ``key``, then returns ``"<catalogName>.<schemaName>.<volumeName>"`` (the
    AIDP dot-form). Returns ``None`` if resolution fails.

    When ``allow_auto_pick`` is False (destructive ops), refuses to auto-pick
    a single-item catalog or schema — the caller must name them explicitly.
    The volume itself ALWAYS requires an explicit name (no auto-pick),
    regardless of the flag.

    Raises:
        ValueError if catalog / schema / volume cannot be resolved or if a
        required name is missing under non-auto-pick semantics.
    """
    from urllib.parse import quote

    if not volume:
        raise ValueError("provide catalog + schema + volume names (volume is required)")

    base, signer, requests_mod, timeout = _client(conf, ctx)

    def _match(items, name):
        for it in items:
            if name in (it.get("displayName"), it.get("key")):
                return it
        return None

    catalogs = _get(requests_mod, signer, f"{base}/catalogs", timeout).get("items", [])
    if catalog:
        cat = _match(catalogs, catalog)
    elif allow_auto_pick and len(catalogs) == 1:
        cat = catalogs[0]
    else:
        cat = None
    if not cat:
        if not catalog and not allow_auto_pick:
            raise ValueError("catalog is required for destructive ops (no auto-pick)")
        raise ValueError(
            f"catalog {catalog!r} not found among "
            f"{[c.get('displayName') for c in catalogs]}"
        )

    schemas = _get(
        requests_mod, signer,
        f"{base}/schemas?catalogKey={quote(cat['key'], safe='')}", timeout,
    ).get("items", [])
    if schema:
        sch = _match(schemas, schema)
    elif allow_auto_pick and len(schemas) == 1:
        sch = schemas[0]
    else:
        sch = None
    if not sch:
        if not schema and not allow_auto_pick:
            raise ValueError("schema is required for destructive ops (no auto-pick)")
        raise ValueError(
            f"schema {schema!r} not found among "
            f"{[s.get('displayName') for s in schemas]}"
        )

    volumes = _get(
        requests_mod, signer,
        f"{base}/volumes?catalogKey={quote(cat['key'], safe='')}"
        f"&schemaKey={quote(sch['key'], safe='')}", timeout,
    ).get("items", [])
    vol = _match(volumes, volume)
    if not vol:
        raise ValueError(
            f"volume {volume!r} not found among "
            f"{[v.get('displayName') for v in volumes]}"
        )

    cat_name = cat.get("displayName") or cat.get("key") or ""
    sch_name = sch.get("displayName") or sch.get("key") or ""
    vol_name = vol.get("displayName") or vol.get("key") or ""
    return f"{cat_name}.{sch_name}.{vol_name}"


def list_catalogs(conf: Optional[Dict[str, Any]],
                  ctx: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """List all catalogs in the data lake.

    Returns the raw item dicts as returned by GET /catalogs (each typically
    has ``key``, ``displayName``, and ``catalogType``).
    """
    base, signer, requests_mod, timeout = _client(conf, ctx)
    return _get(requests_mod, signer, f"{base}/catalogs", timeout).get("items", []) or []


def list_schemas(conf: Optional[Dict[str, Any]],
                 ctx: Optional[Dict[str, Any]],
                 catalog_key: str) -> List[Dict[str, Any]]:
    """List all schemas in a catalog.

    Returns the raw item dicts as returned by GET /schemas?catalogKey=...
    (each typically has ``key`` and ``displayName``).
    """
    if not catalog_key:
        raise ValueError("catalog_key is required")
    from urllib.parse import quote
    base, signer, requests_mod, timeout = _client(conf, ctx)
    url = f"{base}/schemas?catalogKey={quote(catalog_key, safe='')}"
    return _get(requests_mod, signer, url, timeout).get("items", []) or []


def list_tables(conf: Optional[Dict[str, Any]],
                ctx: Optional[Dict[str, Any]],
                catalog_key: str, schema_key: str) -> List[Dict[str, Any]]:
    """List all tables in a (catalog, schema).

    Returns the raw item dicts as returned by
    GET /tables?catalogKey=...&schemaKey=...
    """
    if not catalog_key or not schema_key:
        raise ValueError("catalog_key and schema_key are required")
    from urllib.parse import quote
    base, signer, requests_mod, timeout = _client(conf, ctx)
    url = (
        f"{base}/tables?catalogKey={quote(catalog_key, safe='')}"
        f"&schemaKey={quote(schema_key, safe='')}"
    )
    return _get(requests_mod, signer, url, timeout).get("items", []) or []


def list_volumes(conf: Optional[Dict[str, Any]],
                 ctx: Optional[Dict[str, Any]],
                 catalog_key: str, schema_key: str) -> List[Dict[str, Any]]:
    """List all volumes in a (catalog, schema).

    Returns the raw item dicts as returned by
    GET /volumes?catalogKey=...&schemaKey=...
    """
    if not catalog_key or not schema_key:
        raise ValueError("catalog_key and schema_key are required")
    from urllib.parse import quote
    base, signer, requests_mod, timeout = _client(conf, ctx)
    url = (
        f"{base}/volumes?catalogKey={quote(catalog_key, safe='')}"
        f"&schemaKey={quote(schema_key, safe='')}"
    )
    return _get(requests_mod, signer, url, timeout).get("items", []) or []


def list_knowledge_bases(conf: Optional[Dict[str, Any]],
                         ctx: Optional[Dict[str, Any]],
                         catalog_key: str, schema_key: str) -> List[Dict[str, Any]]:
    """List all knowledge bases in a (catalog, schema).

    Returns the raw item dicts as returned by
    GET /knowledgeBases?catalogKey=...&schemaKey=...&limit=1000
    (each typically has ``key``, ``displayName``, and ``lifecycleState``).
    """
    if not catalog_key or not schema_key:
        raise ValueError("catalog_key and schema_key are required")
    from urllib.parse import quote
    base, signer, requests_mod, timeout = _client(conf, ctx)
    url = (
        f"{base}/knowledgeBases?catalogKey={quote(catalog_key, safe='')}"
        f"&schemaKey={quote(schema_key, safe='')}&limit=1000"
    )
    return _get(requests_mod, signer, url, timeout).get("items", []) or []


def describe_table(conf: Optional[Dict[str, Any]],
                   ctx: Optional[Dict[str, Any]],
                   table_key: str) -> Dict[str, Any]:
    """Describe a table (returns full detail object, including columns).

    Returns the raw response from GET /tables/{table_key}.
    """
    if not table_key:
        raise ValueError("table_key is required")
    from urllib.parse import quote
    base, signer, requests_mod, timeout = _client(conf, ctx)
    url = f"{base}/tables/{quote(table_key, safe='')}"
    return _get(requests_mod, signer, url, timeout) or {}


def list_kb_jobs(conf: Optional[Dict[str, Any]],
                 ctx: Optional[Dict[str, Any]],
                 kb_key: str) -> List[Dict[str, Any]]:
    """List ingestion jobs registered on a knowledge base.

    Returns the raw item dicts as returned by
    GET /knowledgeBases/{kb_key}/jobs (each typically has ``key``,
    ``displayName``, and ``lifecycleState``).
    """
    if not kb_key:
        raise ValueError("kb_key is required")
    from urllib.parse import quote
    base, signer, requests_mod, timeout = _client(conf, ctx)
    url = f"{base}/knowledgeBases/{quote(kb_key, safe='')}/jobs"
    return _get(requests_mod, signer, url, timeout).get("items", []) or []


def trigger_kb_job_run(conf: Optional[Dict[str, Any]],
                       ctx: Optional[Dict[str, Any]],
                       kb_key: str, job_key: str,
                       run_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Trigger a KB ingestion job run.

    POSTs to /knowledgeBases/{kb_key}/jobs/{job_key}/runs with the optional
    ``run_config`` body. Returns the raw response (typically including
    ``key``, ``runKey``, and/or ``id``).
    """
    if not kb_key or not job_key:
        raise ValueError("kb_key and job_key are required")
    from urllib.parse import quote
    base, signer, requests_mod, timeout = _client(conf, ctx)
    url = (
        f"{base}/knowledgeBases/{quote(kb_key, safe='')}"
        f"/jobs/{quote(job_key, safe='')}/runs"
    )
    body = run_config if isinstance(run_config, dict) else {}
    return _post(requests_mod, signer, url, timeout, body=body) or {}


__all__ = [
    "parse_uri",
    "read_file",
    "write_file",
    "list_files",
    "read_text",
    "write_text",
    # Catalog-toolkit delegation helpers
    "resolve_volume_key",
    "list_catalogs",
    "list_schemas",
    "list_tables",
    "list_volumes",
    "list_knowledge_bases",
    "describe_table",
    "list_kb_jobs",
    "trigger_kb_job_run",
]
