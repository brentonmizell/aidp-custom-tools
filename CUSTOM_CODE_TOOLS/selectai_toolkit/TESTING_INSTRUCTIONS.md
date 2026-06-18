# Testing the Select AI Toolkit

End-to-end manual test plan. Designed to run against the construction demo ADB,
but any Autonomous Database with sample data (SH, HR) works - swap the schema
and table names accordingly.

## Pre-requisites

1. **ADB reachable from the AIDP runtime.** Verify with a one-off `oracledb`
   connect, or by hitting any other catalog-bound tool first.
2. **Resource principal enabled** on the ADB (recommended):
   ```sql
   BEGIN DBMS_CLOUD_ADMIN.ENABLE_RESOURCE_PRINCIPAL(); END;
   /
   ```
   Skip if you intend to pass an explicit `credential_name`.
3. **Catalog binding** in AIDP exposing the ADB. For local tests without a
   catalog, collect the `conn_string`, `username`, `password`, and an unzipped
   wallet directory path.
4. **OCI GenAI access** for the chosen model (`xai.grok-4` by default).

## Test data assumptions

The examples below use the `SH` sample schema. Substitute the construction demo
values where noted:

| Field | SH example | Construction demo |
|---|---|---|
| `target_schema` | `SH` | `ADMIN` |
| `target_tables` | `SALES,CUSTOMERS,PRODUCTS,TIMES,COUNTRIES` | `INCIDENT_REPORTS,WORK_ORDERS,EMPLOYEES` |
| `profile_name` | `test_sh` | `constr_demo` |

---

## Step 1 - Provision (first run)

Invoke `SelectAIProvisionTool` with:

```json
{
  "profile_name":  "test_sh",
  "target_schema": "SH",
  "target_tables": "SALES,CUSTOMERS,PRODUCTS,TIMES,COUNTRIES",
  "llm_model_id":  "xai.grok-4",
  "catalog_key":   "<your catalog key>"
}
```

**Expected envelope:**

```json
{
  "ok": true,
  "data": {
    "action": "created",
    "profile_name": "test_sh",
    "tool_name":    "test_sh",
    "config_hash":  "<64-char sha256 hex>",
    "target_schema": "SH",
    "target_tables": ["COUNTRIES","CUSTOMERS","PRODUCTS","SALES","TIMES"],
    "llm_model_id": "xai.grok-4",
    "provider":     "oci"
  }
}
```

Verify directly in SQL:

```sql
SELECT PROFILE_NAME, CONFIG_HASH, UPDATED_AT FROM AIDP_NL2SQL_PROFILES;
SELECT PROFILE_NAME FROM USER_CLOUD_AI_PROFILES;
SELECT TOOL_NAME    FROM USER_CLOUD_AI_AGENT_TOOLS;
```

All three should show `test_sh`.

## Step 2 - Provision idempotency

Re-invoke the exact same payload from Step 1.

**Expected:** `data.action == "unchanged"`, same `config_hash`. No DDL is run
(check `UPDATED_AT` did not change).

Add one table:

```json
{ "...": "...", "target_tables": "SALES,CUSTOMERS,PRODUCTS,TIMES,COUNTRIES,CHANNELS" }
```

**Expected:** `data.action == "recreated"`, new `config_hash`, `UPDATED_AT`
advances.

Force a rebuild without changing inputs:

```json
{ "...": "...", "force_recreate": true }
```

**Expected:** `data.action == "recreated"` even though the hash matches.

## Step 3 - NL2SQL across all four actions

Same prompt, varying `action`:

```json
{
  "profile_name": "test_sh",
  "prompt":       "What were the top 5 product categories by revenue last year?",
  "action":       "<RUNSQL | SHOWSQL | NARRATE | EXPLAINSQL>",
  "max_rows":     100
}
```

**Expected envelopes:**

- `RUNSQL`:
  ```json
  {"ok": true,
   "data": {"action": "RUNSQL", "profile_name": "test_sh",
            "sql": "SELECT ...",
            "columns": ["PROD_CATEGORY", "REVENUE"],
            "row_count": 5,
            "rows": [{"PROD_CATEGORY": "...", "REVENUE": 1423884.12}, ...],
            "truncated": false}}
  ```
- `SHOWSQL`:
  ```json
  {"ok": true,
   "data": {"action": "SHOWSQL", "profile_name": "test_sh",
            "sql": "SELECT ...",
            "result": "SELECT ...",
            "read_only": true}}
  ```
- `NARRATE`:
  ```json
  {"ok": true,
   "data": {"action": "NARRATE", "profile_name": "test_sh",
            "result": "Beverages led with $1.42M in revenue ..."}}
  ```
- `EXPLAINSQL`:
  ```json
  {"ok": true,
   "data": {"action": "EXPLAINSQL", "profile_name": "test_sh",
            "sql": "/* this query joins SALES to PRODUCTS ... */ SELECT ...",
            "result": "/* ... */ SELECT ...",
            "read_only": true}}
  ```

## Step 4 - Negative tests

| Scenario | Payload | Expected |
|---|---|---|
| Missing profile | `NL2SQLTool` with `profile_name: "does_not_exist"` | `ok:false`, Oracle ORA-20000 surfaced in `error`. |
| Empty prompt | `NL2SQLTool` with `prompt: ""` | `ok:false`, `error_type: "ValueError"`. |
| Unknown action | `NL2SQLTool` with `action: "DELETE"` | `ok:false`, `error_type: "ValueError"`. |
| DML-style prompt | `NL2SQLTool` with `prompt: "delete all old customers", action: "RUNSQL"` | `ok:false`, `error_type: "ReadOnlyViolation"`, `generated_sql` included. |
| No connection info | Provision tool with `catalog_key: ""` and no `conn_string` | `ok:false`, `error_type: "ValueError"`. |

## Step 5 - Confirm Debug Channel

Run any of the above with the debug channel enabled and inspect the agent's
debug log. You should see:

- `SelectAIProvisionTool start` with `profile`, `hash`, `force`.
- `NL2SQLTool start` with `profile`, `action`, `prompt_len`.
- On failure: a corresponding `debug_error` entry with the exception text.

If the entries are missing, confirm `aidp_debug.py` is uploaded alongside
`agent.py` in the agent flow and that `DebugLog.embed(result)` is being called
in the agent's `invoke()`.

---

## Troubleshooting

### "ORA-20000: profile already exists"
The audit table fell out of sync with `USER_CLOUD_AI_PROFILES`. Drop the
orphan profile manually and re-run:
```sql
BEGIN DBMS_CLOUD_AI.DROP_PROFILE(profile_name => 'test_sh', force => TRUE); END;
/
DELETE FROM AIDP_NL2SQL_PROFILES WHERE PROFILE_NAME = 'TEST_SH';
COMMIT;
```

### "ORA-20000: tool already exists"
Same root cause for the agent tool. Drop and retry:
```sql
BEGIN DBMS_CLOUD_AI_AGENT.DROP_TOOL(tool_name => 'test_sh', force => TRUE); END;
/
```

### `ok:false, error_type: "ReadOnlyViolation"` for a clearly read-only prompt
Select AI sometimes prepends an `INSERT INTO TEMP ... SELECT ...` pattern for
analytical caching. The Python guard rejects it because the leading keyword
isn't `SELECT`/`WITH`. Re-prompt the user with explicit "show me", or call
`SHOWSQL` instead and execute the SQL through a separate read-only tool after
human review.

### 404 / "compartment not authorized" from Select AI
The ADB resource principal (or the bound `credential_name`) lacks IAM policy
for the OCI GenAI compartment. Add a policy like:
```
allow any-user to manage generative-ai-family in compartment <name>
  where ALL { request.principal.type = 'resource', request.principal.id = '<ADB OCID>' }
```

### Wallet decode failures
The base64 payload from `aidp_io.get_connection_data` is expected to be a raw
zip. If `oracledb.connect` complains about `tnsnames.ora`, inspect the temp dir
printed in the debug log - the wallet probably extracted into a sub-folder and
needs `TNS_ADMIN` pointed one level deeper. File a bug against `oracle_conn.py`.

### "ORA-06550" on `CREATE_PROFILE`
Different ADB versions accept the `attributes` argument as either a JSON
string or a CLOB. If the bind-by-name form fails, modify
`_CREATE_PROFILE_PLSQL` in `tool_implementation.py` to use the positional CLOB
form. Confirm against your ADB version (`SELECT * FROM V$VERSION`).

### Audit table name conflict
The default `AIDP_NL2SQL_PROFILES` collides with an existing object. Set the
`audit_table_name` conf to a unique name (e.g. `AIDP_NL2SQL_PROFILES_V2`) and
re-run. The old table is left alone.
