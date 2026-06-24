"""Step 3 — Workspace picker.

Lists live workspaces from the authenticated tenancy. Single-select.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from ._base import WizardScreen
from .. import aidp_client


class WorkspaceScreen(WizardScreen):
    TITLE = "Step 3 — Pick the AIDP workspace"
    SUBTITLE = "The tool will be configured against resources in this workspace."

    def __init__(self, state):
        super().__init__(state)
        self._workspaces, self._err = aidp_client.list_workspaces(state)

    def compose_body(self) -> ComposeResult:
        with Vertical():
            if self._err:
                yield Static(f"[#f85149]Could not list workspaces:[/]\n{self._err}",
                             classes="status-error")
                return
            if not self._workspaces:
                yield Static("[#d29922]No workspaces found in this Data Lake.[/]",
                             classes="status-warn")
                return
            yield Static(f"Found {len(self._workspaces)} workspaces.", classes="field-hint")
            opts = []
            for ws in self._workspaces:
                key = ws.get("key", "")
                name = ws.get("displayName", "(unnamed)")
                state_str = ws.get("lifecycleState", "")
                color = "#3fb950" if state_str == "ACTIVE" else "#d29922"
                label = f"[bold]{name}[/]  —  [{color}]{state_str}[/]  —  key {key[:8]}…"
                opts.append(Option(label, id=key))
            yield OptionList(*opts, id="workspace-list")
            yield Static("", id="ws-status", classes="status-dim")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "workspace-list":
            return
        key = event.option.id or ""
        ws = next((w for w in (self._workspaces or []) if w.get("key") == key), None)
        if not ws:
            return
        self.state.workspace_key = key
        self.state.workspace_name = ws.get("displayName", "")
        self.state.save()
        self.query_one("#ws-status", Static).update(
            f"[#58a6ff]Selected: {self.state.workspace_name}[/]"
        )

    def on_next(self) -> bool:
        if not self.state.workspace_key:
            self.query_one("#ws-status", Static).update("[#f85149]Pick a workspace.[/]")
            return False
        return True
