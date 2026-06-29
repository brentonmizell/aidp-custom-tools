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

## Credentials — REQUIRED for SmtpEmailTool

**Use the AIDP Credential Store. Do not hand-paste passwords into conf.**

1. AIDP → Settings → Credentials → New. Type: `SECRET_TOKEN`.
2. Display name: e.g. `smtp_oci_email`.
3. Keys:

   | Key | Value |
   |---|---|
   | `host` | e.g. `smtp.email.us-ashburn-1.oci.oraclecloud.com` |
   | `port` | typically `587` |
   | `username` | OCI SMTP credential username |
   | `password` | OCI SMTP credential password |
   | `from_address` | the approved sender `From:` |

4. Set `conf.credential_name` to the display name above. The tool calls
   `aidputils.secrets.get(name)` at invoke time — no plaintext password in
   conf, in source, or in the zip.

For OCI Email Delivery: generate SMTP credentials under the sending user
*after* verifying the `from_address` as an approved sender. **ImapReadTool**
uses a separate IMAP credential with the same `SECRET_TOKEN` pattern (keys:
`host` / `port` / `username` / `password`); use a second credential with a
different display name and a per-tool `conf.credential_name`.

Full pattern: [`../CREDENTIALS.md`](../CREDENTIALS.md) and the working
reference at
[`../credential_store_auth_sample/`](../credential_store_auth_sample/README.md).

## Config notes
- Set `allowed_recipient_domains` (comma-separated) to restrict who the tool
  can email — a useful guardrail for an agent-driven sender.

## Build
```bash
zip -r email_toolkit.zip tool_implementation.py tool_config.json requirements.txt README.md utils/ -x "*__pycache__*" "*.pyc"
```
