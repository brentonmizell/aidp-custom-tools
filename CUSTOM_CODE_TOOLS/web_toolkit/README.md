# web_toolkit

Fetch readable web text, and send Slack/Teams/generic webhook notifications.

Built on the AIDP Custom Tools framework. Each tool class is registered with
`@CustomToolBase.register` and configured in `tool_config.json`.

## Credentials

**Depends on what you're calling.**

- **Slack / Teams incoming webhooks** authenticate via the URL itself (the
  secret IS the URL). Store the webhook URL via the Credential Store rather
  than in conf:
  - Create a `SECRET_TOKEN` credential, key `webhook_url`, value the URL.
  - Set `conf.credential_name` to the display name. The tool reads
    `secrets.get(name)["webhook_url"]` at invoke time.
- **Generic webhook with bearer token** (e.g. an internal HTTP API): same
  pattern, but add a `bearer_token` key (and optional `base_url`).
- **Unauthed public fetches** (HTML / readable text): leave
  `conf.credential_name` empty.

Full pattern: [`../CREDENTIALS.md`](../CREDENTIALS.md) and the working reference
at [`../credential_store_auth_sample/`](../credential_store_auth_sample/README.md).

## Build
```bash
zip -r web_toolkit.zip tool_implementation.py tool_config.json requirements.txt utils/ -x "*__pycache__*" "*.pyc"
```

## Notes
- Config values are read via `utils/config_utils.get_cfg`, which unwraps `conf["conf"]` and coerces template-stringified numbers.
- Tools return `{"error": "..."}` on failure so the framework sets `isError`.
- See the package source for per-tool input schema and config keys.
