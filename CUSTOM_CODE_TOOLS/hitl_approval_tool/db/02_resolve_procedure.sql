-- HITL Approval — atomic authorize + compare-and-set procedure.
-- The UPDATE ... WHERE status='pending' clause is the authoritative gate:
-- SQL%ROWCOUNT tells us whether we won the race. This is why the resolve
-- endpoint is a handler that calls this proc, NOT AutoREST — AutoREST can't
-- do atomic status flips.

CREATE OR REPLACE PROCEDURE resolve_approval (
  p_id        IN  VARCHAR2,
  p_decision  IN  VARCHAR2,     -- 'approved' | 'rejected'
  p_sender    IN  VARCHAR2,     -- verified approver number from inbound relay
  o_result    OUT VARCHAR2,     -- 'unknown' | 'unauthorized' | 'already_decided' | 'ok'
  o_payload   OUT CLOB,
  o_requester OUT VARCHAR2
) AS
  l_status   VARCHAR2(12);
  l_allow    VARCHAR2(1000);
  l_authz    NUMBER;
BEGIN
  -- Row lookup + read authorization list. FOR UPDATE isn't necessary because
  -- the conditional UPDATE below is the actual gate.
  SELECT status, approver_allow, action_payload, requester_ref
    INTO l_status, l_allow, o_payload, o_requester
    FROM hitl_approvals
   WHERE approval_id = p_id;

  -- Authorization check: is p_sender in the approver_allow JSON array?
  -- JSON_EXISTS returns 1 when the SQL/JSON path expression matches.
  SELECT COUNT(*)
    INTO l_authz
    FROM DUAL
   WHERE JSON_EXISTS(l_allow, '$[*]?(@ == $s)' PASSING p_sender AS "s");

  IF l_authz = 0 THEN
    o_result := 'unauthorized';
    o_payload := NULL;
    o_requester := NULL;
    RETURN;
  END IF;

  -- Atomic compare-and-set. If someone (or a re-delivered SMS) already
  -- flipped the row, this UPDATE affects zero rows and we return
  -- 'already_decided'. No re-execution.
  UPDATE hitl_approvals
     SET status      = p_decision,
         decided_by  = p_sender,
         decided_at  = SYSTIMESTAMP
   WHERE approval_id = p_id
     AND status      = 'pending';

  IF SQL%ROWCOUNT = 1 THEN
    o_result := 'ok';
    COMMIT;
  ELSE
    o_result := 'already_decided';
    o_payload := NULL;
    o_requester := NULL;
  END IF;

EXCEPTION
  WHEN NO_DATA_FOUND THEN
    o_result := 'unknown';
    o_payload := NULL;
    o_requester := NULL;
END resolve_approval;
/
