"""Step 4 — Resource picker.

Lets the user pick catalog / schema / volume / KB by name (live
dropdowns), so the resulting tool's tool_config.json comes prefilled
with real values — no hand-typed paths, no 404s.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.widgets import OptionList, Static, Select, Button
from textual.widgets.option_list import Option

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
        self._err = None

    def compose_body(self) -> ComposeResult:
        with Vertical():
            yield Static("Catalog", classes="field-label")
            yield Static("Lists STANDARD + EXTERNAL catalogs visible in this workspace.",
                         classes="field-hint")
            yield Select([("(none)", "")], id="catalog-select", allow_blank=False)

            yield Static("Schema", classes="field-label")
            yield Static("Populated after you pick a catalog.", classes="field-hint")
            yield Select([("(pick a catalog first)", "")], id="schema-select",
                         allow_blank=False, disabled=True)

            yield Static("Volume", classes="field-label")
            yield Static("Pick if your tool reads/writes object-storage files.",
                         classes="field-hint")
            yield Select([("(none)", "")], id="volume-select", allow_blank=False)

            yield Static("Knowledge base", classes="field-label")
            yield Static("Pick if your tool is a RAG tool.", classes="field-hint")
            yield Select([("(none)", "")], id="kb-select", allow_blank=False)

            with Horizontal(id="reload-row"):
                yield Button("Reload resources", id="reload", classes="-secondary")

            yield Static("", id="res-status", classes="status-dim")

    def on_mount(self) -> None:
        self._populate()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "reload":
            self._populate(force=True)
            return
        super().on_button_pressed(event)

    def _populate(self, force: bool = False) -> None:
        status = self.query_one("#res-status", Static)
        status.update("[#58a6ff]Loading catalogs / volumes / KBs...[/]")
        self.app.refresh()

        self._catalogs, c_err = aidp_client.list_catalogs(self.state)
        self._volumes, v_err = aidp_client.list_volumes(self.state)
        self._kbs, k_err = aidp_client.list_kbs(self.state)

        cat_opts = [("(none)", "")] + [
            (f"{c.get('displayName', '?')}  [{c.get('catalogType', 'STANDARD')}]", c.get("key", ""))
            for c in (self._catalogs or [])
        ]
        self.query_one("#catalog-select", Select).set_options(cat_opts)
        if self.state.catalog_key:
            try:
                self.query_one("#catalog-select", Select).value = self.state.catalog_key
            except Exception:
                pass

        vol_opts = [("(none)", "")] + [
            (v.get("displayName", v.get("key", "?")), v.get("key", ""))
            for v in (self._volumes or [])
        ]
        self.query_one("#volume-select", Select).set_options(vol_opts)
        if self.state.volume_key:
            try:
                self.query_one("#volume-select", Select).value = self.state.volume_key
            except Exception:
                pass

        kb_opts = [("(none)", "")] + [
            (kb.get("displayName", kb.get("key", "?")), kb.get("key", ""))
            for kb in (self._kbs or [])
        ]
        self.query_one("#kb-select", Select).set_options(kb_opts)
        if self.state.kb_key:
            try:
                self.query_one("#kb-select", Select).value = self.state.kb_key
            except Exception:
                pass

        errs = [e for e in (c_err, v_err, k_err) if e]
        if errs:
            status.update(f"[#d29922]Loaded with warnings: {'; '.join(errs)}[/]")
        else:
            status.update(
                f"[#3fb950]✓ {len(self._catalogs)} catalogs, "
                f"{len(self._volumes)} volumes, {len(self._kbs)} KBs[/]"
            )

    def on_select_changed(self, event: Select.Changed) -> None:
        sid = event.select.id
        val = event.value
        if sid == "catalog-select":
            self.state.catalog_key = val or ""
            cat = next((c for c in (self._catalogs or []) if c.get("key") == val), None)
            self.state.catalog_name = cat.get("displayName", "") if cat else ""
            self._refresh_schemas()
        elif sid == "schema-select":
            self.state.schema_key = val or ""
            sch = next((s for s in (self._schemas or []) if s.get("key") == val), None)
            self.state.schema_name = sch.get("displayName", "") if sch else ""
        elif sid == "volume-select":
            self.state.volume_key = val or ""
            vol = next((v for v in (self._volumes or []) if v.get("key") == val), None)
            self.state.volume_name = vol.get("displayName", "") if vol else ""
        elif sid == "kb-select":
            self.state.kb_key = val or ""
            kb = next((k for k in (self._kbs or []) if k.get("key") == val), None)
            self.state.kb_name = kb.get("displayName", "") if kb else ""
        self.state.save()

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
        if err:
            self.query_one("#res-status", Static).update(f"[#d29922]Schemas: {err}[/]")

    def on_next(self) -> bool:
        # Everything optional; just persist + advance.
        self.state.save()
        return True
