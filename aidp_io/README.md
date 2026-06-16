# aidp_io — Shared AIDP file-IO module

A single-file Python module that gives every AIDP custom tool a uniform way to
read, write, and list files in **master volumes** and the **workspace**. It
wraps the OCI-signed `aiDataPlatforms` REST API and the Jupyter Contents API
behind one URI contract so tools never need to know which transport is in play.

## Contract

### URI format

```
master:<catalogName>.<schemaName>.<volumeName>:/<path>
workspace:/<path>
<catalogName>.<schemaName>.<volumeName>:/<path>     # alias -> master:
```

Examples:

```
master:construction_catalog.construction_schema.construction_documents:/Plans.pdf
workspace:/Notebooks/foo.ipynb
construction_catalog.construction_schema.construction_documents:/Plans.pdf
```

### Public API

| Function | Returns | Notes |
|---|---|---|
| `parse_uri(uri)` | `dict` | `{kind, volume_key, catalog, schema, volume, path}` or `{kind, path}` |
| `read_file(uri, conf, ctx)` | `bytes` | streams with `max_bytes` guard (default 50 MiB) |
| `write_file(uri, content, conf, ctx)` | `{path, bytes, version_id}` | `content` is `bytes`/`bytearray` |
| `list_files(uri, conf, ctx)` | `list[{name, path, type}]` | `type` is `"file"` or `"directory"` |
| `read_text(uri, conf, ctx, encoding="utf-8")` | `str` | thin wrapper over `read_file` |
| `write_text(uri, content_str, conf, ctx, encoding="utf-8")` | `{path, bytes, version_id}` | thin wrapper over `write_file` |

### Required config (`conf` dict)

Mirrors `aidp_catalog_toolkit`. Any custom tool can pass its existing conf
dict through — no remapping required.

| Key | Purpose | Default |
|---|---|---|
| `region` | OCI region short code (or `OCI_REGION` env) | — |
| `data_lake_ocid` | data lake OCID (or `DATALAKE_ID` env / `ctx["datalake_id"]`) | — |
| `api_version` | AIDP REST version | `20260430` |
| `service_path` | AIDP service path | `aiDataPlatforms` |
| `timeout` | HTTP timeout seconds | `30` |
| `auth_mode` | `resource_principal` / `user_principal` / `instance_principal` | `resource_principal` |
| `tenancy_ocid`, `user_ocid`, `fingerprint`, `private_key_content`, `pass_phrase` | required when `auth_mode=user_principal` | — |
| `workspace_id` | required for `workspace:` URIs | — |
| `max_bytes` | `read_file` streaming guard | 50 MiB |

> Use `api_version="20260430"` + `service_path="aiDataPlatforms"`. The older
> `20240831` / `dataLakes` combination does **not** expose
> `downloadFileMeta` / `uploadFileMeta` or the workspace Contents API.

## Examples

### Read a PDF from a master volume

```python
from aidp_io import read_file

pdf_bytes = read_file(
    "master:construction_catalog.construction_schema.construction_documents:/Plans.pdf",
    conf, context_vars,
)
```

### Read a notebook from the workspace

```python
from aidp_io import read_text

text = read_text("workspace:/Notebooks/exploration.ipynb", conf, context_vars)
```

### Write a JSON report back to a volume

```python
import json
from aidp_io import write_text

result = write_text(
    "master:reports_catalog.reports_schema.daily_reports:/2026-06-16.json",
    json.dumps(payload, indent=2),
    conf, context_vars,
)
# -> {"path": "/2026-06-16.json", "bytes": 1234, "version_id": "..."}
```

### List files in a folder

```python
from aidp_io import list_files

for item in list_files(
    "master:construction_catalog.construction_schema.construction_documents:/",
    conf, context_vars,
):
    print(item["type"], item["path"])
```

## Error envelope

`aidp_io` itself raises plain Python exceptions — it does **not** wrap them.
Tool wrappers should catch and translate to the standard AIDP envelope:

```python
from aidp_io import read_file

try:
    data = read_file(uri, conf, context_vars)
except ValueError as e:
    return {
        "ok": False,
        "error": str(e),
        "error_type": "ValueError",
        "truncated": bool(getattr(e, "truncated", False)),
    }
except Exception as e:
    return {"ok": False, "error": str(e), "error_type": type(e).__name__}
```

Notable error shapes:

- **Malformed URI** → `ValueError("expected master:... or workspace:...")`.
- **Streaming truncation** → `ValueError` with `truncated=True` attribute and,
  when applicable, a `partial_bytes` attribute holding what was read.
- **Missing workspace_id** for a `workspace:` URI → `ValueError`.
- **Empty volume_key** in a `master:` URI → `ValueError`.
- **HTTP errors** → `requests.HTTPError` (with response/status preserved on
  the exception, so wrappers can mine `opc-request-id` and body preview the
  same way `aidp_catalog_toolkit._err` does).

## Deployment note

`aidp_io.py` is a **single file with no external dependencies** beyond what
the AIDP runtime already provides (`requests` and `oci`). To use it in a
custom tool:

1. Drop `aidp_io.py` next to your tool's entry file.
2. Add `from aidp_io import read_file, write_file, list_files` (and friends).
3. Upload `aidp_io.py` alongside the tool's other files when packaging /
   deploying the tool.

All imports of `requests`, `oci`, `urllib.parse`, `base64` are lazy — importing
`aidp_io` is cheap and cannot break tools that never call its functions.
