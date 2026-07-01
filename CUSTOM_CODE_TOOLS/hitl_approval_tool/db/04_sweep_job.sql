-- HITL Approval — expiration sweep.
-- DBMS_SCHEDULER job that runs every 15 min, marks any pending row past
-- its expires_at as 'expired', and (optionally) fires a callback so the
-- tool can text the requester that their request timed out.
--
-- The tool does NOT do this sweep — abandoned approvals must expire on
-- their own without any live compute holding.

BEGIN
  DBMS_SCHEDULER.create_job(
    job_name        => 'hitl_expire_pending',
    job_type        => 'PLSQL_BLOCK',
    job_action      => q'[
      BEGIN
        UPDATE hitl_approvals
           SET status = 'expired'
         WHERE status = 'pending'
           AND expires_at < SYSTIMESTAMP;
        -- Optional: enqueue a notification row so a downstream job can text
        -- the requester. Keep the write cheap; the actual SMS send belongs
        -- outside the sweep.
        COMMIT;
      END;
    ]',
    start_date      => SYSTIMESTAMP,
    repeat_interval => 'FREQ=MINUTELY; INTERVAL=15',
    enabled         => TRUE,
    comments        => 'Sweeps hitl_approvals for TTL expiry (v1: no requester notify)'
  );
END;
/

-- To notify the requester on expiry, either:
--   (a) add an INSERT INTO hitl_expiry_queue in the block above and run a
--       separate scheduled procedure that consumes the queue and calls
--       Twilio server-side, OR
--   (b) leave notification to the caller flow — the row stays queryable
--       forever and the requester's next turn discovers the expired status.
