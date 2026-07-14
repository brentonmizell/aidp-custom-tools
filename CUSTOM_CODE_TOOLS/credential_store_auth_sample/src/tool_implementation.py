"""Credential Store auth — reference implementation for custom code tools.

JR-Sambit thread (Jun 2026) — when a custom tool must call the public AIDP /
OCI APIs from inside AIDP, the working production path is *not* the resource-
principal signer and *not* a hardcoded PEM in the zip. It is:

    1. Operator creates a SECRET_TOKEN credential in AIDP's Credential Store
       containing four secret-key pairs: tenancy / user / fingerprint /
       private_key  (the PEM body, not a path).
    2. The tool config carries the credential's display name as a regular
       (non-secret) parameter — the secret value never leaves the store.
    3. At runtime, the tool calls `aidputils.secrets.get(name, key)` for each
       field and constructs an `oci.signer.Signer(private_key_content=...)`.

This sample exposes two operations against the same volumes endpoint that
returned 401 under resource principal — confirming the credential-store
signer succeeds where rp signing fails.

Required SECRET_TOKEN keys
--------------------------
The credential MUST have these four secret-key entries; the SDK normalizes
them via _normalize_secret_token in CredentialStoreService:

    tenancy       ocid1.tenancy.oc1..…
    user          ocid1.user.oc1..…
    fingerprint   aa:bb:cc:dd:…
    private_key   -----BEGIN RSA PRIVATE KEY-----\\n…\\n-----END RSA PRIVATE KEY-----\\n
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import requests

from aidputils.agents.tools.custom_tools.base import CustomToolBase

from .utils.config_utils import get_cfg, ok, fail

try:
    from aidp_debug import debug, debug_warn, debug_error, DebugLog
except ImportError:
    def debug(*a, **k): pass
    def debug_warn(*a, **k): pass
    def debug_error(*a, **k): pass
    class DebugLog:
        @staticmethod
        def embed(r): return r


REQUIRED_SECRET_KEYS = ("tenancy", "user", "fingerprint", "private_key")

# OCI region-code (in the OCID) -> full region name. Covers the commercial
# realm; unknown codes fall back to env vars / conf.
_OCI_REGION_CODES = {
    "iad": "us-ashburn-1", "phx": "us-phoenix-1", "sjc": "us-sanjose-1",
    "yyz": "ca-toronto-1", "yul": "ca-montreal-1",
    "lhr": "uk-london-1", "cwl": "uk-cardiff-1",
    "fra": "eu-frankfurt-1", "zrh": "eu-zurich-1", "ams": "eu-amsterdam-1",
    "cdg": "eu-paris-1", "mrs": "eu-marseille-1", "mad": "eu-madrid-1",
    "arn": "eu-stockholm-1", "lin": "eu-milan-1",
    "nrt": "ap-tokyo-1", "kix": "ap-osaka-1", "icn": "ap-seoul-1",
    "syd": "ap-sydney-1", "mel": "ap-melbourne-1", "bom": "ap-mumbai-1",
    "hyd": "ap-hyderabad-1", "sin": "ap-singapore-1",
    "gru": "sa-saopaulo-1", "scl": "sa-santiago-1", "vcp": "sa-vinhedo-1",
    "jed": "me-jeddah-1", "dxb": "me-dubai-1", "auh": "me-abudhabi-1",
    "jnb": "af-johannesburg-1",
}


def _region_from_ocid(ocid: str) -> Optional[str]:
    """Derive the region from the OCID's region-code segment:
    ocid1.aidataplatform.oc1.<regioncode>.<unique> -> full region name."""
    parts = str(ocid or "").split(".")
    if len(parts) >= 4:
        return _OCI_REGION_CODES.get(parts[3].strip().lower())
    return None


def _render_map_text(tree: list, counts: dict) -> str:
    """Human-readable indented tree of the map result — easier to scan than
    the nested JSON in the Test panel."""
    lines = [
        f"{counts['catalogs']} catalogs | {counts['schemas']} schemas | "
        f"{counts['tables']} tables | {counts['volumes']} volumes | "
        f"{counts['knowledge_bases']} KBs",
        "",
    ]
    for c in tree:
        lines.append(f"* {c['catalog_key']} [{c.get('type') or '?'}]")
        for s in c.get("schemas", []):
            lines.append(f"  - {s.get('schema_name') or s.get('schema_key')}")
            for label, key in (("tables", "tables"), ("volumes", "volumes"),
                               ("KBs", "knowledge_bases")):
                items = s.get(key, [])
                if items:
                    names = ", ".join(i.get("displayName") or i.get("key")
                                      for i in items)
                    lines.append(f"      {label} ({len(items)}): {names}")
            if s.get("errors"):
                lines.append(f"      ! errors: {s['errors']}")
    return "\n".join(lines)


def _mask(value: Optional[str], keep: int = 4) -> str:
    """Truncate a secret for debug output. Never log full tokens / keys."""
    if not value:
        return "<empty>"
    if len(value) <= keep * 2:
        return f"<{len(value)} chars>"
    return f"{value[:keep]}…{value[-keep:]}  ({len(value)} chars)"


def _build_signer(credential_name: str) -> tuple:
    """Resolve the credential by display name. Returns a 3-tuple:
    (signer, redacted_meta, bundle_cfg). On failure: (None, error_msg, {}).

    bundle_cfg carries the NON-secret connection fields the credential may
    also hold — data_lake_ocid and region — so the tool can be driven by
    credential_name alone (no data_lake_ocid needed in conf or the Test panel).
    """
    try:
        import aidputils.secrets as secrets
    except ImportError as ex:
        return None, f"aidputils.secrets not available: {ex}", {}

    try:
        # One bulk fetch + key validation gives a better error than four
        # silent get(name, key) calls if the credential is misconfigured.
        bundle = secrets.get(credential_name)
    except Exception as ex:
        return None, f"Credential `{credential_name}` could not be read: {ex}", {}

    if not isinstance(bundle, dict):
        return None, (f"Credential `{credential_name}` did not return a dict "
                      f"(got {type(bundle).__name__}). The credential must be "
                      f"SECRET_TOKEN type, not SERVICE_ACCOUNT or VAULT_REFERENCE."), {}

    missing = [k for k in REQUIRED_SECRET_KEYS if not bundle.get(k)]
    if missing:
        return None, (f"Credential `{credential_name}` is missing secret keys: "
                      f"{missing}. Required: {list(REQUIRED_SECRET_KEYS)}."), {}

    import re
    # Normalize: secret stores / paste forms on Windows add \r\n + stray
    # whitespace. A stray char in the fingerprint or OCIDs corrupts the signed
    # keyId header and produces an opaque 401 NotAuthenticated.
    tenancy = str(bundle["tenancy"]).strip()
    user = str(bundle["user"]).strip()
    fingerprint = "".join(str(bundle["fingerprint"]).split())
    private_key = (str(bundle["private_key"]).replace("\r\n", "\n")
                   .replace("\r", "\n").strip() + "\n")

    if not re.fullmatch(r"[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){15}", fingerprint):
        return None, (
            f"fingerprint doesn't look like a valid OCI API-key fingerprint "
            f"(got {len(fingerprint)} chars; expected 47 in the form "
            f"aa:bb:…:zz). Copy it exactly from OCI Console → your user → "
            f"API Keys, or derive it: openssl rsa -pubout -outform DER -in "
            f"key.pem | openssl md5 -c"), {}

    import oci
    # private_key_file_location is a required positional arg in some OCI SDK
    # builds (e.g. 2.175.x preview) even when signing from private_key_content.
    signer = oci.signer.Signer(
        tenancy=tenancy,
        user=user,
        fingerprint=fingerprint,
        private_key_file_location=None,
        private_key_content=private_key,
    )
    redacted = {
        "tenancy":     _mask(tenancy, 6),
        "user":        _mask(user, 6),
        "fingerprint": _mask(fingerprint, 2),
        "fingerprint_len": len(fingerprint),
        "private_key": _mask(private_key, 12),
    }
    bundle_cfg = {
        "data_lake_ocid": str(bundle.get("data_lake_ocid")
                              or bundle.get("datalake_ocid")
                              or bundle.get("lake_ocid") or "").strip(),
        "region": str(bundle.get("region") or "").strip(),
    }
    return signer, redacted, bundle_cfg


@CustomToolBase.register
class CredentialStoreAuthSample(CustomToolBase):
    """Demonstrates calling public AIDP APIs with a credential-store-backed
    OCI signer. Two operations:

        op="whoami"        — calls GET /20240501/users/{userId} on identity
                             to confirm the signer is valid end-to-end.
        op="list_volumes"  — calls GET /20260430/aiDataPlatforms/{lakeOcid}/
                             catalogs/{catalogKey}/schemas/{schemaKey}/volumes
                             with the credential-store signer (the call that
                             returned 401 under resource principal).
    """

    @classmethod
    def _execute_tool(cls, conf: Dict[str, Any], runtime_params: Dict[str, Any],
                      **context_vars) -> Dict[str, Any]:
        op = (runtime_params.get("op") or "map").lower()
        credential_name = (runtime_params.get("credential_name")
                           or get_cfg(conf, "credential_name", ""))
        timeout = get_cfg(conf, "timeout", 30)

        debug(f"CredentialStoreAuthSample op={op} credential_name={credential_name!r}")

        if not credential_name:
            return DebugLog.embed(fail(
                "credential_name is required — pass it as a runtime param or "
                "set conf.credential_name.", "ValidationError"))

        signer, meta, bundle_cfg = _build_signer(credential_name)
        if signer is None:
            return DebugLog.embed(fail(meta, "CredentialStoreError"))
        debug(f"Signer constructed. Redacted credential meta: {meta}")

        # Resolve data_lake_ocid: runtime param -> credential bundle -> conf.
        # Putting it on the credential means the Test panel needs only op +
        # credential_name.
        lake = (runtime_params.get("data_lake_ocid")
                or bundle_cfg.get("data_lake_ocid")
                or get_cfg(conf, "data_lake_ocid", ""))
        if not lake:
            return DebugLog.embed(fail(
                "data_lake_ocid not found. Add a `data_lake_ocid` key to the "
                "credential bundle (recommended), or set conf.data_lake_ocid.",
                "ValidationError"))

        # Region needs no separate credential key — derive it from the OCID's
        # region-code segment (iad -> us-ashburn-1), then fall back to the
        # runtime's OCI_RESOURCE_PRINCIPAL_REGION / OCI_REGION env vars, then
        # conf. Explicit runtime/bundle region still wins if provided.
        import os
        region = (runtime_params.get("region")
                  or bundle_cfg.get("region")
                  or _region_from_ocid(lake)
                  or os.environ.get("OCI_RESOURCE_PRINCIPAL_REGION")
                  or os.environ.get("OCI_REGION")
                  or get_cfg(conf, "region", "us-ashburn-1"))
        debug(f"resolved region={region} lake={lake[:32]}…")

        runtime_params = dict(runtime_params)
        runtime_params["data_lake_ocid"] = lake

        try:
            if op == "whoami":
                return DebugLog.embed(cls._do_whoami(signer, meta, region, timeout))
            if op in ("list_catalogs", "list_schemas", "list_tables",
                      "list_volumes", "list_kbs"):
                return DebugLog.embed(cls._do_list(
                    op, signer, meta, conf, runtime_params, region, timeout))
            if op == "list_files":
                return DebugLog.embed(cls._do_list_files(
                    signer, meta, conf, runtime_params, region, timeout))
            if op in ("get_kb", "get_volume"):
                return DebugLog.embed(cls._do_get_by_key(
                    op, signer, meta, conf, runtime_params, region, timeout))
            if op == "map":
                return DebugLog.embed(cls._do_map(
                    signer, meta, conf, runtime_params, region, timeout))
            return DebugLog.embed(fail(
                f"Unknown op `{op}`. Valid: whoami | list_catalogs | "
                f"list_schemas | list_tables | list_volumes | list_kbs | "
                f"list_files | get_kb | get_volume | map.", "ValidationError"))
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            body = e.response.text[:500] if e.response is not None else ""
            # Distinguish "signature rejected" from "authenticated but not
            # authorized for THIS resource". 401 = bad credential; 404
            # NotAuthorizedOrNotFound on whoami = the signer authenticated but
            # the user lacks `read users` IAM permission — expected for a
            # locked-down service user. That is NOT a credential failure.
            if op == "whoami" and status == 404 and "NotAuthorizedOrNotFound" in body:
                return DebugLog.embed(ok({
                    "operation": "whoami",
                    "authenticated": True,
                    "note": ("Signer AUTHENTICATED successfully (a bad "
                             "credential returns 401, not 404). This 404 means "
                             "the user lacks `read users` IAM permission to "
                             "read its own record — expected for a service "
                             "user, and not needed for AIDP calls. Run "
                             "op=list_volumes to test the actual data-plane "
                             "call."),
                    "redacted_credential": meta,
                }))
            if op == "whoami" and status == 401:
                return DebugLog.embed(fail(
                    "Signer was REJECTED (HTTP 401 NotAuthenticated). The "
                    "credential is wrong: verify the fingerprint matches the "
                    "private key (openssl rsa -pubout -outform DER -in key.pem "
                    "| openssl md5 -c) and that this API key is still active "
                    "for the user in OCI Console.", "HTTPError",
                    redacted_credential=meta))
            return DebugLog.embed(fail(
                f"HTTP {status} from {e.request.url}: {body}", "HTTPError",
                redacted_credential=meta))
        except Exception as e:
            return DebugLog.embed(fail(str(e), type(e).__name__,
                                       redacted_credential=meta))

    @classmethod
    def _do_whoami(cls, signer, meta, region, timeout):
        # Identity endpoint — confirms the API key / signer combination is
        # valid before chasing data-plane 401s.
        user_id = meta["user"]  # masked; rebuild from signer instead
        user_id = signer.api_key.split("/")[1]  # "<tenancy>/<user>/<fp>"
        url = f"https://identity.{region}.oci.oraclecloud.com/20160918/users/{user_id}"
        r = requests.get(url, auth=signer, timeout=timeout)
        r.raise_for_status()
        body = r.json()
        return ok({
            "operation": "whoami",
            "user": {
                "name": body.get("name"),
                "id": body.get("id"),
                "tenancy_id": body.get("compartmentId"),
                "lifecycle_state": body.get("lifecycleState"),
            },
            "redacted_credential": meta,
        })

    @classmethod
    def _do_list(cls, op, signer, meta, conf, runtime_params, region, timeout):
        """Discovery + test chain against the AIDP data plane. The real AIDP
        API uses FLAT resources with query params (not nested path segments):

            list_catalogs  GET /catalogs                              (lake)
            list_schemas   GET /schemas?catalogKey=..                 (+catalog_key)
            list_tables    GET /tables?catalogKey=..&schemaKey=..     (+schema_key)
            list_volumes   GET /volumes?catalogKey=..&schemaKey=..    (+schema_key)
            list_kbs       GET /knowledgeBases?catalogKey=..&schemaKey=..

        Run them in order to discover the keys the next op needs. Keys are
        opaque strings whose format varies by resource type — a catalog key
        may be a hex id, a schema key is often catalog.schema, a KB key is a
        hex id. Never guess; copy the `key` field from the prior op's output.

        .strip() every value: a leading/trailing space pasted into the Test
        panel becomes %20 in the query and yields a spurious 404.
        """
        from urllib.parse import quote

        lake = (runtime_params.get("data_lake_ocid")
                or get_cfg(conf, "data_lake_ocid", "")).strip()
        catalog = (runtime_params.get("catalog_key")
                   or get_cfg(conf, "catalog_key", "")).strip()
        schema = (runtime_params.get("schema_key")
                  or get_cfg(conf, "schema_key", "")).strip()

        _needs_all = [("data_lake_ocid", lake), ("catalog_key", catalog),
                      ("schema_key", schema)]
        required = {"list_catalogs": [("data_lake_ocid", lake)],
                    "list_schemas":  [("data_lake_ocid", lake),
                                      ("catalog_key", catalog)],
                    "list_tables":   _needs_all,
                    "list_volumes":  _needs_all,
                    "list_kbs":      _needs_all}[op]
        for name, val in required:
            if not val:
                return fail(f"{name} is required for {op}.", "ValidationError")

        api_version = str(get_cfg(conf, "api_version", "20260430")).strip()
        service_path = str(get_cfg(conf, "service_path", "aiDataPlatforms")).strip()
        host = f"https://aidp.{region.strip()}.oci.oraclecloud.com"
        base = f"{host}/{api_version}/{service_path}/{lake}"
        cat_q = quote(catalog, safe="")
        sch_q = quote(schema, safe="")
        # /knowledgeBases is served on 20240831/dataLakes, not the newer surface.
        if op == "list_kbs":
            base = f"{host}/20240831/dataLakes/{lake}"
        path = {
            "list_catalogs": "/catalogs",
            "list_schemas":  f"/schemas?catalogKey={cat_q}",
            "list_tables":   f"/tables?catalogKey={cat_q}&schemaKey={sch_q}",
            "list_volumes":  f"/volumes?catalogKey={cat_q}&schemaKey={sch_q}",
            "list_kbs":      f"/knowledgeBases?catalogKey={cat_q}&schemaKey={sch_q}",
        }[op]
        url = base + path
        debug(f"GET {url}")

        try:
            r = requests.get(url, auth=signer, timeout=timeout,
                             headers={"Accept": "application/json"})
            r.raise_for_status()
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            body = e.response.text[:300] if e.response is not None else ""
            hint = ""
            if status in (404, 500):
                hint = (" — catalog_key must be the catalog NAME (e.g. "
                        "construction_catalog), the same name that prefixes the "
                        "dotted schema/volume keys — NOT the hex 'Key' shown on "
                        "the catalog's Details page. (Confirmed from the AIDP "
                        "console's own calls: GET /schemas?catalogKey="
                        "construction_catalog.) A 500 'checking the sourceType "
                        "of Catalog' means you passed the hex key here.")
            return fail(f"HTTP {status} from {url}: {body}{hint}", "HTTPError",
                        redacted_credential=meta)

        body = r.json()
        items = body.get("items", body if isinstance(body, list) else [])
        return ok({
            "operation": op,
            "url": url,
            "count": len(items),
            "items": [
                {"key": it.get("key"),
                 "displayName": it.get("displayName"),
                 "type": it.get("catalogType") or it.get("type"),
                 "lifecycleState": it.get("lifecycleState")}
                for it in items
            ],
            "next": {
                "list_catalogs": "copy a catalog `key` into catalog_key, then "
                                 "run op=list_schemas",
                "list_schemas":  "copy a schema `key` into schema_key, then run "
                                 "op=list_tables / list_volumes / list_kbs",
                "list_tables":   "done — these are your tables",
                "list_volumes":  "copy a volume `key` into volume_key, then "
                                 "run op=list_files to browse its contents",
                "list_kbs":      "done — these are your knowledge bases",
            }[op],
            "note": ("count=0 means the call succeeded but nothing matched. "
                     "Most common cause: catalog_key is the hex 'Key' from the "
                     "console. The catalogKey parameter wants the catalog NAME "
                     "(e.g. construction_catalog) — the same name that prefixes "
                     "the dotted schema/volume keys. Retry with the name."
                     if not items else ""),
            "redacted_credential": meta,
        })

    @classmethod
    def _do_list_files(cls, signer, meta, conf, runtime_params, region, timeout):
        """List files/folders inside a volume — the level below list_volumes.

            GET /volumes/{volumeKey}/files?path=/

        The volume key is the full dotted path catalog.schema.volume, e.g.
        construction_catalog.construction_schema.construction_documents.
        `path` defaults to '/' (the volume root); pass a subfolder to descend.
        """
        from urllib.parse import quote

        volume_key = (runtime_params.get("volume_key")
                      or get_cfg(conf, "volume_key", "")).strip()
        path = (runtime_params.get("path") or "/").strip() or "/"
        if not volume_key:
            return fail("volume_key is required for list_files (the full "
                        "catalog.schema.volume key from op=list_volumes).",
                        "ValidationError")

        lake = (runtime_params.get("data_lake_ocid")
                or get_cfg(conf, "data_lake_ocid", "")).strip()
        api_version = str(get_cfg(conf, "api_version", "20260430")).strip()
        service_path = str(get_cfg(conf, "service_path", "aiDataPlatforms")).strip()
        url = (f"https://aidp.{region.strip()}.oci.oraclecloud.com/"
               f"{api_version}/{service_path}/{lake}/volumes/"
               f"{quote(volume_key, safe='')}/files?path={quote(path, safe='')}")
        debug(f"GET {url}")

        try:
            r = requests.get(url, auth=signer, timeout=timeout,
                             headers={"Accept": "application/json"})
            r.raise_for_status()
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            body = e.response.text[:300] if e.response is not None else ""
            hint = (" — confirm volume_key is the exact `key` from "
                    "op=list_volumes (catalog.schema.volume).") if status == 404 else ""
            return fail(f"HTTP {status} from {url}: {body}{hint}", "HTTPError",
                        redacted_credential=meta)

        body = r.json()
        items = body.get("items", body if isinstance(body, list) else [])
        return ok({
            "operation": "list_files",
            "url": url,
            "volume_key": volume_key,
            "path": path,
            "count": len(items),
            "items": [
                {"name": it.get("name") or it.get("displayName"),
                 "path": it.get("path"),
                 "type": (it.get("type") or it.get("objectType") or "file")}
                for it in items
            ],
            "redacted_credential": meta,
        })

    @classmethod
    def _do_get_by_key(cls, op, signer, meta, conf, runtime_params, region, timeout):
        """Fetch one resource DIRECTLY by its own key — no catalogKey/schemaKey
        filter. This is the pattern that works reliably (same as list_files):

            get_kb      GET /knowledgeBases/{kbKey}     (kb_key,     hex)
            get_volume  GET /volumes/{volumeKey}        (volume_key, catalog.schema.volume)

        Prefer these over the filtered list_* ops when you already have the
        resource's key — they don't depend on getting catalogKey right.
        """
        from urllib.parse import quote

        if op == "get_kb":
            key = (runtime_params.get("kb_key") or get_cfg(conf, "kb_key", "")).strip()
            key_name, resource = "kb_key", "knowledgeBases"
        else:  # get_volume
            key = (runtime_params.get("volume_key")
                   or get_cfg(conf, "volume_key", "")).strip()
            key_name, resource = "volume_key", "volumes"
        if not key:
            return fail(f"{key_name} is required for {op}.", "ValidationError")

        lake = (runtime_params.get("data_lake_ocid")
                or get_cfg(conf, "data_lake_ocid", "")).strip()
        api_version = str(get_cfg(conf, "api_version", "20260430")).strip()
        service_path = str(get_cfg(conf, "service_path", "aiDataPlatforms")).strip()
        host = f"https://aidp.{region.strip()}.oci.oraclecloud.com"
        # /knowledgeBases is served on 20240831/dataLakes, not the newer surface.
        if resource == "knowledgeBases":
            api_version, service_path = "20240831", "dataLakes"
        url = (f"{host}/{api_version}/{service_path}/{lake}/{resource}/"
               f"{quote(key, safe='')}")
        debug(f"GET {url}")

        try:
            r = requests.get(url, auth=signer, timeout=timeout,
                             headers={"Accept": "application/json"})
            r.raise_for_status()
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            body = e.response.text[:300] if e.response is not None else ""
            hint = (f" — confirm {key_name} is the exact `key` for this "
                    f"resource.") if status == 404 else ""
            return fail(f"HTTP {status} from {url}: {body}{hint}", "HTTPError",
                        redacted_credential=meta)

        obj = r.json()
        return ok({
            "operation": op,
            "url": url,
            "resource": {
                "key": obj.get("key"),
                "displayName": obj.get("displayName"),
                "description": obj.get("description"),
                "lifecycleState": obj.get("lifecycleState"),
                "catalogKey": obj.get("catalogKey"),
                "schemaKey": obj.get("schemaKey"),
            },
            "redacted_credential": meta,
        })

    @classmethod
    def _do_map(cls, signer, meta, conf, runtime_params, region, timeout):
        """Walk the whole Master catalog and fill in everything in one call:
        catalogs -> schemas -> (tables, volumes, knowledge bases). Returns a
        nested tree with every real key, so no manual list-and-copy chain.

        Scope: pass catalog_key (a catalog NAME) to map just that catalog;
        omit it to map all catalogs in the data lake. Each GET is isolated —
        a failure on one branch records an error there and keeps going.
        """
        from urllib.parse import quote

        lake = (runtime_params.get("data_lake_ocid")
                or get_cfg(conf, "data_lake_ocid", "")).strip()
        only_catalog = (runtime_params.get("catalog_key")
                        or get_cfg(conf, "catalog_key", "")).strip()
        api_version = str(get_cfg(conf, "api_version", "20260430")).strip()
        service_path = str(get_cfg(conf, "service_path", "aiDataPlatforms")).strip()
        host = f"https://aidp.{region.strip()}.oci.oraclecloud.com"
        base = f"{host}/{api_version}/{service_path}/{lake}"
        # The /knowledgeBases endpoint is not served on 20260430/aiDataPlatforms
        # (404) but works on the console's 20240831/dataLakes surface. Use that
        # for KBs only; catalogs/schemas/tables/volumes stay on the configured
        # surface where they work.
        kb_base = f"{host}/20240831/dataLakes/{lake}"

        def _get(url):
            try:
                r = requests.get(url, auth=signer, timeout=timeout,
                                 headers={"Accept": "application/json"})
                r.raise_for_status()
                body = r.json()
                return body.get("items", body if isinstance(body, list) else []), None
            except requests.HTTPError as e:
                st = e.response.status_code if e.response is not None else "?"
                return [], f"HTTP {st}"
            except Exception as e:
                return [], f"{type(e).__name__}"

        def get_items(path):
            return _get(base + path)

        def get_kbs(cat_q, schema_dotted, schema_plain):
            # Try surface × schemaKey combos; return the first that works.
            for b, sk in ((kb_base, schema_plain), (kb_base, schema_dotted),
                          (base, schema_plain), (base, schema_dotted)):
                items, err = _get(
                    f"{b}/knowledgeBases?catalogKey={cat_q}"
                    f"&schemaKey={quote(sk, safe='')}")
                if err is None:
                    return items, None
            return [], "HTTP 404 (all surface/schemaKey combos)"

        catalogs, cat_err = get_items("/catalogs")
        if cat_err:
            return fail(f"list catalogs failed: {cat_err}", "HTTPError",
                        redacted_credential=meta)

        counts = {"catalogs": 0, "schemas": 0, "tables": 0,
                  "volumes": 0, "knowledge_bases": 0}
        tree = []
        for c in catalogs:
            c_name = c.get("key") or c.get("displayName")
            if only_catalog and c_name != only_catalog \
                    and c.get("displayName") != only_catalog:
                continue
            counts["catalogs"] += 1
            c_q = quote(c_name, safe="")
            schemas, s_err = get_items(f"/schemas?catalogKey={c_q}")
            c_node = {"catalog_key": c_name,
                      "displayName": c.get("displayName"),
                      "type": c.get("catalogType") or c.get("type"),
                      "schemas": [], "error": s_err}
            for s in schemas:
                counts["schemas"] += 1
                # The schemaKey filter wants the schema's own `key` — the
                # DOTTED catalog.schema form (not the plain displayName; that
                # returns HTTP 400/404). Same rule as catalogKey = catalog key.
                s_key = s.get("key") or s.get("displayName")   # dotted
                s_plain = s.get("displayName") or s_key.split(".")[-1]
                s_q = quote(s_key, safe="")
                tbls, t_err = get_items(
                    f"/tables?catalogKey={c_q}&schemaKey={s_q}")
                vols, v_err = get_items(
                    f"/volumes?catalogKey={c_q}&schemaKey={s_q}")
                kbs, k_err = get_kbs(c_q, s_key, s_plain)
                counts["tables"] += len(tbls)
                counts["volumes"] += len(vols)
                counts["knowledge_bases"] += len(kbs)
                c_node["schemas"].append({
                    "schema_key": s.get("key"),      # dotted catalog.schema (used as schemaKey)
                    "schema_name": s.get("displayName"),
                    "tables": [{"key": t.get("key"),
                                "displayName": t.get("displayName")} for t in tbls],
                    "volumes": [{"key": v.get("key"),
                                 "displayName": v.get("displayName")} for v in vols],
                    "knowledge_bases": [{"key": k.get("key"),
                                         "displayName": k.get("displayName")}
                                        for k in kbs],
                    "errors": {k: e for k, e in
                               (("tables", t_err), ("volumes", v_err), ("kbs", k_err))
                               if e},
                })
            tree.append(c_node)

        return ok({
            "operation": "map",
            "scope": only_catalog or "(all catalogs)",
            "summary": counts,
            "text": _render_map_text(tree, counts),
            "catalogs": tree,
            "redacted_credential": meta,
        })
