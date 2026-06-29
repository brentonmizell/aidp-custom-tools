# data_ops_toolkit

Filter records, diff two datasets, and reshape data (group-by/sort/dedupe via pandas).

Built on the AIDP Custom Tools framework. Each tool class is registered with
`@CustomToolBase.register` and configured in `tool_config.json`.

## Credentials
**None required for the ops themselves** — filter / diff / reshape run in-
process on JSON records you pass in.

If you're loading the records **from** an AIDP volume or catalog, that's the
fetcher tool's responsibility (CatalogFileTool, etc.) and **that** tool needs
a credential. Pipe `fetched_records → data_ops_toolkit`. See
[`../CREDENTIALS.md`](../CREDENTIALS.md).

## Build
```bash
zip -r data_ops_toolkit.zip tool_implementation.py tool_config.json requirements.txt utils/ -x "*__pycache__*" "*.pyc"
```

## Notes
- Config values are read via `utils/config_utils.get_cfg`, which unwraps `conf["conf"]` and coerces template-stringified numbers.
- Tools return `{"error": "..."}` on failure so the framework sets `isError`.
- See the package source for per-tool input schema and config keys.
