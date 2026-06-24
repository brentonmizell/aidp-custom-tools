"""Step 4 — Resource picker.

Lets the user pick catalog / schema / volume / KB by name (live
dropdowns), so the resulting tool's tool_config.json comes prefilled
with real values — no hand-typed paths, no 404s.

Catalogs are grouped by type (STANDARD vs EXTERNAL) so the developer
sees at a glance which catalogs are Oracle-managed vs federated.
Volumes + KBs are scoped to the picked catalog/schema and re-load
whenever those change.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.widgets import Static, Select, Button

from ._base import WizardScreen
from .. import aidp_client


class ResourcesScreen(WizardScreen):
    TITLE = "Step 4 — Pick catalog / schema / volume / KB"
    SUBTITLE = ("All four are optional, but every one you pick becomes a "
                "prefilled dropdown in the generated tool's Test panel.")

    def __init__(self, state):
        super().__init__(state)
        self._catalogs: list = []
        self._schemas: list = []
        self._volumes: list = []
        self._kbs: list = []

    def compose_body(self) -> ComposeResult:
        with Vertical():
            yield Static("Catalog", classes="field-label")
            yield Static("Grouped by type — STANDARD catalogs are Oracle-managed; "
                         "EXTERNAL catalogs federate Iceberg / Unity / Glue / etc.",
                         classes="field-hint")
            yield Select([("(none — skip catalog)", "")], id="catalog-select",
                         allow_blank=False)
            yield Static("", id="catalog-summary", classes="status-dim")

            yield Static("Schema", classes="field-label")
            yield Static("Populated after you pick a catalog.", classes="field-hint")
            yield Select([("(pick a catalog first)", "")], id="schema-select",
                         allow_blank=False, disabled=True)

            yield Static("Volume", classes="field-label")
            yield Static("Object-storage volume for file-reader / writer tools. "
                         "Reloads when you change schema.", classes="field-hint")
            yield Select([("(pick a schema first)", "")], id="volume-select",
                         allow_blank=False, disabled=True)

            yield Static("Knowledge base", classes="field-label")
            yield Static("Lists every KB visible in the data lake — scope by "
                         "catalog/schema first if you have many.",
                         classes="field-hint")
            yield Select([("(loading KBs...)", "")], id="kb-select",
                         allow_blank=False)

            with Horizontal(id="reload-row"):
                yield Button("Reload everything", id="reload", classes="-secondary")

            yield Static("", id="res-status", classes="status-dim")

    def on_mount(self) -> None:
        self._load_catalogs()
        self._load_kbs()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "reload":
            self._load_catalogs()
            self._refresh_schemas()
            self._refresh_volumes()
            self._load_kbs()
            return
        super().on_button_pressed(event)

    # ----- loaders -----

    def _load_catalogs(self) -> None:
        status = self.query_one("#res-status", Static)
        status.update("[#58a6ff]Loading catalogs...[/]")
        self.app.refresh()
        self._catalogs, err = aidp_client.list_catalogs(self.state)
        catalogs = self._catalogs or []

        standard = [c for c in catalogs if (c.get("catalogType") or "").upper() == "STANDARD"]
        external = [c for c in catalogs if (c.get("catalogType") or "").upper() == "EXTERNAL"]
        unknown  = [c for c in catalogs
                    if (c.get("catalogType") or "").upper() not in ("STANDARD", "EXTERNAL")]

        opts = [("(none — skip catalog)", "")]
        if standard:
            opts.append((f"─── STANDARD ({len(standard)}) ───", "__hdr_std__"))
            for c in sorted(standard, key=lambda x: (x.get("displayName") or "").lower()):
                opts.append((f"  ✓ {c.get('displayName', c.get('key', '?'))}", c.get("key", "")))
        if external:
            opts.append((f"─── EXTERNAL ({len(external)}) ───", "__hdr_ext__"))
            for c in sorted(external, key=lambda x: (x.get("displayName") or "").lower()):
                opts.append((f"  ↪ {c.get('displayName', c.get('key', '?'))}", c.get("key", "")))
        if unknown:
            opts.append((f"─── OTHER ({len(unknown)}) ───", "__hdr_oth__"))
            for c in unknown:
                opts.append((f"  · {c.get('displayName', c.get('key', '?'))}", c.get("key", "")))

        sel = self.query_one("#catalog-select", Select)
        sel.set_options(opts)
        if self.state.catalog_key:
            try:
                sel.value = self.state.catalog_key
            except Exception:
                pass

        summary = (f"[#3fb950]STANDARD: {len(standard)}[/]   "
                   f"[#d29922]EXTERNAL: {len(external)}[/]")
        if unknown:
            summary += f"   [#6e7681]other: {len(unknown)}[/]"
        self.query_one("#catalog-summary", Static).update(summary)

        if err:
            status.update(f"[#f85149]list_catalogs error: {err}[/]")
        else:
            status.update(f"[#3fb950]✓ {len(catalogs)} catalogs loaded.[/]")

    def _refresh_schemas(self) -> None:
        sel = self.query_one("#schema-select", Select)
        if not self.state.catalog_key:
            sel.set_options([("(pick a catalog first)", "")])
            sel.disabled = True
            return
        sel.disabled = False
        self._schemas, err = aidp_client.list_schemas(self.state)
        opts = [("(none)", "")] + [
            (s.get("displayName", s.get("key", "?")), s.get("key", ""))
            for s in (self._schemas or [])
        ]
        sel.set_options(opts)
        if self.state.schema_key:
            try:
                sel.value = self.state.schema_key
            except Exception:
                pass
        if err:
            self.query_one("#res-status", Static).update(
                f"[#d29922]list_schemas error: {err}[/]"
            )
        elif not self._schemas:
            self.query_one("#res-status", Static).update(
                "[#d29922]No schemas visible in this catalog.[/]"
            )

    def _refresh_volumes(self) -> None:
        sel = self.query_one("#volume-select", Select)
        if not self.state.catalog_key or not self.state.schema_key:
            sel.set_options([("(pick a schema first)", "")])
            sel.disabled = True
            return
        sel.disabled = False
        self._volumes, err = aidp_client.list_volumes(self.state)
        opts = [("(none)", "")] + [
            (v.get("displayName", v.get("key", "?")), v.get("key", ""))
            for v in (self._volumes or [])
        ]
        sel.set_options(opts)
        if self.state.volume_key:
            try:
                sel.value = self.state.volume_key
            except Exception:
                pass
        if err:
            self.query_one("#res-status", Static).update(
                f"[#d29922]list_volumes error: {err}[/]"
            )
        elif not self._volumes:
            self.query_one("#res-status", Static).update(
                "[#d29922]No volumes in this schema (file tools won't have a default).[/]"
            )

    def _load_kbs(self) -> None:
        sel = self.query_one("#kb-select", Select)
        self._kbs, err = aidp_client.list_kbs(self.state)
        opts = [("(none — skip KB)", "")] + [
            (kb.get("displayName", kb.get("key", "?")), kb.get("key", ""))
            for kb in (self._kbs or [])
        ]
        sel.set_options(opts)
        if self.state.kb_key:
            try:
                sel.value = self.state.kb_key
            except Exception:
                pass
        if err:
            self.query_one("#res-status", Static).update(
                f"[#d29922]list_kbs error: {err}[/]"
            )

    # ----- events -----

    def on_select_changed(self, event: Select.Changed) -> None:
        sid = event.select.id
        val = event.value
        # Reject the visual separator rows we injected.
        if isinstance(val, str) and val.startswith("__hdr_"):
            event.select.value = self.state.catalog_key or ""
            return

        if sid == "catalog-select":
            self.state.catalog_key = val or ""
            cat = next((c for c in (self._catalogs or []) if c.get("key") == val), None)
            self.state.catalog_name = cat.get("displayName", "") if cat else ""
            self.state.schema_key = ""
            self.state.schema_name = ""
            self.state.volume_key = ""
            self.state.volume_name = ""
            self._refresh_schemas()
            self._refresh_volumes()
            self._load_kbs()
        elif sid == "schema-select":
            self.state.schema_key = val or ""
            sch = next((s for s in (self._schemas or []) if s.get("key") == val), None)
            self.state.schema_name = sch.get("displayName", "") if sch else ""
            self.state.volume_key = ""
            self.state.volume_name = ""
            self._refresh_volumes()
            self._load_kbs()
        elif sid == "volume-select":
            self.state.volume_key = val or ""
            vol = next((v for v in (self._volumes or []) if v.get("key") == val), None)
            self.state.volume_name = vol.get("displayName", "") if vol else ""
        elif sid == "kb-select":
            self.state.kb_key = val or ""
            kb = next((k for k in (self._kbs or []) if k.get("key") == val), None)
            self.state.kb_name = kb.get("displayName", "") if kb else ""
        self.state.save()

    def on_next(self) -> bool:
        self.state.save()
        return True
