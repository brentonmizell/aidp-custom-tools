# Testing — convert_file_tool

Upload **convert_file_tool.zip** in the workspace file browser, add a Custom Tool node, set the Tool Class Name, then use the Test panel with the values below. Mirror the Display Name / Description / Input Schema from the package's `tool_config.json`.

> Prerequisite: pandas required; parquet->pyarrow, excel->openpyxl (deploy).

## ConvertFileTool
Convert between csv / json / parquet / excel.

**Test 1: CSV to JSON**

| Field | Value |
|-------|-------|
| `from_format` | `csv` |
| `to_format` | `json` |
| `content` | `(paste sample.csv)` |

Expected: rows 3, JSON returned.

**Test 2: JSON to CSV**

| Field | Value |
|-------|-------|
| `from_format` | `json` |
| `to_format` | `csv` |
| `content` | `(paste sample_records.json)` |

Expected: content starts id,name,amount.

## Mock files (in this folder's mock_files/)
- `sample.csv`
- `sample_records.json`

---
Pass = the documented keys come back; any failure returns `{"error": ...}`. If the agent later says tool use is "forbidden", start a fresh chat session.