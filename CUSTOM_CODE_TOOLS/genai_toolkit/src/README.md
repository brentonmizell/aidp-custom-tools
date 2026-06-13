# genai_toolkit

LLM-as-judge rubric scorer, map-reduce summarizer, and OCI GenAI embeddings.

Built on the AIDP Custom Tools framework. Each tool class is registered with
`@CustomToolBase.register` and configured in `tool_config.json`.

## Tools

- **RubricScorerTool** - LLM-as-judge structured scoring with bounded prompts.
- **SummarizerTool** - map-reduce summarization with input + chunk caps and
  `truncated` flag when caps are hit.
- **EmbeddingTool** - OCI GenAI embeddings (`embed_text`) with input-type hint
  and per-batch / per-item caps.

## Build
```bash
zip -r genai_toolkit.zip tool_implementation.py tool_config.json requirements.txt utils/ -x "*__pycache__*" "*.pyc"
```

## Notes

- Config values are read via `utils/config_utils.get_cfg`, which unwraps
  `conf["conf"]` and coerces template-stringified numbers.
- All three tools share a single OCI GenAI client builder
  (`utils/llm_utils.build_oci_genai_client` / `build_llm`) so endpoint /
  compartment / auth resolution is consistent. Endpoint is auto-derived from
  `region`; `model_provider` is auto-set from `model_id` (`cohere.*` -> `cohere`,
  everything else -> `generic`).
- Standardized return envelope:
  - success: `{"ok": true, "data": {...}, <legacy keys preserved>}`
  - error: `{"ok": false, "error": "...", "error_type": "..."}`
- `truncated: true` appears in the response whenever a byte / chunk / batch cap
  is hit so callers can detect partial results.
- The Debug Channel (`aidp_debug`) is imported with a no-op fallback so the
  package still works when the runtime doesn't inject it.

## UI hints

`tool_config.json` includes a `_uiHints` block per tool entry per
`UI_HINTS_SPEC.md`: `region` (regions), `endpoint` (readonly, derived),
`compartment_id` (compartments), `model_id` (ociGenAiModels, depends on
region), `model_provider` (readonly, derived from model_id).
