"""Step 7 — Build the zip + optional deploy.

Writes the generated_code + tool_config + helpers + entry.py +
__init__.py to a fresh build dir, then zips it. Surfaces the
final path so the user can drag it into AIDP's Custom Tools page.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.widgets import Input, Static, Button

from ._base import WizardScreen


HELPER_MODULES = ("aidp_io", "aidp_genai", "aidp_kb", "aidp_session")


class BuildScreen(WizardScreen):
    TITLE = "Step 7 — Build the zip"
    SUBTITLE = ("Writes the tool to disk + zips it. The zip is what you upload "
                "to AIDP → Tools → New Tool → Code.")

    def compose_body(self) -> ComposeResult:
        with Vertical():
            yield Static("Output directory", classes="field-label")
            yield Static("A subfolder named after your class is created here.",
                         classes="field-hint")
            default_out = str(self.state.output_dir) if self.state.output_dir \
                else str(Path.home() / "aidp-tools")
            yield Input(value=default_out, id="output-dir", placeholder=default_out)

            with Horizontal(id="build-row"):
                yield Button("Build zip", id="build", classes="-primary")

            yield Static("", id="build-status", classes="status-dim")

            yield Static("Final artifact", classes="field-label")
            yield Static(str(self.state.built_zip_path) if self.state.built_zip_path
                         else "(not built yet)",
                         id="zip-path", classes="status-dim")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "build":
            self._do_build()
            return
        super().on_button_pressed(event)

    def _do_build(self) -> None:
        status = self.query_one("#build-status", Static)
        out_root = Path(self.query_one("#output-dir", Input).value.strip()).expanduser()
        self.state.output_dir = out_root

        if not self.state.tool_class_name:
            status.update("[#f85149]No class name — go back to step 5.[/]")
            return
        if not self.state.generated_code:
            status.update("[#f85149]No code — go back to step 6.[/]")
            return

        try:
            tool_dir = out_root / self.state.tool_class_name
            if tool_dir.exists():
                shutil.rmtree(tool_dir)
            tool_dir.mkdir(parents=True, exist_ok=True)

            (tool_dir / f"{_snake(self.state.tool_class_name)}.py").write_text(
                self.state.generated_code, encoding="utf-8"
            )
            (tool_dir / "tool_config.json").write_text(
                self.state.generated_config, encoding="utf-8"
            )
            (tool_dir / "__init__.py").write_text("", encoding="utf-8")
            (tool_dir / "entry.py").write_text(_entry_py(self.state), encoding="utf-8")
            (tool_dir / "requirements.txt").write_text(_requirements_for(self.state),
                                                       encoding="utf-8")

            self._copy_helpers_if_available(tool_dir)

            zip_path = out_root / f"{self.state.tool_class_name}.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in tool_dir.rglob("*"):
                    if p.is_file():
                        zf.write(p, p.relative_to(out_root))

            self.state.built_zip_path = zip_path
            self.state.save()
            self.query_one("#zip-path", Static).update(f"[#3fb950]{zip_path}[/]")
            status.update(
                f"[#3fb950]✓ Built {zip_path.name} "
                f"({zip_path.stat().st_size // 1024} KB) — "
                f"upload to AIDP → Tools → New Tool → Code[/]"
            )
        except Exception as exc:
            status.update(f"[#f85149]Build failed: {exc}[/]")

    def _copy_helpers_if_available(self, tool_dir: Path) -> None:
        for mod in HELPER_MODULES:
            for candidate in _search_paths_for_helper(mod):
                if candidate.is_file():
                    shutil.copy(candidate, tool_dir / f"{mod}.py")
                    break

    def on_next(self) -> bool:
        if not self.state.built_zip_path:
            self.query_one("#build-status", Static).update(
                "[#f85149]Click Build zip first.[/]"
            )
            return False
        return True


def _snake(name: str) -> str:
    import re
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s).lower()


def _entry_py(state) -> str:
    return (
        "from custom_tool_base.base import CustomToolBase\n"
        f"from .{_snake(state.class_name)} import {state.class_name}  # noqa: F401\n"
    )


def _requirements_for(state) -> str:
    base = ["pydantic>=2.0", "requests>=2.31"]
    if state.tool_kind == "rag":
        base.append("oci>=2.130.0")
    if state.tool_kind == "sql":
        base.append("sqlglot>=23.0")
    return "\n".join(base) + "\n"


def _search_paths_for_helper(mod: str):
    """Walk likely locations for helper modules so dev runs can copy them."""
    here = Path(__file__).resolve()
    candidates = []
    for parent in here.parents:
        candidates.append(parent / "shared_helpers" / f"{mod}.py")
        candidates.append(parent / f"{mod}.py")
        if parent.name == "wizard":
            candidates.append(parent.parent / "shared_helpers" / f"{mod}.py")
            break
    return candidates
