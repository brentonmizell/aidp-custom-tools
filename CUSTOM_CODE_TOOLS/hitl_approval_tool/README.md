# hitl_approval_tool — human-in-the-loop approval gate for AgentFlow

Two AIDP custom tools that gate high-impact agent actions on out-of-band
human approval. **Nothing blocks, no compute is pinned.** State lives in
an ADB table; the approver replies on SMS; the captured action executes
the moment approval arrives.

## What each tool is for

| Tool | Who calls it | What it does |
|---|---|---|
| `OpenApprovalTool` | The policy/decision agent, when a proposed action exceeds a threshold (e.g. dispatch cost > $3500). | Writes a pending row via ORDS, texts the approver with the summary + short ID, returns `{approval_id, status:"submitted"}`. Agent tells the requester "pending, ref K7M2Q4". Turn ends. |
| `ResolveApprovalTool` | The HITL agent, when the inbound relay routes the approver's decision turn. | ORDS runs the atomic authorize + compare-and-set procedure. Four outcomes: `unknown` / `unauthorized` / `already_decided` / `ok`. On `ok+approve`, executes the captured payload (webhook or queued). SMSes the requester the outcome. |

## Why not interrupt/resume?

Productized `/chat` never issues `Command(resume=...)`, and a sandboxed
custom tool can't hold a process open for hours. So the tool records the
pending action and returns immediately; the approver's reply is a normal
later turn, not a resume. Nothing blocks, no compute is pinned, and it runs
in the low-code canvas as built.

## Credentials

**Use the Credential Store (or OCI Vault) — never plaintext.** See
[`../CREDENTIALS.md`](../CREDENTIALS.md) for the full pattern.

Create a SECRET_TOKEN credential (or OCI Vault JSON secret) with:

| Key | Value |
|---|---|
| `ords_base_url` | Base URL of the ORDS module, e.g. `https://<db-host>/ords/aidp/hitl`. Optional here — can also live in `conf.ords_base_url`. |
| `ords_username` | ORDS user with EXECUTE on `resolve_approval` and INSERT on `hitl_approvals`. |
| `ords_password` | Password for the above. |
| `twilio_account_sid` | Twilio account SID (starts with `AC…`). |
| `twilio_auth_token` | Twilio auth token. |
| `twilio_from_number` | E.164 sender number, e.g. `+15551234567`. |

Set both tools' `conf.credential_name` to the credential's display name (or
Vault OCID). Same bundle for both tools.

## Database setup

**Full ADB runbook with copy-paste SQL, screenshots-in-prose, and a
smoke-test cURL section is [`db/README.md`](db/README.md).** Follow it top
to bottom on a fresh Autonomous Database. ~15 minutes.

Five SQL files, applied in order:

1. [`db/01_schema.sql`](db/01_schema.sql) — creates `hitl_approvals` + index.
2. [`db/02_resolve_procedure.sql`](db/02_resolve_procedure.sql) — the atomic
   authorize + compare-and-set procedure. `SQL%ROWCOUNT` on the conditional
   `UPDATE` is the actual gate; the preceding `SELECT` is only for the
   `NO_DATA_FOUND` and `unauthorized` messages.
3. [`db/03_ords_module.sql`](db/03_ords_module.sql) — publishes
   `POST /hitl/approvals` (insert) and `POST /hitl/approvals/{id}/resolve`
   (calls the procedure, returns JSON via `apex_json`).
4. [`db/04_sweep_job.sql`](db/04_sweep_job.sql) — `DBMS_SCHEDULER` job that
   flips `status='pending' AND expires_at < SYSTIMESTAMP` rows to
   `status='expired'`, every 15 minutes.
5. [`db/05_ords_auth.sql`](db/05_ords_auth.sql) — ORDS authentication. Pick
   ONE of two blocks:
   - **Block A (quick smoke test):** nothing to run. Basic auth uses the
     schema owner's credentials. Personal sandbox only.
   - **Block B (production):** creates a dedicated `HITL_TOOL` DB user with
     just `CREATE SESSION`, an ORDS role `HITL Client`, and an ORDS
     privilege that requires the role on the two URL patterns. Includes
     an `ORDS_ADMIN.grant_role` call plus two fallbacks for older ORDS
     versions.

The HITL tool authenticates via HTTP BASIC with `ords_username` +
`ords_password` from the credential bundle. Path A → user = `HITL_SVC`.
Path B → user = `HITL_TOOL`.

## Tool setup

1. Build + upload the zip:
   ```
   cd CUSTOM_CODE_TOOLS/hitl_approval_tool
   zip -r hitl_approval_tool.zip src/ -x "*__pycache__*" "*.pyc"
   ```
2. AIDP → Tools → Upload zip. Both classes are registered from the
   manifest.
3. Set both tools' `conf.credential_name` (or `conf.ords_base_url` +
   credential_name if you didn't put the URL in the bundle).
4. Attach `OpenApprovalTool` to the policy/decision agent.
5. Attach `ResolveApprovalTool` to the HITL agent.
6. Configure the inbound-SMS relay so approver-number turns route straight
   to the HITL agent (recommended — deterministic). Alternative: the
   supervisor LLM parses "approve/reject `<ID>`" and routes to HITL.

## Payload execution (approve path)

The tool never `eval()`s or `exec()`s the payload. On `ok+approve`:

- If `conf.execute_webhook_url` (or `action_payload.execute_webhook_url`)
  is set, the tool `POST`s the payload to it with `application/json`.
  Basic-auth uses the ORDS creds by default. The webhook can be another
  AIDP tool exposed as REST, an Oracle Function, or any endpoint that
  accepts JSON.
- Otherwise the tool returns `{mode: "queued", action_payload: {...}}`
  and the caller flow (a downstream node) dispatches. Useful when the
  action is another AIDP tool call rather than a generic HTTP webhook.

## Edge cases

| Case | Behavior |
|---|---|
| Wrong / unknown ID | Approver SMSed "No approval found for ID X." Nothing changes. |
| Sender not on allowlist | Approver SMSed "not authorized." Nothing changes. Row untouched. |
| Already decided / redelivered SMS | `SQL%ROWCOUNT=0` → returned as `already_decided`. No re-execution. Approver SMSed. |
| Two approvers tap near-simultaneously | Atomic UPDATE lets exactly one win. The other gets `already_decided`. |
| Expired (past TTL) | Swept to `expired` by the DBMS_SCHEDULER job. Requester notification is left to the caller flow (or an optional expiry queue — see `db/04_sweep_job.sql` comments). |
| ORDS insert fails after ID generation | Tool returns `{ok:false, error_type:"ORDSError", approval_id:…}` — no phantom SMS because SMS is sent AFTER the insert succeeds. |
| SMS to approver fails (all numbers) | Tool returns `{ok:false, error_type:"SMSError", approval_id:…}` — but the DB row is already `pending`. Manual follow-up needed; the row will time out via the sweep. |
| Execution fails after approve | Row stays `approved`. Requester SMSed the failure text. `execution.mode=dispatch_failed` in the result envelope. |
| Malformed `action_payload` in DB | Rare (only if the OPEN insert corrupted it). Tool returns `{ok:false, error_type:"ORDSError"}`; row stays `approved` so a human can re-dispatch. |

## Security invariants (do not weaken)

- **Identity authorizes, ID correlates.** `sender_ref` must be the verified
  inbound-relay number, NOT user-typed. Anyone who sees the ID can guess
  it; only the verified allowlist member can decide.
- **Atomic status flip.** The `UPDATE ... WHERE status='pending'` clause
  is the only correctness check that matters. Do not add "check status
  before update" logic — it's a race.
- **Execute the captured payload, not the conversation.** Re-running the
  agent off the transcript can reach a different decision.
- **TTL is a column + a scheduled job.** Never a live compute wait.

## Verification harness

`verify_wiring.py` (this directory) exercises both tools with mocked ORDS
and mocked Twilio. Covers:

- open: happy path (row inserted, SMS sent, returns `submitted`)
- open: SMS failure (row still written, error surfaced with approval_id)
- resolve: `unknown` / `unauthorized` / `already_decided` / `ok+approve` /
  `ok+reject`
- resolve: approve path with `execute_webhook_url` set — webhook POST fires
  and its response is captured in `execution.response`
- resolve: approve path with no webhook — payload returned as queued

Run: `python verify_wiring.py`.

## Files

```
hitl_approval_tool/
├── README.md                        ← this file
├── verify_wiring.py                 ← standalone dev-box smoke test
├── db/
│   ├── 01_schema.sql
│   ├── 02_resolve_procedure.sql
│   ├── 03_ords_module.sql
│   └── 04_sweep_job.sql
└── src/
    ├── requirements.txt
    ├── tool_config.json
    ├── tool_implementation.py       ← OpenApprovalTool + ResolveApprovalTool
    └── utils/
        ├── config_utils.py          ← get_cfg / ok / fail
        └── credential_resolver.py   ← synced from _shared/
```
