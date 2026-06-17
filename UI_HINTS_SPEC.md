# Tool Config UI Hints Spec

This is the contract our extension (and any other consumer) uses to render
catalog-backed dropdowns and static enums in the AIDP custom-tool config UI.

AIDP's public custom-tool spec treats `config` and `inputSchema` as opaque
`dict(str, object)`. The fields below are **additive**: AIDP ignores
unknown keys, our extension reads them. Packages remain valid against the
upstream SDK.

## Two mechanisms

### B. JSON Schema `enum` (for static value sets)

Use vanilla JSON Schema on `schema[]` entries. Standard, portable.

```json
{
  "name": "operation",
  "type": "string",
  "description": "What to do.",
  "enum": ["get", "list"],
  "default": "get"
}
```

Use when the value set is fixed and known at design time (modes, file
formats, log levels, etc).

### A. `_uiHints` sidecar (for catalog-backed / dynamic dropdowns)

Add a top-level `_uiHints` block per tool entry, mirroring `conf` and
`schema` field names. Each entry declares the picker kind + source.

```json
{
  "toolClassName": "CatalogFileTool",
  "conf": {
    "catalog": "",
    "schema": "",
    "volume": "",
    "timeout": 30
  },
  "schema": [
    { "name": "path", "type": "string", "description": "..." }
  ],
  "_uiHints": {
    "conf": {
      "catalog": { "kind": "dropdown", "source": "catalogs" },
      "schema":  { "kind": "dropdown", "source": "schemas", "dependsOn": "catalog" },
      "volume":  { "kind": "dropdown", "source": "volumes", "dependsOn": "schema" }
    },
    "schema": {
      "catalog": { "kind": "dropdown", "source": "catalogs" },
      "schema":  { "kind": "dropdown", "source": "schemas", "dependsOn": "catalog" },
      "volume":  { "kind": "dropdown", "source": "volumes", "dependsOn": "schema" }
    }
  }
}
```

### Hint entry fields

| Field | Type | Description |
|---|---|---|
| `kind` | string | One of `"dropdown"`, `"enum"`, `"readonly"`, `"secret"`. See below. |
| `source` | string | (dropdown only) one of the sources below. |
| `dependsOn` | string | (optional) field whose value filters this dropdown. See *Reference resolution* below. |
| `values` | array | (enum only) static value set, alternative to JSON-Schema enum on the `schema[]` entry. |
| `displayField` | string | (optional) for object sources, what field to show. Default: `displayName`. |
| `valueField` | string | (optional) for object sources, what field to store. Default: `key`. |
| `multi` | boolean | (optional) allow multi-select. Default: false. |
| `placeholder` | string | (optional) shown when empty. |
| `inputStyle` | string | (optional) `"singleline"` (default) or `"multiline"`. Controls whether the extension UI renders a one-line `<input type="text">` or a resizable `<textarea>` (min 6 rows, monospace). See *Input style* below. |

### Hint `kind` semantics

- **`dropdown`** — render as a select populated from `source`. Requires `source`. May filter by `dependsOn`.
- **`enum`** — render as a select with a fixed set in `values` (or equivalently `enum` on the JSON-Schema entry). Static, no API call.
- **`readonly`** — render as a non-editable display. Useful for derived fields (e.g. an endpoint URL computed from `region`, or an auto-detected namespace).
- **`secret`** — render as a masked input (password-style). Extension UI MUST mask the value in logs, conf editors, and copy-to-clipboard. Used for tokens, API keys, private keys, webhook URLs that act as bearer tokens.

### Input style (`inputStyle`)

Long-form fields (Python source, JSON blobs, SQL, rubric templates) are
unusable in a one-line `<input>`. The optional `inputStyle` key on any
hint entry controls the input element used by the extension UI.

| Value | Render |
|---|---|
| `"singleline"` | one-line `<input type="text">` (default) |
| `"multiline"` | resizable `<textarea>` (min 6 rows, monospace) |

`inputStyle` lives at the same nesting level as `kind` / `source` /
`dependsOn` — inside `_uiHints.conf[fieldName]` or
`_uiHints.schema[fieldName]`. `kind` MAY be omitted when the only
purpose of the hint is `inputStyle` (the extension treats absent `kind`
as `freeform`, equivalent to no hint at all except for the multiline
override).

```json
"_uiHints": {
  "schema": {
    "code":     { "kind": "freeform", "inputStyle": "multiline" },
    "language": { "kind": "enum", "values": ["python", "sql"] }
  },
  "conf": {
    "system_prompt":     { "inputStyle": "multiline" },
    "response_template": { "inputStyle": "multiline" }
  }
}
```

#### Default-multiline field names (LONGFORM list)

When generating `_uiHints` (build step, starter zip, `new-tool`
scaffold), the following field names get `"inputStyle": "multiline"`
automatically:

```
code, content, body, template, template_text, expression, query, sql,
rubric_template, rubric, message, prompt, text, document, json,
json_input, json_data, response_template, json_schema, schema, data,
html, markdown
```

Match is case-insensitive, **exact** match on the field name (NOT
substring — `query_id` stays singleline). An explicit
`"inputStyle": "singleline"` in the source `tool_config.json` always
wins over the auto rule.

#### Interaction with `kind`

- `kind: "secret"` + `inputStyle: "multiline"` — valid (PEM private
  keys). UI renders a masked `<textarea>` that does not echo characters
  in DOM and is excluded from copy-to-clipboard.
- `kind: "dropdown"` + `inputStyle: "multiline"` — illegal; build step
  warns and drops `inputStyle`. Dropdowns are always singleline.
- `kind: "readonly"` + `inputStyle: "multiline"` — valid. Renders a
  scrollable `<pre>` block.
- Old extension versions without `inputStyle` support silently fall
  back to singleline. Field still works, just cramped. No errors.

### Supported `source` values

| Source | API hint | Depends on |
|---|---|---|
| `regions` | Static list of OCI regions where AIDP is available. | — |
| `dataLakes` | `GET /aiDataPlatforms` | — |
| `catalogs` | `GET /aiDataPlatforms/{lakeOcid}/catalogs` | — |
| `schemas` | `GET /aiDataPlatforms/{lakeOcid}/catalogs/{catalogKey}/schemas` | `catalog` |
| `tables` | `GET /aiDataPlatforms/{lakeOcid}/catalogs/{catalogKey}/schemas/{schemaKey}/tables` | `schema` |
| `volumes` | `GET /aiDataPlatforms/{lakeOcid}/catalogs/{catalogKey}/schemas/{schemaKey}/volumes` | `schema` |
| `knowledgeBases` | `GET /aiDataPlatforms/{lakeOcid}/knowledgeBases` | — |
| `jobs` | `GET /aiDataPlatforms/{lakeOcid}/knowledgeBases/{kbKey}/jobs` | `kb_key` |
| `workspaces` | `GET /aiDataPlatforms/{lakeOcid}/workspaces` | `lake_ocid` |
| `clusters` | `GET /aiDataPlatforms/{lakeOcid}/workspaces/{workspaceKey}/clusters` | `workspace_key` |
| `notebookFiles` | Workspace file browser filtered to `*.ipynb`. | `workspace_key` |
| `compartments` | OCI ListCompartments (tenancy-scoped). | — |
| `ociGenAiModels` | Static catalog from `.aidp/knowledge/oci-genai-models.md`, filtered by region. | `region` |
| `ociBuckets` | OCI Object Storage ListBuckets. | — |

### Reference resolution for `dependsOn`

When the same field name appears in both `conf` and `schema`, use a
prefixed reference to disambiguate. Bare names resolve to the same
section first (i.e. a hint in `_uiHints.conf.X` looks in `conf` first
for `dependsOn`, then falls back to `schema`).

- `"dependsOn": "catalog"` — same-section first.
- `"dependsOn": "conf.catalog"` — explicit conf reference.
- `"dependsOn": "schema.catalog"` — explicit schema reference.

### Forward compatibility

- AIDP's console / SDK ignore `_uiHints` (it's an unknown key in an
  opaque dict). Tools remain deployable today.
- If AIDP adds a native dropdown contract later, this spec maps 1:1
  onto the obvious shape.

## Quality bar for every tool (cross-package)

In addition to dropdowns, every tool should converge on:

1. **Structured return envelope** — `{ "ok": true|false, "data": ..., "error": "...", "error_type": "..." }`. Errors always set `ok=false`.
2. **`get_cfg` unwrap** — read every conf value via `get_cfg(conf, key, default)`; never read `conf["foo"]` directly (handles the framework's nested `conf["conf"]` shape + coerces stringified numbers).
3. **Debug Channel** — `from aidp_debug import debug, debug_warn, debug_error, DebugLog`; emit at least one `debug()` per execution path; `DebugLog.embed(result)` before returning.
4. **Byte caps** — every read/fetch/load path bounded by an explicit limit, with `truncated: true` flag in the response when hit.
5. **Timeouts and retries** — split `connect_timeout`/`request_timeout` where applicable; bounded retry on 429/5xx for network calls.
6. **No silent fallbacks for destructive ops** — writes/deletes MUST require explicit identifiers (no "first catalog" auto-pick).
