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

    # SQL-tool connection helpers (mirror AIDP connection_manager.py dispatch)
    get_connection(catalog_key, conf, ctx)                  -> dict
    get_standard_catalog_connection(catalog_key, conf, ctx) -> dict
    get_external_catalog_connection(catalog_key, conf, ctx) -> dict

CONFIG KEYS (conf dict, mirrors aidp_catalog_toolkit)
-----------------------------------------------------
    region              OCI region short code (also OCI_REGION env)
    data_lake_ocid      data lake OCID (also DATALAKE_ID env / ctx["datalake_id"])
    api_version         default "20260430"
    service_path        default "dataLakes"
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

    Supported modes (auth_mode):
      - resource_principal (default; cluster injects identity)
      - instance_principal (OCI VM with instance principals)
      - session_token     (~/.oci/sessions/<profile>/ from `oci session authenticate`
                           — no permanent PEM, auto-rotating)
      - user_principal    (full tenancy + user + fingerprint + private_key_content)
      - auto              (try resource_principal -> session_token -> user_principal
                           in order; first one that constructs without error wins)

    The `auto` mode is the recommended choice for tools deployed across
    cluster (resource_principal works) and developer-local (session_token
    works) contexts. It removes the need to think about auth at all.
    """
    import oci  # lazy

    mode = (_get_cfg(conf, "auth_mode", "resource_principal")
            or "resource_principal").lower()

    if mode == "auto":
        return _build_auto_signer(conf)
    if mode in ("session", "session_token"):
        return _build_session_token_signer(conf)
    if mode in ("user", "user_principal"):
        return _build_user_principal_signer(conf)
    if mode in ("instance", "instance_principal"):
        return oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    # Default: resource_principal.
    return oci.auth.signers.get_resource_principals_signer()


def _build_auto_signer(conf: Dict[str, Any]):
    """Try every available auth mode in priority order; return the first
    that constructs. Logs which one was picked so debugging is possible."""
    import oci
    attempts = [
        ("resource_principal", lambda: oci.auth.signers.get_resource_principals_signer()),
        ("session_token", lambda: _build_session_token_signer(conf)),
        ("user_principal", lambda: _build_user_principal_signer(conf)),
    ]
    last_err = None
    for name, build in attempts:
        try:
            signer = build()
            try:
                from aidp_debug import debug  # type: ignore
                debug("aidp_io._build_signer: auto picked", mode=name)
            except Exception:
                pass
            return signer
        except Exception as e:
            last_err = (name, e)
            continue
    raise ValueError(
        f"auth_mode=auto: no usable signer available. Last error from "
        f"{last_err[0] if last_err else '?'}: {last_err[1] if last_err else 'n/a'}. "
        f"Either deploy on AIDP compute (resource_principal), run "
        f"`oci session authenticate` (session_token), or set tenancy_ocid + "
        f"user_ocid + fingerprint + private_key_content (user_principal)."
    )


def _build_session_token_signer(conf: Dict[str, Any]):
    """Use a session token from `oci session authenticate`.

    Two input shapes (checked in order):

      1. **Inline** — conf carries the session token + session private key
         as strings. Used when the tool's zip is built with `--test-creds`
         and the OCI profile is a session profile. Conf keys:
           - `session_token`        (the JWT string from the token file)
           - `session_key_content`  (the PEM string from the session key file)

      2. **File-based** — read ~/.oci/config[profile] and its referenced
         `security_token_file` / `key_file`. Profile selection order:
            a. conf['oci_config_profile']
            b. AIDP_OCI_PROFILE env var
            c. 'DEFAULT'
         Used for local dev where the home directory is reachable.

    Either way, the session key is rotated by `oci session authenticate`,
    so callers never handle a permanent PEM.
    """
    import oci

    # 1. Inline (the zip-embedded path).
    token = _get_cfg(conf, "session_token", "")
    key_content = _get_cfg(conf, "session_key_content", "")
    if token and key_content:
        from cryptography.hazmat.primitives import serialization as _ser
        from cryptography.hazmat.backends import default_backend as _backend
        private_key = _ser.load_pem_private_key(
            key_content.encode("utf-8") if isinstance(key_content, str) else key_content,
            password=None,
            backend=_backend(),
        )
        return oci.auth.signers.SecurityTokenSigner(token, private_key)

    # 2. File-based fallback.
    import configparser
    from pathlib import Path
    import os

    profile = (
        _get_cfg(conf, "oci_config_profile", "")
        or os.environ.get("AIDP_OCI_PROFILE", "")
        or "DEFAULT"
    )
    cfg_path = Path(os.environ.get("OCI_CONFIG_FILE", "")
                    or (Path.home() / ".oci" / "config"))
    if not cfg_path.is_file():
        raise ValueError(
            f"session_token auth requires inline `session_token` + "
            f"`session_key_content` in conf, or {cfg_path}. "
            f"Run `oci session authenticate` to create the file."
        )
    cp = configparser.ConfigParser()
    cp.read(cfg_path)
    if profile not in cp:
        raise ValueError(
            f"session_token auth: profile [{profile}] not found in {cfg_path}. "
            f"Available: {', '.join(cp.sections()) or '(none)'}"
        )
    section = cp[profile]
    token_path = section.get("security_token_file", "").strip()
    key_path = section.get("key_file", "").strip()
    if not token_path or not key_path:
        raise ValueError(
            f"profile [{profile}] is not a session profile (no security_token_file). "
            f"Run `oci session authenticate --profile-name {profile}` to create one."
        )
    token = Path(token_path).expanduser().read_text(encoding="utf-8").strip()
    private_key = oci.signer.load_private_key_from_file(
        str(Path(key_path).expanduser()), None,
    )
    return oci.auth.signers.SecurityTokenSigner(token, private_key)


def _build_user_principal_signer(conf: Dict[str, Any]):
    """Classic API-key signer. PEM private key embedded in conf."""
    import oci
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
    service_path = _get_cfg(conf, "service_path", "dataLakes") or "dataLakes"
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

def _get(requests_mod, signer, url: str, timeout: int,
         headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    h = {"Accept": "application/json"}
    if headers:
        h.update(headers)
    r = requests_mod.get(url, auth=signer, timeout=timeout, headers=h)
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


# ---------------------------------------------------------------------------
# Fuzzy / forgiving name resolvers
# ---------------------------------------------------------------------------
#
# The first-party AIDP SQL tool's Test panel can't pick names from a dropdown,
# so users end up typing partial / wrong-case / wrong-form names and getting
# 404 NotAuthorizedOrNotFound. These helpers accept any of:
#
#   - exact key                ("construction_catalog")
#   - exact display name       ("Construction Catalog")
#   - case-insensitive match
#   - unique prefix            ("construct" matches "construction_catalog")
#   - unique substring         ("ction_cat" also matches)
#
# Tools that take a catalog/schema/volume/table/kb name should call these
# from _execute_tool BEFORE building the URL — turns the user's "construct"
# into the canonical "construction_catalog" deterministically. If the input
# is ambiguous (>1 hit) or missing (0 hits), the helper raises ValueError
# with the candidates listed, so the tool can surface a useful error envelope.


def _fuzzy_pick(items: List[Dict[str, Any]],
                needle: str,
                *,
                key_field: str = "key",
                name_field: str = "displayName",
                what: str = "item") -> Dict[str, Any]:
    """Pick exactly one item from `items` whose key or display name matches
    `needle`. Match order (first to win):
      1. exact key
      2. exact display name (case-sensitive)
      3. exact display name (case-insensitive)
      4. unique prefix on key OR display name (case-insensitive)
      5. unique substring on key OR display name (case-insensitive)

    Raises ValueError on 0 matches or >1 ambiguous matches at any stage.
    """
    if not needle or not str(needle).strip():
        raise ValueError(f"{what}: empty name provided")
    n = str(needle).strip()
    nl = n.lower()

    # 1. exact key
    hits = [it for it in items if str(it.get(key_field, "")) == n]
    if len(hits) == 1:
        return hits[0]

    # 2. exact display name (case-sensitive)
    hits = [it for it in items if str(it.get(name_field, "")) == n]
    if len(hits) == 1:
        return hits[0]

    # 3. exact display name (case-insensitive)
    hits = [it for it in items if str(it.get(name_field, "")).lower() == nl]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        names = [it.get(name_field) or it.get(key_field) for it in hits]
        raise ValueError(
            f"{what} '{needle}': ambiguous case-insensitive match in {names}"
        )

    # 4. unique prefix
    hits = [
        it for it in items
        if str(it.get(key_field, "")).lower().startswith(nl)
        or str(it.get(name_field, "")).lower().startswith(nl)
    ]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        names = [it.get(name_field) or it.get(key_field) for it in hits]
        raise ValueError(
            f"{what} prefix '{needle}': ambiguous, matches {names}"
        )

    # 5. unique substring
    hits = [
        it for it in items
        if nl in str(it.get(key_field, "")).lower()
        or nl in str(it.get(name_field, "")).lower()
    ]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        names = [it.get(name_field) or it.get(key_field) for it in hits]
        raise ValueError(
            f"{what} substring '{needle}': ambiguous, matches {names}"
        )

    # 0 matches anywhere.
    available = [it.get(name_field) or it.get(key_field) for it in items][:20]
    raise ValueError(
        f"{what} '{needle}': not found. Available: {available}"
        + (" (+ more)" if len(items) > 20 else "")
    )


def resolve_catalog(conf: Optional[Dict[str, Any]],
                    ctx: Optional[Dict[str, Any]],
                    name_or_key: str) -> Dict[str, Any]:
    """Fuzzy-match a catalog by name, key, prefix, or substring.
    Returns the full catalog dict including `key` and `displayName`."""
    items = list_catalogs(conf, ctx)
    return _fuzzy_pick(items, name_or_key, what="catalog")


def resolve_schema(conf: Optional[Dict[str, Any]],
                   ctx: Optional[Dict[str, Any]],
                   catalog_name_or_key: str,
                   schema_name_or_key: str) -> Dict[str, Any]:
    """Fuzzy-match a schema within a (fuzzy-matched) catalog."""
    cat = resolve_catalog(conf, ctx, catalog_name_or_key)
    items = list_schemas(conf, ctx, cat["key"])
    schema = _fuzzy_pick(items, schema_name_or_key, what="schema")
    schema["_catalog"] = cat
    return schema


def resolve_table(conf: Optional[Dict[str, Any]],
                  ctx: Optional[Dict[str, Any]],
                  catalog_name_or_key: str,
                  schema_name_or_key: str,
                  table_name_or_key: str) -> Dict[str, Any]:
    """Fuzzy-match a table within a (fuzzy-matched) catalog + schema."""
    sch = resolve_schema(conf, ctx, catalog_name_or_key, schema_name_or_key)
    items = list_tables(conf, ctx, sch["_catalog"]["key"], sch["key"])
    table = _fuzzy_pick(items, table_name_or_key, what="table")
    table["_schema"] = sch
    return table


def resolve_volume(conf: Optional[Dict[str, Any]],
                   ctx: Optional[Dict[str, Any]],
                   catalog_name_or_key: str,
                   schema_name_or_key: str,
                   volume_name_or_key: str) -> Dict[str, Any]:
    """Fuzzy-match a volume within a (fuzzy-matched) catalog + schema."""
    sch = resolve_schema(conf, ctx, catalog_name_or_key, schema_name_or_key)
    items = list_volumes(conf, ctx, sch["_catalog"]["key"], sch["key"])
    vol = _fuzzy_pick(items, volume_name_or_key, what="volume")
    vol["_schema"] = sch
    return vol


def resolve_kb(conf: Optional[Dict[str, Any]],
               ctx: Optional[Dict[str, Any]],
               name_or_key: str,
               *,
               catalog_name_or_key: Optional[str] = None,
               schema_name_or_key: Optional[str] = None) -> Dict[str, Any]:
    """Fuzzy-match a knowledge base. If catalog + schema are given, scope
    the search; otherwise search all visible KBs."""
    if catalog_name_or_key and schema_name_or_key:
        sch = resolve_schema(conf, ctx, catalog_name_or_key, schema_name_or_key)
        items = list_knowledge_bases(conf, ctx, sch["_catalog"]["key"], sch["key"])
    else:
        items = list_knowledge_bases(conf, ctx)
    return _fuzzy_pick(items, name_or_key, what="knowledge base")


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


# ---------------------------------------------------------------------------
# SQL-tool connection helpers
# ---------------------------------------------------------------------------
#
# These functions mirror AIDP's ``aidputils/agents/tools/sqltool/connection_manager.py``
# verbatim — same endpoint URLs, same auth headers, same response field names —
# so that custom tools running outside the AIDP runtime can fetch the *exact*
# same connection material that AIDP's first-party SQLTool would receive.
#
# DISPATCH OVERVIEW
# -----------------
# AIDP classifies every catalog as either STANDARD or EXTERNAL (defaults to
# EXTERNAL when unset). The two branches answer fundamentally different
# questions:
#
#   STANDARD catalog (e.g. lakehouse-managed Spark tables)
#       --> the "connection" is a Spark/OCI Data Flow execution context.
#       There is no JDBC URL or password — queries are run by submitting a
#       Spark command to an OCI Data Flow cluster via AIDP's gateway. The
#       caller needs: cluster_key, compute_endpoint (AIDP_GATEWAY_ENDPOINT),
#       data_lake_id, workspace_key, and hub_dp_endpoint. Auth is OCI signing
#       (resource principal), handled by the OCI SDK at call time.
#
#       Mirrors: ``_SparkSQLExecutor._resolve_dp_endpoint()`` +
#       ``_resolve_cluster_status_context()`` in spark_sql_executor.py.
#
#   EXTERNAL catalog (e.g. Autonomous Database / ADW, ATP, generic Oracle)
#       --> the "connection" is a JDBC-style credential bundle. AIDP fetches
#       it by signing a GET request to the lakeproxy connectionData endpoint:
#
#           GET {lakeproxy_endpoint}/20240831/dataLakes/{datalakeId}
#               /catalogs/{catalogKey}/connectionData
#
#       The response's ``connectionProperties`` object contains the keys
#       ``user.name``, ``password``, ``tns``, and (optionally) ``wallet.content``
#       (base64 wallet zip) and ``wallet.password``. Those map straight into
#       an ``oracledb.SessionPool`` call. Headers always include
#       ``dh-user-principal`` from the AIDP auth context, when available.
#
#       Mirrors: ``_ConnectionManager.fetch_connection_data()`` +
#       ``get_connection_data_endpoint()`` in connection_manager.py.
#
# Both branches return the same envelope::
#
#       {
#           "type": "STANDARD" | "EXTERNAL",
#           "credentials": {...},   # branch-specific dict the caller passes
#                                   # to oracledb / spark / OCI Data Flow.
#           "raw": {...},           # full unmodified server response (or the
#                                   # collected runtime config, for STANDARD)
#       }
#
# Catalog type detection
# ----------------------
# ``get_connection`` will look up the catalog's type by calling
# ``list_catalogs(conf, ctx)`` and matching ``catalog_key`` against either
# ``displayName`` or ``key``; if a match has ``catalogType == 'STANDARD'`` it
# routes to the standard branch, otherwise EXTERNAL. Callers can short-circuit
# detection by setting ``conf["catalog_type"]`` (or ``conf["catalogType"]``)
# to ``"STANDARD"`` or ``"EXTERNAL"``.


# ---- field-name constants (mirror aidputils.agents.tools.utils.Constants) ----

_CONST_DH_USER_PRINCIPAL_KEY = "dh-user-principal"
_CONST_ACCEPT_JSON = "application/json"
_CONST_CONTENT_TYPE_JSON = "application/json"
_CONST_CONNECTION_PROPS_KEY = "connectionProperties"
_CONST_USER_NAME = "user.name"
_CONST_USER_CREDENTIAL = "password"
_CONST_TNS = "tns"
_CONST_WALLET_CONTENT = "wallet.content"
_CONST_WALLET_CREDENTIAL = "wallet.password"


def _resolve_data_lake_id(conf: Optional[Dict[str, Any]],
                          ctx: Optional[Dict[str, Any]]) -> str:
    """Resolve the data lake OCID from conf, env, or context (same precedence as _client)."""
    val = (
        _get_cfg(conf, "data_lake_ocid", "")
        or os.environ.get("DATALAKE_ID", "")
        or (ctx or {}).get("datalake_id", "")
    )
    if not val:
        raise ValueError(
            "data_lake_ocid is required (config 'data_lake_ocid' or DATALAKE_ID env "
            "or ctx['datalake_id'])"
        )
    return str(val)


def _resolve_lakeproxy_endpoint(conf: Optional[Dict[str, Any]],
                                ctx: Optional[Dict[str, Any]],
                                datalake_id: str) -> str:
    """Resolve the lakeproxy base endpoint, mirroring auth_utils.get_lakeproxy_endpoint.

    Precedence: conf['lakeproxy_endpoint'] > ctx['lakeproxy_endpoint'] >
    env OCI_HUB_DP_ENDPOINT. If the resolved value does not already end in
    ``dataLakes/{datalake_id}``, we append it (matching AIDP's
    auth_utils.get_lakeproxy_endpoint behavior).
    """
    dp_endpoint = (
        _get_cfg(conf, "lakeproxy_endpoint", "")
        or (ctx or {}).get("lakeproxy_endpoint", "")
        or os.environ.get("OCI_HUB_DP_ENDPOINT", "")
    )
    if not dp_endpoint:
        raise ValueError(
            "lakeproxy_endpoint is required (config 'lakeproxy_endpoint' or "
            "ctx['lakeproxy_endpoint'] or OCI_HUB_DP_ENDPOINT env)"
        )
    dp_endpoint = str(dp_endpoint).rstrip("/")
    suffix = f"dataLakes/{datalake_id}"
    if dp_endpoint.endswith(suffix):
        return dp_endpoint
    return f"{dp_endpoint}/{suffix}"


def _resolve_dh_user_principal(conf: Optional[Dict[str, Any]],
                               ctx: Optional[Dict[str, Any]]) -> Optional[str]:
    """Resolve the dh-user-principal header value. May be None (header omitted)."""
    val = (
        _get_cfg(conf, "dh_user_principal", "")
        or (ctx or {}).get("dh_user_principal", "")
        or os.environ.get("DH_USER_PRINCIPAL", "")
    )
    return str(val) if val else None


def _detect_catalog_type(catalog_key: str,
                         conf: Optional[Dict[str, Any]],
                         ctx: Optional[Dict[str, Any]]) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Detect a catalog's type (STANDARD vs EXTERNAL).

    Resolution order (mirrors SQLTool._resolve_catalog_type semantics, then
    falls back to a server lookup when the type is not provided by the caller):

      1. If conf['catalogType'] or conf['catalog_type'] is set, use it.
      2. Otherwise, walk list_catalogs(conf, ctx) and match catalog_key against
         each item's ``key`` or ``displayName``. Use the matched item's
         ``catalogType``.
      3. If still unknown, default to ``"EXTERNAL"`` (same default as SQLTool).

    Returns: (normalized_type, matched_catalog_item_or_None).
    """
    explicit = _get_cfg(conf, "catalogType", "") or _get_cfg(conf, "catalog_type", "")
    if explicit:
        return str(explicit).strip().upper() or "EXTERNAL", None

    matched: Optional[Dict[str, Any]] = None
    try:
        catalogs = list_catalogs(conf, ctx)
    except Exception:
        catalogs = []
    for it in catalogs or []:
        if catalog_key in (it.get("key"), it.get("displayName")):
            matched = it
            break

    if matched is None:
        return "EXTERNAL", None
    catalog_type = str(matched.get("catalogType") or "EXTERNAL").strip().upper()
    return catalog_type or "EXTERNAL", matched


def get_standard_catalog_connection(catalog_key: str,
                                    conf: Optional[Dict[str, Any]],
                                    ctx: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return the standard-catalog (Spark / OCI Data Flow) connection bundle.

    Mirrors AIDP's ``_SparkSQLExecutor`` setup verbatim: the "connection" is a
    set of runtime identifiers (cluster key + gateway / hub-dp endpoints +
    workspace key + data lake id), NOT a JDBC URL. Queries against a STANDARD
    catalog are executed via OCI Data Flow command submission against these
    identifiers, signed at call time by an OCI signer (resource principal by
    default).

    Resolution (matches spark_sql_executor.py):
      - cluster_key:       conf['clusterKey'] (REQUIRED for actual execution;
                           may be empty here — the caller decides whether
                           cluster_key is needed before submitting a command).
      - compute_endpoint:  env ``AIDP_GATEWAY_ENDPOINT``  (or conf override).
      - hub_dp_endpoint:   env ``OCI_HUB_DP_ENDPOINT``    (or conf override).
      - data_lake_id:      ctx['datalake_id'] / env DATALAKE_ID / conf.
      - workspace_key:     ctx['AIDP_WORKSPACE_KEY'] / env AIDP_WORKSPACE_KEY.

    Returns::

        {
            "type": "STANDARD",
            "credentials": {
                "cluster_key": str,            # may be "" if not supplied
                "compute_endpoint": str|None,
                "hub_dp_endpoint": str|None,
                "data_lake_id": str,
                "workspace_key": str|None,
                "dh_user_principal": str|None,
                "auth_type": "resource_principal",
            },
            "raw": {... same fields ...},
        }

    Raises:
        ValueError if data_lake_id cannot be resolved.
    """
    if not catalog_key:
        raise ValueError("catalog_key is required")

    data_lake_id = _resolve_data_lake_id(conf, ctx)

    cluster_key = _get_cfg(conf, "clusterKey", "") or _get_cfg(conf, "cluster_key", "")

    compute_endpoint = (
        _get_cfg(conf, "compute_endpoint", "")
        or (ctx or {}).get("compute_endpoint", "")
        or os.environ.get("AIDP_GATEWAY_ENDPOINT", "")
    )

    hub_dp_endpoint = (
        _get_cfg(conf, "hub_dp_endpoint", "")
        or (ctx or {}).get("hub_dp_endpoint", "")
        or os.environ.get("OCI_HUB_DP_ENDPOINT", "")
    )

    workspace_key = (
        _get_cfg(conf, "workspace_key", "")
        or (ctx or {}).get("AIDP_WORKSPACE_KEY", "")
        or (ctx or {}).get("workspace_key", "")
        or os.environ.get("AIDP_WORKSPACE_KEY", "")
    )

    credentials = {
        "catalog_key": str(catalog_key),
        "cluster_key": str(cluster_key) if cluster_key else "",
        "compute_endpoint": str(compute_endpoint) if compute_endpoint else None,
        "hub_dp_endpoint": str(hub_dp_endpoint) if hub_dp_endpoint else None,
        "data_lake_id": data_lake_id,
        "workspace_key": str(workspace_key) if workspace_key else None,
        "dh_user_principal": _resolve_dh_user_principal(conf, ctx),
        "auth_type": "resource_principal",
    }

    return {
        "type": "STANDARD",
        "credentials": credentials,
        "raw": dict(credentials),
    }


def get_external_catalog_connection(catalog_key: str,
                                    conf: Optional[Dict[str, Any]],
                                    ctx: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return the external-catalog (oracledb) connection bundle.

    Mirrors AIDP's ``_ConnectionManager.fetch_connection_data()`` verbatim:

      - URL:     ``{lakeproxy_endpoint}/20240831/dataLakes/{datalakeId}
                   /catalogs/{catalogKey}/connectionData``
      - Auth:    OCI request signing (resource principal by default).
      - Headers: ``accept: application/json``,
                 ``content-type: application/json``,
                 ``dh-user-principal: <auth context value>`` (when available).
      - Response field: ``connectionProperties`` — a dict with keys
                 ``user.name``, ``password``, ``tns``, and optional
                 ``wallet.content`` (base64 wallet zip) +
                 ``wallet.password``.

    Returns::

        {
            "type": "EXTERNAL",
            "credentials": {
                "user.name":       str,
                "password":        str,
                "tns":             str,
                "wallet.content":  str | None,   # base64-encoded wallet zip
                "wallet.password": str | None,
                "dh_user_principal": str | None,
                "auth_type": "resource_principal",
            },
            "raw": {... full unmodified server response ...},
        }

    Raises:
        ValueError if catalog_key, data_lake_id, or lakeproxy_endpoint cannot
        be resolved, or if the response is missing ``connectionProperties``.
        Any HTTP / OCI signing exception is bubbled up.
    """
    if not catalog_key:
        raise ValueError("catalog_key is required")

    import requests  # lazy

    data_lake_id = _resolve_data_lake_id(conf, ctx)
    lakeproxy_endpoint = _resolve_lakeproxy_endpoint(conf, ctx, data_lake_id)
    signer = _build_signer(conf or {})
    timeout = int(_get_cfg(conf, "timeout", 30) or 30)

    # Mirror connection_manager.get_connection_data_endpoint() verbatim.
    url = (
        f"{lakeproxy_endpoint}/"
        f"20240831/dataLakes/{data_lake_id}/"
        f"catalogs/{catalog_key}/connectionData"
    )

    # Mirror _build_request_headers() verbatim, but only include
    # dh-user-principal when we actually have a value (AIDP sets it
    # unconditionally from auth context, but that context isn't available to
    # custom tools).
    headers = {
        "accept": _CONST_ACCEPT_JSON,
        "content-type": _CONST_CONTENT_TYPE_JSON,
    }
    dh_user = _resolve_dh_user_principal(conf, ctx)
    if dh_user:
        headers[_CONST_DH_USER_PRINCIPAL_KEY] = dh_user

    r = requests.get(url, auth=signer, headers=headers, timeout=timeout)
    r.raise_for_status()
    conn_data = r.json() if r.content else {}

    # Mirror _get_connection_properties() — must be a dict under
    # the verbatim key 'connectionProperties'.
    connection_props = conn_data.get(_CONST_CONNECTION_PROPS_KEY)
    if not isinstance(connection_props, dict):
        raise ValueError(
            f"connectionProperties is missing from SQL connection data for "
            f"catalog {catalog_key!r} (response keys: {list(conn_data.keys())})"
        )

    credentials = {
        _CONST_USER_NAME:        connection_props.get(_CONST_USER_NAME),
        _CONST_USER_CREDENTIAL:  connection_props.get(_CONST_USER_CREDENTIAL),
        _CONST_TNS:              connection_props.get(_CONST_TNS),
        _CONST_WALLET_CONTENT:   connection_props.get(_CONST_WALLET_CONTENT),
        _CONST_WALLET_CREDENTIAL: connection_props.get(_CONST_WALLET_CREDENTIAL),
        "dh_user_principal":     dh_user,
        "auth_type":             "resource_principal",
    }

    return {
        "type": "EXTERNAL",
        "credentials": credentials,
        "raw": conn_data,
    }


def get_connection(catalog_key: str,
                   conf: Optional[Dict[str, Any]],
                   ctx: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Top-level connection dispatcher (STANDARD vs EXTERNAL).

    Mirrors the dispatch performed by AIDP's first-party SQLTool / connection
    manager (see module-level "SQL-tool connection helpers" docstring for the
    full overview):

      1. Determine the catalog type:
           - if conf['catalogType'] / conf['catalog_type'] is set, honor it;
           - otherwise call ``list_catalogs(conf, ctx)`` and look up the
             ``catalogType`` field for the matching item;
           - otherwise default to ``"EXTERNAL"`` (same default as SQLTool).
      2. If type == ``"STANDARD"`` -> ``get_standard_catalog_connection(...)``.
         Else                       -> ``get_external_catalog_connection(...)``.

    Returns::

        {
            "type": "STANDARD" | "EXTERNAL",
            "credentials": {...},   # ready to be handed to spark / oracledb
            "raw": {...},           # full backing response (or runtime cfg)
        }

    Raises:
        ValueError if catalog_key is empty or required IDs cannot be resolved;
        bubbles up HTTP / OCI exceptions from the underlying branch.
    """
    if not catalog_key:
        raise ValueError("catalog_key is required")

    catalog_type, _matched = _detect_catalog_type(catalog_key, conf, ctx)
    if catalog_type == "STANDARD":
        return get_standard_catalog_connection(catalog_key, conf, ctx)
    return get_external_catalog_connection(catalog_key, conf, ctx)


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
    # SQL-tool connection helpers
    "get_connection",
    "get_standard_catalog_connection",
    "get_external_catalog_connection",
    # Fuzzy name resolvers (the SQL-tool dropdown alternative)
    "resolve_catalog",
    "resolve_schema",
    "resolve_table",
    "resolve_volume",
    "resolve_kb",
]
