-- HITL Approval — schema
-- Run as the schema that owns the ORDS module (usually the same schema
-- your ORDS AutoREST calls run under).

CREATE TABLE hitl_approvals (
  approval_id       VARCHAR2(16) PRIMARY KEY,
  status            VARCHAR2(12) DEFAULT 'pending' NOT NULL
                       CONSTRAINT hitl_approvals_status_ck
                       CHECK (status IN ('pending','approved','rejected','expired')),
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
);

-- Sweep hits this: WHERE status='pending' AND expires_at < SYSTIMESTAMP
CREATE INDEX hitl_approvals_status_exp
  ON hitl_approvals (status, expires_at);
