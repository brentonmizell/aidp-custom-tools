# AIDP Custom Tools

A collection of custom code tools for Oracle AI Data Platform (AIDP) agent
flows. Each subdirectory is one deployable toolkit (a `.zip` built from its
`src/`); each toolkit registers one or more tool classes via
`@CustomToolBase.register` and declares their schema in `tool_config.json`.

See also:
- [`CREDENTIALS.md`](CREDENTIALS.md) — the standard 5-key AIDP credential model.
- [`AIDP_KEY_MODEL.md`](AIDP_KEY_MODEL.md) — how catalog / schema / volume / KB
  keys and the two REST surfaces (`aiDataPlatforms` vs `dataLakes`) work.

## Toolkit index

| Toolkit | Tools | What it does |
|---|---|---|
| `aidp_catalog_toolkit` | CatalogFileTool, VolumeWriteTool, CatalogBrowserTool, CatalogMapTool, KBIngestTool, WorkspaceFileTool | Read/write catalog volume & workspace files, browse/map catalog metadata, trigger KB ingests. |
| `selectai_toolkit` | SelectAIProvisionTool, NL2SQLTool, CatalogMapTool, ConversationTool, SyntheticDataTool, VectorIndexTool | Full Oracle Select AI (DBMS_CLOUD_AI): NL2SQL, chat, synthetic data, RAG. |
| `python_runner_tool` | RunPythonTool, RunNotebookTool, RunWorkflowTool | Run sandboxed Python, a workspace notebook, or an AIDP job/workflow. |
| `hitl_approval_tool` | LookupUserTool, OpenIncidentTool, GetIncidentTool, UpdateIncidentTool, OpenApprovalTool, ResolveApprovalTool | Human-in-the-loop incident + approval workflow. |
| `genai_toolkit` | RubricScorerTool, SummarizerTool, EmbeddingTool | Score against a rubric, map-reduce summarize, embed text via OCI GenAI. |
| `data_ops_toolkit` | DataManipulationTool | Reshape/filter/compare tabular records (`operation`: select/rename/sort/dedupe/groupby/filter/compare). |
| `text_utils_toolkit` | TextUtilTool | Jinja2 templating, regex, and JSONPath (`operation`: template/regex/json). |
| `email_toolkit` | SmtpEmailTool, ImapReadTool | Send SMTP mail, read an IMAP inbox. |
| `web_toolkit` | WebFetchTool, WebhookSenderTool | Fetch readable page text, post to Slack/Teams/HTTP webhooks. |
| `compute_toolkit` | MathTool, SchemaValidatorTool | Exact arithmetic/stats, JSON Schema validation. |
| `object_storage_tool` | ObjectStorageTool | List/read/write/delete objects in an OCI bucket. |
| `convert_file_tool` | ConvertFileTool | Convert between csv/json/jsonl/parquet/xlsx/tsv/txt. |
| `document_extract_tool` | DocumentExtractTool | Extract clean text/tables from PDF/DOCX/CSV/TXT/MD/JSON. |
| `test_credentials` | TestCredentialsTool | Diagnose a credential (resolved, keys present, signer builds, authenticates). |
| `runtime_probe` | RuntimeProbe | Introspect the live AIDP runtime (modules, versions). |
| `credential_store_auth_sample` | CredentialStoreAuthSample | Reference sample of the credential/signer + data-plane call pattern. |

## Shared AIDP utils

Every toolkit bundles a `src/utils/` package. Most modules are shared verbatim
across toolkits; a couple are specialized to one tool. `credential_resolver.py`
and `aidp_discovery.py` are kept canonical in [`_shared/`](_shared/) and copied
into each toolkit by [`_shared/sync.py`](_shared/sync.py) — **edit the `_shared/`
copy, then run `python _shared/sync.py`**, never edit a tool's copy directly.

### `config_utils.py` — config unwrapping + result envelopes
Pure Python, no network. The one module every tool uses.
- `get_cfg(conf, key, default)` — reads a config value, unwrapping the nested
  `conf["conf"]` shape and coercing template-stringified values (e.g. `"30"`
  from a `{{variable}}`) to the default's type so comparisons/arithmetic don't
  crash.
- `as_rows(value)` — coerce a list/JSON-string/`{rows|data|items|records}`
  wrapper into a list of dict rows; returns `(rows, error)`.
- `ok(data, **legacy)` / `fail(error, error_type, **extra)` — build the standard
  `{"ok": true, "data": {...}}` / `{"ok": false, "error": ...}` envelopes, with
  legacy top-level keys preserved for back-compat.

### `credential_resolver.py` — 5-key credential → OCI signer *(synced from `_shared/`)*
Resolves the standard AIDP credential (tenancy, user, fingerprint, private_key,
data_lake_ocid) out of the Credential Store / OCI Vault and builds a request
signer. See [`CREDENTIALS.md`](CREDENTIALS.md).
- `resolve_oci_signer(credential_name)` → `(signer, meta, error)` — the main
  entry point.
- `resolve_bundle(credential_name)` / `bundle_connection(credential_name)` —
  fetch the raw credential bundle.
- `enrich_conf_from_bundle(conf)` — fill `data_lake_ocid` + `region` into `conf`
  from the resolved bundle.
- `build_oci_signer_from_bundle(bundle)` — construct an `oci.signer.Signer`.
- `region_from_ocid(ocid)` / `resolve_region(explicit, ocid, conf_region)` —
  infer the OCI region from an OCID's region code.
- `normalize_fingerprint(value)` / `normalize_pem(value)` — validate/clean the
  key material; `mask(value, keep)` — redact secrets for debug output.

### `aidp_io.py` — AIDP data-plane file & metadata I/O
The catalog/workspace file layer. Understands the URI grammar
`master:<catalog>.<schema>.<volume>:/<path>` and `workspace:/<path>` (plus the
bare `<catalog>.<schema>.<volume>:/<path>` alias).
- Files: `parse_uri`, `read_file` / `write_file`, `read_text` / `write_text`,
  `list_files`.
- Metadata: `list_catalogs`, `list_schemas`, `list_tables`, `list_volumes`,
  `list_knowledge_bases`, `describe_table`; and `resolve_*` helpers
  (`resolve_catalog/schema/table/volume/kb`, `resolve_volume_key`) that turn
  names into the hex/dotted keys the REST API expects.
- KB ingests: `list_kb_jobs`, `trigger_kb_job_run`.
- DB connections: `get_connection` (dispatches by catalog type),
  `get_standard_catalog_connection`, `get_external_catalog_connection` — return
  `oracledb`-ready connection info (TNS/user/wallet) for a catalog binding.

### `aidp_discovery.py` — whole-lake discovery walk *(synced from `_shared/`)*
Self-contained catalog crawler so any tool can let an agent find data.
- `map_data_lake(conf, get_cfg, only_catalog="", timeout=30)` — walk catalogs →
  schemas → tables/volumes/KBs, returning a nested tree + text summary; volumes
  carry a `/Volumes/<catalog>/<schema>/<volume>` location.
- `build_client(conf, get_cfg)` — resolve `(signer, base, kb_base, error)` for
  the two REST surfaces from the standard credential.

### `aidp_kb.py` — knowledge base helper
- `kb_search(query, kb_key, conf)` — RAG-style semantic search against a KB.
- `list_kbs(conf)` — enumerate knowledge bases.
- `trigger_ingest(kb_key, job_key, conf)` — kick off a (re)index run.

### `aidp_genai.py` — OCI GenAI helper
- `chat(prompt, conf)` / `chat_messages(messages, conf)` — call an OCI GenAI
  chat model.
- `embed(texts, conf)` — get embedding vectors.

### `aidp_session.py` — runtime / session helpers
Runtime glue used by tools that read AIDP session context.
- `resolve_session_variable_references(value, session)` — expand `{{var}}`-style
  references against the injected session.
- `unwrap_exception_group_message(ex)` — flatten Python `ExceptionGroup`s into a
  readable message.
- Classes: `Constants` (shared literals), `SystemUtils`, `HttpUtil` (signed HTTP
  helpers).

### Specialized (single-toolkit) utils
- `genai_toolkit/…/llm_utils.py` — OCI GenAI client builders for LangChain:
  `build_llm`, `call_llm`, `build_oci_genai_client`, `estimate_tokens`.
- `python_runner_tool/…/oci_signer.py` — OCI request signing for the notebook
  REST + WebSocket calls: `get_auth_provider`, `sign_request`,
  `make_signed_request`.
- `python_runner_tool/…/jupyter_protocol.py` — binary Jupyter message
  encode/decode for the Spark notebook WebSocket kernel: `encode_binary_message`,
  `decode_binary_message`, `make_execute_request`, `make_kernel_info_request`.

## Building a toolkit

Each toolkit's `.zip` is the contents of its `src/` (excluding `__pycache__`):

```bash
cd <toolkit>/src
zip -r ../<toolkit>.zip tool_implementation.py tool_config.json requirements.txt README.md utils/ -x "*__pycache__*" "*.pyc"
```
