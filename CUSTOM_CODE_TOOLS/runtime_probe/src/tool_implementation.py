"""RuntimeProbe — introspect what's actually available in the AIDP runtime.

Use this when a tool reports "aidputils.secrets not available" or similar
import errors. Probes for the modules/packages we depend on for credential
resolution, OCI signing, and database connectivity, and reports versions
so you can compare to what your code expects.

Run it from the AIDP Test panel — no inputs needed. The result is a single
ok envelope with a `runtime` dict you can copy/paste into a ticket.
"""

from __future__ import annotations

import importlib
import os
import platform
import sys
from typing import Any, Dict

from aidputils.agents.tools.custom_tools.base import CustomToolBase

try:
    from aidp_debug import debug, DebugLog
except ImportError:
    def debug(*a, **k): pass
    class DebugLog:
        @staticmethod
        def embed(r): return r


# Modules the credential-store wiring depends on, in priority order.
# Each tuple is (import_path, optional_submodule_to_probe, hint).
TARGETS = [
    ("aidputils",                  None,                 "AIDP shared utilities package."),
    ("aidputils.secrets",          "get",                "REQUIRED for the recommended credential-store path."),
    ("aidputils.agents.tools.custom_tools.base", "CustomToolBase",
     "Required for every custom tool."),
    ("aidputils.agents.auth.signer.custom_remote_signer", "CustomRemoteSigner",
     "Optional. Lakeproxy-delegated signing — alternative to credential store."),
    ("datahub_dp_python_client",   None,
     "Underlying SDK aidputils.secrets uses. If aidputils.secrets is missing "
     "but this is present, a direct fallback might be possible."),
    ("datahub_dp_python_client.datahub_dp.credentials_client", "CredentialsClient",
     "The class aidputils.secrets calls under the hood."),
    ("oci",                        None,                 "OCI Python SDK."),
    ("oci.auth.signers",           "get_resource_principals_signer",
     "Resource principal signer factory."),
    ("oci.signer",                 "Signer",             "API-key signer."),
    ("oci.object_storage",         None,                 "For object_storage_tool."),
    ("oci.generative_ai_inference", None,                "For genai_toolkit."),
    ("oracledb",                   None,                 "For selectai_toolkit DB connections."),
    ("requests",                   None,                 "HTTP client every tool uses."),
]


# Environment variables that the credential-store path or signers look at.
ENV_KEYS = [
    "OCI_RESOURCE_PRINCIPAL_VERSION",   # set on AIDP compute
    "OCI_RESOURCE_PRINCIPAL_RPST",
    "OCI_RESOURCE_PRINCIPAL_PRIVATE_PEM",
    "OCI_RESOURCE_PRINCIPAL_REGION",
    "OCI_HUB_DP_ENDPOINT",              # lakeproxy endpoint for credential REST
    "DATALAKE_ID",                      # required by aidputils.secrets
    "OCI_REGION",
]


def _probe_module(import_path: str, attr: str | None) -> Dict[str, Any]:
    result: Dict[str, Any] = {"available": False}
    try:
        mod = importlib.import_module(import_path)
        result["available"] = True
        result["module_file"] = getattr(mod, "__file__", "(no __file__)") or ""
        ver = getattr(mod, "__version__", None)
        if ver:
            result["version"] = ver
        if attr:
            if hasattr(mod, attr):
                result["has_attr"] = attr
            else:
                result["available"] = False
                result["error"] = f"module loaded but missing attribute `{attr}`"
    except ImportError as ex:
        result["error"] = f"ImportError: {ex}"
    except Exception as ex:
        result["error"] = f"{type(ex).__name__}: {ex}"
    return result


def _can_get_resource_principal_signer() -> Dict[str, Any]:
    """Try to actually construct a resource-principal signer — proves the
    runtime is on compute and has working RP claims, not just the SDK."""
    try:
        import oci
        signer = oci.auth.signers.get_resource_principals_signer()
        return {"ok": True,
                "claim_tenancy": getattr(signer, "tenancy_id", "?")[:18] + "…"
                                 if getattr(signer, "tenancy_id", None) else None}
    except Exception as ex:
        return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}


def _can_aidputils_secrets_get(probe_name: str) -> Dict[str, Any]:
    """If aidputils.secrets is available, try a real .get() call (with a
    name we expect to fail) so we can distinguish "module missing" from
    "credential missing" from "runtime config missing"."""
    try:
        import aidputils.secrets as secrets
    except Exception as ex:
        return {"reached_secrets_module": False, "error": str(ex)}
    try:
        secrets.get(probe_name)
        return {"reached_secrets_module": True, "lookup": "succeeded — "
                "credential exists with that name (unexpected for a probe)"}
    except Exception as ex:
        return {"reached_secrets_module": True,
                "lookup_failed_with": f"{type(ex).__name__}: {ex}"}


@CustomToolBase.register
class RuntimeProbe(CustomToolBase):
    """Reports what's installed in this AIDP runtime so credential-store
    wiring issues can be diagnosed without guessing."""

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        debug("RuntimeProbe._execute_tool start")
        probe_name = (runtime_params or {}).get("probe_credential_name",
                                                "__runtime_probe_does_not_exist__")

        modules: Dict[str, Any] = {}
        for path, attr, hint in TARGETS:
            modules[path] = {**_probe_module(path, attr), "hint": hint}

        env = {k: ("(set)" if os.environ.get(k) else "(empty)")
               for k in ENV_KEYS}

        result = {
            "python": {
                "version": sys.version.split()[0],
                "platform": platform.platform(),
                "executable": sys.executable,
            },
            "modules": modules,
            "env": env,
            "live_probes": {
                "resource_principal_signer": _can_get_resource_principal_signer(),
                "aidputils_secrets_get": _can_aidputils_secrets_get(probe_name),
            },
            "summary": _summarize(modules),
        }

        return DebugLog.embed({"ok": True, "data": result})


def _summarize(modules: Dict[str, Any]) -> Dict[str, Any]:
    sec = modules.get("aidputils.secrets", {})
    dh = modules.get("datahub_dp_python_client.datahub_dp.credentials_client", {})
    out = {}
    if sec.get("available"):
        out["credential_store_via_aidputils"] = "READY"
    elif dh.get("available"):
        out["credential_store_via_aidputils"] = "NOT READY (but the underlying " \
            "CredentialsClient is present — a direct fallback could work)"
    else:
        out["credential_store_via_aidputils"] = "NOT READY (neither aidputils." \
            "secrets nor datahub_dp_python_client is in this runtime)"
    if modules.get("oci.auth.signers", {}).get("available"):
        out["resource_principal_signer"] = "READY"
    else:
        out["resource_principal_signer"] = "NOT READY"
    return out
