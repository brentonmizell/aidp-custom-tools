"""Thin async-friendly wrapper around the shared aidp_io helpers.

The wizard uses these to populate dropdowns and validate auth. Errors
become tuples (None, error_message) so the UI can render them in a
status line without try/except boilerplate inside every screen.
"""

from __future__ import annotations

import configparser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Locate the shared aidp_io module
# ---------------------------------------------------------------------------
#
# Two install scenarios:
#   1. Wizard installed as pip package; aidp_io must also be importable as
#      a top-level module (we ship a small shim via pyproject extras OR
#      the user installed the parent repo with `pip install -e ..`).
#   2. Wizard run from inside the repo tree (development); aidp_io is two
#      directories up at `../aidp_io/aidp_io.py`.
#
# Lazy + tolerant: import at call time, give a useful error if missing.

def _load_aidp_io():
    try:
        import aidp_io  # type: ignore
        return aidp_io
    except Exception:
        pass
    # Dev-tree fallback: hunt for ../../aidp_io/aidp_io.py
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "aidp_io" / "aidp_io.py"
        if candidate.is_file():
            import importlib.util
            spec = importlib.util.spec_from_file_location("aidp_io", candidate)
            mod = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(mod)
            return mod
    raise ImportError(
        "aidp_io not found. From the wizard/ directory run "
        "`pip install -e ..` to make the shared helpers importable."
    )


# ---------------------------------------------------------------------------
# OCI profile discovery (used by step 2)
# ---------------------------------------------------------------------------

def list_oci_profiles() -> List[Dict[str, Any]]:
    """Return ~/.oci/config profiles with metadata so the wizard can show:
       'DEFAULT       — API-key       — fingerprint 3c:ea:...'
       'aidp-session  — session token — expires in 47 min'
    """
    cfg_path = Path.home() / ".oci" / "config"
    if not cfg_path.is_file():
        return []
    cp = configparser.ConfigParser()
    cp.read(cfg_path)
    profiles = []
    for name in cp.sections() + (["DEFAULT"] if "DEFAULT" in cp else []):
        if name not in cp:
            continue
        section = cp[name]
        prof = {
            "name": name,
            "region": section.get("region", "").strip(),
            "tenancy": section.get("tenancy", "").strip(),
            "user": section.get("user", "").strip(),
            "fingerprint": section.get("fingerprint", "").strip(),
            "security_token_file": section.get("security_token_file", "").strip(),
            "key_file": section.get("key_file", "").strip(),
        }
        prof["kind"] = "session_token" if prof["security_token_file"] else "user_principal"
        # Session token expiry — best-effort decode of the JWT exp claim.
        prof["expires_in_min"] = None
        if prof["security_token_file"]:
            try:
                token = Path(prof["security_token_file"]).expanduser().read_text(
                    encoding="utf-8"
                ).strip()
                import base64, json as _json, time as _time
                payload = token.split(".")[1]
                payload += "=" * (-len(payload) % 4)
                j = _json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
                if "exp" in j:
                    prof["expires_in_min"] = int((j["exp"] - _time.time()) / 60)
            except Exception:
                pass
        profiles.append(prof)
    return profiles


# ---------------------------------------------------------------------------
# OCI session creation + manual profile entry (used by step 2 helpers)
# ---------------------------------------------------------------------------

def oci_cli_installed() -> bool:
    """True if the `oci` CLI is on PATH (needed for browser session auth)."""
    import shutil
    return shutil.which("oci") is not None


def build_session_auth_argv(region: str, profile_name: str,
                            tenancy_name: Optional[str] = None) -> List[str]:
    """Argv the wizard shells out to. Kept separate so tests can introspect it."""
    argv = ["oci", "session", "authenticate",
            "--region", region,
            "--profile-name", profile_name]
    if tenancy_name:
        argv += ["--tenancy-name", tenancy_name]
    return argv


def write_api_key_profile(profile_name: str, *, tenancy_ocid: str, user_ocid: str,
                          fingerprint: str, key_file: str, region: str) -> Tuple[bool, str]:
    """Append (or overwrite) a permanent API-key profile in ~/.oci/config.
    Returns (ok, message). Does NOT touch DEFAULT — always a named section."""
    if not profile_name or profile_name.upper() == "DEFAULT":
        return False, "Pick a profile name other than DEFAULT."
    for field, val in (("tenancy_ocid", tenancy_ocid), ("user_ocid", user_ocid),
                       ("fingerprint", fingerprint), ("key_file", key_file),
                       ("region", region)):
        if not val.strip():
            return False, f"{field} is required."
    key_path = Path(key_file).expanduser()
    if not key_path.is_file():
        return False, f"Key file not found: {key_path}"

    cfg_path = Path.home() / ".oci" / "config"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cp = configparser.ConfigParser()
    if cfg_path.is_file():
        cp.read(cfg_path)
    cp[profile_name] = {
        "tenancy": tenancy_ocid.strip(),
        "user": user_ocid.strip(),
        "fingerprint": fingerprint.strip(),
        "key_file": str(key_path),
        "region": region.strip(),
    }
    with cfg_path.open("w", encoding="utf-8") as fh:
        cp.write(fh)
    try:
        import os as _os
        _os.chmod(cfg_path, 0o600)
    except Exception:
        pass
    return True, f"Profile [{profile_name}] written to {cfg_path}."


# ---------------------------------------------------------------------------
# Conf builder — turn wizard state into the shape aidp_io expects
# ---------------------------------------------------------------------------

def conf_from_state(state) -> Dict[str, Any]:
    """Build the conf dict aidp_io functions consume.

    Pins the live AIDP REST surface: /20260430/aiDataPlatforms/{lakeOcid}/.
    aidp_io.py defaults service_path to the old "dataLakes" — passing the
    explicit value is what stops the 404 on /catalogs.
    """
    return {
        "region": state.region,
        "data_lake_ocid": state.data_lake_ocid,
        "api_version": "20260430",
        "service_path": "aiDataPlatforms",
        "auth_mode": state.oci_auth_mode or "auto",
        "oci_config_profile": state.oci_profile or "DEFAULT",
    }


# ---------------------------------------------------------------------------
# Validation calls used by the wizard
# ---------------------------------------------------------------------------

def validate_auth(state) -> Tuple[bool, str]:
    """Return (ok, message). Tries to list catalogs as a smoke test that
    auth + region + data_lake_ocid are all configured correctly."""
    try:
        io = _load_aidp_io()
    except Exception as e:
        return False, str(e)
    if not state.region:
        return False, "Region is required."
    if not state.data_lake_ocid:
        return False, "Data Lake OCID is required."
    try:
        items = io.list_catalogs(conf_from_state(state), {})
        return True, f"Authenticated — found {len(items)} catalogs in this Data Lake."
    except Exception as e:
        return False, f"AIDP REST call failed: {e}"


# ---------------------------------------------------------------------------
# Resource listings (used by the dropdowns)
# ---------------------------------------------------------------------------

def list_workspaces(state) -> Tuple[Optional[List[Dict]], Optional[str]]:
    try:
        io = _load_aidp_io()
        items = io.list_workspaces(conf_from_state(state), {})
        return items, None
    except Exception as e:
        return None, str(e)


def list_catalogs(state) -> Tuple[Optional[List[Dict]], Optional[str]]:
    try:
        io = _load_aidp_io()
        return io.list_catalogs(conf_from_state(state), {}), None
    except Exception as e:
        return None, str(e)


def list_schemas(state) -> Tuple[Optional[List[Dict]], Optional[str]]:
    try:
        io = _load_aidp_io()
        return io.list_schemas(conf_from_state(state), {}, state.catalog_key), None
    except Exception as e:
        return None, str(e)


def list_tables(state) -> Tuple[Optional[List[Dict]], Optional[str]]:
    try:
        io = _load_aidp_io()
        return io.list_tables(
            conf_from_state(state), {}, state.catalog_key, state.schema_key,
        ), None
    except Exception as e:
        return None, str(e)


def list_volumes(state) -> Tuple[Optional[List[Dict]], Optional[str]]:
    try:
        io = _load_aidp_io()
        return io.list_volumes(
            conf_from_state(state), {}, state.catalog_key, state.schema_key,
        ), None
    except Exception as e:
        return None, str(e)


def list_kbs(state) -> Tuple[Optional[List[Dict]], Optional[str]]:
    try:
        io = _load_aidp_io()
        return io.list_knowledge_bases(
            conf_from_state(state), {},
            state.catalog_key or None, state.schema_key or None,
        ), None
    except Exception as e:
        return None, str(e)
