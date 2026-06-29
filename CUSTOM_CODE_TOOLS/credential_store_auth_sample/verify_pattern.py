"""Verification harness for the credential-store auth pattern.

Standalone — does NOT need a live AIDP environment. Mocks
aidputils.secrets and requests so the pattern can be exercised end-to-end
on a dev box. Three scenarios:

  1. Happy path        — credential resolves, signer constructs, HTTP call
                         returns a fake body, ok envelope embeds the redacted
                         meta.
  2. Missing key       — credential is missing `private_key`. Tool should fail
                         with a clean CredentialStoreError naming the key.
  3. Wrong cred type   — secrets.get returns a SERVICE_ACCOUNT dict shape
                         instead of a SECRET_TOKEN flat dict. Tool should
                         reject it with a useful message.

Run:  python verify_pattern.py
Exit: 0 if every scenario behaves as expected, 1 otherwise.
"""

from __future__ import annotations

import sys
import types
import unittest.mock as mock
from pathlib import Path


HERE = Path(__file__).resolve().parent
SRC = HERE / "src"

# Sample SECRET_TOKEN bundle — what aidputils.secrets.get(name) returns for
# a well-formed credential.  Uses an in-memory RSA key so oci.signer.Signer
# can actually parse it (otherwise the call raises before we can verify
# anything downstream).
DUMMY_PEM = """-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQCxZ6Z3qy7Mi3PG
QFW3VHrAvNlIE9j6yh6+/QM3lODYJxYwYJpRkbjAvkVHl5RGUyaCSc+lG3vw2Zr4
Wf5KWBlw5e0XpFw7Aj1mB68V0L7XGy0sLvkLgUcM3vsuyKWmThaCH1AVnXmlOH+P
M3JFs3LRgvfwTKr5Yo7BfvqxR6vGY1J6lI/m4lL3l7Wpa6vKR3RP0qbHQRYCt5gI
xCV7H1hppOMVz9R3uMb9bMTYpb3qoSx4mvJxBnAo3kCFwUZS3vJk3kCJ7zCsZX+H
3qhJ0L+M88rj1szNXYjm1lAydQbnRWXi4F2sBdQ87xqzwSctApFy7uOdRwj8aRdh
0kVNqzm3AgMBAAECggEAEh8++D4Q9D2lYBXmaXMOnVHFnvHk+5jE7BMwk++0kuW6
=== TRUNCATED INVALID KEY (verify_pattern stubs oci.signer.Signer below) ===
-----END PRIVATE KEY-----
"""

GOOD_BUNDLE = {
    "tenancy":     "ocid1.tenancy.oc1..aaaaaaaatestaaaaaaaaaaaaaaaaaaaaaaaa",
    "user":        "ocid1.user.oc1..aaaaaaaatestaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "fingerprint": "aa:bb:cc:dd:ee:ff:11:22:33:44:55:66:77:88:99:00",
    "private_key": DUMMY_PEM,
}

MISSING_KEY_BUNDLE = {k: v for k, v in GOOD_BUNDLE.items() if k != "private_key"}

# SERVICE_ACCOUNT shape is the OTHER credential type that exists in AIDP —
# CredentialStoreService normalizes it to camelCase keys.
SERVICE_ACCOUNT_BUNDLE = {
    "tenancyId":   GOOD_BUNDLE["tenancy"],
    "userId":      GOOD_BUNDLE["user"],
    "fingerprint": GOOD_BUNDLE["fingerprint"],
    "privateKey":  GOOD_BUNDLE["private_key"],
    "region":      "us-ashburn-1",
}


def _install_aidputils_stubs() -> None:
    """Stub aidputils.* modules so tool_implementation.py imports cleanly."""
    # aidputils.agents.tools.custom_tools.base.CustomToolBase
    pkg_aidputils = types.ModuleType("aidputils")
    pkg_agents = types.ModuleType("aidputils.agents")
    pkg_tools = types.ModuleType("aidputils.agents.tools")
    pkg_ct = types.ModuleType("aidputils.agents.tools.custom_tools")
    pkg_base = types.ModuleType("aidputils.agents.tools.custom_tools.base")

    class _CustomToolBase:
        @classmethod
        def register(cls, klass):
            return klass

    pkg_base.CustomToolBase = _CustomToolBase

    # aidputils.secrets.get(name, key=None)
    pkg_secrets = types.ModuleType("aidputils.secrets")

    def _get(name, key=None):
        bundle = _get._bundles.get(name)
        if bundle is None:
            raise KeyError(f"credential {name!r} not found in stub")
        if key is None:
            return bundle
        return bundle.get(key) if isinstance(bundle, dict) else None

    _get._bundles = {}  # tests poke this
    pkg_secrets.get = _get
    pkg_secrets._stub_bundles = _get._bundles

    for name, mod in [
        ("aidputils", pkg_aidputils),
        ("aidputils.agents", pkg_agents),
        ("aidputils.agents.tools", pkg_tools),
        ("aidputils.agents.tools.custom_tools", pkg_ct),
        ("aidputils.agents.tools.custom_tools.base", pkg_base),
        ("aidputils.secrets", pkg_secrets),
    ]:
        sys.modules[name] = mod


def _import_tool():
    """Import the sample tool with sys.path pointed at src/."""
    sys.path.insert(0, str(SRC))
    sys.path.insert(0, str(HERE))
    import importlib
    if "tool_implementation" in sys.modules:
        del sys.modules["tool_implementation"]
    # The sample uses `from .utils.config_utils import ...` — load as a
    # package so the relative import resolves.
    pkg = importlib.import_module("src")  # registers `src` as a package
    return importlib.import_module("src.tool_implementation")


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def scenario_happy_path(tool):
    """Mocks the OCI signer + the requests call; checks the ok envelope."""
    bundles = sys.modules["aidputils.secrets"]._stub_bundles
    bundles.clear()
    bundles["my_oci_api_key"] = GOOD_BUNDLE

    fake_signer = mock.MagicMock()
    fake_signer.api_key = (f"{GOOD_BUNDLE['tenancy']}/{GOOD_BUNDLE['user']}/"
                           f"{GOOD_BUNDLE['fingerprint']}")
    fake_response = mock.MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "id":              GOOD_BUNDLE["user"],
        "name":            "verify_pattern_user",
        "compartmentId":   GOOD_BUNDLE["tenancy"],
        "lifecycleState":  "ACTIVE",
    }
    fake_response.raise_for_status.return_value = None

    with mock.patch("oci.signer.Signer", return_value=fake_signer), \
         mock.patch("requests.get", return_value=fake_response) as rg:
        result = tool.CredentialStoreAuthSample._execute_tool(
            conf={"conf": {"region": "us-ashburn-1"}},
            runtime_params={"op": "whoami", "credential_name": "my_oci_api_key"},
        )

    assert result["ok"] is True, f"expected ok=True, got {result}"
    data = result["data"]
    assert data["operation"] == "whoami"
    assert data["user"]["name"] == "verify_pattern_user"
    assert data["redacted_credential"]["private_key"].endswith("chars)"), \
        "private_key should be masked"
    assert GOOD_BUNDLE["private_key"] not in str(result), \
        "raw PEM leaked into result envelope"
    assert rg.call_count == 1
    return "happy path: signer built, whoami succeeded, secrets masked"


def scenario_missing_key(tool):
    """The credential is missing private_key — tool must fail cleanly."""
    bundles = sys.modules["aidputils.secrets"]._stub_bundles
    bundles.clear()
    bundles["incomplete_key"] = MISSING_KEY_BUNDLE

    result = tool.CredentialStoreAuthSample._execute_tool(
        conf={"conf": {"region": "us-ashburn-1"}},
        runtime_params={"op": "whoami", "credential_name": "incomplete_key"},
    )
    assert result["ok"] is False, f"expected ok=False, got {result}"
    assert result["error_type"] == "CredentialStoreError"
    assert "private_key" in result["error"], \
        f"error should name the missing key, got: {result['error']!r}"
    return f"missing-key path: rejected with `{result['error_type']}`"


def scenario_wrong_credential_type(tool):
    """SERVICE_ACCOUNT credential — wrong key shape. Tool must reject."""
    bundles = sys.modules["aidputils.secrets"]._stub_bundles
    bundles.clear()
    bundles["service_account_cred"] = SERVICE_ACCOUNT_BUNDLE

    result = tool.CredentialStoreAuthSample._execute_tool(
        conf={"conf": {"region": "us-ashburn-1"}},
        runtime_params={"op": "whoami", "credential_name": "service_account_cred"},
    )
    assert result["ok"] is False, f"expected ok=False, got {result}"
    # _build_signer should fail on missing standard keys (tenancy/user/...)
    # because SERVICE_ACCOUNT uses tenancyId/userId/privateKey instead.
    assert result["error_type"] == "CredentialStoreError"
    assert any(k in result["error"]
               for k in ("tenancy", "user", "private_key")), result["error"]
    return f"wrong-type path: rejected with `{result['error_type']}`"


def scenario_missing_credential_name(tool):
    """No credential_name supplied — must fail before reaching secrets.get."""
    result = tool.CredentialStoreAuthSample._execute_tool(
        conf={"conf": {"region": "us-ashburn-1"}},
        runtime_params={"op": "whoami"},
    )
    assert result["ok"] is False
    assert result["error_type"] == "ValidationError"
    assert "credential_name" in result["error"]
    return "missing-credential_name path: validation rejects early"


def main() -> int:
    _install_aidputils_stubs()
    tool = _import_tool()

    scenarios = [
        scenario_missing_credential_name,
        scenario_happy_path,
        scenario_missing_key,
        scenario_wrong_credential_type,
    ]
    failures = 0
    for sc in scenarios:
        try:
            msg = sc(tool)
            print(f"  PASS  {sc.__name__}  —  {msg}")
        except Exception as ex:
            failures += 1
            print(f"  FAIL  {sc.__name__}  —  {type(ex).__name__}: {ex}")
    print()
    if failures:
        print(f"{failures} / {len(scenarios)} scenarios FAILED")
        return 1
    print(f"All {len(scenarios)} scenarios PASS — pattern verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
