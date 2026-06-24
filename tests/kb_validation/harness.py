#!/usr/bin/env python3
"""
KB API Error Message Validation Harness.

Loads tests/kb_validation/catalog/*.json, fires each `validation`/`noop`
scenario against AIDP, and asserts the response matches the documented
catalog. Skips infra/db_inject/race rows (not client-reproducible).

Reuses aidp_io's signer + REST plumbing — same auth modes the rest of
the repo uses (resource_principal / session_token / user_principal / auto).
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CATALOG_DIR = Path(__file__).resolve().parent / "catalog"

# ---------------------------------------------------------------------------
# ANSI colors
# ---------------------------------------------------------------------------
_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
def _c(code, s): return f"\x1b[{code}m{s}\x1b[0m" if _USE_COLOR else s
def green(s):  return _c("32", s)
def red(s):    return _c("31", s)
def yellow(s): return _c("33", s)
def cyan(s):   return _c("36", s)
def dim(s):    return _c("2", s)
def bold(s):   return _c("1", s)

# ---------------------------------------------------------------------------
# Conf loader
# ---------------------------------------------------------------------------

def _aidp_config_path() -> Path:
    return Path.home() / ".aidp" / "aidp-deploy.config.json"


def _load_aidp_config() -> Dict[str, Any]:
    p = _aidp_config_path()
    if not p.is_file():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _build_conf(profile: str) -> Dict[str, Any]:
    """Build the conf dict aidp_io._build_signer + _client expect."""
    aidp = _load_aidp_config()
    cfg_path = Path.home() / ".oci" / "config"
    section: Dict[str, Any] = {}
    if cfg_path.is_file():
        cp = configparser.ConfigParser()
        cp.read(cfg_path)
        if profile in cp:
            section = dict(cp[profile])

    # Detect session profile.
    if section.get("security_token_file"):
        auth_mode = "session_token"
    else:
        auth_mode = "user_principal" if section.get("user") else "resource_principal"

    conf: Dict[str, Any] = {
        "region": aidp.get("region", "us-ashburn-1"),
        "data_lake_ocid": aidp.get("dataLakeOcid", ""),
        "api_version": aidp.get("apiVersion", "20260430"),
        "service_path": "aiDataPlatforms",
        "timeout": 30,
        "auth_mode": auth_mode,
        "oci_config_profile": profile,
    }
    if auth_mode == "user_principal":
        # Read PEM inline so aidp_io doesn't need to.
        key_path = Path(section.get("key_file", "")).expanduser() if section.get("key_file") else None
        if key_path and key_path.is_file():
            conf.update({
                "tenancy_ocid": section.get("tenancy", ""),
                "user_ocid": section.get("user", ""),
                "fingerprint": section.get("fingerprint", ""),
                "private_key_content": key_path.read_text(encoding="utf-8"),
                "pass_phrase": section.get("pass_phrase") or "",
            })
    return conf


# ---------------------------------------------------------------------------
# Signer + base URL via aidp_io
# ---------------------------------------------------------------------------

def _import_aidp_io():
    """Import aidp_io from the repo root."""
    sys.path.insert(0, str(REPO_ROOT / "aidp_io"))
    try:
        import aidp_io  # type: ignore
        return aidp_io
    finally:
        # Keep on path so re-imports work.
        pass


# ---------------------------------------------------------------------------
# Catalog loader + classification stats
# ---------------------------------------------------------------------------

CLASSES = ("validation", "fixture", "infra", "db_inject", "race", "noop")


def _load_catalog() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not CATALOG_DIR.is_dir():
        return rows
    for f in sorted(CATALOG_DIR.glob("*.json")):
        if f.name.startswith("_"):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(red(f"[err] {f.name}: invalid JSON — {e}"))
            continue
        if isinstance(data, list):
            rows.extend(data)
        elif isinstance(data, dict) and "scenarios" in data:
            rows.extend(data["scenarios"])
    return rows


def _coverage_report(rows: List[Dict[str, Any]]) -> None:
    by_method: Dict[str, Dict[str, int]] = {}
    for r in rows:
        method = r.get("method", "?")
        cls = r.get("class", "?")
        by_method.setdefault(method, {c: 0 for c in CLASSES})
        if cls in by_method[method]:
            by_method[method][cls] += 1

    width = max((len(m) for m in by_method), default=20)
    header = f"{'KB METHOD':{width}} " + " ".join(f"{c:>10}" for c in CLASSES) + "  TOTAL"
    print(bold(header))
    totals = {c: 0 for c in CLASSES}
    for method in sorted(by_method):
        row = by_method[method]
        line = f"{method:{width}} " + " ".join(f"{row[c]:>10}" for c in CLASSES)
        total = sum(row.values())
        line += f"  {total:>5}"
        print(line)
        for c in CLASSES:
            totals[c] += row[c]
    print(dim("-" * len(header)))
    print(f"{'TOTALS':{width}} " + " ".join(f"{totals[c]:>10}" for c in CLASSES) +
          f"  {sum(totals.values()):>5}")
    testable = totals["validation"] + totals["fixture"] + totals["noop"]
    skipped = totals["infra"] + totals["db_inject"] + totals["race"]
    grand = testable + skipped
    if grand:
        pct_t = 100 * testable / grand
        pct_s = 100 * skipped / grand
        print()
        print(f"{green('TESTABLE')}  (validation + fixture + noop): {testable:>4} / {grand}  ({pct_t:.0f}%)")
        print(f"{yellow('SKIPPED')}   (infra + db_inject + race):    {skipped:>4} / {grand}  ({pct_s:.0f}%)")


# ---------------------------------------------------------------------------
# Request firing
# ---------------------------------------------------------------------------

def _build_request(row: Dict[str, Any], variant: Dict[str, Any],
                   conf: Dict[str, Any], aidp_io) -> Tuple[str, str, Optional[Any], Dict[str, str]]:
    """Return (verb, url, body, headers) for a scenario variant."""
    verb = row.get("verb", "POST")
    path_tpl = row.get("path_template", "/knowledgeBases")
    # Build base URL using aidp_io's helper.
    base, _signer, _requests, _timeout = aidp_io._client(conf, {})  # noqa: SLF001
    # Substitute path placeholders from variant.path_params.
    path = path_tpl
    for key, val in (variant.get("path_params") or {}).items():
        path = path.replace("{" + key + "}", quote(str(val), safe=""))
    url = f"{base}{path}"
    body = variant.get("body")
    if body == "__OMIT__":
        body = None
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    headers.update(variant.get("headers") or {})
    return verb, url, body, headers


def _fire(verb: str, url: str, body: Optional[Any], headers: Dict[str, str],
          conf: Dict[str, Any], aidp_io) -> Dict[str, Any]:
    """Sign + send. Returns {status, body_text, body_json, opc_request_id, latency_ms}."""
    base, signer, requests_mod, timeout = aidp_io._client(conf, {})  # noqa: SLF001
    start = time.time()
    kwargs: Dict[str, Any] = {"auth": signer, "timeout": timeout, "headers": headers}
    if body is not None:
        kwargs["json"] = body
    elif verb.upper() in ("POST", "PUT", "PATCH"):
        # Literal null body case — send empty bytes with content-length 0.
        kwargs["data"] = b""
    resp = requests_mod.request(verb.upper(), url, **kwargs)
    latency_ms = int((time.time() - start) * 1000)
    txt = (resp.text or "")[:4096]
    try:
        bj = resp.json() if resp.content else None
    except Exception:
        bj = None
    return {
        "status": resp.status_code,
        "body_text": txt,
        "body_json": bj,
        "opc_request_id": resp.headers.get("opc-request-id", ""),
        "latency_ms": latency_ms,
    }


def _extract_message(body_json: Optional[Any], body_text: str) -> str:
    """Best-effort extract a message string from the response body."""
    if isinstance(body_json, dict):
        for key in ("message", "detail", "error_message", "errorMessage"):
            v = body_json.get(key)
            if isinstance(v, str) and v:
                return v
        # nested error
        err = body_json.get("error") or {}
        if isinstance(err, dict):
            for key in ("message", "detail"):
                v = err.get(key)
                if isinstance(v, str) and v:
                    return v
    return body_text


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------

def _assert_validation(row: Dict[str, Any], variant: Dict[str, Any],
                       resp: Dict[str, Any]) -> Tuple[bool, str]:
    expected_status = row.get("expected_status", [400, 422])
    if isinstance(expected_status, int):
        expected_status = [expected_status]
    if resp["status"] not in expected_status:
        return False, f"status {resp['status']} not in {expected_status}"
    msg = _extract_message(resp["body_json"], resp["body_text"])
    needle = row.get("expected_message_substring", "")
    if needle and needle not in msg:
        return False, f"message missing substring '{needle[:60]}…' (got: '{msg[:120]}')"
    # Optional: <paramName> filled
    if row.get("expected_message_param_filled"):
        if "<paramName>" in msg:
            return False, "message still contains literal '<paramName>' (not substituted)"
    return True, ""


def _assert_noop(row: Dict[str, Any], variant: Dict[str, Any],
                 resp: Dict[str, Any]) -> Tuple[bool, str]:
    """A noop scenario is documented as 'no customer-visible error'.
    Either the request succeeded (2xx) or it returned without the documented
    error pattern. We assert the response was NOT an error envelope."""
    if 200 <= resp["status"] < 400:
        return True, ""
    # Some noop scenarios still produce non-2xx (e.g. silent best-effort
    # callbacks during otherwise-successful async operations). We just
    # assert NO customer-visible message is present.
    msg = _extract_message(resp["body_json"], resp["body_text"])
    # If the response carries a documented KB error message, that's a regression.
    documented_patterns = [
        "Invalid request",
        "Knowledge Base not enabled",
        "Service unavailable",
        "Knowledge Base already exists",
        "Embedding model is not found",
        "Operation timed out",
        "Conflict",
        "Database connection",
        "Not found",
        "Database operation failed",
        "Database resources unavailable",
        "Invalid database wallet",
    ]
    for p in documented_patterns:
        if p in msg:
            return False, f"noop row produced documented error: '{p}'"
    return True, ""


# ---------------------------------------------------------------------------
# Main run loop
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    rows = _load_catalog()
    if not rows:
        print(red("[err] no rows found under tests/kb_validation/catalog/"))
        return 1

    # Filtering.
    if args.method:
        rows = [r for r in rows if r.get("method") == args.method]
    if args.id:
        rows = [r for r in rows if r.get("id") == args.id]
    if args.cls:
        rows = [r for r in rows if r.get("class") == args.cls]
    if not rows:
        print(yellow("[warn] no rows match the filters"))
        return 0

    if args.report:
        _coverage_report(rows)
        return 0

    conf = _build_conf(args.profile)
    if not conf.get("data_lake_ocid"):
        print(red("[err] no data_lake_ocid resolvable from ~/.aidp/aidp-deploy.config.json"))
        return 2

    aidp_io = _import_aidp_io()

    print(bold(f"\n=== KB validation run: profile=[{args.profile}] auth_mode={conf['auth_mode']} ==="))
    print(f"target: {conf['region']} / {conf['data_lake_ocid'][:48]}...")
    print(f"rows:   {len(rows)}\n")

    if not args.skip_preflight:
        base, signer, requests_mod, timeout = aidp_io._client(conf, {})  # noqa: SLF001
        probe_url = f"{base}/knowledgeBases?catalogKey=__preflight__&schemaKey=__preflight__"
        try:
            pr = requests_mod.get(probe_url, auth=signer, timeout=timeout)
        except Exception as e:
            print(red(f"[preflight] request failed: {e}"))
            return 4
        if pr.status_code == 404 and "NotAuthorizedOrNotFound" in (pr.text or ""):
            print(red("[preflight] KB endpoint returns 404 NotAuthorizedOrNotFound."))
            print(yellow("  This tenancy either does not have the Knowledge Base feature enabled"))
            print(yellow("  or the caller lacks IAM permission on KB resources. Without KB"))
            print(yellow("  enabled, every scenario would FAIL with the same generic 404."))
            print(dim("  Bypass this check with --skip-preflight if you want to run anyway."))
            return 5
        print(dim(f"[preflight] /knowledgeBases reachable (status {pr.status_code})\n"))

    counters = {"PASS": 0, "FAIL": 0, "SKIP": 0, "ERR": 0}
    failures: List[str] = []

    for row in rows:
        rid = row.get("id", "?")
        method = row.get("method", "?")
        cls = row.get("class", "?")
        scenario = row.get("scenario", "?")[:60]

        # Class-based skip.
        if cls in ("infra", "db_inject", "race"):
            counters["SKIP"] += 1
            if args.verbose:
                print(f"  {yellow('SKIP')} {rid:>8}  {method:24} {scenario:60} [{cls}]")
            continue
        if cls == "fixture" and not args.fixtures:
            counters["SKIP"] += 1
            if args.verbose:
                print(f"  {yellow('SKIP')} {rid:>8}  {method:24} {scenario:60} [needs --fixtures]")
            continue

        variants = row.get("variants") or [{"name": "default", "body": row.get("body")}]
        for v in variants:
            vname = v.get("name", "default")
            label = f"{rid}/{vname}" if len(variants) > 1 else rid
            try:
                verb, url, body, headers = _build_request(row, v, conf, aidp_io)
                resp = _fire(verb, url, body, headers, conf, aidp_io)
            except Exception as e:
                counters["ERR"] += 1
                failures.append(f"{label}: harness error: {e}")
                print(f"  {red('ERR ')} {label:>16}  {method:24} {scenario:60} {e}")
                continue

            if cls == "noop":
                ok, why = _assert_noop(row, v, resp)
            else:
                ok, why = _assert_validation(row, v, resp)
            mark = green("PASS") if ok else red("FAIL")
            counters["PASS" if ok else "FAIL"] += 1
            if not ok:
                failures.append(f"{label}: {why}")
            if args.verbose or not ok:
                print(f"  {mark} {label:>16}  {method:24} {scenario:60} "
                      f"status={resp['status']} {resp['latency_ms']}ms"
                      + (f" — {why}" if why else ""))

    print()
    print(bold("RESULT  ") +
          green(f"PASS={counters['PASS']}") + "  " +
          (red(f"FAIL={counters['FAIL']}") if counters['FAIL'] else f"FAIL={counters['FAIL']}") + "  " +
          (red(f"ERR={counters['ERR']}") if counters['ERR'] else f"ERR={counters['ERR']}") + "  " +
          yellow(f"SKIP={counters['SKIP']}"))

    if failures and args.verbose:
        print(red(f"\nFailures ({len(failures)}):"))
        for f in failures[:20]:
            print(f"  - {f}")
        if len(failures) > 20:
            print(dim(f"  ... and {len(failures) - 20} more"))

    if args.strict and (counters["FAIL"] > 0 or counters["ERR"] > 0):
        return 3
    return 0


def main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog="kb_validation_harness")
    p.add_argument("--profile", default=os.environ.get("AIDP_OCI_PROFILE", "DEFAULT"),
                   help="OCI config profile (default: DEFAULT or $AIDP_OCI_PROFILE)")
    p.add_argument("--method", help="Filter by KB method name (e.g. CreateKnowledgeBase)")
    p.add_argument("--id", help="Filter by scenario id (e.g. CKB-002)")
    p.add_argument("--class", dest="cls", choices=CLASSES,
                   help="Filter by scenario class")
    p.add_argument("--fixtures", action="store_true",
                   help="Enable fixture-class scenarios (creates real KBs in your tenancy)")
    p.add_argument("--report", action="store_true",
                   help="Print the coverage matrix and exit (no requests fired)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Print every row, including SKIP and PASS")
    p.add_argument("--strict", action="store_true",
                   help="Exit non-zero on any FAIL or ERR (CI flag)")
    p.add_argument("--skip-preflight", action="store_true",
                   help="Skip the KB-endpoint-reachable preflight (run anyway)")
    args = p.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
