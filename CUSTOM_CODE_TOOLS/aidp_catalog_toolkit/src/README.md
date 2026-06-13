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

## Auth (auth_mode in config)
The control plane enforces OCI IAM RBAC on whatever identity signs the request.
- `resource_principal` (default): the agent deployment's identity. Grant the
  deployment's dynamic group the IAM policy to access AIDP. No keys. **Preferred.**
- `user_principal`: act as a specific IAM user. Supply `tenancy_ocid`,
  `user_ocid`, `fingerprint`, `private_key_content` (and `pass_phrase` if any) in
  config. **Provide these through the Credential Store / OCI Vault as templated
  `{{...}}` values — never hard-code a private key in the tool source or commit
  it in plaintext.** Note this authorizes that one configured user, not the
  end user of the chat.
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
