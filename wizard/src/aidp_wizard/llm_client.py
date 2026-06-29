"""LLM provider abstraction. Two providers shipped:
  - anthropic  (Claude API)
  - openai     (GPT / Codex)

Both follow the same interface so the wizard's UI doesn't care which one
is selected. Each provider does ONE thing the wizard needs: take the
user's natural-language tool description + the AIDP context, return a
complete tool_implementation.py as a string.
"""

from __future__ import annotations

from typing import Optional, Tuple

# Models per provider. Defaults are chosen for code-generation quality.
PROVIDER_DEFAULTS = {
    "anthropic": {
        "model": "claude-opus-4-7",
        "validate_endpoint": "https://api.anthropic.com/v1/models",
    },
    "openai": {
        "model": "gpt-5",
        "validate_endpoint": "https://api.openai.com/v1/models",
    },
}


def validate_api_key(provider: str, api_key: str) -> Tuple[bool, str]:
    """Cheap-as-possible test that the key is valid. Returns (ok, message).
    Used by the LLM-token screen to give immediate feedback before storing.

    Strategy: hit the provider's `/models` endpoint with the bearer token —
    succeeds with 200 if the key is valid, returns 401 if not. No tokens
    consumed.
    """
    import requests
    api_key = (api_key or "").strip()
    if not api_key:
        return False, "API key cannot be empty."
    if provider not in PROVIDER_DEFAULTS:
        return False, f"Unknown provider: {provider}"
    url = PROVIDER_DEFAULTS[provider]["validate_endpoint"]
    headers = _auth_headers(provider, api_key)
    try:
        r = requests.get(url, headers=headers, timeout=10)
    except requests.exceptions.Timeout:
        return False, "Timeout reaching the provider — check network."
    except Exception as e:
        return False, f"Network error: {e}"
    if r.status_code == 200:
        return True, "API key validated."
    if r.status_code == 401:
        return False, "Unauthorized — the key was rejected."
    if r.status_code == 403:
        return False, "Forbidden — key is valid but lacks permission for this endpoint."
    return False, f"Provider returned HTTP {r.status_code}: {r.text[:200]}"


def _auth_headers(provider: str, api_key: str) -> dict:
    if provider == "anthropic":
        return {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    if provider == "openai":
        return {"Authorization": f"Bearer {api_key}"}
    return {}


def generate_tool_code(
    provider: str,
    api_key: str,
    model: str,
    *,
    user_intent: str,
    tool_class_name: str,
    aidp_context: dict,
    helpers_summary: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Call the LLM to generate a tool_implementation.py body.

    Returns (code, error). On success code is non-None and error is None.
    On failure code is None and error has a human-readable message.
    """
    system_prompt = _system_prompt(helpers_summary)
    user_prompt = _user_prompt(
        user_intent=user_intent,
        tool_class_name=tool_class_name,
        aidp_context=aidp_context,
    )
    try:
        if provider == "anthropic":
            return _call_anthropic(api_key, model, system_prompt, user_prompt), None
        if provider == "openai":
            return _call_openai(api_key, model, system_prompt, user_prompt), None
    except Exception as e:
        return None, f"{provider} call failed: {e}"
    return None, f"unknown provider: {provider}"


def _call_anthropic(api_key: str, model: str, system_prompt: str, user_prompt: str) -> str:
    from anthropic import Anthropic
    client = Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    parts = []
    for block in response.content:
        if getattr(block, "type", "") == "text":
            parts.append(block.text)
    return _extract_python(("".join(parts)).strip())


def _call_openai(api_key: str, model: str, system_prompt: str, user_prompt: str) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        max_tokens=4096,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    return _extract_python(response.choices[0].message.content or "")


def _extract_python(text: str) -> str:
    """If the LLM wrapped the code in ```python ... ``` fences, strip them."""
    text = text.strip()
    if text.startswith("```"):
        # Drop opening fence (may be ```python or ```)
        first_newline = text.find("\n")
        if first_newline >= 0:
            text = text[first_newline + 1 :]
        # Drop closing fence
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].rstrip()
    return text


def _system_prompt(helpers_summary: str) -> str:
    return f"""You are an expert at writing AIDP Custom Code tools.

You produce a single Python file (`tool_implementation.py`) that defines
one or more classes decorated with `@CustomToolBase.register`. Each class
overrides `_execute_tool(cls, conf, runtime_params, **context_vars)` and
returns a dict of the form
`{{"ok": True, "data": {{...}}}}` or
`{{"ok": False, "error": "...", "error_type": "..."}}`.

Mandatory conventions:
- Use `from .utils.config_utils import get_cfg, ok, fail` for the envelope helpers.
- Import the debug channel with a graceful no-op fallback:
    try:
        from aidp_debug import debug, debug_warn, debug_error, DebugLog
    except ImportError:
        def debug(*a, **k): pass
        def debug_warn(*a, **k): pass
        def debug_error(*a, **k): pass
        class DebugLog:
            @staticmethod
            def embed(r): return r
- Use `get_cfg(conf, key, default)` for every conf access; never `conf[key]` directly.
- Wrap the entire `_execute_tool` body in try/except — exceptions become `fail(...)`.
- Cap any read/fetch/load with explicit bytes/rows limits + a `truncated:true` flag.

OCI authentication — REQUIRED for any tool calling public AIDP / OCI APIs:
- Never embed PEM files or ~/.oci/config in the zip. Never hardcode keys.
- Do NOT rely on `oci.auth.signers.get_resource_principals_signer()` for the
  public AIDP data-plane endpoints (catalogs / schemas / volumes / KBs) —
  resource principal currently returns 401 there.
- Use AIDP's Credential Store. The tool accepts a `credential_name` runtime
  param (or conf default) and resolves the bundle at invoke time:
    import aidputils.secrets as secrets
    bundle = secrets.get(credential_name)        # SECRET_TOKEN credential
    # bundle has keys: tenancy, user, fingerprint, private_key (PEM body)
    import oci
    signer = oci.signer.Signer(
        tenancy=bundle["tenancy"], user=bundle["user"],
        fingerprint=bundle["fingerprint"],
        private_key_content=bundle["private_key"],
    )
    requests.get(url, auth=signer, timeout=timeout)
- ALWAYS mask credential values in any debug output / returned envelope.
  Never log the PEM. Truncate fingerprints / OCIDs to first+last few chars.
- Validate the bundle before constructing the signer: if any of the four
  required keys is missing, fail() with `CredentialStoreError` naming the
  missing key. Don't pass through to oci.signer.Signer with partial data.

Available shared helpers (already bundled into the package's src/utils/):
{helpers_summary}

Output rules:
- Output ONLY the Python source code — no commentary, no explanation, no markdown fences.
- The code must `ast.parse` cleanly.
- Include thorough docstrings on each class.
"""


def _user_prompt(*, user_intent: str, tool_class_name: str, aidp_context: dict) -> str:
    lines = [
        f"Generate tool_implementation.py for a tool class named `{tool_class_name}`.",
        "",
        "## What the tool should do (user's description)",
        "",
        user_intent,
        "",
        "## AIDP context the tool will run against",
        "",
    ]
    for k, v in aidp_context.items():
        if v:
            lines.append(f"- **{k}**: {v}")
    lines.append("")
    lines.append("Output the full tool_implementation.py now.")
    return "\n".join(lines)
