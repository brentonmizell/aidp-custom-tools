# AIDP Select AI Toolkit (in-zip README)

This README is bundled inside the toolkit zip so anyone who downloads the
package alone - without the surrounding `CUSTOM_CODE_TOOLS` repo - has the
full picture.

## What this toolkit does

Two AIDP custom tools that together turn natural-language questions into rows
from an Oracle Autonomous Database:

- **`SelectAIProvisionTool`** - one-time (per config) setup. Creates a
  `DBMS_CLOUD_AI` profile and a `DBMS_CLOUD_AI_AGENT` tool that bind a set of
  tables and an LLM model into a reusable Select AI artifact. Writes a row to
  an audit table so re-runs are no-ops when nothing changed.
- **`NL2SQLTool`** - runs every agent turn. Calls
  `SELECT DBMS_CLOUD_AI.GENERATE(:prompt, :profile, :action) FROM dual` and
  returns rows / SQL / narration depending on the action. Enforces a
  `SELECT`/`WITH` only policy in Python before executing.

All database I/O is direct `oracledb`; there is no MCP server and no extra
service to deploy.

## Layout

```
src/
  __init__.py
  tool_config.json            (manifest the AIDP runtime reads)
  tool_implementation.py      (both tool classes)
  utils/
    __init__.py
    config_utils.py           (get_cfg / ok / fail helpers)
    aidp_io.py                (canonical AIDP IO module - do not edit)
    oracle_conn.py            (open_connection helper, wallet bootstrap)
requirements.txt              (oracledb pin)
```

## Prerequisites on the target ADB

1. **Enable resource principal (recommended).** As `ADMIN`:
   ```sql
   BEGIN DBMS_CLOUD_ADMIN.ENABLE_RESOURCE_PRINCIPAL(); END;
   /
   ```
   Then leave `credential_name` empty. Alternatively pre-create a
   `DBMS_CLOUD` credential and pass its name.
2. **OCI GenAI access.** The ADB's resource principal (or bound credential)
   must have IAM policy access to the OCI GenAI compartment of the chosen
   model. Default model is `xai.grok-4`; `xai.grok-4.1` works the same.
3. **Schema grants.** The connecting user owns (or has `SELECT` on) every
   table listed in `target_tables`. The audit table is created in the
   connecting user's own schema.

## Quickstart

### 1. Upload the zip

In the AIDP console: **Tools -> Add custom tool -> Upload zip**, then point at
this `selectai_toolkit.zip`. Both tools are registered automatically from
`tool_config.json`.

### 2. Configure conf

Either pattern works:

- **Catalog-bound (preferred):** set `catalog_key` to a value like
  `construction_catalog.construction_schema.construction_db`. The toolkit
  resolves connection details via `aidp_io.get_connection_data` and unpacks
  the wallet zip into a per-call temp dir.
- **Explicit credentials:** leave `catalog_key` empty and pass `conn_string`,
  `username`, `password`, and optionally `wallet_path` as runtime params (or
  conf defaults).

### 3. Provision once

```python
SelectAIProvisionTool.invoke({
    "profile_name":  "constr_demo",
    "target_schema": "ADMIN",
    "target_tables": "INCIDENT_REPORTS,WORK_ORDERS,EMPLOYEES",
    "catalog_key":   "construction_catalog.construction_schema.construction_db",
})
```

Re-running with the same inputs returns `action: "unchanged"`. Change a
table, the model, or the credential, and the next call returns
`action: "recreated"`.

### 4. Answer questions

```python
NL2SQLTool.invoke({
    "profile_name": "constr_demo",
    "prompt":       "how many open incidents last month?",
    "action":       "RUNSQL",     # or SHOWSQL / NARRATE / EXPLAINSQL
})
```

`RUNSQL` returns `{sql, columns, row_count, rows, truncated}`.
`SHOWSQL` / `EXPLAINSQL` return `{sql, result, read_only}`.
`NARRATE` returns `{result}` (a paragraph).

## Read-only guarantee

`NL2SQLTool` refuses any statement whose leading keyword (after stripping
whitespace, `--` line comments, and `/* */` block comments) is not `SELECT`
or `WITH`. Refusals come back as:

```json
{"ok": false,
 "error_type": "ReadOnlyViolation",
 "error": "Refusing to execute non-SELECT statement.",
 "generated_sql": "..."}
```

For `SHOWSQL` and `EXPLAINSQL` the tool surfaces a `read_only` flag so the
agent can decide whether to display or refuse the generated SQL.

## Audit table

`SelectAIProvisionTool` creates `AIDP_NL2SQL_PROFILES` on first run in the
connecting user's schema. Columns:

- `PROFILE_NAME` (PK), `TOOL_NAME`
- `TARGET_SCHEMA`, `TARGET_TABLES_CSV` (uppercased, sorted)
- `LLM_MODEL_ID`, `PROVIDER`, `CREDENTIAL_NAME`
- `CONFIG_HASH` (sha256 hex), `CONFIG_JSON` (the literal hashed payload)
- `CREATED_AT`, `UPDATED_AT`

The hash is computed over a canonicalised JSON of the inputs - tables are
uppercased and sorted so `"A,B"` and `"b,a"` hash identically.

Rename the table via the `audit_table_name` conf if it conflicts.

## Connection sourcing (inside `oracle_conn.py`)

1. `catalog_key` -> `aidp_io.get_connection_data(catalog_key)` ->
   `{connectionProperties: {user.name, password, tns, wallet.content (b64
   zip), wallet.password}}`. The wallet is extracted to a fresh
   `tempfile.mkdtemp()` directory; `TNS_ADMIN` and `wallet_location` are set
   on the connection.
2. Runtime params `conn_string` + `username` + `password` (+ `wallet_path`).
3. The same names from `conf` as a final fallback.

Missing inputs raise a `ValueError` returned as
`{"ok": false, "error_type": "ValueError"}`.

## Provider list

`SelectAIProvisionTool.provider` accepts `oci`, `openai`, `cohere`, or
`azure` - the four currently documented in the `DBMS_CLOUD_AI` reference.
Default is `oci`. Add more once Oracle ships them.
