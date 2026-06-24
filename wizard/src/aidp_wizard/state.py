"""Wizard state — carries data between steps.

All step screens read + mutate the same WizardState instance held on the
App. State is persisted to ~/.aidp/wizard.config.json so re-launches can
skip already-completed steps (LLM key, OCI auth) and resume mid-flow.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional


CONFIG_PATH = Path.home() / ".aidp" / "wizard.config.json"


@dataclass
class WizardState:
    # Step 1 — LLM
    llm_provider: str = "anthropic"        # "anthropic" | "openai"
    llm_api_key: str = ""                  # bearer token, secret
    llm_model: str = "claude-opus-4-7"     # default; user can override
    llm_validated: bool = False

    # Step 2 — AIDP auth
    oci_profile: str = "DEFAULT"
    oci_auth_mode: str = "auto"            # "auto" | "session_token" | "user_principal"
    region: str = "us-ashburn-1"
    data_lake_ocid: str = ""
    auth_validated: bool = False

    # Step 3 — Workspace
    workspace_key: str = ""
    workspace_name: str = ""

    # Step 4 — Resources picked for this tool
    catalog_key: str = ""
    catalog_name: str = ""
    schema_key: str = ""
    schema_name: str = ""
    volume_key: str = ""
    volume_name: str = ""
    kb_key: str = ""
    kb_name: str = ""

    # Step 5 — Tool kind + details
    tool_kind: str = ""                    # "file_reader" | "sql" | "rest" | "rag" | "llm_custom"
    tool_class_name: str = "MyCustomTool"
    tool_display_name: str = "My Custom Tool"
    tool_description: str = ""

    # Step 6 — LLM generation
    user_intent: str = ""                  # natural-language description for LLM
    generated_code: str = ""               # tool_implementation.py contents
    generated_config: str = ""             # tool_config.json contents

    # Step 7 — Build
    output_dir: Path = field(default_factory=lambda: Path.home() / ".aidp" / "wizard" / "dist")
    built_zip_path: Optional[Path] = None
    upload_after_build: bool = False

    # Step tracking
    current_step: int = 0
    completed_steps: List[int] = field(default_factory=list)

    # --- Persistence ---

    def save(self) -> None:
        """Save (non-secret) state to ~/.aidp/wizard.config.json."""
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = self._serializable()
        CONFIG_PATH.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except Exception:
            pass

    def _serializable(self) -> Dict[str, Any]:
        d = asdict(self)
        # Path -> str
        for k, v in list(d.items()):
            if isinstance(v, Path):
                d[k] = str(v)
        return d

    @classmethod
    def load(cls) -> "WizardState":
        if not CONFIG_PATH.is_file():
            return cls()
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return cls()
        # Coerce paths back
        if "output_dir" in data and isinstance(data["output_dir"], str):
            data["output_dir"] = Path(data["output_dir"])
        if "built_zip_path" in data and isinstance(data["built_zip_path"], str):
            data["built_zip_path"] = Path(data["built_zip_path"]) if data["built_zip_path"] else None
        try:
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except Exception:
            return cls()

    # --- Step bookkeeping ---

    def mark_step_done(self, step_idx: int) -> None:
        if step_idx not in self.completed_steps:
            self.completed_steps.append(step_idx)
        self.save()


# Ordered step metadata. Single source of truth for the sidebar + the
# screen navigation in app.py. Adding a step = one entry here + one
# screen class.
STEPS: List[Dict[str, str]] = [
    {"id": "llm",       "title": "LLM token",      "screen": "LLMTokenScreen"},
    {"id": "auth",      "title": "AIDP auth",      "screen": "AuthScreen"},
    {"id": "workspace", "title": "Workspace",      "screen": "WorkspaceScreen"},
    {"id": "resources", "title": "Resources",      "screen": "ResourcesScreen"},
    {"id": "kind",      "title": "Tool kind",      "screen": "ToolKindScreen"},
    {"id": "generate",  "title": "Generate",       "screen": "GenerateScreen"},
    {"id": "build",     "title": "Build & deploy", "screen": "BuildScreen"},
]
