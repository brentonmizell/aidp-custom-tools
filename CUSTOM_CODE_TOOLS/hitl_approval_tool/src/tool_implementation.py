"""HITL Toolkit — human-in-the-loop gate + incident tracking for AgentFlow.

Channel-agnostic. SMS/Twilio-REST is ONE adapter; every notification can
instead be returned to the calling agent for delivery over whatever channel
the flow already has — in a low-code MAS that is typically an SMS/comms
**MCP tool** attached to the Approvals Agent. Set conf.notify_mode:

    auto    (default) send via Twilio REST if creds are in the bundle, else defer
    twilio  require Twilio REST; fail if creds missing
    mcp     never send directly; return notifications for the agent to
            deliver via its attached MCP communication tool  (recommended
            for low-code canvases that already have a Twilio/SMS MCP tool)
    defer   same behavior as mcp; generic name for non-MCP delivery paths
            (chat replies, email tools, web front ends)

Six tools:

  LookupUserTool      Identify the person behind a verified channel ref
                      (phone/email/slack id). Returns role + their current
                      in-window incident + approvals awaiting their decision.
                      Call this FIRST on every turn.
  OpenIncidentTool    Create a durable incident (short human-typable ID).
  GetIncidentTool     Load an incident + its approvals (touches the session
                      window so lookups keep the session alive).
  UpdateIncidentTool  Update summary/detail/status as the agents learn more.
  OpenApprovalTool    Escalate an action to a human approver. Writes the
                      pending row, notifies approvers, returns immediately.
                      Nothing blocks; no compute is pinned.
  ResolveApprovalTool Apply the approver's decision. Authorization + the
                      atomic compare-and-set happen server-side in the DB;
                      exactly one resolution can ever win.

Design rules (non-negotiable):
- Identity authorizes, ID correlates. `sender_ref` / `user_ref` must be the
  VERIFIED channel identity from your relay (e.g. Twilio's From field via a
  [meta64:] envelope) — never text the user typed. In a CODE flow, bind it
  at tool-construction time in invoke() so the LLM cannot supply it.
- Atomic status flip in the database, not in this Python.
- Execute the captured payload, not a conversation re-run.
- TTL is a column; a scheduled job sweeps abandoned approvals.

Credentials: conf.credential_name -> AIDP Credential Store display name OR
OCI Vault secret OCID. Bundle keys:

    ords_base_url          (may live in conf instead)
    ords_username          e.g. HITL_TOOL   (from db/install.sql)
    ords_password
    twilio_account_sid     ┐
    twilio_auth_token      │ optional — omit for defer-mode deployments
    twilio_from_number     ┘
"""

from __future__ import annotations

import json
import secrets as _stdlib_secrets
from typing import Any, Dict, List, Optional, Tuple

import requests

from aidputils.agents.tools.custom_tools.base import CustomToolBase

from .utils.config_utils import get_cfg, ok, fail

try:
    from aidp_debug import debug, debug_warn, debug_error, DebugLog
except ImportError:
    def debug(*a, **k): pass
    def debug_warn(*a, **k): pass
    def debug_error(*a, **k): pass
    class DebugLog:
        @staticmethod
        def embed(r): return r


# ID alphabet excludes 0/O/1/I/L to stay unambiguous over SMS.
_ID_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _gen_id(n: int = 6) -> str:
    return "".join(_stdlib_secrets.choice(_ID_ALPHABET) for _ in range(n))


def _resolve_credentials(conf: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
    """Pull ORDS + Twilio creds from the Credential Store bundle. Returns
    (creds_dict, error). Twilio keys are optional (defer mode)."""
    cred_name = get_cfg(conf, "credential_name", "")
    if not cred_name:
        debug_warn("hitl: no conf.credential_name set — falling back to plaintext conf")
        return {
            "ords_base_url":       get_cfg(conf, "ords_base_url", ""),
            "ords_username":       get_cfg(conf, "ords_username", ""),
            "ords_password":       get_cfg(conf, "ords_password", ""),
            "twilio_account_sid":  get_cfg(conf, "twilio_account_sid", ""),
            "twilio_auth_token":   get_cfg(conf, "twilio_auth_token", ""),
            "twilio_from_number":  get_cfg(conf, "twilio_from_number", ""),
        }, None

    try:
        from .utils.credential_resolver import resolve_bundle
    except ImportError as ex:
        return {}, f"credential_resolver missing from this build: {ex}"

    bundle, err = resolve_bundle(cred_name)
    if err:
        return {}, err
    if not bundle:
        return {}, f"Credential `{cred_name}` returned no bundle."

    return {
        "ords_base_url":       (bundle.get("ords_base_url")
                                or get_cfg(conf, "ords_base_url", "")),
        "ords_username":       bundle.get("ords_username", ""),
        "ords_password":       bundle.get("ords_password", ""),
        "twilio_account_sid":  bundle.get("twilio_account_sid", ""),
        "twilio_auth_token":   bundle.get("twilio_auth_token", ""),
        "twilio_from_number":  bundle.get("twilio_from_number", ""),
    }, None


def _ords_post(ords_base: str, path: str, body: Dict[str, Any],
               username: str, password: str, timeout: int) -> Dict[str, Any]:
    """POST to an ORDS endpoint with basic auth. Raises for status."""
    url = ords_base.rstrip("/") + "/" + path.lstrip("/")
    r = requests.post(url, json=body,
                      auth=(username, password) if username else None,
                      timeout=timeout,
                      headers={"Accept": "application/json"})
    r.raise_for_status()
    if not r.content:
        return {}
    ct = r.headers.get("Content-Type", "")
    return r.json() if "json" in ct else {"raw": r.text}


def _ords_call(conf: Dict[str, Any], path: str,
               body: Dict[str, Any]) -> Tuple[Optional[Dict], Optional[str],
                                              Dict[str, Any]]:
    """Resolve creds + POST in one step. Returns (response, error, creds)."""
    creds, cred_err = _resolve_credentials(conf)
    if cred_err:
        return None, cred_err, {}
    ords_base = creds.get("ords_base_url", "")
    if not ords_base:
        return None, "ords_base_url not set in conf or credential bundle.", creds
    timeout = get_cfg(conf, "http_timeout", 20)
    try:
        resp = _ords_post(ords_base, path, body,
                          creds.get("ords_username", ""),
                          creds.get("ords_password", ""), timeout)
        return resp, None, creds
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        text = e.response.text[:500] if e.response is not None else ""
        return None, f"ORDS {path} failed HTTP {status}: {text}", creds
    except Exception as e:
        return None, f"ORDS {path} failed: {type(e).__name__}: {e}", creds


# ---------------------------------------------------------------------------
# Channel-agnostic notification seam
# ---------------------------------------------------------------------------

def _twilio_available(creds: Dict[str, Any]) -> bool:
    return bool(creds.get("twilio_account_sid")
                and creds.get("twilio_auth_token")
                and creds.get("twilio_from_number"))


def _send_sms(creds: Dict[str, Any], to_number: str, body: str,
              timeout: int) -> Dict[str, Any]:
    sid = creds["twilio_account_sid"]
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    r = requests.post(
        url,
        auth=(sid, creds["twilio_auth_token"]),
        data={"From": creds["twilio_from_number"], "To": to_number, "Body": body},
        timeout=timeout,
    )
    r.raise_for_status()
    j = r.json()
    return {"sid": j.get("sid"), "status": j.get("status", "queued")}


def _notify(creds: Dict[str, Any], conf: Dict[str, Any], to_ref: str,
            body: str, timeout: int) -> Dict[str, Any]:
    """Deliver (or defer) one notification. Never raises.

    Returns {"to", "body", "channel": "sms"|"deferred"|"failed", ...}.
    "deferred" means the CALLING AGENT is responsible for delivery — in a
    low-code MAS, send each entry via the SMS/comms MCP tool attached to
    the agent (notify_mode=mcp); otherwise a chat reply, Slack tool, email
    tool, whatever channel the flow owns. This is what makes the toolkit
    channel-agnostic.
    """
    mode = (get_cfg(conf, "notify_mode", "auto") or "auto").lower()
    entry: Dict[str, Any] = {"to": to_ref, "body": body}
    if not to_ref:
        entry["channel"] = "skipped"
        return entry
    if mode in ("defer", "mcp") or (mode == "auto" and not _twilio_available(creds)):
        entry["channel"] = "deferred"
        if mode == "mcp":
            entry["deliver_via"] = "mcp_tool"
        return entry
    if not _twilio_available(creds):   # mode == twilio but creds missing
        entry["channel"] = "failed"
        entry["error"] = ("notify_mode=twilio but Twilio credentials missing "
                          "from the bundle")
        return entry
    try:
        result = _send_sms(creds, to_ref, body, timeout)
        entry["channel"] = "sms"
        entry["sid"] = result.get("sid")
        return entry
    except Exception as e:
        debug_error(f"hitl: SMS to {_mask_ref(to_ref)} failed: {e}")
        entry["channel"] = "failed"
        entry["error"] = f"{type(e).__name__}: {e}"
        return entry


def _mask_ref(n: Optional[str]) -> str:
    if not n:
        return "<empty>"
    s = str(n)
    if len(s) <= 6:
        return "***"
    return s[:3] + "***" + s[-2:]


def _normalize_allowlist(raw: Any) -> List[str]:
    """approver_allow arrives as a list, JSON string, or comma-separated
    string. Normalize to list of stripped strings."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        if raw.startswith("["):
            try:
                v = json.loads(raw)
                if isinstance(v, list):
                    return [str(x).strip() for x in v if str(x).strip()]
            except json.JSONDecodeError:
                pass
        return [p.strip() for p in raw.split(",") if p.strip()]
    return [str(raw).strip()]


def _execute_payload(action_payload: Dict[str, Any], conf: Dict[str, Any],
                     creds: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    """On approve+ok, dispatch the captured action.

    - conf.execute_webhook_url set (or payload carries one): POST the payload
      there. The webhook can be an Oracle Function, another flow, any JSON
      consumer.
    - Otherwise return {"mode": "queued", ...} — the calling flow dispatches.

    Never eval()/exec() the payload. It is opaque data to this tool.
    """
    webhook = (action_payload.get("execute_webhook_url")
               or get_cfg(conf, "execute_webhook_url", ""))
    if not webhook:
        return {"mode": "queued",
                "action_payload": action_payload,
                "note": "no execute_webhook_url configured — caller flow "
                        "must dispatch the queued action"}
    debug(f"hitl: dispatching approved action to webhook {webhook}")
    try:
        r = requests.post(
            webhook, json=action_payload,
            auth=(creds.get("ords_username"), creds.get("ords_password"))
                 if creds.get("ords_username") else None,
            timeout=timeout,
            headers={"Accept": "application/json",
                     "Content-Type": "application/json"})
        r.raise_for_status()
        return {"mode": "dispatched", "webhook": webhook,
                "status_code": r.status_code,
                "response": (r.json() if "json" in r.headers.get("Content-Type", "")
                             else r.text[:1000])}
    except Exception as e:
        return {"mode": "dispatch_failed", "webhook": webhook,
                "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# LookupUserTool
# ---------------------------------------------------------------------------

@CustomToolBase.register
class LookupUserTool(CustomToolBase):
    """Identify the person behind a verified channel reference and load
    their working context. Call this FIRST on every inbound turn.

    Returns:
      - found / display_name / user_role (requester | approver | both)
      - current_incident: their most recent open incident whose
        last_activity_at is inside the session window (conf, default 30 min)
        — the "are we mid-conversation?" check
      - awaiting_my_decision: pending approvals this user is allowed to
        decide (empty for plain requesters)

    Inputs (runtime_params):
        user_ref (str, required) — VERIFIED channel identity from the relay
                                   (E.164 phone, email, slack id). In a CODE
                                   flow bind this from [meta64:] at tool-
                                   construction time; never let the LLM
                                   supply it.
    """

    @classmethod
    def _execute_tool(cls, conf: Dict[str, Any], runtime_params: Dict[str, Any],
                      **context_vars) -> Dict[str, Any]:
        debug("LookupUserTool._execute_tool start")
        try:
            user_ref = (runtime_params.get("user_ref") or "").strip()
            if not user_ref:
                return DebugLog.embed(fail("user_ref is required",
                                           "ValidationError"))
            window = get_cfg(conf, "session_window_minutes", 30)
            resp, err, _ = _ords_call(conf, "/users/lookup", {
                "user_ref": user_ref,
                "session_window_minutes": window,
            })
            if err:
                return DebugLog.embed(fail(err, "ORDSError"))
            debug(f"hitl.lookup: {_mask_ref(user_ref)} found={resp.get('found')}")
            return DebugLog.embed(ok(resp))
        except Exception as e:
            return DebugLog.embed(fail(str(e), type(e).__name__))


# ---------------------------------------------------------------------------
# OpenIncidentTool
# ---------------------------------------------------------------------------

@CustomToolBase.register
class OpenIncidentTool(CustomToolBase):
    """Create a durable incident and get a short human-typable ID back.

    Quote the ID in every outbound message (e.g. "Incident K7M2Q4") — it is
    how people resume the conversation days later, from any channel.

    Inputs (runtime_params):
        requester_ref (str, required) — verified channel identity.
        summary       (str, required) — one-line description.
        detail        (str, optional) — structured context the agents keep
                                        updated as they learn more.
    """

    @classmethod
    def _execute_tool(cls, conf: Dict[str, Any], runtime_params: Dict[str, Any],
                      **context_vars) -> Dict[str, Any]:
        debug("OpenIncidentTool._execute_tool start")
        try:
            requester_ref = (runtime_params.get("requester_ref") or "").strip()
            summary = (runtime_params.get("summary") or "").strip()
            detail = runtime_params.get("detail") or ""
            if not requester_ref or not summary:
                return DebugLog.embed(fail(
                    "requester_ref and summary are required", "ValidationError"))
            incident_id = _gen_id()
            resp, err, _ = _ords_call(conf, "/incidents", {
                "incident_id": incident_id,
                "requester_ref": requester_ref,
                "summary": summary,
                "detail": detail if isinstance(detail, str) else json.dumps(detail),
            })
            if err:
                return DebugLog.embed(fail(err, "ORDSError"))
            debug(f"hitl.open_incident: {incident_id} for {_mask_ref(requester_ref)}")
            return DebugLog.embed(ok({
                "incident_id": incident_id,
                "status": "open",
                "note": f"Quote 'Incident {incident_id}' in every message to "
                        f"the requester so they can resume later.",
            }))
        except Exception as e:
            return DebugLog.embed(fail(str(e), type(e).__name__))


# ---------------------------------------------------------------------------
# GetIncidentTool
# ---------------------------------------------------------------------------

@CustomToolBase.register
class GetIncidentTool(CustomToolBase):
    """Load an incident + all its approvals by ID. Use when someone quotes
    an incident ID ("what happened with K7M2Q4?") or LookupUserTool returned
    a current_incident. Reading an incident refreshes its session window.

    Inputs (runtime_params):
        incident_id (str, required)
    """

    @classmethod
    def _execute_tool(cls, conf: Dict[str, Any], runtime_params: Dict[str, Any],
                      **context_vars) -> Dict[str, Any]:
        debug("GetIncidentTool._execute_tool start")
        try:
            incident_id = (runtime_params.get("incident_id") or "").strip().upper()
            if not incident_id:
                return DebugLog.embed(fail("incident_id is required",
                                           "ValidationError"))
            resp, err, _ = _ords_call(conf, "/incidents/get",
                                      {"incident_id": incident_id})
            if err:
                return DebugLog.embed(fail(err, "ORDSError"))
            return DebugLog.embed(ok(resp))
        except Exception as e:
            return DebugLog.embed(fail(str(e), type(e).__name__))


# ---------------------------------------------------------------------------
# UpdateIncidentTool
# ---------------------------------------------------------------------------

@CustomToolBase.register
class UpdateIncidentTool(CustomToolBase):
    """Update an incident's summary/detail/status as the flow learns more.
    Also refreshes the session window.

    Inputs (runtime_params):
        incident_id (str, required)
        summary     (str, optional) — replaces the one-liner.
        detail      (str, optional) — replaces the structured context.
        status      (str, optional) — open | resolved | closed. (Use
                    OpenApprovalTool, not this, to move into
                    pending_approval — that transition is automatic.)
    """

    @classmethod
    def _execute_tool(cls, conf: Dict[str, Any], runtime_params: Dict[str, Any],
                      **context_vars) -> Dict[str, Any]:
        debug("UpdateIncidentTool._execute_tool start")
        try:
            incident_id = (runtime_params.get("incident_id") or "").strip().upper()
            if not incident_id:
                return DebugLog.embed(fail("incident_id is required",
                                           "ValidationError"))
            status = (runtime_params.get("status") or "").strip().lower()
            if status and status not in ("open", "resolved", "closed"):
                return DebugLog.embed(fail(
                    "status must be open | resolved | closed", "ValidationError"))
            detail = runtime_params.get("detail")
            body = {
                "incident_id": incident_id,
                "summary": (runtime_params.get("summary") or None),
                "detail": (detail if isinstance(detail, (str, type(None)))
                           else json.dumps(detail)),
                "status": status or None,
            }
            resp, err, _ = _ords_call(conf, "/incidents/update", body)
            if err:
                return DebugLog.embed(fail(err, "ORDSError"))
            return DebugLog.embed(ok(resp))
        except Exception as e:
            return DebugLog.embed(fail(str(e), type(e).__name__))


# ---------------------------------------------------------------------------
# OpenApprovalTool
# ---------------------------------------------------------------------------

@CustomToolBase.register
class OpenApprovalTool(CustomToolBase):
    """Escalate an action to a human approver — the out-of-band gate.

    Writes a pending row with the captured payload + allowlist, notifies
    every approver (SMS or deferred, per notify_mode), and returns
    immediately with an approval ID. Nothing blocks. The agent's turn ends
    by telling the requester it's pending. When an approver replies (in a
    LATER turn), call ResolveApprovalTool.

    If incident_id is provided, the incident is parked as pending_approval
    and automatically re-opened when the decision or TTL arrives.

    Inputs (runtime_params):
        action_summary  (str, required)  — what happens if approved. Shown
                                           verbatim to the approver.
        action_payload  (dict, required) — the concrete action to execute on
                                           approve. Opaque to this tool.
        requester_ref   (str, required)  — verified ref to notify the outcome.
        approver_allow  (list|str, required) — refs allowed to decide.
        incident_id     (str, optional)  — links the gate to an incident.
        conversation_ref(str, optional)  — audit only.
    """

    @classmethod
    def _execute_tool(cls, conf: Dict[str, Any], runtime_params: Dict[str, Any],
                      **context_vars) -> Dict[str, Any]:
        debug("OpenApprovalTool._execute_tool start")
        try:
            action_summary = (runtime_params.get("action_summary") or "").strip()
            action_payload = runtime_params.get("action_payload") or {}
            requester_ref = (runtime_params.get("requester_ref") or "").strip()
            approver_allow = _normalize_allowlist(runtime_params.get("approver_allow"))
            incident_id = (runtime_params.get("incident_id") or "").strip().upper()
            conversation_ref = (runtime_params.get("conversation_ref") or "").strip()

            for name, val in (("action_summary", action_summary),
                              ("requester_ref", requester_ref)):
                if not val:
                    return DebugLog.embed(fail(f"{name} is required",
                                               "ValidationError"))
            if not action_payload:
                return DebugLog.embed(fail("action_payload cannot be empty",
                                           "ValidationError"))
            if not approver_allow:
                return DebugLog.embed(fail(
                    "approver_allow must contain at least one ref",
                    "ValidationError"))
            if isinstance(action_payload, str):
                try:
                    action_payload = json.loads(action_payload)
                except json.JSONDecodeError:
                    return DebugLog.embed(fail(
                        "action_payload is a string but not valid JSON",
                        "ValidationError"))

            ttl_hours = get_cfg(conf, "ttl_hours", 48)
            timeout = get_cfg(conf, "http_timeout", 20)
            approval_id = _gen_id()

            resp, err, creds = _ords_call(conf, "/approvals", {
                "approval_id": approval_id,
                "incident_id": incident_id or None,
                "action_summary": action_summary,
                "action_payload": json.dumps(action_payload),
                "requester_ref": requester_ref,
                "approver_allow": json.dumps(approver_allow),
                "conversation_ref": conversation_ref,
                "ttl_hours": ttl_hours,
            })
            if err:
                return DebugLog.embed(fail(err, "ORDSError",
                                           approval_id=approval_id))

            body = (f"Approval {approval_id}"
                    + (f" (incident {incident_id})" if incident_id else "")
                    + f": {action_summary}\n"
                    f"Reply: approve {approval_id}   or   reject {approval_id}")
            notifications = [_notify(creds, conf, a, body, timeout)
                             for a in approver_allow]

            delivered = sum(1 for n in notifications if n["channel"] == "sms")
            deferred = sum(1 for n in notifications if n["channel"] == "deferred")
            failed = sum(1 for n in notifications if n["channel"] == "failed")
            if delivered == 0 and deferred == 0:
                return DebugLog.embed(fail(
                    "Approval row written but no approver could be notified",
                    "NotifyError", approval_id=approval_id,
                    notifications=notifications))

            return DebugLog.embed(ok({
                "approval_id": approval_id,
                "incident_id": incident_id or None,
                "status": "submitted",
                "ttl_hours": ttl_hours,
                "notified_sms": delivered,
                "deferred": deferred,
                "failed": failed,
                "notifications": notifications,
                "note": ("Deferred notifications MUST be delivered by the "
                         "calling flow — send each notifications[] entry via "
                         "your SMS/comms MCP tool (or channel of choice) now."
                         if deferred else ""),
            }))
        except Exception as e:
            return DebugLog.embed(fail(str(e), type(e).__name__))


# ---------------------------------------------------------------------------
# ResolveApprovalTool
# ---------------------------------------------------------------------------

@CustomToolBase.register
class ResolveApprovalTool(CustomToolBase):
    """Apply an approver's decision. Server-side atomic gate; exactly one
    resolution can ever win. Result codes:

      ok               decision applied. On approve, the captured payload is
                       executed (webhook or queued); the requester is
                       notified either way.
      unknown          no approval with that ID.
      unauthorized     sender_ref not on the allowlist.
      already_decided  someone (or a redelivered message) got there first.
      expired          the TTL sweep expired it before the decision arrived.

    Inputs (runtime_params):
        approval_id (str, required) — the ID the approver quoted.
        decision    (str, required) — approve | reject (prefix-parsed).
        sender_ref  (str, required) — the approver's VERIFIED channel ref
                                      from the relay. In a CODE flow, bind
                                      this at tool-construction time from
                                      the [meta64:] envelope; never accept
                                      an LLM-supplied value.
    """

    @classmethod
    def _execute_tool(cls, conf: Dict[str, Any], runtime_params: Dict[str, Any],
                      **context_vars) -> Dict[str, Any]:
        debug("ResolveApprovalTool._execute_tool start")
        try:
            approval_id = (runtime_params.get("approval_id") or "").strip().upper()
            raw_decision = (runtime_params.get("decision") or "").strip().lower()
            sender_ref = (runtime_params.get("sender_ref") or "").strip()

            for name, val in (("approval_id", approval_id),
                              ("decision", raw_decision),
                              ("sender_ref", sender_ref)):
                if not val:
                    return DebugLog.embed(fail(f"{name} is required",
                                               "ValidationError"))
            if raw_decision.startswith("a"):
                decision = "approved"
            elif raw_decision.startswith("r"):
                decision = "rejected"
            else:
                return DebugLog.embed(fail(
                    f"decision must be 'approve' or 'reject', got {raw_decision!r}",
                    "ValidationError"))

            timeout = get_cfg(conf, "http_timeout", 20)
            debug(f"hitl.resolve: {approval_id} decision={decision} "
                  f"sender={_mask_ref(sender_ref)}")
            resp, err, creds = _ords_call(
                conf, f"/approvals/{approval_id}/resolve",
                {"decision": decision, "sender": sender_ref})
            if err:
                return DebugLog.embed(fail(err, "ORDSError"))

            result = resp.get("result", "unknown")
            notifications: List[Dict[str, Any]] = []

            if result in ("unknown", "unauthorized", "already_decided", "expired"):
                msgs = {
                    "unknown":         f"No approval found for ID {approval_id}.",
                    "unauthorized":    f"You are not authorized to decide {approval_id}.",
                    "already_decided": f"Approval {approval_id} was already decided.",
                    "expired":         f"Approval {approval_id} expired before a "
                                       f"decision arrived.",
                }
                notifications.append(
                    _notify(creds, conf, sender_ref, msgs[result], timeout))
                return DebugLog.embed(ok({
                    "status": result,
                    "approval_id": approval_id,
                    "notifications": notifications,
                }))
            if result != "ok":
                return DebugLog.embed(fail(
                    f"unexpected ORDS result code: {result}", "ORDSError"))

            requester = resp.get("requester_ref") or ""
            incident_id = resp.get("incident_id") or None
            raw_payload = resp.get("payload") or "{}"
            try:
                action_payload = (json.loads(raw_payload)
                                  if isinstance(raw_payload, str) else raw_payload)
            except json.JSONDecodeError as e:
                return DebugLog.embed(fail(
                    f"ORDS returned malformed action_payload JSON: {e}",
                    "ORDSError"))

            if decision == "approved":
                exec_result = _execute_payload(action_payload, conf, creds, timeout)
                outcome_msg = _summarize_exec_result(approval_id, exec_result)
                notifications.append(
                    _notify(creds, conf, requester, outcome_msg, timeout))
                return DebugLog.embed(ok({
                    "status": "approved",
                    "approval_id": approval_id,
                    "incident_id": incident_id,
                    "execution": exec_result,
                    "notifications": notifications,
                }))

            notifications.append(_notify(
                creds, conf, requester,
                f"Your request {approval_id} was not approved.", timeout))
            return DebugLog.embed(ok({
                "status": "rejected",
                "approval_id": approval_id,
                "incident_id": incident_id,
                "notifications": notifications,
            }))
        except Exception as e:
            return DebugLog.embed(fail(str(e), type(e).__name__))


def _summarize_exec_result(approval_id: str, exec_result: Dict[str, Any]) -> str:
    mode = exec_result.get("mode")
    if mode == "dispatched":
        return f"Approved {approval_id}. Action dispatched."
    if mode == "queued":
        return f"Approved {approval_id}. Action queued for execution."
    if mode == "dispatch_failed":
        return (f"Approved {approval_id}, but execution failed: "
                f"{exec_result.get('error', 'unknown')}. Manual follow-up needed.")
    return f"Approved {approval_id}."
