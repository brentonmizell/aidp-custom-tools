#!/usr/bin/env python3
"""
build_with_config.py — auto-fill conf defaults across every Custom Tool
package's tool_config.json, then rebuild each package's .zip.

Reads:
  ~/.aidp/aidp-deploy.config.json   (AIDP region / data lake OCID / workspace)
  ~/.oci/config                     (tenancy_ocid / user_ocid / fingerprint)
                                    or the profile named by env AIDP_OCI_PROFILE
                                    or --profile <name>

Auto-fills these conf keys when present in a tool's conf:
  region              ← aidp.region
  data_lake_ocid      ← aidp.dataLakeOcid
  workspace_id        ← aidp.workspaceId
  api_version         ← aidp.apiVersion
  tenancy_ocid        ← oci.tenancy
  user_ocid           ← oci.user
  fingerprint         ← oci.fingerprint

NEVER fills (catalog/schema-specific or secret):
  catalog, schema, volume, volume_key, kb_key, table_key,
  private_key_content, pass_phrase, webhook_url, smtp_password

Preserves any non-empty existing value by default. Use --force to overwrite.

Usage:
  python build_with_config.py                    # auto-fill + rebuild all zips
  python build_with_config.py --force            # overwrite existing values
  python build_with_config.py --profile aidp     # use the 'aidp' profile in ~/.oci/config
  python build_with_config.py --dry-run          # show planned changes; don't write
  python build_with_config.py --no-zip           # auto-fill only; skip zip rebuild
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import sys
import zipfile
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent
TOOLS_DIR = REPO_ROOT / "CUSTOM_CODE_TOOLS"

# Conf keys we'll touch (and the source of their value).
AUTO_FILL_KEYS = {
    "region":         ("aidp",  "region"),
    "data_lake_ocid": ("aidp",  "dataLakeOcid"),
    "workspace_id":   ("aidp",  "workspaceId"),
    "api_version":    ("aidp",  "apiVersion"),
    "tenancy_ocid":   ("oci",   "tenancy"),
    "user_ocid":      ("oci",   "user"),
    "fingerprint":    ("oci",   "fingerprint"),
}

# Never touch these (user-specific or sensitive).
SKIP_KEYS = {
    "catalog", "schema", "volume", "volume_key",
    "kb_key", "job_key", "table_key",
    "catalog_key", "schema_key",
    "private_key_content", "pass_phrase",
    "webhook_url", "smtp_password",
    "namespace",
    "compartment_id",
}


def load_aidp_config(explicit_path: Optional[Path] = None) -> Dict:
    """Load ~/.aidp/aidp-deploy.config.json, or empty dict if missing."""
    paths = []
    if explicit_path:
        paths.append(explicit_path)
    paths.extend([
        Path.home() / ".aidp" / "aidp-deploy.config.json",
        Path.home() / ".aidp-flow-designer" / "aidp-flow-designer.config.json",
    ])
    for p in paths:
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                data["__source__"] = str(p)
                return data
            except json.JSONDecodeError as e:
                print(f"  [warn] could not parse {p}: {e}", file=sys.stderr)
    return {}


def load_oci_config(profile: str = "DEFAULT") -> Dict:
    """Load the named profile from ~/.oci/config."""
    cfg_path = Path.home() / ".oci" / "config"
    if not cfg_path.is_file():
        return {}
    parser = configparser.ConfigParser()
    parser.read(cfg_path)
    if profile not in parser:
        # Try common alternates.
        for candidate in (profile.upper(), profile.lower(), "DEFAULT"):
            if candidate in parser:
                profile = candidate
                break
        else:
            return {}
    section = dict(parser[profile])
    section["__profile__"] = profile
    return section


def value_for_key(key: str, aidp: Dict, oci: Dict) -> Optional[str]:
    """Return the auto-fill value for `key`, or None if we can't fill it."""
    if key not in AUTO_FILL_KEYS:
        return None
    src_name, src_key = AUTO_FILL_KEYS[key]
    src = aidp if src_name == "aidp" else oci
    val = src.get(src_key)
    if val is None or val == "":
        return None
    return str(val)


def patch_conf(
    conf: Dict,
    aidp: Dict,
    oci: Dict,
    force: bool,
) -> Tuple[Dict, list]:
    """Return a (new conf, list of (key, old, new) tuples for the changes)."""
    out = dict(conf)
    changes = []
    for key in list(out.keys()):
        if key in SKIP_KEYS:
            continue
        if key not in AUTO_FILL_KEYS:
            continue
        new = value_for_key(key, aidp, oci)
        if new is None:
            continue
        old = out.get(key, "")
        if old and not force:
            continue
        if old == new:
            continue
        out[key] = new
        changes.append((key, old, new))
    return out, changes


def walk_tool_configs(tools_dir: Path) -> Iterable[Path]:
    if not tools_dir.is_dir():
        return []
    for pkg in sorted(tools_dir.iterdir()):
        candidate = pkg / "src" / "tool_config.json"
        if candidate.is_file():
            yield candidate


def update_one(
    path: Path,
    aidp: Dict,
    oci: Dict,
    force: bool,
    dry_run: bool,
) -> int:
    """Patch one tool_config.json. Returns total changes across its tools."""
    data = json.loads(path.read_text(encoding="utf-8"))
    tools = data.get("tools") or []
    if not isinstance(tools, list):
        return 0
    pkg = path.parent.parent.name
    total = 0
    for tool in tools:
        conf = tool.get("conf")
        if not isinstance(conf, dict):
            continue
        patched, changes = patch_conf(conf, aidp, oci, force=force)
        if not changes:
            continue
        total += len(changes)
        for k, old, new in changes:
            old_display = old if old else "(empty)"
            new_display = new[:18] + "…" if len(new) > 20 else new
            print(f"  {pkg:24} {tool.get('toolClassName','?'):22} {k:18} {old_display!s:>20} -> {new_display}")
        tool["conf"] = patched
    if total > 0 and not dry_run:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return total


def rebuild_zips(tools_dir: Path) -> None:
    """Re-zip every package's src/ into <pkg>/<pkg>.zip."""
    for pkg_dir in sorted(tools_dir.iterdir()):
        src = pkg_dir / "src"
        if not src.is_dir():
            continue
        zip_path = pkg_dir / f"{pkg_dir.name}.zip"
        if zip_path.exists():
            zip_path.unlink()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for entry in src.rglob("*"):
                if entry.is_dir():
                    continue
                # Skip pyc and __pycache__.
                if "__pycache__" in entry.parts or entry.suffix == ".pyc":
                    continue
                arcname = entry.relative_to(src).as_posix()
                zf.write(entry, arcname)
        print(f"  rebuilt {zip_path.name} ({zip_path.stat().st_size:>7,} bytes)")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=os.environ.get("AIDP_OCI_PROFILE", "DEFAULT"),
                        help="OCI config profile (default: DEFAULT or $AIDP_OCI_PROFILE)")
    parser.add_argument("--aidp-config", type=Path, default=None,
                        help="Path to aidp-deploy.config.json (auto-discovered if omitted)")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite existing non-empty conf values")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show planned changes; don't write")
    parser.add_argument("--no-zip", action="store_true",
                        help="Skip rebuilding the .zip artifacts")
    args = parser.parse_args(argv)

    print("=== build_with_config ===")
    aidp = load_aidp_config(args.aidp_config)
    oci = load_oci_config(args.profile)

    if not aidp:
        print("[warn] No AIDP config found at ~/.aidp/aidp-deploy.config.json", file=sys.stderr)
    else:
        print(f"[ok] AIDP config: {aidp.get('__source__','(unknown)')}")
        print(f"     region={aidp.get('region','?')}  workspace={aidp.get('workspaceDisplayName') or aidp.get('workspaceId','?')}")
    if not oci:
        print(f"[warn] No OCI config profile [{args.profile}] in ~/.oci/config — tenancy/user/fingerprint will be skipped", file=sys.stderr)
    else:
        print(f"[ok] OCI profile: {oci.get('__profile__','DEFAULT')}")

    if not aidp and not oci:
        print("\nNothing to auto-fill — both config sources are empty.", file=sys.stderr)
        print("Create ~/.aidp/aidp-deploy.config.json (see CONFIG.md) and try again.", file=sys.stderr)
        return 1

    print(f"\n[scan] {TOOLS_DIR}")
    total = 0
    files = 0
    for path in walk_tool_configs(TOOLS_DIR):
        files += 1
        total += update_one(path, aidp, oci, args.force, args.dry_run)
    print(f"\n[summary] {total} field(s) updated across {files} tool_config.json file(s)" +
          (" (dry-run; no files written)" if args.dry_run else ""))

    if not args.dry_run and not args.no_zip:
        print(f"\n[zip] rebuilding artifacts in {TOOLS_DIR}")
        rebuild_zips(TOOLS_DIR)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
