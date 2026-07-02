-- ===========================================================================
-- HITL (Human-In-The-Loop) — single-file installer for Autonomous Database
-- ===========================================================================
--
--   WHAT THIS CREATES
--     Schema     HITL_SVC          owns everything below
--     Tables     hitl_users        who can request / approve (channel-agnostic)
--                hitl_incidents    durable business state per incident
--                hitl_approvals    pending/decided approval gates
--     Procedure  resolve_approval  atomic authorize + compare-and-set
--     ORDS       module 'hitl'     7 REST endpoints (listed below)
--     Job        HITL_EXPIRE_PENDING  15-min TTL sweep
--     Auth       HITL_TOOL DB user + ORDS role/privilege gating /hitl/*
--
--   HOW TO RUN
--     1. OCI Console -> your Autonomous DB -> Database Actions -> SQL
--     2. Log in as ADMIN
--     3. Find/replace the two passwords marked CHANGE_ME below
--     4. Paste this whole file, click Run Script (F5)
--     5. Scroll the output for ORA- errors (re-run errors listed at bottom
--        of this file are benign)
--
--   The entire file runs as ADMIN. Objects are created into HITL_SVC via
--   fully-qualified names; ORDS objects via the ORDS_ADMIN package with
--   p_schema. You never need to log in as HITL_SVC.
--
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- 0. PASSWORDS — change both before running. Store them in your credential
--    bundle (AIDP Credential Store or OCI Vault) immediately; you should
--    never need to type them again.
--      HITL_SVC  = schema owner. Not used at runtime after install.
--      HITL_TOOL = the identity the custom tool authenticates as over HTTPS.
-- ---------------------------------------------------------------------------

-- ===========================================================================
-- 1. Schema owner
-- ===========================================================================
DECLARE
  user_exists NUMBER;
BEGIN
  SELECT COUNT(*) INTO user_exists FROM dba_users WHERE username = 'HITL_SVC';
  IF user_exists = 0 THEN
    EXECUTE IMMEDIATE
      'CREATE USER hitl_svc IDENTIFIED BY "CHANGE_ME_SchemaOwnerPw#2026"';
    EXECUTE IMMEDIATE 'GRANT CONNECT, RESOURCE, CREATE SESSION TO hitl_svc';
    EXECUTE IMMEDIATE 'GRANT UNLIMITED TABLESPACE TO hitl_svc';
  END IF;
END;
/

-- Enable ORDS for the schema (idempotent — re-enabling is a no-op update).
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

-- ===========================================================================
-- 2. Tables
-- ===========================================================================

-- Users: channel-agnostic identities. user_ref is whatever your channel
-- verifies — E.164 phone for SMS, email address, Slack member ID, etc.
DECLARE
  t NUMBER;
BEGIN
  SELECT COUNT(*) INTO t FROM dba_tables
   WHERE owner = 'HITL_SVC' AND table_name = 'HITL_USERS';
  IF t = 0 THEN
    EXECUTE IMMEDIATE q'[
      CREATE TABLE hitl_svc.hitl_users (
        user_ref     VARCHAR2(200) PRIMARY KEY,
        display_name VARCHAR2(200),
        user_role    VARCHAR2(20) DEFAULT 'requester' NOT NULL
                     CONSTRAINT hitl_users_role_ck
                     CHECK (user_role IN ('requester','approver','both')),
        active       CHAR(1) DEFAULT 'Y' NOT NULL
                     CONSTRAINT hitl_users_active_ck CHECK (active IN ('Y','N')),
        notes        VARCHAR2(1000),
        created_at   TIMESTAMP DEFAULT SYSTIMESTAMP
      )]';
  END IF;
END;
/

-- Incidents: the durable unit of work. One row per incident; agents update
-- summary/detail as they learn more; last_activity_at powers the
-- session-window check.
DECLARE
  t NUMBER;
BEGIN
  SELECT COUNT(*) INTO t FROM dba_tables
   WHERE owner = 'HITL_SVC' AND table_name = 'HITL_INCIDENTS';
  IF t = 0 THEN
    EXECUTE IMMEDIATE q'[
      CREATE TABLE hitl_svc.hitl_incidents (
        incident_id      VARCHAR2(16) PRIMARY KEY,
        status           VARCHAR2(20) DEFAULT 'open' NOT NULL
                         CONSTRAINT hitl_incidents_status_ck
                         CHECK (status IN
                           ('open','pending_approval','resolved','closed')),
        requester_ref    VARCHAR2(200) NOT NULL,
        summary          VARCHAR2(2000),
        detail           CLOB,
        created_at       TIMESTAMP DEFAULT SYSTIMESTAMP,
        updated_at       TIMESTAMP DEFAULT SYSTIMESTAMP,
        last_activity_at TIMESTAMP DEFAULT SYSTIMESTAMP
      )]';
    EXECUTE IMMEDIATE q'[
      CREATE INDEX hitl_svc.hitl_incidents_req_act
        ON hitl_svc.hitl_incidents (requester_ref, last_activity_at)]';
  END IF;
END;
/

-- Approvals: the out-of-band gates. incident_id links back to the incident
-- (nullable so the approval gate also works standalone, without incidents).
DECLARE
  t NUMBER;
BEGIN
  SELECT COUNT(*) INTO t FROM dba_tables
   WHERE owner = 'HITL_SVC' AND table_name = 'HITL_APPROVALS';
  IF t = 0 THEN
    EXECUTE IMMEDIATE q'[
      CREATE TABLE hitl_svc.hitl_approvals (
        approval_id       VARCHAR2(16) PRIMARY KEY,
        incident_id       VARCHAR2(16),
        status            VARCHAR2(12) DEFAULT 'pending' NOT NULL
                          CONSTRAINT hitl_approvals_status_ck
                          CHECK (status IN
                            ('pending','approved','rejected','expired')),
        action_summary    VARCHAR2(1000),
        action_payload    CLOB,
        requester_ref     VARCHAR2(200),
        approver_allow    VARCHAR2(1000),
        conversation_ref  VARCHAR2(200),
        created_at        TIMESTAMP DEFAULT SYSTIMESTAMP,
        expires_at        TIMESTAMP,
        decided_by        VARCHAR2(200),
        decided_at        TIMESTAMP,
        execution_result  CLOB
      )]';
    EXECUTE IMMEDIATE q'[
      CREATE INDEX hitl_svc.hitl_approvals_status_exp
        ON hitl_svc.hitl_approvals (status, expires_at)]';
    EXECUTE IMMEDIATE q'[
      CREATE INDEX hitl_svc.hitl_approvals_incident
        ON hitl_svc.hitl_approvals (incident_id)]';
  END IF;
END;
/

-- ===========================================================================
-- 3. Atomic resolve procedure (definer's rights — runs as HITL_SVC)
-- ===========================================================================
CREATE OR REPLACE PROCEDURE hitl_svc.resolve_approval (
  p_id        IN  VARCHAR2,
  p_decision  IN  VARCHAR2,     -- 'approved' | 'rejected'
  p_sender    IN  VARCHAR2,     -- VERIFIED approver ref from the channel
  o_result    OUT VARCHAR2,     -- unknown | unauthorized | already_decided | expired | ok
  o_payload   OUT CLOB,
  o_requester OUT VARCHAR2,
  o_incident  OUT VARCHAR2
) AS
  l_status   VARCHAR2(12);
  l_allow    VARCHAR2(1000);
  l_authz    NUMBER;
BEGIN
  SELECT status, approver_allow, action_payload, requester_ref, incident_id
    INTO l_status, l_allow, o_payload, o_requester, o_incident
    FROM hitl_approvals
   WHERE approval_id = p_id;

  -- Authorization: is p_sender in the approver_allow JSON array?
  SELECT COUNT(*)
    INTO l_authz
    FROM DUAL
   WHERE JSON_EXISTS(l_allow, '$[*]?(@ == $s)' PASSING p_sender AS "s");

  IF l_authz = 0 THEN
    o_result := 'unauthorized';
    o_payload := NULL; o_requester := NULL; o_incident := NULL;
    RETURN;
  END IF;

  -- Give a precise message for the expired case instead of already_decided.
  IF l_status = 'expired' THEN
    o_result := 'expired';
    o_payload := NULL; o_requester := NULL; o_incident := NULL;
    RETURN;
  END IF;

  -- Atomic compare-and-set: exactly one caller can win this UPDATE. A
  -- redelivered message, a double-tap, or a racing second approver all hit
  -- rows=0 and get already_decided. No re-execution, ever.
  UPDATE hitl_approvals
     SET status      = p_decision,
         decided_by  = p_sender,
         decided_at  = SYSTIMESTAMP
   WHERE approval_id = p_id
     AND status      = 'pending';

  IF SQL%ROWCOUNT = 1 THEN
    o_result := 'ok';
    -- Un-park the linked incident so the flow can continue.
    IF o_incident IS NOT NULL THEN
      UPDATE hitl_incidents
         SET status = 'open',
             updated_at = SYSTIMESTAMP,
             last_activity_at = SYSTIMESTAMP
       WHERE incident_id = o_incident
         AND status = 'pending_approval';
    END IF;
    COMMIT;
  ELSE
    o_result := 'already_decided';
    o_payload := NULL; o_requester := NULL; o_incident := NULL;
  END IF;

EXCEPTION
  WHEN NO_DATA_FOUND THEN
    o_result := 'unknown';
    o_payload := NULL; o_requester := NULL; o_incident := NULL;
END resolve_approval;
/

-- ===========================================================================
-- 4. ORDS module — 7 endpoints under /hitl/
-- ===========================================================================
-- Re-runs: delete then re-define so edits to handler source take effect.
BEGIN
  BEGIN
    ORDS_ADMIN.delete_module(p_schema => 'HITL_SVC', p_module_name => 'hitl');
  EXCEPTION WHEN OTHERS THEN NULL;
  END;

  ORDS_ADMIN.define_module(
    p_schema         => 'HITL_SVC',
    p_module_name    => 'hitl',
    p_base_path      => '/hitl/',
    p_items_per_page => 25,
    p_status         => 'PUBLISHED');

  -- ---- POST /users/lookup --------------------------------------------------
  -- Body: {user_ref, session_window_minutes?}
  -- Returns the user row + their most recent incident if it is inside the
  -- session window + any open approvals awaiting them.
  ORDS_ADMIN.define_template(
    p_schema => 'HITL_SVC', p_module_name => 'hitl',
    p_pattern => 'users/lookup');
  ORDS_ADMIN.define_handler(
    p_schema => 'HITL_SVC', p_module_name => 'hitl',
    p_pattern => 'users/lookup', p_method => 'POST',
    p_source_type => 'plsql/block', p_mimes_allowed => 'application/json',
    p_source => q'[
      DECLARE
        l_name   hitl_users.display_name%TYPE;
        l_role   hitl_users.user_role%TYPE;
        l_active hitl_users.active%TYPE;
        l_window NUMBER := NVL(:session_window_minutes, 30);
        l_inc_id  hitl_incidents.incident_id%TYPE;
        l_inc_st  hitl_incidents.status%TYPE;
        l_inc_sum hitl_incidents.summary%TYPE;
        l_inc_act hitl_incidents.last_activity_at%TYPE;
      BEGIN
        BEGIN
          SELECT display_name, user_role, active
            INTO l_name, l_role, l_active
            FROM hitl_users WHERE user_ref = :user_ref;
        EXCEPTION WHEN NO_DATA_FOUND THEN
          apex_json.open_object;
          apex_json.write('found', FALSE);
          apex_json.close_object;
          RETURN;
        END;

        apex_json.open_object;
        apex_json.write('found', TRUE);
        apex_json.write('user_ref', :user_ref);
        apex_json.write('display_name', l_name);
        apex_json.write('user_role', l_role);
        apex_json.write('active', l_active);

        -- Most recent incident inside the session window (the "current
        -- session" check — no separate session table needed).
        BEGIN
          SELECT incident_id, status, summary, last_activity_at
            INTO l_inc_id, l_inc_st, l_inc_sum, l_inc_act
            FROM (SELECT incident_id, status, summary, last_activity_at
                    FROM hitl_incidents
                   WHERE requester_ref = :user_ref
                     AND status IN ('open','pending_approval')
                     AND last_activity_at >
                         SYSTIMESTAMP - NUMTODSINTERVAL(l_window, 'MINUTE')
                   ORDER BY last_activity_at DESC)
           WHERE ROWNUM = 1;
          apex_json.open_object('current_incident');
          apex_json.write('incident_id', l_inc_id);
          apex_json.write('status', l_inc_st);
          apex_json.write('summary', l_inc_sum);
          apex_json.write('last_activity_at',
                          TO_CHAR(l_inc_act, 'YYYY-MM-DD"T"HH24:MI:SS'));
          apex_json.close_object;
        EXCEPTION WHEN NO_DATA_FOUND THEN
          apex_json.write('current_incident', ''); -- explicit empty
        END;

        -- Open approvals this user can decide (only meaningful for approvers).
        apex_json.open_array('awaiting_my_decision');
        IF l_role IN ('approver','both') THEN
          FOR r IN (SELECT approval_id, incident_id, action_summary, expires_at
                      FROM hitl_approvals
                     WHERE status = 'pending'
                       AND JSON_EXISTS(approver_allow,
                             '$[*]?(@ == $s)' PASSING :user_ref AS "s")
                     ORDER BY created_at) LOOP
            apex_json.open_object;
            apex_json.write('approval_id', r.approval_id);
            apex_json.write('incident_id', r.incident_id);
            apex_json.write('action_summary', r.action_summary);
            apex_json.write('expires_at',
                            TO_CHAR(r.expires_at, 'YYYY-MM-DD"T"HH24:MI:SS'));
            apex_json.close_object;
          END LOOP;
        END IF;
        apex_json.close_array;
        apex_json.close_object;
      END;
    ]');

  -- ---- POST /users ----------------------------------------------------------
  -- Body: {user_ref, display_name, user_role} — upsert an allowlist entry.
  -- Admin operation; call from the Test panel or an ops flow, not from chat.
  ORDS_ADMIN.define_template(
    p_schema => 'HITL_SVC', p_module_name => 'hitl', p_pattern => 'users');
  ORDS_ADMIN.define_handler(
    p_schema => 'HITL_SVC', p_module_name => 'hitl',
    p_pattern => 'users', p_method => 'POST',
    p_source_type => 'plsql/block', p_mimes_allowed => 'application/json',
    p_source => q'[
      BEGIN
        MERGE INTO hitl_users u
        USING (SELECT :user_ref AS user_ref FROM dual) s
           ON (u.user_ref = s.user_ref)
         WHEN MATCHED THEN UPDATE SET
              display_name = NVL(:display_name, u.display_name),
              user_role    = NVL(:user_role, u.user_role),
              active       = NVL(:active, u.active)
         WHEN NOT MATCHED THEN INSERT
              (user_ref, display_name, user_role, active)
              VALUES (:user_ref, :display_name,
                      NVL(:user_role,'requester'), NVL(:active,'Y'));
        COMMIT;
        apex_json.open_object;
        apex_json.write('status', 'upserted');
        apex_json.write('user_ref', :user_ref);
        apex_json.close_object;
      END;
    ]');

  -- ---- POST /incidents -------------------------------------------------------
  ORDS_ADMIN.define_template(
    p_schema => 'HITL_SVC', p_module_name => 'hitl', p_pattern => 'incidents');
  ORDS_ADMIN.define_handler(
    p_schema => 'HITL_SVC', p_module_name => 'hitl',
    p_pattern => 'incidents', p_method => 'POST',
    p_source_type => 'plsql/block', p_mimes_allowed => 'application/json',
    p_source => q'[
      BEGIN
        INSERT INTO hitl_incidents (incident_id, requester_ref, summary, detail)
        VALUES (:incident_id, :requester_ref, :summary, :detail);
        COMMIT;
        apex_json.open_object;
        apex_json.write('status', 'created');
        apex_json.write('incident_id', :incident_id);
        apex_json.close_object;
      EXCEPTION WHEN DUP_VAL_ON_INDEX THEN
        :status_code_out := 409;
        apex_json.open_object;
        apex_json.write('error', 'duplicate incident_id');
        apex_json.close_object;
      END;
    ]');

  -- ---- POST /incidents/get ---------------------------------------------------
  -- Body: {incident_id}. Returns the incident + all its approvals. Also
  -- touches last_activity_at (a lookup IS activity — keeps sessions alive).
  ORDS_ADMIN.define_template(
    p_schema => 'HITL_SVC', p_module_name => 'hitl',
    p_pattern => 'incidents/get');
  ORDS_ADMIN.define_handler(
    p_schema => 'HITL_SVC', p_module_name => 'hitl',
    p_pattern => 'incidents/get', p_method => 'POST',
    p_source_type => 'plsql/block', p_mimes_allowed => 'application/json',
    p_source => q'[
      DECLARE
        l_row hitl_incidents%ROWTYPE;
      BEGIN
        BEGIN
          SELECT * INTO l_row FROM hitl_incidents
           WHERE incident_id = UPPER(:incident_id);
        EXCEPTION WHEN NO_DATA_FOUND THEN
          apex_json.open_object;
          apex_json.write('found', FALSE);
          apex_json.close_object;
          RETURN;
        END;

        UPDATE hitl_incidents SET last_activity_at = SYSTIMESTAMP
         WHERE incident_id = l_row.incident_id;
        COMMIT;

        apex_json.open_object;
        apex_json.write('found', TRUE);
        apex_json.write('incident_id', l_row.incident_id);
        apex_json.write('status', l_row.status);
        apex_json.write('requester_ref', l_row.requester_ref);
        apex_json.write('summary', l_row.summary);
        apex_json.write('detail', l_row.detail);
        apex_json.write('created_at',
                        TO_CHAR(l_row.created_at, 'YYYY-MM-DD"T"HH24:MI:SS'));
        apex_json.open_array('approvals');
        FOR r IN (SELECT approval_id, status, action_summary,
                         decided_by, decided_at, expires_at
                    FROM hitl_approvals
                   WHERE incident_id = l_row.incident_id
                   ORDER BY created_at) LOOP
          apex_json.open_object;
          apex_json.write('approval_id', r.approval_id);
          apex_json.write('status', r.status);
          apex_json.write('action_summary', r.action_summary);
          apex_json.write('decided_by', r.decided_by);
          apex_json.write('decided_at',
                          TO_CHAR(r.decided_at, 'YYYY-MM-DD"T"HH24:MI:SS'));
          apex_json.write('expires_at',
                          TO_CHAR(r.expires_at, 'YYYY-MM-DD"T"HH24:MI:SS'));
          apex_json.close_object;
        END LOOP;
        apex_json.close_array;
        apex_json.close_object;
      END;
    ]');

  -- ---- POST /incidents/update -------------------------------------------------
  -- Body: {incident_id, summary?, detail?, status?}
  ORDS_ADMIN.define_template(
    p_schema => 'HITL_SVC', p_module_name => 'hitl',
    p_pattern => 'incidents/update');
  ORDS_ADMIN.define_handler(
    p_schema => 'HITL_SVC', p_module_name => 'hitl',
    p_pattern => 'incidents/update', p_method => 'POST',
    p_source_type => 'plsql/block', p_mimes_allowed => 'application/json',
    p_source => q'[
      BEGIN
        UPDATE hitl_incidents
           SET summary          = NVL(:summary, summary),
               detail           = NVL(:detail, detail),
               status           = NVL(:status, status),
               updated_at       = SYSTIMESTAMP,
               last_activity_at = SYSTIMESTAMP
         WHERE incident_id = UPPER(:incident_id);
        IF SQL%ROWCOUNT = 0 THEN
          :status_code_out := 404;
          apex_json.open_object;
          apex_json.write('error', 'incident not found');
          apex_json.close_object;
        ELSE
          COMMIT;
          apex_json.open_object;
          apex_json.write('status', 'updated');
          apex_json.write('incident_id', UPPER(:incident_id));
          apex_json.close_object;
        END IF;
      END;
    ]');

  -- ---- POST /approvals ---------------------------------------------------------
  ORDS_ADMIN.define_template(
    p_schema => 'HITL_SVC', p_module_name => 'hitl', p_pattern => 'approvals');
  ORDS_ADMIN.define_handler(
    p_schema => 'HITL_SVC', p_module_name => 'hitl',
    p_pattern => 'approvals', p_method => 'POST',
    p_source_type => 'plsql/block', p_mimes_allowed => 'application/json',
    p_source => q'[
      BEGIN
        INSERT INTO hitl_approvals (
          approval_id, incident_id, action_summary, action_payload,
          requester_ref, approver_allow, conversation_ref, expires_at
        ) VALUES (
          :approval_id, UPPER(:incident_id), :action_summary, :action_payload,
          :requester_ref, :approver_allow, :conversation_ref,
          SYSTIMESTAMP + NUMTODSINTERVAL(NVL(:ttl_hours, 48), 'HOUR')
        );
        -- Park the linked incident while it waits.
        IF :incident_id IS NOT NULL THEN
          UPDATE hitl_incidents
             SET status = 'pending_approval',
                 updated_at = SYSTIMESTAMP,
                 last_activity_at = SYSTIMESTAMP
           WHERE incident_id = UPPER(:incident_id);
        END IF;
        COMMIT;
        apex_json.open_object;
        apex_json.write('status', 'created');
        apex_json.write('approval_id', :approval_id);
        apex_json.close_object;
      EXCEPTION WHEN DUP_VAL_ON_INDEX THEN
        :status_code_out := 409;
        apex_json.open_object;
        apex_json.write('error', 'duplicate approval_id');
        apex_json.close_object;
      END;
    ]');

  -- ---- POST /approvals/:id/resolve ----------------------------------------------
  ORDS_ADMIN.define_template(
    p_schema => 'HITL_SVC', p_module_name => 'hitl',
    p_pattern => 'approvals/:id/resolve');
  ORDS_ADMIN.define_handler(
    p_schema => 'HITL_SVC', p_module_name => 'hitl',
    p_pattern => 'approvals/:id/resolve', p_method => 'POST',
    p_source_type => 'plsql/block', p_mimes_allowed => 'application/json',
    p_source => q'[
      DECLARE
        l_result    VARCHAR2(20);
        l_payload   CLOB;
        l_requester VARCHAR2(200);
        l_incident  VARCHAR2(16);
      BEGIN
        resolve_approval(
          p_id        => UPPER(:id),
          p_decision  => :decision,
          p_sender    => :sender,
          o_result    => l_result,
          o_payload   => l_payload,
          o_requester => l_requester,
          o_incident  => l_incident);
        apex_json.open_object;
        apex_json.write('result', l_result);
        IF l_payload IS NOT NULL THEN
          apex_json.write('payload', l_payload);
        END IF;
        IF l_requester IS NOT NULL THEN
          apex_json.write('requester_ref', l_requester);
        END IF;
        IF l_incident IS NOT NULL THEN
          apex_json.write('incident_id', l_incident);
        END IF;
        apex_json.close_object;
      END;
    ]');

  COMMIT;
END;
/

-- ===========================================================================
-- 5. TTL sweep — every 15 minutes, expire overdue pendings and un-park
--    their incidents. Re-run safe (drops + recreates).
-- ===========================================================================
BEGIN
  BEGIN
    DBMS_SCHEDULER.drop_job('HITL_SVC.HITL_EXPIRE_PENDING', force => TRUE);
  EXCEPTION WHEN OTHERS THEN NULL;
  END;
  DBMS_SCHEDULER.create_job(
    job_name        => 'HITL_SVC.HITL_EXPIRE_PENDING',
    job_type        => 'PLSQL_BLOCK',
    job_action      => q'[
      BEGIN
        -- Un-park incidents whose gate is about to expire.
        UPDATE hitl_incidents i
           SET i.status = 'open', i.updated_at = SYSTIMESTAMP
         WHERE i.status = 'pending_approval'
           AND EXISTS (SELECT 1 FROM hitl_approvals a
                        WHERE a.incident_id = i.incident_id
                          AND a.status = 'pending'
                          AND a.expires_at < SYSTIMESTAMP);
        UPDATE hitl_approvals
           SET status = 'expired'
         WHERE status = 'pending'
           AND expires_at < SYSTIMESTAMP;
        COMMIT;
      END;
    ]',
    start_date      => SYSTIMESTAMP,
    repeat_interval => 'FREQ=MINUTELY; INTERVAL=15',
    enabled         => TRUE,
    comments        => 'HITL: expire pending approvals past TTL');
END;
/

-- ===========================================================================
-- 6. Auth — dedicated login user + ORDS role/privilege gating /hitl/*
--    (This is "Block B" from the old 05_ords_auth.sql, now the default.)
-- ===========================================================================
DECLARE
  user_exists NUMBER;
BEGIN
  SELECT COUNT(*) INTO user_exists FROM dba_users WHERE username = 'HITL_TOOL';
  IF user_exists = 0 THEN
    EXECUTE IMMEDIATE
      'CREATE USER hitl_tool IDENTIFIED BY "CHANGE_ME_ToolUserPw#2026"';
    EXECUTE IMMEDIATE 'GRANT CREATE SESSION TO hitl_tool';
  END IF;
END;
/

BEGIN
  BEGIN
    ORDS_ADMIN.create_role(p_schema => 'HITL_SVC', p_role_name => 'HITL Client');
  EXCEPTION WHEN OTHERS THEN NULL;  -- exists from a prior run
  END;
  BEGIN
    ORDS_ADMIN.define_privilege(
      p_schema         => 'HITL_SVC',
      p_privilege_name => 'hitl.client',
      p_roles          => ORDS.varchar2_table('HITL Client'),
      p_patterns       => ORDS.varchar2_table('/hitl/*'),
      p_label          => 'HITL Client',
      p_description    => 'Access to HITL REST endpoints');
  EXCEPTION WHEN OTHERS THEN NULL;  -- exists from a prior run
  END;
  COMMIT;
END;
/

-- Give HITL_TOOL the badge. If this block errors with PLS-00201 on
-- ORDS_ADMIN.GRANT_ROLE (older ORDS), grant via Database Actions UI:
-- REST -> Security -> Roles -> HITL Client -> Grant Role to User -> HITL_TOOL
BEGIN
  ORDS_ADMIN.grant_role(p_role_name => 'HITL Client', p_user_name => 'HITL_TOOL');
  COMMIT;
EXCEPTION WHEN OTHERS THEN
  DBMS_OUTPUT.put_line('grant_role failed (' || SQLERRM || ') — grant via ' ||
                       'Database Actions UI: REST > Security > Roles.');
END;
/

-- ===========================================================================
-- DONE. Verify:
-- ===========================================================================
SELECT 'table: ' || table_name AS created FROM dba_tables
 WHERE owner = 'HITL_SVC' AND table_name LIKE 'HITL%'
UNION ALL
SELECT 'proc: RESOLVE_APPROVAL' FROM dba_objects
 WHERE owner = 'HITL_SVC' AND object_name = 'RESOLVE_APPROVAL'
   AND object_type = 'PROCEDURE' AND status = 'VALID'
UNION ALL
SELECT 'job: ' || job_name FROM dba_scheduler_jobs
 WHERE owner = 'HITL_SVC' AND job_name = 'HITL_EXPIRE_PENDING'
UNION ALL
SELECT 'user: ' || username FROM dba_users
 WHERE username IN ('HITL_SVC','HITL_TOOL');

-- Expected output: 3 tables + 1 proc + 1 job + 2 users = 7 rows.
--
-- Your ORDS base URL is:
--   https://<your-adb-host>/ords/hitl_svc/hitl/
-- Find <your-adb-host> in OCI Console -> your ADB -> Tool Configuration.
--
-- BENIGN RE-RUN ERRORS: none expected — every block guards itself. If you
-- see "ORA-01920: user name conflicts" you edited a password and re-ran;
-- users are only created once. Change passwords with:
--   ALTER USER hitl_tool IDENTIFIED BY "NewPw";
