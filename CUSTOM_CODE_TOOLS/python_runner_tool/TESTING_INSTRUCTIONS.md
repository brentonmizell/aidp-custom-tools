# Testing — python_runner_tool

Upload **python_runner_tool.zip** in the workspace file browser, add a Custom Tool node, set the Tool Class Name, then use the Test panel with the values below. Mirror the Display Name / Description / Input Schema from the package's `tool_config.json`.

> Prerequisite: RunPython local; RunNotebook needs AIDP kernel (endpoint, lake_ocid, ws_host, cluster).

## RunPythonTool
Run a Python block in an isolated subprocess.

**Test 1: Aggregate w/ data**

| Field | Value |
|-------|-------|
| `code` | `total = sum(r['amount'] for r in data); result = {'total': total, 'rows': len(data)}` |
| `data` | `[{"amount":100},{"amount":250},{"amount":75}]` |

Expected: result {"total":425,"rows":3}. (Use ; not \n in the single-line box.)

**Test 2: Error reporting**

| Field | Value |
|-------|-------|
| `code` | `x=[1,2,3]; y=x[10]` |

Expected: returncode != 0; error names the IndexError.

## RunNotebookTool
Run a workspace .ipynb on the AIDP kernel.

Config to set:
- aidp_endpoint = https://aidp.<region>.oci.oraclecloud.com
- lake_ocid = <OCID>
- ws_host = <ws host>
- cluster_key = <active cluster>
- default_notebook_path = (optional default)

**Test 1: Run by path**

| Field | Value |
|-------|-------|
| `notebook_path` | `Workspace/sample_notebook.ipynb` |

Expected: ok=true, cells_run=2, outputs 'cell one ran' and 'total = 425'.

**Test 2: Run inline**

| Field | Value |
|-------|-------|
| `notebook_json` | `(paste sample_notebook.ipynb)` |

Expected: same result without the file read.

## Mock files (in this folder's mock_files/)
- `python_runner_snippets.json`
- `sample_notebook.ipynb`

---
Pass = the documented keys come back; any failure returns `{"error": ...}`. If the agent later says tool use is "forbidden", start a fresh chat session.