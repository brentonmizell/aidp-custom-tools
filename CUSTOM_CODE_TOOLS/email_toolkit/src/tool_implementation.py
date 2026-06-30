"""
Email Toolkit
=============
  SmtpEmailTool - send an email via SMTP (OCI Email Delivery or any relay)
  ImapReadTool  - read/search a mailbox via IMAP

SMTP send is stdlib-only and works against any relay. The intended config is
OCI Email Delivery (smtp.email.<region>.oci.oraclecloud.com:587, STARTTLS, with
generated SMTP credentials), which keeps sending inside OCI. It also works
against a corporate relay or a provider SMTP endpoint.

IMAP read is included because it is also stdlib, but note it overlaps the
Gmail / Outlook OAuth integrations on the roadmap. Prefer those for full
mailbox access; use ImapReadTool for simple IMAP-reachable inboxes.

Credentials come from tool config (use {{template}} variables for the password),
never from the model.
"""

import smtplib
import ssl
import time
import imaplib
import email as email_pkg
from email.message import EmailMessage
from email.header import decode_header, make_header
from email.utils import parseaddr, getaddresses, parsedate_to_datetime

from aidputils.agents.tools.custom_tools.base import CustomToolBase
from .utils.config_utils import get_cfg


def _resolve_creds_from_store(conf, expected_keys):
    """Look up conf.credential_name in the AIDP Credential Store and return
    the subset of bundle entries matching expected_keys.

    Returns {} when credential_name is not set (so callers can OR-fall through
    to existing plaintext-conf fields). Returns {} silently on lookup failure
    too — the existing path can still take over. Errors surface in debug only.
    """
    cred_name = get_cfg(conf, "credential_name", "")
    if not cred_name:
        return {}
    try:
        from .utils.credential_resolver import resolve_bundle
    except ImportError:
        return {}
    bundle, err = resolve_bundle(cred_name)
    if err or not bundle:
        return {}
    return {k: bundle.get(k) for k in expected_keys if bundle.get(k)}


def _resolve_smtp_creds_from_store(conf):
    return _resolve_creds_from_store(
        conf, ("host", "port", "username", "password", "from_address"))


def _resolve_imap_creds_from_store(conf):
    return _resolve_creds_from_store(
        conf, ("host", "port", "username", "password"))

# --------------------------------------------------------------------------- #
# Debug Channel (with no-op fallback if runtime doesn't inject aidp_debug)
# --------------------------------------------------------------------------- #
try:
    from aidp_debug import debug, debug_warn, debug_error, DebugLog  # type: ignore
except ImportError:  # pragma: no cover
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
def _ok(data, **extra_legacy):
    """Standard success envelope. extra_legacy adds top-level keys for callers
    that may rely on the pre-1.1 flat shape."""
    out = {"ok": True, "data": data}
    if extra_legacy:
        out.update(extra_legacy)
    return out


def _err(message, error_type="ToolError", **extra_legacy):
    out = {"ok": False, "error": str(message), "error_type": error_type}
    if extra_legacy:
        out.update(extra_legacy)
    return out


def _split_addrs(value):
    """Accept a list or a comma/semicolon separated string of addresses.
    Tolerates whitespace, newlines, and mixed separators."""
    if not value:
        return []
    if isinstance(value, list):
        items = value
    else:
        # Normalize semicolons, newlines, and tabs to commas before splitting.
        normalized = str(value).replace(";", ",").replace("\n", ",").replace("\t", ",")
        items = normalized.split(",")
    return [a.strip() for a in items if a and a.strip()]


def _valid_addr(a):
    name, addr = parseaddr(a)
    return "@" in addr and "." in addr.split("@")[-1]


# --------------------------------------------------------------------------- #
# SMTP send
# --------------------------------------------------------------------------- #
@CustomToolBase.register
class SmtpEmailTool(CustomToolBase):
    """Send an email via SMTP. Configure host/port/credentials and the from
    address in tool config (works with OCI Email Delivery or any relay). Use to
    send notifications, reports, or alerts, including telling a person a flow
    needs their input. Recipients, subject, and body come at call time."""

    @classmethod
    def _validate_config(cls, conf, runtime_params=None, **context_vars):
        if not get_cfg(conf, "smtp_host", ""):
            raise ValueError("smtp_host is required in tool config")
        if not get_cfg(conf, "from_addr", ""):
            raise ValueError("from_addr is required in tool config")

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        debug("SmtpEmailTool._execute_tool start", tool="SmtpEmailTool")
        # Credential resolution: if conf.credential_name is set, pull SMTP
        # creds (host/port/username/password/from_address) from the Credential
        # Store bundle. Otherwise fall back to plaintext conf fields (legacy).
        smtp_creds = _resolve_smtp_creds_from_store(conf)
        host = smtp_creds.get("host") or get_cfg(conf, "smtp_host", "")
        port = smtp_creds.get("port") or get_cfg(conf, "smtp_port", 587)
        username = smtp_creds.get("username") or get_cfg(conf, "smtp_username", "")
        password = smtp_creds.get("password") or get_cfg(conf, "smtp_password", "")
        from_addr = smtp_creds.get("from_address") or get_cfg(conf, "from_addr", "")
        use_ssl = get_cfg(conf, "use_ssl", False)            # implicit TLS (port 465)
        use_starttls = get_cfg(conf, "use_starttls", True)   # STARTTLS (port 587)
        timeout = get_cfg(conf, "timeout", 30)
        max_retries = max(0, get_cfg(conf, "max_retries", 2))
        # Optional allow-list of recipient domains, enforced as a guard.
        allowed_domains = get_cfg(conf, "allowed_recipient_domains", "")

        to = _split_addrs(runtime_params.get("to"))
        cc = _split_addrs(runtime_params.get("cc"))
        bcc = _split_addrs(runtime_params.get("bcc"))
        subject = runtime_params.get("subject", "") or ""
        body = runtime_params.get("body", "") or ""

        # Body content type: prefer explicit content_type, fall back to legacy html bool.
        content_type = (runtime_params.get("content_type") or "").strip().lower()
        legacy_html = bool(runtime_params.get("html", False))
        if content_type not in ("plain", "html"):
            content_type = "html" if legacy_html else "plain"
        is_html = content_type == "html"

        # Priority header (validated enum).
        priority = (runtime_params.get("priority") or "normal").strip().lower()
        if priority not in ("low", "normal", "high"):
            priority = "normal"

        debug(
            "SmtpEmailTool params",
            host=host, port=port, has_username=bool(username),
            has_password=bool(password),
            to_count=len(to), cc_count=len(cc), bcc_count=len(bcc),
            subject_len=len(subject), body_len=len(body),
            content_type=content_type, priority=priority,
        )

        if not to:
            debug_error("SmtpEmailTool: no recipients")
            return DebugLog.embed(_err("at least one 'to' recipient is required",
                                       error_type="ValidationError"))
        all_rcpts = to + cc + bcc
        bad = [a for a in all_rcpts if not _valid_addr(a)]
        if bad:
            debug_error("SmtpEmailTool: invalid recipients", bad=bad)
            return DebugLog.embed(_err(f"invalid recipient address(es): {bad}",
                                       error_type="ValidationError"))

        if allowed_domains:
            allow = {
                d.strip().lower()
                for d in str(allowed_domains).replace(";", ",").split(",")
                if d.strip()
            }
            offenders = [
                a for a in all_rcpts
                if parseaddr(a)[1].split("@")[-1].lower() not in allow
            ]
            if offenders:
                debug_error("SmtpEmailTool: recipient domain blocked",
                            offenders=offenders, allow=sorted(allow))
                return DebugLog.embed(_err(
                    f"recipient domain not allowed: {offenders}. Allowed: {sorted(allow)}",
                    error_type="GuardrailError"))

        # Build the message.
        msg = EmailMessage()
        msg["From"] = from_addr
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = subject

        # X-Priority: 1 (high), 3 (normal), 5 (low). Importance is also commonly used.
        if priority == "high":
            msg["X-Priority"] = "1"
            msg["Importance"] = "High"
        elif priority == "low":
            msg["X-Priority"] = "5"
            msg["Importance"] = "Low"
        else:
            msg["X-Priority"] = "3"

        if is_html:
            msg.set_content("This message requires an HTML-capable client.")
            msg.add_alternative(body, subtype="html")
        else:
            msg.set_content(body)

        # Send with bounded retry on transient connection errors.
        transient_excs = (
            smtplib.SMTPConnectError,
            smtplib.SMTPServerDisconnected,
            smtplib.SMTPHeloError,
            ConnectionError,
            TimeoutError,
            OSError,
        )

        last_exc = None
        for attempt in range(max_retries + 1):
            server = None
            try:
                debug("SmtpEmailTool connect attempt", attempt=attempt + 1, host=host, port=port)
                if use_ssl:
                    ctx = ssl.create_default_context()
                    server = smtplib.SMTP_SSL(host, int(port),
                                              timeout=int(timeout), context=ctx)
                else:
                    server = smtplib.SMTP(host, int(port), timeout=int(timeout))

                server.ehlo()
                # Always attempt STARTTLS when the server advertises it (unless we're already SSL).
                if not use_ssl:
                    supports_starttls = False
                    try:
                        supports_starttls = server.has_extn("starttls")
                    except Exception:
                        supports_starttls = False
                    if use_starttls or supports_starttls:
                        if supports_starttls:
                            server.starttls(context=ssl.create_default_context())
                            server.ehlo()
                            debug("SmtpEmailTool STARTTLS upgraded")
                        elif use_starttls:
                            debug_warn("SmtpEmailTool STARTTLS requested but not advertised")
                if username:
                    server.login(username, password)
                # send_message respects To/Cc; pass bcc via to_addrs so it isn't headered.
                server.send_message(msg, from_addr=from_addr, to_addrs=all_rcpts)

                data = {
                    "sent": True,
                    "to": to, "cc": cc, "bcc_count": len(bcc),
                    "subject": subject,
                    "recipients_total": len(all_rcpts),
                    "content_type": content_type,
                    "priority": priority,
                    "attempts": attempt + 1,
                }
                # Preserve legacy top-level keys for back-compat.
                result = _ok(data,
                             sent=True,
                             to=to, cc=cc, bcc_count=len(bcc),
                             subject=subject,
                             recipients_total=len(all_rcpts))
                debug("SmtpEmailTool sent", **data)
                return DebugLog.embed(result)

            except smtplib.SMTPAuthenticationError as e:
                debug_error("SmtpEmailTool auth failed", error=str(e))
                return DebugLog.embed(_err(f"SMTP authentication failed: {e}",
                                           error_type="SMTPAuthenticationError"))
            except transient_excs as e:
                last_exc = e
                debug_warn("SmtpEmailTool transient error",
                           attempt=attempt + 1, error=str(e))
                if attempt < max_retries:
                    time.sleep(min(2 ** attempt, 5))
                    continue
                break
            except Exception as e:
                debug_error("SmtpEmailTool send failed", error=str(e),
                            error_type=type(e).__name__)
                return DebugLog.embed(_err(str(e), error_type=type(e).__name__))
            finally:
                if server is not None:
                    try:
                        server.quit()
                    except Exception:
                        pass

        # Exhausted retries on transient errors.
        debug_error("SmtpEmailTool exhausted retries", error=str(last_exc))
        return DebugLog.embed(_err(
            f"SMTP send failed after {max_retries + 1} attempt(s): {last_exc}",
            error_type=type(last_exc).__name__ if last_exc else "SMTPError"))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _decode(value):
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value or ""


def _decode_payload(part, max_bytes):
    """Decode a single part's payload to a string, capped at max_bytes."""
    try:
        payload = part.get_payload(decode=True) or b""
    except Exception:
        return "", False
    if not payload:
        return "", False
    truncated = False
    if len(payload) > max_bytes:
        payload = payload[:max_bytes]
        truncated = True
    charset = part.get_content_charset() or "utf-8"
    try:
        text = payload.decode(charset, "replace")
    except Exception:
        text = payload.decode("utf-8", "replace")
    return text, truncated


def _body_snippet(message, n):
    """Best-effort plain-text snippet, capped at n bytes."""
    try:
        if message.is_multipart():
            for part in message.walk():
                if part.get_content_type() == "text/plain":
                    text, _ = _decode_payload(part, n)
                    return text.strip()
            return ""
        text, _ = _decode_payload(message, n)
        return text.strip()
    except Exception:
        return ""


def _extract_bodies(message, max_bytes):
    """Return (text, html, text_truncated, html_truncated) for a message."""
    text, html = "", ""
    text_trunc = html_trunc = False
    try:
        if message.is_multipart():
            for part in message.walk():
                ctype = part.get_content_type()
                if ctype == "text/plain" and not text:
                    text, text_trunc = _decode_payload(part, max_bytes)
                elif ctype == "text/html" and not html:
                    html, html_trunc = _decode_payload(part, max_bytes)
                if text and html:
                    break
        else:
            ctype = message.get_content_type()
            payload_text, trunc = _decode_payload(message, max_bytes)
            if ctype == "text/html":
                html, html_trunc = payload_text, trunc
            else:
                text, text_trunc = payload_text, trunc
    except Exception:
        pass
    return text, html, text_trunc, html_trunc


def _parse_since(value):
    """Convert a YYYY-MM-DD or IMAP date string into IMAP's DD-Mon-YYYY format."""
    if not value:
        return None
    s = str(value).strip()
    # If it already looks like IMAP date (e.g. 01-Jan-2026), pass through.
    months = {"jan", "feb", "mar", "apr", "may", "jun",
              "jul", "aug", "sep", "oct", "nov", "dec"}
    parts = s.split("-")
    if len(parts) == 3 and parts[1][:3].lower() in months:
        return s
    # Otherwise try ISO-8601.
    try:
        from datetime import datetime
        # Tolerate trailing time component.
        iso = s.split("T")[0]
        dt = datetime.strptime(iso, "%Y-%m-%d")
        return dt.strftime("%d-%b-%Y")
    except Exception:
        return None


ALLOWED_FIELDS = {"uid", "from", "to", "subject", "date",
                  "body_text", "body_html", "headers"}


# --------------------------------------------------------------------------- #
# IMAP read
# --------------------------------------------------------------------------- #
@CustomToolBase.register
class ImapReadTool(CustomToolBase):
    """Fetch messages from an IMAP mailbox.

    Configure host/port/SSL and credentials in tool config. At call time choose
    the folder (default INBOX), filtering (last N, unread only, since date),
    and which fields to include per message. Returns a structured envelope:
    {ok, data: {messages, count, folder}}.
    """

    @classmethod
    def _validate_config(cls, conf, runtime_params=None, **context_vars):
        if not get_cfg(conf, "imap_host", ""):
            raise ValueError("imap_host is required in tool config")
        if not get_cfg(conf, "imap_username", ""):
            raise ValueError("imap_username is required in tool config")

    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        debug("ImapReadTool._execute_tool start", tool="ImapReadTool")
        # Credential resolution: if conf.credential_name is set, pull IMAP
        # creds from the Credential Store. Otherwise fall back to plaintext.
        imap_creds = _resolve_imap_creds_from_store(conf)
        host = imap_creds.get("host") or get_cfg(conf, "imap_host", "")
        port = imap_creds.get("port") or get_cfg(conf, "imap_port", 993)
        username = imap_creds.get("username") or get_cfg(conf, "imap_username", "")
        password = imap_creds.get("password") or get_cfg(conf, "imap_password", "")
        use_ssl = get_cfg(conf, "use_ssl", True)
        timeout = get_cfg(conf, "timeout", 30)
        # Server-side hard cap so the model can't ask for the world.
        max_messages_cap = max(1, get_cfg(conf, "max_messages", 100))
        max_body_bytes = max(1024, get_cfg(conf, "max_body_bytes", 65536))

        folder = runtime_params.get("folder") or "INBOX"
        try:
            requested_limit = int(runtime_params.get("limit", 25) or 25)
        except (TypeError, ValueError):
            requested_limit = 25
        limit = max(1, min(requested_limit, max_messages_cap))
        unread_only = bool(runtime_params.get("unread_only", False))
        since_raw = runtime_params.get("since")
        mark_seen = bool(runtime_params.get("mark_seen", False))

        # Parse and validate field list.
        fields_raw = runtime_params.get("fields") or "uid,from,to,subject,date,body_text"
        requested_fields = [f.strip().lower() for f in str(fields_raw).split(",") if f.strip()]
        fields = [f for f in requested_fields if f in ALLOWED_FIELDS]
        if not fields:
            fields = ["uid", "from", "to", "subject", "date", "body_text"]

        since_imap = _parse_since(since_raw)
        if since_raw and not since_imap:
            debug_error("ImapReadTool: invalid since date", since=since_raw)
            return DebugLog.embed(_err(
                f"invalid 'since' date: {since_raw!r}. Use YYYY-MM-DD or DD-Mon-YYYY.",
                error_type="ValidationError"))

        debug("ImapReadTool params",
              host=host, port=port, use_ssl=use_ssl,
              folder=folder, limit=limit, requested_limit=requested_limit,
              unread_only=unread_only, since=since_imap, mark_seen=mark_seen,
              fields=fields)

        # Connect.
        imap = None
        try:
            try:
                # imaplib doesn't accept a timeout kwarg until 3.9+; pass if available.
                if use_ssl:
                    try:
                        imap = imaplib.IMAP4_SSL(host, int(port), timeout=int(timeout))
                    except TypeError:
                        imap = imaplib.IMAP4_SSL(host, int(port))
                else:
                    try:
                        imap = imaplib.IMAP4(host, int(port), timeout=int(timeout))
                    except TypeError:
                        imap = imaplib.IMAP4(host, int(port))
            except (OSError, imaplib.IMAP4.error) as e:
                debug_error("ImapReadTool connect failed", error=str(e))
                return DebugLog.embed(_err(f"IMAP connect failed: {e}",
                                           error_type="IMAPConnectError"))

            try:
                imap.login(username, password)
            except imaplib.IMAP4.error as e:
                debug_error("ImapReadTool auth failed", error=str(e))
                return DebugLog.embed(_err(f"IMAP authentication failed: {e}",
                                           error_type="IMAPAuthenticationError"))

            # Select folder (readonly unless mark_seen).
            try:
                status, data = imap.select(folder, readonly=not mark_seen)
                if status != "OK":
                    raise imaplib.IMAP4.error(f"select returned {status}: {data!r}")
            except imaplib.IMAP4.error as e:
                debug_error("ImapReadTool select failed", folder=folder, error=str(e))
                return DebugLog.embed(_err(f"failed to open folder {folder!r}: {e}",
                                           error_type="IMAPSelectError"))

            # Build search criteria.
            criteria = []
            if unread_only:
                criteria.append("UNSEEN")
            if since_imap:
                criteria.extend(["SINCE", since_imap])
            if not criteria:
                criteria = ["ALL"]

            try:
                status, search_data = imap.search(None, *criteria)
                if status != "OK":
                    raise imaplib.IMAP4.error(f"search returned {status}: {search_data!r}")
            except imaplib.IMAP4.error as e:
                debug_error("ImapReadTool search failed", error=str(e), criteria=criteria)
                return DebugLog.embed(_err(f"IMAP search failed: {e}",
                                           error_type="IMAPSearchError"))

            ids_blob = (search_data or [b""])[0] or b""
            ids = ids_blob.split()
            total_matched = len(ids)
            # Newest last in IMAP sequence numbers; take the last N for "last N".
            selected_ids = ids[-limit:] if total_matched > limit else ids
            # Reverse so newest first in the response.
            selected_ids = list(reversed(selected_ids))
            truncated = total_matched > limit
            debug("ImapReadTool search results",
                  total_matched=total_matched, returning=len(selected_ids),
                  truncated=truncated)

            # Use PEEK to avoid setting \\Seen unless mark_seen requested.
            fetch_spec = "(UID BODY.PEEK[])" if not mark_seen else "(UID RFC822)"

            messages_out = []
            any_body_truncated = False
            for seq_id in selected_ids:
                try:
                    status, msg_data = imap.fetch(seq_id, fetch_spec)
                    if status != "OK" or not msg_data:
                        debug_warn("ImapReadTool fetch failed", id=seq_id.decode("ascii", "replace"))
                        continue
                    # msg_data is a list; the first tuple has the raw message.
                    raw_bytes = None
                    uid = None
                    for item in msg_data:
                        if isinstance(item, tuple) and len(item) >= 2:
                            header_blob = item[0] or b""
                            raw_bytes = item[1]
                            # Try to pull UID out of the response prelude.
                            try:
                                hb = header_blob.decode("ascii", "replace")
                                idx = hb.upper().find("UID ")
                                if idx >= 0:
                                    rest = hb[idx + 4:]
                                    uid_str = ""
                                    for ch in rest:
                                        if ch.isdigit():
                                            uid_str += ch
                                        else:
                                            break
                                    if uid_str:
                                        uid = uid_str
                            except Exception:
                                pass
                            break
                    if not raw_bytes:
                        continue
                    parsed = email_pkg.message_from_bytes(raw_bytes)
                    entry = {}

                    if "uid" in fields:
                        entry["uid"] = uid or seq_id.decode("ascii", "replace")
                    if "from" in fields:
                        entry["from"] = _decode(parsed.get("From", ""))
                    if "to" in fields:
                        entry["to"] = _decode(parsed.get("To", ""))
                    if "subject" in fields:
                        entry["subject"] = _decode(parsed.get("Subject", ""))
                    if "date" in fields:
                        date_raw = parsed.get("Date", "")
                        entry["date"] = date_raw
                        try:
                            dt = parsedate_to_datetime(date_raw)
                            if dt is not None:
                                entry["date_iso"] = dt.isoformat()
                        except Exception:
                            pass
                    if "body_text" in fields or "body_html" in fields:
                        text, html, t_trunc, h_trunc = _extract_bodies(parsed, max_body_bytes)
                        if "body_text" in fields:
                            entry["body_text"] = text
                            if t_trunc:
                                entry["body_text_truncated"] = True
                                any_body_truncated = True
                        if "body_html" in fields:
                            entry["body_html"] = html
                            if h_trunc:
                                entry["body_html_truncated"] = True
                                any_body_truncated = True
                    if "headers" in fields:
                        # Cap headers list to avoid huge dumps.
                        hdrs = []
                        for k, v in parsed.items():
                            hdrs.append({"name": k, "value": _decode(v)})
                            if len(hdrs) >= 50:
                                break
                        entry["headers"] = hdrs

                    messages_out.append(entry)
                except Exception as e:
                    debug_warn("ImapReadTool message decode error",
                               id=seq_id.decode("ascii", "replace"), error=str(e))
                    continue

            data = {
                "messages": messages_out,
                "count": len(messages_out),
                "folder": folder,
                "total_matched": total_matched,
                "truncated": truncated or any_body_truncated,
                "fields": fields,
            }
            debug("ImapReadTool returning", count=len(messages_out),
                  truncated=data["truncated"])
            return DebugLog.embed(_ok(data,
                                      messages=messages_out,
                                      count=len(messages_out),
                                      folder=folder))

        finally:
            if imap is not None:
                try:
                    try:
                        imap.close()
                    except Exception:
                        pass
                    imap.logout()
                except Exception:
                    pass
