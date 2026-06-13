# document_extract_tool

Extract text from a document in a specific format: PDF, DOCX, CSV, or text.

## DocumentExtractTool
- Input: `content_base64` (binary files like PDF/DOCX) or `content` (text/CSV),
  plus `format` (or a `filename` to infer it).
- Output: extracted `text` (and `table_text` for DOCX, structured `rows` for CSV).
- PDF uses pdfplumber (better layout/tables) with a pypdf fallback.

## Pairs with the file tools
Fetching and extracting are separate on purpose, so one extractor works for any
source:
- Catalog volume file -> CatalogFileTool (returns content/content_base64) -> here.
- Workspace file -> WorkspaceFileTool -> here.
- Object Storage / HTTP -> their content -> here.

## Deps
Text/CSV need nothing. PDF/DOCX need pypdf, pdfplumber, python-docx (install at
deploy; bundle wheels for offline test pods).

## Build
```bash
zip -r document_extract_tool.zip tool_implementation.py tool_config.json requirements.txt README.md utils/ -x "*__pycache__*" "*.pyc"
```
