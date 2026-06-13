# Testing — email_toolkit

Upload **email_toolkit.zip** in the workspace file browser, add a Custom Tool node, set the Tool Class Name, then use the Test panel with the values below. Mirror the Display Name / Description / Input Schema from the package's `tool_config.json`.

> Prerequisite: Needs an SMTP relay (e.g. OCI Email Delivery).

## SmtpEmailTool
Send email via SMTP.

Config to set:
- smtp_host = smtp.email.<region>.oci.oraclecloud.com
- smtp_username = {{smtp_username}}
- smtp_password = {{smtp_password}}
- from_addr = <verified sender>
- allowed_recipient_domains = (optional guardrail)

**Test 1: Send**

| Field | Value |
|-------|-------|
| `to` | `<your address>` |
| `subject` | `AgentFlow test` |
| `body` | `Hello from a flow.` |

Expected: sent=true; email arrives.

**Test 2: Guardrail**

| Field | Value |
|-------|-------|
| `to` | `a@gmail.com` |
| `subject` | `x` |
| `body` | `y` |

Expected: with allowed_recipient_domains=oracle.com, returns an error.

## Mock files (in this folder's mock_files/)
- `email_message.json`

---
Pass = the documented keys come back; any failure returns `{"error": ...}`. If the agent later says tool use is "forbidden", start a fresh chat session.