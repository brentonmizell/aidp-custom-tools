"""Step 2 — AIDP auth.

Detects OCI profiles in ~/.oci/config, shows them with kind +
expiry, lets the user pick. Also collects region + Data Lake OCID
(prefilled from ~/.aidp/aidp-deploy.config.json if present).
Validates by calling list_catalogs as a smoke test before continuing.
"""

from __future__ import annotations

from pathlib import Path
import json

from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from ._base import WizardScreen
from .. import aidp_client


class AuthScreen(WizardScreen):
    TITLE = "Step 2 — AIDP authentication"
    SUBTITLE = ("Pick the OCI profile the wizard uses to call AIDP. Session "
                "tokens (from `oci session authenticate`) are preferred over "
                "permanent API keys.")

    def __init__(self, state):
        super().__init__(state)
        self._profiles = aidp_client.list_oci_profiles()

    def compose_body(self) -> ComposeResult:
        with Vertical():
            yield Static("OCI profile", classes="field-label")
            if not self._profiles:
                yield Static("[#f85149]No ~/.oci/config found. Run `oci setup config` or "
                             "`oci session authenticate` first.[/]", classes="status-error")
            else:
                yield Static("Detected from ~/.oci/config:", classes="field-hint")
                opts = []
                for p in self._profiles:
                    label = self._format_profile(p)
                    opts.append(Option(label, id=p["name"]))
                yield OptionList(*opts, id="profile-list")

            yield Static("Region", classes="field-label")
            yield Input(value=self.state.region or self._guess_region(),
                        id="region", placeholder="us-ashburn-1")

            yield Static("Data Lake OCID", classes="field-label")
            yield Static("Auto-detected from ~/.aidp/aidp-deploy.config.json if present.",
                         classes="field-hint")
            yield Input(value=self.state.data_lake_ocid or self._detect_lake_ocid(),
                        id="lake-ocid",
                        placeholder="ocid1.aidataplatform.oc1.iad.aaaaaaaa...")

            yield Static("", id="auth-status", classes="status-dim")

    def _format_profile(self, p: dict) -> str:
        kind = "session" if p["kind"] == "session_token" else "API key"
        suffix = ""
        if p["kind"] == "session_token" and p["expires_in_min"] is not None:
            m = p["expires_in_min"]
            if m < 0:
                suffix = f"  [#f85149]expired ({abs(m)} min ago)[/]"
            elif m < 10:
                suffix = f"  [#d29922]{m} min remaining[/]"
            else:
                suffix = f"  [#3fb950]{m} min remaining[/]"
        return f"[bold]{p['name']}[/]  —  {kind}  —  region {p['region'] or '(none)'}{suffix}"

    def _guess_region(self) -> str:
        for p in self._profiles:
            if p.get("region"):
                return p["region"]
        return "us-ashburn-1"

    def _detect_lake_ocid(self) -> str:
        p = Path.home() / ".aidp" / "aidp-deploy.config.json"
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8")).get("dataLakeOcid", "")
            except Exception:
                pass
        return ""

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "profile-list":
            return
        prof_name = event.option.id or "DEFAULT"
        self.state.oci_profile = prof_name
        # If session profile, prefer session_token mode; else auto.
        prof = next((p for p in self._profiles if p["name"] == prof_name), None)
        if prof and prof["kind"] == "session_token":
            self.state.oci_auth_mode = "session_token"
        else:
            self.state.oci_auth_mode = "auto"
        self.state.save()
        self.query_one("#auth-status", Static).update(
            f"[#58a6ff]Profile selected: {prof_name} ({self.state.oci_auth_mode})[/]"
        )

    def on_next(self) -> bool:
        # Capture region + lake OCID.
        self.state.region = self.query_one("#region", Input).value.strip()
        self.state.data_lake_ocid = self.query_one("#lake-ocid", Input).value.strip()

        status = self.query_one("#auth-status", Static)
        if not self.state.region:
            status.update("[#f85149]Region is required.[/]")
            return False
        if not self.state.data_lake_ocid:
            status.update("[#f85149]Data Lake OCID is required.[/]")
            return False
        if not self.state.oci_profile:
            status.update("[#f85149]Pick an OCI profile from the list.[/]")
            return False

        status.update("[#58a6ff]Calling list_catalogs to validate auth + region + Data Lake...[/]")
        self.app.refresh()

        ok, msg = aidp_client.validate_auth(self.state)
        if not ok:
            status.update(f"[#f85149]✗ {msg}[/]")
            return False

        self.state.auth_validated = True
        self.state.save()
        status.update(f"[#3fb950]✓ {msg}[/]")
        self.status(msg, "info")
        return True
