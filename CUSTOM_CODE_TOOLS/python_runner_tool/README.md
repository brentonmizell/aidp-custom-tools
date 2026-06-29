# python_runner_tool

Two tools for running code in an AIDP flow.

## RunPythonTool — local subprocess
Drop Python in, run it in an isolated subprocess. stdlib only.
- Input `data` (any JSON) is available as a variable `data`.
- Set a variable `result` to return structured JSON.
- Per-call timeout + output cap; accurate tracebacks.
This is the standalone Custom Code runner (DATAHUB-25052). Runs in the tool's
own venv, NOT the AIDP kernel.

## RunNotebookTool — workspace notebook on the AIDP kernel
Run a `.ipynb` from the workspace against the live AIDP notebook kernel and
return each code cell's output. Runs with the real workspace context (Spark
session, datalake), unlike RunPythonTool.

- Provide `notebook_path` (e.g. `Workspace/analysis.ipynb`) — the tool reads it
  via the workspace file API — or pass `notebook_json` directly.
- It creates a kernel session, runs code cells in order (markdown skipped),
  stops on the first cell error, and deletes the session when done.
- Config: `aidp_endpoint`, `lake_ocid`, `ws_host` (required), plus
  `workspace_key`, `cluster_key`, `oci_config_profile`, `execution_timeout`,
  `max_cells`, `max_output_chars`.
- Reuses the proven AIDP notebook protocol (`utils/jupyter_protocol.py`,
  `utils/oci_signer.py`) — the same signed-WebSocket path the Spark tool uses.
- Dependency: `websocket-client` (installs at deploy).

## Credentials
**Mixed — depends on which tool you call.**

- **`RunPythonTool`** runs Python in a local subprocess. **No credential
  required.** Leave `conf.credential_name` empty.
- **`RunNotebookTool`** opens a signed WebSocket against the AIDP notebook
  kernel — that's a public AIDP API call and **needs an OCI signer**.
  Create a `SECRET_TOKEN` credential in AIDP's Credential Store with keys
  `tenancy` / `user` / `fingerprint` / `private_key`, set
  `conf.credential_name` to its display name. The current implementation
  reads `oci_config_profile` and signs from a local `~/.oci/config` — that's
  a known gap. To migrate to the Credential Store path, swap the
  `utils/oci_signer.py` import for `aidputils.secrets.get(name)` →
  `oci.signer.Signer(private_key_content=...)`. Reference pattern:
  [`../credential_store_auth_sample/`](../credential_store_auth_sample/README.md).

See the toolkit-wide reference [`../CREDENTIALS.md`](../CREDENTIALS.md).

## Build
```bash
zip -r python_runner_tool.zip tool_implementation.py tool_config.json requirements.txt README.md utils/ -x "*__pycache__*" "*.pyc"
```
