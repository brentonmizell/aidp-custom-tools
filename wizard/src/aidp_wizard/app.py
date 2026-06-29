"""AIDP Tool Wizard — main Textual application.

Orchestrates the 8-step flow: each Step is a Screen, the App swaps the
current screen on Next/Back while keeping the StepSidebar in sync.
Errors during step transitions surface as Textual notifications and
are appended to ~/.aidp/wizard.log so silent crashes can be diagnosed.

Run with:  aidp-wizard
"""

from __future__ import annotations

import logging
import sys
import traceback
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Header

from .state import STEPS, WizardState
from .widgets.step_sidebar import StepSidebar


LOG_PATH = Path.home() / ".aidp" / "wizard.log"


def _setup_logging() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("aidp_wizard")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


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
        self.log_file = _setup_logging()
        self.log_file.info("=== wizard launched ===")

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="wizard-body"):
            self._sidebar = StepSidebar(self.state.current_step, self.state.completed_steps)
            yield self._sidebar
            # Content area starts empty; mounted on push_step below.
        yield Footer()

    def on_mount(self) -> None:
        # If the saved current_step is past the new step count, clamp it.
        # Also, if the persisted state was created before the Describe step
        # existed, force the user back to it so we capture the intent.
        if self.state.current_step >= len(STEPS):
            self.state.current_step = max(0, len(STEPS) - 1)
        elif (self.state.current_step >= 3
              and not (self.state.user_intent or self.state.tool_description)):
            self.log_file.info("user advanced past describe step with empty "
                               "intent — rewinding to describe step")
            self.state.current_step = 2  # describe
        self.push_step(self.state.current_step)

    # --- Step navigation ---

    def push_step(self, idx: int) -> None:
        """Swap the current screen for step `idx` and refresh the sidebar.
        Any exception during compose/mount is logged + surfaced as a toast
        so the user sees what went wrong instead of a silent crash."""
        if idx < 0 or idx >= len(STEPS):
            self.log_file.warning(f"push_step({idx}) out of range; ignored")
            return
        screen_cls_name = STEPS[idx]["screen"]
        from . import screens
        screen_cls = getattr(screens, screen_cls_name)
        self.state.current_step = idx
        self.state.save()
        if self._sidebar:
            self._sidebar.refresh_state(idx, self.state.completed_steps)
        try:
            self.push_screen(screen_cls(self.state))
            self.log_file.info(f"pushed step {idx} ({screen_cls_name})")
        except Exception as exc:
            tb = traceback.format_exc()
            self.log_file.error(f"FAILED to push step {idx} ({screen_cls_name}): {exc}\n{tb}")
            self.notify(
                f"Step {idx} ({screen_cls_name}) crashed: {exc}\n"
                f"Full traceback in {LOG_PATH}",
                title="Wizard error", severity="error", timeout=20,
            )

    def advance(self) -> None:
        """Mark current step complete and advance to the next. Called by
        screens when their Next button validates."""
        try:
            self.state.mark_step_done(self.state.current_step)
            self.pop_screen()
            next_idx = self.state.current_step + 1
            if next_idx < len(STEPS):
                self.push_step(next_idx)
            else:
                self.exit(0)
        except Exception as exc:
            self.log_file.error(f"advance() crashed: {exc}\n{traceback.format_exc()}")
            self.notify(f"advance crashed: {exc} — see {LOG_PATH}",
                        title="Wizard error", severity="error", timeout=20)

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
