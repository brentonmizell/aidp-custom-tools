# Testing — compute_toolkit

Upload **compute_toolkit.zip** in the workspace file browser, add a Custom Tool node, set the Tool Class Name, then use the Test panel with the values below. Mirror the Display Name / Description / Input Schema from the package's `tool_config.json`.

## MathTool
Exact arithmetic and statistics.

**Test 1: Evaluate**

| Field | Value |
|-------|-------|
| `mode` | `eval` |
| `expression` | `(1500*1.08)/12` |

Expected: result = 135.0.

**Test 2: Stats**

| Field | Value |
|-------|-------|
| `mode` | `stats` |
| `values` | `(paste stats_values.json)` |

Expected: count 7, mean ~38.57, median 30.

**Test 3: Injection blocked**

| Field | Value |
|-------|-------|
| `mode` | `eval` |
| `expression` | `__import__('os').system('ls')` |

Expected: error (disallowed) - security check.

## SchemaValidatorTool
Validate data against a JSON Schema.

**Test 1: Invalid doc**

| Field | Value |
|-------|-------|
| `data` | `(paste validator_data_invalid.json)` |
| `schema` | `(paste validator_schema.json)` |

Expected: valid=false, 2 violations.

**Test 2: Valid doc**

| Field | Value |
|-------|-------|
| `data` | `(paste validator_data_valid.json)` |
| `schema` | `(paste validator_schema.json)` |

Expected: valid=true.

## Mock files (in this folder's mock_files/)
- `stats_values.json`
- `validator_data_invalid.json`
- `validator_data_valid.json`
- `validator_schema.json`

---
Pass = the documented keys come back; any failure returns `{"error": ...}`. If the agent later says tool use is "forbidden", start a fresh chat session.