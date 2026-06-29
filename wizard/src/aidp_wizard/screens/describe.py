"""Step 3 — Describe the tool (LLM-assisted).

Comes right after AIDP auth so the LLM has the developer's intent before
any resource picks happen. The LLM does three things:

  1. Rewrites the user's free-form description into a precise spec.
  2. Suggests the best-fit template (file_reader / sql / rest / rag /
     llm_custom / echo_stub) plus reasoning.
  3. Proposes a PascalCase class name + a short display name.

These suggestions become pre-fills for later steps. The user can edit
them at any point — nothing's locked.
"""

from __future__ import annotations

import json
import re

from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.widgets import Static, TextArea, Button

from ._base import WizardScreen


KIND_VALUES = ("llm_custom", "file_reader", "sql", "rest", "rag", "echo_stub")


_SYSTEM_PROMPT = """You are an AIDP custom-tool design assistant. The
developer describes what they want to build; you respond with a single
JSON object (NO markdown fences, NO commentary) with exactly these keys:

{
  "refined_intent": "<rewritten 2-5 sentence spec — concrete inputs/outputs/data sources>",
  "suggested_kind": "<one of: llm_custom | file_reader | sql | rest | rag | echo_stub>",
  "kind_reasoning": "<one sentence: why this template fits>",
  "suggested_class_name": "<PascalCase, no spaces, e.g. PdfTableExtractor>",
  "suggested_display_name": "<2-4 words, e.g. 'PDF Table Extractor'>",
  "open_questions": ["<question 1>", "<question 2>"]
}

Pick llm_custom whenever the logic is non-trivial; the other templates
are only for very narrow shapes (single REST call, single SQL query,
read one file, search one KB). When uncertain, prefer llm_custom.
Class names must start with a letter and contain only A-Za-z0-9.
"""


class DescribeScreen(WizardScreen):
    TITLE = "Step 3 — Describe your tool (LLM-assisted)"
    SUBTITLE = ("Tell the wizard what you want to build. Click 'Refine with "
                "LLM' to get a structured spec + template suggestion. The "
                "rest of the wizard pre-fills from these answers.")

    def compose_body(self) -> ComposeResult:
        with Vertical():
            yield Static("What should this tool do?", classes="field-label")
            yield Static(
                "Free-form. The more concrete, the better the suggestions. "
                "Mention inputs, outputs, data sources, and any constraints.",
                classes="field-hint",
            )
            yield TextArea(text=self.state.tool_description or "",
                           id="raw-description")

            with Horizontal(id="describe-row"):
                yield Button("✨ Refine with LLM", id="refine", classes="-primary")
                yield Button("Skip refinement", id="skip", classes="-secondary")

            yield Static("LLM-refined spec (editable)", classes="field-label")
            yield Static("Used as the prompt in step 7 (Generate).",
                         classes="field-hint")
            yield TextArea(text=self.state.user_intent or self.state.tool_description or "",
                           id="refined-intent")

            yield Static("Suggested template + name", classes="field-label")
            yield Static(self._render_suggestion(), id="suggestion",
                         classes="field-hint")

            yield Static("", id="describe-status", classes="status-dim")

    def _render_suggestion(self) -> str:
        if not self.state.tool_kind and not self.state.tool_class_name:
            return "(click Refine with LLM to populate)"
        bits = []
        if self.state.tool_kind:
            bits.append(f"[bold]template:[/] {self.state.tool_kind}")
        if self.state.tool_class_name:
            bits.append(f"[bold]class:[/] {self.state.tool_class_name}")
        if self.state.tool_display_name:
            bits.append(f"[bold]display:[/] {self.state.tool_display_name}")
        return "   ".join(bits) or "(nothing suggested yet)"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "refine":
            self._do_refine()
            event.stop(); return
        if bid == "skip":
            # Copy raw -> refined so user can still proceed without LLM.
            raw = self.query_one("#raw-description", TextArea).text.strip()
            self.query_one("#refined-intent", TextArea).text = raw
            self.query_one("#describe-status", Static).update(
                "[#d29922]Skipped — the raw description will be used as the "
                "LLM prompt later. Template defaults to llm_custom.[/]"
            )
            if not self.state.tool_kind:
                self.state.tool_kind = "llm_custom"
            event.stop(); return
        super().on_button_pressed(event)

    def _do_refine(self) -> None:
        status = self.query_one("#describe-status", Static)
        raw = self.query_one("#raw-description", TextArea).text.strip()
        if not raw:
            status.update("[#f85149]Describe the tool first (top text box).[/]")
            return
        if not self.state.llm_api_key:
            status.update("[#f85149]No LLM key on file — go back to step 1.[/]")
            return

        status.update(f"[#58a6ff]Calling {self.state.llm_provider} "
                      f"({self.state.llm_model})...[/]")
        self.app.refresh()

        try:
            answer = _call_llm_for_refinement(
                provider=self.state.llm_provider,
                api_key=self.state.llm_api_key,
                model=self.state.llm_model,
                raw_description=raw,
            )
        except Exception as exc:
            self.app.log_file.error(f"DescribeScreen LLM call failed: {exc}")
            status.update(f"[#f85149]LLM call failed: {exc}[/]")
            return

        parsed, parse_err = _parse_llm_json(answer)
        if parse_err:
            self.app.log_file.warning(
                f"DescribeScreen LLM returned unparseable JSON: {parse_err}\n"
                f"Raw answer: {answer[:1000]}"
            )
            status.update(f"[#d29922]LLM response wasn't valid JSON — falling "
                          f"back to raw description. ({parse_err})[/]")
            self.query_one("#refined-intent", TextArea).text = raw
            return

        # Apply suggestions to state + UI.
        self.state.tool_description = raw
        self.state.user_intent = parsed.get("refined_intent", raw)
        kind = parsed.get("suggested_kind", "llm_custom")
        if kind not in KIND_VALUES:
            kind = "llm_custom"
        self.state.tool_kind = kind
        suggested_cls = re.sub(r"[^A-Za-z0-9]+", "",
                               str(parsed.get("suggested_class_name", "MyCustomTool")))
        if suggested_cls and suggested_cls[0].isalpha():
            self.state.tool_class_name = suggested_cls
        self.state.tool_display_name = str(parsed.get("suggested_display_name", "")
                                           or self.state.tool_display_name
                                           or "My Custom Tool")
        self.state.save()

        self.query_one("#refined-intent", TextArea).text = self.state.user_intent
        self.query_one("#suggestion", Static).update(self._render_suggestion())

        open_questions = parsed.get("open_questions") or []
        if open_questions:
            qs = " · ".join(f"[#58a6ff]?[/] {q}" for q in open_questions[:3])
            status.update(f"[#3fb950]✓ Refined.[/]  Open questions: {qs}")
        else:
            status.update(
                f"[#3fb950]✓ Refined.  Template: {self.state.tool_kind}, "
                f"class: {self.state.tool_class_name}.[/]"
            )

    def on_next(self) -> bool:
        status = self.query_one("#describe-status", Static)
        raw = self.query_one("#raw-description", TextArea).text.strip()
        refined = self.query_one("#refined-intent", TextArea).text.strip()
        if not raw and not refined:
            status.update("[#f85149]Describe the tool before moving on.[/]")
            return False
        self.state.tool_description = raw or refined
        self.state.user_intent = refined or raw
        if not self.state.tool_kind:
            self.state.tool_kind = "llm_custom"
        if not self.state.tool_class_name:
            self.state.tool_class_name = "MyCustomTool"
        if not self.state.tool_display_name:
            self.state.tool_display_name = "My Custom Tool"
        self.state.save()
        return True


# ---------------------------------------------------------------------------
# LLM call (shape-locked: structured JSON only)
# ---------------------------------------------------------------------------

def _call_llm_for_refinement(*, provider: str, api_key: str, model: str,
                             raw_description: str) -> str:
    user_msg = (f"Developer's free-form description:\n\n{raw_description}\n\n"
                "Reply with ONLY the JSON object specified in the system prompt.")
    if provider == "anthropic":
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=model, max_tokens=1024,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    if provider == "openai":
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model=model, max_tokens=1024,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
        )
        return resp.choices[0].message.content or ""
    raise RuntimeError(f"Unknown LLM provider: {provider}")


def _parse_llm_json(raw: str):
    """Strip ```json fences if present, then json.loads. Returns (obj, err)."""
    text = (raw or "").strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl >= 0:
            text = text[first_nl + 1:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].rstrip()
    try:
        obj = json.loads(text)
        if not isinstance(obj, dict):
            return None, f"expected JSON object, got {type(obj).__name__}"
        return obj, None
    except json.JSONDecodeError as e:
        return None, str(e)
