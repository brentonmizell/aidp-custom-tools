"""Cross-scenario verification harness for hitl_approval_tool.

Mocks aidputils, oci, and every outbound HTTP call (ORDS + Twilio) so this
runs on any dev box without an AIDP environment, a database, or a phone.

Scenarios:
  open_happy         — ORDS insert 200 + Twilio 200 → returns submitted
  open_sms_fail      — ORDS insert 200 + Twilio 500 → error surfaces
                         approval_id so the row can be swept later
  resolve_unknown    — ORDS returns result=unknown → tool SMSes approver
  resolve_unauth     — ORDS returns result=unauthorized → SMS + status
  resolve_dup        — ORDS returns result=already_decided → SMS + status
  resolve_ok_approve_webhook — ok + approve + webhook set → POST fires,
                         status=approved, execution.mode=dispatched
  resolve_ok_approve_queued — ok + approve + no webhook → queued_action
                         returned; requester notified
  resolve_ok_reject  — ok + reject → requester notified, no webhook

Run: python verify_wiring.py
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest import mock


TOOL_DIR = Path(__file__).resolve().parent


def _install_stubs():
    # aidputils.agents.tools.custom_tools.base.CustomToolBase
    for path in ("aidputils", "aidputils.agents", "aidputils.agents.tools",
                 "aidputils.agents.tools.custom_tools"):
        sys.modules[path] = types.ModuleType(path)
    base_mod = types.ModuleType("aidputils.agents.tools.custom_tools.base")

    class _Base:
        @classmethod
        def register(cls, k): return k
    base_mod.CustomToolBase = _Base
    sys.modules["aidputils.agents.tools.custom_tools.base"] = base_mod

    # aidputils.secrets — installed but returns configurable bundle.
    secrets_mod = types.ModuleType("aidputils.secrets")
    secrets_mod._bundles = {}

    def _get(name, key=None):
        b = secrets_mod._bundles.get(name)
        if b is None:
            raise KeyError(f"no credential {name!r}")
        return b if key is None else b.get(key)
    secrets_mod.get = _get
    sys.modules["aidputils.secrets"] = secrets_mod
    return secrets_mod


def _load_tool_module():
    sys.path.insert(0, str(TOOL_DIR))
    for m in list(sys.modules):
        if m == "src" or m.startswith("src."):
            del sys.modules[m]
    import importlib
    importlib.invalidate_caches()
    importlib.import_module("src")
    return importlib.import_module("src.tool_implementation")


CREDS = {
    "ords_base_url":       "https://ords.example.com/ords/aidp/hitl",
    "ords_username":       "hitl_svc",
    "ords_password":       "supersecret",
    "twilio_account_sid":  "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "twilio_auth_token":   "tokentokentokentokentokentokenxx",
    "twilio_from_number":  "+15551234567",
}


def _mock_response(status_code=200, json_body=None, headers=None, text=""):
    resp = mock.MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {"Content-Type": "application/json"}
    resp.content = b"body"
    resp.text = text or json.dumps(json_body or {})
    resp.json = mock.MagicMock(return_value=json_body or {})
    if status_code >= 400:
        import requests
        resp.raise_for_status = mock.MagicMock(
            side_effect=requests.HTTPError(f"HTTP {status_code}",
                                            response=resp))
    else:
        resp.raise_for_status = mock.MagicMock(return_value=None)
    return resp


def check(label, cond, detail=""):
    m = "PASS" if cond else "FAIL"
    line = f"  {m}  {label}"
    if detail:
        line += f"  —  {detail}"
    print(line)
    return cond


def scenario_open_happy(mod, secrets_mod):
    print("\n[open_happy]")
    secrets_mod._bundles = {"hitl_creds": CREDS}
    calls = []
    def fake_post(url, **kw):
        calls.append({"url": url, **kw})
        if "twilio.com" in url:
            return _mock_response(json_body={"sid": "SMxxx", "status": "queued"})
        return _mock_response(json_body={"status": "created",
                                          "approval_id": "STUB"})
    with mock.patch("requests.post", side_effect=fake_post):
        result = mod.OpenApprovalTool._execute_tool(
            conf={"conf": {"credential_name": "hitl_creds"}},
            runtime_params={
                "action_summary": "Send $3,600 to Acme.",
                "action_payload": {"kind": "wire", "amount": 3600},
                "requester_ref": "+15550001111",
                "approver_allow": ["+15559998888"],
            },
        )
    ok1 = check("ok=True", result.get("ok") is True, f"result={result}")
    d = result.get("data", {})
    ok2 = check("returns approval_id + status=submitted",
                d.get("status") == "submitted" and len(d.get("approval_id", "")) == 6)
    ok3 = check("2 outbound POSTs (ORDS then Twilio)", len(calls) == 2,
                f"got {len(calls)}: {[c['url'][:40] for c in calls]}")
    ok4 = check("ORDS body carries json-encoded action_payload",
                calls[0].get("json", {}).get("action_payload", "").startswith("{"))
    ok5 = check("Twilio auth used SID/token",
                calls[1].get("auth") == (CREDS["twilio_account_sid"],
                                          CREDS["twilio_auth_token"]))
    return all((ok1, ok2, ok3, ok4, ok5))


def scenario_open_sms_fail(mod, secrets_mod):
    print("\n[open_sms_fail]")
    secrets_mod._bundles = {"hitl_creds": CREDS}
    def fake_post(url, **kw):
        if "twilio.com" in url:
            return _mock_response(status_code=500, text="upstream fail")
        return _mock_response(json_body={"status": "created"})
    with mock.patch("requests.post", side_effect=fake_post):
        result = mod.OpenApprovalTool._execute_tool(
            conf={"conf": {"credential_name": "hitl_creds"}},
            runtime_params={
                "action_summary": "x", "action_payload": {"k": 1},
                "requester_ref": "+15550001111",
                "approver_allow": ["+15559998888"],
            },
        )
    ok1 = check("ok=False", result.get("ok") is False)
    ok2 = check("error_type=SMSError", result.get("error_type") == "SMSError")
    ok3 = check("approval_id surfaced so row can be swept",
                len(result.get("approval_id", "")) == 6,
                f"got approval_id={result.get('approval_id')!r}")
    return ok1 and ok2 and ok3


def _resolve(mod, secrets_mod, ords_response, decision="approve",
             extra_conf=None):
    secrets_mod._bundles = {"hitl_creds": CREDS}
    calls = []
    def fake_post(url, **kw):
        calls.append({"url": url, **kw})
        if "twilio.com" in url:
            return _mock_response(json_body={"sid": "SMx", "status": "queued"})
        if "webhook.example.com" in url:
            return _mock_response(json_body={"result": "dispatched"})
        return _mock_response(json_body=ords_response)
    conf = {"conf": {"credential_name": "hitl_creds"}}
    if extra_conf:
        conf["conf"].update(extra_conf)
    with mock.patch("requests.post", side_effect=fake_post):
        result = mod.ResolveApprovalTool._execute_tool(
            conf=conf,
            runtime_params={
                "approval_id": "K7M2Q4", "decision": decision,
                "sender_ref": "+15559998888",
            },
        )
    return result, calls


def scenario_resolve_unknown(mod, secrets_mod):
    print("\n[resolve_unknown]")
    result, calls = _resolve(mod, secrets_mod, {"result": "unknown"})
    return (check("ok=True", result.get("ok") is True)
            and check("status=unknown_id",
                      result.get("data", {}).get("status") == "unknown_id")
            and check("approver SMSed", any("twilio.com" in c["url"] for c in calls)))


def scenario_resolve_unauth(mod, secrets_mod):
    print("\n[resolve_unauth]")
    result, calls = _resolve(mod, secrets_mod, {"result": "unauthorized"})
    return (check("ok=True", result.get("ok") is True)
            and check("status=unauthorized",
                      result.get("data", {}).get("status") == "unauthorized")
            and check("sender number masked in envelope",
                      "***" in str(result.get("data", {}).get("sender", ""))))


def scenario_resolve_dup(mod, secrets_mod):
    print("\n[resolve_dup]")
    result, calls = _resolve(mod, secrets_mod, {"result": "already_decided"})
    return (check("ok=True", result.get("ok") is True)
            and check("status=already_decided",
                      result.get("data", {}).get("status") == "already_decided"))


def scenario_resolve_ok_approve_webhook(mod, secrets_mod):
    print("\n[resolve_ok_approve_webhook]")
    result, calls = _resolve(
        mod, secrets_mod,
        {"result": "ok",
         "payload": json.dumps({"kind": "wire", "amount": 3600}),
         "requester_ref": "+15550001111"},
        decision="approve",
        extra_conf={"execute_webhook_url": "https://webhook.example.com/execute"})
    d = result.get("data", {})
    return (check("status=approved", d.get("status") == "approved")
            and check("execution.mode=dispatched",
                      d.get("execution", {}).get("mode") == "dispatched")
            and check("webhook was POSTed",
                      any("webhook.example.com" in c["url"] for c in calls))
            and check("requester_notified=True", d.get("requester_notified") is True))


def scenario_resolve_ok_approve_queued(mod, secrets_mod):
    print("\n[resolve_ok_approve_queued]")
    result, _ = _resolve(
        mod, secrets_mod,
        {"result": "ok",
         "payload": json.dumps({"kind": "wire", "amount": 3600}),
         "requester_ref": "+15550001111"},
        decision="approve")  # no webhook
    d = result.get("data", {})
    return (check("status=approved", d.get("status") == "approved")
            and check("execution.mode=queued",
                      d.get("execution", {}).get("mode") == "queued")
            and check("action_payload preserved for caller flow",
                      d.get("execution", {}).get("action_payload", {}).get("amount") == 3600))


def scenario_resolve_ok_reject(mod, secrets_mod):
    print("\n[resolve_ok_reject]")
    result, calls = _resolve(
        mod, secrets_mod,
        {"result": "ok",
         "payload": json.dumps({"kind": "wire", "amount": 3600}),
         "requester_ref": "+15550001111"},
        decision="reject")
    d = result.get("data", {})
    return (check("status=rejected", d.get("status") == "rejected")
            and check("requester_notified=True", d.get("requester_notified") is True)
            and check("no webhook POST", not any(
                "webhook" in c["url"] for c in calls)))


def main() -> int:
    secrets_mod = _install_stubs()
    mod = _load_tool_module()
    scenarios = [
        scenario_open_happy,
        scenario_open_sms_fail,
        scenario_resolve_unknown,
        scenario_resolve_unauth,
        scenario_resolve_dup,
        scenario_resolve_ok_approve_webhook,
        scenario_resolve_ok_approve_queued,
        scenario_resolve_ok_reject,
    ]
    failures = 0
    for sc in scenarios:
        try:
            if not sc(mod, secrets_mod):
                failures += 1
        except Exception:
            import traceback
            print(f"\n[{sc.__name__}] CRASHED")
            traceback.print_exc()
            failures += 1
    print()
    if failures:
        print(f"FAIL — {failures}/{len(scenarios)} scenarios failed")
        return 1
    print(f"PASS — all {len(scenarios)} scenarios verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
