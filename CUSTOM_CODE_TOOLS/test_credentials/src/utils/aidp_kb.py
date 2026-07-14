"""
aidp_kb — Knowledge Base helper for AIDP custom tools.

Public API:
  kb_search(query, kb_key, conf, *, top_k=5) -> list[dict]
      POST {base}/knowledgeBases/{kb_key}/actions/query
      Normalizes returned items to {"score", "chunk", "source", "metadata", ...}.
  list_kbs(conf, *, catalog_key=None, schema_key=None) -> list[dict]
      Delegates to aidp_io.list_knowledge_bases.
  trigger_ingest(kb_key, job_key, conf) -> dict
      Delegates to aidp_io.trigger_kb_job_run.

Uses the shared aidp_io._client + _build_signer pattern. If aidp_io is
missing, every function raises ImportError with an actionable message so the
caller knows to bundle aidp_io alongside this module.

Conf keys consumed (same as aidp_io):
  region, data_lake_ocid, api_version (default 20260430), service_path
  (default aiDataPlatforms), auth_mode (resource_principal | user_principal
  | instance_principal), tenancy_ocid, user_ocid, fingerprint,
  private_key_content, pass_phrase, timeout.

Errors raise ValueError carrying the OCI body preview (max 1024 chars) and
the HTTP status. Callers wrap with their tool's {"ok": false, ...} envelope.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import quote

try:
    from aidp_debug import debug, debug_warn, debug_error  # type: ignore
except ImportError:  # pragma: no cover
    def debug(*a, **k): pass
    def debug_warn(*a, **k): pass
    def debug_error(*a, **k): pass


_AIDP_IO_REQUIRED_MSG = (
    "aidp_kb requires the aidp_io module. Bundle utils/aidp_io.py into the "
    "same package (the build step normally syncs this automatically). "
    "Re-run `python setup.py build` to regenerate."
)


def _load_aidp_io():
    """Locate aidp_io regardless of how this module was imported."""
    try:
        from . import aidp_io as _io  # type: ignore
        return _io
    except Exception:
        pass
    try:
        import aidp_io as _io  # type: ignore
        return _io
    except Exception:
        pass
    raise ImportError(_AIDP_IO_REQUIRED_MSG)


def _err_from_response(resp, call: str):
    status = getattr(resp, "status_code", "?")
    try:
        body = (resp.text or "")[:1024]
    except Exception:
        body = ""
    if status == 404:
        return ValueError(f"KB endpoint not found ({call}): {body}")
    return ValueError(f"KB {call} failed (HTTP {status}): {body}")


def kb_search(query: str,
              kb_key: str,
              conf: Optional[Dict[str, Any]],
              *,
              top_k: int = 5,
              context_vars: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Run a RAG-style search against an AIDP Knowledge Base."""
    if not query or not str(query).strip():
        raise ValueError("kb_search: query must be a non-empty string")
    if not kb_key or not str(kb_key).strip():
        raise ValueError("kb_search: kb_key is required (catalog.schema.kbName form)")
    try:
        top_k = int(top_k)
    except Exception:
        top_k = 5
    if top_k <= 0:
        top_k = 5
    if top_k > 100:
        debug_warn("kb_search: top_k > 100, clamping to 100")
        top_k = 100

    aidp_io = _load_aidp_io()
    base, signer, requests, timeout = aidp_io._client(conf or {}, context_vars or {})

    url = f"{base}/knowledgeBases/{quote(kb_key, safe='')}/actions/query"
    body = {"query": str(query), "topK": top_k}
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    debug("aidp_kb.kb_search", url=url, top_k=top_k, query_len=len(query))
    resp = requests.post(url, auth=signer, json=body, headers=headers, timeout=timeout)
    if not resp.ok:
        raise _err_from_response(resp, "kb_search")

    payload = resp.json() if resp.content else {}
    raw_items = payload.get("items") or payload.get("results") or payload.get("hits") or []
    if isinstance(raw_items, dict):
        raw_items = raw_items.get("items") or raw_items.get("results") or []

    out: List[Dict[str, Any]] = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        normalized = {
            "score": it.get("score") or it.get("relevance") or it.get("similarity"),
            "chunk": it.get("chunk") or it.get("text") or it.get("content") or it.get("snippet") or "",
            "source": it.get("source") or it.get("path") or it.get("file") or it.get("uri") or "",
            "metadata": it.get("metadata") or it.get("meta") or {},
        }
        for k, v in it.items():
            if k not in normalized:
                normalized[k] = v
        out.append(normalized)

    debug("aidp_kb.kb_search returned", count=len(out))
    return out


def list_kbs(conf: Optional[Dict[str, Any]],
             *,
             catalog_key: Optional[str] = None,
             schema_key: Optional[str] = None,
             context_vars: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """List Knowledge Bases visible to the caller. Delegates to aidp_io."""
    aidp_io = _load_aidp_io()
    if not hasattr(aidp_io, "list_knowledge_bases"):
        raise ImportError(
            "aidp_io.list_knowledge_bases is not available. Update aidp_io.py."
        )
    # aidp_io.list_knowledge_bases(conf, ctx, catalog_key, schema_key)
    return aidp_io.list_knowledge_bases(
        conf or {}, context_vars or {}, catalog_key, schema_key,
    )


def trigger_ingest(kb_key: str,
                   job_key: str,
                   conf: Optional[Dict[str, Any]],
                   *,
                   run_config: Optional[Dict[str, Any]] = None,
                   context_vars: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Trigger a KB ingestion job run via aidp_io."""
    if not kb_key:
        raise ValueError("trigger_ingest: kb_key required")
    if not job_key:
        raise ValueError("trigger_ingest: job_key required")
    aidp_io = _load_aidp_io()
    if not hasattr(aidp_io, "trigger_kb_job_run"):
        raise ImportError("aidp_io.trigger_kb_job_run is not available.")
    return aidp_io.trigger_kb_job_run(
        conf or {}, context_vars or {}, kb_key, job_key, run_config,
    )


__all__ = ["kb_search", "list_kbs", "trigger_ingest"]
