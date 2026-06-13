# AIDP Custom Code Tool — Template

A ready-to-edit starting point for building a Custom Code tool for AIDP Agent
Flows. Copy this folder, change four things, run `./build.sh`, upload the zip.

---

## 60-second quickstart

1. **Open `tool_implementation.py`.** Rename `MyTool` and fill in the four
   numbered steps inside `_execute_tool`.
2. **Open `tool_config.json`.** Set the tool's `description` (the model reads
   this to decide when to call it), list your inputs under `schema`, and put any
   settings under `conf`.
3. **Add any pip packages** to `requirements.txt` (skip ones already on the
   runtime — see the list below).
4. **Build and upload:**
   ```bash
   ./build.sh my_tool          # -> my_tool.zip
   ```
   Upload `my_tool.zip` in the workspace file browser, add a Custom Tool node,
   point it at the zip + class name, hit Test.

That's the whole loop. Everything below is reference.

---

## What each file is for

| File | What it does | Edit it? |
|------|--------------|----------|
| `tool_implementation.py` | Your tool class(es). Only `_execute_tool` is required. | **Yes — your logic** |
| `tool_config.json` | Display name, description, inputs (schema), settings (conf). | **Yes** |
| `requirements.txt` | Pip dependencies (the platform filters these). | If you have deps |
| `utils/config_utils.py` | The `get_cfg` helper. You usually don't touch this. | Rarely |
| `utils/__init__.py` | Makes `utils` importable. Keep it. | No |
| `config/settings.yaml` | Optional static config file. | Delete if unused |
| `wheels/` | Optional bundled `.whl` files for offline installs. | Only for offline |
| `build.sh` | Zips the package. | No |

---

## The contract (how a tool works)

Your class registers with `@CustomToolBase.register` and implements one method:

```python
@classmethod
def _execute_tool(cls, conf, runtime_params, **context_vars):
    ...
    return {"some_key": "some_value"}     # success
    return {"error": "what went wrong"}   # failure
```

- `runtime_params` — the inputs the model passed (these match `schema`).
- `conf` — your settings. **Read them with `get_cfg(conf, key, default)`**,
  not `conf[key]` (see gotcha #1).
- `context_vars` — system context like `datalake_id`. Optional.
- Return a **dict**. The framework wraps it for the model automatically.

---

## Worked example

`tool_implementation.py`:
```python
@CustomToolBase.register
class WordCountTool(CustomToolBase):
    @classmethod
    def _execute_tool(cls, conf, runtime_params, **context_vars):
        text = runtime_params.get("text", "")
        if not text.strip():
            return {"error": "text is required"}
        words = text.split()
        return {"word_count": len(words), "char_count": len(text)}
```

`tool_config.json` (the one tool entry):
```json
{
  "toolClassName": "WordCountTool",
  "displayName": "Word Count",
  "description": "Count the words and characters in a piece of text. Use when the user asks how long something is.",
  "version": "1.0.0",
  "schema": [
    {"name": "text", "type": "string", "description": "The text to measure."}
  ],
  "conf": {}
}
```

Build, upload, test with `text` = `"hello there world"` → `{"word_count": 3, "char_count": 17}`.

---

## Five gotchas (the reason this template exists)

1. **Settings live under `conf["conf"]`, not the top level.** Always use
   `get_cfg(conf, "key", default)` — it looks in the right place for you.
2. **Config numbers can arrive as strings.** Anything passed through a
   `{{template}}` variable comes in as text, so `"100" > 50` would crash.
   `get_cfg` converts the value to the type of your default, so `get_cfg(conf,
   "max", 100)` always gives you an int.
3. **Return `{"error": "..."}` on failure.** Not a success dict with an error
   message inside it. The framework turns `error` into `isError: true` so the
   model sees a real failure. (An ambiguous failure can make the model decide
   tools are "forbidden" and stop calling them for the rest of the chat.)
4. **The `description` in `tool_config.json` is for the model.** It's how the
   model decides whether to call your tool. Write what it does and when to use
   it — never leave it blank or paste UI placeholder text.
5. **Keep an `__init__.py` in every subfolder.** Otherwise `from .utils...`
   imports fail.

---

## Dependencies

Add pip packages to `requirements.txt`, one per line. The platform filters them:

- **Already on the runtime — don't list:** `requests`, `oci`, `oracledb`,
  `numpy`, `pydantic`, `jsonschema`, `aiohttp`, `httpx`, `sqlalchemy`,
  `cryptography`, `langchain-core`, `pyyaml`, `orjson`.
- **Blocked:** platform packages (`langgraph`, `langchain-core`), URL/VCS
  installs (`git+https://...`).
- **Everything else** (e.g. `humanize`, `beautifulsoup4`) installs at deploy.

**Test pods have no network.** If you want a dependency to work with the *Test*
button (not just after deploy), bundle a wheel:
```bash
# pure-python package:
pip download --dest wheels/ --no-deps humanize
# compiled package, target the Linux runtime:
pip download --dest wheels/ --platform manylinux2014_x86_64 \
  --python-version 3.11 --only-binary=:all: pandas
```
The platform installs from `wheels/` first, no network needed.

---

## After building

1. Upload the zip in the workspace file browser.
2. Add a **Custom Tool** node in the agent flow.
3. Set **Package Path** (the zip path), **Tool Class Name**, **Display Name**,
   **Description**, **Input Schema** — mirror what's in `tool_config.json`.
4. Click **Test Tool**, enter inputs, check the result.
5. **Deploy** when it works.

For the full platform reference, see `custom_tools_user_guide.md`.
