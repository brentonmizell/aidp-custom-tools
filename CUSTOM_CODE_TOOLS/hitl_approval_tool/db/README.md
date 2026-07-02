# HITL — Autonomous Database setup

Everything installs from **one file**: [`install.sql`](install.sql).

## Install (~5 minutes)

1. **OCI Console → Databases → Autonomous Database → your DB →
   Database Actions** (blue button) → **SQL** tile. Log in as **ADMIN**.
2. Open `install.sql` in a text editor. Find/replace both passwords:
   - `CHANGE_ME_SchemaOwnerPw#2026` → a strong password for `HITL_SVC`
     (the schema owner — not used at runtime after install)
   - `CHANGE_ME_ToolUserPw#2026` → a strong password for `HITL_TOOL`
     (what the custom tool authenticates as; goes in your credential bundle)
3. Paste the whole file into the worksheet. **Run Script (F5)** — not "Run
   Statement".
4. Check the output of the verification query at the end. Expected **7 rows**:

   ```
   table: HITL_USERS
   table: HITL_INCIDENTS
   table: HITL_APPROVALS
   proc: RESOLVE_APPROVAL
   job: HITL_EXPIRE_PENDING
   user: HITL_SVC
   user: HITL_TOOL
   ```

5. Put `HITL_TOOL` + its password + your ORDS base URL into the credential
   bundle (see the main [README](../README.md), Step 3).

The installer is **re-runnable**: users are created only if absent, tables
only if absent, and the ORDS module + sweep job are dropped and re-created
so handler edits take effect. Running it twice is safe.

## What it creates

| Object | What it is |
|---|---|
| `HITL_SVC` schema | Owns everything. ORDS-enabled with URL segment `hitl_svc`. |
| `hitl_users` | Channel-agnostic identity allowlist (requester / approver / both). |
| `hitl_incidents` | Durable incidents; `last_activity_at` powers session windows. |
| `hitl_approvals` | Approval gates with TTL; `incident_id` links back (nullable). |
| `resolve_approval` proc | Atomic authorize + compare-and-set. `SQL%ROWCOUNT` on the conditional `UPDATE` decides who wins; also un-parks the linked incident. |
| ORDS module `hitl` | 7 endpoints: `/users/lookup`, `/users`, `/incidents`, `/incidents/get`, `/incidents/update`, `/approvals`, `/approvals/{id}/resolve`. |
| `HITL_EXPIRE_PENDING` job | Every 15 min: un-parks incidents whose gate expired, flips overdue pendings to `expired`. |
| `HITL_TOOL` user + ORDS role/privilege | Login-only DB user (CREATE SESSION, nothing else). ORDS privilege `hitl.client` gates `/hitl/*` and requires the `HITL Client` role, which only HITL_TOOL holds. Blast radius of a leaked tool password = the seven endpoints, not the schema. |

## Find your ORDS base URL

```
https://<adb-host>/ords/hitl_svc/hitl/
```

`<adb-host>` is in OCI Console → your ADB → **Tool Configuration** (copy the
host from the Database Actions URL). Or in Database Actions → REST →
Modules → `hitl` — the full URL is shown at the top.

## Smoke test with curl

```bash
BASE="https://<adb-host>/ords/hitl_svc/hitl"
AUTH="HITL_TOOL:<tool password>"

# 1. Seed a user
curl -u "$AUTH" -X POST "$BASE/users" -H "Content-Type: application/json" \
  -d '{"user_ref":"+15550001111","display_name":"Test User","user_role":"both"}'
# -> {"status":"upserted","user_ref":"+15550001111"}

# 2. Look them up
curl -u "$AUTH" -X POST "$BASE/users/lookup" -H "Content-Type: application/json" \
  -d '{"user_ref":"+15550001111"}'
# -> {"found":true,...,"awaiting_my_decision":[]}

# 3. Open an approval
curl -u "$AUTH" -X POST "$BASE/approvals" -H "Content-Type: application/json" \
  -d '{"approval_id":"SMOKE1","action_summary":"smoke","action_payload":"{}",
       "requester_ref":"+15550001111","approver_allow":"[\"+15550001111\"]","ttl_hours":1}'
# -> {"status":"created","approval_id":"SMOKE1"}

# 4. Resolve it (authorized sender)
curl -u "$AUTH" -X POST "$BASE/approvals/SMOKE1/resolve" -H "Content-Type: application/json" \
  -d '{"decision":"approved","sender":"+15550001111"}'
# -> {"result":"ok","payload":"{}","requester_ref":"+15550001111"}

# 5. Resolve again — the atomic gate
curl -u "$AUTH" -X POST "$BASE/approvals/SMOKE1/resolve" -H "Content-Type: application/json" \
  -d '{"decision":"approved","sender":"+15550001111"}'
# -> {"result":"already_decided"}

# 6. Unauthenticated must 401 (the ORDS privilege at work)
curl -X POST "$BASE/users/lookup" -H "Content-Type: application/json" -d '{}'
# -> 401
```

Clean up:

```sql
DELETE FROM hitl_svc.hitl_approvals WHERE approval_id = 'SMOKE1';
DELETE FROM hitl_svc.hitl_users WHERE user_ref = '+15550001111';
COMMIT;
```

## Troubleshooting

| Symptom | Cause + fix |
|---|---|
| `curl` returns 401 with correct HITL_TOOL creds | The role grant failed silently (older ORDS without `ORDS_ADMIN.grant_role`). Grant via UI: Database Actions as ADMIN → REST → Security → Roles → **HITL Client** → Grant Role to User → `HITL_TOOL`. |
| `curl` returns 404 | URL segment mismatch. It's `/ords/` + schema mapping (`hitl_svc`) + module base (`hitl`) + endpoint. Verify: `SELECT url_mapping_pattern FROM ords_metadata.ords_schemas WHERE schema_name='HITL_SVC';` |
| `ORA-01920: user name conflicts` on re-run | You changed a password in the file and re-ran. Users are created once; rotate with `ALTER USER hitl_tool IDENTIFIED BY "NewPw";` |
| `resolve` says `unauthorized` for the right sender | `approver_allow` isn't a JSON array of strings. Check: `SELECT approver_allow FROM hitl_svc.hitl_approvals WHERE approval_id='…';` — must look like `["+15550001111"]` with the quotes. |
| Sweep never fires | `SELECT state FROM dba_scheduler_jobs WHERE owner='HITL_SVC' AND job_name='HITL_EXPIRE_PENDING';` should say `SCHEDULED`. Re-run the installer if not. |
| `PLS-00201: ORDS_ADMIN.GRANT_ROLE must be declared` in install output | Older ORDS. The installer catches this and prints a note — do the UI grant from the first row of this table. Everything else installed fine. |

## Uninstall

```sql
BEGIN ORDS_ADMIN.delete_module(p_schema=>'HITL_SVC', p_module_name=>'hitl'); END;
/
BEGIN DBMS_SCHEDULER.drop_job('HITL_SVC.HITL_EXPIRE_PENDING', force=>TRUE); END;
/
DROP USER hitl_tool CASCADE;
DROP USER hitl_svc CASCADE;   -- drops tables, proc, and ORDS metadata with it
```
