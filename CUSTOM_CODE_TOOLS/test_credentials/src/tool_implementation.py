"""Test Credentials — is this AIDP Credential Store secret good?

A focused diagnostic: point it at a credential (display name or OCI Vault
secret OCID) and it runs the full chain and reports pass/fail on each step:

    1. resolves        — the secret exists and returned a dict
    2. required keys   — tenancy / user / fingerprint / private_key present
    3. fingerprint     — looks like a valid 47-char OCI fingerprint
    4. data_lake_ocid  — present (so the tool knows which lake to hit)
    5. signer built    — oci.signer.Signer constructed from the PEM
    6. authenticated   — GET /catalogs succeeds (signature accepted by AIDP)

Nothing is written; nothing sensitive is returned (all values masked).

credential_name is a runtime input here on purpose — the whole point is to
test an arbitrary secret. It also falls back to conf.credential_name.
"""

from __future__ import annotations

from typing import Any, Dict

import requests

from aidputils.agents.tools.custom_tools.base import CustomToolBase

from .utils.config_utils import get_cfg, ok, fail

try:
    from aidp_debug import debug, debug_error, DebugLog
except ImportError:
    def debug(*a, **k): pass
    def debug_error(*a, **k): pass
    class DebugLog:
        @staticmethod
        def embed(r): return r


@CustomToolBase.register
class TestCredentialsTool(CustomToolBase):
    """Validate an AIDP Credential Store secret end to end and report which
    checks pass. Use this before wiring the credential into other tools."""

    @classmethod
    def _execute_tool(cls, conf: Dict[str, Any], runtime_params: Dict[str, Any],
                      **context_vars) -> Dict[str, Any]:
        cred = (runtime_params.get("credential_name")
                or get_cfg(conf, "credential_name", "")).strip()
        debug(f"TestCredentialsTool credential_name={cred!r}")
        checks: Dict[str, Any] = {"credential_name": cred}

        if not cred:
            return DebugLog.embed(fail(
                "Provide credential_name (a Credential Store display name or an "
                "OCI Vault secret OCID) as a runtime param, or set "
                "conf.credential_name.", "ValidationError", checks=checks))

        try:
            from .utils.credential_resolver import (
                resolve_bundle, build_oci_signer_from_bundle, resolve_region, mask)
        except ImportError as ex:
            return DebugLog.embed(fail(
                f"credential_resolver not bundled: {ex}", "ConfigError",
                checks=checks))

        # 1. resolve
        bundle, err = resolve_bundle(cred)
        checks["resolved"] = bool(bundle) and not err
        if err or not bundle:
            checks["verdict"] = "FAIL — credential did not resolve"
            return DebugLog.embed(fail(err or "empty bundle",
                                       "CredentialStoreError", checks=checks))

        # 2. required keys + 4. data_lake_ocid present
        required = ("tenancy", "user", "fingerprint", "private_key")
        missing = [k for k in required if not bundle.get(k)]
        checks["required_oci_keys_present"] = not missing
        if missing:
            checks["missing_keys"] = missing
        lake = str(bundle.get("data_lake_ocid") or bundle.get("datalake_ocid")
                   or bundle.get("lake_ocid") or get_cfg(conf, "data_lake_ocid", "")).strip()
        checks["data_lake_ocid_present"] = bool(lake)

        # 3. + 5. fingerprint format + signer build
        try:
            signer, meta = build_oci_signer_from_bundle(bundle)
            checks["fingerprint_valid"] = True
            checks["signer_built"] = True
        except Exception as e:
            checks["fingerprint_valid"] = "fingerprint" not in str(e).lower()
            checks["signer_built"] = False
            checks["verdict"] = f"FAIL — {e}"
            return DebugLog.embed(fail(str(e), "CredentialStoreError", checks=checks))

        # 6. authenticate — the simplest data-plane GET
        if not lake:
            checks["authenticated"] = "skipped (no data_lake_ocid to test against)"
            checks["verdict"] = ("PARTIAL — signer built, but add data_lake_ocid "
                                 "to the credential to verify AIDP access")
            return DebugLog.embed(ok({**checks, "redacted_credential": meta}))

        region = resolve_region(ocid=lake, conf_region=get_cfg(conf, "region", ""))
        api_version = str(get_cfg(conf, "api_version", "20260430")).strip()
        service_path = str(get_cfg(conf, "service_path", "aiDataPlatforms")).strip()
        timeout = get_cfg(conf, "timeout", 20)
        url = (f"https://aidp.{region}.oci.oraclecloud.com/"
               f"{api_version}/{service_path}/{lake}/catalogs")
        debug(f"auth check GET {url}")
        try:
            r = requests.get(url, auth=signer, timeout=timeout,
                             headers={"Accept": "application/json"})
            r.raise_for_status()
            body = r.json()
            items = body.get("items", body if isinstance(body, list) else [])
            checks["authenticated"] = True
            checks["region"] = region
            checks["catalogs_visible"] = len(items)
            checks["verdict"] = "PASS — credential is good"
            return DebugLog.embed(ok({**checks, "redacted_credential": meta}))
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            body = e.response.text[:300] if e.response is not None else ""
            checks["authenticated"] = False
            checks["http_status"] = status
            if status == 401:
                checks["verdict"] = ("FAIL — HTTP 401: signature rejected. The "
                                     "fingerprint doesn't match the private key, "
                                     "or the API key was deleted for this user.")
            else:
                checks["verdict"] = f"FAIL — HTTP {status}: {body}"
            return DebugLog.embed(fail(checks["verdict"], "HTTPError", checks=checks,
                                       redacted_credential=meta))
        except Exception as e:
            checks["authenticated"] = False
            checks["verdict"] = f"FAIL — {type(e).__name__}: {e}"
            return DebugLog.embed(fail(str(e), type(e).__name__, checks=checks))
