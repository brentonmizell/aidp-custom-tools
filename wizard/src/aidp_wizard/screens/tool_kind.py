"""Step 6 — Confirm template + class name.

The description + LLM-suggested template come from step 3 (Describe).
This screen exists to let the developer override the LLM's pick or
rename the class before code generation. If they accepted everything,
they can just click Next.
"""

from __future__ import annotations

import re

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, RadioButton, RadioSet, Static

from ._base import WizardScreen


TOOL_KINDS = [
    ("llm_custom",  "LLM-generated (default, best for non-trivial logic)",
     "The LLM writes the Python from your description. Validation runs "
     "before you advance."),
    ("file_reader", "File reader",
     "Reads a file from an AIDP volume. Inputs: volume + path."),
    ("sql",         "SQL runner",
     "Runs SQL via dynamic_query against the catalog you picked in step 5."),
    ("rest",        "REST caller",
     "Calls a REST endpoint with bearer / API-key auth + JSON body."),
    ("rag",         "RAG retriever",
     "Queries the knowledge base you picked in step 5."),
    ("echo_stub",   "Echo / stub",
     "Minimal scaffold that echoes its inputs — useful for smoke tests."),
]


class ToolKindScreen(WizardScreen):
    TITLE = "Step 6 — Confirm template + class name"
    SUBTITLE = ("The LLM's suggestions from step 3 are pre-filled. Override "
                "anything that doesn't fit, then continue to code generation.")

    def compose_body(self) -> ComposeResult:
        with Vertical():
            yield Static("Description from step 3 (read-only here — go back "
                         "to edit)", classes="field-label")
            yield Static(
                self.state.user_intent or self.state.tool_description
                or "[#f85149](no description — go back to step 3)[/]",
                classes="field-hint", id="desc-display",
            )

            yield Static("Template", classes="field-label")
            current = self.state.tool_kind or "llm_custom"
            with RadioSet(id="kind"):
                for kid, label, _desc in TOOL_KINDS:
                    yield RadioButton(label, value=(current == kid),
                                      id=f"rb-{kid}")
            yield Static(self._desc_for(current), id="kind-desc",
                         classes="field-hint")

            yield Static("Class name (PascalCase, no spaces)",
                         classes="field-label")
            yield Input(value=self.state.tool_class_name or "MyCustomTool",
                        id="class-name", placeholder="MyCustomTool")

            yield Static("Display name (shown in the AIDP UI)",
                         classes="field-label")
            yield Input(value=self.state.tool_display_name or "My Custom Tool",
                        id="display-name", placeholder="My Custom Tool")

            yield Static("", id="kind-status", classes="status-dim")

    def _desc_for(self, kind: str) -> str:
        for kid, _label, desc in TOOL_KINDS:
            if kid == kind:
                return desc
        return ""

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        if event.radio_set.id != "kind":
            return
        rb_id = event.pressed.id or ""
        kind = rb_id.removeprefix("rb-") if rb_id.startswith("rb-") else "llm_custom"
        self.state.tool_kind = kind
        self.query_one("#kind-desc", Static).update(self._desc_for(kind))

    def on_next(self) -> bool:
        status = self.query_one("#kind-status", Static)

        if not (self.state.user_intent or self.state.tool_description):
            status.update("[#f85149]Missing description — go back to step 3.[/]")
            return False

        if not self.state.tool_kind:
            self.state.tool_kind = "llm_custom"

        raw = self.query_one("#class-name", Input).value.strip()
        if not raw:
            status.update("[#f85149]Class name is required.[/]")
            return False
        cleaned = re.sub(r"[^A-Za-z0-9]+", " ", raw).title().replace(" ", "")
        if not cleaned or not cleaned[0].isalpha():
            status.update("[#f85149]Class name must start with a letter.[/]")
            return False
        self.state.tool_class_name = cleaned

        display = self.query_one("#display-name", Input).value.strip()
        self.state.tool_display_name = display or cleaned

        self.state.save()
        return True
