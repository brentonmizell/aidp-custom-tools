# Testing — data_ops_toolkit

Upload **data_ops_toolkit.zip** in the workspace file browser, add a Custom Tool node, set the Tool Class Name, then use the Test panel with the values below. Mirror the Display Name / Description / Input Schema from the package's `tool_config.json`.

## FilterTool
Keep only records matching a condition.

**Test 1: Numbers over 1000**

| Field | Value |
|-------|-------|
| `data` | `(paste sales.json)` |
| `field` | `amount` |
| `operator` | `gt` |
| `value` | `1000` |

Expected: matched = 1 (id-2, amount 1500).

**Test 2: Closed only**

| Field | Value |
|-------|-------|
| `data` | `(paste sales.json)` |
| `field` | `status` |
| `operator` | `eq` |
| `value` | `closed` |

Expected: matched = 1 (id-3).

## CompareTool
Diff two record sets on a key.

**Test 1: Diff v1 vs v2**

| Field | Value |
|-------|-------|
| `left` | `(paste sales_v1.json)` |
| `right` | `(paste sales_v2.json)` |
| `key` | `id` |

Expected: added 1, removed 1, changed 1, unchanged 0.

## DataManipulationTool
Group / sort / dedupe / reshape records (needs pandas).

**Test 1: Group by region, sum**

| Field | Value |
|-------|-------|
| `data` | `(paste sales.json)` |
| `operation` | `groupby` |
| `spec` | `{"by":["region"],"agg":{"amount":"sum"}}` |

Expected: West 1700, East 2200.

**Test 2: Dedupe by rep**

| Field | Value |
|-------|-------|
| `data` | `(paste sales.json)` |
| `operation` | `dedupe` |
| `spec` | `{"subset":["rep"]}` |

Expected: count = 3.

## Mock files (in this folder's mock_files/)
- `sales.json`
- `sales_v1.json`
- `sales_v2.json`

---
Pass = the documented keys come back; any failure returns `{"error": ...}`. If the agent later says tool use is "forbidden", start a fresh chat session.