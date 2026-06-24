"""Step 1 — LLM bearer token.

Asks the user for an Anthropic or OpenAI API key, validates it by
hitting the provider's /models endpoint (no tokens consumed), stores
it in ~/.aidp/wizard.config.json. Required first because step 6
(generate) needs it; better to fail fast than at the end.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, RadioButton, RadioSet, Static

from ._base import WizardScreen
from ..llm_client import PROVIDER_DEFAULTS, validate_api_key


class LLMTokenScreen(WizardScreen):
    TITLE = "Step 1 — LLM bearer token"
    SUBTITLE = ("Paste the API key for the LLM the wizard will use to "
                "generate your custom-tool code. Validated before continuing.")

    def compose_body(self) -> ComposeResult:
        with Vertical():
            yield Static("Provider", classes="field-label")
            yield Static("Pick which LLM the wizard calls in step 6.", classes="field-hint")
            with RadioSet(id="provider"):
                yield RadioButton("Anthropic (Claude)", value=(self.state.llm_provider == "anthropic"),
                                  id="rb-anthropic")
                yield RadioButton("OpenAI (GPT / Codex)", value=(self.state.llm_provider == "openai"),
                                  id="rb-openai")

            yield Static("Model", classes="field-label")
            yield Static("Default is the latest available for the picked provider.",
                         classes="field-hint")
            yield Input(value=self.state.llm_model or "claude-opus-4-7",
                        id="model", placeholder="claude-opus-4-7 / gpt-5 / ...")

            yield Static("API key", classes="field-label")
            yield Static("Stored locally at ~/.aidp/wizard.config.json (chmod 0600). "
                         "Never committed, never uploaded.", classes="field-hint")
            yield Input(value=self.state.llm_api_key, id="api-key",
                        password=True,
                        placeholder="sk-ant-... or sk-proj-...")

            yield Static("", id="validation-status", classes="status-dim")

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        # Update default model when provider flips.
        if event.radio_set.id != "provider":
            return
        rb_id = event.pressed.id or ""
        provider = "anthropic" if rb_id == "rb-anthropic" else "openai"
        self.state.llm_provider = provider
        default_model = PROVIDER_DEFAULTS.get(provider, {}).get("model", "")
        self.query_one("#model", Input).value = default_model
        self.state.llm_model = default_model

    def on_next(self) -> bool:
        # Read current values.
        provider = self.state.llm_provider or "anthropic"
        model = self.query_one("#model", Input).value.strip()
        key = self.query_one("#api-key", Input).value.strip()
        status = self.query_one("#validation-status", Static)

        if not key:
            status.update("[#f85149]API key cannot be empty.[/]")
            return False

        status.update("[#58a6ff]Validating key with " + provider + "...[/]")
        self.app.refresh()
        ok, msg = validate_api_key(provider, key)
        if not ok:
            status.update(f"[#f85149]✗ {msg}[/]")
            return False

        # Persist + advance.
        self.state.llm_provider = provider
        self.state.llm_model = model or PROVIDER_DEFAULTS[provider]["model"]
        self.state.llm_api_key = key
        self.state.llm_validated = True
        self.state.save()
        status.update(f"[#3fb950]✓ {msg}[/]")
        self.status(f"LLM key validated ({provider} / {self.state.llm_model})", "info")
        return True
