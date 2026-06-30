"""
Web Toolkit
===========
  WebFetchTool     - fetch a URL and return clean, readable text
  WebhookSenderTool- POST a structured message to a webhook (Slack/Teams/generic)

Both use the framework _make_http_request helper, which applies SSRF protection
and the configured auth. WebhookSenderTool is the notification half of a
Human-in-the-Loop pattern: the actual pause/resume is a native flow node, but
this is how you notify a channel that input is needed.
"""

import json

from aidputils.agents.tools.custom_tools.base import CustomToolBase
from .utils.config_utils import get_cfg

# Debug channel: try real module, fall back to no-ops so the tool runs even when
# the runtime hasn't injected aidp_debug into the import path.
try:
    from aidp_debug import debug, debug_warn, debug_error, DebugLog
except ImportError:  # pragma: no cover - shim for local/standalone runs
    def debug(*args, **kwargs): pass
    def debug_warn(*args, **kwargs): pass
    def debug_error(*args, **kwargs): pass
    class DebugLog:
        @staticmethod
        def embed(result):
            return result


def _ok(data):
    """Standard success envelope. Also spreads data keys at the top level so
    callers that still read legacy fields (url, status, title, text, delivered,
    response) continue to work."""
    out = {"ok": True, "data": data}
    if isinstance(data, dict):
        for k, v in data.items():
            if k not in out:
                out[k] = v
    return out


def _err(message, error_type="ToolError", **legacy):
    out = {"ok": False, "error": str(message), "error_type": error_type}
    out.update(legacy)
    # Legacy: callers expecting {"error": "..."} still see it.
    return out


# --------------------------------------------------------------------------- #
# Web fetch + readability
# --------------------------------------------------------------------------- #
@CustomToolBase.register
class WebFetchTool(CustomToolBase):
    """Fetch a web page and return its main readable text (boilerplate stripped).
    Use to read an article or page the user references. Respects the workspace
    SSRF protections built into the platform HTTP layer."""

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        url = (runtime_params.get("url") or "").strip()
        debug("WebFetchTool: start", url=url)
        if not url:
            debug_error("WebFetchTool: missing url")
            return _err("url is required", "ValidationError")

        timeout = get_cfg(conf, "timeout", 30)
        max_chars = get_cfg(conf, "max_chars", 20000)
        max_bytes = get_cfg(conf, "max_bytes", 5_242_880)  # 5 MiB hard cap on body
        raw_html = get_cfg(conf, "return_raw_html", False)
        user_agent = get_cfg(conf, "user_agent", "AIDP-Agent/1.0")

        try:
            resp = cls._make_http_request(
                method="GET",
                url=url,
                conf=conf,
                headers={"User-Agent": user_agent},
                timeout=timeout,
            )
        except Exception as e:
            debug_error("WebFetchTool: HTTP error", error=str(e))
            return _err(e, type(e).__name__)

        # Content-Length pre-check (best-effort: header may be missing/wrong).
        body_truncated = False
        try:
            cl = int((resp.headers or {}).get("Content-Length", "0") or "0")
        except (TypeError, ValueError):
            cl = 0
        if cl and cl > max_bytes:
            debug_warn("WebFetchTool: content-length exceeds cap",
                       content_length=cl, max_bytes=max_bytes)
            body_truncated = True

        try:
            html = resp.text or ""
        except Exception as e:
            debug_error("WebFetchTool: decode error", error=str(e))
            return _err(e, type(e).__name__)

        # Enforce max_bytes on the actual decoded body too.
        encoded = html.encode("utf-8", errors="ignore")
        if len(encoded) > max_bytes:
            html = encoded[:max_bytes].decode("utf-8", errors="ignore")
            body_truncated = True
            debug_warn("WebFetchTool: body truncated to max_bytes",
                       max_bytes=max_bytes)

        try:
            if raw_html:
                result = _ok({
                    "url": url,
                    "status": resp.status_code,
                    "html": html[:max_chars],
                    "truncated": body_truncated or len(html) > max_chars,
                })
                debug("WebFetchTool: returning raw html",
                      status=resp.status_code, bytes=len(html))
                return DebugLog.embed(result)

            title, text = _extract_readable(html)
            text_truncated = len(text) > max_chars
            result = _ok({
                "url": url,
                "status": resp.status_code,
                "title": title,
                "text": text[:max_chars],
                "truncated": body_truncated or text_truncated,
            })
            debug("WebFetchTool: extracted",
                  status=resp.status_code, title=(title or "")[:80],
                  text_len=len(text), truncated=result["data"]["truncated"])
            return DebugLog.embed(result)
        except Exception as e:
            debug_error("WebFetchTool: extraction error", error=str(e))
            return _err(e, type(e).__name__)


def _extract_readable(html):
    """Try readability-lxml, then BeautifulSoup, then a crude tag strip.
    Always falls through to bs4 get_text() on any readability failure (empty
    summary or exception) before giving up to the regex stripper."""
    # 1) readability + bs4 cleanup of the summary fragment
    try:
        from readability import Document
        from bs4 import BeautifulSoup
        doc = Document(html)
        title = doc.short_title() or ""
        summary_html = doc.summary() or ""
        text = BeautifulSoup(summary_html, "html.parser").get_text("\n", strip=True) if summary_html else ""
        if text.strip():
            return title, text
        # readability returned nothing useful — fall through to bs4 on full page
    except Exception as e:
        debug_warn("_extract_readable: readability failed", error=str(e))

    # 2) bs4 over the full page (also our explicit fallback when readability is empty)
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
            tag.decompose()
        title = (soup.title.string if soup.title and soup.title.string else "") or ""
        return title, soup.get_text("\n", strip=True)
    except Exception as e:
        debug_warn("_extract_readable: bs4 failed", error=str(e))

    # 3) Crude tag-strip
    import re
    text = re.sub(r"<[^>]+>", " ", html or "")
    return "", re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------- #
# Webhook sender
# --------------------------------------------------------------------------- #
@CustomToolBase.register
class WebhookSenderTool(CustomToolBase):
    """Send a message to a webhook URL (Slack, Teams, or any generic endpoint).
    Use to notify a channel, e.g. to alert a human that a flow needs their input
    or that a step completed. For Slack/Teams it formats the message for you."""

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        message = (runtime_params.get("message") or "")
        debug("WebhookSenderTool: start", has_message=bool(message.strip()))
        if not message.strip():
            debug_error("WebhookSenderTool: missing message")
            return _err("message is required", "ValidationError")

        # URL resolution priority:
        #   1. AIDP Credential Store via conf.credential_name (recommended —
        #      the URL is a secret for Slack/Teams incoming webhooks)
        #   2. conf.webhook_url (legacy plaintext)
        #   3. runtime_params.webhook_url (last resort; allows LLM override
        #      only when operator did not configure either of the above)
        url = ""
        cred_name = (get_cfg(conf, "credential_name", "") or "").strip()
        if cred_name:
            try:
                from .utils.credential_resolver import resolve_bundle
                bundle, cred_err = resolve_bundle(cred_name)
                if cred_err:
                    debug_error(f"WebhookSenderTool: credential_name='{cred_name}' "
                                f"failed: {cred_err}")
                    return _err(cred_err, "CredentialStoreError")
                if bundle:
                    url = (bundle.get("webhook_url") or "").strip()
            except ImportError:
                pass
        if not url:
            url = (get_cfg(conf, "webhook_url", "") or "").strip()
        if not url:
            url = (runtime_params.get("webhook_url") or "").strip()
        if not url:
            debug_error("WebhookSenderTool: missing webhook_url")
            return _err("webhook_url must be set via credential_name (recommended), "
                        "conf, or runtime param", "ValidationError")

        style = (get_cfg(conf, "style", "slack") or "slack").lower()
        method = (get_cfg(conf, "method", "POST") or "POST").upper()
        if method not in ("POST", "PUT"):
            debug_warn("WebhookSenderTool: invalid method, defaulting to POST",
                       method=method)
            method = "POST"
        timeout = get_cfg(conf, "timeout", 30)
        title = runtime_params.get("title", "")

        if style == "slack":
            payload = {"text": (f"*{title}*\n{message}" if title else message)}
        elif style == "teams":
            payload = {
                "@type": "MessageCard",
                "@context": "http://schema.org/extensions",
                "summary": title or "Notification",
                "title": title,
                "text": message,
            }
        else:  # generic_json (or any other value)
            payload = {"title": title, "message": message} if title else {"message": message}

        try:
            resp = cls._make_http_request(
                method=method,
                url=url,
                conf=conf,
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=timeout,
            )
        except Exception as e:
            debug_error("WebhookSenderTool: HTTP error", error=str(e))
            return _err(e, type(e).__name__, delivered=False)

        ok = 200 <= resp.status_code < 300
        body = (resp.text or "")
        body_truncated = len(body) > 500
        data = {
            "delivered": ok,
            "status": resp.status_code,
            "response": body[:500],
            "truncated": body_truncated,
        }
        debug("WebhookSenderTool: sent",
              method=method, style=style, status=resp.status_code, delivered=ok)
        if ok:
            return DebugLog.embed(_ok(data))
        # Non-2xx: still surface as an error envelope but keep legacy fields.
        return DebugLog.embed(_err(f"webhook returned {resp.status_code}",
                                   "HTTPError", **data))
