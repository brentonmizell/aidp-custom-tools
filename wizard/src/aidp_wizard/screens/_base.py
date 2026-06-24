"""Shared base for every wizard step screen.

Each step gets a consistent layout:
  - title + subtitle
  - content area (overridden by subclass)
  - footer button row (Back / Next / Cancel) with keyboard hints

Subclasses override:
  TITLE          one-line page title
  SUBTITLE       one-line page subtitle
  compose_body() yields the step's main widgets
  on_next()      called when Next is pressed; return True to advance
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.screen import Screen
from textual.widgets import Button, Static


class WizardScreen(Screen):
    """Base class. Subclass + override TITLE / SUBTITLE / compose_body() / on_next()."""

    TITLE: str = ""
    SUBTITLE: str = ""

    def __init__(self, state):
        super().__init__()
        self.state = state

    def compose(self) -> ComposeResult:
        with Vertical(id="wizard-content"):
            yield Static(f"[bold]{self.TITLE}[/]", classes="screen-title")
            if self.SUBTITLE:
                yield Static(self.SUBTITLE, classes="screen-subtitle")
            yield from self.compose_body()
            with Horizontal(id="button-row"):
                yield Button("← Back", id="back", classes="-secondary")
                yield Button("Cancel", id="cancel", classes="-danger")
                yield Button("Next →", id="next", classes="-primary")

    def compose_body(self) -> ComposeResult:
        """Override in subclass to add the step-specific widgets."""
        yield Static("(no content)", classes="status-dim")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.app.go_back()
        elif event.button.id == "cancel":
            self.app.state.save()
            self.app.exit(0)
        elif event.button.id == "next":
            if self.on_next():
                self.app.advance()

    def on_next(self) -> bool:
        """Override to validate the step's input. Return True to advance."""
        return True

    def status(self, msg: str, level: str = "info") -> None:
        """Convenience: surface a Textual notification at the configured severity."""
        sev = {"info": "information", "warn": "warning", "error": "error"}.get(level, "information")
        self.app.notify(msg, severity=sev)
