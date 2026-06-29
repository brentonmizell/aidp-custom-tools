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


def _mask(value: Optional[str], keep: int = 4) -> str:
    """Truncate a secret for debug output. Never log full tokens / keys."""
    if not value:
        return "<empty>"
    if len(value) <= keep * 2:
        return f"<{len(value)} chars>"
    return f"{value[:keep]}…{value[-keep:]}  ({len(value)} chars)"


def _build_signer(credential_name: str) -> tuple:
    """Resolve the credential by display name and return (signer, redacted_meta).
    Returns (None, error_msg) if anything is missing."""
    try:
        import aidputils.secrets as secrets
    except ImportError as ex:
        return None, f"aidputils.secrets not available: {ex}"

    try:
        # One bulk fetch + key validation gives a better error than four
        # silent get(name, key) calls if the credential is misconfigured.
        bundle = secrets.get(credential_name)
    except Exception as ex:
        return None, f"Credential `{credential_name}` could not be read: {ex}"

    if not isinstance(bundle, dict):
        return None, (f"Credential `{credential_name}` did not return a dict "
                      f"(got {type(bundle).__name__}). The credential must be "
                      f"SECRET_TOKEN type, not SERVICE_ACCOUNT or VAULT_REFERENCE.")

    missing = [k for k in REQUIRED_SECRET_KEYS if not bundle.get(k)]
    if missing:
        return None, (f"Credential `{credential_name}` is missing secret keys: "
                      f"{missing}. Required: {list(REQUIRED_SECRET_KEYS)}.")

    import oci
    signer = oci.signer.Signer(
        tenancy=bundle["tenancy"],
        user=bundle["user"],
        fingerprint=bundle["fingerprint"],
        private_key_content=bundle["private_key"],
    )
    redacted = {
        "tenancy":     _mask(bundle["tenancy"], 6),
        "user":        _mask(bundle["user"], 6),
        "fingerprint": _mask(bundle["fingerprint"], 2),
        "private_key": _mask(bundle["private_key"], 12),
    }
    return signer, redacted


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
        op = (runtime_params.get("op") or "whoami").lower()
        credential_name = (runtime_params.get("credential_name")
                           or get_cfg(conf, "credential_name", ""))
        timeout = get_cfg(conf, "timeout", 30)
        region = get_cfg(conf, "region", "us-ashburn-1")

        debug(f"CredentialStoreAuthSample op={op} credential_name={credential_name!r}")

        if not credential_name:
            return DebugLog.embed(fail(
                "credential_name is required — pass it as a runtime param or "
                "set conf.credential_name.", "ValidationError"))

        signer, meta = _build_signer(credential_name)
        if signer is None:
            return DebugLog.embed(fail(meta, "CredentialStoreError"))
        debug(f"Signer constructed. Redacted credential meta: {meta}")

        try:
            if op == "whoami":
                return DebugLog.embed(cls._do_whoami(signer, meta, region, timeout))
            if op == "list_volumes":
                return DebugLog.embed(cls._do_list_volumes(
                    signer, meta, conf, runtime_params, region, timeout))
            return DebugLog.embed(fail(
                f"Unknown op `{op}`. Valid: whoami | list_volumes.",
                "ValidationError"))
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            body = e.response.text[:500] if e.response is not None else ""
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
    def _do_list_volumes(cls, signer, meta, conf, runtime_params, region, timeout):
        lake = (runtime_params.get("data_lake_ocid")
                or get_cfg(conf, "data_lake_ocid", ""))
        catalog = (runtime_params.get("catalog_key")
                   or get_cfg(conf, "catalog_key", ""))
        schema = (runtime_params.get("schema_key")
                  or get_cfg(conf, "schema_key", ""))
        for name, val in (("data_lake_ocid", lake), ("catalog_key", catalog),
                          ("schema_key", schema)):
            if not val:
                return fail(f"{name} is required for list_volumes.",
                            "ValidationError")
        api_version = get_cfg(conf, "api_version", "20260430")
        service_path = get_cfg(conf, "service_path", "aiDataPlatforms")
        url = (f"https://aidp.{region}.oci.oraclecloud.com/{api_version}/"
               f"{service_path}/{lake}/catalogs/{catalog}/schemas/{schema}/volumes")
        debug(f"GET {url}")
        r = requests.get(url, auth=signer, timeout=timeout)
        r.raise_for_status()
        body = r.json()
        items = body.get("items", body if isinstance(body, list) else [])
        return ok({
            "operation": "list_volumes",
            "url": url,
            "count": len(items),
            "volumes": [
                {"key": v.get("key"), "displayName": v.get("displayName"),
                 "lifecycleState": v.get("lifecycleState")}
                for v in items
            ],
            "redacted_credential": meta,
        })
