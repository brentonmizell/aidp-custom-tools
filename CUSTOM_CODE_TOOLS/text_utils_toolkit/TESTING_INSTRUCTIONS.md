# Testing — text_utils_toolkit

Upload **text_utils_toolkit.zip** in the workspace file browser, add a Custom Tool node, set the Tool Class Name, then use the Test panel with the values below. Mirror the Display Name / Description / Input Schema from the package's `tool_config.json`.

## TemplateRenderTool
Render a Jinja2 template with variables.

**Test 1: Render the email**

| Field | Value |
|-------|-------|
| `template` | `(paste email_template.txt)` |
| `variables` | `(paste template_vars.json)` |

Expected: renders for Banerjee; includes the 'review pipeline' line.

## RegexTool
Extract matches or replace text with a regex.

**Test 1: Extract emails**

| Field | Value |
|-------|-------|
| `text` | `(paste logs.txt)` |
| `pattern` | `[\w.]+@[\w.]+` |
| `mode` | `extract` |

Expected: count = 3.

**Test 2: Redact order ids**

| Field | Value |
|-------|-------|
| `text` | `(paste logs.txt)` |
| `pattern` | `ORD-\d+` |
| `mode` | `replace` |
| `replacement` | `[REDACTED]` |

Expected: order ids replaced.

## JsonTransformTool
Pull/reshape JSON with JSONPath.

**Test 1: Pull names**

| Field | Value |
|-------|-------|
| `data` | `(paste api_response.json)` |
| `path` | `$.items[*].name` |

Expected: value = ["Widget","Gadget","Gizmo"].

**Test 2: Build a shape**

| Field | Value |
|-------|-------|
| `data` | `(paste api_response.json)` |
| `mapping` | `{"count":"$.meta.total","first":"$.items[0].name"}` |

Expected: result = {"count":3,"first":"Widget"}.

## Mock files (in this folder's mock_files/)
- `email_template.txt`
- `template_vars.json`
- `logs.txt`
- `api_response.json`

---
Pass = the documented keys come back; any failure returns `{"error": ...}`. If the agent later says tool use is "forbidden", start a fresh chat session.