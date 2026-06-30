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


def mask(value: Optional[str], keep: int = 4) -> str:
    """Truncate a secret for debug output. Never log full tokens / keys."""
    if not value:
        return "<empty>"
    s = str(value)
    if len(s) <= keep * 2:
        return f"<{len(s)} chars>"
    return f"{s[:keep]}…{s[-keep:]}  ({len(s)} chars)"


def resolve_bundle(credential_name: Optional[str]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Look up a SECRET_TOKEN credential by display name.

    Returns:
        (bundle, None)   on success — bundle is the dict of secret-key pairs.
        (None, None)     when credential_name is empty/None — caller should
                         fall through to its existing auth path (no error).
        (None, error)    when the lookup raised or returned the wrong shape.
    """
    if not credential_name or not str(credential_name).strip():
        return None, None
    try:
        import aidputils.secrets as secrets
    except ImportError as ex:
        return None, (f"aidputils.secrets not available: {ex}. "
                      f"Update the agent runtime or remove conf.credential_name.")

    try:
        bundle = secrets.get(str(credential_name).strip())
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
    import oci
    signer = oci.signer.Signer(
        tenancy=bundle["tenancy"],
        user=bundle["user"],
        fingerprint=bundle["fingerprint"],
        private_key_content=bundle["private_key"],
    )
    redacted = {
        "tenancy":     mask(bundle["tenancy"], 6),
        "user":        mask(bundle["user"], 6),
        "fingerprint": mask(bundle["fingerprint"], 2),
        "private_key": mask(bundle["private_key"], 12),
    }
    return signer, redacted


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
