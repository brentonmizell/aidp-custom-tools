"""
aidp_genai - Shared AIDP GenAI helper for custom tools.

Centralizes OCI Generative AI chat + embedding calls so individual tool
packages don't each re-implement auth, endpoint construction, and the
chat/embed request shapes. Mirrors the design of ``aidp_io`` (same
``_build_signer`` dispatch, same ``conf`` dict, same lazy ``import oci``
discipline so non-LLM tools pay no import cost).

PUBLIC API
----------
    chat(prompt, conf, *, model_id=None, system=None,
         max_tokens=800, temperature=0.2)                 -> str
    chat_messages(messages, conf, *, model_id=None,
                  max_tokens=800, temperature=0.2)        -> str
    embed(texts, conf, *, model_id=None,
          input_type='search_document')                   -> list[list[float]]

CONFIG KEYS (conf dict, mirrors aidp_io / aidp_catalog_toolkit)
---------------------------------------------------------------
    region              OCI region short code (also OCI_REGION env)
    compartment_id      compartment OCID for inference calls
    auth_mode           "resource_principal" (default) | "user_principal"
                        | "instance_principal"
    tenancy_ocid        (user_principal only)
    user_ocid           (user_principal only)
    fingerprint         (user_principal only)
    private_key_content (user_principal only)
    pass_phrase         (user_principal only, optional)
    model_id            default model when call-site omits model_id=
    endpoint            optional explicit endpoint override

DEFAULTS
--------
    endpoint  -> https://inference.generativeai.<region>.oci.oraclecloud.com
    chat model fallback   -> 'cohere.command-r-plus'
    embed model fallback  -> 'cohere.embed-english-v3.0'
    provider auto-detect  -> model_id.startswith('cohere.') -> 'cohere'
                             everything else                -> 'generic'

ERROR CONTRACT
--------------
All errors raise ValueError with a message of shape
    "GenAI <call> failed (HTTP {status}): {body[:1024]}"
Tool wrappers should catch and translate to the standard
{"ok": false, "error": ..., "error_type": ...} envelope.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence, Union

DEFAULT_CHAT_MODEL = "cohere.command-r-plus"
DEFAULT_EMBED_MODEL = "cohere.embed-english-v3.0"

# Hard caps mirroring server-side behavior.
_TEMP_MIN = 0.0
_TEMP_MAX = 2.0


# ---------------------------------------------------------------------------
# Config helpers (mirrors aidp_io)
# ---------------------------------------------------------------------------

def _get_cfg(conf: Optional[Dict[str, Any]], key: str, default: Any = "") -> Any:
    """Tolerant config read: ``conf`` may be None / missing / hold falsy values."""
    if not conf:
        return default
    val = conf.get(key, default)
    if val is None or val == "":
        return default
    return val


def _build_signer(conf: Dict[str, Any]):
    """Build the OCI request signer for the configured auth_mode.

    Reuses ``aidp_io._build_signer`` when the helper is co-located on disk
    (build step always copies both into ``utils/``). Falls back to a
    hand-rolled mirror with identical semantics when ``aidp_io`` is not
    importable - keeps ``aidp_genai`` independently usable.
    """
    # Reuse path: when both helpers are synced into the same utils/ folder
    # ``aidp_io._build_signer`` already exists and is battle-tested.
    try:  # pragma: no cover - import-environment dependent
        from .aidp_io import _build_signer as _io_signer  # type: ignore
        return _io_signer(conf)
    except Exception:
        pass
    try:  # pragma: no cover - import-environment dependent
        from aidp_io import _build_signer as _io_signer  # type: ignore
        return _io_signer(conf)
    except Exception:
        pass

    # Fallback mirror - byte-for-byte the same dispatch as aidp_io.
    import oci  # lazy
    mode = (_get_cfg(conf, "auth_mode", "resource_principal")
            or "resource_principal").lower()
    if mode in ("user", "user_principal"):
        tenancy = _get_cfg(conf, "tenancy_ocid", "")
        user = _get_cfg(conf, "user_ocid", "")
        fingerprint = _get_cfg(conf, "fingerprint", "")
        key = _get_cfg(conf, "private_key_content", "")
        passphrase = _get_cfg(conf, "pass_phrase", "") or None
        missing = [
            k for k, v in (
                ("tenancy_ocid", tenancy),
                ("user_ocid", user),
                ("fingerprint", fingerprint),
                ("private_key_content", key),
            ) if not v
        ]
        if missing:
            raise ValueError(f"user_principal auth needs {missing} in config")
        return oci.signer.Signer(
            tenancy=tenancy, user=user, fingerprint=fingerprint,
            private_key_file_location=None,
            private_key_content=key, pass_phrase=passphrase,
        )
    if mode in ("instance", "instance_principal"):
        return oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    return oci.auth.signers.get_resource_principals_signer()


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------

def _resolve_region(conf: Optional[Dict[str, Any]]) -> str:
    region = _get_cfg(conf, "region", "") or os.environ.get("OCI_REGION", "")
    if not region:
        raise ValueError(
            "region is required (set conf['region'] or OCI_REGION env)"
        )
    return str(region)


def _resolve_endpoint(conf: Optional[Dict[str, Any]]) -> str:
    explicit = _get_cfg(conf, "endpoint", "")
    if explicit:
        return str(explicit)
    region = _resolve_region(conf)
    return f"https://inference.generativeai.{region}.oci.oraclecloud.com"


def _resolve_model(conf: Optional[Dict[str, Any]],
                   kwarg_model_id: Optional[str],
                   fallback: str) -> str:
    return (
        kwarg_model_id
        or _get_cfg(conf, "model_id", "")
        or fallback
    )


def _resolve_compartment(conf: Optional[Dict[str, Any]]) -> str:
    cid = (
        _get_cfg(conf, "compartment_id", "")
        or os.environ.get("OCI_COMPARTMENT_ID", "")
    )
    if not cid:
        raise ValueError(
            "compartment_id is required "
            "(set conf['compartment_id'] or OCI_COMPARTMENT_ID env)"
        )
    return str(cid)


def _detect_provider(model_id: str) -> str:
    """Return 'cohere' for cohere.* model IDs, 'generic' otherwise."""
    if not model_id:
        return "generic"
    return "cohere" if model_id.startswith("cohere.") else "generic"


def _clamp_temperature(temperature: float) -> float:
    """Clamp temperature into [0.0, 2.0]; warn via debug channel when adjusted."""
    try:
        t = float(temperature)
    except (TypeError, ValueError):
        t = 0.2
    if t < _TEMP_MIN or t > _TEMP_MAX:
        try:  # pragma: no cover - debug channel may not be present
            from aidp_debug import debug_warn  # type: ignore
            debug_warn(
                "aidp_genai: temperature clamped",
                requested=temperature, min=_TEMP_MIN, max=_TEMP_MAX,
            )
        except Exception:
            pass
        t = max(_TEMP_MIN, min(_TEMP_MAX, t))
    return t


def _build_client(conf: Dict[str, Any]):
    """Build a GenerativeAiInferenceClient using the resolved signer + endpoint."""
    import oci  # lazy
    signer = _build_signer(conf)
    endpoint = _resolve_endpoint(conf)
    # Same construction pattern across all three auth modes - the signer
    # already encodes the auth choice, the client just rides along.
    if isinstance(signer, oci.signer.Signer):
        # user_principal: config carries the user/tenancy/fingerprint already
        # set on the signer; pass empty config since the signer handles auth.
        return oci.generative_ai_inference.GenerativeAiInferenceClient(
            config={}, signer=signer, service_endpoint=endpoint,
        )
    return oci.generative_ai_inference.GenerativeAiInferenceClient(
        config={}, signer=signer, service_endpoint=endpoint,
    )


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------

def _raise_from_oci(call: str, exc: Exception) -> None:
    """Translate an OCI ServiceError (or other) into a ValueError per contract."""
    import oci  # lazy - only reached on the error path
    status = ""
    body = ""
    if isinstance(exc, oci.exceptions.ServiceError):
        status = str(getattr(exc, "status", "") or "")
        # OCI ServiceError stringifies to a useful diagnostic; cap to 1024.
        body = str(exc)
    else:
        body = f"{type(exc).__name__}: {exc}"
    if len(body) > 1024:
        body = body[:1024]
    raise ValueError(f"GenAI {call} failed (HTTP {status}): {body}") from exc


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

def chat(prompt: str, conf: Dict[str, Any], *,
         model_id: Optional[str] = None,
         system: Optional[str] = None,
         max_tokens: int = 800,
         temperature: float = 0.2) -> str:
    """Single-turn chat completion. Returns the assistant's text content."""
    if not isinstance(prompt, str):
        raise ValueError("prompt must be a string")

    messages: List[Dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    # chat_messages does the heavy lifting; system handling matches the
    # provider-specific contract there (Cohere -> preamble; generic ->
    # SYSTEM message).
    return chat_messages(
        messages, conf,
        model_id=model_id,
        max_tokens=max_tokens,
        temperature=temperature,
    )


def chat_messages(messages: Sequence[Dict[str, str]],
                  conf: Dict[str, Any], *,
                  model_id: Optional[str] = None,
                  max_tokens: int = 800,
                  temperature: float = 0.2) -> str:
    """Multi-turn chat. ``messages`` is [{role, content}, ...].

    Roles accepted: ``user``, ``assistant``, ``system``. Per-provider
    handling: Cohere extracts the system message into ``preamble`` and
    treats the last user message as ``message`` with the rest as
    ``chat_history``; generic providers send the messages as-is.
    """
    if not messages:
        raise ValueError("messages must be a non-empty list")
    for m in messages:
        if not isinstance(m, dict) or "role" not in m or "content" not in m:
            raise ValueError(
                "each message must be a dict with 'role' and 'content' keys"
            )

    import oci  # lazy
    model = _resolve_model(conf, model_id, DEFAULT_CHAT_MODEL)
    provider = _detect_provider(model)
    compartment = _resolve_compartment(conf)
    temp = _clamp_temperature(temperature)
    try:
        max_t = int(max_tokens)
    except (TypeError, ValueError):
        max_t = 800

    client = _build_client(conf)
    serving = oci.generative_ai_inference.models.OnDemandServingMode(model_id=model)

    if provider == "cohere":
        # Split: system -> preamble; last user -> message; rest -> history.
        preamble = None
        history: List[Any] = []
        last_user_text = ""
        seen_user = False
        # Walk messages: any system becomes preamble (first wins); user/
        # assistant entries before the final user go into chat_history;
        # the LAST user content becomes the prompt.
        # Find the final user index first.
        final_user_idx = -1
        for i, m in enumerate(messages):
            if m.get("role") == "user":
                final_user_idx = i
        if final_user_idx < 0:
            raise ValueError("Cohere chat requires at least one user message")

        cohere_models = oci.generative_ai_inference.models
        for i, m in enumerate(messages):
            role = m.get("role")
            content = str(m.get("content") or "")
            if role == "system":
                if preamble is None:
                    preamble = content
                continue
            if i == final_user_idx:
                last_user_text = content
                seen_user = True
                continue
            if role == "user":
                history.append(cohere_models.CohereUserMessage(message=content))
            elif role == "assistant":
                history.append(cohere_models.CohereChatBotMessage(message=content))
            # silently skip unknown roles

        if not seen_user:
            raise ValueError("Cohere chat requires at least one user message")

        req = cohere_models.CohereChatRequest(
            message=last_user_text,
            chat_history=history or None,
            preamble_override=preamble,
            max_tokens=max_t,
            temperature=temp,
            is_stream=False,
        )
        details = oci.generative_ai_inference.models.ChatDetails(
            serving_mode=serving,
            compartment_id=compartment,
            chat_request=req,
        )
        try:
            resp = client.chat(details)
        except Exception as e:
            _raise_from_oci("chat", e)
            return ""  # unreachable, satisfies type checkers
        # Cohere response: resp.data.chat_response.text
        text = ""
        try:
            text = getattr(resp.data.chat_response, "text", "") or ""
        except Exception:
            text = ""
        return text

    # generic provider (xAI, Meta, OpenAI, Google)
    generic_models = oci.generative_ai_inference.models
    msg_objs: List[Any] = []
    for m in messages:
        role = (m.get("role") or "").lower()
        content_text = str(m.get("content") or "")
        content_parts = [generic_models.TextContent(text=content_text)]
        if role == "system":
            msg_objs.append(generic_models.SystemMessage(content=content_parts))
        elif role == "assistant":
            msg_objs.append(generic_models.AssistantMessage(content=content_parts))
        else:  # user (default)
            msg_objs.append(generic_models.UserMessage(content=content_parts))

    req = generic_models.GenericChatRequest(
        api_format=generic_models.BaseChatRequest.API_FORMAT_GENERIC,
        messages=msg_objs,
        max_tokens=max_t,
        temperature=temp,
        is_stream=False,
    )
    details = oci.generative_ai_inference.models.ChatDetails(
        serving_mode=serving,
        compartment_id=compartment,
        chat_request=req,
    )
    try:
        resp = client.chat(details)
    except Exception as e:
        _raise_from_oci("chat", e)
        return ""  # unreachable

    # Generic response: resp.data.chat_response.choices[0].message.content[0].text
    try:
        choices = getattr(resp.data.chat_response, "choices", None) or []
        if not choices:
            return ""
        msg = getattr(choices[0], "message", None)
        if msg is None:
            return ""
        content = getattr(msg, "content", None) or []
        if not content:
            return ""
        first = content[0]
        return getattr(first, "text", "") or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------

def embed(texts: Union[str, Sequence[str]],
          conf: Dict[str, Any], *,
          model_id: Optional[str] = None,
          input_type: str = "search_document") -> List[List[float]]:
    """Embed a string or list of strings. Always returns list[list[float]].

    A single-string input returns a list with one vector. An empty list
    short-circuits to ``[]`` without calling the API.
    """
    if isinstance(texts, str):
        inputs: List[str] = [texts]
    else:
        inputs = [str(t) for t in (texts or [])]

    if not inputs:
        return []

    import oci  # lazy
    model = _resolve_model(conf, model_id, DEFAULT_EMBED_MODEL)
    compartment = _resolve_compartment(conf)
    client = _build_client(conf)

    serving = oci.generative_ai_inference.models.OnDemandServingMode(model_id=model)
    req = oci.generative_ai_inference.models.EmbedTextDetails(
        inputs=inputs,
        serving_mode=serving,
        compartment_id=compartment,
        input_type=input_type,
    )
    try:
        resp = client.embed_text(req)
    except Exception as e:
        _raise_from_oci("embed", e)
        return []  # unreachable

    raw = getattr(resp.data, "embeddings", None) or []
    # Normalize to plain Python lists of floats so callers don't accidentally
    # hold a SDK type that won't survive JSON serialization.
    out: List[List[float]] = []
    for vec in raw:
        out.append([float(x) for x in (vec or [])])
    return out


__all__ = [
    "chat",
    "chat_messages",
    "embed",
    "DEFAULT_CHAT_MODEL",
    "DEFAULT_EMBED_MODEL",
]
