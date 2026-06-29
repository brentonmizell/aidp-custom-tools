# object_storage_tool

List, read, write, and delete objects in an OCI Object Storage bucket.
oci SDK is pre-installed; no extra deps.

## Tool
**ObjectStorageTool** — operation = list | get | put | delete.
- Config: `bucket` (required), `namespace` (auto-detected if blank), `prefix`,
  `max_keys`.

## Credentials

OCI Object Storage authenticates via OCI signer. Two paths:

- **Resource principal (default).** Grant the agent deployment's dynamic group
  the `manage objects` policy on the target bucket. If your environment is
  already wired that way, leave `conf.credential_name` empty.
- **Credential Store (recommended for cross-tenancy or service-user access).**
  Create a `SECRET_TOKEN` credential with keys `tenancy` / `user` /
  `fingerprint` / `private_key`, set `conf.credential_name` to its display
  name. The tool resolves it via `aidputils.secrets.get(name)` and constructs
  `oci.signer.Signer(private_key_content=...)` per call. Use this when the
  bucket is in a different tenancy than the agent, or when you need a service
  user with narrower IAM than the dynamic group.

Full pattern: [`../CREDENTIALS.md`](../CREDENTIALS.md) and the working reference
at [`../credential_store_auth_sample/`](../credential_store_auth_sample/README.md).

## Build
```bash
zip -r object_storage_tool.zip tool_implementation.py tool_config.json requirements.txt README.md utils/ -x "*__pycache__*" "*.pyc"
```
