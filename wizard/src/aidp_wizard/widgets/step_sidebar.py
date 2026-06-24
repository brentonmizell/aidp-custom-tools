"""Left-rail step sidebar.

Renders the wizard's step list with one of three visual states per row:
  -active   currently on this step  (orange accent, left border, bold)
  -done     completed earlier       (green checkmark)
  -pending  not yet reached         (muted gray, hollow circle)
"""

from __future__ import annotations

from typing import List

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Static

from ..state import STEPS


class StepSidebar(Widget):
    """Wizard progress sidebar. Updates when current_step / completed_steps change."""

    DEFAULT_ID = "step-sidebar"

    def __init__(self, current_step: int, completed_steps: List[int]):
        super().__init__()
        self.current_step = current_step
        self.completed_steps = completed_steps

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("AIDP Tool Wizard", classes="step-title")
            for idx, step in enumerate(STEPS):
                yield Static(self._row_label(idx, step), classes=self._row_class(idx))

    def _row_label(self, idx: int, step: dict) -> str:
        n = idx + 1
        if idx in self.completed_steps:
            marker = "[green]✓[/]"
        elif idx == self.current_step:
            marker = "[#ff6b35]▶[/]"
        else:
            marker = "[#6e7681]○[/]"
        return f"{marker} {n}. {step['title']}"

    def _row_class(self, idx: int) -> str:
        classes = ["step-row"]
        if idx == self.current_step:
            classes.append("-active")
        elif idx in self.completed_steps:
            classes.append("-done")
        else:
            classes.append("-pending")
        return " ".join(classes)

    def refresh_state(self, current_step: int, completed_steps: List[int]) -> None:
        """Re-render the sidebar after step state changes. Called by the App
        whenever a screen advances or moves back."""
        self.current_step = current_step
        self.completed_steps = completed_steps
        # Replace children with fresh rows.
        self.query("Static").remove()
        with self.app.batch_update():
            self.mount(Static("AIDP Tool Wizard", classes="step-title"))
            for idx, step in enumerate(STEPS):
                self.mount(Static(self._row_label(idx, step), classes=self._row_class(idx)))
