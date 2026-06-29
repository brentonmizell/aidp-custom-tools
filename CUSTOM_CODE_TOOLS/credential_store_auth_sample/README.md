# Credential Store Auth — reference custom tool

Production-safe pattern for custom code tools that need to call public AIDP /
OCI APIs from inside the AIDP agent runtime.

**Context (Jun 2026 thread, JR / Sambit):** the resource-principal signer
the agent runtime hands out today **does not work** against the public AIDP
volume / catalog APIs — those endpoints return 401. The supported fallback
is **OCI API-key auth resolved at runtime from AIDP's Credential Store**.
The PEM is never shipped in the zip and never lives in source.

This package is the working reference for that pattern, plus a verification
harness you can run on a dev box to confirm the wiring before deploying.

---

## TL;DR — what to do in your own tool

```python
import oci
import aidputils.secrets as secrets

bundle = secrets.get("my_oci_api_key")          # SECRET_TOKEN credential by display name
signer = oci.signer.Signer(
    tenancy=bundle["tenancy"],
    user=bundle["user"],
    fingerprint=bundle["fingerprint"],
    private_key_content=bundle["private_key"],  # PEM body, not a path
)
r = requests.get(url, auth=signer, timeout=30)
```

Required keys in the credential: `tenancy`, `user`, `fingerprint`, `private_key`.

---

## What this sample exposes

Two operations against the same volumes endpoint that returned 401 under
resource principal:

| `op` | What it does |
|---|---|
| `whoami` | Hits `identity.<region>.oci.oraclecloud.com/20160918/users/<userId>`. Confirms the signer is valid end-to-end before chasing data-plane 401s. |
| `list_volumes` | Hits `aidp.<region>.oci.oraclecloud.com/20260430/aiDataPlatforms/<lakeOcid>/catalogs/<catKey>/schemas/<schKey>/volumes`. The exact call from the Jun-15 thread. |

Both call signatures use the **same** credential-store-resolved signer, so a
green `whoami` followed by a green `list_volumes` proves the signer works
against both Identity and the AIDP data plane.

---

## One-time operator setup in AIDP

Create the credential once per environment. The tool never sees the raw
values — only the display name.

1. **Mint an OCI API key pair** the way you would for any service user.
   Recommend a dedicated service user (least privilege; only the IAM
   policies the tool needs). Don't reuse a developer's personal key.
2. **AIDP → Settings → Credentials → New** (or the workspace-scoped
   equivalent your tenancy uses).
3. **Credential type:** `SECRET_TOKEN` (not `SERVICE_ACCOUNT`, not
   `VAULT_REFERENCE` — those normalize to camelCase keys and the sample
   refuses them).
4. **Display name:** something stable. e.g. `aidp_demo_key` or
   `<workspace>_oci_signer`. This is what you'll pass to
   `secrets.get(...)`.
5. **Four secret-key pairs:**

   | Key | Value |
   |---|---|
   | `tenancy` | the tenancy OCID, e.g. `ocid1.tenancy.oc1..aaaa…` |
   | `user` | the user OCID for the service user above |
   | `fingerprint` | the fingerprint of the uploaded public key (`aa:bb:…`) |
   | `private_key` | the **PEM body of the private key**, beginning with `-----BEGIN PRIVATE KEY-----` and ending with `-----END PRIVATE KEY-----`. Paste the file contents, not a path. |

6. Save. The credential is now resolvable by name from any custom code
   tool running in this Data Lake's agent runtime.

> **Why SECRET_TOKEN and not SERVICE_ACCOUNT?** Both types store roughly
> the same fields, but `CredentialStoreService` normalizes SERVICE_ACCOUNT
> into camelCase (`tenancyId`, `userId`, `privateKey`, `region`) while
> SECRET_TOKEN preserves whatever key names you set. The sample expects
> the snake_case keys above and the harness verifies it rejects the
> SERVICE_ACCOUNT shape with a clear error rather than silently
> mis-resolving.

---

## Verification — run before deploying

The package ships a self-contained harness that stubs both `aidputils.secrets`
and the HTTP layer, so you can exercise the four real failure modes on a dev
box without a live AIDP environment:

```bash
cd CUSTOM_CODE_TOOLS/credential_store_auth_sample
python verify_pattern.py
```

Expected output:

```
  PASS  scenario_missing_credential_name  —  validation rejects early
  PASS  scenario_happy_path                 —  signer built, whoami succeeded, secrets masked
  PASS  scenario_missing_key                —  rejected with `CredentialStoreError`
  PASS  scenario_wrong_credential_type      —  rejected with `CredentialStoreError`

All 4 scenarios PASS — pattern verified.
```

What the harness proves:

1. **Happy path** — `aidputils.secrets.get(name)` returns the bundle,
   `oci.signer.Signer` is constructed with `private_key_content=...`, the
   call succeeds, and the ok envelope embeds only **masked** credential
   metadata. The raw PEM is asserted absent from the result.
2. **Missing-credential\_name** — tool rejects with `ValidationError`
   before reaching the secrets API (no wasted calls).
3. **Missing-key in credential** — tool rejects with `CredentialStoreError`
   naming the missing key. No partial signer construction.
4. **Wrong credential type** — a SERVICE_ACCOUNT-shaped bundle is
   rejected with a clear error, not a confusing TypeError mid-signer-build.

---

## Deploy

1. Build the zip the way every toolkit in this repo does:
   ```bash
   cd CUSTOM_CODE_TOOLS/credential_store_auth_sample/src
   zip -r ../credential_store_auth_sample.zip . -x "*__pycache__*" "*.pyc"
   ```
2. AIDP → Tools → New Tool → Code, upload the zip.
3. Attach an AI Compute, deploy as TEST.
4. On the Test panel, set:
   - `op` = `whoami`
   - `credential_name` = the display name from operator setup above
5. Run. If `whoami` returns the service user's identity, the signer is
   wired correctly. Switch `op` to `list_volumes`, fill in `data_lake_ocid`
   + `catalog_key` + `schema_key`, run again. A 200 response with the
   `volumes` array is the proof the credential-store signer succeeds where
   resource principal returned 401.

---

## Operational guidance (what to put in service docs)

Suggested wording for the customer-facing docs / oracle-aidp-samples README
update:

> **OCI auth for custom code tools — recommended pattern**
>
> Custom code tools that need to call OCI / AIDP REST APIs must **not**
> embed PEM files or `~/.oci/config` in the tool zip, and must not assume
> the resource-principal signer is sufficient for public AIDP data-plane
> calls. The supported production pattern is:
>
> 1. Create a `SECRET_TOKEN` credential in AIDP's Credential Store with
>    keys `tenancy`, `user`, `fingerprint`, `private_key`.
> 2. Resolve the credential at runtime with
>    `aidputils.secrets.get("<display-name>")`.
> 3. Construct an `oci.signer.Signer(...)` with
>    `private_key_content=bundle["private_key"]`.
> 4. Pass the signer as `requests.get(url, auth=signer, ...)`.
>
> See `CUSTOM_CODE_TOOLS/credential_store_auth_sample` for a runnable
> reference + verification harness.

---

## What this pattern does NOT solve

- **Resource principal is still the right answer for AIDP-internal APIs**
  (the ones the agent runtime itself uses). The credential-store pattern
  is for *public* AIDP / OCI endpoints that today reject the runtime
  signer.
- **Credential rotation** is the operator's responsibility: rotate the
  underlying API key, update the four `secret-key` values in the
  credential — the tool picks up the new values on the next invocation
  (no rebuild, no redeploy).
- **Audit** — every call signs with the same service user. If you need
  per-end-user attribution, you still need the runtime `dh-user-principal`
  header path, which is a separate (and currently unsolved) gap.

---

## File layout

```
credential_store_auth_sample/
├── README.md                           ← this file
├── verify_pattern.py                   ← dev-box harness, no AIDP needed
└── src/
    ├── __init__.py
    ├── requirements.txt
    ├── tool_config.json                ← AIDP tool descriptor
    ├── tool_implementation.py          ← CredentialStoreAuthSample class
    └── utils/
        ├── __init__.py
        └── config_utils.py             ← shared get_cfg / ok / fail helpers
```
