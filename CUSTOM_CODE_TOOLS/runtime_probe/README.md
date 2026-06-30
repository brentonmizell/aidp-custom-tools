# Runtime Probe

Diagnostic tool. Use when a wired custom tool reports
`aidputils.secrets not available` or any other "module / API not found"
error in the AIDP runtime.

## Why it exists

The credential-store path Sambit recommended (Jun-17 thread) calls
`aidputils.secrets.get(name)`. If the runtime you're deployed against ships
an older `aidputils` that predates the `secrets` submodule, you get:

```
aidputils.secrets not available: No module named 'aidputils.secrets'
CredentialStoreError
```

That's a runtime version gap, not a code bug. This tool tells you what
your runtime actually has so you know whether to:

- file a ticket asking for a newer `aidp-utils` (preferred)
- pin a different runtime image
- temporarily use a fallback auth path

## How to use

1. Build + upload the zip (or grab the prebuilt `runtime_probe.zip` from
   this folder).
2. AIDP → Tools → upload the zip → deploy as TEST.
3. Open the Test panel — no inputs needed — click Run.
4. The `data` block in the response tells you:
   - `modules.aidputils.secrets.available` — is the recommended path usable?
   - `modules.datahub_dp_python_client.*.available` — is the underlying SDK
     present (could power a direct fallback if `aidputils.secrets` is absent)?
   - `env.OCI_HUB_DP_ENDPOINT` and `env.DATALAKE_ID` — these MUST be set for
     the credential store path to work even if the module exists.
   - `live_probes.resource_principal_signer.ok` — confirms basic OCI auth
     in the runtime.
   - `live_probes.aidputils_secrets_get.lookup_failed_with` — distinguishes
     "module missing" from "credential missing" from "lakeproxy endpoint
     missing".
   - `summary.credential_store_via_aidputils` — one-line verdict.

## Reading the summary

| Summary value | What it means |
|---|---|
| `READY` | You should be able to use the credential-store wiring. If a specific credential lookup still fails, the problem is naming, type (SECRET_TOKEN vs SERVICE_ACCOUNT), or IAM, not the runtime. |
| `NOT READY (but the underlying CredentialsClient is present — a direct fallback could work)` | We could add a fallback path that calls `CredentialsClient` directly. File a request to add that. |
| `NOT READY (neither aidputils.secrets nor datahub_dp_python_client is in this runtime)` | The runtime is too old for any credential-store path. Need a runtime upgrade. |

## What to attach to a ticket

The full `ok` envelope. Includes Python version, where each module is loaded
from (helps the platform team identify which package was actually installed),
plus the verdict. No secrets, no PEMs, nothing sensitive — safe to share.
