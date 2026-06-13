# Testing — object_storage_tool

Upload **object_storage_tool.zip** in the workspace file browser, add a Custom Tool node, set the Tool Class Name, then use the Test panel with the values below. Mirror the Display Name / Description / Input Schema from the package's `tool_config.json`.

> Prerequisite: Needs OCI. RP needs bucket access.

## ObjectStorageTool
List / get / put / delete objects in an OCI bucket.

Config to set:
- bucket = <your bucket>

**Test 1: List**

| Field | Value |
|-------|-------|
| `operation` | `list` |

Expected: object listing.

**Test 2: Round-trip**

| Field | Value |
|-------|-------|
| `operation` | `put` |
| `name` | `test.txt` |
| `content` | `hello` |

Expected: written_bytes 5; then get, then delete.

---
Pass = the documented keys come back; any failure returns `{"error": ...}`. If the agent later says tool use is "forbidden", start a fresh chat session.