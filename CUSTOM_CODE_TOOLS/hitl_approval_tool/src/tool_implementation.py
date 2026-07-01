"""HITL Approval Tool — out-of-band approval gate for AgentFlow.

Two tools:

- OpenApprovalTool
    Called by a policy/decision agent when an action crosses a threshold.
    Writes a pending row via ORDS, texts the approver, returns immediately.
    Nothing blocks; no compute is pinned.

- ResolveApprovalTool
    Called by the HITL agent when the approver's decision turn arrives.
    ORDS runs an atomic authorize + compare-and-set procedure that returns
    one of four result codes (unknown / unauthorized / already_decided / ok).
    On `ok + approve`, the captured action_payload is executed (via
    execute_webhook_url if set, else returned as `queued_action`).

Design rules (spec, non-negotiable):
- Identity authorizes, ID correlates.
- Atomic status flip in the database, not in this Python.
- Execute the captured payload, not a conversation re-run.
- TTL is a column; a scheduled job sweeps abandoned approvals.

Credentials: this tool uses the shared credential_resolver. Set
conf.credential_name to either an AIDP Credential Store display name OR an
OCI Vault secret OCID; the bundle must contain the keys:

    ords_base_url          (may live in conf instead — see below)
    ords_username
    ords_password
    twilio_account_sid
    twilio_auth_token
    twilio_from_number

If ords_base_url isn't in the bundle it falls back to conf.ords_base_url.
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

def _gen_approval_id(n: int = 6) -> str:
    return "".join(_stdlib_secrets.choice(_ID_ALPHABET) for _ in range(n))


def _resolve_credentials(conf: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
    """Pull ORDS + Twilio creds from the Credential Store bundle. Returns
    (creds_dict, error). Missing keys in the bundle are logged but not
    fatal — callers assert what they need."""
    cred_name = get_cfg(conf, "credential_name", "")
    if not cred_name:
        # Legacy fallback: read directly from conf. Warns via debug channel;
        # tool still runs but this is a deployment antipattern.
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


def _send_sms(creds: Dict[str, Any], to_number: str, body: str,
              timeout: int) -> Dict[str, Any]:
    """Send a Twilio SMS. Returns {sid, status} on success or raises."""
    if not creds.get("twilio_account_sid") or not creds.get("twilio_auth_token"):
        raise ValueError("Twilio credentials missing (account_sid / auth_token). "
                         "Set them in the Credential Store bundle.")
    if not creds.get("twilio_from_number"):
        raise ValueError("twilio_from_number missing in Credential Store bundle.")
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
    return {"sid": j.get("sid"), "status": j.get("status", "queued"),
            "to": to_number}


def _mask_number(n: Optional[str]) -> str:
    if not n:
        return "<empty>"
    s = str(n)
    if len(s) <= 6:
        return "***"
    return s[:3] + "***" + s[-2:]


def _normalize_allowlist(raw: Any) -> List[str]:
    """approver_allow can arrive as a Python list, a JSON string, or a
    comma-separated string. Normalize to list of stripped strings."""
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

    Two modes:
      - If conf.execute_webhook_url is set (or the payload carries one), POST
        the payload to it with basic auth (ORDS creds by default). The webhook
        can be another AIDP tool exposed as REST, an Oracle Function, or any
        endpoint that consumes JSON.
      - Otherwise return {"mode": "queued", ...} — the calling flow reads the
        payload from the tool result and dispatches on its own.

    Never eval() or exec() the payload. It is opaque data to this tool.
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
            webhook,
            json=action_payload,
            auth=(creds.get("ords_username"), creds.get("ords_password"))
                 if creds.get("ords_username") else None,
            timeout=timeout,
            headers={"Accept": "application/json",
                     "Content-Type": "application/json"},
        )
        r.raise_for_status()
        return {"mode": "dispatched",
                "webhook": webhook,
                "status_code": r.status_code,
                "response": (r.json() if "json" in r.headers.get("Content-Type", "")
                             else r.text[:1000])}
    except Exception as e:
        return {"mode": "dispatch_failed",
                "webhook": webhook,
                "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# OpenApprovalTool
# ---------------------------------------------------------------------------

@CustomToolBase.register
class OpenApprovalTool(CustomToolBase):
    """Escalate an action to a human approver over SMS.

    Writes a pending row to ORDS with the captured payload + allowlist,
    texts the first approver, and returns immediately with an approval ID.
    Nothing blocks; the agent's turn ends with a "pending" reply to the
    requester. When the approver replies, a later turn calls
    ResolveApprovalTool with the ID.

    Inputs (runtime_params):
        action_summary  (str, required) — plain-language description shown
                                          to the approver. Include the money
                                          amount + destination + reason.
        action_payload  (dict, required) — the concrete action to execute on
                                          approve. Opaque to this tool.
        requester_ref   (str, required) — sessionKey / phone number to text
                                          the outcome back to.
        approver_allow  (list|str, required) — allowed approver phone numbers.
                                          List or comma-separated string.
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
            conversation_ref = (runtime_params.get("conversation_ref") or "").strip()

            for name, val in (("action_summary", action_summary),
                              ("requester_ref", requester_ref)):
                if not val:
                    return DebugLog.embed(fail(f"{name} is required", "ValidationError"))
            if not action_payload:
                return DebugLog.embed(fail("action_payload cannot be empty",
                                            "ValidationError"))
            if not approver_allow:
                return DebugLog.embed(fail(
                    "approver_allow must contain at least one phone number",
                    "ValidationError"))

            if isinstance(action_payload, str):
                try:
                    action_payload = json.loads(action_payload)
                except json.JSONDecodeError:
                    return DebugLog.embed(fail(
                        "action_payload is a string but not valid JSON",
                        "ValidationError"))

            creds, cred_err = _resolve_credentials(conf)
            if cred_err:
                return DebugLog.embed(fail(cred_err, "CredentialStoreError"))
            ords_base = creds.get("ords_base_url", "")
            if not ords_base:
                return DebugLog.embed(fail(
                    "ords_base_url not set in conf or credential bundle.",
                    "ConfigError"))

            timeout = get_cfg(conf, "http_timeout", 20)
            ttl_hours = get_cfg(conf, "ttl_hours", 48)
            approval_id = _gen_approval_id()

            ords_body = {
                "approval_id": approval_id,
                "action_summary": action_summary,
                "action_payload": json.dumps(action_payload),
                "requester_ref": requester_ref,
                "approver_allow": json.dumps(approver_allow),
                "conversation_ref": conversation_ref,
                "ttl_hours": ttl_hours,
            }
            debug(f"hitl.open: POST /approvals id={approval_id} "
                  f"approver_count={len(approver_allow)}")
            try:
                _ords_post(ords_base, "/approvals", ords_body,
                           creds.get("ords_username", ""),
                           creds.get("ords_password", ""),
                           timeout)
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else "?"
                body = e.response.text[:500] if e.response is not None else ""
                return DebugLog.embed(fail(
                    f"ORDS insert failed HTTP {status}: {body}", "ORDSError",
                    approval_id=approval_id))

            sms_body = (f"Approval {approval_id}: {action_summary}\n"
                        f"Reply: approve {approval_id}   or   reject {approval_id}")
            sms_results = []
            for approver in approver_allow:
                try:
                    sms_results.append(_send_sms(creds, approver, sms_body, timeout))
                except Exception as e:
                    sms_results.append({"to": _mask_number(approver),
                                         "error": f"{type(e).__name__}: {e}"})
                    debug_error(f"hitl.open: SMS to {_mask_number(approver)} "
                                f"failed: {e}")

            sms_succeeded = any(r.get("sid") for r in sms_results)
            if not sms_succeeded:
                return DebugLog.embed(fail(
                    "ORDS row written but SMS delivery failed to every approver",
                    "SMSError", approval_id=approval_id, sms_results=sms_results))

            return DebugLog.embed(ok({
                "approval_id": approval_id,
                "status": "submitted",
                "approvers_notified": sum(1 for r in sms_results if r.get("sid")),
                "ttl_hours": ttl_hours,
                "sms_results": sms_results,
            }))
        except Exception as e:
            return DebugLog.embed(fail(str(e), type(e).__name__))


# ---------------------------------------------------------------------------
# ResolveApprovalTool
# ---------------------------------------------------------------------------

@CustomToolBase.register
class ResolveApprovalTool(CustomToolBase):
    """Resolve a pending approval when the approver's decision turn arrives.

    All authorization and the compare-and-set happen server-side in ORDS via
    the resolve_approval PL/SQL procedure. This tool only translates the
    result code into a user-facing SMS and (on ok+approve) executes the
    captured payload.

    Inputs (runtime_params):
        approval_id  (str, required) — the ID the approver typed.
        decision     (str, required) — "approve" or "reject". Free-form
                                       parsing: any string starting with
                                       'a' becomes approved, 'r' rejected.
        sender_ref   (str, required) — the approver's VERIFIED phone number
                                       from the inbound relay. This is the
                                       identity check — do not accept
                                       user-typed sender values.
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

            creds, cred_err = _resolve_credentials(conf)
            if cred_err:
                return DebugLog.embed(fail(cred_err, "CredentialStoreError"))
            ords_base = creds.get("ords_base_url", "")
            if not ords_base:
                return DebugLog.embed(fail(
                    "ords_base_url not set in conf or credential bundle.",
                    "ConfigError"))

            timeout = get_cfg(conf, "http_timeout", 20)

            debug(f"hitl.resolve: POST /approvals/{approval_id}/resolve "
                  f"decision={decision} sender={_mask_number(sender_ref)}")
            try:
                resp = _ords_post(
                    ords_base, f"/approvals/{approval_id}/resolve",
                    {"decision": decision, "sender": sender_ref},
                    creds.get("ords_username", ""),
                    creds.get("ords_password", ""),
                    timeout,
                )
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else "?"
                body = e.response.text[:500] if e.response is not None else ""
                return DebugLog.embed(fail(
                    f"ORDS resolve failed HTTP {status}: {body}", "ORDSError"))

            result = resp.get("result", "unknown")

            if result == "unknown":
                _try_sms(creds, sender_ref,
                         f"No approval found for ID {approval_id}.", timeout)
                return DebugLog.embed(ok({"status": "unknown_id",
                                          "approval_id": approval_id}))
            if result == "unauthorized":
                _try_sms(creds, sender_ref,
                         f"You are not authorized to decide {approval_id}.",
                         timeout)
                return DebugLog.embed(ok({"status": "unauthorized",
                                          "approval_id": approval_id,
                                          "sender": _mask_number(sender_ref)}))
            if result == "already_decided":
                _try_sms(creds, sender_ref,
                         f"Approval {approval_id} was already decided.", timeout)
                return DebugLog.embed(ok({"status": "already_decided",
                                          "approval_id": approval_id}))
            if result != "ok":
                return DebugLog.embed(fail(
                    f"unexpected ORDS result code: {result}", "ORDSError"))

            # ok — execute (approve) or notify (reject).
            requester = resp.get("requester_ref") or ""
            raw_payload = resp.get("payload") or "{}"
            try:
                action_payload = json.loads(raw_payload) \
                                 if isinstance(raw_payload, str) else raw_payload
            except json.JSONDecodeError as e:
                return DebugLog.embed(fail(
                    f"ORDS returned malformed action_payload JSON: {e}",
                    "ORDSError"))

            if decision == "approved":
                exec_result = _execute_payload(action_payload, conf, creds, timeout)
                if requester:
                    outcome_msg = _summarize_exec_result(approval_id, exec_result)
                    _try_sms(creds, requester, outcome_msg, timeout)
                return DebugLog.embed(ok({
                    "status": "approved",
                    "approval_id": approval_id,
                    "requester_notified": bool(requester),
                    "execution": exec_result,
                }))

            # decision == rejected
            if requester:
                _try_sms(creds, requester,
                         f"Your request {approval_id} was not approved.", timeout)
            return DebugLog.embed(ok({
                "status": "rejected",
                "approval_id": approval_id,
                "requester_notified": bool(requester),
            }))
        except Exception as e:
            return DebugLog.embed(fail(str(e), type(e).__name__))


def _try_sms(creds: Dict[str, Any], to: str, body: str, timeout: int) -> None:
    """Fire-and-log-only SMS. Failure here should not fail the tool call —
    the DB is already authoritative."""
    if not to:
        return
    try:
        _send_sms(creds, to, body, timeout)
    except Exception as e:
        debug_error(f"hitl: SMS to {_mask_number(to)} failed: {e}")


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
