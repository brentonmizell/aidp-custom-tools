"""Step 2 — AIDP auth.

Detects OCI profiles in ~/.oci/config, shows them with kind +
expiry, lets the user pick. If no profile exists (or the user wants
a fresh one) the screen can:

  - shell out to `oci session authenticate` (browser flow) and add
    the new profile to ~/.oci/config — this is the recommended path
    because it produces a short-lived (~1h) JWT, no permanent PEM.

  - accept a permanent API-key profile by typing tenancy / user /
    fingerprint / key-file / region into the inline form — fallback
    for users who already have a key pair set up.

After either path, the profile list is reloaded so the user can pick
the new entry. Also collects region + Data Lake OCID (prefilled from
~/.aidp/aidp-deploy.config.json if present). Validates by calling
list_catalogs as a smoke test before continuing.
"""

from __future__ import annotations

from pathlib import Path
import json

from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.widgets import Input, OptionList, Static, Button
from textual.widgets.option_list import Option

from ._base import WizardScreen
from .. import aidp_client


class AuthScreen(WizardScreen):
    TITLE = "Step 2 — AIDP authentication"
    SUBTITLE = ("Pick the OCI profile the wizard uses to call AIDP. No profile? "
                "Create one inline with the browser flow, or type an existing "
                "API key into the form below.")

    def __init__(self, state):
        super().__init__(state)
        self._profiles = aidp_client.list_oci_profiles()
        self._show_api_form = False

    # ----- compose -----

    def compose_body(self) -> ComposeResult:
        with Vertical():
            yield Static("OCI profile", classes="field-label")
            yield Static(self._profile_hint(), classes="field-hint", id="profile-hint")
            yield OptionList(*self._profile_options(), id="profile-list")

            yield Static("Region", classes="field-label")
            yield Input(value=self.state.region or self._guess_region(),
                        id="region", placeholder="us-ashburn-1")

            yield Static("Data Lake OCID", classes="field-label")
            yield Static("Auto-detected from ~/.aidp/aidp-deploy.config.json if present.",
                         classes="field-hint")
            yield Input(value=self.state.data_lake_ocid or self._detect_lake_ocid(),
                        id="lake-ocid",
                        placeholder="ocid1.aidataplatform.oc1.iad.aaaaaaaa...")

            yield Static("Add or refresh a profile", classes="field-label")
            with Horizontal(id="auth-actions-row"):
                yield Button("🔑 Create OCI session (browser)", id="create-session",
                             classes="-primary")
                yield Button("Add API-key profile", id="toggle-api-form",
                             classes="-secondary")
                yield Button("Reload profile list", id="reload-profiles",
                             classes="-secondary")

            # Inline API-key entry — hidden until "Add API-key profile" is clicked.
            with Vertical(id="api-key-form"):
                yield Static("[bold]Add a permanent API-key profile[/]",
                             classes="field-label")
                yield Static(
                    "Writes to ~/.oci/config. Reuses an existing PEM file you "
                    "already have on disk. Prefer the session button above; this "
                    "is here for users who already manage long-lived keys.",
                    classes="field-hint",
                )
                yield Input(value="AIDP_KEY", id="api-profile-name",
                            placeholder="Profile name (e.g. AIDP_KEY)")
                yield Input(value="", id="api-tenancy",
                            placeholder="tenancy OCID  (ocid1.tenancy.oc1..…)")
                yield Input(value="", id="api-user",
                            placeholder="user OCID  (ocid1.user.oc1..…)")
                yield Input(value="", id="api-fingerprint",
                            placeholder="key fingerprint  (aa:bb:cc:…)")
                yield Input(value="", id="api-keyfile",
                            placeholder="path to PEM file  (~/.oci/oci_api_key.pem)")
                yield Input(value="", id="api-region",
                            placeholder="region  (us-ashburn-1)")
                yield Button("Save profile", id="save-api-profile",
                             classes="-primary")

            yield Static("", id="auth-status", classes="status-dim")

    # ----- helpers -----

    def _profile_options(self):
        if not self._profiles:
            return [Option("[#d29922]No ~/.oci/config profiles found — use the buttons below.[/]",
                           id="__none__", disabled=True)]
        return [Option(self._format_profile(p), id=p["name"]) for p in self._profiles]

    def _profile_hint(self) -> str:
        if not self._profiles:
            return ("[#d29922]No profiles detected.[/] Click [bold]Create OCI session[/] "
                    "below to create one via the browser flow, or expand the API-key form.")
        return f"Detected {len(self._profiles)} from ~/.oci/config:"

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

    def on_mount(self) -> None:
        # Hide the API-key form until the user expands it.
        self.query_one("#api-key-form", Vertical).display = False

    # ----- events -----

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "profile-list":
            return
        prof_name = event.option.id or "DEFAULT"
        if prof_name == "__none__":
            return
        self.state.oci_profile = prof_name
        prof = next((p for p in self._profiles if p["name"] == prof_name), None)
        if prof and prof["kind"] == "session_token":
            self.state.oci_auth_mode = "session_token"
        else:
            self.state.oci_auth_mode = "auto"
        self.state.save()
        self.query_one("#auth-status", Static).update(
            f"[#58a6ff]Profile selected: {prof_name} ({self.state.oci_auth_mode})[/]"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "create-session":
            self._do_create_session()
            event.stop(); return
        if bid == "toggle-api-form":
            self._show_api_form = not self._show_api_form
            self.query_one("#api-key-form", Vertical).display = self._show_api_form
            event.stop(); return
        if bid == "reload-profiles":
            self._reload_profiles("[#58a6ff]Reloaded.[/]")
            event.stop(); return
        if bid == "save-api-profile":
            self._do_save_api_profile()
            event.stop(); return
        super().on_button_pressed(event)

    # ----- actions -----

    def _do_create_session(self) -> None:
        status = self.query_one("#auth-status", Static)
        if not aidp_client.oci_cli_installed():
            status.update("[#f85149]`oci` CLI not on PATH. Install: "
                          "[bold]pip install oci-cli[/].[/]")
            return

        region = self.query_one("#region", Input).value.strip() or "us-ashburn-1"
        # Suggest a name that doesn't collide with existing profiles.
        existing = {p["name"] for p in self._profiles}
        base = "AIDP_SESSION"
        name = base
        n = 2
        while name in existing:
            name = f"{base}_{n}"; n += 1

        argv = aidp_client.build_session_auth_argv(region, name)
        status.update(f"[#58a6ff]Launching browser flow: [bold]{' '.join(argv)}[/]\n"
                      f"(terminal will hand off to the OCI CLI — wait for the browser)[/]")
        self.app.refresh()

        import subprocess
        try:
            with self.app.suspend():
                print()
                print(f"=== oci session authenticate (profile={name}, region={region}) ===")
                print("A browser window should open. Sign in to OCI to mint the token.")
                print()
                result = subprocess.run(argv, check=False)
        except Exception as exc:
            status.update(f"[#f85149]Subprocess failed: {exc}[/]")
            return

        if result.returncode != 0:
            status.update(f"[#f85149]oci session authenticate exited "
                          f"with code {result.returncode}.[/]")
            return

        # Adopt the new profile.
        self.state.oci_profile = name
        self.state.oci_auth_mode = "session_token"
        self.state.region = region
        self.state.save()
        self._reload_profiles(f"[#3fb950]✓ Session created — profile [bold]{name}[/] "
                              f"is now active.[/]")

    def _do_save_api_profile(self) -> None:
        status = self.query_one("#auth-status", Static)
        name = self.query_one("#api-profile-name", Input).value.strip()
        tenancy = self.query_one("#api-tenancy", Input).value.strip()
        user = self.query_one("#api-user", Input).value.strip()
        fingerprint = self.query_one("#api-fingerprint", Input).value.strip()
        keyfile = self.query_one("#api-keyfile", Input).value.strip()
        region = (self.query_one("#api-region", Input).value.strip()
                  or self.query_one("#region", Input).value.strip())

        ok, msg = aidp_client.write_api_key_profile(
            name, tenancy_ocid=tenancy, user_ocid=user,
            fingerprint=fingerprint, key_file=keyfile, region=region,
        )
        if not ok:
            status.update(f"[#f85149]{msg}[/]")
            return

        self.state.oci_profile = name
        self.state.oci_auth_mode = "user_principal"
        self.state.region = region
        self.state.save()
        self._reload_profiles(f"[#3fb950]✓ {msg} Selected [bold]{name}[/].[/]")
        # Collapse the form again.
        self._show_api_form = False
        self.query_one("#api-key-form", Vertical).display = False

    def _reload_profiles(self, status_msg: str) -> None:
        self._profiles = aidp_client.list_oci_profiles()
        opt_list = self.query_one("#profile-list", OptionList)
        opt_list.clear_options()
        for opt in self._profile_options():
            opt_list.add_option(opt)
        self.query_one("#profile-hint", Static).update(self._profile_hint())
        self.query_one("#auth-status", Static).update(status_msg)

    # ----- footer Next -----

    def on_next(self) -> bool:
        self.state.region = self.query_one("#region", Input).value.strip()
        self.state.data_lake_ocid = self.query_one("#lake-ocid", Input).value.strip()
        status = self.query_one("#auth-status", Static)

        if not self.state.region:
            status.update("[#f85149]Region is required.[/]"); return False
        if not self.state.data_lake_ocid:
            status.update("[#f85149]Data Lake OCID is required.[/]"); return False
        if not self.state.oci_profile:
            status.update("[#f85149]Pick an OCI profile from the list above.[/]"); return False

        status.update("[#58a6ff]Calling list_catalogs to validate auth + region + Data Lake...[/]")
        self.app.refresh()

        ok, msg = aidp_client.validate_auth(self.state)
        if not ok:
            status.update(f"[#f85149]✗ {msg}[/]"); return False

        self.state.auth_validated = True
        self.state.save()
        status.update(f"[#3fb950]✓ {msg}[/]")
        self.status(msg, "info")
        return True
