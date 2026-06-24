"""Step 6 — Generate the tool code.

If tool_kind == llm_custom, takes the user's plain-English intent +
the helpers summary + the prior-step state and calls the LLM. For
other kinds, uses the template path. Either way, validates the
result with ast.parse + class/method/decorator checks.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.widgets import Static, TextArea, Button

from ._base import WizardScreen
from .. import code_gen


class GenerateScreen(WizardScreen):
    TITLE = "Step 6 — Generate the Python"
    SUBTITLE = ("Pre-filled with the description you wrote in step 5. Edit if "
                "you want to refine the prompt the LLM sees, then click Generate.")

    def compose_body(self) -> ComposeResult:
        with Vertical():
            yield Static(
                f"Tool kind: [bold]{self.state.tool_kind}[/]   "
                f"Class: [bold]{self.state.tool_class_name}[/]",
                classes="field-hint",
            )

            yield Static("Prompt to the LLM (editable)", classes="field-label")
            yield Static(
                "For [bold]llm_custom[/] this is the entire generation prompt. "
                "For templated kinds the wizard renders a Jinja template instead "
                "and ignores this field.",
                classes="field-hint",
            )
            yield TextArea(text=self.state.user_intent or self.state.tool_description or "",
                           id="intent")

            with Horizontal(id="gen-row"):
                yield Button("Generate code", id="generate", classes="-primary")
                yield Button("Re-generate", id="regenerate", classes="-secondary")

            yield Static("Generated code (read-only preview)", classes="field-label")
            # NOTE: no language= — tree-sitter isn't a default dep; the preview
            # is intentionally plain text so the wizard works in any env.
            yield TextArea(text=self.state.generated_code or "",
                           id="code-preview", read_only=True)

            yield Static("", id="gen-status", classes="status-dim")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id in ("generate", "regenerate"):
            self._do_generate()
            return
        super().on_button_pressed(event)

    def _do_generate(self) -> None:
        status = self.query_one("#gen-status", Static)
        self.state.user_intent = self.query_one("#intent", TextArea).text.strip()
        self.state.save()

        if self.state.tool_kind == "llm_custom":
            if not self.state.user_intent:
                status.update("[#f85149]Intent is required for llm_custom.[/]")
                return
            status.update(f"[#58a6ff]Calling {self.state.llm_provider} ({self.state.llm_model})...[/]")
            self.app.refresh()
            code, err = code_gen.llm_implementation(self.state)
        else:
            status.update("[#58a6ff]Rendering template...[/]")
            self.app.refresh()
            code, err = code_gen.template_implementation(self.state), None

        if err:
            status.update(f"[#f85149]✗ {err}[/]")
            return
        if not code:
            status.update("[#f85149]✗ Generator returned empty code.[/]")
            return

        ok, problems = code_gen.validate_implementation(code, expected_class=self.state.tool_class_name)
        if not ok:
            status.update(f"[#d29922]Generated, but validation failed: {problems}[/]")
        else:
            status.update(f"[#3fb950]✓ Generated + validated ({len(code.splitlines())} lines)[/]")

        self.state.generated_code = code
        self.state.generated_config = code_gen.build_tool_config(self.state)
        self.state.save()

        preview = self.query_one("#code-preview", TextArea)
        preview.text = code

    def on_next(self) -> bool:
        status = self.query_one("#gen-status", Static)
        if not self.state.generated_code:
            status.update("[#f85149]Click Generate code first.[/]")
            return False
        ok, problems = code_gen.validate_implementation(
            self.state.generated_code, expected_class=self.state.tool_class_name
        )
        if not ok:
            status.update(f"[#f85149]Code still fails validation: {problems}[/]")
            return False
        return True
