"""
OCI request signing for AIDP notebook REST and WebSocket calls.

Supports:
- CustomRemoteSigner (on AIDP compute — uses dh_user_principal + lakeproxy)
- Security Token auth (OCI CLI session — local dev)
- Standard API Key auth (local dev fallback)
"""

import logging
import os

import oci
import requests as req_lib

logger = logging.getLogger(__name__)


def get_auth_provider(oci_config_profile="DEFAULT"):
    """Get OCI authentication provider (signer).

    On compute: uses CustomRemoteSigner (signs via lakeproxy using dh_user_principal).
    Locally: falls back to config file auth (security token or API key).
    """
    # On AIDP compute, use CustomRemoteSigner which delegates signing to lakeproxy
    if os.environ.get("OCI_RESOURCE_PRINCIPAL_VERSION"):
        try:
            from aidputils.agents.auth.signer.custom_remote_signer import CustomRemoteSigner
            logger.info("Using CustomRemoteSigner (compute environment)")
            return CustomRemoteSigner()
        except ImportError:
            logger.warning("CustomRemoteSigner not available, falling back to Resource Principal")
            return oci.auth.signers.get_resource_principals_signer()

    config = oci.config.from_file(profile_name=oci_config_profile)

    # Security token auth (from `oci session authenticate`)
    if config.get("security_token_file"):
        token_file = config["security_token_file"]
        with open(token_file, "r") as f:
            token = f.read().strip()
        private_key = oci.signer.load_private_key_from_file(config["key_file"])
        return oci.auth.signers.SecurityTokenSigner(token, private_key)

    # Standard API key auth
    return oci.signer.Signer(
        tenancy=config["tenancy"],
        user=config["user"],
        fingerprint=config["fingerprint"],
        private_key_file_location=config.get("key_file"),
        pass_phrase=config.get("pass_phrase"),
    )


def sign_request(signer, method, url, body=None, additional_headers=None):
    """Sign an HTTP request using OCI signer.

    Returns dict of headers to include in the request.
    """
    headers = {}
    if additional_headers:
        headers.update(additional_headers)

    kwargs = {"headers": headers}
    if body is not None:
        kwargs["data"] = body.encode("utf-8") if isinstance(body, str) else body
        if "content-type" not in {k.lower() for k in headers}:
            headers["content-type"] = "application/json"

    prepared = req_lib.Request(method.upper(), url, **kwargs).prepare()
    signer(prepared)

    return dict(prepared.headers)


def make_signed_request(signer, method, url, body=None, additional_headers=None, timeout=30):
    """Make an HTTP request signed with OCI auth. Returns response."""
    headers = {}
    if additional_headers:
        headers.update(additional_headers)

    kwargs = {
        "auth": signer,
        "headers": headers,
        "timeout": timeout,
        "verify": True,
    }
    if body is not None:
        kwargs["data"] = body.encode("utf-8") if isinstance(body, str) else body
        if "content-type" not in {k.lower() for k in headers}:
            headers["content-type"] = "application/json"

    resp = req_lib.request(method.upper(), url, **kwargs)
    resp.raise_for_status()
    return resp
