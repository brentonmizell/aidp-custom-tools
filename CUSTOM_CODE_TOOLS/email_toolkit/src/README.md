# email_toolkit

Send email via SMTP (OCI Email Delivery or any relay) and read via IMAP.

Built on the AIDP Custom Tools framework. stdlib only - no dependencies.

## Tools
- **SmtpEmailTool** - send via SMTP. Configure host/port/credentials/from in
  config. Defaults target OCI Email Delivery (STARTTLS on 587). Supports
  to/cc/bcc (comma OR semicolon separated), plain or HTML body, priority
  hint, an optional `allowed_recipient_domains` guard, and a bounded retry
  on transient connection errors. STARTTLS is auto-attempted whenever the
  server advertises it. BCC is routed in the envelope, not exposed in headers.
- **ImapReadTool** - fetch messages from an IMAP mailbox. Choose folder,
  filter by unread / since-date / last-N, and select per-message fields
  (uid, from, to, subject, date, body_text, body_html, headers). Bodies
  are capped at `max_body_bytes` per part; the response sets
  `truncated: true` when any cap is hit. Overlaps the Gmail/Outlook OAuth
  integrations - prefer those for full access.

## Return envelope
All tools return:
```json
{ "ok": true, "data": { ... } }
```
or, on failure:
```json
{ "ok": false, "error": "...", "error_type": "..." }
```
Legacy top-level keys (e.g. `sent`, `to`, `subject`, `count`, `folder`)
are preserved alongside `data` for back-compat with existing callers.

## Config notes
- Passwords via `{{template}}` variables, never hard-coded.
- For OCI Email Delivery: set `smtp_host` to
  `smtp.email.<region>.oci.oraclecloud.com`, generate SMTP credentials for the
  sending user, and verify the `from_addr` as an approved sender.
- Set `allowed_recipient_domains` (comma-separated) to restrict who the tool can
  email - a useful guardrail for an agent-driven sender.
- `max_retries` (SMTP) caps transient-error retries (default 2).
- `max_messages` and `max_body_bytes` (IMAP) cap how much data a single call
  can pull back.

## Build
```bash
zip -r email_toolkit.zip tool_implementation.py tool_config.json requirements.txt README.md utils/ -x "*__pycache__*" "*.pyc"
```
