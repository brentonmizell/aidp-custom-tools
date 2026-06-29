# compute_toolkit

Safe arithmetic + statistics (no eval), and JSON Schema validation.

Built on the AIDP Custom Tools framework. Each tool class is registered with
`@CustomToolBase.register` and configured in `tool_config.json`.

## Credentials
**None required.** This toolkit runs entirely in-process: arithmetic, statistics,
and JSON Schema validation. No network calls. Leave `conf.credential_name` empty.
For the toolkit-wide auth reference see [`../CREDENTIALS.md`](../CREDENTIALS.md).

## Build
```bash
zip -r compute_toolkit.zip tool_implementation.py tool_config.json requirements.txt utils/ -x "*__pycache__*" "*.pyc"
```

## Notes
- Config values are read via `utils/config_utils.get_cfg`, which unwraps `conf["conf"]` and coerces template-stringified numbers.
- Tools return `{"error": "..."}` on failure so the framework sets `isError`.
- See the package source for per-tool input schema and config keys.
