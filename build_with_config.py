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
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent
TOOLS_DIR = REPO_ROOT / "CUSTOM_CODE_TOOLS"

# Canonical source for each shared helper. The zip-build step copies these
# into each package's src/utils/<helper>.py before zipping so the deployed
# package is self-contained, and ALSO writes them onto disk under each package
# so local dev imports (e.g. `from utils.aidp_io import ...`) keep working
# without a build step.
#
# Each entry is (canonical_source_path, dest_filename_in_src_utils). New
# helpers can be added by extending this list — sync_shared_modules() walks
# it and skips any source that isn't on disk yet (partial rollouts work).
SHARED_MODULES: List[Tuple[Path, str]] = [
    (REPO_ROOT / "aidp_io"    / "aidp_io.py",    "aidp_io.py"),
    (REPO_ROOT / "aidp_genai" / "aidp_genai.py", "aidp_genai.py"),
    (REPO_ROOT / "aidp_kb"    / "aidp_kb.py",    "aidp_kb.py"),
]

# Back-compat alias for any external caller that imported the old name.
SHARED_AIDP_IO_SRC = SHARED_MODULES[0][0]

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

# AIDP's live REST surface uses /20260430/aiDataPlatforms/{lake}/... — the
# old probe at /20240831/dataLakes/ doesn't have the same endpoint coverage.
# We default to the live shape and let the tool's Python code accept conf
# overrides on the rare custom-path tenancy.
LIVE_API_VERSION = "20260430"
LIVE_SERVICE_PATH = "aiDataPlatforms"
# api_version is no longer auto-flipped to whatever's in aidp-deploy.config.json
# — the live surface is fixed. service_path is derived from api_version inside
# the tool itself. When AIDP migrates, bump LIVE_API_VERSION here.
ALWAYS_FORCE_KEYS: set = set()

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
    """Return a (new conf, list of (key, old, new) tuples for the changes).

    ALWAYS_FORCE_KEYS override the tool's default even if `force=False`. They
    have to match the live AIDP API shape (api_version -> service_path) or
    every request 404s.
    """
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
        force_this = force or key in ALWAYS_FORCE_KEYS
        if old and not force_this:
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


_EXTENSION_ONLY_KEYS = ("_uiHints", "_confDescriptions")


def _strip_extension_only_keys(tool_config: Dict) -> bool:
    """AIDP's CustomToolEntry schema rejects unknown top-level keys with a
    Pydantic ValidationError. Strip sidecar keys (_uiHints, _confDescriptions)
    that are valid only for the extension UI."""
    stripped = False
    for tool in tool_config.get("tools") or []:
        for k in _EXTENSION_ONLY_KEYS:
            if k in tool:
                del tool[k]
                stripped = True
        for field in tool.get("schema") or []:
            if isinstance(field, dict):
                for k in _EXTENSION_ONLY_KEYS:
                    if k in field:
                        del field[k]
                        stripped = True
    return stripped


def sync_shared_modules(tools_dir: Path) -> int:
    """Copy every canonical helper in SHARED_MODULES into each package's
    src/utils/<helper>.py.

    Runs ONCE per build (before the per-package zip loop):
      - so the zip contains self-contained helpers inside src/utils/,
      - and so dev-time imports from CUSTOM_CODE_TOOLS/<pkg>/src/utils/<helper>.py
        keep working without an explicit build step.

    Missing canonical source for any single helper -> warn and skip THAT
    helper, continue with the rest. This lets partial rollouts work (e.g.
    aidp_io is canonical but aidp_kb hasn't been written yet). Returns the
    number of packages touched (i.e. at least one helper was copied into them).

    Final summary line: "[shared] N helpers synced to M packages".
    """
    if not tools_dir.is_dir():
        print(f"  [warn] {tools_dir} not found — skipping sync", file=sys.stderr)
        return 0

    # Resolve which canonical sources are actually on disk.
    present: List[Tuple[Path, str]] = []
    for src_path, dest_name in SHARED_MODULES:
        if src_path.is_file():
            present.append((src_path, dest_name))
        else:
            print(f"  [warn] canonical {dest_name} not found at {src_path} — skipping",
                  file=sys.stderr)

    if not present:
        print("[shared] no canonical helpers available — nothing to sync")
        return 0

    packages_touched = 0
    for pkg in sorted(tools_dir.iterdir()):
        if not pkg.is_dir():
            continue
        src_dir = pkg / "src"
        if not src_dir.is_dir():
            continue
        utils_dir = src_dir / "utils"
        utils_dir.mkdir(parents=True, exist_ok=True)
        init_py = utils_dir / "__init__.py"
        if not init_py.exists():
            init_py.write_text("", encoding="utf-8")
        copied_any = False
        for src_path, dest_name in present:
            dest = utils_dir / dest_name
            try:
                shutil.copyfile(src_path, dest)
                copied_any = True
            except OSError as e:
                print(f"  [warn] could not copy {dest_name} into {pkg.name}: {e}",
                      file=sys.stderr)
        if copied_any:
            packages_touched += 1
    print(f"[shared] {len(present)} helpers synced to {packages_touched} packages")
    return packages_touched


# Back-compat alias for any external caller that imported the singular name.
sync_shared_module = sync_shared_modules


def rebuild_zips(tools_dir: Path) -> None:
    """Re-zip every package's src/ into <pkg>/<pkg>.zip. tool_config.json is
    sanitized into the zip — sidecar keys stripped so AIDP's Pydantic model
    accepts the package. Source on disk stays unchanged.

    Sync ALL shared helpers ONCE at the top so the zip step picks them up
    and dev imports keep working.
    """
    import json as _json
    sync_shared_modules(tools_dir)
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
                if "__pycache__" in entry.parts or entry.suffix == ".pyc":
                    continue
                arcname = entry.relative_to(src).as_posix()
                if arcname == "tool_config.json":
                    data = _json.loads(entry.read_text(encoding="utf-8"))
                    _strip_extension_only_keys(data)
                    zf.writestr(arcname, _json.dumps(data, indent=2) + "\n")
                    continue
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
        # rebuild_zips() syncs the shared helpers internally before the
        # per-package zip loop, so we don't need a separate sync call here.
        rebuild_zips(TOOLS_DIR)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
