-- HITL Approval — ORDS module + handlers.
-- Run as the schema that owns hitl_approvals, after
-- ORDS.enable_schema has been called once for that schema.
--
-- Endpoints:
--   POST {ords}/hitl/approvals              -- insert pending row
--   POST {ords}/hitl/approvals/{id}/resolve -- atomic decision + payload return
--
-- Both handlers respond with application/json. Enable BASIC auth in front
-- of them via ORDS role/privilege — the tool passes basic-auth credentials
-- from the Credential Store bundle.

BEGIN
  ORDS.enable_schema;

  -- Module ----------------------------------------------------------------
  ORDS.define_module(
    p_module_name    => 'hitl',
    p_base_path      => '/hitl/',
    p_items_per_page => 25,
    p_status         => 'PUBLISHED'
  );

  -- POST /approvals ---------------------------------------------------------
  ORDS.define_template(
    p_module_name => 'hitl',
    p_pattern     => 'approvals'
  );

  ORDS.define_handler(
    p_module_name    => 'hitl',
    p_pattern        => 'approvals',
    p_method         => 'POST',
    p_source_type    => ORDS.source_type_plsql,
    p_mimes_allowed  => 'application/json',
    p_source         => q'[
      BEGIN
        INSERT INTO hitl_approvals (
          approval_id, action_summary, action_payload,
          requester_ref, approver_allow, conversation_ref,
          expires_at
        ) VALUES (
          :approval_id, :action_summary, :action_payload,
          :requester_ref, :approver_allow, :conversation_ref,
          SYSTIMESTAMP + NUMTODSINTERVAL(NVL(:ttl_hours, 48), 'HOUR')
        );
        COMMIT;
        apex_json.open_object;
        apex_json.write('status', 'created');
        apex_json.write('approval_id', :approval_id);
        apex_json.close_object;
      EXCEPTION
        WHEN DUP_VAL_ON_INDEX THEN
          :status_code_out := 409;
          apex_json.open_object;
          apex_json.write('error', 'duplicate approval_id');
          apex_json.close_object;
      END;
    ]'
  );

  -- POST /approvals/{id}/resolve --------------------------------------------
  ORDS.define_template(
    p_module_name => 'hitl',
    p_pattern     => 'approvals/:id/resolve'
  );

  ORDS.define_handler(
    p_module_name    => 'hitl',
    p_pattern        => 'approvals/:id/resolve',
    p_method         => 'POST',
    p_source_type    => ORDS.source_type_plsql,
    p_mimes_allowed  => 'application/json',
    p_source         => q'[
      DECLARE
        l_result    VARCHAR2(20);
        l_payload   CLOB;
        l_requester VARCHAR2(200);
      BEGIN
        resolve_approval(
          p_id        => :id,
          p_decision  => :decision,
          p_sender    => :sender,
          o_result    => l_result,
          o_payload   => l_payload,
          o_requester => l_requester
        );
        apex_json.open_object;
        apex_json.write('result', l_result);
        IF l_payload IS NOT NULL THEN
          apex_json.write('payload', l_payload);
        END IF;
        IF l_requester IS NOT NULL THEN
          apex_json.write('requester_ref', l_requester);
        END IF;
        apex_json.close_object;
      END;
    ]'
  );

  COMMIT;
END;
/
