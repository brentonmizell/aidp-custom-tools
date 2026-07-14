"""Shared credential-store resolver for AIDP custom code tools.

Single source of truth for the pattern documented in
CUSTOM_CODE_TOOLS/CREDENTIALS.md. Each tool gets a copy of this module
into its src/utils/ directory at build time (see _shared/sync.py).

Public surface (everything tools should need):

    resolve_bundle(credential_name)
        -> (bundle: dict | None, error: str | None)
        Resolve a SECRET_TOKEN credential by display name. Empty
        credential_name returns (None, None) so callers can fall through
        to their existing auth path without raising.

    build_oci_signer_from_bundle(bundle)
        -> (signer, redacted_meta) | raises
        Validate the four OCI keys (tenancy/user/fingerprint/private_key)
        and construct oci.signer.Signer(private_key_content=...). Returns
        the signer + a dict of masked credential metadata that's safe to
        log / embed in tool responses.

    resolve_oci_signer(credential_name)
        -> (signer, redacted_meta, error)
        Convenience: combine the two above. signer is None when no
        credential is set (caller should fall through). error is set if
        the credential lookup or signer construction failed.

    mask(value, keep=4)
        Truncate a secret string for safe debug output.

This module has zero runtime dependencies on the rest of the tool —
aidputils.secrets and oci are both imported lazily so a tool that never
sets credential_name doesn't pay an import cost.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


# Required keys for an OCI API-key SECRET_TOKEN credential.
OCI_REQUIRED_KEYS = ("tenancy", "user", "fingerprint", "private_key")

# OCI region-code (embedded in every OCID) -> full region name. Commercial
# realm; unknown codes fall back to env vars / conf at the call site.
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


def region_from_ocid(ocid: str) -> Optional[str]:
    """Derive the region from an OCID's region-code segment:
    ocid1.<type>.oc1.<regioncode>.<unique> -> full region name (or None)."""
    parts = str(ocid or "").split(".")
    if len(parts) >= 4:
        return _OCI_REGION_CODES.get(parts[3].strip().lower())
    return None


def resolve_region(explicit: str = "", ocid: str = "", conf_region: str = "") -> str:
    """Resolve a region without needing it as a separate credential key:
    explicit -> derived-from-OCID -> OCI_RESOURCE_PRINCIPAL_REGION /
    OCI_REGION env -> conf -> us-ashburn-1."""
    import os
    return (str(explicit or "").strip()
            or region_from_ocid(ocid)
            or os.environ.get("OCI_RESOURCE_PRINCIPAL_REGION")
            or os.environ.get("OCI_REGION")
            or str(conf_region or "").strip()
            or "us-ashburn-1")


def bundle_connection(credential_name: str) -> Tuple[Dict[str, str], Optional[str]]:
    """Non-secret connection fields carried on a standard 5-key credential:
    data_lake_ocid (+ datalake_ocid / lake_ocid aliases) and region (inferred
    from the OCID). Returns ({}, None) when no credential is set so callers can
    fall through. ({}, error) on a lookup failure."""
    bundle, err = resolve_bundle(credential_name)
    if err:
        return {}, err
    if not bundle:
        return {}, None
    lake = str(bundle.get("data_lake_ocid") or bundle.get("datalake_ocid")
               or bundle.get("lake_ocid") or "").strip()
    out: Dict[str, str] = {}
    if lake:
        out["data_lake_ocid"] = lake
    region = region_from_ocid(lake) or region_from_ocid(str(bundle.get("tenancy") or ""))
    if region:
        out["region"] = region
    return out, None


def enrich_conf_from_bundle(conf: Any) -> Any:
    """Standard credential model: every cred-using tool gets data_lake_ocid +
    region from the credential bundle (5 keys: tenancy/user/fingerprint/
    private_key/data_lake_ocid; region inferred). Call at the top of a tool's
    _execute_tool: `conf = enrich_conf_from_bundle(conf)`.

    Fills data_lake_ocid + region into conf ONLY where conf doesn't already set
    them (explicit conf/runtime still wins). Handles the nested {"conf": {...}}
    shape and the flat shape. Returns conf unchanged if no credential is set.
    """
    if not isinstance(conf, dict):
        return conf
    inner = conf.get("conf") if isinstance(conf.get("conf"), dict) else conf
    if not isinstance(inner, dict):
        return conf
    cred = str(inner.get("credential_name") or "").strip()
    if not cred:
        return conf
    fields, err = bundle_connection(cred)
    if err or not fields:
        return conf
    new_inner = dict(inner)
    for k, v in fields.items():
        if not str(new_inner.get(k) or "").strip():
            new_inner[k] = v
    if conf.get("conf") is inner:
        out = dict(conf)
        out["conf"] = new_inner
        return out
    return new_inner


def mask(value: Optional[str], keep: int = 4) -> str:
    """Truncate a secret for debug output. Never log full tokens / keys."""
    if not value:
        return "<empty>"
    s = str(value)
    if len(s) <= keep * 2:
        return f"<{len(s)} chars>"
    return f"{s[:keep]}…{s[-keep:]}  ({len(s)} chars)"


def resolve_bundle(credential_name: Optional[str]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Look up a credential bundle. Auto-routes based on the name shape:

      - `ocid1.vaultsecret.…`           -> OCI Vault path (resource principal +
                                            SecretsClient.get_secret_bundle).
                                            Secret content must be a JSON object.
      - anything else (display name)    -> AIDP Credential Store path
                                            (aidputils.secrets.get).

    The OCID-routing exists because many AIDP runtimes today predate the
    `aidputils.secrets` submodule (Jun-17 thread). OCI Vault works against
    any runtime that has the modern OCI SDK + resource principal, which is
    every AIDP runtime tested as of writing.

    Returns:
        (bundle, None)   on success.
        (None, None)     when credential_name is empty — caller falls through.
        (None, error)    when the lookup raised or returned the wrong shape.
    """
    if not credential_name or not str(credential_name).strip():
        return None, None
    name = str(credential_name).strip()

    # Route 1: OCI Vault secret OCID.
    if name.startswith("ocid1.vaultsecret."):
        return _resolve_bundle_via_oci_vault(name)

    # Route 2: AIDP Credential Store display name.
    return _resolve_bundle_via_aidputils(name)


def _resolve_bundle_via_aidputils(credential_name: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        import aidputils.secrets as secrets
    except ImportError as ex:
        return None, (
            f"aidputils.secrets not available in this runtime: {ex}. "
            "The agent runtime's aidp-utils predates the credential-store "
            "submodule. Either (a) ask the platform team to upgrade aidp-utils, "
            "or (b) switch credential_name to an OCI Vault secret OCID "
            "(starts with `ocid1.vaultsecret.`) — that path works under "
            "resource principal on every runtime tested. See "
            "CUSTOM_CODE_TOOLS/CREDENTIALS.md for the OCI Vault setup steps."
        )

    try:
        bundle = secrets.get(credential_name)
    except Exception as ex:
        return None, f"Credential `{credential_name}` could not be read: {ex}"

    if bundle is None:
        return None, f"Credential `{credential_name}` resolved to None."
    if not isinstance(bundle, dict):
        return None, (f"Credential `{credential_name}` is a "
                      f"{type(bundle).__name__}, not a dict. The credential "
                      f"must be SECRET_TOKEN type (SERVICE_ACCOUNT / "
                      f"VAULT_REFERENCE shapes are not accepted).")
    return bundle, None


def _resolve_bundle_via_oci_vault(secret_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Fetch a JSON-encoded credential bundle from OCI Vault by secret OCID.
    Uses the agent runtime's resource principal to authenticate the Vault
    call — no API key needed for the lookup itself."""
    try:
        import base64
        import json
        import oci
        from oci.secrets import SecretsClient
    except ImportError as ex:
        return None, f"OCI Vault path requires oci.secrets: {ex}"

    try:
        signer = oci.auth.signers.get_resource_principals_signer()
    except Exception as ex:
        return None, (f"OCI Vault path needs a working resource-principal "
                      f"signer in this runtime: {ex}")

    try:
        client = SecretsClient({}, signer=signer)
        resp = client.get_secret_bundle(secret_id=secret_id)
        content_b64 = resp.data.secret_bundle_content.content
    except Exception as ex:
        return None, (f"OCI Vault secret `{secret_id[:30]}…` could not be read: "
                      f"{ex}. Check that the agent runtime's dynamic group has "
                      f"`read secret-bundles` on the compartment containing "
                      f"this secret.")

    try:
        content_str = base64.b64decode(content_b64).decode("utf-8")
    except Exception as ex:
        return None, f"OCI Vault secret content is not valid base64/utf-8: {ex}"

    try:
        bundle = json.loads(content_str)
    except json.JSONDecodeError as ex:
        return None, (f"OCI Vault secret content must be a JSON object with "
                      f"keys like {list(OCI_REQUIRED_KEYS)}. Got: {ex}")

    if not isinstance(bundle, dict):
        return None, (f"OCI Vault secret content is a "
                      f"{type(bundle).__name__}, not a JSON object/dict.")
    return bundle, None


def build_oci_signer_from_bundle(bundle: Dict[str, Any]) -> Tuple[Any, Dict[str, str]]:
    """Validate + construct an OCI signer from a credential bundle.

    Raises ValueError with the missing-key list if the bundle is incomplete.
    Returns (signer, redacted_meta).
    """
    missing = [k for k in OCI_REQUIRED_KEYS if not bundle.get(k)]
    if missing:
        raise ValueError(
            f"Credential is missing required OCI keys: {missing}. "
            f"Expected SECRET_TOKEN credential with keys "
            f"{list(OCI_REQUIRED_KEYS)}."
        )

    # Normalize the four fields. Secret stores / paste forms on Windows add
    # \r\n and stray whitespace; a stray char in the fingerprint or OCIDs
    # corrupts the signed keyId header and yields an opaque 401.
    tenancy = str(bundle["tenancy"]).strip()
    user = str(bundle["user"]).strip()
    fingerprint = normalize_fingerprint(bundle["fingerprint"])
    private_key = normalize_pem(bundle["private_key"])
    pass_phrase = (str(bundle["pass_phrase"]).strip()
                   if bundle.get("pass_phrase") else None)

    # Fail early with an actionable message rather than a raw OCI 401.
    fp_ok = _fingerprint_looks_valid(fingerprint)
    if not fp_ok:
        raise ValueError(
            f"fingerprint doesn't look like a valid OCI API-key fingerprint "
            f"(got {len(fingerprint)} chars; expected 47 in the form "
            f"aa:bb:cc:…:zz — 16 colon-separated hex pairs). Copy it exactly "
            f"from OCI Console → your user → API Keys, or derive it from your "
            f"private key with: openssl rsa -pubout -outform DER -in key.pem | "
            f"openssl md5 -c"
        )

    import oci
    # private_key_file_location is a required positional arg in some OCI SDK
    # builds (e.g. 2.175.x preview) even when signing from private_key_content.
    signer = oci.signer.Signer(
        tenancy=tenancy,
        user=user,
        fingerprint=fingerprint,
        private_key_file_location=None,
        private_key_content=private_key,
        pass_phrase=pass_phrase,
    )
    redacted = {
        "tenancy":     mask(tenancy, 6),
        "user":        mask(user, 6),
        "fingerprint": mask(fingerprint, 2),
        "fingerprint_len": len(fingerprint),
        "private_key": mask(private_key, 12),
    }
    return signer, redacted


def normalize_fingerprint(value: Any) -> str:
    """Strip whitespace and any embedded CR/LF from a fingerprint."""
    return "".join(str(value).split())  # removes all internal whitespace too


def normalize_pem(value: Any) -> str:
    """Normalize a PEM: CRLF/CR -> LF, strip surrounding blank lines, ensure a
    single trailing newline. Loads identically but avoids keyId/edge issues."""
    s = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    return s + "\n"


def _fingerprint_looks_valid(fp: str) -> bool:
    """OCI API-key fingerprints are 16 colon-separated hex pairs = 47 chars."""
    import re
    return bool(re.fullmatch(r"[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){15}", fp))


def resolve_oci_signer(credential_name: Optional[str]) -> Tuple[Any, Dict[str, str], Optional[str]]:
    """One-shot resolver: bundle lookup + signer construction.

    Returns (signer, redacted_meta, error):
        (signer, meta, None)  — caller should use this signer.
        (None, {}, None)      — no credential set; caller falls through.
        (None, {}, error_msg) — credential lookup or signer build failed.
    """
    bundle, err = resolve_bundle(credential_name)
    if err:
        return None, {}, err
    if bundle is None:
        return None, {}, None
    try:
        signer, meta = build_oci_signer_from_bundle(bundle)
    except Exception as ex:
        return None, {}, str(ex)
    return signer, meta, None
