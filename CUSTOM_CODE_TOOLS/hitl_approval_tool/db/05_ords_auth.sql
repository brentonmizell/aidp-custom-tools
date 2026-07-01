-- HITL Approval — ORDS authentication setup.
--
-- Pick ONE of the two blocks below. Do NOT run both.
--
-- Block A  (quick-smoke-test) — schema owner does everything.
--          Skip Block A on any deployment beyond a personal ADB sandbox.
--
-- Block B  (production, recommended) — dedicated DB user, gated by an ORDS
--          privilege attached to the /hitl/* URL patterns. The HITL tool
--          authenticates as this user; the schema owner's password is
--          never used at runtime.
--
-- Both blocks assume steps 01-04 already ran in the HITL_SVC schema.
-- Block B assumes you are logged in as ADMIN for the CREATE USER piece,
-- then switch to HITL_SVC for the ORDS admin calls.

--==========================================================================
-- BLOCK A — Quick smoke test (uses schema owner for basic auth)
--==========================================================================
-- Nothing to do. ORDS on ADB accepts basic auth with any DB user that has
-- CREATE SESSION and belongs to a schema where ORDS.ENABLE_SCHEMA was called.
--
-- Tool config:
--   ords_username = HITL_SVC
--   ords_password = <HITL_SVC schema password>
--
-- To make Block A the current setup, do nothing. To switch AWAY from Block
-- A later, don't roll back — just apply Block B and change the tool's
-- credential bundle to point at the new user.

--==========================================================================
-- BLOCK B — Production: dedicated DB user + ORDS role + ORDS privilege
--==========================================================================
--
-- Step B1 — as ADMIN, create the DB user the HITL tool will authenticate as.
--            No table privileges; ORDS handlers run in HITL_SVC context, not
--            as this user. This user only needs to LOG IN.
--
--            Store the password in your Credential Store bundle immediately
--            after this CREATE — it never needs to be typed again.
--
CREATE USER hitl_tool
  IDENTIFIED BY "REPLACE_WITH_STRONG_PASSWORD_FROM_VAULT"
  DEFAULT TABLESPACE data
  TEMPORARY TABLESPACE temp;
GRANT CREATE SESSION TO hitl_tool;
-- Optional: keep the account from expiring on a policy the tool can't rotate.
ALTER USER hitl_tool PROFILE default;
ALTER USER hitl_tool ACCOUNT UNLOCK;

--
-- Step B2 — as HITL_SVC (owner of the /hitl/ module), create the ORDS
--            role and privilege. The privilege gates the two URL patterns
--            our module publishes and requires the role for access.
--
BEGIN
  ORDS.create_role(p_role_name => 'HITL Client');
  COMMIT;
END;
/

BEGIN
  ORDS.define_privilege(
    p_privilege_name => 'hitl.client',
    p_roles          => ORDS.varchar2_table('HITL Client'),
    p_patterns       => ORDS.varchar2_table(
                          '/hitl/approvals',
                          '/hitl/approvals/*'
                        ),
    p_label          => 'HITL Client',
    p_description    => 'Access to HITL approval REST endpoints');
  COMMIT;
END;
/

--
-- Step B3 — grant the ORDS role to the DB user. This is what makes calls
--            to /hitl/approvals return 200 for HITL_TOOL and 401 for
--            everyone else.
--
--            Note: the exact PL/SQL call for "grant an ORDS role to a DB
--            user" has drifted across ORDS versions. Try these in order;
--            the first one that exists on your ADB will succeed. Comment
--            out the ones that error.
--
-- Option B3.i (ORDS 22.4+):
BEGIN
  ORDS_ADMIN.grant_role(
    p_role_name => 'HITL Client',
    p_user_name => 'HITL_TOOL');
  COMMIT;
END;
/

-- Option B3.ii (older ORDS metadata direct path — uncomment ONLY if B3.i
-- returns "PLS-00201: identifier 'ORDS_ADMIN.GRANT_ROLE' must be declared"):
--
-- INSERT INTO ords_metadata.ords_user_roles (user_name, role_name)
-- VALUES ('HITL_TOOL', 'HITL Client');
-- COMMIT;
--
-- Option B3.iii (Database Actions UI — no SQL needed):
-- Log into Database Actions as ADMIN → REST → Security → Roles.
-- Find "HITL Client", click the ⋮ menu, "Grant Role to User", pick
-- HITL_TOOL. Save.

--==========================================================================
-- VERIFY (either block) — hit the endpoints from a shell
--==========================================================================
-- After apply, test from a terminal (replace <adb-host> and <password>):
--
--   # Should return 200 {"status":"created", ...} on Block A or Block B:
--   curl -u HITL_TOOL:<password> -X POST \
--     "https://<adb-host>/ords/hitl_svc/hitl/approvals" \
--     -H "Content-Type: application/json" \
--     -d '{"approval_id":"SMOKE1","action_summary":"smoke",
--          "action_payload":"{}","requester_ref":"+15550001111",
--          "approver_allow":"[\"+15559998888\"]","ttl_hours":1}'
--
--   # Should return 401 without credentials on Block B (and 200 on
--   # Block A because BASIC auth is off):
--   curl -X POST "https://<adb-host>/ords/hitl_svc/hitl/approvals" ...
--
-- Clean up the smoke test row afterwards:
--   DELETE FROM hitl_approvals WHERE approval_id = 'SMOKE1';
--   COMMIT;

--==========================================================================
-- ROLLBACK (Block B only)
--==========================================================================
-- If you need to remove Block B and go back to Block A:
--
-- BEGIN
--   ORDS_ADMIN.revoke_role(p_role_name => 'HITL Client',
--                          p_user_name => 'HITL_TOOL');
--   ORDS.delete_privilege(p_name => 'hitl.client');
--   ORDS.delete_role(p_role_name => 'HITL Client');
--   COMMIT;
-- END;
-- /
-- DROP USER hitl_tool CASCADE;
