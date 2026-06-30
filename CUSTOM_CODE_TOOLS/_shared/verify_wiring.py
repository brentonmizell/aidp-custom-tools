"""Cross-toolkit verification — confirm every wired tool consumes
conf.credential_name through the shared resolver. Mocks aidputils.secrets
and (where needed) the network so this runs on any dev box.

Run:  python CUSTOM_CODE_TOOLS/_shared/verify_wiring.py
Exit: 0 on full pass, nonzero on first failure.
"""

from __future__ import annotations

import sys
import types
import importlib
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent  # CUSTOM_CODE_TOOLS/

GOOD_BUNDLE_OCI = {
    "tenancy":     "ocid1.tenancy.oc1..aaaaaaaatestaaaaa",
    "user":        "ocid1.user.oc1..bbbbbbbbtestbbbbb",
    "fingerprint": "aa:bb:cc:dd:ee:ff:11:22:33:44:55:66:77:88:99:00",
    "private_key": "-----BEGIN PRIVATE KEY-----\nMOCKBODY\n-----END PRIVATE KEY-----",
}

GOOD_BUNDLE_SMTP = {
    "host": "smtp.example.com", "port": "587",
    "username": "u@example.com", "password": "p4ssw0rd",
    "from_address": "from@example.com",
}

GOOD_BUNDLE_DB = {
    "username": "ADMIN", "password": "dbpass",
    "connection_string": "mydb_high",
    "wallet_b64": "",  # empty for the smoke test
}

GOOD_BUNDLE_WEBHOOK = {
    "webhook_url": "https://hooks.slack.com/services/T00/B00/XXX",
}


def _install_global_stubs():
    """Stub aidputils, oci, and other heavyweight deps so any tool can import."""
    secrets_mod = types.ModuleType("aidputils.secrets")
    secrets_mod._bundles = {}
    def fake_get(name, key=None):
        b = secrets_mod._bundles.get(name)
        if b is None:
            raise KeyError(f"no credential {name!r}")
        return b if key is None else b.get(key)
    secrets_mod.get = fake_get

    sys.modules["aidputils"] = types.ModuleType("aidputils")
    sys.modules["aidputils.secrets"] = secrets_mod

    # CustomToolBase + register
    for path in ("aidputils.agents", "aidputils.agents.tools",
                 "aidputils.agents.tools.custom_tools"):
        sys.modules[path] = types.ModuleType(path)
    base_mod = types.ModuleType("aidputils.agents.tools.custom_tools.base")
    class _Base:
        @classmethod
        def register(cls, k): return k
        # _make_http_request is the framework-provided HTTP helper. Stub it
        # so tools that use it can be exercised under the harness. Default
        # returns a 200 OK; per-test code can patch this.
        _captured_http_calls = []
        @classmethod
        def _make_http_request(cls, **kwargs):
            cls._captured_http_calls.append(kwargs)
            resp = mock.MagicMock(status_code=200, text="ok",
                                  headers={"Content-Type": "text/plain"})
            resp.read.return_value = b"ok"
            return resp
    base_mod.CustomToolBase = _Base
    sys.modules["aidputils.agents.tools.custom_tools.base"] = base_mod

    # OCI signer stub
    oci_mod = types.ModuleType("oci")
    oci_signer_mod = types.ModuleType("oci.signer")
    class FakeSigner:
        def __init__(self, **kw): self.kw = kw
        def __call__(self, req): return req
    oci_signer_mod.Signer = FakeSigner
    oci_auth_mod = types.ModuleType("oci.auth")
    oci_auth_signers_mod = types.ModuleType("oci.auth.signers")
    class _RP:
        kind = "rp"
        def __call__(self, req): return req
    oci_auth_signers_mod.get_resource_principals_signer = lambda: _RP()
    oci_auth_signers_mod.InstancePrincipalsSecurityTokenSigner = type("IP", (), {})
    oci_auth_mod.signers = oci_auth_signers_mod
    oci_mod.signer = oci_signer_mod
    oci_mod.auth = oci_auth_mod
    sys.modules["oci"] = oci_mod
    sys.modules["oci.signer"] = oci_signer_mod
    sys.modules["oci.auth"] = oci_auth_mod
    sys.modules["oci.auth.signers"] = oci_auth_signers_mod
    return secrets_mod


def _load_tool(tool_dir_name, module_subpath):
    """Import a tool's module as src.<subpath> so relative imports work.

    Clears every other tool root from sys.path first so Python can't
    pick up a stale `src` package from a previously-loaded tool.
    """
    tool_dir = ROOT / tool_dir_name
    # Drop every tool root we might have added in a prior scenario.
    sys.path[:] = [p for p in sys.path
                   if not p.startswith(str(ROOT)) or p == str(ROOT)]
    sys.path.insert(0, str(tool_dir))
    # Drop ALL src.* modules so the next import resolves against this tool.
    for mod in list(sys.modules):
        if mod == "src" or mod.startswith("src."):
            del sys.modules[mod]
    importlib.invalidate_caches()
    importlib.import_module("src")
    return importlib.import_module(f"src.{module_subpath}")


def check(name, condition, detail=""):
    mark = "PASS" if condition else "FAIL"
    line = f"  {mark}  {name}"
    if detail:
        line += f"  —  {detail}"
    print(line)
    return condition


def scenario_aidp_catalog(secrets_mod):
    print("\n[aidp_catalog_toolkit]")
    secrets_mod._bundles = {"oci_key": GOOD_BUNDLE_OCI}
    sys.path = [p for p in sys.path if "aidp_catalog_toolkit" not in p]
    mod = _load_tool("aidp_catalog_toolkit", "tool_implementation")

    signer = mod._build_signer({"conf": {"credential_name": "oci_key"}})
    ok1 = check("credential_name -> Credential Store signer",
                signer.__class__.__name__ == "FakeSigner",
                f"got {type(signer).__name__}")
    signer = mod._build_signer({"conf": {}})
    ok2 = check("empty credential_name -> resource principal fallback",
                signer.__class__.__name__ == "_RP",
                f"got {type(signer).__name__}")
    try:
        mod._build_signer({"conf": {"credential_name": "missing"}})
        ok3 = check("missing credential -> ValueError", False, "did not raise")
    except ValueError as e:
        ok3 = check("missing credential -> ValueError", "missing" in str(e),
                    f"error: {str(e)[:60]}")
    return all((ok1, ok2, ok3))


def scenario_object_storage(secrets_mod):
    print("\n[object_storage_tool]")
    secrets_mod._bundles = {"oci_key": GOOD_BUNDLE_OCI}
    sys.path = [p for p in sys.path if "object_storage_tool" not in p]
    mod = _load_tool("object_storage_tool", "tool_implementation")

    # Just test the credential_resolver pathway in isolation — full _execute_tool
    # path also depends on oci.object_storage which we haven't stubbed.
    from src.utils.credential_resolver import resolve_oci_signer
    s, m, e = resolve_oci_signer("oci_key")
    ok1 = check("credential_resolver imports + resolves bundle",
                s is not None and not e, f"meta={m.get('user', '?')[:20]}")
    s, m, e = resolve_oci_signer("")
    ok2 = check("empty credential_name -> (None, {}, None) fall-through",
                s is None and not e)
    return ok1 and ok2


def scenario_genai(secrets_mod):
    print("\n[genai_toolkit]")
    secrets_mod._bundles = {"oci_key": GOOD_BUNDLE_OCI}
    sys.path = [p for p in sys.path if "genai_toolkit" not in p]
    # Just verify the wired function imports + the path branch exists by reading
    # the edited file (the full build_oci_genai_client requires oci.generative_ai
    # which we don't stub).
    src_file = (ROOT / "genai_toolkit" / "src" / "utils" / "llm_utils.py").read_text()
    ok1 = check("build_oci_genai_client has Credential Store branch",
                "from .credential_resolver import resolve_oci_signer" in src_file)
    ok2 = check("falls through to legacy auth_type paths when no credential",
                'auth_type = (resolved.get("auth_type") or "resource_principal")' in src_file)
    return ok1 and ok2


def scenario_email(secrets_mod):
    print("\n[email_toolkit]")
    secrets_mod._bundles = {"smtp_creds": GOOD_BUNDLE_SMTP}
    sys.path = [p for p in sys.path if "email_toolkit" not in p]
    mod = _load_tool("email_toolkit", "tool_implementation")

    creds = mod._resolve_smtp_creds_from_store(
        {"conf": {"credential_name": "smtp_creds"}})
    ok1 = check("SMTP creds resolved from store",
                creds.get("host") == GOOD_BUNDLE_SMTP["host"]
                and creds.get("password") == GOOD_BUNDLE_SMTP["password"])
    creds = mod._resolve_smtp_creds_from_store({"conf": {}})
    ok2 = check("empty credential_name -> {} fall-through",
                creds == {})
    return ok1 and ok2


def scenario_selectai(secrets_mod):
    print("\n[selectai_toolkit]")
    secrets_mod._bundles = {"db_creds": GOOD_BUNDLE_DB}
    sys.path = [p for p in sys.path if "selectai_toolkit" not in p]
    # selectai needs oracledb. Stub it.
    oracledb_mod = types.ModuleType("oracledb")
    class FakeConn:
        def __init__(self, **kw): self.kw = kw
    oracledb_mod.connect = lambda **kw: FakeConn(**kw)
    sys.modules["oracledb"] = oracledb_mod
    mod = _load_tool("selectai_toolkit", "tool_implementation")

    conn = mod._open_connection(
        catalog_key="",
        conf={"conf": {"aidp_credential_name": "db_creds"}},
        runtime_params={},
        context_vars={},
    )
    ok1 = check("aidp_credential_name -> oracledb.connect with bundle creds",
                conn.kw.get("user") == "ADMIN" and conn.kw.get("dsn") == "mydb_high")
    return ok1


def scenario_python_runner(secrets_mod):
    print("\n[python_runner_tool]")
    secrets_mod._bundles = {"oci_key": GOOD_BUNDLE_OCI}
    sys.path = [p for p in sys.path if "python_runner_tool" not in p]
    # Avoid loading the full tool — directly test oci_signer.get_auth_provider
    # which is the wired surface.
    mod = _load_tool("python_runner_tool", "utils.oci_signer")
    signer = mod.get_auth_provider("DEFAULT", credential_name="oci_key")
    ok1 = check("credential_name -> Credential Store signer",
                signer.__class__.__name__ == "FakeSigner")
    return ok1


def scenario_web(secrets_mod):
    print("\n[web_toolkit]")
    secrets_mod._bundles = {"webhook_creds": GOOD_BUNDLE_WEBHOOK}
    sys.path = [p for p in sys.path if "web_toolkit" not in p]

    # Reset the captured http calls list on the stubbed base class.
    from aidputils.agents.tools.custom_tools.base import CustomToolBase
    CustomToolBase._captured_http_calls.clear()

    mod = _load_tool("web_toolkit", "tool_implementation")

    mod.WebhookSenderTool._execute_tool(
        conf={"conf": {"credential_name": "webhook_creds"}},
        runtime_params={"message": "hello"},
    )
    calls = CustomToolBase._captured_http_calls
    ok1 = check(
        "Slack URL resolved from Credential Store",
        len(calls) == 1 and calls[0].get("url") == GOOD_BUNDLE_WEBHOOK["webhook_url"],
        f"calls={len(calls)}, url={calls[0].get('url') if calls else None!r}")

    CustomToolBase._captured_http_calls.clear()
    mod.WebhookSenderTool._execute_tool(
        conf={"conf": {"webhook_url": "https://plain.example.com/hook"}},
        runtime_params={"message": "hello"},
    )
    calls = CustomToolBase._captured_http_calls
    ok2 = check(
        "empty credential_name -> plain conf.webhook_url",
        len(calls) == 1 and calls[0].get("url") == "https://plain.example.com/hook",
        f"url={calls[0].get('url') if calls else None!r}")
    return ok1 and ok2


def main() -> int:
    secrets_mod = _install_global_stubs()
    scenarios = [
        scenario_aidp_catalog,
        scenario_object_storage,
        scenario_genai,
        scenario_email,
        scenario_selectai,
        scenario_python_runner,
        scenario_web,
    ]
    failures = 0
    for sc in scenarios:
        try:
            if not sc(secrets_mod):
                failures += 1
        except Exception as ex:
            import traceback
            print(f"\n[{sc.__name__}] CRASHED")
            traceback.print_exc()
            failures += 1
    print()
    if failures:
        print(f"FAIL — {failures}/{len(scenarios)} tool wirings failed")
        return 1
    print(f"PASS — all {len(scenarios)} tool wirings verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
