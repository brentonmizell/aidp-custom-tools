"""AIDP Tool Wizard — main Textual application.

Orchestrates the 7-step flow: each Step is a Screen, the App swaps the
current screen on Next/Back while keeping the StepSidebar in sync.

Run with:  aidp-wizard
"""

from __future__ import annotations

import sys
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Header

from .state import STEPS, WizardState
from .widgets.step_sidebar import StepSidebar


class WizardApp(App):
    """Top-level wizard application."""

    CSS_PATH = "theme.tcss"
    TITLE = "AIDP Tool Wizard"
    SUB_TITLE = "Build a Custom Code tool end-to-end"

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+s", "save_state", "Save"),
    ]

    def __init__(self):
        super().__init__()
        self.state = WizardState.load()
        self._sidebar: StepSidebar | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="wizard-body"):
            self._sidebar = StepSidebar(self.state.current_step, self.state.completed_steps)
            yield self._sidebar
            # Content area starts empty; mounted on push_step below.
        yield Footer()

    def on_mount(self) -> None:
        self.push_step(self.state.current_step)

    # --- Step navigation ---

    def push_step(self, idx: int) -> None:
        """Swap the current screen for step `idx` and refresh the sidebar."""
        if idx < 0 or idx >= len(STEPS):
            return
        screen_cls_name = STEPS[idx]["screen"]
        # Lazy import to keep app startup fast.
        from . import screens
        screen_cls = getattr(screens, screen_cls_name)
        self.state.current_step = idx
        self.state.save()
        if self._sidebar:
            self._sidebar.refresh_state(idx, self.state.completed_steps)
        self.push_screen(screen_cls(self.state))

    def advance(self) -> None:
        """Mark current step complete and advance to the next. Called by
        screens when their Next button validates."""
        self.state.mark_step_done(self.state.current_step)
        self.pop_screen()
        next_idx = self.state.current_step + 1
        if next_idx < len(STEPS):
            self.push_step(next_idx)
        else:
            # Reached the end — exit cleanly.
            self.exit(0)

    def go_back(self) -> None:
        """Move to the previous step."""
        prev_idx = self.state.current_step - 1
        if prev_idx >= 0:
            self.pop_screen()
            self.push_step(prev_idx)

    # --- Bindings ---

    def action_save_state(self) -> None:
        self.state.save()
        self.notify("State saved.", title="Wizard", severity="information")

    def action_quit(self) -> None:
        self.state.save()
        self.exit(0)


def main() -> int:
    """Console entry point — invoked by `aidp-wizard`."""
    try:
        WizardApp().run()
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
