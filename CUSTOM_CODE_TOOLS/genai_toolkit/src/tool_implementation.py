"""
GenAI Toolkit
=============
  RubricScorerTool  - LLM-as-judge: score an output against a rubric (structured)
  SummarizerTool    - map-reduce summarization of long text
  EmbeddingTool     - text-to-vector via OCI GenAI embeddings

All run on OCI GenAI with resource-principal auth by default. No API keys are
handled by these tools.

Standardized response envelope:
    success: {"ok": True,  "data": {...}, <legacy keys preserved>}
    error:   {"ok": False, "error": str,  "error_type": str}
"""

import json

from aidputils.agents.tools.custom_tools.base import CustomToolBase
from .utils.config_utils import get_cfg, resolve_oci_conf
from .utils.llm_utils import call_llm, build_llm, build_oci_genai_client, estimate_tokens

# Debug Channel with no-op fallback so the package still works when the runtime
# doesn't inject aidp_debug (e.g. local unit testing).
try:
    from aidp_debug import debug, debug_warn, debug_error, DebugLog  # type: ignore
except Exception:  # pragma: no cover - runtime shim
    def debug(*args, **kwargs):
        return None

    def debug_warn(*args, **kwargs):
        return None

    def debug_error(*args, **kwargs):
        return None

    class DebugLog:  # type: ignore
        @staticmethod
        def embed(result):
            return result


# --------------------------------------------------------------------------- #
# Envelope helpers
# --------------------------------------------------------------------------- #
def _ok(data=None, **legacy):
    out = {"ok": True, "data": data if data is not None else {}}
    for k, v in legacy.items():
        if k not in out:
            out[k] = v
    return DebugLog.embed(out)


def _err(error, error_type="ToolError", **extra):
    out = {"ok": False, "error": str(error), "error_type": error_type}
    for k, v in extra.items():
        if k not in out:
            out[k] = v
    return DebugLog.embed(out)


# --------------------------------------------------------------------------- #
# Rubric scorer (LLM-as-judge)
# --------------------------------------------------------------------------- #
@CustomToolBase.register
class RubricScorerTool(CustomToolBase):
    _JUDGE_SYSTEM = (
        "You are a strict, fair evaluator. Score the candidate text against the "
        "rubric. Respond with ONLY a JSON object, no prose, no markdown fences, "
        "in exactly this shape: "
        '{"overall_score": <number 0-10>, "criteria": '
        '[{"name": "<criterion>", "score": <0-10>, "comment": "<short>"}], '
        '"rationale": "<2-3 sentences>", "pass": <true|false>}'
    )

    _DEFAULT_PROMPT_CHAR_BUDGET = 120000

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        debug("RubricScorerTool: start", keys=list((runtime_params or {}).keys()))
        candidate = (runtime_params or {}).get("candidate", "") or ""
        rubric = (runtime_params or {}).get("rubric", "") or ""

        if not candidate.strip():
            debug_warn("RubricScorerTool: empty candidate")
            return _err("candidate (the text to score) is required", "ValidationError")
        if not rubric.strip():
            debug_warn("RubricScorerTool: empty rubric")
            return _err("rubric (the scoring criteria) is required", "ValidationError")

        pass_threshold = get_cfg(conf, "pass_threshold", 7.0)
        max_candidate_chars = get_cfg(conf, "max_candidate_chars", 60000)
        max_rubric_chars = get_cfg(conf, "max_rubric_chars", 8000)
        prompt_budget = get_cfg(conf, "prompt_char_budget", cls._DEFAULT_PROMPT_CHAR_BUDGET)

        truncated = False
        original_candidate_len = len(candidate)
        original_rubric_len = len(rubric)

        if len(candidate) > max_candidate_chars:
            candidate = candidate[:max_candidate_chars]
            truncated = True
            debug_warn("RubricScorerTool: candidate truncated",
                       original=original_candidate_len, kept=max_candidate_chars)
        if len(rubric) > max_rubric_chars:
            rubric = rubric[:max_rubric_chars]
            truncated = True
            debug_warn("RubricScorerTool: rubric truncated",
                       original=original_rubric_len, kept=max_rubric_chars)

        user = (
            f"RUBRIC:\n{rubric}\n\n"
            f"PASS THRESHOLD (overall): {pass_threshold}\n\n"
            f"CANDIDATE TEXT:\n{candidate}"
        )

        full_prompt_chars = len(cls._JUDGE_SYSTEM) + len(user)
        if full_prompt_chars > prompt_budget:
            overshoot = full_prompt_chars - prompt_budget
            new_cand_len = max(500, len(candidate) - overshoot - 200)
            candidate = candidate[:new_cand_len]
            truncated = True
            debug_warn("RubricScorerTool: prompt over budget, candidate further trimmed",
                       budget=prompt_budget, new_candidate_chars=new_cand_len)
            user = (
                f"RUBRIC:\n{rubric}\n\n"
                f"PASS THRESHOLD (overall): {pass_threshold}\n\n"
                f"CANDIDATE TEXT:\n{candidate}"
            )

        try:
            raw = call_llm(conf, cls._JUDGE_SYSTEM, user)
            verdict = _parse_json_block(raw)
            if verdict is None:
                debug_error("RubricScorerTool: judge returned unparseable output")
                return _err("judge did not return parseable JSON",
                            "ParseError", raw=raw[:500] if raw else "")
            try:
                verdict["pass"] = float(verdict.get("overall_score", 0)) >= float(pass_threshold)
            except (TypeError, ValueError):
                pass
            verdict["truncated"] = truncated
            verdict["prompt_chars"] = len(cls._JUDGE_SYSTEM) + len(user)
            verdict["estimated_tokens"] = estimate_tokens(cls._JUDGE_SYSTEM + user)
            debug("RubricScorerTool: ok",
                  overall=verdict.get("overall_score"), truncated=truncated)
            return _ok(data=verdict, **verdict)
        except Exception as e:
            debug_error("RubricScorerTool: exception", error=str(e))
            return _err(e, type(e).__name__)


# --------------------------------------------------------------------------- #
# Summarizer (map-reduce)
# --------------------------------------------------------------------------- #
@CustomToolBase.register
class SummarizerTool(CustomToolBase):
    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        debug("SummarizerTool: start", keys=list((runtime_params or {}).keys()))
        text = (runtime_params or {}).get("text", "") or ""
        instruction = (runtime_params or {}).get(
            "instruction", "Summarize the key points concisely."
        ) or "Summarize the key points concisely."

        if not text.strip():
            debug_warn("SummarizerTool: empty text")
            return _err("text is required", "ValidationError")

        chunk_chars = get_cfg(conf, "chunk_chars", 8000)
        max_chunks = get_cfg(conf, "max_chunks", 30)
        max_input_chars = get_cfg(conf, "max_input_chars", 2000000)

        truncated = False
        input_truncated = False
        original_text_len = len(text)

        if len(text) > max_input_chars:
            text = text[:max_input_chars]
            truncated = True
            input_truncated = True
            debug_warn("SummarizerTool: input truncated",
                       original=original_text_len, kept=max_input_chars)

        try:
            all_chunks = _chunk_text(text, chunk_chars)
            total_chunks = len(all_chunks)
            chunks = all_chunks[:max_chunks]
            chunks_dropped = max(0, total_chunks - len(chunks))
            if chunks_dropped > 0:
                truncated = True
                debug_warn("SummarizerTool: chunks truncated",
                           total=total_chunks, processed=len(chunks),
                           dropped=chunks_dropped)

            if len(chunks) == 1:
                summary = call_llm(
                    conf,
                    "You are a precise summarizer.",
                    f"{instruction}\n\nTEXT:\n{chunks[0]}",
                )
                data = {
                    "summary": summary,
                    "chunks_processed": 1,
                    "chunks_total": total_chunks,
                    "chunks_dropped": chunks_dropped,
                    "truncated": truncated,
                    "input_truncated": input_truncated,
                }
                debug("SummarizerTool: ok (single chunk)", truncated=truncated)
                return _ok(data=data, summary=summary,
                           chunks_processed=1, truncated=truncated)

            partials = []
            for i, ch in enumerate(chunks):
                p = call_llm(
                    conf,
                    "You are a precise summarizer. Summarize this section faithfully.",
                    f"Section {i + 1} of {len(chunks)}:\n{ch}",
                )
                partials.append(p)

            joined = "\n\n".join(f"[Section {i+1}] {p}" for i, p in enumerate(partials))
            final = call_llm(
                conf,
                "You are a precise summarizer.",
                f"{instruction}\n\nCombine these section summaries into one coherent summary:\n\n{joined}",
            )
            data = {
                "summary": final,
                "chunks_processed": len(chunks),
                "chunks_total": total_chunks,
                "chunks_dropped": chunks_dropped,
                "truncated": truncated,
                "input_truncated": input_truncated,
                "partial_summaries": partials,
            }
            debug("SummarizerTool: ok (map-reduce)",
                  processed=len(chunks), dropped=chunks_dropped)
            return _ok(data=data,
                       summary=final,
                       chunks_processed=len(chunks),
                       truncated=truncated)
        except Exception as e:
            debug_error("SummarizerTool: exception", error=str(e))
            return _err(e, type(e).__name__)


# --------------------------------------------------------------------------- #
# Embedding (OCI GenAI embeddings)
# --------------------------------------------------------------------------- #
@CustomToolBase.register
class EmbeddingTool(CustomToolBase):
    _ALLOWED_INPUT_TYPES = {
        "search_document", "search_query", "classification", "clustering",
    }

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        debug("EmbeddingTool: start", keys=list((runtime_params or {}).keys()))
        rp = runtime_params or {}

        texts = rp.get("texts")
        single = rp.get("text")
        if texts is None and single is None:
            debug_warn("EmbeddingTool: no text/texts provided")
            return _err("provide 'text' (string) or 'texts' (list of strings)",
                        "ValidationError")
        if texts is None:
            if not isinstance(single, str):
                return _err("'text' must be a string", "ValidationError")
            texts = [single]
        if isinstance(texts, str):
            try:
                parsed = json.loads(texts)
                if isinstance(parsed, list):
                    texts = parsed
                else:
                    texts = [texts]
            except Exception:
                texts = [texts]
        if not isinstance(texts, list):
            return _err("'texts' must be a list of strings", "ValidationError")
        clean_texts = []
        for t in texts:
            if isinstance(t, str):
                clean_texts.append(t)
            elif t is None:
                continue
            else:
                clean_texts.append(str(t))
        if not clean_texts:
            return _err("no non-empty texts to embed", "ValidationError")

        max_texts = get_cfg(conf, "max_texts", 96)
        max_text_chars = get_cfg(conf, "max_text_chars", 2048)
        truncate_mode = get_cfg(conf, "truncate", "END")
        input_type_param = rp.get("input_type")
        input_type = (
            input_type_param
            if isinstance(input_type_param, str) and input_type_param.strip()
            else get_cfg(conf, "input_type", "search_document")
        )
        if input_type not in cls._ALLOWED_INPUT_TYPES:
            debug_warn("EmbeddingTool: unknown input_type, defaulting",
                       requested=input_type)
            input_type = "search_document"

        truncated = False
        original_count = len(clean_texts)
        if original_count > max_texts:
            clean_texts = clean_texts[:max_texts]
            truncated = True
            debug_warn("EmbeddingTool: text count truncated",
                       original=original_count, kept=max_texts)

        capped = []
        for t in clean_texts:
            if len(t) > max_text_chars:
                capped.append(t[:max_text_chars])
                truncated = True
            else:
                capped.append(t)
        clean_texts = capped

        resolved = resolve_oci_conf(conf)
        model_id = resolved["model_id"]
        compartment_id = resolved["compartment_id"]

        if not compartment_id:
            debug_error("EmbeddingTool: missing compartment_id")
            return _err("compartment_id is required for embeddings",
                        "ValidationError")

        try:
            import oci
            client, _ = build_oci_genai_client(conf)

            serving_mode = oci.generative_ai_inference.models.OnDemandServingMode(
                model_id=model_id
            )
            embed_request = oci.generative_ai_inference.models.EmbedTextDetails(
                inputs=clean_texts,
                serving_mode=serving_mode,
                compartment_id=compartment_id,
                input_type=input_type,
                truncate=truncate_mode,
            )
            resp = client.embed_text(embed_request)
            raw_embeddings = getattr(resp.data, "embeddings", None) or []

            data = {
                "embeddings": raw_embeddings,
                "model": model_id,
                "input_type": input_type,
                "count": len(raw_embeddings),
                "requested_count": original_count,
                "truncated": truncated,
            }
            debug("EmbeddingTool: ok", count=len(raw_embeddings), truncated=truncated)
            return _ok(data=data,
                       embeddings=raw_embeddings,
                       model=model_id,
                       input_type=input_type,
                       count=len(raw_embeddings))
        except Exception as e:
            debug_error("EmbeddingTool: exception", error=str(e))
            return _err(e, type(e).__name__)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _chunk_text(text, chunk_chars):
    try:
        chunk_chars = int(chunk_chars)
    except (TypeError, ValueError):
        chunk_chars = 8000
    paras = text.split("\n\n")
    chunks, cur = [], ""
    for p in paras:
        if len(cur) + len(p) + 2 > chunk_chars and cur:
            chunks.append(cur)
            cur = p
        else:
            cur = (cur + "\n\n" + p) if cur else p
    if cur:
        chunks.append(cur)
    return chunks or [text]


def _parse_json_block(raw):
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(s[start:end + 1])
    except Exception:
        return None
