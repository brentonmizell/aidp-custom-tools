"""Step 5 — Describe the tool, then pick template + name it.

Order matters: the wizard asks "what should this tool do?" FIRST so
the developer's intent is captured before any template decision.
Step 6 (generate) seeds its LLM prompt from this description, so the
LLM sees the goal up-front rather than asking after the fact.
"""

from __future__ import annotations

import re

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, RadioButton, RadioSet, Static, TextArea

from ._base import WizardScreen


TOOL_KINDS = [
    ("llm_custom",  "LLM-generated (recommended for new tools)",
     "Best for non-trivial logic: the LLM writes the Python from your "
     "description above. Validation runs before you advance."),
    ("file_reader", "File reader",
     "Reads a file from an AIDP volume. Inputs: volume + path."),
    ("sql",         "SQL runner",
     "Runs SQL via dynamic_query against the catalog you picked in step 4."),
    ("rest",        "REST caller",
     "Calls a REST endpoint with bearer / API-key auth + JSON body."),
    ("rag",         "RAG retriever",
     "Queries the knowledge base you picked in step 4."),
    ("echo_stub",   "Echo / stub",
     "Minimal scaffold that echoes its inputs — useful for smoke tests."),
]


class ToolKindScreen(WizardScreen):
    TITLE = "Step 5 — Describe the tool, pick a template"
    SUBTITLE = ("Tell the wizard what the tool should do — this becomes the "
                "LLM's primary input in step 6. Then pick the template that "
                "best fits.")

    def compose_body(self) -> ComposeResult:
        with Vertical():
            yield Static("What should this tool do?", classes="field-label")
            yield Static(
                "2–5 sentences. Be concrete: inputs, what it returns, any APIs "
                "or data sources it touches. Example: 'Reads safety manuals "
                "from the docs volume, extracts the section about lockout/"
                "tagout, and returns the matching paragraphs.'",
                classes="field-hint",
            )
            yield TextArea(text=self.state.tool_description or "", id="description")

            yield Static("Template", classes="field-label")
            yield Static(
                "Pick LLM-generated unless one of the canned templates is an "
                "exact fit. The description above drives the generation.",
                classes="field-hint",
            )
            with RadioSet(id="kind"):
                # Default = llm_custom (first in TOOL_KINDS).
                current = self.state.tool_kind or "llm_custom"
                for kid, label, _desc in TOOL_KINDS:
                    yield RadioButton(label, value=(current == kid),
                                      id=f"rb-{kid}")
            yield Static(self._desc_for(self.state.tool_kind or "llm_custom"),
                         id="kind-desc", classes="field-hint")

            yield Static("Class name (PascalCase, no spaces)", classes="field-label")
            yield Input(value=self.state.tool_class_name or "MyCustomTool",
                        id="class-name", placeholder="MyCustomTool")

            yield Static("Display name (shown in the AIDP UI)", classes="field-label")
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

        desc = self.query_one("#description", TextArea).text.strip()
        if not desc:
            status.update("[#f85149]Tell the wizard what the tool does first.[/]")
            return False
        self.state.tool_description = desc
        # Pre-seed the step-6 LLM intent from this description so the user
        # doesn't have to re-type it. They can still edit on step 6.
        if not self.state.user_intent:
            self.state.user_intent = desc

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
