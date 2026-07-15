"""Sync the shared helper modules into every tool's src/utils/.

Run after editing any module in _shared/:
    python CUSTOM_CODE_TOOLS/_shared/sync.py
"""
from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # CUSTOM_CODE_TOOLS/
SHARED = ROOT / "_shared"
# Modules copied into every tool's src/utils/.
MODULES = ("credential_resolver.py", "aidp_discovery.py")


def main() -> int:
    missing = [m for m in MODULES if not (SHARED / m).is_file()]
    if missing:
        print(f"ERROR: shared source(s) missing: {missing}")
        return 1
    synced = 0
    for tool_dir in sorted(ROOT.iterdir()):
        if not tool_dir.is_dir() or tool_dir.name in ("_shared",):
            continue
        utils = tool_dir / "src" / "utils"
        if not utils.is_dir():
            continue
        for m in MODULES:
            shutil.copy2(SHARED / m, utils / m)
        print(f"  synced {len(MODULES)} module(s) -> {(utils).relative_to(ROOT)}")
        synced += 1
    print(f"\nSynced {MODULES} to {synced} tool(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
