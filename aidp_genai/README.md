# aidp_genai

Shared OCI Generative AI helper for AIDP custom tools. Same role as
`aidp_io` but for LLM chat and embedding calls instead of file IO.

Centralizes auth (`resource_principal` / `user_principal` /
`instance_principal`), endpoint construction, and the per-provider
request shapes so individual tool packages don't each re-implement
them.

## Location and sync

- **Canonical source**: `aidp_genai/aidp_genai.py` (this folder)
- **Per-package copy**: `CUSTOM_CODE_TOOLS/<pkg>/src/utils/aidp_genai.py`
- The build step (`setup.py build`) copies the canonical file into every
  package's `utils/` directory, identical to the `aidp_io` flow.
  Stale copies are overwritten on every build.

## API

```python
chat(prompt: str, conf: dict, *,
     model_id: Optional[str] = None,
     system:   Optional[str] = None,
     max_tokens:  int   = 800,
     temperature: float = 0.2) -> str

chat_messages(messages: list[dict], conf: dict, *,
              model_id:    Optional[str] = None,
              max_tokens:  int   = 800,
              temperature: float = 0.2) -> str
# messages: [{"role": "user"|"assistant"|"system", "content": "..."}, ...]

embed(texts: str | list[str], conf: dict, *,
      model_id:   Optional[str] = None,
      input_type: str = "search_document") -> list[list[float]]
```

- `chat` returns the assistant's text content only.
- `chat_messages` accepts a list of role/content dicts. The system
  message is sent as `preamble_override` for Cohere models and as a
  `SystemMessage` for generic-API providers.
- `embed` always returns `list[list[float]]`. A single-string input
  returns a list of length 1. An empty input list short-circuits to `[]`
  without calling the API.

## Conf keys consumed

| Key | Purpose |
|---|---|
| `region` | OCI region short code (also `OCI_REGION` env) |
| `compartment_id` | compartment OCID for inference (also `OCI_COMPARTMENT_ID` env) |
| `auth_mode` | `resource_principal` (default) / `user_principal` / `instance_principal` |
| `tenancy_ocid`, `user_ocid`, `fingerprint`, `private_key_content`, `pass_phrase` | user_principal only |
| `model_id` | default model when call-site omits `model_id=` |
| `endpoint` | explicit override; otherwise computed from `region` |

### Defaults

- **Endpoint**: `https://inference.generativeai.<region>.oci.oraclecloud.com`
- **Chat model fallback**: `cohere.command-r-plus`
- **Embed model fallback**: `cohere.embed-english-v3.0`
- **Provider auto-detect**: `cohere.*` -> `'cohere'`; anything else
  (`xai.*`, `meta.*`, `openai.*`, `google.*`) -> `'generic'`.

Model selection order: explicit `model_id=` kwarg ->
`conf['model_id']` -> per-function fallback above.

## 5-line example

```python
from utils.aidp_genai import chat, embed

summary = chat("Summarize: " + long_text, conf, max_tokens=400)
vectors = embed(["hello world", "another doc"], conf)
print(summary, len(vectors), len(vectors[0]))
```

## Error contract

All failures raise `ValueError` shaped:

```
GenAI <call> failed (HTTP <status>): <body[:1024]>
```

Tool wrappers should catch and translate to the standard
`{"ok": false, "error": ..., "error_type": ...}` envelope. The
`error_type` is typically `"ValueError"`, but callers can pass their
own type from the exception class name.

## Edge cases

- **Empty `texts` to `embed`** -> returns `[]`, no API call.
- **`temperature` outside `[0.0, 2.0]`** -> clamped silently, with a
  `debug_warn` if the debug channel is loaded.
- **System message with Cohere** -> sent as `preamble_override`.
- **System message with generic providers** -> sent as a
  `SystemMessage` in the messages array.
- **`oci` not installed at import time** -> fine; imports are lazy and
  happen only inside the call paths.

## Reuse rule

`aidp_genai` tries to import `aidp_io._build_signer` first (since both
helpers are normally synced together into the same `utils/` folder). It
only falls back to its in-file copy when `aidp_io` is unreachable.
This keeps auth-mode behavior in lockstep across both modules.
