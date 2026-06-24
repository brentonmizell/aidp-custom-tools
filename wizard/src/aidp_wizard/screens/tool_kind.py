"""Step 5 — Tool kind + identity.

Pick the tool template (file_reader / sql / rest / rag / llm_custom /
echo_stub) and supply the class name + display name + description.
Class name is normalized to PascalCase and used as the Python class
+ tool key.
"""

from __future__ import annotations

import re

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, RadioButton, RadioSet, Static, TextArea

from ._base import WizardScreen


TOOL_KINDS = [
    ("file_reader", "File reader",
     "Reads a file from an AIDP volume. Inputs: volume + path."),
    ("sql",         "SQL runner",
     "Runs SQL via dynamic_query against the catalog you picked in step 4."),
    ("rest",        "REST caller",
     "Calls a REST endpoint with bearer / API-key auth + JSON body."),
    ("rag",         "RAG retriever",
     "Queries the knowledge base you picked in step 4."),
    ("llm_custom",  "LLM-generated (custom logic)",
     "Lets you describe the tool in plain English; LLM writes the Python."),
    ("echo_stub",   "Echo / stub",
     "Minimal scaffold that echoes its inputs — useful for smoke tests."),
]


class ToolKindScreen(WizardScreen):
    TITLE = "Step 5 — Pick tool template + name it"
    SUBTITLE = ("This determines what the generated Python does and what "
                "fields the Test panel shows in AIDP.")

    def compose_body(self) -> ComposeResult:
        with Vertical():
            yield Static("Template", classes="field-label")
            with RadioSet(id="kind"):
                for kid, label, _desc in TOOL_KINDS:
                    yield RadioButton(label, value=(self.state.tool_kind == kid),
                                      id=f"rb-{kid}")
            yield Static(self._desc_for(self.state.tool_kind or "file_reader"),
                         id="kind-desc", classes="field-hint")

            yield Static("Class name (PascalCase, no spaces)", classes="field-label")
            yield Input(value=self.state.tool_class_name or "MyCustomTool",
                        id="class-name", placeholder="MyCustomTool")

            yield Static("Display name (shown in the AIDP UI)", classes="field-label")
            yield Input(value=self.state.tool_display_name or "My Custom Tool",
                        id="display-name", placeholder="My Custom Tool")

            yield Static("Description (1–2 sentences, what the tool does)",
                         classes="field-label")
            yield TextArea(text=self.state.tool_description or "", id="description")

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
        kind = rb_id.removeprefix("rb-") if rb_id.startswith("rb-") else "file_reader"
        self.state.tool_kind = kind
        self.query_one("#kind-desc", Static).update(self._desc_for(kind))

    def on_next(self) -> bool:
        status = self.query_one("#kind-status", Static)
        if not self.state.tool_kind:
            self.state.tool_kind = "file_reader"

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

        desc = self.query_one("#description", TextArea).text.strip()
        if not desc:
            status.update("[#f85149]Description is required.[/]")
            return False
        self.state.tool_description = desc

        self.state.save()
        return True
