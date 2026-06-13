# object_storage_tool

List, read, write, and delete objects in an OCI Object Storage bucket via
resource-principal auth. oci SDK is pre-installed; no extra deps.

## Tool
**ObjectStorageTool** — operation = list | get | put | delete.
- Config: `bucket` (required), `namespace` (auto-detected if blank), `prefix`,
  `max_keys`.
- The deployment's resource principal needs access to the bucket.

## Build
```bash
zip -r object_storage_tool.zip tool_implementation.py tool_config.json requirements.txt README.md utils/ -x "*__pycache__*" "*.pyc"
```
