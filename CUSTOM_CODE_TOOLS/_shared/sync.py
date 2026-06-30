"""Sync the shared credential_resolver.py into every tool's src/utils/.

Run after editing _shared/credential_resolver.py:
    python CUSTOM_CODE_TOOLS/_shared/sync.py
"""
from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # CUSTOM_CODE_TOOLS/
SRC = ROOT / "_shared" / "credential_resolver.py"


def main() -> int:
    if not SRC.is_file():
        print(f"ERROR: source missing: {SRC}")
        return 1
    synced = 0
    for tool_dir in sorted(ROOT.iterdir()):
        if not tool_dir.is_dir() or tool_dir.name in ("_shared",):
            continue
        utils = tool_dir / "src" / "utils"
        if not utils.is_dir():
            continue
        dest = utils / "credential_resolver.py"
        shutil.copy2(SRC, dest)
        print(f"  synced -> {dest.relative_to(ROOT)}")
        synced += 1
    print(f"\nSynced credential_resolver.py to {synced} tool(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
