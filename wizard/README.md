# AIDP Tool Wizard

A visual TUI wizard for building AIDP Custom Code tools end-to-end with
LLM-assisted code generation.

```
 ╔════════════════════════════════════════════════════════════════════╗
 ║  AIDP Tool Wizard                                       step 3 / 7 ║
 ╠════════════════════════════════════════════════════════════════════╣
 ║                                                                    ║
 ║  ✓ 1. LLM token        ┃  Pick the AIDP workspace                  ║
 ║  ✓ 2. AIDP auth        ┃                                           ║
 ║  ▶ 3. Workspace        ┃  ▸ Brenton_WS         active              ║
 ║    4. Resources        ┃    Construction_Demo  active              ║
 ║    5. Tool kind        ┃    Sales_QA           active              ║
 ║    6. Generate         ┃                                           ║
 ║    7. Build & deploy   ┃                                           ║
 ║                        ┃   [Back]              [Next →]            ║
 ╚════════════════════════════════════════════════════════════════════╝
```

## Install

```bash
cd wizard/
pip install -e .
```

## Run

```bash
aidp-wizard
```

## What it does

A 7-step wizard with a real graphical-style TUI (mouse + keyboard, themed,
form validation, syntax-highlighted code review):

| Step | What happens |
|---|---|
| 1. **LLM token** | Paste a Claude or OpenAI API key. Validated via a 1-token test call before continuing. Stored at `~/.aidp/wizard.config.json` so future runs skip this step. |
| 2. **AIDP auth** | Auto-detects OCI session token / API key from `~/.oci/config`. Confirms the wizard can reach AIDP REST endpoints. Falls back to `oci session authenticate` if no auth exists. |
| 3. **Workspace** | Lists workspaces from your AIDP tenancy. Pick one. |
| 4. **Resources** | Live picker for catalog / schema / volume / KB from the workspace. The dropdowns AIDP's Test panel doesn't have. |
| 5. **Tool kind** | Choose: file reader, SQL runner, REST caller, RAG search, or "custom (LLM-generated)". The first four use templates; the last hands off to step 6 with full freedom. |
| 6. **Generate** | If LLM-assisted: describe the tool in plain English. The wizard ships the description + your picked resources + the AIDP conventions to Claude/GPT, gets back a `tool_implementation.py`, validates via `ast.parse`. Review and iterate. |
| 7. **Build & deploy** | Runs Oracle's `build_custom_tool.py` to validate + package the zip. Optionally uploads to the workspace volume you picked in step 4. Done. |

## Visual style

Built on **Textual** (the modern Python TUI framework). Looks like a real
GUI wizard: header bar, numbered sidebar with checkmarks, themed form
widgets, footer with `Back` / `Next` / `Cancel` buttons and keyboard
hints (`↑/↓` navigate, `Enter` accept, `Ctrl+C` quit).

Oracle red accent on the active step. Dark theme by default. Works over
SSH. Pip-installable, no system-level dependencies.

## Notes

- The wizard uses the shared `aidp_io.py` helpers from the parent repo
  for AIDP REST calls. If you cloned just the wizard subdirectory,
  install the parent helpers via `pip install -e ..`.
- LLM API keys are stored locally in `~/.aidp/wizard.config.json`. Never
  committed, never uploaded.
- Built zip lands in `~/.aidp/wizard/dist/` by default.
