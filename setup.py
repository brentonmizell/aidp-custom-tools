#!/usr/bin/env python3
"""
setup.py - interactive wizard for the AIDP Custom Tools repo.

Subcommands:
  python setup.py wizard       # default: init -> configure -> build
  python setup.py init         # generate / verify ~/.aidp + ~/.oci config
  python setup.py configure    # walk every tool's conf and fill values
  python setup.py new-tool     # scaffold a brand-new custom tool package
  python setup.py build        # auto-fill + rebuild all zips (non-interactive)
  python setup.py status       # show what's filled vs blank across all tools

Cross-platform stdlib only - no pip deps.
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent
TOOLS_DIR = REPO_ROOT / "CUSTOM_CODE_TOOLS"
TEMPLATE_DIR = REPO_ROOT / "CUSTOM_CODE_TEMPLATE"

# ---------------------------------------------------------------------------
# ANSI colors (auto-disabled when stdout isn't a TTY)
# ---------------------------------------------------------------------------

_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

def _c(code: str, s: str) -> str:
    return f"\x1b[{code}m{s}\x1b[0m" if _USE_COLOR else s

def bold(s: str) -> str:    return _c("1", s)
def dim(s: str) -> str:     return _c("2", s)
def cyan(s: str) -> str:    return _c("36", s)
def green(s: str) -> str:   return _c("32", s)
def yellow(s: str) -> str:  return _c("33", s)
def red(s: str) -> str:     return _c("31", s)

# ---------------------------------------------------------------------------
# Conf key categorization (mirrors build_with_config.py)
# ---------------------------------------------------------------------------

AUTO_FILL_KEYS = {
    "region":         ("aidp",  "region"),
    "data_lake_ocid": ("aidp",  "dataLakeOcid"),
    "workspace_id":   ("aidp",  "workspaceId"),
    "workspace_key":  ("aidp",  "workspaceId"),
    "lake_ocid":      ("aidp",  "dataLakeOcid"),
    "api_version":    ("aidp",  "apiVersion"),
    "tenancy_ocid":   ("oci",   "tenancy"),
    "user_ocid":      ("oci",   "user"),
    "fingerprint":    ("oci",   "fingerprint"),
}

# These two must match the live AIDP API surface and are always overwritten,
# even when there's an existing tool default. 20240831 -> /dataLakes/,
# 20260430 -> /aiDataPlatforms/. A mismatch 404s on every call.
ALWAYS_FORCE_KEYS = {"api_version", "service_path"}

API_VERSION_TO_SERVICE_PATH = {
    "20240831": "dataLakes",
    "20260430": "aiDataPlatforms",
}

def derive_service_path(api_version: Optional[str]) -> Optional[str]:
    if not api_version:
        return None
    return API_VERSION_TO_SERVICE_PATH.get(str(api_version).strip())

# Fields the wizard prompts for interactively (workspace-specific, no auto-fill).
PROMPTABLE_KEYS = {
    "catalog", "schema", "volume", "volume_key",
    "kb_key", "job_key", "table_key",
    "catalog_key", "schema_key",
    "compartment_id", "bucket", "namespace",
    "model_id", "model_provider",
    "cluster_key", "notebook_path",
    "smtp_host", "smtp_port", "smtp_username", "from_address",
    "imap_host", "imap_port", "imap_username",
    "service_path", "auth_mode",
    "endpoint",
    "user_agent",
}

# Never prompt for these - they're secrets, set at deploy time.
SECRET_KEYS = {
    "private_key_content", "pass_phrase",
    "webhook_url", "smtp_password", "imap_password",
    "api_key", "bearer_token",
}

# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------

def ask(prompt: str, default: Optional[str] = None, validate=None, secret: bool = False) -> str:
    """Prompt the user, returning the answer (or default on empty)."""
    suffix = f" [{dim(default)}]" if default else ""
    full = f"{cyan('?')} {prompt}{suffix}: "
    while True:
        if secret:
            try:
                import getpass
                val = getpass.getpass(full)
            except Exception:
                val = input(full)
        else:
            try:
                val = input(full)
            except EOFError:
                val = ""
        val = val.strip()
        if not val and default is not None:
            val = default
        if validate:
            ok, err = validate(val)
            if not ok:
                print(red(f"  {err}"))
                continue
        return val

def ask_yes_no(prompt: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        raw = input(f"{cyan('?')} {prompt} {suffix}: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False

def ask_choice(prompt: str, choices: List[str], default: Optional[str] = None) -> str:
    print(f"{cyan('?')} {prompt}")
    for i, c in enumerate(choices, 1):
        marker = "  >" if c == default else "   "
        print(f"{marker} {i}) {c}")
    while True:
        raw = input(f"  Enter 1-{len(choices)}{' (Enter to keep default)' if default else ''}: ").strip()
        if not raw and default:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return choices[int(raw) - 1]
        print(red("  invalid choice"))

# ---------------------------------------------------------------------------
# Config I/O
# ---------------------------------------------------------------------------

OCID_RE = re.compile(r"^ocid1\.[a-z]+\.[a-z0-9-]+\.[a-z0-9-]*\..+$")

def _validate_ocid(prefix: str):
    def _f(v: str) -> Tuple[bool, str]:
        if not v:
            return True, ""
        if not v.startswith(f"ocid1.{prefix}."):
            return False, f"expected an OCID starting with ocid1.{prefix}."
        return True, ""
    return _f

def _validate_required(v: str) -> Tuple[bool, str]:
    return (True, "") if v else (False, "this field is required")

def aidp_config_path() -> Path:
    return Path.home() / ".aidp" / "aidp-deploy.config.json"

def load_aidp_config() -> Dict:
    p = aidp_config_path()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_aidp_config(cfg: Dict) -> None:
    p = aidp_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Strip transient keys.
    out = {k: v for k, v in cfg.items() if not k.startswith("__")}
    p.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")

def load_oci_config(profile: str = "DEFAULT") -> Dict:
    p = Path.home() / ".oci" / "config"
    if not p.is_file():
        return {}
    cp = configparser.ConfigParser()
    cp.read(p)
    if profile not in cp:
        return {}
    return dict(cp[profile])

# ---------------------------------------------------------------------------
# Subcommand: init
# ---------------------------------------------------------------------------

def cmd_init(args) -> int:
    print(bold("\n=== Init: AIDP + OCI configuration ===\n"))

    # 1. AIDP config.
    existing = load_aidp_config()
    if existing and not args.re_prompt:
        print(green(f"[ok] ~/.aidp/aidp-deploy.config.json already present"))
        print(f"     workspace={existing.get('workspaceDisplayName') or existing.get('workspaceId','?')}"
              f"  region={existing.get('region','?')}")
        if not ask_yes_no("Re-prompt for AIDP config values?", default=False):
            pass
        else:
            existing = _prompt_aidp_config(existing)
            save_aidp_config(existing)
            print(green(f"[ok] saved {aidp_config_path()}"))
    else:
        print(yellow("[init] no ~/.aidp/aidp-deploy.config.json found"))
        print("       I'll prompt for the required values. Find them in:")
        print("       AIDP Console -> top-right -> Workspace info\n")
        cfg = _prompt_aidp_config(existing)
        save_aidp_config(cfg)
        print(green(f"[ok] saved {aidp_config_path()}"))

    # 2. OCI config (read-only check; we don't write it here).
    oci_path = Path.home() / ".oci" / "config"
    if oci_path.is_file():
        profile = args.profile or "DEFAULT"
        oci = load_oci_config(profile)
        if oci:
            print(green(f"[ok] ~/.oci/config has profile [{profile}]"))
        else:
            print(yellow(f"[warn] ~/.oci/config has no profile [{profile}]"))
            print("       available profiles:", ", ".join(_list_oci_profiles()))
    else:
        print(yellow(f"[warn] no ~/.oci/config - auto-fill of tenancy/user/fingerprint will be skipped"))
        print("       run `oci setup config` to create one, then re-run this wizard")

    return 0

def _list_oci_profiles() -> List[str]:
    p = Path.home() / ".oci" / "config"
    if not p.is_file():
        return []
    cp = configparser.ConfigParser()
    cp.read(p)
    return list(cp.sections())

def _prompt_aidp_config(existing: Dict) -> Dict:
    cfg = dict(existing)
    cfg["region"] = ask("AIDP region", default=existing.get("region") or "us-ashburn-1",
                        validate=_validate_required)
    cfg["dataLakeOcid"] = ask("Data Lake OCID", default=existing.get("dataLakeOcid"),
                              validate=_validate_ocid("aidataplatform"))
    cfg["workspaceId"] = ask("Workspace UUID (not OCID - the GUID under Workspace info)",
                             default=existing.get("workspaceId"), validate=_validate_required)
    cfg["apiVersion"] = ask("AIDP API version", default=existing.get("apiVersion") or "20260430")
    cfg["workspaceDisplayName"] = ask("Workspace display name (cosmetic, optional)",
                                      default=existing.get("workspaceDisplayName") or "")
    cb = existing.get("consoleBaseUrl") or ""
    cfg["consoleBaseUrl"] = ask("Console base URL (optional, e.g. https://<id>.datalake.oci.oraclecloud.com)",
                                default=cb)
    return cfg

# ---------------------------------------------------------------------------
# Subcommand: configure
# ---------------------------------------------------------------------------

def cmd_configure(args) -> int:
    print(bold("\n=== Configure: per-tool walk-through ===\n"))
    aidp = load_aidp_config()
    oci = load_oci_config(args.profile or "DEFAULT")
    if not aidp:
        print(red("[err] No AIDP config - run `python setup.py init` first"))
        return 1

    packages = sorted([p for p in TOOLS_DIR.iterdir() if p.is_dir()]) if TOOLS_DIR.is_dir() else []
    if not packages:
        print(yellow("[warn] No tool packages under CUSTOM_CODE_TOOLS/"))
        return 0

    # Optionally scope to specific packages.
    if args.package:
        wanted = set(args.package)
        packages = [p for p in packages if p.name in wanted]

    pkg_choice = packages
    if not args.all and len(packages) > 1:
        if ask_yes_no(f"Configure all {len(packages)} packages? (No = pick one)", default=True):
            pass
        else:
            pick = ask_choice("Which package?", [p.name for p in packages])
            pkg_choice = [p for p in packages if p.name == pick]

    skip_empty_workspace = ask_yes_no(
        "Skip fields you don't have values for yet? (You can re-run later)", default=True,
    )

    for pkg in pkg_choice:
        cfg_path = pkg / "src" / "tool_config.json"
        if not cfg_path.is_file():
            print(dim(f"  ({pkg.name}: no tool_config.json - skipping)"))
            continue
        print(f"\n{bold(cyan(f'--- {pkg.name} ---'))}")
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        tools = data.get("tools") or []
        for tool in tools:
            _configure_one_tool(tool, aidp, oci, skip_empty_workspace)
        cfg_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(green(f"  [ok] saved {cfg_path.relative_to(REPO_ROOT)}"))

    if ask_yes_no("\nRebuild all .zip artifacts now?", default=True):
        _rebuild_zips()
    return 0

def _configure_one_tool(tool: Dict, aidp: Dict, oci: Dict, skip_empty: bool) -> None:
    cls = tool.get("toolClassName", "?")
    conf = tool.get("conf")
    if not isinstance(conf, dict):
        return
    descriptions = tool.get("_confDescriptions") or {}
    hints = tool.get("_uiHints", {}).get("conf", {}) or {}

    print(f"\n  {bold(cls)}")
    for key, current in list(conf.items()):
        # Auto-fill?
        if key in AUTO_FILL_KEYS:
            src_name, src_key = AUTO_FILL_KEYS[key]
            src = aidp if src_name == "aidp" else oci
            auto_val = src.get(src_key)
            if auto_val and not current:
                conf[key] = str(auto_val)
                print(f"    {green('auto')} {key:22} = {_truncate(str(auto_val))}")
                continue
            elif auto_val and current and current != str(auto_val):
                # Already set but doesn't match - leave alone, just inform.
                print(f"    {dim('keep')} {key:22} = {_truncate(str(current))} {dim(f'(auto would set {_truncate(str(auto_val))})')}")
                continue
        # Secret?
        if key in SECRET_KEYS or hints.get(key, {}).get("kind") == "secret":
            print(f"    {yellow('skip')} {key:22} = (secret - set at deploy time)")
            continue
        # Already filled - show + offer change.
        if current:
            print(f"    {dim('have')} {key:22} = {_truncate(str(current))}")
            continue
        # Prompt for it.
        if key not in PROMPTABLE_KEYS and not isinstance(current, str):
            # Numeric or boolean already has a default - don't prompt.
            continue
        if skip_empty:
            desc = descriptions.get(key, "")
            print(f"    {dim('todo')} {key:22}  {dim(desc[:80])}")
            continue
        desc = descriptions.get(key)
        if desc:
            print(f"      {dim(desc)}")
        # Enum hint?
        enum_values = hints.get(key, {}).get("values")
        if enum_values:
            val = ask_choice(f"  {key}", [str(v) for v in enum_values], default=current or None)
        else:
            val = ask(f"  {key}", default=current or "")
        if val:
            conf[key] = val

def _truncate(s: str, n: int = 32) -> str:
    return s if len(s) <= n else s[: n - 1] + "..."

# ---------------------------------------------------------------------------
# Subcommand: new-tool
# ---------------------------------------------------------------------------

NEW_TOOL_CONFIG = """{{
  "displayName": "{display}",
  "description": "{description}",
  "tools": [
    {{
      "toolClassName": "{cls}",
      "displayName": "{tool_display}",
      "description": "{tool_description}",
      "version": "1.0.0",
      "schema": [
        {{
          "name": "query",
          "type": "string",
          "description": "Runtime input to {cls}."
        }}
      ],
      "conf": {conf_block},
      "_uiHints": {{
        "conf": {{}},
        "schema": {{}}
      }},
      "_confDescriptions": {{}}
    }}
  ]
}}
"""

NEW_TOOL_IMPL = '''from typing import Any, Dict
try:
    from aidputils.agents.toolkit.tool_helper import register
    from aidputils.agents.toolkit.base import CustomToolBase
except ImportError:
    # Local-test fallback: provide minimal shims so the module imports.
    def register(_cls=None, **_kw):
        return _cls if _cls else (lambda c: c)
    class CustomToolBase: ...

try:
    from aidp_debug import debug, debug_warn, debug_error, DebugLog
except ImportError:
    def debug(*_a, **_k): pass
    def debug_warn(*_a, **_k): pass
    def debug_error(*_a, **_k): pass
    class DebugLog:
        @staticmethod
        def embed(x): return x


def get_cfg(conf: Dict[str, Any], key: str, default: Any = None) -> Any:
    """Unwrap conf['conf'] nesting + coerce stringified numbers."""
    if not isinstance(conf, dict):
        return default
    if "conf" in conf and isinstance(conf["conf"], dict):
        conf = conf["conf"]
    val = conf.get(key, default)
    if isinstance(val, str) and isinstance(default, (int, float)):
        try:
            return type(default)(val)
        except Exception:
            return default
    return val


def ok(data: Any = None, **extra) -> Dict[str, Any]:
    return DebugLog.embed({{"ok": True, "data": data, **extra}})


def fail(error: str, error_type: str = "ToolError", **extra) -> Dict[str, Any]:
    return DebugLog.embed({{"ok": False, "error": error, "error_type": error_type, **extra}})


@register
class {cls}(CustomToolBase):
    """Generated by setup.py new-tool. Replace the body of _execute_tool."""

    @classmethod
    def _execute_tool(cls, runtime_params: Dict[str, Any], conf: Dict[str, Any]) -> Dict[str, Any]:
        debug("{cls} invoked", params=list(runtime_params.keys()))
        query = runtime_params.get("query") or ""
        if not query:
            return fail("'query' is required", "ValidationError")
        try:
            # TODO: replace with real implementation.
            return ok({{"echo": query}}, query=query)
        except Exception as e:
            return fail(str(e), "RuntimeError")
'''

NEW_TOOL_REQS = """# Add pip deps your tool needs (one per line).
# requests
# pandas
"""

NEW_TOOL_README = """# {display}

{description}

## Tools

- **{tool_display}** - {tool_description}

## Build

From the repo root:
```
python setup.py build
```

The zip will be written to `{pkg}/{pkg}.zip` ready to upload via
AIDP Console -> Tools -> Upload custom tool.
"""

def cmd_new_tool(args) -> int:
    print(bold("\n=== New custom tool ===\n"))
    pkg = ask("Package name (snake_case, e.g. my_toolkit)",
              validate=lambda v: (re.fullmatch(r"[a-z][a-z0-9_]*", v) is not None,
                                  "must be lowercase snake_case"))
    pkg_dir = TOOLS_DIR / pkg
    if pkg_dir.exists():
        print(red(f"[err] {pkg_dir} already exists"))
        return 1
    cls = ask("Python class name (PascalCase, e.g. MyToolkit)",
              validate=lambda v: (re.fullmatch(r"[A-Z][A-Za-z0-9]*Tool", v) is not None,
                                  "must be PascalCase and end with 'Tool'"))
    display = ask("Package display name", default=pkg.replace("_", " ").title())
    description = ask("Package description")
    tool_display = ask("Tool display name", default=cls.replace("Tool", " Tool"))
    tool_description = ask("Tool description (read by the LLM - be precise)")

    print(f"\n{cyan('?')} What conf shape do you want as a starting point?")
    starter = ask_choice("Starter conf", ["minimal (just timeout)",
                                          "aidp_catalog (region, data_lake_ocid, etc.)",
                                          "oci_genai (region, compartment_id, model_id)",
                                          "http (base_url, timeout, headers)",
                                          "empty"], default="minimal (just timeout)")
    conf_starter = {
        "minimal (just timeout)": {"timeout": 30},
        "aidp_catalog (region, data_lake_ocid, etc.)": {
            "region": "us-ashburn-1", "data_lake_ocid": "", "api_version": "20260430",
            "catalog": "", "schema": "", "timeout": 30, "auth_mode": "resource_principal",
        },
        "oci_genai (region, compartment_id, model_id)": {
            "region": "us-ashburn-1", "compartment_id": "", "model_id": "cohere.command-r-plus",
            "model_provider": "cohere", "timeout": 60,
        },
        "http (base_url, timeout, headers)": {
            "base_url": "", "timeout": 30, "headers": {},
        },
        "empty": {},
    }[starter]

    print(f"\n{bold('Creating package...')}")
    pkg_dir.mkdir(parents=True)
    src_dir = pkg_dir / "src"
    src_dir.mkdir()
    (src_dir / "utils").mkdir()
    (src_dir / "utils" / "__init__.py").write_text("", encoding="utf-8")

    conf_block = json.dumps(conf_starter, indent=8).replace("\n", "\n      ")
    (src_dir / "tool_config.json").write_text(
        NEW_TOOL_CONFIG.format(
            display=display, description=description, cls=cls,
            tool_display=tool_display, tool_description=tool_description,
            conf_block=conf_block,
        ),
        encoding="utf-8",
    )
    (src_dir / "tool_implementation.py").write_text(
        NEW_TOOL_IMPL.format(cls=cls), encoding="utf-8",
    )
    (src_dir / "requirements.txt").write_text(NEW_TOOL_REQS, encoding="utf-8")
    (src_dir / "README.md").write_text(
        NEW_TOOL_README.format(display=display, description=description,
                                tool_display=tool_display, tool_description=tool_description,
                                pkg=pkg),
        encoding="utf-8",
    )
    (pkg_dir / "README.md").write_text(f"# {display}\n\n{description}\n", encoding="utf-8")
    (pkg_dir / "TESTING_INSTRUCTIONS.md").write_text(
        f"# Testing {display}\n\n1. Run `python setup.py build` from the repo root.\n"
        f"2. Upload `{pkg}.zip` via AIDP Console.\n"
        f"3. Add a Custom Tool node bound to `{cls}` and configure it.\n", encoding="utf-8",
    )
    (pkg_dir / "mock_files").mkdir()
    (pkg_dir / "mock_files" / "_no_mock_files.txt").write_text(
        "Add sample inputs here for testing.\n", encoding="utf-8",
    )

    print(green(f"[ok] created {pkg_dir.relative_to(REPO_ROOT)}"))
    print(dim(f"  edit: {(src_dir / 'tool_implementation.py').relative_to(REPO_ROOT)}"))
    print(dim(f"  edit: {(src_dir / 'tool_config.json').relative_to(REPO_ROOT)}"))
    if ask_yes_no("\nWalk through its conf now (run configure)?", default=True):
        return cmd_configure(argparse.Namespace(
            profile=args.profile, all=False, package=[pkg],
        ))
    if ask_yes_no("Build the .zip now?", default=True):
        _rebuild_one(pkg_dir)
    return 0

# ---------------------------------------------------------------------------
# Subcommand: build (wraps build_with_config logic)
# ---------------------------------------------------------------------------

def cmd_build(args) -> int:
    print(bold("\n=== Build: auto-fill + rebuild zips ===\n"))
    aidp = load_aidp_config()
    oci = load_oci_config(args.profile or "DEFAULT")
    if not aidp and not oci:
        print(red("[err] No AIDP or OCI config found - run `python setup.py init` first"))
        return 1
    total = 0
    files = 0
    for pkg in sorted(TOOLS_DIR.iterdir() if TOOLS_DIR.is_dir() else []):
        cfg_path = pkg / "src" / "tool_config.json"
        if not cfg_path.is_file():
            continue
        files += 1
        n = _autofill_one(cfg_path, aidp, oci, force=args.force, dry_run=args.dry_run)
        total += n
    print(f"\n[summary] {total} field(s) updated across {files} tool_config.json file(s)" +
          (" (dry-run; no writes)" if args.dry_run else ""))
    if not args.dry_run and not args.no_zip:
        _rebuild_zips()
    return 0

def _autofill_one(path: Path, aidp: Dict, oci: Dict, force: bool, dry_run: bool) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    tools = data.get("tools") or []
    pkg = path.parent.parent.name
    total = 0
    for tool in tools:
        conf = tool.get("conf")
        if not isinstance(conf, dict):
            continue
        # Pass 1: regular auto-fill.
        for key in list(conf.keys()):
            if key not in AUTO_FILL_KEYS:
                continue
            src_name, src_key = AUTO_FILL_KEYS[key]
            src = aidp if src_name == "aidp" else oci
            val = src.get(src_key)
            if not val:
                continue
            current = conf.get(key, "")
            force_this = force or key in ALWAYS_FORCE_KEYS
            if current and not force_this:
                continue
            if str(current) == str(val):
                continue
            conf[key] = str(val)
            total += 1
            old = current or "(empty)"
            print(f"  {pkg:24} {tool.get('toolClassName','?'):22} {key:18} {str(old)[:20]:>20} -> {str(val)[:24]}")
        # Pass 2: derive service_path from api_version (always).
        if "service_path" in conf:
            derived = derive_service_path(conf.get("api_version"))
            if derived and str(conf.get("service_path", "")) != derived:
                old = conf.get("service_path", "") or "(empty)"
                conf["service_path"] = derived
                total += 1
                print(f"  {pkg:24} {tool.get('toolClassName','?'):22} {'service_path':18} {str(old)[:20]:>20} -> {derived}")
    if total > 0 and not dry_run:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return total

# ---------------------------------------------------------------------------
# Subcommand: status
# ---------------------------------------------------------------------------

def cmd_status(args) -> int:
    print(bold("\n=== Status ===\n"))
    aidp = load_aidp_config()
    oci = load_oci_config(args.profile or "DEFAULT")
    print(f"AIDP config:    {green('found') if aidp else red('missing')} - {aidp_config_path()}")
    print(f"OCI config:     {green('found') if oci else red('missing')} - ~/.oci/config (profile {args.profile or 'DEFAULT'})")

    if TOOLS_DIR.is_dir():
        print(f"\n{bold('Per-package fill status:')}")
        for pkg in sorted(TOOLS_DIR.iterdir()):
            cfg = pkg / "src" / "tool_config.json"
            if not cfg.is_file():
                continue
            data = json.loads(cfg.read_text(encoding="utf-8"))
            tools = data.get("tools") or []
            filled = 0
            total = 0
            blanks: List[str] = []
            for tool in tools:
                conf = tool.get("conf") or {}
                for k, v in conf.items():
                    if k in SECRET_KEYS:
                        continue
                    total += 1
                    if v not in ("", None):
                        filled += 1
                    elif k in PROMPTABLE_KEYS:
                        blanks.append(k)
            color = green if filled == total else (yellow if filled > total / 2 else red)
            tag = color(f"{filled}/{total}")
            print(f"  {pkg.name:28} {tag} filled" +
                  (f"  {dim('todo: ' + ', '.join(sorted(set(blanks))[:5]))}" if blanks else ""))
    return 0

# ---------------------------------------------------------------------------
# Zip rebuilds
# ---------------------------------------------------------------------------

def _rebuild_zips() -> None:
    print(f"\n{bold('Rebuilding .zip artifacts...')}")
    for pkg in sorted(TOOLS_DIR.iterdir()):
        if pkg.is_dir():
            _rebuild_one(pkg)

def _rebuild_one(pkg_dir: Path) -> None:
    src = pkg_dir / "src"
    if not src.is_dir():
        return
    zip_path = pkg_dir / f"{pkg_dir.name}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry in src.rglob("*"):
            if entry.is_dir():
                continue
            if "__pycache__" in entry.parts or entry.suffix == ".pyc":
                continue
            zf.write(entry, entry.relative_to(src).as_posix())
    print(f"  {green('rebuilt')} {zip_path.name} ({zip_path.stat().st_size:>7,} bytes)")

# ---------------------------------------------------------------------------
# Default wizard (init -> configure -> build)
# ---------------------------------------------------------------------------

def cmd_wizard(args) -> int:
    print(bold("\n=== AIDP Custom Tools Wizard ===\n"))
    print("This walks you through the full setup:")
    print(f"  1. {cyan('init')}      - generate / verify ~/.aidp + ~/.oci config")
    print(f"  2. {cyan('configure')} - walk every tool's conf and fill values")
    print(f"  3. {cyan('build')}     - auto-fill + rebuild zips\n")
    if not ask_yes_no("Continue?", default=True):
        return 0
    rc = cmd_init(args)
    if rc:
        return rc
    rc = cmd_configure(args)
    if rc:
        return rc
    rc = cmd_build(args)
    return rc

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="setup.py", description=__doc__)
    parser.add_argument("--profile", default=os.environ.get("AIDP_OCI_PROFILE"),
                        help="OCI config profile (default: DEFAULT or $AIDP_OCI_PROFILE)")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("wizard", help="run init -> configure -> build")
    p.add_argument("--re-prompt", action="store_true", help="re-prompt for AIDP config even if present")
    p.add_argument("--all", action="store_true", help="configure all packages without asking")
    p.add_argument("--package", action="append", help="limit configure to specific packages")
    p.add_argument("--force", action="store_true", help="overwrite existing non-empty conf values")
    p.add_argument("--dry-run", action="store_true", help="don't write")
    p.add_argument("--no-zip", action="store_true", help="don't rebuild zips")

    p = sub.add_parser("init", help="generate / verify ~/.aidp + ~/.oci config")
    p.add_argument("--re-prompt", action="store_true")

    p = sub.add_parser("configure", help="walk every tool's conf and fill values")
    p.add_argument("--all", action="store_true")
    p.add_argument("--package", action="append")

    p = sub.add_parser("new-tool", help="scaffold a brand-new custom tool package")

    p = sub.add_parser("build", help="auto-fill + rebuild zips (non-interactive)")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-zip", action="store_true")

    p = sub.add_parser("status", help="show what's filled vs blank across all tools")

    args = parser.parse_args(argv)

    # Default to wizard when no subcommand.
    cmd = args.cmd or "wizard"
    if cmd in ("wizard",):
        # Ensure all attrs the sub-handlers reference exist.
        for attr in ("re_prompt", "all", "package", "force", "dry_run", "no_zip"):
            if not hasattr(args, attr):
                setattr(args, attr, False if attr in ("re_prompt", "all", "force", "dry_run", "no_zip") else None)

    handlers = {
        "wizard": cmd_wizard,
        "init": cmd_init,
        "configure": cmd_configure,
        "new-tool": cmd_new_tool,
        "build": cmd_build,
        "status": cmd_status,
    }
    return handlers[cmd](args)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print(red("\n[aborted]"))
        sys.exit(130)
