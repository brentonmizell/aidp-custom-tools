# Testing — web_toolkit

Upload **web_toolkit.zip** in the workspace file browser, add a Custom Tool node, set the Tool Class Name, then use the Test panel with the values below. Mirror the Display Name / Description / Input Schema from the package's `tool_config.json`.

> Prerequisite: Needs network.

## WebFetchTool
Fetch a page and return clean readable text.

**Test 1: Fetch a test page**

| Field | Value |
|-------|-------|
| `url` | `https://httpbin.org/html` |

Expected: status 200, readable text.

## WebhookSenderTool
POST a message to Slack/Teams/generic webhook.

Config to set:
- webhook_url = https://httpbin.org/post
- style = generic

**Test 1: Send to echo**

| Field | Value |
|-------|-------|
| `message` | `test from AgentFlow` |
| `title` | `Smoke` |

Expected: delivered=true; echo shows your JSON.

## Mock files (in this folder's mock_files/)
- `web_test_urls.txt`
- `sample_page.html`

---
Pass = the documented keys come back; any failure returns `{"error": ...}`. If the agent later says tool use is "forbidden", start a fresh chat session.