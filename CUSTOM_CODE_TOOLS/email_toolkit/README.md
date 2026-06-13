# email_toolkit

Send email via SMTP (OCI Email Delivery or any relay) and read via IMAP.

Built on the AIDP Custom Tools framework. stdlib only — no dependencies.

## Tools
- **SmtpEmailTool** — send via SMTP. Configure host/port/credentials/from in
  config. Defaults target OCI Email Delivery (STARTTLS on 587). Supports
  to/cc/bcc, plain or HTML body, and an optional `allowed_recipient_domains`
  guard. BCC is routed in the envelope, not exposed in headers.
- **ImapReadTool** — read/search a mailbox over IMAP, returns summaries.
  Overlaps the Gmail/Outlook OAuth integrations; prefer those for full access.

## Config notes
- Passwords via `{{template}}` variables, never hard-coded.
- For OCI Email Delivery: set `smtp_host` to
  `smtp.email.<region>.oci.oraclecloud.com`, generate SMTP credentials for the
  sending user, and verify the `from_addr` as an approved sender.
- Set `allowed_recipient_domains` (comma-separated) to restrict who the tool can
  email — a useful guardrail for an agent-driven sender.

## Build
```bash
zip -r email_toolkit.zip tool_implementation.py tool_config.json requirements.txt README.md utils/ -x "*__pycache__*" "*.pyc"
```
