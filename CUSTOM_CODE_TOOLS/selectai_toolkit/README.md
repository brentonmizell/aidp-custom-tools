# AIDP Select AI Toolkit

Two-tool pair for natural-language-to-SQL against Oracle Autonomous Database via
`DBMS_CLOUD_AI` and `DBMS_CLOUD_AI_AGENT`. No MCP layer, no extra microservice -
the agent talks directly to ADB through `oracledb`, and Select AI does the
NL-to-SQL translation server-side.

## What's in the box

| Tool | When to call | What it does |
|---|---|---|
| `SelectAIProvisionTool` | **Once** per `(connection, schema, table list, model)` combo. Re-runnable, hash-checked. | Connects to ADB, creates the `AIDP_NL2SQL_PROFILES` audit table on first run, then either no-ops (config hash matches), creates (no row), or drops+recreates (hash mismatch or `force_recreate=true`) the Select AI profile via `DBMS_CLOUD_AI.CREATE_PROFILE` and the agent tool via `DBMS_CLOUD_AI_AGENT.CREATE_TOOL`. |
| `NL2SQLTool` | **Every** agent turn that needs data from the bound tables. | Calls `SELECT DBMS_CLOUD_AI.GENERATE(:prompt, :profile_name, :action) FROM dual`. Supports `RUNSQL`, `SHOWSQL`, `NARRATE`, `EXPLAINSQL`. For `RUNSQL` the tool performs a two-step guard - it first asks for `SHOWSQL`, refuses anything whose leading keyword isn't `SELECT`/`WITH`, then executes the validated SQL itself so the result rows come back structured. |

## When to use this vs. a generic NL2SQL toolkit

Pick `selectai_toolkit` when:

- The target database is Oracle ADB (Autonomous Database).
- You want Oracle to own the semantic layer (table metadata, column comments,
  sample rows) instead of stuffing the schema into every LLM prompt.
- You want the LLM call itself routed through OCI GenAI under a resource
  principal, with no API keys stored in your agent.

Pick a generic NL2SQL toolkit (e.g. an MCP-based one) when:

- The target database is something other than ADB.
- You need to mix multiple non-Oracle sources in a single tool call.
- You need fine-grained client-side prompt control over the schema snippet.

The two patterns coexist - nothing stops an agent from holding both kinds of
tools.

## Prerequisites on the ADB

1. **Resource principal enabled** (recommended). Run **once** as `ADMIN`:
   ```sql
   BEGIN DBMS_CLOUD_ADMIN.ENABLE_RESOURCE_PRINCIPAL(); END;
   /
   ```
   With this enabled you can leave `credential_name` empty and Select AI will
   call OCI GenAI under the ADB's own principal. Alternatively pre-create a
   `DBMS_CLOUD` credential and pass its name as `credential_name`.

2. **OCI GenAI access**. The ADB's resource principal (or the bound credential)
   must have IAM policy access to the OCI GenAI compartment that hosts the
   chosen model.

3. **Model compatibility**. Default is `xai.grok-4`; `xai.grok-4.1` works
   identically. Any chat-capable OCI GenAI model id will be accepted - override
   via the `llm_model_id` runtime param or the `default_llm_model_id` conf.

4. **Schema grants**. The connecting user must own (or have `SELECT` on) every
   table listed in `target_tables`. The audit table is created in the
   connecting user's own schema, so no extra grant is needed for it.

## Quickstart

### 1. Build and upload the zip

From the toolkit root:

```
cd CUSTOM_CODE_TOOLS/selectai_toolkit
zip -r ../selectai_toolkit.zip src/ requirements.txt
```

Then in the AIDP console: **Tools -> Add custom tool -> Upload zip**, point at
`selectai_toolkit.zip`. AIDP will register both `SelectAIProvisionTool` and
`NL2SQLTool` from the manifest.

### 2. Attach the tools to an agent flow

Add both tools to your agent's tool list. Configure either pattern:

- **Preferred (catalog-bound):** set the `catalog_key` conf to a value like
  `construction_catalog.construction_schema.construction_db`. The toolkit
  resolves credentials via `aidp_io.get_connection_data` and materializes the
  wallet zip into a per-call temp dir.
- **Fallback (explicit creds):** leave `catalog_key` empty and pass
  `conn_string`, `username`, `password`, and optionally `wallet_path` as
  runtime params (or in conf).

### 3. Provision once

Call `SelectAIProvisionTool` from a setup step or the agent's first message:

```python
provision = SelectAIProvisionTool.invoke({
    "profile_name":  "constr_demo",
    "target_schema": "ADMIN",
    "target_tables": "INCIDENT_REPORTS,WORK_ORDERS,EMPLOYEES",
    "catalog_key":   "construction_catalog.construction_schema.construction_db",
})
# -> {"ok": true, "data": {"action": "created", "profile_name": "constr_demo", ...}}
```

Subsequent calls with identical inputs return `action: "unchanged"` and do no
DDL. Change a table or the model id and the next call returns
`action: "recreated"`.

### 4. Answer questions every turn

```python
answer = NL2SQLTool.invoke({
    "profile_name": "constr_demo",
    "prompt":       user_question,    # verbatim user message
    "action":       "RUNSQL",         # or SHOWSQL / NARRATE / EXPLAINSQL
})
```

`RUNSQL` returns `{sql, columns, row_count, rows, truncated}`. The other actions
return `{result, sql?, read_only?}`.

## Read-only guarantee

The Python layer of `NL2SQLTool` enforces a `SELECT` / `WITH` only policy via a
regex that skips leading whitespace, `--` line comments, and `/* */` block
comments before checking the first keyword. Any statement that fails the check
is rejected with:

```json
{"ok": false, "error_type": "ReadOnlyViolation",
 "error": "Refusing to execute non-SELECT statement.",
 "generated_sql": "..."}
```

For `SHOWSQL` and `EXPLAINSQL` the tool surfaces a `read_only` boolean so the
agent can decide whether to display or refuse a response that Select AI happened
to write as DML.

## Audit table

On first run `SelectAIProvisionTool` creates `AIDP_NL2SQL_PROFILES` in the
connecting user's schema:

| Column | Purpose |
|---|---|
| `PROFILE_NAME` | PK; matches the Select AI profile name. |
| `TOOL_NAME` | Matches the `DBMS_CLOUD_AI_AGENT` tool name (currently `= PROFILE_NAME`). |
| `TARGET_SCHEMA`, `TARGET_TABLES_CSV` | Bound tables (sorted, uppercased). |
| `LLM_MODEL_ID`, `PROVIDER`, `CREDENTIAL_NAME` | Profile attributes. |
| `CONFIG_HASH` | SHA-256 over a canonicalised JSON of the inputs; drives idempotency. |
| `CONFIG_JSON` | The exact JSON payload that was hashed (for diffing). |
| `CREATED_AT`, `UPDATED_AT` | Timestamps. |

Rename via the `audit_table_name` conf if `AIDP_NL2SQL_PROFILES` conflicts with
an existing object in your schema.

## Connection sourcing

Order of resolution inside `open_connection`:

1. `catalog_key` (runtime or conf) -> `aidp_io.get_connection_data(catalog_key)`
   -> use the returned `connectionProperties` (`user.name`, `password`, `tns`,
   `wallet.content` base64 zip, `wallet.password`). The wallet is extracted to
   a fresh `tempfile.mkdtemp()` directory; `TNS_ADMIN` is set and
   `wallet_location` is passed to `oracledb.connect`.
2. Runtime params `conn_string` + `username` + `password` (+ `wallet_path`).
3. Same names from `conf` as a final fallback.

If none of these yield a complete tuple, the tool returns
`ok:false, error_type:"ValueError"`.

## Files

```
selectai_toolkit/
  README.md                  <-- this file
  TESTING_INSTRUCTIONS.md
  requirements.txt           (oracledb pin)
  src/
    README.md                (in-zip copy of the user-facing docs)
    __init__.py
    tool_config.json         (manifest: both tools, schemas, conf, _uiHints)
    tool_implementation.py   (SelectAIProvisionTool + NL2SQLTool classes)
    utils/
      __init__.py
      config_utils.py        (get_cfg / ok / fail)
      aidp_io.py             (canonical AIDP IO module - do not edit)
      oracle_conn.py         (open_connection helper)
  mock_files/
    _no_mock_files.txt       (placeholder - real mocks added later)
```
