# AIDP SQL Tool — Integrated Developer Documentation

**Status**: Draft for the AIDP product team.
**Authors**: Brenton Mizell (compiled from PDF docs + source code review).
**Purpose**: A single document that reproduces the official AIDP Workbench
SQL Tool documentation, surfaces gaps between the docs and the live code,
and positions the tool against sibling Custom Code tools maintained in
[brentonmizell/aidp-custom-tools](https://github.com/brentonmizell/aidp-custom-tools).

Sources merged in this document:

| Source | What it provides |
|---|---|
| *Oracle AI Data Platform Workbench User's Guide* (Nov LA PDF), pp. 222-225, 282-284, 297-302 | The canonical user-facing SQL Tool docs |
| `aidp-utils/.../tools/sql.py` + `tools/sqltool/` (datahub source) | Actual runtime behavior, including features not in the PDF |
| [docs.oracle.com — Table SQL Grammar](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aidug/table-sql-grammar.html) | The SQL dialect (Spark SQL) used for catalog DDL |
| [docs.oracle.com — AI Data Platform Workbench REST API](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aiwap/index.html) | REST API index for future endpoint references |

---

## 1. Overview

The AIDP SQL Tool lets an agent flow execute parameterized SQL queries
against tables registered in an AIDP catalog. The query is fixed at design
time; the agent supplies parameter values at runtime. Results return as
structured rows the agent can summarize or hand to a downstream node.

Two surfaces ship the tool:

- **Visual flow** — drag a SQL Tool node onto the canvas, fill the
  Configuration tab (catalog + schema + query), define parameter
  descriptions, click Apply. Test from the Test tab.
- **LangGraph code** — call `create_langgraph_tool()` from
  `aidputils.agents.toolkit.tool_helper` with an `AIDPToolConf` whose
  `tool_class="SQLTool"`. The visual flow is a thin UI on top of this.

The tool is **read-oriented**: typical usage is `SELECT` queries with
`{{parameter}}` placeholders. DDL/DML execution is technically possible
but not the design intent.

---

## 2. Tool Configuration Properties

Per the user's guide (PDF p. 224, Table 20-3):

| Property | Type | Description |
|---|---|---|
| `catalogKey` | string | Identifier for the catalog or database connection. |
| `schemaKey` | string | Schema name within the catalog/database. |
| `query` | string | SQL query string. May include `{{name}}` placeholders. |

Each `{{name}}` placeholder is declared in the tool's `params` array
with a `name`, `type`, `description`, and optional `defaultValue`. The
agent sees only the tool's `name`, `description`, and `inputSchema` (the
latter auto-generated from `params`). The `catalogKey` / `schemaKey` /
`query` themselves are NOT visible to the agent — they're tool-private.

This shape conforms to the [Model Context Protocol — Tools
specification](https://modelcontextprotocol.io/specification/server/tools).

### 2.1 Code example (LangGraph)

```python
from aidputils.agents.toolkit.tool_helper import create_langgraph_tool
from aidputils.agents.toolkit.configs import AIDPToolConf

sql_config = {
    "catalogKey": "adw23ai_phx",
    "schemaKey": "gold",
    "query": "SELECT employee_id, first_name, last_name FROM employees "
             "WHERE salary >= {{min_salary}} LIMIT {{max_rows}}",
}

sql_params = [
    {"name": "min_salary", "type": "integer",
     "description": "Minimum salary filter, USD.", "defaultValue": "50000"},
    {"name": "max_rows", "type": "integer",
     "description": "Row cap.", "defaultValue": "100"},
]

sql_conf = AIDPToolConf(
    name="employee_lookup",
    description="Look up employees by minimum salary.",
    tool_class="SQLTool",
    conf=sql_config,
    params=sql_params,
)

sql_tool = create_langgraph_tool(sql_conf.model_dump())
```

---

## 3. Catalog Support — Where the Docs and Code Diverge

### 3.1 What the user's guide currently says (PDF p. 222)

> **Note**
> The SQL tool only performs queries against data in an external
> catalog. It does not support data stored in a standard catalog.

### 3.2 What the source code actually does

The current runtime (`aidp-utils/.../tools/sql.py`) dispatches by
catalog type:

```python
@staticmethod
def _resolve_catalog_type(conf: dict) -> str:
    return str(conf.get("catalogType") or conf.get("catalog_type")
               or "EXTERNAL").strip().upper()

# in _invoke_tool:
catalog_type = cls._resolve_catalog_type(conf)
if catalog_type == "STANDARD":
    result = _SparkSQLExecutor.execute(...)        # <- Spark SQL path
else:
    conn_manager = cls.__connect(...)              # <- oracledb path
    result = _QueryExecutor(conn_manager.pool).execute(query, bind_params, ...)
```

**Both branches are implemented today**:

| `catalogType` | Execution path | Backend | Where the code lives |
|---|---|---|---|
| `EXTERNAL` (default) | `_QueryExecutor` via `_ConnectionManager.pool` | `python-oracledb` against ADW/ADB | `sqltool/connection_manager.py`, `sqltool/query_executor.py` |
| `STANDARD` | `_SparkSQLExecutor.execute` | Spark SQL via the AIDP cluster gateway | `sqltool/spark_sql_executor.py` (1,125 lines) |

This means a STANDARD catalog query goes through Spark SQL, NOT
oracledb. The grammar for STANDARD-catalog queries is therefore [Spark
SQL](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aidug/table-sql-grammar.html),
not Oracle SQL.

### 3.3 Recommended documentation fix

Update the PDF note to read:

> **Note**
> The SQL tool routes to two execution paths based on the catalog type:
> - **External catalogs** — queries execute via `python-oracledb`
>   against the underlying ADW/ADB. Uses Oracle SQL.
> - **Standard catalogs** — queries execute via Spark SQL on the
>   AIDP compute cluster. Uses [Spark SQL
>   grammar](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aidug/table-sql-grammar.html).
>
> The `catalogType` field on the tool config selects the path
> (`EXTERNAL` is the default).

### 3.4 New config field to document

Per the source dispatch, the following config field exists but is not
documented in the PDF:

| Property | Type | Description |
|---|---|---|
| `catalogType` | string | `EXTERNAL` (default) or `STANDARD`. Selects the execution path. Alternate spelling `catalog_type` is also accepted. |

---

## 4. Parameter Syntax

### 4.1 Runtime placeholders — `{{name}}`

Per the user's guide (PDF p. 222):

```sql
SELECT * FROM employees WHERE salary >= {{MAX_SALARY}}
```

These are filled by the agent at call time and declared in the tool's
`params` array.

### 4.2 Session placeholders — `[[name]]`

The source supports a second placeholder syntax not documented in the
PDF (per `sql.py` query-formatting code and `utils.py`'s
`resolve_session_variable_references`):

```sql
SELECT * FROM orders
WHERE customer_id = {{customer_id}}
  AND tenant_id = [[session.tenant_id]]
```

`{{ }}` resolves from `runtime_params`; `[[ ]]` resolves from the
session/context. The two have different precedence and should be
treated as distinct features, not aliases.

### 4.3 Bind vs. inline classification

The query formatter (`format_query(template, runtime_params,
context_vars) -> (formatted_query, bind_params)`) automatically
classifies each placeholder as either:

- a **bind parameter** (passed to the driver as a `:1`-style bind), or
- an **inline value** (rendered into the SQL string verbatim — required
  for identifiers like table names that can't be bound).

The classification is based on the parameter's declared `type` and
position in the query.

---

## 5. Row Limit Semantics

Source: `sqltool/limit_processor.py` + `sqltool/row_limit_planner.py`.

### 5.1 How limits are computed

```python
class _LimitProcessor:
    @classmethod
    def resolve_row_limit_config(cls, conf: dict) -> tuple[bool, int]:
        default_cap = cls._resolve_max_rows_hard_cap()
        row_limit_enabled = cls._parse_is_row_limit_enabled(conf)
        effective_row_limit = cls._resolve_effective_max_rows(conf, default_cap)
        return row_limit_enabled, effective_row_limit
```

| Conf field | Type | Meaning |
|---|---|---|
| `isRowLimitEnabled` | boolean | If false, no limit is applied. |
| `maxRows` | integer | Caller's desired cap; hard-capped to the server's `SQL_TOOL_MAX_ROWS_HARD_CAP` (default `1000`). |

The effective limit = `min(conf.maxRows, SQL_TOOL_MAX_ROWS_HARD_CAP)`.
Set the env var `SQL_TOOL_MAX_ROWS_HARD_CAP` on the AIDP runtime to
change the hard cap.

### 5.2 Truncation detection

The executor fetches `limit + 1` rows from the cursor. If `limit + 1`
rows come back, the result is sliced to `limit` and the response
metadata sets `results_truncated: true` with
`truncation_reason: ["ROW_LIMIT_APPLIED"]`.

### 5.3 Documentation gap

The PDF does not document `isRowLimitEnabled` or `maxRows`. Recommend
adding both to Table 20-3.

---

## 6. Output Envelope

Source: `sqltool/query_executor.py` and `sqltool/error_handler.py`.

### 6.1 Success envelope

```json
{
  "result": [ { "EMPLOYEE_ID": 101, "FIRST_NAME": "John" } ],
  "metadata": {
    "execution_time": 0.142,
    "rows_fetched": 100,
    "rows_affected": null,
    "query": "SELECT employee_id, first_name, last_name FROM employees ...",
    "timestamp": "2026-06-19T08:14:32.014Z",
    "results_truncated": false,
    "truncation_reason": null,
    "row_limit": 100
  }
}
```

`result` is a list of column-keyed dicts (column names follow Oracle's
default casing for EXTERNAL, Spark's for STANDARD).
`metadata.query` is the formatted/redacted public form — bind
placeholders are NOT expanded into the public form.

### 6.2 Error envelope (MCP-conformant)

Constructed by `build_mcp_error_response(exception)`:

```json
{
  "isError": true,
  "error": {
    "code": 400,
    "aidp_error_code": "SQL_TOOL_INVALID_INPUT",
    "message": "Parameter 'max_rows' must be an integer (got string).",
    "data": {
      "query": "...",
      "reason": "type_mismatch"
    }
  }
}
```

| Field | Source |
|---|---|
| `code` | HTTP status code from `ERROR_DETAILS[aidp_error_code].httpCode` |
| `aidp_error_code` | Symbolic code mapped from the internal `SQLToolException.error_code` |
| `message` | Human-readable error message (safe to surface to end users) |
| `data` | Optional structured detail; commonly includes the formatted query and the precise reason |

### 6.3 Documentation gap

The PDF says nothing about either envelope. Recommend adding a
"Response Format" section to the SQL Tool chapter with both shapes.

---

## 7. Connection Management

Source: `sqltool/connection_manager.py` (340 lines) +
`sqltool/external_catalog_manager.py` (119 lines).

### 7.1 External catalogs (oracledb)

For `catalogType=EXTERNAL`:

1. Look up the catalog's credentials via the AIDP credential broker
   (wallet + user + password + TNS descriptor).
2. Open a pooled oracledb connection scoped to
   `(datalake_id, catalog_key, schema_key)`.
3. Cache the pool. TTL is the first of:
   `context["sql_conn_ttl_seconds"]` → env `SQL_CONN_TTL_SECONDS` →
   default `3600` seconds.
4. On TTL expiry or on `ORA-01017`/`ORA-12537`-class errors,
   refresh the pool transparently.

Service metrics emitted:

- `sqltool.connection.pool.success.total` / `…failure.total`
- `sqltool.connection.refresh.success.total` / `…failure.total`

### 7.2 Standard catalogs (Spark)

For `catalogType=STANDARD`, `_SparkSQLExecutor` handles its own
connection setup against the workspace's Spark cluster gateway. No
oracledb pool involved.

---

## 8. Logging and Observability

Source: `sql.py` (logging section) + `aidputils.agents.observability`.

| Log event name | Emitted when |
|---|---|
| `sql.query.execution.status` | Every query, with the outcome |
| `sql.row_limit.config` | After `_LimitProcessor` resolves the effective limit |
| `sql.row_limit.applied` | When truncation actually happens |
| `sql.query.success` | After a successful row fetch |

Camel-case fields used throughout: `rowsFetched`, `rowsAffected`,
`resultsTruncated`, `rowLimit`, `catalogType`, `catalogKey`,
`schemaKey`, `clusterKey`, `query`. Fields containing the SQL query
and OCIDs are full-value (scrubbed); other fields are summarized.

Metric histograms / counters in addition to the connection ones above:

- `sqltool.query.success` / `sqltool.query.failure` (per query)
- `sqltool.invoke.latency_ms` (per-invocation histogram)

---

## 9. Testing — AI Compute Requirement

Per PDF p. 224:

> Testing a tool requires that your agent flow is attached to an AI
> compute. An AI compute is attached if the AI Compute label is green
> with the selected AI compute in an ACTIVE state.

The Test tab is the only built-in way to exercise a SQL Tool with
real parameter values. The panel renders the tool's `inputSchema`
fields and submits them as `runtime_params`. Output is shown raw
(success envelope) or formatted (error envelope).

> ⚠️ A separate friction point — the Test panel currently renders
> only field name + a generic "Enter" placeholder; it ignores
> `description`, `default`, `examples`, and `enum` from `inputSchema`.
> See [AIDP_FEEDBACK.md → Issue 1](AIDP_FEEDBACK.md).

---

## 10. Comparison to Sibling Custom Code Tools

The AIDP SQL Tool is a first-party tool. The repo at
[brentonmizell/aidp-custom-tools](https://github.com/brentonmizell/aidp-custom-tools)
ships three companion Custom Code tools that operate at adjacent layers:

| Tool | Layer | Use when |
|---|---|---|
| **AIDP SQL Tool** (first-party) | SQL execution against a catalog | The agent (or developer) wrote the SQL and just needs it run. Works for both EXTERNAL (oracledb) and STANDARD (Spark) catalogs. |
| **aidp_catalog_toolkit.CatalogFileTool / VolumeWriteTool** | File IO on standard catalog volumes (and workspace files) | Reading/writing files in a Standard catalog volume or the workspace tree. NOT a SQL tool. |
| **selectai_toolkit.NL2SQLTool** | Natural-language → SQL on Oracle ADB | The user asks a question in English; `DBMS_CLOUD_AI.GENERATE` produces and runs the SQL. Oracle ADB only. |
| **selectai_toolkit.SelectAIProvisionTool** | One-time setup for NL2SQLTool | Provisioning a Select AI profile + agent tool against a fixed table set. |

Decision shortcuts for the agent:

- *User has a SQL string they want run* → AIDP SQL Tool.
- *User wrote a Spark SQL query against a STANDARD catalog* → AIDP SQL
  Tool with `catalogType=STANDARD`.
- *User asks a question in English about an Oracle ADB* →
  selectai_toolkit.NL2SQLTool.
- *User needs to read a PDF/CSV from a Standard catalog volume* →
  aidp_catalog_toolkit.CatalogFileTool.

The Custom Code tools are intentionally **not** substitutes for the
first-party SQL Tool — they fill in NL2SQL and file IO surfaces the
SQL Tool was never intended to cover.

---

## 11. Documentation Gaps — Recommended Updates to the PDF

The November LA PDF's SQL Tool chapter (pp. 222-225) should be
updated with:

| # | Gap | Recommended action |
|---|---|---|
| 1 | The note "only external catalogs" contradicts the code's STANDARD/Spark SQL path. | Replace with the dispatch description from §3.3 above. |
| 2 | `catalogType` config field is undocumented. | Add to Table 20-3. |
| 3 | `isRowLimitEnabled` and `maxRows` are undocumented. | Add to Table 20-3 with the default cap reference. |
| 4 | Success and error envelope shapes are undocumented. | Add a "Response Format" subsection with the JSON examples from §6. |
| 5 | Session-variable placeholders `[[name]]` are undocumented (only `{{name}}` is in the PDF). | Add to §4 of the SQL Tool chapter. |
| 6 | Spark SQL grammar for STANDARD-catalog queries is undocumented and not cross-linked. | Cross-link to [docs.oracle.com — Table SQL Grammar](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aidug/table-sql-grammar.html). |
| 7 | Test-panel rendering of `inputSchema` metadata is incomplete. | Tracked separately in [AIDP_FEEDBACK.md → Issue 1](AIDP_FEEDBACK.md). |

---

## 12. References

### 12.1 Oracle documentation

- *Oracle AI Data Platform Workbench User's Guide* — SQL Tool section:
  Chapter 20, pp. 222-225 (current PDF: "Nov LA"). Sample code at
  pp. 297-302.
- [docs.oracle.com — Table SQL Grammar Reference](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aidug/table-sql-grammar.html) —
  Spark SQL grammar for catalog DDL (referenced by the STANDARD path).
- [docs.oracle.com — AI Data Platform Workbench REST API](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aiwap/index.html) —
  REST API index (use for future endpoint references).

### 12.2 Source code (datahub repo)

| File | Lines | Purpose |
|---|---|---|
| `aidp-utils/src/aidputils/agents/tools/sql.py` | 1,141 | Tool entry point, catalogType dispatch, metrics |
| `aidp-utils/.../tools/sqltool/connection_manager.py` | 340 | External-catalog connection pool + refresh |
| `aidp-utils/.../tools/sqltool/external_catalog_manager.py` | 119 | External-catalog-specific helpers |
| `aidp-utils/.../tools/sqltool/spark_sql_executor.py` | 1,125 | Standard-catalog Spark SQL execution path |
| `aidp-utils/.../tools/sqltool/query_executor.py` | 275 | Cursor + truncation logic |
| `aidp-utils/.../tools/sqltool/limit_processor.py` | 130 | Row-limit resolution |
| `aidp-utils/.../tools/sqltool/row_limit_planner.py` | 204 | Pre-execution row-limit planning |
| `aidp-utils/.../tools/sqltool/error_handler.py` | 392 | MCP error envelope builder |
| `aidp-utils/.../tools/utils.py` | 233 | `resolve_session_variable_references`, `SystemUtils`, `HttpUtil` (ported into this repo's `aidp_session/aidp_session.py`) |

### 12.3 In this repo

- [`UI_HINTS_SPEC.md`](UI_HINTS_SPEC.md) — the `_uiHints` sidecar contract
  the Custom Code tools use to declare dropdowns and field shapes the
  Test panel doesn't render yet.
- [`AIDP_FEEDBACK.md`](AIDP_FEEDBACK.md) — the broader feedback ticket
  for the AIDP team, including the Test-panel rendering ask (Issue 1)
  and the catalog-picker request (Issue 2).
- [`aidp_io/aidp_io.py`](aidp_io/aidp_io.py) — shared helper module with
  the catalog-connection dispatch mirroring `connection_manager.py`'s
  pattern (`get_standard_catalog_connection`, `get_external_catalog_connection`,
  `get_connection`).
- [`aidp_session/aidp_session.py`](aidp_session/aidp_session.py) — port of
  `resolve_session_variable_references` and friends, standalone (no
  AIDP-internal imports).
- [`CUSTOM_CODE_TOOLS/selectai_toolkit/`](CUSTOM_CODE_TOOLS/selectai_toolkit/) —
  the NL2SQL Custom Code tool for Oracle ADB.

---

## Changelog

- *2026-06-19*: Initial integrated draft. Reproduces the November LA PDF's
  SQL Tool chapter, surfaces the STANDARD-catalog Spark SQL path
  (undocumented in the PDF, implemented in code), adds the row-limit /
  output-envelope / error-envelope / session-variable sections from the
  source, and positions the tool against the Custom Code companions.
