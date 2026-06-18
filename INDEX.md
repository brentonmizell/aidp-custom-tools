# AIDP Custom Tools — Curated Set

Twelve packages, 24 tools. Functional tools that bring real value to AIDP. Each
was verified to register, import, and run (live-service calls stubbed where a
backend is required). One ZIP = one isolated deploy venv.

## Packages

| Package | Tools | Deps beyond base runtime | Demo-safe (no network)? |
|---------|-------|--------------------------|--------------------------|
| `data_ops_toolkit` | FilterTool, CompareTool, DataManipulationTool | pandas | Filter/Compare yes; DataManipulation needs pandas |
| `text_utils_toolkit` | TemplateRenderTool, RegexTool, JsonTransformTool | jinja2, jsonpath-ng | Yes |
| `compute_toolkit` | MathTool, SchemaValidatorTool | none (numpy, jsonschema pre-installed) | Yes |
| `genai_toolkit` | RubricScorerTool, SummarizerTool | none (langchain-core pre-installed) | Needs OCI GenAI |
| `convert_file_tool` | ConvertFileTool | pandas, pyarrow, openpyxl | CSV/JSON yes; Parquet/Excel need deploy |
| `web_toolkit` | WebFetchTool, WebhookSenderTool | readability-lxml, beautifulsoup4 | Needs network |
| `object_storage_tool` | ObjectStorageTool | none (oci pre-installed) | Needs OCI |
| `email_toolkit` | SmtpEmailTool | none (stdlib) | Needs SMTP relay |
| `aidp_catalog_toolkit` | CatalogFileTool, VolumeWriteTool, CatalogBrowserTool, KBIngestTool, WorkspaceFileTool | none (oci, requests pre-installed) | Needs AIDP Data Lake API + RP |
| `document_extract_tool` | DocumentExtractTool | pypdf, pdfplumber, python-docx | text/csv yes; pdf/docx need deploy |
| `python_runner_tool` | RunPythonTool, RunNotebookTool | websocket-client (for RunNotebook) | RunPython yes; RunNotebook needs AIDP kernel |
| `selectai_toolkit` | SelectAIProvisionTool, NL2SQLTool | oracledb | Needs ADB + OCI GenAI |
| `custom_tool_template` | (template, not a tool) | — | — |

## The AIDP-native set (aidp_catalog_toolkit + selectai_toolkit) — highest strategic value
Built against the real AIDP Data Lake REST API (the same calls the Flow Designer
extension uses), resource-principal signed:
- **CatalogFileTool** — read/list Standard Catalog volume files.
- **VolumeWriteTool** — write files back into a volume.
- **CatalogBrowserTool** — discover catalogs/schemas/tables/volumes/KBs; describe a table.
- **KBIngestTool** — list/trigger KB ingestion jobs.
Together they close the RAG loop: write files -> trigger ingestion -> the KB is
queryable. File meta actions send the file `path` as an HTTP header (verified
against the extension) and use the pre-authenticated-URL (parUrl) pattern.
`selectai_toolkit` closes the NL2SQL loop alongside `aidp_catalog_toolkit`:
CatalogBrowser surfaces the tables, SelectAIProvision wires up an Autonomous
Database SELECT AI profile, and NL2SQL turns a natural-language question into a
governed SQL answer against those same catalog assets.

## Built-in quality characteristics (every tool)
- `get_cfg` unwraps `conf["conf"]` and coerces template-stringified numbers.
- `{"error": "..."}` on every failure path so the framework sets `isError`.
- OCI-native tools use resource-principal auth; no keys handled.
- HTTP tools use the framework `_make_http_request` (SSRF protection + auth) where applicable.
- Safe math via AST (no eval); SQL/identifier guards where relevant.

## Map to the kanban
- DataManipulation/Compare/Convert → Compare Dataset / Convert File node prototypes.
- RubricScorer → optimizer eval-layer building block.
- WebFetch → bridge until native Web Search ships (governed HTTP path).
- WebhookSender + SmtpEmail → notification halves of Human in the Loop.
- aidp_catalog_toolkit → File Operations PRD (DATAHUB-30353) read+write,
  Catalog Browser, WorkspaceFile (read/list workspace files via the Jupyter
  Contents API), and KB ingestion (pairs with Triggers V1).
- document_extract_tool → File Ops extraction reqs: PDF/DOCX/CSV/text to clean
  text. Pairs with the file tools (fetch -> extract) and RAG prep.
- RunPython → standalone Custom Code (DATAHUB-25052).
- RunNotebook → run a workspace .ipynb on the AIDP kernel (reuses the Spark
  tool's signed-WebSocket notebook protocol; real Spark/datalake context).
- selectai_toolkit → NL2SQL on Autonomous Database via SELECT AI; pairs with
  CatalogBrowser to point profiles at governed tables.
