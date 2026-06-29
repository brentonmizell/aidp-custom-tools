# aidp_catalog_toolkit

AIDP-native tools over the AIDP Data Lake control-plane REST API for Standard
Catalogs, workspace files, and KB ingestion. oci + requests are pre-installed.

## Tools
- **CatalogFileTool** — read or list files in a Standard Catalog volume.
- **VolumeWriteTool** — write a file into a Standard Catalog volume.
- **CatalogBrowserTool** — list catalogs/schemas/tables/volumes/KBs; describe a table.
- **KBIngestTool** — list KB ingestion jobs; trigger an ingestion run.
- **WorkspaceFileTool** — read or list workspace files (Jupyter Contents API).

## Base URL
`https://aidp.<region>.oci.oraclecloud.com/<apiVersion>/aiDataPlatforms/<dataLakeOcid>`
- `api_version` default `20260430`; resource segment `aiDataPlatforms`
  (both config-overridable: `api_version`, `service_path`).

## Credentials — REQUIRED (every tool in this toolkit calls public AIDP APIs)

Every tool here hits `aidp.<region>.oci.oraclecloud.com/.../aiDataPlatforms/...`
— these are **public AIDP data-plane endpoints**. The Jun-17 JR/Sambit thread
confirmed: **`resource_principal` returns 401 against these endpoints today.**
Until that's fixed service-side, you must use the Credential Store path.

**Quick setup:**

1. AIDP → Settings → Credentials → New. Type: `SECRET_TOKEN`.
2. Keys: `tenancy` / `user` / `fingerprint` / `private_key` (PEM body).
3. Set this tool's `conf.credential_name` to the credential's display name.
4. The tools call `aidputils.secrets.get(name)` →
   `oci.signer.Signer(private_key_content=...)` at invoke time.

Full how-to + verification harness:
[`../credential_store_auth_sample/`](../credential_store_auth_sample/README.md).
Toolkit-wide reference: [`../CREDENTIALS.md`](../CREDENTIALS.md).

### auth_mode values (legacy; prefer credential_name)
- `auto` / `user_principal`: works with `credential_name` (the recommended path
  above) or with hand-supplied `tenancy_ocid` / `user_ocid` / `fingerprint` /
  `private_key_content` in conf — but you should not hand-supply those in
  plaintext, use the Credential Store.
- `resource_principal`: kept for AIDP-internal-only API calls (where it works).
  For the public endpoints this toolkit hits, **it 401s**.
- `instance_principal`: instance-based compute.

True per-end-user delegation needs token exchange / aidpUtils, which isn't
available yet.

## Picking the volume (precedence)
CatalogFileTool / VolumeWriteTool resolve the target volume in this order:
1. `volume_key` in the call parameters (Test tab / agent)
2. `catalog` + `schema` + `volume` names in the call parameters
3. `volume_key` in config (Parameters tab)
4. `catalog` + `schema` + `volume` names in config

Every response includes a `resolved` block (`source` = param:names /
config:volume_key / …) so you can see exactly which input was used.

## Build
```bash
zip -r aidp_catalog_toolkit.zip tool_implementation.py tool_config.json requirements.txt README.md utils/ -x "*__pycache__*" "*.pyc"
```
