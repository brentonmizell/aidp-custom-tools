# aidp_session — Shared AIDP session/runtime helpers

A single-file Python module that ports the **standalone-safe** helpers from
AIDP's internal `aidputils/agents/tools/utils.py` so custom tools can use the
same primitives the first-party tools use, without depending on AIDP-internal
classes (`ParamResolverFactory`, `SessionVariableResolver`, `CustomRemoteSigner`).

Drop `aidp_session.py` into your tool package; the build step at
`setup.py` already syncs it into every package's `src/utils/` directory.

## What it provides

| Symbol | Why it exists |
|---|---|
| `resolve_session_variable_references(value, session)` | Hydrate `{{var}}` placeholders in strings / lists / dicts. Lets a SQL template, prompt string, or HTTP header carry session state without the tool author having to hand-roll regex substitution. The upstream version pulls from AIDP's session context; this standalone port takes a plain dict so it works in tests and in tools deployed outside the AIDP runtime. |
| `unwrap_exception_group_message(ex)` | Flatten Python 3.11+ `ExceptionGroup` / `TaskGroup` errors. Async libraries (notably MCP clients) wrap real failures inside `ExceptionGroup("unhandled errors in a TaskGroup", [...])`, so the user-visible `str(ex)` is useless. This helper digs out the first nested exception's message. Safe on plain exceptions (returns `str(ex)`). |
| `Constants` | Shared config constants: auth-mode identifiers (`AUTH_TYPE_RESOURCE_PRINCIPAL`, …), OCI connection-property keys (`USER_NAME`, `TNS`, `WALLET_CONTENT`), HTTP headers (`DH_USER_PRINCIPAL_KEY`), database types, and `SQL_TOOL_MAX_ROWS_HARD_CAP=1000`. Mirrors the upstream class. |
| `SystemUtils.get_env_var(name, default)` | Thin `os.getenv` wrapper, kept for parity so call sites match the upstream style. |
| `SystemUtils.get_signer(signer_type, config_path, config_profile, **ctx)` | Factory for OCI request signers: `resource_principal`, `instance_principal`, `security_token`, and user-principal (default). The upstream `"remote"` signer is intentionally omitted — it requires AIDP-internal `CustomRemoteSigner`. Lazy-imports `oci`. |
| `SystemUtils.make_security_token_signer(oci_config)` | Builds a `SecurityTokenSigner` from a parsed OCI config dict; used by `get_signer` and exposed for direct callers. |
| `HttpUtil.get_request / post_request / put_request / delete_request` | Tenacity-backed HTTP helpers with 3-attempt exponential backoff, optional OCI signer auth, JSON responses. Falls back to a hand-rolled retry loop when `tenacity` is not installed, so a custom tool that doesn't bundle `tenacity` still works. |

## Example: hydrate a templated SQL query from a session dict

```python
from aidp_session import resolve_session_variable_references

template = "SELECT * FROM orders WHERE region = '{{region}}' AND customer = {{user.id}}"
session = {"region": "us-ashburn-1", "user": {"id": 42}}

query = resolve_session_variable_references(template, session)
# -> "SELECT * FROM orders WHERE region = 'us-ashburn-1' AND customer = 42"
```

A `KeyError(name)` is raised if a referenced key is missing — catch it at the
tool boundary and translate to the standard `{"ok": false, "error": ...}`
envelope.

## Error contract

`aidp_session` raises plain Python exceptions; it never wraps them. Translate
at the tool boundary the same way `aidp_io` / `aidp_genai` / `aidp_kb` do:

```python
from aidp_session import resolve_session_variable_references, unwrap_exception_group_message

try:
    hydrated = resolve_session_variable_references(conf, session)
except KeyError as e:
    return {"ok": False, "error": f"missing session var: {e.args[0]}", "error_type": "KeyError"}
except Exception as e:
    return {"ok": False, "error": unwrap_exception_group_message(e), "error_type": type(e).__name__}
```

## Deployment note

`aidp_session.py` is a **single file with no top-level non-stdlib imports**.
`oci`, `requests`, and `tenacity` are imported lazily inside the functions
that need them, so importing `aidp_session` is free and cannot fail because of
a missing optional dep. The build step at `setup.py build` copies this file
into every package's `src/utils/aidp_session.py` automatically — no manual
sync needed.
