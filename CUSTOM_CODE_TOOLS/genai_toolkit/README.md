# genai_toolkit

LLM-as-judge rubric scorer, map-reduce summarizer, and OCI GenAI embeddings.

Built on the AIDP Custom Tools framework. Each tool class is registered with
`@CustomToolBase.register` and configured in `tool_config.json`.

## Credentials

These tools call **OCI Generative AI** (`inference.generativeai.<region>.oci.oraclecloud.com`).

- **OCI GenAI typically works under resource principal** when the agent
  deployment's dynamic group has the GenAI IAM policy. If that's already set up
  in your environment, leave `conf.credential_name` empty — no Credential Store
  entry needed.
- **If you hit 401 / NotAuthorized** against OCI GenAI (the rest of the AIDP
  data-plane pattern from the Jun-17 thread), switch to the Credential Store:
  create a `SECRET_TOKEN` credential with keys `tenancy` / `user` /
  `fingerprint` / `private_key`, set `conf.credential_name` to its display
  name. The tool will resolve the bundle via `aidputils.secrets.get(name)` and
  sign with `oci.signer.Signer(private_key_content=...)`.

Full pattern: [`../CREDENTIALS.md`](../CREDENTIALS.md) and the working reference
at [`../credential_store_auth_sample/`](../credential_store_auth_sample/README.md).

## Build
```bash
zip -r genai_toolkit.zip tool_implementation.py tool_config.json requirements.txt utils/ -x "*__pycache__*" "*.pyc"
```

## Notes
- Config values are read via `utils/config_utils.get_cfg`, which unwraps `conf["conf"]` and coerces template-stringified numbers.
- Tools return `{"error": "..."}` on failure so the framework sets `isError`.
- See the package source for per-tool input schema and config keys.
