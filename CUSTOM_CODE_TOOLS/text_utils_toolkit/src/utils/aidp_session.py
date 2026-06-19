"""
aidp_session — Shared AIDP session/runtime helpers for custom tools.

This is the standalone port of the ADOPT-marked helpers from AIDP's internal
``aidputils/agents/tools/utils.py``. Tools deployed outside the AIDP runtime
(or in standalone test harnesses) can drop this single file next to
``tool_implementation.py`` and import the same primitives the first-party
tools use, with **no dependency on AIDP internals** (no ``ParamResolverFactory``,
no ``SessionVariableResolver``, no ``CustomRemoteSigner``).

PUBLIC API
----------
    resolve_session_variable_references(value, session)
        Recursively hydrate ``{{var}}`` placeholders in strings, lists, and
        dicts from a plain session dict.

    unwrap_exception_group_message(ex)
        Flatten the message of a Python 3.11+ ExceptionGroup / TaskGroup so
        the surfaced error is actionable instead of "unhandled errors in a
        TaskGroup".

    Constants
        Module-level configuration constants (auth types, OCI connection
        property keys, HTTP header names, database type identifiers, row
        limit defaults). Mirrors the upstream ``Constants`` class.

    SystemUtils
        Thin wrappers around environment / OCI signer creation. Lazy-imports
        ``oci`` so importing this module is free when no OCI call is made.

    HttpUtil
        Tenacity-backed HTTP helpers (GET/POST/PUT/DELETE) with optional OCI
        signer auth, 3-attempt exponential backoff, and JSON return.

DESIGN NOTES
------------
- Lazy imports: nothing non-stdlib is imported at module top level. ``oci``,
  ``requests``, ``tenacity`` are imported only inside the functions that
  need them. This matches the ``aidp_io`` / ``aidp_genai`` / ``aidp_kb``
  layout — importing ``aidp_session`` is cheap and never fails because of a
  missing optional dep.
- No AIDP-specific imports. The upstream ``resolve_session_variable_references``
  uses ``ParamResolverFactory``; we replace that with a plain ``session``
  dict the caller passes in. ``SystemUtils.get_signer`` drops the ``"remote"``
  signer (that required ``CustomRemoteSigner``); the four standard signer
  modes from the AIDP runtime are kept.
- ``HttpUtil`` falls back to a hand-rolled retry loop when ``tenacity`` is
  unavailable, so a custom tool that doesn't ship ``tenacity`` in its
  ``requirements.txt`` still works.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Constants — ported from aidputils.agents.tools.utils.Constants
# ---------------------------------------------------------------------------

class Constants:
    """Configuration constants shared across AIDP tool helpers.

    Kept as a class (matching the upstream layout) so call sites can write
    ``Constants.AUTH_TYPE_RESOURCE_PRINCIPAL`` directly. All values are
    plain strings or ints with no runtime dependencies.
    """

    # Auth mode identifiers (matches the values AIDP's CustomToolBase passes
    # through to SystemUtils.get_signer).
    AUTH_TYPE = "auth_type"
    AUTH_TYPE_SECURITY_TOKEN = "security_token"
    AUTH_TYPE_INSTANCE_PRINCIPAL = "instance_principal"
    AUTH_TYPE_RESOURCE_PRINCIPAL = "resource_principal"
    AUTH_PROFILE_DEFAULT = "DEFAULT"

    # OCI connection property keys (mirror the upstream Constants. These are
    # the dot-notation keys AIDP's lakeproxy returns inside
    # ``connectionProperties`` for external SQL catalogs).
    CONNECTION_PROPS_KEY = "connectionProperties"
    USER_NAME = "user.name"
    USER_CREDENTIAL = "password"
    TNS = "tns"
    WALLET_CONTENT = "wallet.content"
    WALLET_CREDENTIAL = "wallet.password"
    WALLET_LOC_KEY = "wallet.zip"

    # HTTP / content-type headers used when calling AIDP REST endpoints.
    DH_USER_PRINCIPAL_KEY = "dh-user-principal"
    ACCEPT_JSON = "application/json"
    CONTENT_TYPE_JSON = "application/json"

    # Database type identifiers (used by SQL-shaped tools to branch on
    # backend dialect).
    DB_TYPE_ADW_23_AI = "ADW_23_AI"
    DB_TYPE_ADW_26_AI = "ADW_26_AI"
    DB_TYPE_GEN_AI = "GEN_AI"

    # SQLTool row-limit hard cap (upstream default; overridable via the
    # ``SQL_TOOL_MAX_ROWS_HARD_CAP`` env var at the call site).
    SQL_TOOL_MAX_ROWS_HARD_CAP = 1000


# ---------------------------------------------------------------------------
# Template variable resolution — standalone port (no ParamResolverFactory)
# ---------------------------------------------------------------------------

_TEMPLATE_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def _template_key(value: str) -> str:
    """Extract the first ``{{ var }}`` key from a string for error reporting.

    Returns the stripped key inside the braces, or the original string if
    no template marker is present.
    """
    m = _TEMPLATE_RE.search(value)
    return m.group(1).strip() if m else value.strip()


def _lookup_session_value(key: str, session: Dict[str, Any]) -> Any:
    """Look up a dotted key path in a session dict.

    Supports ``sessionvariables.foo``, ``user.id``, plain ``key``. Raises
    ``KeyError`` if the path cannot be resolved.
    """
    if not isinstance(session, dict):
        raise KeyError(key)
    if key in session:
        return session[key]
    cur: Any = session
    for part in key.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
            continue
        raise KeyError(key)
    return cur


def resolve_session_variable_references(value: Any, session: Optional[Dict[str, Any]] = None) -> Any:
    """Recursively replace ``{{var}}`` placeholders using a session dict.

    Unlike the upstream ``resolve_session_variable_references`` (which depends
    on AIDP's internal ``ParamResolverFactory``), this standalone version
    takes the session as a plain dict, so it works in test harnesses and
    custom tools deployed outside the AIDP runtime.

    Args:
        value: A string, dict, list, or any other value. Strings get scanned
            for ``{{ key }}`` markers; dicts and lists are walked recursively.
        session: A plain dict of available variables. Dotted keys
            (``user.id``, ``sessionvariables.foo``) are looked up by walking
            the dict.

    Returns:
        The same shape as ``value`` with all templates substituted. Non-
        string scalars are returned unchanged.

    Raises:
        KeyError: A template references a key not in ``session``. The key
            name is included in the exception message.
    """
    session = session or {}

    if isinstance(value, str):
        if "{{" not in value:
            return value

        # Fully-templated string ("{{ key }}" with nothing else) returns the
        # raw value (so non-string types survive). Anything more complex
        # stringifies the substitutions.
        stripped = value.strip()
        m = _TEMPLATE_RE.fullmatch(stripped)
        if m:
            key = m.group(1).strip()
            try:
                return _lookup_session_value(key, session)
            except KeyError:
                raise KeyError(key)

        def _sub(match: "re.Match[str]") -> str:
            key = match.group(1).strip()
            try:
                resolved = _lookup_session_value(key, session)
            except KeyError:
                raise KeyError(key)
            return "" if resolved is None else str(resolved)

        return _TEMPLATE_RE.sub(_sub, value)

    if isinstance(value, dict):
        return {k: resolve_session_variable_references(v, session) for k, v in value.items()}

    if isinstance(value, list):
        return [resolve_session_variable_references(v, session) for v in value]

    if isinstance(value, tuple):
        return tuple(resolve_session_variable_references(v, session) for v in value)

    return value


# ---------------------------------------------------------------------------
# Exception group flattening — exact port (no upstream dependencies)
# ---------------------------------------------------------------------------

def unwrap_exception_group_message(ex: Exception) -> str:
    """Return a more actionable message for ExceptionGroup/TaskGroup failures.

    Async code (notably MCP clients) can raise
    ``ExceptionGroup("unhandled errors in a TaskGroup", [...])`` where
    ``str(ex)`` is a generic wrapper. When possible, prefer the first
    nested exception's message (recursively).

    Safe on non-group exceptions: ``getattr(ex, "exceptions", None)``
    returns ``None`` for plain ``Exception`` instances, in which case
    ``str(ex)`` is returned unchanged.
    """
    try:
        sub_excs = getattr(ex, "exceptions", None)
        if isinstance(sub_excs, (list, tuple)) and len(sub_excs) > 0:
            return unwrap_exception_group_message(sub_excs[0])
    except Exception:
        pass
    return str(ex)


# ---------------------------------------------------------------------------
# SystemUtils — env + OCI signer factory (lazy oci import)
# ---------------------------------------------------------------------------

class SystemUtils:
    """Environment + OCI signer helpers.

    Mirrors the upstream ``SystemUtils`` class but drops the
    ``"remote"`` signer mode (which required AIDP's internal
    ``CustomRemoteSigner``). The four standard signer modes from the
    upstream factory are kept: ``security_token``, ``instance_principal``,
    ``resource_principal``, and the default-from-file ``user_principal``.

    All methods are ``@classmethod`` to match the upstream call style.
    """

    @classmethod
    def get_env_var(cls, var_name: str, default: Optional[str] = None) -> Optional[str]:
        """Read an environment variable. Thin wrapper around ``os.getenv``."""
        return os.getenv(var_name, default)

    @classmethod
    def make_security_token_signer(cls, oci_config: Dict[str, Any]):
        """Create an OCI SecurityTokenSigner from a parsed OCI config dict.

        Expects ``oci_config`` to contain at least ``key_file`` and
        ``security_token_file``. Reads the token file and private key
        from disk and returns the signer.

        Raises:
            ValueError: required keys are missing from ``oci_config``.
            ImportError: the ``oci`` package is not installed.
        """
        try:
            import oci  # lazy
            from oci.auth.signers.security_token_signer import SecurityTokenSigner
        except ImportError as e:
            raise ImportError(
                "oci package is required for security_token signer; "
                "add 'oci' to requirements.txt"
            ) from e

        token_file = oci_config.get("security_token_file")
        key_file = oci_config.get("key_file")
        if not token_file or not key_file:
            raise ValueError(
                "security_token signer requires 'security_token_file' and "
                "'key_file' in oci_config"
            )

        with open(token_file, "r", encoding="utf-8") as f:
            token = f.read().strip()
        private_key = oci.signer.load_private_key_from_file(key_file)
        return SecurityTokenSigner(token, private_key)

    @classmethod
    def get_signer(cls,
                   signer_type: str = Constants.AUTH_TYPE_INSTANCE_PRINCIPAL,
                   config_path: Optional[str] = None,
                   config_profile: str = Constants.AUTH_PROFILE_DEFAULT,
                   **context_vars: Any):
        """Factory for OCI request signers.

        Supported signer types:
            - ``"resource_principal"`` — for code running inside OCI
              functions / AIDP managed compute.
            - ``"instance_principal"`` — for code on an OCI compute
              instance with an instance principal attached.
            - ``"security_token"`` — for local dev with ``oci session
              authenticate`` (reads token + key from ``oci_config``).
            - ``"user_principal"`` (any other value, including missing) —
              user-principal auth from ``~/.oci/config``.

        The upstream ``"remote"`` mode is intentionally not supported here
        (it required AIDP's internal ``CustomRemoteSigner`` class). Callers
        that need ``"remote"`` should keep using the upstream helper inside
        the AIDP runtime.

        Args:
            signer_type: One of the AUTH_TYPE_* values above.
            config_path: Path to OCI config file (defaults to
                ``~/.oci/config``).
            config_profile: Profile name within the config file.
            **context_vars: Forward-compat slot for future signer kwargs.

        Returns:
            An ``oci`` signer object suitable for passing as the ``auth=``
            parameter of a ``requests`` call.

        Raises:
            ImportError: the ``oci`` package is not installed.
            ValueError: an unsupported ``signer_type`` is passed.
        """
        try:
            import oci  # lazy
        except ImportError as e:
            raise ImportError(
                "oci package is required for signer creation; "
                "add 'oci' to requirements.txt"
            ) from e

        signer_type = (signer_type or "").lower()
        if signer_type == Constants.AUTH_TYPE_RESOURCE_PRINCIPAL:
            return oci.auth.signers.get_resource_principals_signer()
        if signer_type == Constants.AUTH_TYPE_INSTANCE_PRINCIPAL:
            return oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        if signer_type == Constants.AUTH_TYPE_SECURITY_TOKEN:
            oci_config = oci.config.from_file(
                file_location=config_path or "~/.oci/config",
                profile_name=config_profile,
            )
            return cls.make_security_token_signer(oci_config)
        # Default: user-principal auth from ~/.oci/config.
        oci_config = oci.config.from_file(
            file_location=config_path or "~/.oci/config",
            profile_name=config_profile,
        )
        return oci.signer.Signer(
            tenancy=oci_config["tenancy"],
            user=oci_config["user"],
            fingerprint=oci_config["fingerprint"],
            private_key_file_location=oci_config.get("key_file"),
            pass_phrase=oci_config.get("pass_phrase"),
        )


# ---------------------------------------------------------------------------
# HttpUtil — retrying HTTP helpers with optional OCI signer
# ---------------------------------------------------------------------------

class HttpUtil:
    """Tenacity-backed HTTP helpers that mirror the upstream ``HttpUtil``.

    All methods are ``@staticmethod`` so call sites write
    ``HttpUtil.get_request(...)`` without instantiation. Each method:

    - Retries 3 times with exponential backoff (1s -> 10s) on
      ``requests.RequestException`` / ``HTTPError``.
    - Returns parsed JSON.
    - Raises ``requests.HTTPError`` on a non-2xx response (with the
      status code in the message).

    The OCI signer is optional. Pass ``signer=None`` for unauthenticated
    requests; pass a ``SystemUtils.get_signer(...)`` result to sign each
    request with OCI's request-signing spec.

    When the ``tenacity`` package is not installed, falls back to a
    hand-rolled retry loop with the same semantics (3 attempts, exp
    backoff). This keeps the helper usable in custom-tool zips that don't
    bundle ``tenacity``.
    """

    DEFAULT_TIMEOUT: Tuple[float, float] = (10.0, 60.0)

    # ---- internal retry plumbing -----------------------------------------

    @staticmethod
    def _retry_call(fn, *args, **kwargs):
        """Run ``fn`` with 3-attempt exp backoff on RequestException/HTTPError.

        Prefers ``tenacity`` when available (matches upstream behavior
        exactly); falls back to a small hand-rolled loop otherwise.
        """
        import requests  # lazy
        try:
            from tenacity import (  # lazy
                retry, stop_after_attempt, wait_exponential, retry_if_exception_type,
            )

            wrapped = retry(
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=1, min=1, max=10),
                retry=retry_if_exception_type(
                    (requests.exceptions.RequestException, requests.exceptions.HTTPError)
                ),
                reraise=True,
            )(fn)
            return wrapped(*args, **kwargs)
        except ImportError:
            # Hand-rolled fallback — same number of attempts, same backoff curve.
            import time
            delay = 1.0
            last_exc: Optional[Exception] = None
            for attempt in range(3):
                try:
                    return fn(*args, **kwargs)
                except (requests.exceptions.RequestException,
                        requests.exceptions.HTTPError) as e:
                    last_exc = e
                    if attempt == 2:
                        break
                    time.sleep(min(delay, 10.0))
                    delay *= 2
            if last_exc is not None:
                raise last_exc
            raise RuntimeError("HttpUtil retry loop exited without result")

    # ---- HTTP methods -----------------------------------------------------

    @staticmethod
    def get_request(endpoint: str,
                    signer=None,
                    headers: Optional[Dict[str, str]] = None,
                    timeout: Tuple[float, float] = DEFAULT_TIMEOUT) -> Dict[str, Any]:
        """HTTP GET with 3-attempt retry, returns parsed JSON."""
        def _do():
            import requests  # lazy
            resp = requests.get(endpoint, headers=headers, auth=signer, timeout=timeout)
            if resp.status_code >= 400:
                raise requests.exceptions.HTTPError(
                    f"Status code: {resp.status_code}", response=resp,
                )
            return resp.json()
        return HttpUtil._retry_call(_do)

    @staticmethod
    def post_request(endpoint: str,
                     data: Optional[Any] = None,
                     signer=None,
                     headers: Optional[Dict[str, str]] = None,
                     timeout: Tuple[float, float] = DEFAULT_TIMEOUT) -> Dict[str, Any]:
        """HTTP POST with 3-attempt retry, sends ``data`` as JSON body."""
        def _do():
            import requests  # lazy
            resp = requests.post(
                endpoint, json=data, headers=headers, auth=signer, timeout=timeout,
            )
            if resp.status_code >= 400:
                raise requests.exceptions.HTTPError(
                    f"Status code: {resp.status_code}", response=resp,
                )
            return resp.json() if resp.content else {}
        return HttpUtil._retry_call(_do)

    @staticmethod
    def put_request(endpoint: str,
                    data: Optional[Any] = None,
                    signer=None,
                    headers: Optional[Dict[str, str]] = None,
                    timeout: Tuple[float, float] = DEFAULT_TIMEOUT) -> Dict[str, Any]:
        """HTTP PUT with 3-attempt retry, sends ``data`` as JSON body."""
        def _do():
            import requests  # lazy
            resp = requests.put(
                endpoint, json=data, headers=headers, auth=signer, timeout=timeout,
            )
            if resp.status_code >= 400:
                raise requests.exceptions.HTTPError(
                    f"Status code: {resp.status_code}", response=resp,
                )
            return resp.json() if resp.content else {}
        return HttpUtil._retry_call(_do)

    @staticmethod
    def delete_request(endpoint: str,
                       signer=None,
                       headers: Optional[Dict[str, str]] = None,
                       timeout: Tuple[float, float] = DEFAULT_TIMEOUT) -> Dict[str, Any]:
        """HTTP DELETE with 3-attempt retry, returns parsed JSON (or {})."""
        def _do():
            import requests  # lazy
            resp = requests.delete(
                endpoint, headers=headers, auth=signer, timeout=timeout,
            )
            if resp.status_code >= 400:
                raise requests.exceptions.HTTPError(
                    f"Status code: {resp.status_code}", response=resp,
                )
            return resp.json() if resp.content else {}
        return HttpUtil._retry_call(_do)


__all__ = [
    "Constants",
    "resolve_session_variable_references",
    "unwrap_exception_group_message",
    "SystemUtils",
    "HttpUtil",
]
