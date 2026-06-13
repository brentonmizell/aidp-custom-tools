# Testing — document_extract_tool

Upload **document_extract_tool.zip** in the workspace file browser, add a Custom Tool node, set the Tool Class Name, then use the Test panel with the values below. Mirror the Display Name / Description / Input Schema from the package's `tool_config.json`.

> Prerequisite: text/csv work as-is; PDF/DOCX need parsers (deploy).

## DocumentExtractTool
Extract text from PDF / DOCX / CSV / text.

**Test 1: CSV (inline text)**

| Field | Value |
|-------|-------|
| `content` | `(paste sample.csv)` |
| `format` | `csv` |

Expected: columns + rows returned.

**Test 2: PDF/DOCX**

| Field | Value |
|-------|-------|
| `content_base64` | `(base64 of sample.pdf or sample.docx)` |
| `format` | `pdf` |

Expected: extracted text. Tip: CatalogFileTool/WorkspaceFile can give you content_base64 directly; chain fetch -> extract.

## Mock files (in this folder's mock_files/)
- `sample.csv`
- `sample.pdf`
- `sample.docx`

---
Pass = the documented keys come back; any failure returns `{"error": ...}`. If the agent later says tool use is "forbidden", start a fresh chat session.