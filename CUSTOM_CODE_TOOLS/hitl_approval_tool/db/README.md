# HITL Approval — Autonomous Database runbook

Explicit, ADB-specific runbook for the SQL in this folder. Follow top to
bottom on a fresh ADB (Autonomous AI Database or plain ADB — same steps).

## What you need before you start

- An Autonomous Database instance (Always Free tier works)
- The **ADMIN** password for that DB
- OCI Console access for Database Actions

Total time: about 15 minutes.

---

## Step 0 — open the SQL worksheet

1. OCI Console → **Databases** → **Autonomous Database** → click your DB.
2. Click **Database Actions** (blue button, top of the page).
3. In the tile grid, click **SQL**.
4. When prompted for a login, use `ADMIN` and its password.

You now have a SQL worksheet. Everything below runs here.

---

## Step 1 — create the HITL schema

*Run this block as ADMIN.*

```sql
-- One-time: create the schema that will own the table + procedure + ORDS module.
CREATE USER hitl_svc IDENTIFIED BY "PickAStrongPasswordHere_UseAVaultLater";
GRANT CONNECT, RESOURCE, CREATE SESSION TO hitl_svc;
GRANT UNLIMITED TABLESPACE TO hitl_svc;

-- Turn on ORDS for this schema. p_url_mapping_pattern is the URL segment
-- that identifies the schema in ORDS URLs (e.g. .../ords/hitl_svc/...).
-- p_auto_rest_auth=FALSE means AutoREST (table CRUD) is off — we only
-- publish the two custom handlers we wrote.
BEGIN
  ORDS_ADMIN.ENABLE_SCHEMA(
    p_enabled             => TRUE,
    p_schema              => 'HITL_SVC',
    p_url_mapping_type    => 'BASE_PATH',
    p_url_mapping_pattern => 'hitl_svc',
    p_auto_rest_auth      => FALSE
  );
  COMMIT;
END;
/
```

**Verify:** `SELECT * FROM ords_metadata.ords_schemas WHERE schema_name = 'HITL_SVC';` returns one row.

---

## Step 2 — switch to the HITL_SVC schema

Top-right corner of Database Actions → click the user avatar → **Sign Out**.
Log back in as `HITL_SVC` with the password from Step 1.

All remaining steps run as HITL_SVC unless the SQL comment says otherwise.

---

## Step 3 — run the schema + procedure + module + sweep

Open each file, paste into the worksheet, click **Run Script** (F5). In
order:

```
01_schema.sql            → creates hitl_approvals table + index
02_resolve_procedure.sql → creates resolve_approval PL/SQL procedure
03_ords_module.sql       → publishes POST /hitl/approvals and
                           POST /hitl/approvals/{id}/resolve
04_sweep_job.sql         → schedules the 15-min TTL sweep
```

Expected output after each: `PL/SQL procedure successfully completed.`
or `Table HITL_APPROVALS created.` — no errors.

If you re-run any of these, benign errors are OK:
- `ORA-00955: name is already used by an existing object` → already ran
- `ORA-20001: Module name already exists` → already ran; delete it first
  with `BEGIN ORDS.delete_module('hitl'); COMMIT; END;` if you're iterating

---

## Step 4 — set up ORDS authentication

**Pick one path.** Do not run both.

### Path A — Quick smoke test (schema owner as the API user)

For personal ADB sandboxes only. Nothing to run — ORDS on ADB accepts basic
auth for any schema that had `ORDS_ADMIN.ENABLE_SCHEMA` called on it.

The HITL tool authenticates with:
- `ords_username = HITL_SVC`
- `ords_password = <the schema password from Step 1>`

Skip to Step 5 and use these credentials.

### Path B — Production (dedicated user + ORDS role + privilege)

Run [`05_ords_auth.sql`](05_ords_auth.sql), **Block B only**. It:

1. Creates a DB user `HITL_TOOL` with only `CREATE SESSION`. No table
   privileges — ORDS handlers run as HITL_SVC, not as this user.
2. Creates an ORDS role `HITL Client`.
3. Creates an ORDS privilege `hitl.client` that:
   - Gates the URL patterns `/hitl/approvals` and `/hitl/approvals/*`
   - Requires the `HITL Client` role
4. Grants the role to `HITL_TOOL` via `ORDS_ADMIN.grant_role`.

If step 4 errors with `PLS-00201: identifier 'ORDS_ADMIN.GRANT_ROLE' must be
declared` (older ORDS), use the fallback INSERT INTO `ords_metadata.ords_user_roles`
that's commented at the bottom of `05_ords_auth.sql`. Or use the Database
Actions UI: **REST → Security → Roles → HITL Client → Grant Role to User → HITL_TOOL**.

The HITL tool then authenticates with:
- `ords_username = HITL_TOOL`
- `ords_password = <what you set in Block B, Step B1>`

The `HITL_SVC` password never leaves the DB after this.

---

## Step 5 — find your ORDS base URL

You need this URL to configure the tool.

**Option 5.i (fastest):** in Database Actions → **REST** tile → **Modules**
→ click `hitl` → the URL at the top of the page is
`https://…/ords/hitl_svc/hitl/`. That's what goes in `conf.ords_base_url`.

**Option 5.ii (via SQL):**
```sql
SELECT '/ords/' || url_mapping_pattern || '/hitl/' AS ords_base_path
  FROM user_ords_schemas;
```
Prepend the ADB's REST hostname (visible in **OCI Console → your ADB → Tool Configuration → Database Actions URL** — copy the host portion, drop everything after `.oraclecloudapps.com`).

Result looks like:
```
https://myadb.adb.us-ashburn-1.oraclecloudapps.com/ords/hitl_svc/hitl/
```

---

## Step 6 — smoke-test the endpoints

From a terminal on your dev box (replace the URL, user, password):

```bash
# Insert a pending row via the ORDS endpoint
curl -u HITL_TOOL:'<password>' -X POST \
  "https://<your-adb-host>/ords/hitl_svc/hitl/approvals" \
  -H "Content-Type: application/json" \
  -d '{
    "approval_id":"SMOKE1",
    "action_summary":"smoke test — safe to delete",
    "action_payload":"{}",
    "requester_ref":"+15550001111",
    "approver_allow":"[\"+15559998888\"]",
    "conversation_ref":"smoke",
    "ttl_hours":1
  }'
```

Expected: `{"status":"created","approval_id":"SMOKE1"}` with HTTP 200.

Now the resolve endpoint (this exercises the atomic procedure):

```bash
curl -u HITL_TOOL:'<password>' -X POST \
  "https://<your-adb-host>/ords/hitl_svc/hitl/approvals/SMOKE1/resolve" \
  -H "Content-Type: application/json" \
  -d '{"decision":"approved","sender":"+15559998888"}'
```

Expected: `{"result":"ok","payload":"{}","requester_ref":"+15550001111"}`.

Try it again with the same body — you should get `{"result":"already_decided"}`.
Try with a `sender` not in the allow list — `{"result":"unauthorized"}`.

Clean up:

```sql
DELETE FROM hitl_approvals WHERE approval_id = 'SMOKE1';
COMMIT;
```

**If those three curl calls behave as documented, the DB side is done.**

---

## Step 7 — put the ORDS credentials in the tool's Credential Store bundle

Add these keys to whatever SECRET_TOKEN credential (or OCI Vault secret)
you're using for the HITL tool. Same bundle for `OpenApprovalTool` and
`ResolveApprovalTool`.

| Key | Value (Path A) | Value (Path B) |
|---|---|---|
| `ords_base_url` | `https://<adb-host>/ords/hitl_svc/hitl/` | same |
| `ords_username` | `HITL_SVC` | `HITL_TOOL` |
| `ords_password` | schema password | dedicated user password |
| `twilio_account_sid` | `AC…` | same |
| `twilio_auth_token` | Twilio token | same |
| `twilio_from_number` | `+15551234567` | same |

Point `conf.credential_name` in both tools at this credential and you're
done — the tool auto-resolves ORDS + Twilio creds from the same bundle.

---

## Troubleshooting

| Symptom | Cause + fix |
|---|---|
| `curl` returns 401 on Path A | Basic auth is off for the schema. Re-run `ORDS_ADMIN.ENABLE_SCHEMA` in Step 1 with `p_auto_rest_auth => FALSE` (that's the default for named handlers) and try again. |
| `curl` returns 404 | ORDS URL segment mismatch. The URL is `/ords/<url_mapping_pattern>/<module_base_path>/<endpoint>`. Both segments come from the `ORDS.` package calls — check `SELECT * FROM user_ords_schemas;` and `SELECT * FROM user_ords_modules;`. |
| `curl` returns 500 with `ORA-00942: table or view does not exist` | You ran Step 3 as ADMIN by mistake, so the table is in the ADMIN schema. Drop it there, log back in as HITL_SVC, re-run. |
| `resolve` returns `{"result":"unauthorized"}` for the correct sender | JSON quoting issue in `approver_allow`. Verify with `SELECT approver_allow FROM hitl_approvals WHERE approval_id='SMOKE1';` — should look like `["+15559998888"]` including the double quotes. |
| Sweep job doesn't fire | Check with `SELECT job_name, state, last_start_date FROM user_scheduler_jobs WHERE job_name='HITL_EXPIRE_PENDING';`. `STATE` should be `SCHEDULED`. If not, re-run `04_sweep_job.sql`. |
| `ORDS_ADMIN.grant_role` errors on Path B | Older ORDS version. Use the `INSERT INTO ords_metadata.ords_user_roles` fallback commented at the bottom of `05_ords_auth.sql`, or use the Database Actions UI. |

---

## File index

| File | What it does | Run as |
|---|---|---|
| [`01_schema.sql`](01_schema.sql) | Creates `hitl_approvals` table + index | HITL_SVC |
| [`02_resolve_procedure.sql`](02_resolve_procedure.sql) | Atomic authorize + compare-and-set procedure | HITL_SVC |
| [`03_ords_module.sql`](03_ords_module.sql) | Publishes the two REST endpoints | HITL_SVC |
| [`04_sweep_job.sql`](04_sweep_job.sql) | Schedules the TTL sweep every 15 min | HITL_SVC |
| [`05_ords_auth.sql`](05_ords_auth.sql) | Auth setup: Block A (quick) or Block B (production) | ADMIN for CREATE USER, HITL_SVC for the ORDS package calls |
